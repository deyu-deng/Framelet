#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 Voice.md + Voice.en.md（+ 实测配音 timing.json）生成 Script.md 的段表。

为什么是生成而不是手写：同一集曾经同时存在三份互相矛盾的台词
（JSON 段表 / 手写分镜 / 配音脚本副本），改一处另两处不跟着动。
现在 **Voice.md 是唯一出处**，机器读的那份是产物。

两层时间线，因为**口播段数 ≠ 画面节拍数**：
    speech[]  配音与字幕      —— 长度来自实测音频
    beats[]   动画、道具、站位 —— 挂在口播段内的相对位置上

节拍规则用他自己的记法：稿子里一个（…）= 一拍，从该处跑到下一拍或段尾，
第一个括号之前是"站定说话"。整行都是括号的 = 开口前的纯动作，不占一段话。

相对位置与绝对时间分开算是关键：节拍只记"我在这段的第几成"，绝对时间最后一步
统一换算，所以配音变长变短都不需要重排分镜。早先两者混在一起算，
切出过 58.2–53.6 这种倒挂的拍。

用法：
    python Scripts/BuildScript.py            # 预览
    python Scripts/BuildScript.py --write    # 写进 Script.md 的 json 块
"""
import argparse
import bisect
import io
import json
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
EP = os.path.join(os.path.dirname(HERE), "Episodes", "EP001_SelfIntro")
TIMING = os.path.join(EP, "timing.json")
FPS = 30
WPM = 170.0        # 本项目实测英文语速；只在没有 timing.json 时用于估算
GAP = 0.35         # 段间气口
LEAD_IN = 4.5      # 开场：先有画面动作再开口
FADE = 0.5         # 段尾淡出预留，必须落在静音上

# 站位规则（是规则不是内容）：他把方位写在稿子里，这里翻译成坐标。16:9 画布宽 1920。
SIDE_PATTERNS = [(re.compile(r"画面右侧|向右|右侧出现"), "right"),
                 (re.compile(r"画面左侧|向左|左侧地面|左侧出现"), "left")]
CHAR_X = {"center": 960, "right": 1330, "left": 590}
MIN_BEAT = 0.4     # 短于此的拍并进相邻拍，不单独占一格
TRIGGER_CHARS = 14   # 一条舞台指示最多往回锚这么多说话字（≈3.5 秒）
# 一条指示自己的先后顺序，最多铺这么多秒演完；拍比这长，剩下的时间是"保持"。
# 没有这条时，28 秒长拍里的"水母出现→展示界面"被按比例摊到 86.5 / 91.1 秒，
# 比那句"现在插播一个广告"晚了 5~10 秒 —— 括号内文字很短，不该被拍长拉稀。
CUE_WINDOW = 6.0

# ---- 从他的舞台指示里认关键词，翻成机器可读的记号 -----------------------------
# 三条规则：
#   1) 只认他写过的词，不从剧情"推理"出没写的东西；
#   2) 一条指示里出现几个道具就出几个记号，cue 的 at 是该词在这拍里的**字位置**
#      （和段内括号同一个办法），所以"篮筐碎掉""电视出现"能落在各自该落的时间点；
#   3) 认不出的词不猜，留空 —— 漏了会在预览里看得见，猜错了要渲染完才发现。
CUE_PATTERNS = [
    # (正则, 道具 id, 记号)  ——  先列特例再列通例，同一次扫描里先命中的算
    (re.compile("篮筐碎|篮筐破|框碎"), "propHoop", "break"),
    (re.compile("篮筐"), "propHoop", "show"),
    (re.compile("电视机|电视"), "propTv", "show"),
    (re.compile("水母"), "propJellyfish", "show"),
    (re.compile("气韵界面|软件.*界面|Aura"), "propAuraUi", "show"),
    (re.compile("三连|点赞|关注"), "propBadges", "show"),
]
# 电视机**屏幕里**放什么。留白就是留白 —— 他说要自己剪《牛来》，这里不替他填。
SCREEN_PATTERNS = [(re.compile("牛来|牛来了"), "niulai"),
                   (re.compile("糖豆人"), "jellybean"),
                   (re.compile("水母|气韵|界面|插播一个广告"), "aura")]
ACTION_PATTERNS = [
    (re.compile("走出来|穿好|丢出来|龟壳"), "entrance"),
    (re.compile("扣进篮筐|威亚|飞起来|篮筐碎"), "dunk"),
    (re.compile("指向|指着|插播|出现在电视机|电视里面出现"), "point"),
    (re.compile("三连|嘻嘻|私信"), "outro"),
]
# 爱好墙那三件 + 手机：他只在**口播词**里说了"吉他""漫画""动画""插播一个广告"，
# 括号里没写。所以这些记号一律带 from=口播，段表和预览里都标得出来，他一眼能删。
SPOKEN_PROPS = [(re.compile("吉他"), "propGuitar", "show"),
                (re.compile("漫画"), "propComic", "show"),
                (re.compile("磨平"), "propComic", "tilt"),
                (re.compile("动画的热情|重拾了对动画"), "propCelluloid", "show"),
                (re.compile("插播一个广告|情绪记录软件"), "propPhone", "show")]


def cues_of(text):
    """舞台指示 → [{id, at, kind}]，at 是该词在指示文本里的相对位置 0~1。

    同一个道具只留第一条 show（"出现了一个篮筐""把篮球扣进篮筐"是同一件东西），
    以及与某条碎/破同位置的 show（"篮筐碎掉"既命中"篮筐"也命中"篮筐碎"）。
    """
    hits = []
    for pat, pid, kind in CUE_PATTERNS:
        for m in pat.finditer(text or ""):
            hits.append((m.start(), pid, kind))
    n = max(1, len(text))
    shattered = {(off, pid) for off, pid, kind in hits if kind == "break"}
    hits.sort()
    out, shown, broke = [], set(), set()
    for off, pid, kind in hits:
        if kind == "show":
            # "篮筐碎掉"同时命中"篮筐"和"篮筐碎"：同位置的 show 是重复命中，丢掉
            if (off, pid) in shattered or pid in shown:
                continue
            shown.add(pid)
        elif pid not in broke:
            broke.add(pid)
        else:
            continue
        out.append({"id": pid, "at": round(off / n, 4), "kind": kind})
    return out


def screen_of(text):
    for pat, name in SCREEN_PATTERNS:
        if pat.search(text or ""):
            return name
    return None


def action_of(text):
    for pat, name in ACTION_PATTERNS:
        if pat.search(text or ""):
            return name
    return "talk"


def side_of(text):
    for pat, name in SIDE_PATTERNS:
        if pat.search(text or ""):
            return name
    return None


def read_lines(path):
    if not os.path.exists(path):
        sys.exit("找不到 %s" % path)
    out = []
    for raw in open(path, encoding="utf-8").read().splitlines():
        s = raw.strip()
        if s:
            m = re.match(r"^\[(原|写|AI)\]\s*(.*)$", s)
            out.append((m.group(1) if m else "原", m.group(2) if m else s))
    return out


def is_pure_stage(txt):
    """整行都是括号 = 只有画面、不张口。"""
    return txt.startswith("（") and txt.endswith("）")


def split_parenthesis(text):
    """返回 (去掉括号后的说话文本, [(括号在原句中的字符位置, 指示内容), ...])。

    舞台指示整段原样保留，不解析成结构 —— 解析就是二次翻译，
    而二次翻译正是丢原话的地方。
    """
    parts, beats, pos = [], [], 0
    for m in re.finditer(r"（([^（）]*)）", text):
        parts.append(text[pos:m.start()].strip())
        beats.append((m.start(), m.group(1).strip()))
        pos = m.end()
    parts.append(text[pos:].strip())
    return " ".join(p for p in parts if p), beats


def anchor_fracs(zh):
    """每个括号锚在哪一句话说完之前 —— 换算成"说话字符"占整段说话字符的比例。

    两条改过来的语义，都是先前理解反了的地方：
      1) 括号标记的是**它前面那句话正在发生的事**，不是"这句话之后才做"。
         所以他写"…比如...打篮球（这时候筐出现，我扣进去）"，
         动作要从"当然，我也比较爱运动"这个字开始跑，而不是从括号的位置。
      2) 分母只数**说话的字**，不数括号自己。旧算法把括号里那 84 个指示字
         也算进长度，于是指示写得越长、画面抢拍越多——实测篮筐早 6.2 秒。
    但"只按句号找开头"在他稿子里会失效：第三段整段只有一个句号，
    三个括号全落在同一句里，锚点会挤成同一个时刻、拍被压成零长。
    所以再加一条窗口：锚点最多往回 TRIGGER_CHARS 个说话字。
    14 字 ≈ 他实测语速下的 3.5 秒，正好是"起手—做动作—落定"的最短可读长度；
    再短就读不出他在做啥，再长就早于那句话了。
    """
    parens = [(m.start(), m.end()) for m in re.finditer(r"（[^（）]*）", zh)]
    inside = set()
    for a, b in parens:
        inside.update(range(a, b))
    spoken_idx = [i for i in range(len(zh)) if i not in inside]
    spoken = "".join(zh[i] for i in spoken_idx)
    total = max(1, len(spoken))
    out = []
    for a, _b in parens:
        k = bisect.bisect_left(spoken_idx, a)      # 括号前有多少个说话字符
        start = 0
        for j in range(k - 1, -1, -1):
            if spoken[j] in "。！？；":
                start = j + 1
                break
        start = max(start, k - TRIGGER_CHARS)
        out.append((a, min(1.0, start / total), spoken[start:k]))
    return out


def build(zh_lines, en_lines):
    """产出相对结构：每段带说话文本与若干拍，每拍只记 0~1 的相对位置。"""
    spoken_zh = [x for x in zh_lines if not is_pure_stage(x[1])]
    if len(spoken_zh) != len(en_lines):
        sys.exit("口播 %d 段（另有 %d 行纯舞台指示）与英文稿 %d 段对不上"
                 % (len(spoken_zh), len(zh_lines) - len(spoken_zh), len(en_lines)))
    segs, pending = [], None
    for idx, (tag, zh) in enumerate(x for x in zh_lines):
        spoken, st = split_parenthesis(zh)
        if not spoken and st:
            pending = st[0][1]                       # 下一段开口前的动作
            continue
        en_tag, en = en_lines[len(segs)]
        total = max(1, len(zh))
        # marks[0]=0，marks[1..n]=各括号的相对位置，marks[n+1]=1。
        # 括号标记一拍的**开始**：先有"开口到第一个括号"的站定拍，
        # 然后第 j 个括号的拍跑到下一个括号（或段尾）。
        anch = anchor_fracs(zh)
        marks = [0.0] + [f for _off, f, _t in anch] + [1.0]
        beats = []
        if pending:                             # 上一行纯舞台指示 → 开口前的动作拍
            beats.append({"lead": True, "stage": pending, "side": side_of(pending)})
            pending = None
        if not st:
            beats.append({"a0": 0.0, "a1": 1.0, "stage": "", "side": None})
        else:
            if marks[1] > 0:
                beats.append({"a0": 0.0, "a1": marks[1], "stage": "", "side": None})
            for j, (off, text) in enumerate(st):
                beats.append({"a0": marks[j + 1], "a1": marks[j + 2],
                              "stage": text, "side": side_of(text),
                              # 触发句原样带上：门禁要照它定位"该动的那一秒"
                              "trigger": anch[j][2] if j < len(anch) else ""})
        segs.append({"zh": spoken, "en": en, "zh_src": tag, "en_src": en_tag,
                     "beats": beats})
    return segs


def _units(text, sent_re, join_re, cap):
    """按句切块；单句超过 cap 字符再按次级标点切。返回块列表。"""
    out = []
    for s in (x.strip() for x in sent_re.findall(text or "")):
        if not s:
            continue
        if len(s) <= cap:
            out.append(s)
            continue
        buf = ""
        for p in (x.strip() for x in join_re.split(s)):
            if not p:
                continue
            if buf and len(buf) + 1 + len(p) > cap:
                out.append(buf)
                buf = p
            else:
                buf = (buf + " " + p).strip()
        if buf:
            out.append(buf)
    return out


SENT_EN = re.compile(r"[^.!?]+[.!?]*")
JOIN_EN = re.compile(r"(?<=[,;:])\s+|(?<=\s)[—-]\s+")


ZH_BREAKS = {"。": 3, "！": 3, "？": 3, "；": 2, "，": 2, "、": 1, "…": 1}


def _zh_cut_at(text, target, floor=0, window=12):
    """在 target 附近找一个断句处切；找不到就硬切。

    评分里"距离"比"断句强度"权重高（rank*3 − 距离）：先前用 rank*10，
    一个 15 字外的句号会压掉 2 字外的逗号，切点一路向前窜，
    后面没字可分时只能切出"华为 / 应用市场"这种碎块。
    """
    best = None
    lo, hi = max(floor, target - window), min(len(text) - 1, target + window)
    for i in range(lo, hi):
        rank = ZH_BREAKS.get(text[i - 1], 0)
        if not rank:
            continue
        score = rank * 3 - abs(i - target)
        if best is None or score > best[0]:
            best = (score, i)
    return best[1] if best else max(floor, min(target, len(text)))


def _zh_lines(text, fracs):
    """把整段中文按每条字幕的时间占比切分，切点吸附到最近的标点。

    不做"中英句对句"：同一段他 5 句、英文 7 句，配对必然有一边落空
    （实测 30 条字幕里 13 条没中文）。改成按同一时间轴分配字数。

    先算每份**该占几个字**，再逐刀切、切点吸附标点：
    早先按累计时间比例取绝对位置，切点被标点往前拽，几次之后位置就跑到
    已切位置前面，只能硬夹成两字碎块；更早上一种写法直接把尾巴切成了
    "！！！！！ / ！！！"。这里从计数出发，sum(份数) == 原文长度，不重不漏。
    """
    n = len(fracs)
    # 中文里不该有空格：口播文本是被"去掉括号指示后 join(' ')"拼出来的，
    # 括号位置会留下空洞（"水平 。"）。先抹平，切分和校验才对得上。
    text = re.sub(r"\s+", "", text or "")
    if not text:
        return [""] * n
    if n == 1:
        return [text.strip()]
    L = len(text)
    shares = [fracs[0]] + [b - a for a, b in zip(fracs, fracs[1:])]
    cnt = [max(4, int(round(L * s))) for s in shares]
    cnt[cnt.index(max(cnt))] += L - sum(cnt)          # 凑回原文长度，余量给最长的一条
    while sum(cnt) > L:                               # 保底 4 字可能超发，从最长的削
        cnt[cnt.index(max(cnt))] -= 1
    out, pos = [], 0
    for k, c in enumerate(cnt):
        if k == n - 1:
            out.append(text[pos:].strip())
            break
        target = min(L, pos + c)
        cut = max(pos + 4, _zh_cut_at(text, target, floor=pos + 4))
        # 这里**不**做行长上限：切短了余量会挤到最后一条，实测反而变成 44 字。
        # 时间分配与视觉行长是两件事 —— 行长由渲染器换行处理（RenderVideo._wrap）。
        out.append(text[pos:cut].strip())
        pos = cut
    return out


def captions(seg, start, end):
    """双语字幕：按**本段实测语速**比例分配，不按全局 170wpm 硬算。

    两处教训：
      * 早先用全局 WPM 累加，遇到 63 s 的长段会算出比窗口还长的时间线，
        末尾几条被 min() 夹成一堆同一秒的字幕；
      * 中英句数对不上（同一段中文 5 句、英文 7 句），不做 1:1 配对，
        英文按词数铺满时间窗、中文按同一窗分配字数。两端对齐，中间允许漂移。
    """
    en = _units(seg["en"], SENT_EN, JOIN_EN, 76)
    if not en:
        return []
    span = max(0.01, end - start)
    ew = [max(1, len(x.split())) for x in en]
    tot = float(sum(ew))
    subs, fracs, acc = [], [], 0
    for text, w in zip(en, ew):
        a = start + acc / tot * span
        acc += w
        fracs.append(acc / tot)
        subs.append({"start": round(a, 2),
                     "end": round(start + acc / tot * span, 2), "en": text, "zh": ""})
    for s, piece in zip(subs, _zh_lines(seg["zh"], fracs)):
        s["zh"] = piece
    # 短于 1.2 s 的字幕读不完：与下一条并（中英一起并），不是把它挤成 0 长
    out = []
    for s in subs:
        if out and s["end"] - s["start"] < 1.2:
            out[-1]["en"] = out[-1]["en"] + " " + s["en"]
            out[-1]["zh"] += s["zh"]
            out[-1]["end"] = s["end"]
        else:
            out.append(dict(s))
    return [s for s in out if s["end"] > s["start"] + 0.05]


def layout(segs, measured=None):
    """相对 → 绝对。measured 为 timing.json 内容；缺省时按英文词数估算。"""
    speech, beats, t = [], [], 0.0
    voiced = 0.0
    for i, seg in enumerate(segs):
        lead = bool(seg["beats"] and seg["beats"][0].get("lead"))
        if measured:
            m = measured["speech"][i]
            start, dur = float(m["start"]), float(m["dur"])
            end = float(m["end"])
            # TTS 每段开头带 0.12~0.18 s 空白。字幕与口型按"真正出声"那一刻开，
            # 不然每句都抢在声音前面出现（CheckSync 的"有字无声"逮到过）。
            voiced = start + float(m.get("lead", 0.0) or 0.0)
        else:
            dur = round(max(1.2, len(seg["en"].split()) / WPM * 60.0), 2)
            start = round(t + (LEAD_IN if (lead and i == 0) else GAP), 2)
            end = round(start + dur + FADE, 2)
        span = max(0.01, end - start)
        raw = []
        for b in seg["beats"]:
            if b.get("lead"):
                s0, s1 = round(t, 2), start          # 开口之前的纯动作段
            else:
                s0 = round(start + b.get("a0", 0.0) * span, 2)
                s1 = round(start + b["a1"] * span, 2)
            raw.append({"start": s0, "end": s1, "stage": b["stage"],
                        "side": b["side"], "trigger": b.get("trigger", ""),
                        "cues": cues_of(b["stage"]),
                        "screen": screen_of(b["stage"]),
                        "action": action_of(b["stage"])})
        # 短于 MIN_BEAT 的拍不丢掉 —— 它带着他点名的道具。并进前一拍，
        # 记号时间按合并后的区间重新折算，道具该出现的时候照样出现。
        merged = []
        for b in raw:
            if merged and b["end"] - b["start"] < MIN_BEAT:
                prev = merged[-1]
                k = max(0.01, prev["end"] - prev["start"])
                shift = (b["start"] - prev["end"]) / k
                for c in b["cues"]:
                    prev["cues"].append({"id": c["id"], "kind": c["kind"],
                                         "at": round(min(1.0, max(0.0, 1.0 + shift + c["at"] * (b["end"] - b["start"]) / k)), 4)})
                prev["cues"].sort(key=lambda c: c["at"])
                prev["end"] = max(prev["end"], b["end"])
                prev["stage"] = (prev["stage"] + " " + b["stage"]).strip()
                if b["screen"]:
                    prev["screen"] = b["screen"]
            else:
                merged.append(b)
        beats += merged
        subs = captions(seg, voiced if measured else start,
                        end - (FADE if measured else 0.0))
        speech.append({"id": "s%d" % (i + 1), "start": start, "end": end,
                       "dur": dur, "en": seg["en"], "zh": seg["zh"],
                       "zh_src": seg["zh_src"], "en_src": seg["en_src"], "subs": subs})
        t = end
    beats.sort(key=lambda x: x["start"])
    # 空隙并入前一拍（标杆 auto-motion 的硬规则："时间必须连续，空隙并入前一镜头"）。
    for prev, nxt in zip(beats, beats[1:]):
        if 0 < nxt["start"] - prev["end"] <= GAP + 0.01:
            prev["end"] = nxt["start"]
    last, last_screen = "center", None
    for b in beats:                                  # 站位与屏幕都是粘的：没新指示就留在原地
        last = b.pop("side") or last
        b["char_x"] = CHAR_X[last]
        last_screen = b["screen"] = b["screen"] or last_screen
    # "矢量线条在屏幕上蠕动"说的是这部片子本身 —— 让电视里真的蠕几条线。
    # 这条从他**口播词**里认（括号里没写），所以标 from=口播，他一眼能删。
    for si, seg in enumerate(segs):
        for pat, pid, kind in SPOKEN_PROPS:
            wm2 = pat.search(seg["zh"])
            if wm2 is None or not measured or si >= len(measured["speech"]):
                continue
            mm = measured["speech"][si]
            wt = float(mm["start"]) + wm2.start() / max(1, len(seg["zh"])) *                 (float(mm["end"]) - float(mm["start"]))
            for b in beats:
                if b["start"] <= wt < b["end"] and                         not any(c["id"] == pid and c["kind"] == kind for c in b["cues"]):
                    # 口播记号按"说到那个词的那一秒"存绝对偏移。
                    # CUE_WINDOW 那条 6 秒规则是给**括号指示**用的（一条指示内部的
                    # 先后顺序铺 6 秒），拿它套口播会把 20 秒后才说到的词挤到拍尾。
                    b["cues"].append({"id": pid, "kind": kind,
                                      "at": round(min(1.0, (wt - b["start"]) /
                                                      max(0.01, b["end"] - b["start"])), 4),
                                      "at_s": round(wt - b["start"], 3), "from": "口播"})
                    b["cues"].sort(key=lambda c: c["at"])
                    break
        wm = re.search("矢量线条", seg["zh"])
        if wm is None or si >= len(measured["speech"] if measured else []):
            continue
        m0 = measured["speech"][si]
        wt = float(m0["start"]) + wm.start() / max(1, len(seg["zh"])) *             (float(m0["end"]) - float(m0["start"]))
        for b in beats:
            if b["start"] <= wt < b["end"]:
                b["screen"], b["screen_from"] = "vector", "口播"
                break
    # 谢幕是他唯一一句"话里有动作、括号里没写"的指示："欢迎大家三连关注"。
    # 三连图标是 B 站通用约定，出现位置取那句话在末拍里的字位置。
    # 这条不是从他括号里读出来的，所以标 from=口播，让他一眼能删。
    cm = re.search("三连|关注|点赞", segs[-1]["zh"])
    if cm and beats:
        tail, frac = beats[-1], cm.start() / max(1, len(segs[-1]["zh"]))
        tail["action"] = "outro"
        tail["cues"] = [c for c in tail["cues"] if c["id"] != "propBadges"]
        tail["cues"].append({"id": "propBadges", "at": round(frac, 4),
                             "kind": "show", "from": "口播"})
        tail["cues"].sort(key=lambda c: c["at"])
    total = measured["duration"] if measured else max(b["end"] for b in beats) + 1.0
    return {"fps": FPS, "duration": round(float(total), 2),
            "cue_window": CUE_WINDOW,
            "speech": speech, "beats": sorted(beats, key=lambda x: x["start"])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="写进 Script.md 的 json 块")
    a = ap.parse_args()
    segs = build(read_lines(os.path.join(EP, "Voice.md")),
                 read_lines(os.path.join(EP, "Voice.en.md")))
    measured = json.load(open(TIMING, encoding="utf-8")) if os.path.exists(TIMING) else None
    if measured:
        if len(measured["speech"]) != len(segs):
            sys.exit("timing.json 有 %d 段，稿子有 %d 段 —— 台词改过，先重跑 GenerateAudio.py"
                     % (len(measured["speech"]), len(segs)))
        print("[+] 用实测配音时间（timing.json）")
    else:
        print("[!] 没有 timing.json，按 170wpm 估算 —— 出片前必须先跑配音")
    data = layout(segs, measured)

    print("口播 %d 段 ｜ 画面节拍 %d 拍 ｜ 片长 %.2f 秒"
          % (len(data["speech"]), len(data["beats"]), data["duration"]))
    for b in data["beats"]:
        if b["end"] <= b["start"]:
            sys.exit("节拍倒挂：%s" % b)
        cues = " ".join("%s%s@%.2f%s" % (c["id"].replace("prop", ""),
                                      "碎" if c["kind"] == "break" else "", c["at"],
                                      "(非括号)" if c.get("from") else "")
                 for c in b["cues"])
        print("  %6.1f–%6.1f x=%-4s %-8s 屏幕=%-8s %-26s %s"
              % (b["start"], b["end"], b["char_x"], b["action"], b["screen"] or "—",
                 cues or "—", (b["stage"] or "（站定说话）")[:30]))
    for s in data["speech"]:
        got = "".join(c["zh"] for c in s["subs"])
        want = re.sub(r"\s+", "", s["zh"])
        if got != want:
            sys.exit("第 %s 段的中文字幕没铺满原稿：切完 %d 字 / 原稿 %d 字 ｜ "
                     "切出来 [%s] ｜ 应该是 [%s]"
                     % (s["id"], len(got), len(want), got, want))
    allsub = [x for s in data["speech"] for x in s["subs"]]
    long_zh = [x for x in allsub if len(x["zh"]) > 28]
    print("字幕 %d 条 ｜ 中文最长 %d 字（超 28 字的 %d 条，一行放不下）"
          % (len(allsub), max((len(x["zh"]) for x in allsub), default=0), len(long_zh)))
    if a.write:
        path = os.path.join(EP, "Script.md")
        io.open(path, "w", encoding="utf-8").write(render_md(data))
        print("[+] 已写入 Script.md（整份生成：手写的分镜副本就是第三份会漂移的真相）")
    return 0


def render_md(data):
    """Script.md 的**全文**。这份文件里没有任何人肉维护的内容。

    以前它是"手写分镜 + 机器段表"混在一起：改一处另两处不动，
    CheckSync 花一整节校验两者是否同步。现在整份由本脚本生成，
    不同步这件事在物理上不可能发生。
    """
    L = ["# EP001 自我介绍 —— 口播与画面时间线",
         "",
         "> **生成物，别手改。** 出处只有三份：",
         "> `Voice.md`（创作者原稿，括号里是舞台指示）、"
         "`Voice.en.md`（英文口播稿）、`timing.json`（配音实测时长）。",
         "> 改内容改那三份，然后 `python Scripts/BuildScript.py --write`。",
         "",
         "片长 **%.2f s** ｜ %d fps ｜ 口播 %d 段 ｜ 画面 %d 拍"
         % (data["duration"], data["fps"], len(data["speech"]), len(data["beats"])),
         "",
         "## 口播层（配音与字幕）",
         "",
         "| 段 | 起 | 止 | 实测时长 | 中文（原稿） |",
         "| :-- | --: | --: | --: | :-- |"]
    for s in data["speech"]:
        L.append("| %s | %.2f | %.2f | %.2f | %s |"
                 % (s["id"], s["start"], s["end"], s["dur"], s["zh"][:40]))
    L += ["", "## 画面层（动画、道具、站位）", "",
          "一拍 = 他稿子里的一个（…）。括号里的原话一字不改地抄在下面，"
          "动作/道具/屏幕内容是**从这些词里认出来的**，不是另写一份清单。", "",
          "| 起 | 止 | 动作 | 站位 x | 道具记号 | 屏幕 | 他的舞台指示原话 |",
          "| --: | --: | :-- | --: | :-- | :-- | :-- |"]
    for b in data["beats"]:
        cues = "、".join("%s%s@%.0f%%" % (c["id"].replace("prop", ""),
                                           "碎" if c["kind"] == "break" else "",
                                           100 * c["at"])
                         for c in b["cues"]) or "—"
        L.append("| %.2f | %.2f | %s | %d | %s | %s | %s |"
                 % (b["start"], b["end"], b["action"], b["char_x"], cues,
                    b["screen"] or "—", (b["stage"] or "（站定说话）").replace("|", "／")))
    L += ["", "## 字幕（中英，逐条）", "", "| 起 | 止 | English | 中文 |",
          "| --: | --: | :-- | :-- |"]
    for s in data["speech"]:
        for c in s["subs"]:
            L.append("| %.2f | %.2f | %s | %s |"
                     % (c["start"], c["end"], c["en"].replace("|", "／"),
                        c["zh"].replace("|", "／")))
    L += ["", "## 机器可读段表", "", "```json",
          json.dumps(data, ensure_ascii=False, indent=2), "```", ""]
    return "\n".join(L)


if __name__ == "__main__":
    sys.exit(main())
