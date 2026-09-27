#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""溯源检查：让"创作者说的"和"AI 提的"在机器眼里长得不一样。

为什么需要：本项目反复出现同一类事故 —— AI 的推断被记成创作者的决定。
已发生的两例：① 分镜里「念到嘻嘻时身体害羞地往龟壳里缩进 20%」是 AI 编的表演指令，
因为它长得合理，已经被实现成 `RenderVideo.py` 的 `sf = 1.0 - 0.20 * p`，成了代码里的事实；
② 三个"动作人格词"是 AI 拟的候选、创作者从里面挑了一个，却被文档写成"创作者裁决"。

对标 `Ref/vibe-motion/skills/brand-launch-video-star/references/asset-authenticity.md`
的 "Do not turn inferred behavior into a product claim"：推断允许存在，但不许冒充事实。

标签约定：
    [原]  创作者原话，逐字可追到 Voice.md
    [写]  创作者的意思，AI 重排/翻译过，需要他确认
    [AI]  创作者完全没说过，AI 提出 —— 默认不生效，进代码前必须有他的批准记录

**默认标签按文件给**：Voice.md 整份默认 [原]（那是他写的），Voice.en.md 整份默认
[写]（AI 按他意思重排的）。行首前缀只用来记偏离默认的那几条 —— 早先要求逐条打标，
结果一份只有他写过的稿子也要每行盖 [原]，等于在自家门牌上签字，很快就没人标了。

Script.md 现在整份是生成物，不带标签（标签会漏进字幕）；
它的溯源靠"段表逐字等于两份稿子（括号指示除外）"这条继承。

用法：python Scripts/CheckProvenance.py
退出码 1 = 有未标注项 / 有假 [原] / 有未批准的 [AI] 已进代码 / 段表与稿子对不上。
"""
import json
import os
import re
import sys
import difflib

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EP = os.path.join(ROOT, "Episodes", "EP001_SelfIntro")
MD = os.path.join(EP, "Script.md")
ZH_MD = os.path.join(EP, "Voice.md")          # 他的原稿：逐条带标签
EN_MD = os.path.join(EP, "Voice.en.md")       # 英文口播稿：逐条带标签
CODE = os.path.join(HERE, "RenderVideo.py")

TAGS = ("原", "写", "AI")
# 一条 [原] 至少要有这么长的连续实词能在原稿里找到，才算真原话
TRACE_MIN = 6
# 内容词重叠率低于此值就认定"原稿里找不到"
TRACE_RATIO = 0.55

fails = []
notes = []


def norm(s):
    """去标点空白；中文按字，英文按词。虚词也去掉，否则"出现了"和"出现"会误判。"""
    s = re.sub(r"[，。！？、；：,.!?;:\s“”\"'（）()《》*—\-–…]+", "", s or "")
    return re.sub(r"[的了呢吧啊呀嘛哦哈呃过再就都也还又把被和与及]", "", s)


def content_words(s):
    return [w for w in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{3,}", s or "") if len(w) >= 2]


def traceable(item, his_norm, his_words):
    """[原] 的判据：去虚词后是原稿子串，或内容词几乎全部在原稿里出现。"""
    n = norm(item)
    if not n:
        return True, 1.0
    if n in his_norm:
        return True, 1.0
    ws = content_words(item) or [n]
    hit = sum(1 for w in ws if w in his_words)
    ratio = hit / len(ws)
    if ratio >= TRACE_RATIO:
        return True, ratio
    # 兜底：最长公共片段，抓"改写很少"的情况
    best = max((difflib.SequenceMatcher(None, n, his_norm[max(0, k - 40):k + 40]).ratio()
                for k in range(0, max(1, len(his_norm)), 6)), default=0.0)
    return best > 0.8, best


def _lines(path):
    """读一份逐行带标签的稿子 → [(标签, 正文)]。没标签的行由调用方判成未标注。"""
    out = []
    if not os.path.exists(path):
        return out
    for raw in open(path, encoding="utf-8").read().splitlines():
        t = raw.strip()
        if not t or t.startswith("#") or t.startswith(">"):
            continue
        m = re.match(r"^\[(原|写|AI)\]\s*(.*)$", t)
        out.append((m.group(1) if m else None, m.group(2) if m else t))
    return out


def parse(md_text):
    """Script.md 现在是**生成物**，标签不在它身上 —— 出处只有两份稿子。

    旧版从 Script.md 里的手写分镜抓标签，那份副本已经删掉：
    同一集曾同时存在三份互相矛盾的台词，改一处另两处不跟着动。
    """
    table = json.loads(re.search(r"```json\s*(\{.*?\})\s*```", md_text, re.S).group(1))
    # 标签按**文件**默认：Voice.md 就是他的原稿（[原]），Voice.en.md 是 AI 按他
    # 意思重排的英文（[写]）。逐条前缀只用来记"偏离本文件默认"的那些 ——
    # 给一份只有他一个人写过的稿子每行补 [原]，等于在自家门牌上签字。
    zh = [(tag or "原", t) for tag, t in _lines(ZH_MD)]
    en = [(tag or "写", t) for tag, t in _lines(EN_MD)]
    his_raw = "\n".join(t for tag, t in zh if tag == "原")
    # 整行都是括号的 = 纯舞台指示，不占一段话
    spoken = [(tag, t) for tag, t in zh
              if not (t.startswith("（") and t.endswith("）"))]
    segs = [{"ZH": [t for _, t in spoken], "EN": [t for _, t in en],
             "zh_tags": [tag for tag, _ in spoken], "en_tags": [tag for tag, _ in en]}]
    return table, segs, his_raw


def propose(md_text):
    """按判据自动分档并打印打好标签的分镜，供人只改判错的那些。

    工具不写回 Script.md —— 那是脚本侧的文件，而且自动判据只能给初稿：
    [写]（意思对、措辞是 AI 重排的）机器分不出来，必须人看。
    """
    table, segs, his_raw = parse(md_text)
    his_norm = norm(his_raw)
    his_words = set()
    for w in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{3,}", his_raw):
        his_words.add(w)
    for k in (2, 3, 4):
        for i in range(len(his_norm) - k + 1):
            his_words.add(his_norm[i:i + k])
    print("=== 自动分档提案（只打印，不写回；请只改判错的那些）===\n")
    tally = {"原": 0, "AI": 0}
    for i, groups in enumerate(segs, 1):
        print("### 段%d" % i)
        for gname in ("ZH", "EN"):
            items = [("[%s] " % t) + x for t, x in
                     zip(groups[gname.lower() + "_tags"], groups[gname])]
            if gname == "EN":
                # 英文稿是"他的意思 + AI 重排措辞"，默认 [写]。
                # 拿英文去中文原稿里找来源永远是 0 重叠 —— 那是跨语言，不是没出处。
                for it in items:
                    print("  - [写]（跳过自动判源：中英对照无法用字面重叠判定） %s"
                          % re.sub(r"^\[\w+\] ", "", it)[:70])
                continue
            for it in items:
                text = re.sub(r"^\[(原|写|AI)\]\s*", "", it.strip())
                ok, score = traceable(text, his_norm, his_words)
                tag = "原" if ok else "AI"
                tally[tag] += 1
                mark = "" if ok else "   ← 原稿无来源，需你点头才生效（重叠 %.2f）" % score
                print("  - [%s] %s%s" % (tag, text, mark))
        print()
    print("合计：判为 [原] %d 条 ｜ 判为 [AI] %d 条" % (tally["原"], tally["AI"]))
    print("注意：[写]（你的意思、AI 重排措辞）机器分不出来，需要你在 [原] 里挑一遍。")


def main():
    if not os.path.exists(MD):
        print("找不到 %s" % MD)
        return 1
    md_text = open(MD, encoding="utf-8").read()
    if "--propose" in sys.argv:
        propose(md_text)
        return 0
    table, segs, his_raw = parse(md_text)
    his_norm = norm(his_raw)
    his_words = set()
    for w in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{3,}", his_raw):
        his_words.add(w)
    for k in range(2, 5):                      # 中文按 2~4 字滑窗建索引
        for i in range(len(his_norm) - k + 1):
            his_words.add(his_norm[i:i + k])

    code = open(CODE, encoding="utf-8").read() if os.path.exists(CODE) else ""
    print("=== 溯源检查 EP001 ===")
    print("原稿 %d 字 ｜ 稿子条目 %d 条 ｜ 标签约定 [原]/[写]/[AI]"
          % (len(his_raw), len(segs[0]["ZH"]) + len(segs[0]["EN"])))

    untagged = fake_orig = ai_in_code = tagged = 0
    for groups in segs:
        for gname in ("ZH", "EN"):
            for tag, text in zip(groups[gname.lower() + "_tags"], groups[gname]):
                if tag is None:                     # 两份稿子都有默认标签，走到这里
                    untagged += 1                   # 说明默认被改坏了 —— 报出来
                    fails.append("无标签：%s｜%s" % (gname, text[:46]))
                    continue
                tagged += 1
                if tag == "原":
                    ok, score = traceable(text, his_norm, his_words)
                    if not ok:
                        fake_orig += 1
                        fails.append("假[原]：%s（Voice.md 里找不到，重叠 %.2f）"
                                     % (text[:40], score))
                if tag == "AI":
                    ws = [w for w in content_words(text) if len(w) >= 3]
                    hit = [w for w in ws if w in code]
                    if ws and len(hit) / len(ws) >= 0.5:
                        ai_in_code += 1
                        fails.append("未批准的 [AI] 已进代码：%s（命中 %s）"
                                     % (text[:38], ", ".join(hit[:4])))

    # 段表不带标签（标签会漏进字幕），它的溯源靠"逐字等于稿子"继承
    g = segs[0]
    for i, (seg, zh, en, zt, et) in enumerate(
            zip(table.get("speech", []), g["ZH"], g["EN"], g["zh_tags"], g["en_tags"]), 1):
        # 括号里是舞台指示，不是台词：段表存的是去掉括号后的说话部分
        spoken = re.sub(r"（[^（）]*）", "", zh).strip()
        if norm(spoken) != norm(str(seg.get("zh", ""))):
            fails.append("段%d 的中文与 Voice.md 对不上 —— 段表继承了哪条溯源说不清" % i)
        if norm(en) != norm(str(seg.get("en", ""))):
            fails.append("段%d 的英文与 Voice.en.md 对不上" % i)
        if zt != "原":
            fails.append("段%d 的中文稿行标了 [%s] —— Voice.md 里出现非 [原] 就是他的"
                         "原话被改过，得他本人确认" % (i, zt))
    print("  溯源链：Voice.md[%s] + Voice.en.md[%s] → 段表 %d 段逐字一致"
          % ("/".join(sorted(set(str(x) for x in g["zh_tags"]))),
             "/".join(sorted(set(str(x) for x in g["en_tags"]))),
             len(table.get("speech", []))))

    for line in fails[:18]:
        print("  [FAIL] %s" % line)
    if len(fails) > 18:
        print("  [FAIL] …另有 %d 条同类" % (len(fails) - 18))
    print("\n  已标注 %d 条 ｜ 未标注 %d ｜ 假[原] %d ｜ 未批准已进代码 %d"
          % (tagged, untagged, fake_orig, ai_in_code))
    print("%s" % ("全部通过" if not fails else "未通过 %d 项" % len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
