"""EP001 封面 V1 收尾：中文改成**空心勾线字**，换色、加粗。

"用描边的形式写"＝把字写成空心轮廓（勾线），不是给实心字加一圈黑边。
后者是营销号写法，上一版就是栽在这儿。空心勾线还有个附带好处：
同一条线压在暗底和亮底上都读得出来，正好治 V1 那句跨到脸上的对比度问题。

做法：字形先栅格化成掩膜 → 膨胀一圈 → 减掉原掩膜 = 只剩外轮廓环。
环宽控制"加粗"程度；逐字旋转抖动照旧；最后整张过一次 Crayon，
让勾线也带上蜡笔的毛边，否则空心字会显得太"矢量"。
"""
import os
import sys
import math
import random
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageChops

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import RenderVideo as RV
from Texture import Crayon
from Fonts import font_path
from MakeCoverFinal import face_canvas, hand_text, W, H, OUT

# 空心勾线需要粗笔画，但微软雅黑 Bold 不稚嫩。解法：**用幼圆，先把字形栅格"养胖"**
# （MaxFilter 膨胀 2 次），再做空心环 —— 既保住圆头小孩字的形状，又拿到够粗的笔画。
F_CN_KID = font_path("round_cn")   # 幼圆

# 三个方向，都刻意避开龟壳的陶土色 #CD6B43
SWATCH = {
    "mint": (159, 224, 200),        # 淡薄荷
    "lilac": (201, 182, 232),       # 薰衣草
    "cream": (246, 241, 228),       # 奶油白（与身体同族，靠勾线本身拉开）
}


def hollow_glyph(ch, fnt, color, ring=5, shadow=(10, 12, 20, 150)):
    """把一个字画成空心勾线，返回 RGBA 贴片。"""
    box = fnt.size * 2
    m = Image.new("L", (box, box), 0)
    d = ImageDraw.Draw(m)
    bb = d.textbbox((0, 0), ch, font=fnt)
    d.text((box // 2 - (bb[0] + bb[2]) // 2, box // 2 - (bb[1] + bb[3]) // 2),
           ch, font=fnt, fill=255)
    fat = m.filter(ImageFilter.MaxFilter(5))      # 养胖：细笔画也够撑出空心
    fat = fat.point(lambda v: 255 if v > 90 else 0)
    outer = fat.filter(ImageFilter.MaxFilter(1 + 2 * ring))
    band = ImageChops.subtract(outer, fat)        # 只留外圈环 = 空心
    g = Image.new("RGBA", (box, box), (0, 0, 0, 0))
    if shadow is not None:
        sh = Image.new("RGBA", (box, box), (0, 0, 0, 0))
        sh.putalpha(band)
        g.alpha_composite(sh, (3, 4))
    line = Image.new("RGBA", (box, box), color + (255,))
    line.putalpha(band)
    g.alpha_composite(line)
    return g


def hand_hollow_line(base, text, x, y, size, color, ring=5, tilt=0.0, arc=0.0,
                     jitter_rot=6.0, jitter_y=8.0, seed=5, spread=1.06):
    fnt = ImageFont.truetype(F_CN_KID, size)
    rnd = random.Random(seed)
    adv = [fnt.getbbox(c)[2] * spread for c in text]
    total = sum(adv) + size * 0.10 * len(text)
    pen = x - total / 2
    for i, c in enumerate(text):
        g = hollow_glyph(c, fnt, color, ring)
        rot = tilt + rnd.uniform(-jitter_rot, jitter_rot)
        dy = math.sin((i / max(1, len(text) - 1)) * math.pi - math.pi / 2) * arc + \
            rnd.uniform(-jitter_y, jitter_y)
        g = g.rotate(rot, resample=Image.BICUBIC)
        base.alpha_composite(g, (int(pen + adv[i] / 2 - g.width / 2),
                                 int(y + dy - g.height / 2)))
        pen += adv[i] + size * 0.10
    return base


EN_FONT = font_path("hand_en")     # Ink Free

# 实测（他 2026-09-22 的投稿页截图）：首页推荐封面 = 从 16:9 原图**上下不裁、
# 左右各裁 12.5%**，即居中 75% 宽那条竖带。1920×1080 上就是 x∈[240,1680]。
BAND_X0, BAND_X1 = 240, 1680
BAND_W = BAND_X1 - BAND_X0


def face_canvas_crown(crown_y=0.235, share=0.92, t=7.0):
    """和 face_canvas 同一取景、同一放大倍率，只把取景框往上挪，
    让**头顶**落在画面 crown_y 处。

    为什么往上挪而不是把脸缩小：源帧头顶以上只有 261 px 暗底，
    想把标题放到头顶上方又不缩小脸，唯一的办法是把取景框抬高 ——
    脸的大小、蜡笔倍率全不变，只是腾出一条暗底带。
    """
    f = RV.render_frame_rgb(t, int(round(t * RV.FPS)))
    m = (np.abs(f.astype(np.int16) - np.array((253, 237, 210), np.int16)).max(axis=2) < 40)
    m[int(H * 0.78):, :] = False
    ys, xs = np.where(m)
    y0 = int(ys.min())
    y1 = int(y0 + (int(ys.max()) - y0) * 0.55)
    cx = (int(xs.min()) + int(xs.max())) / 2
    ch = int(min((y1 - y0) / share, H))
    cw = int(ch * 16 / 9)
    by = max(0, min(int(round(y0 - crown_y * ch)), H - ch))
    bx = int(min(max(cx - cw / 2, 0), W - cw))
    return np.asarray(Image.fromarray(f[by:by + ch, bx:bx + cw]).resize((W, H), Image.LANCZOS))


def build_43(tag="43", color=None, ring=4, crown_y=0.212, share=0.875, cn_size=92,
             cn_y=0.115, cn_arc=20, en_x=0.186, en_size=70, save=True):
    """按 4:3 安全带给整张排版：**所有文字都落在 x∈[240,1680] 里**，
    所以一张图能同时喂首页推荐（4:3 裁切）和个人空间（16:9 原图）。

    share 比 16:9 版的 0.92 小一点：脸缩 5%，换回两样东西——
    嘴巴不被底边切掉，以及左侧那条暗带宽到能放下 "my first video" 不压头轮廓。
    """
    color = color or SWATCH["mint"]
    img = Image.fromarray(face_canvas_crown(crown_y=crown_y, share=share)).convert("RGBA")
    for i, (word, dy, tl) in enumerate((("my", 0.375, -9), ("first", 0.475, 5),
                                        ("video", 0.575, -4))):
        img = hand_text(img, word, W * en_x, H * dy, en_size, EN_FONT,
                        (168, 178, 205), tilt=tl, seed=31 + i,
                        arc=0, jitter_rot=4, jitter_y=4)
    cx = (BAND_X0 + BAND_X1) / 2          # 960：安全带中心，不是画布中心
    img = hand_hollow_line(img, "求求了，点进来留下点什么吧", cx, H * cn_y,
                           cn_size, color, ring=ring, tilt=-2.5, arc=cn_arc, seed=12)
    arr = Crayon(H, W).apply(np.asarray(img.convert("RGB")), 88, spot=0.3)
    if not save:
        return arr
    p = os.path.join(OUT, f"cover_{tag}.png")
    Image.fromarray(arr).save(p)
    Image.fromarray(arr[:, BAND_X0:BAND_X1]).save(os.path.join(OUT, f"cover_{tag}_crop43.png"))
    chk = Image.fromarray(arr).convert("RGB")
    d = ImageDraw.Draw(chk)
    for x in (BAND_X0, BAND_X1):
        d.line([x, 0, x, H], fill=(90, 220, 130), width=5)
    chk.save(os.path.join(OUT, f"cover_{tag}_check.png"))
    return p


def build(tag, color, ring=4, cn_y=0.235, final=False):
    img = Image.fromarray(face_canvas()).convert("RGBA")
    if final:
        # 英文改成竖列：一个单词一行，塞进 Plobi 左侧、"求求了"下面那条竖直空白带。
        # 每行各自歪一点，别对齐成表格。
        for i, (word, dy, tl) in enumerate((("my", 0.355, -9), ("first", 0.455, 5),
                                            ("video", 0.555, -4))):
            img = hand_text(img, word, W * 0.108, H * dy, 74, EN_FONT,
                            (168, 178, 205), tilt=tl, seed=31 + i,
                            arc=0, jitter_rot=4, jitter_y=4)
        # 中文往回收一点：既更居中，也顺手解决"吧"贴右边缘
        img = hand_hollow_line(img, "求求了，点进来留下点什么吧", W * 0.525, H * cn_y,
                               100, color, ring=ring, tilt=-2.5, arc=22, seed=12)
    else:
        img = hand_text(img, "my first video", W * 0.17, H * 0.12, 62, EN_FONT,
                        (168, 178, 205), tilt=-6, seed=11, arc=0,
                        jitter_rot=5, jitter_y=5)
        img = hand_hollow_line(img, "求求了，点进来留下点什么吧", W * 0.575, H * cn_y,
                               108, color, ring=ring, tilt=-2.5, arc=26, seed=12)
    arr = Crayon(H, W).apply(np.asarray(img.convert("RGB")), 88, spot=0.3)
    p = os.path.join(OUT, f"cover_V1_{tag}.png")
    Image.fromarray(arr).save(p)
    return p


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    if "--43" in sys.argv:
        p = build_43()
        print("已出", p, "cover_43_crop43.png / cover_43_check.png")
        raise SystemExit(0)
    if "--final" in sys.argv:
        p = build("FINAL", SWATCH["mint"], final=True)
        im = Image.open(p)
        im.resize((320, 180), Image.LANCZOS).save(os.path.join(OUT, "cover_FINAL_small.png"))
        # 安全区检查：上下各 6.25% 的裁切线 + 右下角时长角标占位
        chk = im.convert("RGB").copy()
        d = ImageDraw.Draw(chk)
        d.rectangle([0, int(H * .0625), W, int(H * .9375)], outline=(90, 220, 130), width=6)
        d.rectangle([W - 300, H - 92, W - 24, H - 24], outline=(230, 90, 90), width=6)
        chk.save(os.path.join(OUT, "cover_FINAL_check.png"))
        print("已出", p, "cover_FINAL_small.png / cover_FINAL_check.png")
        raise SystemExit(0)
    made = [build(k, c) for k, c in SWATCH.items()]
    strip = Image.new("RGB", (320 * 3 + 40, 180 * 2 + 30), (24, 20, 15))
    for i, p in enumerate(made):
        strip.paste(Image.open(p).resize((320, 180), Image.LANCZOS), (10 + i * 330, 10))
    strip.save(os.path.join(OUT, "cover_V1_colors.png"))
    print("已出：", *[os.path.basename(p) for p in made], "cover_V1_colors.png", sep="\n  ")
