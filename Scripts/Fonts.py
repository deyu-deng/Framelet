"""跨平台字体解析。把散在渲染器 / 门禁 / 封面脚本里的 `C:\\Windows\\Fonts` 收拢到这一处。

为什么必须收拢：那些候选链原来各自硬编码 Windows 字体路径，用 `os.path.exists` 逐个
试，全都不存在就**静默**退回 `ImageFont.load_default()`。换到 macOS 上等于中文变方框、
字幕宽度量错，而成片照样渲得出来 —— 悄悄换字体是那种不会有人报警的质量缺陷。

候选顺序是 Windows 在前：那边跑出来的结果与现有成片一字不差；本机没有才往下找。
一个候选都没有时 SystemExit，不静默降级。

每个 kind 实际落到哪个字面（face）会打印一次。降级是有代价的：封面要的是幼圆那种
圆头手写字，落到黑体上画面就变了 —— 变了就得让人看见，而不是事后从成片的字上认出来。
"""
import os
import sys

WIN = "C:/Windows/Fonts/"
MAC = "/System/Library/Fonts/"
MACS = "/System/Library/Fonts/Supplemental/"

# kind -> 候选链。同一 kind 内按"最像原设计那款"排，不按平台排。
CANDIDATES = {
    # 字幕与署名：微软雅黑是成片用的那款，macOS 上最接近的是冬青黑体
    "zh": [WIN + "msyh.ttc", WIN + "msyhbd.ttc", WIN + "simhei.ttf", WIN + "Deng.ttf",
           MAC + "PingFang.ttc", MAC + "Hiragino Sans GB.ttc",
           MAC + "STHeiti Medium.ttc", MACS + "Songti.ttc",
           "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
           "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"],
    # 加粗那一档：封面方案 B 用
    "zh_bold": [WIN + "msyhbd.ttc", WIN + "msyh.ttc", WIN + "simhei.ttf",
                MAC + "STHeiti Medium.ttc", MAC + "Hiragino Sans GB.ttc"],
    # 字幕英文行：Segoe UI
    "en": [WIN + "segoeui.ttf", WIN + "arial.ttf", WIN + "calibri.ttf",
           MAC + "Helvetica.ttc", MAC + "LucidaGrande.ttc"],
    # 封面正文：楷体
    "kai": [WIN + "simkai.ttf", MAC + "Kaiti SC.ttf", MACS + "Kaiti.ttc",
            MACS + "Songti.ttc"],
    # 封面标题：幼圆（圆头、像小孩写的）。macOS 不随附圆体，只能降到黑体
    "round_cn": [WIN + "SIMYOU.TTF", MAC + "Yuanti.ttc", MACS + "Yuanti.ttc",
                 MAC + "STHeiti Light.ttc", MAC + "Hiragino Sans GB.ttc"],
    # 封面英文手写：Ink Free / Segoe Print
    "hand_en": [WIN + "Inkfree.ttf", WIN + "SEGOEPR.TTF",
                MACS + "Bradley Hand Bold.ttf", MAC + "MarkerFelt.ttc",
                MACS + "Chalkboard.ttc"],
}

_RESOLVED = {}
_REPORTED = set()


def font_path(kind):
    """该 kind 第一个真实存在的字体文件；一个都没有就 SystemExit，不降级到默认位图字体。"""
    if kind not in _RESOLVED:
        cands = CANDIDATES[kind]
        p = next((f for f in cands if os.path.exists(f)), None)
        if p is None:
            raise SystemExit("[FAIL] '%s' 这一档找不到任何字体。找过：\n  %s\n"
                             "装一个，或把候选补进 Scripts/Fonts.py。"
                             % (kind, "\n  ".join(cands)))
        _RESOLVED[kind] = p
    return _RESOLVED[kind]


def font_load(kind, size, index=0):
    """按 kind 开一个字号为 size 的字体，并把实际落到的字面报一次。"""
    from PIL import ImageFont
    p = font_path(kind)
    f = ImageFont.truetype(p, size, index=index)
    if kind not in _REPORTED:
        _REPORTED.add(kind)
        print("  [font] %-9s -> %s %s（%s）"
              % ((kind,) + f.getname() + (os.path.basename(p),)), file=sys.stderr)
    return f
