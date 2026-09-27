#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""风险窗口夹帧对比：同一批时刻，改造前 / 改造后各渲一帧，拼成左右对照表。

为什么不等间距抽帧：Docs/VibeMotionPlan.md §9 第 11 条——"等间距的帧条会把交接
缺陷完全藏起来"。这里挑的全是事件点与段边界（缺陷最容易现形的地方）。

用法：python Scripts/SnapshotFrames.py
     可选：--only after   只出现改造后的整帧
"""
import importlib.util
import os
import sys

import fitz  # PyMuPDF
from PIL import Image, ImageDraw, ImageFont

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_DIR = os.path.join(ROOT, "Episodes", "EP001_SelfIntro", "Render", "verify_pose")

# (时刻, 属于哪段, 这个点在看什么)
SHOTS = [
    (2.9,  "S1", "卡壳峰值：腿屈膝蹬地 / 整体后仰拽壳"),
    (5.4,  "S1", "弹回落点：腿前伸准备吸收"),
    (12.0, "边界", "段边界 1：右臂从 0 抬向 28°（旧版此处先掉到 0 再弹起）"),
    (19.5, "S2", "蓄力起跳：双腿深蹲压到 22%"),
    (25.5, "S2", "腾空顶点：腿蹬直拉长（旧版腿全程冻结）"),
    (27.9, "S2", "落地接触：屈膝吸收 + 躯干同刻 squash"),
    (50.0, "边界", "段边界 2：右臂 5° → 教鞭 50°（旧版中途掉到 0°）"),
    (69.0, "边界", "段边界 3：鞠躬前一站（旧版右臂在此白甩 42°）"),
    (72.0, "S5", "谢幕摇摆：重心在两腿间来回"),
    (75.0, "S5", "鞠躬：整具装配含龟壳一起前倾"),
]


def load(path, alias):
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cjk_font(size=20):
    """PIL 默认位图字体不含中文字形，标注会变方框。"""
    from Fonts import font_load
    return font_load("zh", size, index=0)


def render(mod, t):
    # 走渲染器自己的像素出口：质感层 + 字幕合成都在里面。
    # 以前这里自己光栅化 SVG，抽出来的帧和成片差一层后处理，目视检查白做。
    arr = mod.render_frame_rgb(t, int(round(t * 30)))
    return Image.fromarray(arr).convert("RGB")


def main():
    only_after = "--only" in sys.argv and sys.argv[-1] == "after"
    os.makedirs(OUT_DIR, exist_ok=True)

    new = load(os.path.join(HERE, "RenderVideo.py"), "rv_new")
    base = None
    if not only_after:
        src = os.path.join(HERE, "_baseline_tmp.py")
        # 基线 = git HEAD 版本；由调用方事先取出，缺失则退化为只出现版本
        if not os.path.exists(src):
            print("[!] 未找到基线 %s，只输出改造后帧" % src)
        else:
            base = load(src, "rv_base")

    W, H = 900, 506           # 16:9 缩略
    GAP, LABEL, ROW = 12, 30, 8
    cols = 1 if base is None else 2
    sheet = Image.new("RGB", (cols * W + (cols + 1) * GAP,
                              len(SHOTS) * (H + LABEL + ROW) + GAP), (18, 18, 20))

    dr = ImageDraw.Draw(sheet)
    f_note = cjk_font(21)
    f_tag = cjk_font(19)
    for r, (t, tag, note) in enumerate(SHOTS):
        y0 = GAP + r * (H + LABEL + ROW)
        dr.text((GAP + 2, y0 + 3), "t=%.1fs  [%s]  %s" % (t, tag, note),
                fill=(235, 235, 235), font=f_note)
        y = y0 + LABEL
        imgs = []
        if base is not None:
            imgs.append(("改造前", render(base, t)))
        imgs.append(("改造后", render(new, t)))
        for c, (label, img) in enumerate(imgs):
            x = GAP + c * (W + GAP)
            sheet.paste(img.resize((W, H)), (x, y))
            dr.text((x + 5, y + H - 26), label, fill=(255, 220, 120), font=f_tag)

    out = os.path.join(OUT_DIR, "compare_before_after.png")
    sheet.save(out)
    print("[+] %d 组对照帧 → %s" % (len(SHOTS), out))
    # 同时存单帧，便于放大看局部
    for t, tag, note in SHOTS:
        render(new, t).save(os.path.join(OUT_DIR, "after_%05.1f.png" % t))
    print("[+] 单帧目录: %s" % OUT_DIR)


if __name__ == "__main__":
    main()
