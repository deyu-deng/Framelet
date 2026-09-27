"""EP001 封面终稿方向：C 的脸特写 + 两句"标题之外"的话。

去俗气的三条硬规矩（上一版犯的错就是不遵守）：
  1. **文字不加黑色描边**。那圈 stroke 是营销号字体的标志，一加就俗。
     要可读性就用极淡的暗色投影，或者把字放在画面本来就暗的地方。
  2. **不居中堆叠**。居中三行 = 海报模板。字要歪、要散、要像随手写的。
  3. **字必须吃同一层蜡笔质感**。干净地贴上去的字，在手绘画面里永远是贴纸。

字体：中文用幼圆（圆头、像小孩写的），英文用 Segoe Print（手写）。
逐字旋转 + 上下抖动 + 沿弧排布，最后整张过一遍 Crayon。
"""
import os
import sys
import math
import random
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import RenderVideo as RV
from Texture import Crayon
from Fonts import font_path
from MakeCoverPlans import cutout, over          # 复用已验证的抠图，别重写一份

OUT = os.path.join(ROOT, "Ref", "StyleLab", "covers")
W, H = 1920, 1080
CREAM = (253, 237, 210)
BLUSH = (250, 171, 161)
DIM = (168, 178, 205)

F_CN = font_path("round_cn")     # 幼圆：圆头、像小孩写的
F_EN = font_path("hand_en")      # Ink Free


def face_canvas(share=0.92, t=7.0):
    """C 方案：只留头，占画面高 share。"""
    f = RV.render_frame_rgb(t, int(round(t * RV.FPS)))
    tgt = np.array(CREAM, np.int16)
    m = (np.abs(f.astype(np.int16) - tgt).max(axis=2) < 40)
    m[int(H * 0.78):, :] = False
    ys, xs = np.where(m)
    y0, y1 = int(ys.min()), int(ys.min() + (int(ys.max()) - int(ys.min())) * 0.55)
    cx, cy = (int(xs.min()) + int(xs.max())) / 2, (y0 + y1) / 2
    want = (y1 - y0) / share
    ch = int(min(want, H)); cw = int(ch * 16 / 9)
    if cw > W:
        cw, ch = W, int(cw * 9 / 16)
    bx = int(min(max(cx - cw / 2, 0), W - cw)); by = int(min(max(cy - ch / 2, 0), H - ch))
    return np.asarray(Image.fromarray(f[by:by + ch, bx:bx + cw]).resize((W, H), Image.LANCZOS))


def hand_text(base, text, x, y, size, fontpath, color, tilt=0.0, spread=1.0,
              jitter_rot=7.0, jitter_y=9.0, arc=0.0, seed=3, shadow=True):
    """逐字排版：每个字单独旋转、单独上下抖，整行再沿弧走一段。
    不加描边；靠极淡的暗投影保证在亮部也读得出来。"""
    rnd = random.Random(seed)
    fnt = ImageFont.truetype(fontpath, size)
    # 先量总宽，好让整块居中于给定的 x
    widths = []
    for ch in text:
        bb = fnt.getbbox(ch)
        widths.append((bb[2] - bb[0]) * spread)
    total = sum(widths) + size * 0.12 * len(text)
    pen = x - total / 2
    for i, (ch, cw) in enumerate(zip(text, widths)):
        gi = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
        gd = ImageDraw.Draw(gi)
        px, py = gi.width // 2, gi.height // 2
        if shadow:
            gd.text((px + 3, py + 4), ch, font=fnt, fill=(10, 12, 20, 130))
        gd.text((px, py), ch, font=fnt, fill=color + (255,))
        rot = tilt + rnd.uniform(-jitter_rot, jitter_rot)
        dy = math.sin((i / max(1, len(text) - 1)) * math.pi - math.pi / 2) * arc + \
            rnd.uniform(-jitter_y, jitter_y)
        gi = gi.rotate(rot, resample=Image.BICUBIC)
        base.alpha_composite(gi, (int(pen + cw / 2 - gi.width / 2), int(y + dy - gi.height / 2)))
        pen += cw + size * 0.12
    return base


def build(name, cn_pos, en_pos, cn_size, en_size, arc, tilt_cn, seed):
    img = Image.fromarray(face_canvas()).convert("RGBA")
    # 英文小标签：手写体，歪一点
    img = hand_text(img, "my first video", en_pos[0], en_pos[1], en_size, F_EN,
                    DIM, tilt=-6, seed=seed, arc=0, jitter_rot=5, jitter_y=5)
    # 中文主句：幼圆，沿弧走，整体歪
    img = hand_text(img, "求求了，点进来留下点什么吧", cn_pos[0], cn_pos[1], cn_size, F_CN,
                    BLUSH, tilt=tilt_cn, seed=seed + 1, arc=arc)
    arr = np.asarray(img.convert("RGB"))
    arr = Crayon(H, W).apply(arr, 88, spot=0.3)      # 字和画吃同一层抖动与纸纹
    Image.fromarray(arr).save(os.path.join(OUT, name))
    return arr


if __name__ == "__main__":
    # V1：话压在头顶上方的暗部，英文甩在左上
    build("cover_V1.png", (W * 0.52, H * 0.20), (W * 0.17, H * 0.12), 96, 62, 26, -2.5, 11)
    # V2：话走右下斜向，英文放左下，画面更空
    build("cover_V2.png", (W * 0.62, H * 0.30), (W * 0.16, H * 0.72), 104, 66, -34, 6.0, 23)

    strip = Image.new("RGB", (320 * 2 + 30, 180 * 2 + 40), (24, 20, 15))
    for i, n in enumerate(["cover_V1.png", "cover_V2.png"]):
        strip.paste(Image.open(os.path.join(OUT, n)).resize((320, 180), Image.LANCZOS),
                    (10 + (i % 2) * 330, 10 + (i // 2) * 195))
    strip.save(os.path.join(OUT, "cover_V_strip.png"))
    print("已出 cover_V1.png / cover_V2.png / cover_V_strip.png")
