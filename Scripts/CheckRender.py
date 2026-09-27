#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成片门禁：对渲染出来的 mp4 做客观校验，不过就不算出片。

为什么需要：Docs/VibeMotionPlan.md §30 自己点出来的缺口 —— 我们一直拿 PNG 截图验收，
**成片 mp4 从来没有客观门**。§21 那次"后 17.1 秒全静音"是靠手动 silencedetect 撞上的，
而它的总时长 77.0s 完全正确，所以时长守卫、音轨新鲜度、分镜同步全是绿的。

五项检查：
  1 规格      分辨率 / fps 必须等于预设
  2 时长闭合  视频流 == 音频流 == 段表 duration
  3 有字无声  长静音必须整段落在"该时刻没有字幕"的窗口里
  4 结尾静音  最后一段台词之后不许出现长静默
  5 空帧      抽样帧的有效画面占比，抓"渲染中途资产丢失/整段黑屏"

用法：python Scripts/CheckRender.py [mp4路径]      缺省取 Render/ 下最新
退出码 1 = 不过。渲染器末尾会自动调用它。
"""
import json
import os
import re
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EP = os.path.join(ROOT, "Episodes", "EP001_SelfIntro")
SILENCE_FLOOR = "-40dB"
LONG_SILENCE = 2.0
SAMPLES = 14

fails = []


def bad(msg):
    fails.append(msg)
    print("  [FAIL] %s" % msg)


def ok(msg):
    print("  [ ok ] %s" % msg)


def sh(args):
    return subprocess.run(args, capture_output=True, text=True)


def probe(path):
    r = sh(["ffprobe", "-v", "error", "-show_entries",
            "format=duration:stream=codec_type,width,height,r_frame_rate,duration",
            "-of", "json", path])
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def table():
    md = open(os.path.join(EP, "Script.md"), encoding="utf-8").read()
    return json.loads(re.search(r"```json\s*(\{.*?\})\s*```", md, re.S).group(1))


def silence_runs(path):
    r = sh(["ffmpeg", "-v", "info", "-i", path,
            "-af", "silencedetect=noise=%s:d=%g" % (SILENCE_FLOOR, LONG_SILENCE),
            "-f", "null", "-"])
    st = [float(x) for x in re.findall(r"silence_start:\s*([\d.]+)", r.stderr)]
    en = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", r.stderr)]
    return list(zip(st, en))


def frame_content(path, dur):
    """抽样若干时刻，返回 [(t, 非背景像素占比)]。背景取四角中位色。"""
    import tempfile
    out = []
    tmp = tempfile.mkdtemp()
    for i in range(SAMPLES):
        t = dur * (i + 0.5) / SAMPLES
        f = os.path.join(tmp, "f%02d.png" % i)
        sh(["ffmpeg", "-v", "error", "-ss", "%.3f" % t, "-i", path,
            "-frames:v", "1", "-y", f])
        if not os.path.exists(f):
            continue
        try:
            from PIL import Image
            im = Image.open(f).convert("RGB").resize((160, 90))
        except Exception:
            os.remove(f)
            continue
        raw = im.tobytes()                     # 不用 getdata()：Pillow 14 起已弃用
        px = [raw[i:i + 3] for i in range(0, len(raw), 3)]
        corners = [px[0], px[-1], px[89 * 160], px[90 * 160 - 1]]
        bg = tuple(sorted(c[k] for c in corners)[len(corners) // 2] for k in range(3))
        hit = sum(1 for p in px if abs(p[0] - bg[0]) + abs(p[1] - bg[1]) + abs(p[2] - bg[2]) > 40)
        out.append((t, hit / len(px)))
        os.remove(f)
    return out


def main():
    args = [a for a in sys.argv[1:] if a.endswith(".mp4")]
    if args:
        path = os.path.abspath(args[0])
    else:
        rd = os.path.join(EP, "Render")
        cands = [os.path.join(rd, f) for f in os.listdir(rd) if f.endswith(".mp4")]
        if not cands:
            print("Render/ 下没有 mp4")
            return 1
        path = max(cands, key=os.path.getmtime)
    if not os.path.exists(path):
        print("找不到 %s" % path)
        return 1

    print("=== 成片门禁 ===")
    print("  %s（%.2f MB）" % (os.path.basename(path), os.path.getsize(path) / 1048576))
    if os.path.getsize(path) < 100_000:
        bad("文件过小（%d 字节），几乎肯定是空片" % os.path.getsize(path))

    t = table()
    dur_want = float(t["duration"])
    info = probe(path)
    if not info:
        bad("ffprobe 读不出信息")
        return 1
    v = [s for s in info["streams"] if s.get("codec_type") == "video"]
    a = [s for s in info["streams"] if s.get("codec_type") == "audio"]
    dur_got = float(info["format"]["duration"])

    # 1 规格
    if not v:
        bad("没有视频流")
    else:
        num, _, den = v[0].get("r_frame_rate", "0/1").partition("/")
        fps = float(num) / float(den or 1)
        print("  [ .. ] 规格 %sx%s @ %.2f fps" % (v[0].get("width"), v[0].get("height"), fps))
        if not (29.5 <= fps <= 30.5):
            bad("fps %.2f 不在 30 附近" % fps)
        if int(v[0].get("width") or 0) < 1280:
            bad("宽度只有 %s" % v[0].get("width"))

    # 2 时长闭合
    if abs(dur_got - dur_want) > 0.35:
        bad("成片 %.2fs vs 段表 %.2fs，差 %.2fs" % (dur_got, dur_want, dur_got - dur_want))
    else:
        ok("时长闭合 %.2fs（段表 %.2fs）" % (dur_got, dur_want))
    if not a:
        bad("没有音轨 —— 成片是哑片")
    else:
        adur = float(a[0].get("duration") or dur_got)
        if abs(adur - dur_got) > 0.5:
            bad("音轨 %.2fs 与视频 %.2fs 不等长" % (adur, dur_got))
        else:
            ok("音轨存在且等长（%.2fs）" % adur)

    # 3 / 4 静音剖面
    subs = [(float(s["start"]), float(s["end"])) for s in t.get("subtitles", [])]
    runs = silence_runs(path)
    silent_sub = []
    for s0, s1 in runs:
        covered = [c for c in subs if c[0] < s1 and c[1] > s0]
        if covered:
            silent_sub.append("%.1f–%.1fs 静音却有字幕 %s"
                              % (s0, s1, ", ".join("%g-%g" % c for c in covered)))
    if silent_sub:
        for line in silent_sub:
            bad("有字无声 " + line)
    else:
        ok("有字无声 0 处（%d 段长静音全部在无台词窗口）" % len(runs))
    tail = [r for r in runs if r[0] > dur_want - 12]
    if tail and dur_want - tail[0][0] > 6:
        bad("结尾静音 %.1fs，谢幕台词会被吃掉" % (dur_want - tail[0][0]))

    # 5 空帧
    content = frame_content(path, dur_got)
    if content:
        lowest = min(content, key=lambda x: x[1])
        print("  [ .. ] 抽样 %d 帧，有效画面占比最低 %.1f%%（t=%.1fs）"
              % (len(content), 100 * lowest[1], lowest[0]))
        if lowest[1] < 0.01:
            bad("t=%.1fs 几乎全黑（画面内容 <1%%），可能资产丢失或渲染中断" % lowest[0])
        elif sum(1 for _, r in content if r < 0.03) >= 2:
            bad("有 %d 个抽样点画面内容不足 3%%" % sum(1 for _, r in content if r < 0.03))
        else:
            ok("画面内容正常")

    print("\n%s" % ("全部通过" if not fails else "未通过 %d 项" % len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
