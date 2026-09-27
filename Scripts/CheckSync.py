#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音画文稿一致性检查：把"改了一处、另外三处没跟上"变成立刻可见的失败。

来历（都是 2026-09-21 手查出来的，此前没有任何自动拦截）：
  · 渲染器写死 vo_60s.mp3，60 s 音轨塞进 77 s 画面，apad 静默补白
    → 成片 59.9 s 之后连续 17.1 s 全静音，谢幕整段没声音
  · Script.md 20:42 改过台词，音轨 18:51 合成
    → 43.5–50.2 s 有字无声 6.7 s
  · 同一集曾同时存在三份不一致台词（JSON 段表 / 手写分镜 / GenerateAudio 副本）

对应 Docs/VibeMotionPlan.md §4 技术债 #4。

用法：
    python Scripts/CheckSync.py                     # 默认 EP001
    python Scripts/CheckSync.py --video Render/x.mp4
退出码 1 = 有不一致。
"""
import argparse
import importlib.util
import io
import json
import os
import re
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SILENCE_FLOOR = "-40dB"      # 与人工排查时同一判据
LONG_SILENCE = 2.0           # 超过这个长度（秒）的静音才单独追究
ACTIVE_SUB_W = 1100        # 字幕 PNG 画布宽（RenderVideo.prebake_subs 的 sub_w），超出即被截

fails = []
warns = []


def bad(section, msg):
    fails.append((section, msg))
    print("  [FAIL] %-14s %s" % (section, msg))


def note(section, msg):
    warns.append((section, msg))
    print("  [warn] %-14s %s" % (section, msg))


def ok(section, msg):
    print("  [ ok ] %-14s %s" % (section, msg))


def sh(args):
    return subprocess.run(args, capture_output=True, text=True)


def probe_duration(path):
    r = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "csv=p=0", path])
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def has_frame_index(path):
    """这个 mp4 有没有帧索引（moov）。渲染途中的、和 --watchable 出的分片片都没有。

    为什么门禁要挑开它：分片写法省掉索引换来"边渲边能播"，代价是解码起步晚
    约 0.07 s。同一份音轨实测 —— 普通 mp4 量到首段静音 0.00–4.70 s，
    分片量到 0.03–4.76 s。字幕 4.68 s 起，于是 0.05 s 容差被顶穿，
    报一条根本不存在的"有字无声"。顺手还挡住了另一件事：
    没写完的半成品不会被当成"最新成片"来检。
    """
    r = sh(["ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=nb_frames", "-of", "csv=p=0", path])
    return r.stdout.strip() not in ("", "N/A")


def silence_runs(path):
    """返回 [(start, end), ...] 的长静音区间。"""
    r = sh(["ffmpeg", "-v", "info", "-i", path,
            "-af", "silencedetect=noise=%s:d=%g" % (SILENCE_FLOOR, LONG_SILENCE),
            "-f", "null", "-"])
    text = r.stderr
    starts = [float(x) for x in re.findall(r"silence_start:\s*([\d.]+)", text)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", text)]
    return list(zip(starts, ends))


def parse_script(md_path):
    """读 Script.md：JSON 段表 + 手写分镜的 [EN]/[ZH] + 创作者原稿手记。"""
    src = open(md_path, encoding="utf-8").read()
    m = re.search(r"```json\s*(\{.*?\})\s*```", src, re.S)
    table = json.loads(m.group(1)) if m else None

    storyboard = []
    for sec in re.finditer(r"### \[(\d\d):(\d\d) - (\d\d):(\d\d)\]([^\n]*)\n(.*?)(?=\n### |\n## 附|\Z)",
                           src, re.S):
        body = sec.group(6)
        en = re.search(r"\*\*\[EN\]\*\*:\s*\*?" + '"' + r"?(.*?)" + '"' + r"?\*?\s*\n", body, re.S)
        zh = re.search(r"\*\*\[ZH\]\*\*:\s*[“\"](.*?)[”\"]\s*\n", body, re.S)
        storyboard.append({
            "start": int(sec.group(1)) * 60 + int(sec.group(2)),
            "end": int(sec.group(3)) * 60 + int(sec.group(4)),
            "en": (en.group(1) if en else "").strip(),
            "zh": (zh.group(1) if zh else "").strip(),
        })

    raw = re.search(r"## 附：创作者原稿手记.*?```text\s*(.*?)```", src, re.S)
    return table, storyboard, (raw.group(1).strip() if raw else "")


def _lost_tail_text(vo, seconds):
    """按语速把被淡掉的秒数折算成句尾文本，让报错直接指出丢了哪句话。"""
    sentences = [x.strip() for x in re.split(r'(?<=[.!?])\s+', re.sub(r'^"|"$', "", vo)) if x.strip()]
    if not sentences:
        return "（无文本）"
    total_chars = sum(len(x) for x in sentences) or 1
    tail, acc = [], 0.0
    for x in reversed(sentences):
        tail.insert(0, x)
        acc += seconds * len(x) / total_chars
        if acc >= seconds or len(tail) >= 2:
            break
    return '"…%s"' % " ".join(tail)[:110]


def norm(s):
    """比较台词时忽略标点与空白差异 —— 只关心内容是否被改掉。"""
    return re.sub(r"[\s,.!?\-—…“”\"'、，。！？：:;；()（）*]+", "", s or "")


def main():
    ap = argparse.ArgumentParser(description="音画文稿一致性检查")
    ap.add_argument("--episode", default="EP001_SelfIntro")
    ap.add_argument("--video", default=None, help="待检成片；缺省取 Render/ 下最新的 mp4")
    args = ap.parse_args()

    ep = os.path.join(ROOT, "Episodes", args.episode)
    md = os.path.join(ep, "Script.md")
    audio_dir = os.path.join(ep, "Audio")
    render_dir = os.path.join(ep, "Render")
    if not os.path.exists(md):
        print("找不到 %s" % md)
        return 1

    table, storyboard, raw_notes = parse_script(md)
    print("=== 一致性检查 %s ===" % args.episode)
    if table is None:
        bad("段表", "Script.md 里没有 ```json 段表 —— 渲染器会直接 exit 1")
        return 1
    if "segments" in table:
        bad("段表", "Script.md 还是旧的单层段表，跑 python Scripts/BuildScript.py --write")
        return 1

    segs = table["speech"]
    beats = table["beats"]
    subs = [c for s_ in segs for c in s_["subs"]]
    dur = float(table.get("duration", 0))
    voice = os.path.join(ep, "Voice.md")
    manuscript = io.open(voice, encoding="utf-8").read() if os.path.exists(voice) else ""
    print("口播 %d 段 ｜ 画面 %d 拍 ｜ 字幕 %d 条 ｜ 声明时长 %s s"
          % (len(segs), len(beats), len(subs), dur))

    # ---- 1. 两层时间线各自连续，且都铺到片长 ----
    gaps = []
    prev = 0.0
    for s in segs:
        if float(s["start"]) < prev - 1e-6:
            gaps.append("%s 起点 %g 早于上一段止点 %g" % (s["id"], s["start"], prev))
        if float(s["end"]) <= float(s["start"]):
            gaps.append("段 %s 时长非正" % s["id"])
        prev = max(prev, float(s["end"]))
    if abs(prev - dur) > 1.0:
        gaps.append("末段止于 %g 但 duration=%g" % (prev, dur))
    if abs(float(beats[0]["start"])) > 1e-6:
        gaps.append("画面层从 %g s 才开始，前面是空画面" % beats[0]["start"])
    for a, b in zip(beats, beats[1:]):
        if abs(float(a["end"]) - float(b["start"])) > 0.02:
            gaps.append("拍 %g→%g 与下一拍之间有缝" % (a["start"], a["end"]))
    if abs(float(beats[-1]["end"]) - dur) > 1.0:
        gaps.append("画面层止于 %g 但片长 %g" % (beats[-1]["end"], dur))
    if gaps:
        bad("时间线连续", "；".join(gaps))
    else:
        ok("时间线连续", "口播与画面两层都无缝、都铺到片长")

    # ---- 2. 音轨：时长 == 片长 ----
    tracks = sorted(f for f in os.listdir(audio_dir)
                    if f.lower().endswith((".mp3", ".wav", ".m4a"))
                    and not f.startswith(("seg", "my_voice")))
    chosen, cdur = None, None
    for f in tracks:
        d = probe_duration(os.path.join(audio_dir, f))
        if d is not None and abs(d - dur) <= 1.0:
            chosen, cdur = f, d
            break
    if not chosen:
        bad("音轨", "没有一条音轨时长贴合片长 %g s（现有：%s）"
            % (dur, ", ".join(tracks) or "无"))
    else:
        ok("音轨", "%s = %.2f s" % (chosen, cdur))

    # ---- 3. 每段音频不得超出段窗口（超窗会被静默截断，不是压缩） ----
    over = []
    for i, s in enumerate(segs, 1):
        p = os.path.join(audio_dir, "seg%d.wav" % i)
        if not os.path.exists(p):
            over.append("seg%d.wav 缺失" % i)
            continue
        d = probe_duration(p)
        win = float(s["end"]) - float(s["start"]) - 0.5      # mix 时留 0.5 s 淡出
        # 容差 0.02 s：段窗口就是"实测时长 + 0.5 淡出"算出来的，
        # 两边都取整到 3 位小数，相等时必然差几个毫秒。0.01 s 的"超窗"不是缺陷。
        if d is not None and d > win + 0.02:
            lost = _lost_tail_text(str(s["en"]), d - win)
            over.append("seg%d.wav %.1fs 超窗 %.1fs → 混音 afade 会把尾巴淡成静音，丢掉：%s"
                        % (i, d, d - win, lost))
    if over:
        bad("分段音频", "；".join(over))
    else:
        ok("分段音频", "各段都短于自己的窗口")

    # ---- 4. 音轨新鲜度：不得比原稿旧（Script.md 是生成物，比它没意义） ----
    if chosen:
        t_audio = os.path.getmtime(os.path.join(audio_dir, chosen))
        t_md = max(os.path.getmtime(f) for f in
                   (voice, os.path.join(ep, "Voice.en.md")) if os.path.exists(f))
        if t_audio < t_md:
            bad("音轨新鲜度", "%s 比原稿早 %.0f 分钟合成 —— 台词改过但配音没重跑"
                % (chosen, (t_md - t_audio) / 60))
        else:
            ok("音轨新鲜度", "音轨晚于原稿最后修改")

    # ---- 5. 段表新鲜度：Script.md 必须由当前三份出处重新生成 ----
    # 旧版这里校验"手写分镜 vs 段表"两份是否同步；现在整份 Script.md 都是生成物，
    # 会漂移的那一份副本已经不存在，改成查"生成物是否过期"。
    try:
        spec = importlib.util.spec_from_file_location(
            "bs", os.path.join(ROOT, "Scripts", "BuildScript.py"))
        bs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bs)
        fresh = bs.render_md(bs.layout(
            bs.build(bs.read_lines(os.path.join(ep, "Voice.md")),
                     bs.read_lines(os.path.join(ep, "Voice.en.md"))),
            json.load(open(os.path.join(ep, "timing.json"), encoding="utf-8"))
            if os.path.exists(os.path.join(ep, "timing.json")) else None))
        if fresh != io.open(md, encoding="utf-8").read():
            bad("段表新鲜度", "Script.md 与三份出处对不上 —— 台词/配音改过但没重跑生成器")
        else:
            ok("段表新鲜度", "Script.md 就是 Voice.md + Voice.en.md + timing.json 的当前产物")
    except SystemExit as exc:
        bad("段表新鲜度", "生成器自己就拒了：%s" % exc)
    except Exception as exc:
        bad("段表新鲜度", "没法重跑生成器核对：%s" % exc)

    # ---- 6. 原稿的中文是否都被段表覆盖（防止改稿丢掉原话） ----
    raw_notes = manuscript
    if raw_notes:
        joined = norm(" ".join(str(s["zh"]) for s in segs))
        # 原稿里（…）是画面指示不是台词，不剔掉会把舞台指示报成"丢失的原话"。
        # 注意：他的原稿存在不闭合的括号（舞台指示写到行尾就换行接台词），
        # 所以按行处理：先删配对的，再把未闭合的从括号删到行尾。
        kept = []
        for line in raw_notes.splitlines():
            line = re.sub(r"（[^（）]*）", "", line)
            kept.append(re.sub(r"（.*$", "", line))
        sentences = [norm(x) for x in re.split(r"[\n。！？，、；]", "\n".join(kept))
                     if len(norm(x)) >= 6]
        lost = [x for x in sentences if x not in joined]
        if lost:
            note("原稿覆盖", "%d/%d 句原稿话在段表里找不到：%s"
                 % (len(lost), len(sentences), " / ".join(l[:14] + "…" for l in lost[:4])))
        else:
            ok("原稿覆盖", "原稿每一句都在台词里")

    # ---- 7. 字幕单行宽度：必须用渲染器同一套字体实测像素 ----
    # 早先这里用"字符数 ≤ 85"当判据，是弱统计：中英混排下 85 字符可能宽 900px 也可能 1400px。
    # 字体取不到时宁可让这条判据炸掉：原来字体路径全是 C:/Windows/Fonts，
    # 换台机器就 zh_f=None → 宽度全量成 0 → 报"实测通过"，假绿比红更难查。
    try:
        from Fonts import font_load
        zh_f = font_load("zh", 20, index=0)
        en_f = font_load("en", 22)
        limit = ACTIVE_SUB_W
        over = []
        for x in subs:
            we = en_f.getlength(str(x.get("en", "")))
            wz = zh_f.getlength(str(x.get("zh", "")))
            if max(we, wz) > limit:
                over.append("%g-%gs（EN %.0fpx / ZH %.0fpx > %d）" % (x["start"], x["end"], we, wz, limit))
        if over:
            bad("字幕宽度", "；".join(over))
        else:
            ok("字幕宽度", "全部 ≤ %d px（字体实测，非字符数估算）" % limit)
    except Exception as exc:
        note("字幕宽度", "字体测量不可用：%s" % exc)

    # ---- 8. 指示同步：画面动作必须落在"它挂在的那句话"说出来的窗口里 ----
    # 判据出处：创作者与朋友都指出"扣完篮口播才说我喜欢运动"。量下来是
    # 系统性抢拍（篮筐早 6.2 s），根因在括号语义与分母，见 BuildScript.anchor_fracs。
    # 这条检查会随台词改动自动跟上，不靠人眼回看。
    try:
        import RenderVideo as RV
        cue_of = RV._cue_at
        cw = RV.CUE_WINDOW
    except SystemExit:
        cue_of = None
    if cue_of is None:
        bad("指示同步", "渲染器加载不了，量不了画面时刻")
    else:
        drift, checked = [], 0
        for i, b in enumerate(beats, 1):
            if not b["stage"]:
                continue
            # 窗口 = 触发句被说出来的那几秒，不是"这一拍覆盖到的字幕范围"。
            # 后者在 28 秒长拍里等于整段，怎么放都算合格 —— 那是假判据。
            trig = re.sub(r"[\s，、]", "", str(b.get("trigger") or ""))
            if len(trig) < 4:
                continue
            w0 = w1 = None
            norm_c = lambda x: re.sub(r"[\s，、]", "", x or "")
            for s_ in segs:
                # 两边同一套归一化，否则字符下标对不上（留着逗号就找不到）
                src = norm_c(s_["zh"])
                pos = src.find(trig)
                if pos < 0:
                    continue
                acc, spans = 0, []
                for c in s_["subs"]:
                    n = max(1, len(norm_c(c["zh"])))
                    spans.append((acc, acc + n, c["start"], c["end"]))
                    acc += n
                lo, hi = pos, pos + len(trig)
                at = lambda p: next(
                    (t0 + (p - a) / max(1, bb - a) * (t1 - t0)
                     for a, bb, t0, t1 in spans if a <= p <= bb), None)
                w0, w1 = at(lo), at(hi)
                # 舞台指示夹在两句话中间，"往后半句延伸"是合理的；
                # 不合理的只有"晚了五秒十秒"。所以后边界让给下一条字幕，
                # 前边界不让 —— 抢在话前面就是观众看到的"图不对文"。
                nxt = [c for c in s_["subs"] if c["start"] >= w1 - 0.01]
                if nxt:
                    w1 = nxt[0]["end"]
                break
            if w0 is None or w1 is None:
                note("指示同步", "触发句「%s」在字幕里找不到，这一拍的记号没核" % trig[:12])
                continue
            for c in b["cues"]:
                tc = cue_of(b, c)
                checked += 1
                if tc > w1 + 0.5:
                    drift.append("%s 在 %.1f s 出现，比「%s」说完(%0.1f s) 晚 %.1f s"
                                 % (c["id"].replace("prop", ""), tc,
                                    (b["stage"] or "")[:10], w1, tc - w1))
                elif tc < w0 - 0.5:
                    drift.append("%s 在 %.1f s 出现，比它挂在的话(%0.1f–%.1f) 早 %.1f s"
                                 % (c["id"].replace("prop", ""), tc, w0, w1, w0 - tc))
        if drift:
            bad("指示同步", "；".join(drift[:5]))
        else:
            ok("指示同步", "%d 个画面记号全部落在它挂在的那句话的窗口内（±0.5 s）"
               % checked)

    # ---- 9. 成片：时长 + 有字无声 ----
    video = args.video
    if not video:
        cands = [os.path.join(render_dir, f) for f in sorted(os.listdir(render_dir))
                 if f.endswith(".mp4")]
        # 只在"有索引"的那些里挑最新：预览片和半成品都不算成片
        whole = [p for p in cands if has_frame_index(p)]
        skipped = len(cands) - len(whole)
        video = max(whole, key=os.path.getmtime) if whole else None
        if skipped:
            note("成片选择", "跳过 %d 个没有帧索引的 mp4（渲染中的 / --watchable 预览片）" % skipped)
    if not video or not os.path.exists(video):
        note("成片", "找不到待检 mp4，跳过音画对齐")
    else:
        vd = probe_duration(video)
        print("\n  成片：%s（%.1f s）" % (os.path.basename(video), vd or -1))
        if vd is None or abs(vd - dur) > 1.0:
            bad("成片时长", "成片 %s s vs 段表 %g s" % (vd, dur))
        else:
            ok("成片时长", "与段表一致")
        runs = silence_runs(video)
        speech = [(float(s["start"]), float(s["end"])) for s in subs]
        dead_air = []
        for a, b in runs:
            # 容差 0.05 s：一帧是 0.033 s，mp3 编码器还有几十毫秒延迟。
            # 真出问题的量级是"字幕比声音早 0.2 s"或"17 s 静音挂着台词"，
            # 这个容差挡不住任何一条（本轮实测：修好后残差 0.02 s）。
            covered = [s for s in speech if s[0] < b - 0.05 and s[1] > a + 0.05]
            if covered:
                dead_air.append("%.1f–%.1f s 静音，但字幕 %s 在滚"
                                % (a, b, ", ".join("%g-%g" % c for c in covered)))
        if dead_air:
            bad("有字无声", "；".join(dead_air))
        else:
            ok("有字无声", "%d 段长静音全部落在无台词窗口" % len(runs))
        tail = [r for r in runs if r[0] > dur - 12]
        if tail and (dur - tail[0][0]) > 6:
            bad("结尾静音", "最后 %.1f s 没有声音（谢幕台词会被吃掉）" % (dur - tail[0][0]))

    kinds = {}
    for s, _ in fails:
        kinds[s] = kinds.get(s, 0) + 1
    print("\n%s" % ("全部通过" if not fails else
                    "未通过 %d 项：%s" % (len(fails), ", ".join("%s×%d" % kv for kv in sorted(kinds.items())))))
    if warns:
        print("另有 %d 条提示（不阻塞）" % len(warns))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
