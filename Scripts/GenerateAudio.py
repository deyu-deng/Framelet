"""
GenerateAudio.py — EP001 配音合成管线 (MiniMax T2A + Voice Cloning)

两种运行模式:
  --clone   首次运行: 上传参考音频 → 声音复刻 → 用复刻 voice_id 合成 5 段配音
  (默认)    后续运行: 直接用 .env 里已有的 MINIMAX_VOICE_ID 合成 5 段配音

所有凭证从 .env 或环境变量读取，绝不硬编码。.env 在 .gitignore 中。

使用流程:
  1. 复制 .env.example 为 .env, 填入 MINIMAX_API_KEY / MINIMAX_GROUP_ID
  2. 按 Docs/EP001_VoiceSampleScript.md 录 35-50s 参考音频, 存到 VOICE_SAMPLE_PATH
  3. python Scripts/GenerateAudio.py --clone     # 首次: 复刻 + 合成
  4. 把返回的 voice_id 填到 .env 的 MINIMAX_VOICE_ID
  5. python Scripts/GenerateAudio.py             # 后续: 仅合成
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# 让本脚本能直接 import 同目录下的兄弟模块（Texture / Rig 等同款用法）
sys.path.insert(0, str(Path(__file__).resolve().parent))
# （以前这里 import script_table 读 Script.md 的 vo 字段。配音先行之后上游换了：
#   台词 = Voice.en.md，时长 = 实测出来的 timing.json。
#   Script.md 是这两者的**产物**，拿它当配音的输入就是本末倒置，故删除该依赖。


# =============================================================================
# 1. 凭证与路径加载 (Configuration)
# =============================================================================
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

# ⚠️ 单一事实来源 = Voice.en.md（台词）+ 本脚本实测出的 timing.json（时长）。
# 本脚本不硬编码任何 VO 台词 / 时段。改稿只改 Voice.en.md，严禁在这里再写死。
# 每段开头的 emotion 标签仅 speech-2.8-hd / turbo 支持。


GAP_SEC = 0.35          # 段间气口
FADE_SEC = 0.5          # 段尾淡出预留：必须落在静音上，不能吃掉台词
LEAD_IN_SEC = 4.5       # 开场：先有画面动作再开口
VOICE_EN = PROJECT_ROOT / "Episodes" / "EP001_SelfIntro" / "Voice.en.md"
TIMING_OUT = PROJECT_ROOT / "Episodes" / "EP001_SelfIntro" / "timing.json"


def head_silence(path, db="-45dB"):
    """这一段音频里，第一声之前有多少秒静音。

    TTS 每段开头都带 0.12~0.18 s 的空白。字幕和口型按"段起点"开，
    就等于每句话都先于声音 0.15 s 出现在屏幕上 —— CheckSync 的
    "有字无声"就是这么逮到的（0.0–4.7 s 静音，但 4.5 s 字幕已经挂上）。
    """
    import subprocess as sp
    try:
        err = sp.run(["ffmpeg", "-i", str(path), "-af",
                      "silencedetect=noise=%s:d=0.05" % db,
                      "-f", "null", "-"], capture_output=True, text=True).stderr
    except Exception:
        return 0.0
    m = re.search(r"silence_start: 0(?:\.0+)?\s+.*?silence_end: ([\d.]+)", err, re.S)
    return round(float(m.group(1)), 3) if m else 0.0


def measure(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return float(out.stdout.strip())


def load_segments():
    """从 Voice.en.md 读口播稿 —— 配音先行的正解：音频是时间线的上游。

    旧实现读 Script.md 的 vo 字段，那是"先定时段、再往窗口里塞台词"的顺序，
    结果装不下就被 afade 淡成静音，三句话从未发声（含推 Aura 那句）。
    现在稿子有几段就合成几段，时段由实测时长反算，时间线反过来消费它。
    """
    if not VOICE_EN.exists():
        raise SystemExit(f"找不到口播稿 {VOICE_EN}；配音先行流程下它是唯一输入")
    segs = []
    for raw in VOICE_EN.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^\[(原|写|AI)\]\s*", "", line)     # 溯源标签不念出来
        segs.append({"file": f"seg{len(segs) + 1}.wav", "text": line,
                     "emotion": "happy", "start": None, "end": None, "delay_ms": 0})
    if not segs:
        raise SystemExit("口播稿是空的")
    return segs, None


def assign_timeline(segs, audio_dir):
    """合成完之后用实测时长反算时段。返回总时长。"""
    t = LEAD_IN_SEC
    for s_ in segs:
        d = measure(Path(audio_dir) / s_["file"])
        s_["dur"] = round(d, 3)
        # 段尾必须比音频多留 FADE_SEC —— 否则 afade 会淡掉最后半秒台词。
        # 这条正是 mix_vo 里那个超窗守卫抓出来的：它拦下了我自己刚写的反算逻辑。
        s_["start"] = round(t, 3)
        s_["end"] = round(t + d + FADE_SEC, 3)
        s_["delay_ms"] = int(s_["start"] * 1000)
        t = s_["end"] + GAP_SEC
    return round(t - GAP_SEC, 3)


# MiniMax T2A 同步合成端点（注意：必须把 GroupId 作为 query 参数，不能只在 header）
T2A_URL = "https://api.minimaxi.com/v1/t2a_v2"
VOICE_CLONE_URL = "https://api.minimaxi.com/v1/voice_clone"
FILE_UPLOAD_URL = "https://api.minimaxi.com/v1/files/upload"

# 速率/音调/采样率等合成参数
T2A_PARAMS = {
    "speed": 1.0,
    "vol": 1.0,
    "pitch": 0,
    "sample_rate": 32000,
    "bitrate": 128000,
    "format": "mp3",
    "channel": 1,
}


# =============================================================================
# 2. 凭证加载：.env → 环境变量，凭证不硬编码
# =============================================================================
def load_credentials():
    """从 .env 文件加载凭证到环境变量（如果尚未设置）。

    优先级：已有环境变量 > .env 文件 > 命令行（无）。
    """
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            # 不覆盖已有环境变量（更高优先级）
            if key and val and key not in os.environ:
                os.environ[key] = val
    else:
        print(f"[!] .env not found at {ENV_FILE}")
        print("    Copy .env.example to .env and fill in your credentials.")

    required = ["MINIMAX_API_KEY", "MINIMAX_GROUP_ID"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"[X] Missing required env vars: {missing}")
        print("    Set them in .env or your shell environment.")
        sys.exit(1)

    return {
        "api_key": os.environ["MINIMAX_API_KEY"],
        "group_id": os.environ["MINIMAX_GROUP_ID"],
        "voice_id": os.environ.get("MINIMAX_VOICE_ID", "").strip(),
        "sample_path": os.environ.get(
            "VOICE_SAMPLE_PATH",
            "Episodes/EP001_SelfIntro/Audio/my_voice_sample.wav",
        ),
        "tts_model": os.environ.get("MINIMAX_TTS_MODEL", "speech-2.8-hd"),
    }


# =============================================================================
# 3. MiniMax API 调用层（纯 urllib，零额外依赖）
# =============================================================================
def _request(url, *, headers=None, json_body=None, files=None, timeout=60, raise_on_error=False):
    """统一的 HTTP 请求封装，JSON 返回 / 错误透传。

    raise_on_error=True 时不 sys.exit 而是抛异常，让调用者自己处理（用于重试）。
    """
    # 关键：清空 HTTPS_PROXY 环境变量（与 .workbuddy memory 一致）
    # 此项目 .workbuddy 配置了 127.0.0.1:12006 的代理，但该代理可能未运行
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ("http_proxy", "https_proxy")}
    # 构造请求
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        req_headers = {"Content-Type": "application/json"}
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
    else:
        req = urllib.request.Request(url, headers=headers or {}, method="POST")
        if files:
            # multipart/form-data 手动构造
            boundary = "----MiniMaxBoundary" + os.urandom(8).hex()
            req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
            body = b""
            for k, v in (files.get("data") or {}).items():
                body += f"--{boundary}\r\n".encode()
                body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
                body += f"{v}\r\n".encode()
            for f in (files.get("files") or []):
                field, filename, content = f
                body += f"--{boundary}\r\n".encode()
                body += f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode()
                body += b"Content-Type: application/octet-stream\r\n\r\n"
                body += content + b"\r\n"
            body += f"--{boundary}--\r\n".encode()
            req.data = body

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if raise_on_error:
            raise
        print(f"[X] HTTP {e.code} from {url}")
        print(f"    {body}")
        sys.exit(1)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        if raise_on_error:
            raise
        print(f"[X] Network error: {e}")
        sys.exit(1)


def upload_clone_audio(sample_path, api_key, group_id):
    """步骤 1: 上传参考音频用于声音复刻。

    Returns: file_id (int64)
    """
    p = Path(sample_path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    if not p.exists():
        print(f"[X] Voice sample not found: {p}")
        print("    按 Docs/EP001_VoiceSampleScript.md 录制后放到该路径。")
        sys.exit(1)

    url = f"{FILE_UPLOAD_URL}?GroupId={urllib.parse.quote(str(group_id))}"
    print(f"[*] Uploading voice sample: {p} ({p.stat().st_size / 1024:.1f} KB)")

    with open(p, "rb") as f:
        resp = _request(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            files={
                "data": {"purpose": "voice_clone"},
                "files": [("file", p.name, f.read())],
            },
        )
    file_id = resp.get("file", {}).get("file_id")
    if not file_id:
        print(f"[X] Upload failed: {resp}")
        sys.exit(1)
    print(f"[+] Uploaded. file_id = {file_id}")
    return file_id


def clone_voice(file_id, voice_id, api_key, group_id, model="speech-2.8-hd", max_retries=2):
    """步骤 2: 调用 /v1/voice_clone 创建可复用的 voice_id。

    voice_clone 是慢操作（30s-2min），失败自动重试 1 次。

    Returns: 复刻后的 voice_id
    """
    import time
    url = f"{VOICE_CLONE_URL}?GroupId={urllib.parse.quote(str(group_id))}"
    print(f"[*] Cloning voice from file_id={file_id} (model={model}, timeout=180s) ...")
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = _request(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                json_body={
                    "file_id": file_id,
                    "voice_id": voice_id,
                    "model": model,
                    "need_noise_reduction": True,
                    "need_volume_normalization": True,
                },
                timeout=180,
                raise_on_error=True,
            )
            if resp.get("base_resp", {}).get("status_code") != 0:
                last_err = resp
                print(f"[!] Clone API error on attempt {attempt}: {resp.get('base_resp')}")
                if attempt < max_retries:
                    print(f"[*] Retrying in 5s ...")
                    time.sleep(5)
                continue
            print(f"[+] Voice cloned. voice_id = {voice_id}")
            print(f"    ⚠️  Save this voice_id to .env as MINIMAX_VOICE_ID")
            print(f"    ⚠️  Use it in TTS within 168 hours or it will be deleted")
            return voice_id
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            print(f"[!] Network error on attempt {attempt}: {e}")
            if attempt < max_retries:
                print(f"[*] Retrying in 5s ...")
                time.sleep(5)

    print(f"[X] Clone failed after {max_retries} attempts")
    if last_err is not None:
        print(f"    Last error: {last_err}")
    sys.exit(1)


def synthesize_segment(text, voice_id, out_path, api_key, group_id,
                       model="speech-2.8-hd", emotion=None):
    """步骤 3: 同步 T2A 合成一段音频，hex 解码后写入文件。"""
    url = f"{T2A_URL}?GroupId={urllib.parse.quote(str(group_id))}"
    voice_setting = {
        "voice_id": voice_id,
        "speed": T2A_PARAMS["speed"],
        "vol": T2A_PARAMS["vol"],
        "pitch": T2A_PARAMS["pitch"],
    }
    if emotion:
        voice_setting["emotion"] = emotion
    audio_setting = {
        "sample_rate": T2A_PARAMS["sample_rate"],
        "bitrate": T2A_PARAMS["bitrate"],
        "format": T2A_PARAMS["format"],
        "channel": T2A_PARAMS["channel"],
    }
    payload = {
        "model": model,
        "text": text,
        "stream": False,
        "language_boost": "auto",
        "output_format": "hex",
        "voice_setting": voice_setting,
        "audio_setting": audio_setting,
    }
    resp = _request(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        json_body=payload,
        timeout=120,
    )
    if resp.get("base_resp", {}).get("status_code") != 0:
        print(f"[X] TTS failed: {resp}")
        sys.exit(1)
    # 响应结构: data.audio 是 hex 编码的 MP3
    audio_hex = resp.get("data", {}).get("audio")
    if not audio_hex:
        print(f"[X] TTS returned no audio: {resp}")
        sys.exit(1)
    audio_bytes = bytes.fromhex(audio_hex)
    Path(out_path).write_bytes(audio_bytes)
    print(f"[+] {Path(out_path).name}: {len(audio_bytes) / 1024:.1f} KB")


# =============================================================================
# 4. ffmpeg 混合：N 段配音 → total_duration 秒 vo_77s.mp3
#    （时长由实测音频相加决定，本函数不写死任何 "60" 魔法数）
# =============================================================================
def mix_vo(segment_files, output_mp3, segments, total_duration):
    """按实测时长用 afade 拼配音：每段在窗口结束前 0.5s 淡出，
    非首段在阶段开始时 0.3s 淡入（沿用原有做法）。无重叠、无硬切。
    解决之前 amix 叠加 + volume=enable 不生效导致的"双声"问题。

    关键：delay_ms 与时段窗口全部由传进来的 segments 的 start/end 推导，
    不再依赖任何硬编码的 stage_audio_windows 或 SEGMENTS 全局变量。
    """
    parts = []
    inputs = []

    # ---- 硬守卫：每段音频必须装得进自己的时段窗口 ----
    # afade=t=out 的语义是"淡到零之后保持静音"，所以超窗的部分不是被压缩，
    # 是整句从未被说出来。此前这里静默淡出：seg4 结尾
    # "Welcome to use the HarmonyOS mood-recording software I just made."
    # ——推 Aura 的那句，也就是这条视频存在的理由——在成片里根本没发声，
    # 而成片总时长 77.0s 完全正确，没有任何一处报错。
    # 见 Docs/VibeMotionPlan.md §4 债 #4 与 Scripts/CheckSync.py。
    over = []
    for path, seg in zip(segment_files, segments):
        win = float(seg["end"]) - float(seg["start"]) - 0.5      # 段尾留 0.5s 淡出
        try:
            d = float(subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(path)],
                capture_output=True, text=True).stdout.strip())
        except ValueError:
            d = None
        if d is not None and d > win + 0.02:   # 浮点边界：等值不算超窗
            over.append("  %s–%gs  %s  音频 %.1fs > 可用 %.1fs → 尾部 %.1fs 会被淡成静音"
                        % (seg["start"], seg["end"], os.path.basename(str(path)), d, win, d - win))
    if over:
        print("[-] 配音超窗，拒绝混音（宁可不出片，不出'时长对但话没说完'的废片）：", flush=True)
        for line in over:
            print(line, flush=True)
        print("    处理：改 Script.md 段表把窗口放宽到实测时长（动画阈值从段表推导，会自动跟上），"
              "\n    或按原稿精简台词；然后重跑本脚本，并用 python Scripts/CheckSync.py 复核。", flush=True)
        sys.exit(1)

    for i, (path, seg) in enumerate(zip(segment_files, segments)):
        inputs.extend(["-i", str(path)])
        # 由段表推导：入场延迟 = start，时段窗口 = [start, end]
        win_start = float(seg["start"])
        win_end = float(seg["end"])
        delay_ms = int(win_start * 1000)
        fade_out_start = win_end - 0.5
        if i == 0:
            # 首段不淡入（attack 直接进场），仅段尾 0.5s 淡出
            parts.append(
                f"[{i}:a]adelay={delay_ms}|{delay_ms},"
                f"afade=t=out:st={fade_out_start}:d=0.5[a{i}]"
            )
        else:
            # 段首 0.3s 淡入（st=段起始时刻，真正作用在音频 onset 上）+ 段尾 0.5s 淡出
            parts.append(
                f"[{i}:a]adelay={delay_ms}|{delay_ms},"
                f"afade=t=in:st={win_start}:d=0.3,"
                f"afade=t=out:st={fade_out_start}:d=0.5[a{i}]"
            )
    mix_inputs = "".join(f"[a{i}]" for i in range(len(segments)))
    # amix 默认 normalize=1：把每一路除以输入总数再相加。这四段**不重叠**
    # （上面那道超窗守卫就是保证这件事），所以除以 4 纯粹是白白削 6~7 dB。
    # 实测：说话窗峰值中位 -16.2 → normalize=0 之后 -9.1 dBFS。
    #
    # 后面那两级是把"人声该有的响度"补齐，不是把音量拧大：
    #   acompressor 3:1 压掉最响那几个字，让整体更密，loudnorm 就不用猛推；
    #   loudnorm 按 EBU R128 归一到 -16 LUFS（B站/YouTube 一档），真峰限在 -1.5 dBTP。
    # 实测整条链：-28.1 → -21.7（只修 amix）→ -16.8 LUFS（加上这两级）。
    filter_complex = (
        ";".join(parts)
        + f";{mix_inputs}amix=inputs={len(segments)}:duration=longest"
          f":dropout_transition=0:normalize=0"
        + ",acompressor=threshold=-18dB:ratio=3:attack=20:release=250"
        + ",loudnorm=I=-16:TP=-1.5:LRA=11"
        + f",apad=whole_dur={total_duration}[aout]"
    )
    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", filter_complex,
        "-map", "[aout]",
        "-t", str(total_duration),
        # 48k 单声道 mp3 是有损中间产物，而且它是"权威终版音轨"——太亏。
        "-codec:a", "libmp3lame", "-b:a", "192k",
        str(output_mp3),
    ]
    print(f"[*] Mixing {len(segments)} VO segments to {output_mp3} "
          f"(total {total_duration}s, afade-gated per Script.md stage) ...")
    subprocess.run(cmd, check=True)
    print(f"[+] Done: {output_mp3}")
# =============================================================================
# 5. 主流程
# =============================================================================
def main():
    # Windows 兜底：PowerShell 默认 GBK，会让 emoji/中文 print 炸 UnicodeEncodeError
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    # ⚠️ 关键：清空 HTTP_PROXY/HTTPS_PROXY 环境变量（与 .workbuddy 配置一致）
    # 127.0.0.1:12006 代理可能未运行，会导致连不上 MiniMax
    for k in list(os.environ):
        if k.lower() in ("http_proxy", "https_proxy"):
            os.environ.pop(k)

    parser = argparse.ArgumentParser(
        description="EP001 配音合成管线 (MiniMax T2A + Voice Cloning)"
    )
    parser.add_argument(
        "--remeasure",
        action="store_true",
        help="不花钱：只重测已有 seg*.wav 的时长与开头静音，重写 timing.json",
    )
    parser.add_argument(
        "--clone",
        action="store_true",
        help="首次运行：上传参考音频 + 声音复刻 + 合成 5 段",
    )
    parser.add_argument(
        "--voice-id",
        default=None,
        help="自定义 voice_id（仅 --clone 模式生效），默认 plobi_voice_<日期>",
    )
    parser.add_argument(
        "--out-dir",
        default="Episodes/EP001_SelfIntro/Audio",
        help="输出目录（默认 EP001_SelfIntro/Audio）",
    )
    parser.add_argument(
        "--remix", action="store_true",
        help="不花钱、不调 API：拿已有的 seg*.wav 按修好的混音链重出终版音轨。"
             "改了混音参数（响度、amix、淡入淡出）之后用这个，不用重新配音。",
    )
    args = parser.parse_args()

    creds = load_credentials()
    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = PROJECT_ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    voice_id = creds["voice_id"]
    if args.clone:
        # ---- 复刻模式 ----
        from datetime import datetime
        if voice_id and not args.voice_id:
            # 已存有 voice_id 且未显式传新值 → 避免撞 MiniMax 去重
            print(f"[!] MINIMAX_VOICE_ID='{voice_id}' already in .env")
            print("    MiniMax 不允许 voice_id 重复，复刻会失败。")
            print("    请用 --voice-id plobi_voice_<新日期> 显式指定，或先从 .env 删掉这行。")
            sys.exit(1)
        voice_id = args.voice_id or f"plobi_voice_{datetime.now().strftime('%Y%m%d')}"
        file_id = upload_clone_audio(creds["sample_path"], creds["api_key"], creds["group_id"])
        clone_voice(file_id, voice_id, creds["api_key"], creds["group_id"],
                    model=creds["tts_model"])
        # 提示用户保存 voice_id
        print()
        print("=" * 60)
        print(f"  请将以下行写入 .env:")
        print(f"  MINIMAX_VOICE_ID={voice_id}")
        print("=" * 60)
        print()
    elif not voice_id:
        print("[X] MINIMAX_VOICE_ID not set in .env")
        print("    首次运行请加 --clone 参数完成声音复刻。")
        sys.exit(1)

    # ---- 从口播稿读取段（配音先行：音频是上游）----
    segs, _ = load_segments()

    # ---- 只重测：不花钱、不调 API，量已有 seg*.wav 的时长与开头静音 ----
    if args.remeasure:
        import json as _json
        missing = [str(out_dir / ("seg%d.wav" % (i + 1))) for i in range(len(segs))
                   if not (out_dir / ("seg%d.wav" % (i + 1))).exists()]
        if missing:
            print("[-] --remeasure 要的分句素材不在：%s" % ", ".join(missing))
            return 1
        total = assign_timeline(segs, out_dir)
        for s_ in segs:
            s_["lead"] = head_silence(out_dir / s_["file"])
        TIMING_OUT.write_text(_json.dumps(
            {"fps": 30, "duration": total, "lead_in": LEAD_IN_SEC, "gap": GAP_SEC,
             "speech": [{"file": s_["file"], "text": s_["text"], "dur": s_["dur"],
                         "start": s_["start"], "end": s_["end"], "lead": s_["lead"]}
                        for s_ in segs]},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print("[+] 只重测了时长与开头静音，没调用任何付费接口：%s" % TIMING_OUT.name)
        print("    各段开头静音 %s s" % [s_["lead"] for s_ in segs])
        return 0

    # ---- 只重混：素材不变，换混音链（响度/淡入淡出/amix）时用 ----
    if args.remix:
        seg_paths = [out_dir / ("seg%d.wav" % (i + 1)) for i in range(len(segs))]
        missing = [str(p) for p in seg_paths if not p.exists()]
        if missing:
            print("[-] --remix 要的分句素材不在：%s" % ", ".join(missing))
            return 1
        total = assign_timeline(segs, out_dir)
        for s_ in segs:
            s_["lead"] = head_silence(out_dir / s_["file"])
        import json as _json
        TIMING_OUT.write_text(_json.dumps(
            {"fps": 30, "duration": total, "lead_in": LEAD_IN_SEC, "gap": GAP_SEC,
             "speech": [{"file": s_["file"], "text": s_["text"], "dur": s_["dur"],
                         "start": s_["start"], "end": s_["end"], "lead": s_["lead"]}
                        for s_ in segs]},
            ensure_ascii=False, indent=2), encoding="utf-8")
        mix_vo(seg_paths, out_dir / f"vo_{int(round(total))}s.mp3", segs, total)
        print("[+] 只重混了音轨，没调用任何付费接口。成片用 -c:v copy 重新封装即可，"
              "\n    不需要重渲 15 分钟。")
        return 0

    # ---- TTS 合成各段 ----
    print(f"[*] Synthesizing {len(segs)} segments from Voice.en.md "
          f"with voice_id={voice_id} ... (时段稍后由实测反算)")
    seg_paths = []
    for seg in segs:
        out_path = out_dir / seg["file"]
        synthesize_segment(
            seg["text"], voice_id, out_path,
            creds["api_key"], creds["group_id"],
            model=creds["tts_model"],
            emotion=seg.get("emotion"),
        )
        seg_paths.append(out_path)

    # ---- 实测时长反算时段，再把时间线交回给 BuildScript 消费 ----
    total_duration = assign_timeline(segs, out_dir)
    print(f"[+] 时段由实测反算：总长 {total_duration}s，"
          f"各段 {[round(s['dur'],2) for s in segs]}")
    import json as _json
    TIMING_OUT.write_text(_json.dumps(
        {"fps": 30, "duration": total_duration, "lead_in": LEAD_IN_SEC, "gap": GAP_SEC,
         "speech": [{"file": s["file"], "text": s["text"], "dur": s["dur"],
                     "start": s["start"], "end": s["end"],
                     "lead": head_silence(out_dir / s["file"])} for s in segs]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[+] 时间线已写出 {TIMING_OUT.name}")

    # ---- 按实测时段混合 ----
    # 输出文件名随 duration 走（77s → vo_77s.mp3），不再写死 60。
    # ⚠️ 注意区分：
    #   * vo_77s.mp3  —— 本脚本产物，是用 Script.md 新文案重新 TTS 合成的【权威终版音轨】。
    #   * vo_77s_preview.mp3 —— 临时过渡产物（旧配音 seg1..seg5.wav + 新时间轴混出），
    #     仅用于今晚审片占位，**不等于最终音轨**，由单独的 ffmpeg 命令生成、不在本脚本内产出。
    mix_vo(seg_paths, out_dir / f"vo_{int(round(total_duration))}s.mp3", segs, total_duration)
    print(f"\n[✓] All done. Output: {out_dir}")


if __name__ == "__main__":
    main()
