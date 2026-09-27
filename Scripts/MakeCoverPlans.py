"""EP001 封面方案对比。标题定为
「这是一条由代码生成的广告，但是自我介绍」——封面文案只从这句里**截取**，不另写。

三条路数，各自验证一条要素：
  A 文字主导：把"广告"划掉改成"自我介绍"，涂改痕迹本身就是质感与张力
  B 概念主导：让"由代码生成"可见 —— 屏幕上蠕动的矢量线条（原稿自己的说法）
  C 角色主导：不用壳，纯脸特写，赌"有眼睛"的识别优势

统一处理：所有文字都走同一层 Crayon 抖动与纸纹，避免"贴纸感"。
"""
import os
import sys
import math
import numpy as np
import fitz
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import RenderVideo as RV
from Texture import Crayon
from Fonts import font_path

OUT = os.path.join(ROOT, "Ref", "StyleLab", "covers")
os.makedirs(OUT, exist_ok=True)
W, H = 1920, 1080
INK = (36, 24, 18)
CREAM = (253, 237, 210)
TERRA = (205, 107, 67)
BLUSH = (250, 171, 161)
SKY = (26, 32, 53)
GND = (16, 19, 30)

FONTS = {                      # kind -> 候选链见 Scripts/Fonts.py
    "kai": font_path("kai"),
    "hei": font_path("zh"),
    "hei_b": font_path("zh_bold"),
}


def font(kind, size):
    return ImageFont.truetype(FONTS[kind], size)


def bg():
    a = np.empty((H, W, 3), np.float32)
    yy = np.linspace(0, 1, H)[:, None, None]
    a[:] = (1 - yy) * np.array(SKY, np.float32) + yy * np.array(GND, np.float32)
    # 一束顶光，让主体有落点
    yy2, xx2 = np.mgrid[0:H, 0:W].astype(np.float32)
    half = np.interp(yy2, [-40, H], [W * 0.10, W * 0.34])
    cone = np.clip(1 - (np.abs(xx2 - W / 2) - half * 0.55) / (half * 0.5 + 1), 0, 1)
    cone *= np.clip(1.05 - yy2 / (H * 1.15), 0, 1) ** 0.5
    cone = np.clip(cone, 0, 1)[..., None] * 0.55
    a = a * (1 - cone) + np.array([232, 226, 208], np.float32) * cone
    return a.astype(np.uint8)


def draw_text(d, xy, txt, fnt, fill=CREAM, stroke=INK, sw=9, anchor="mm"):
    """字必须有暗色描边：底色是渐变，白字没有描边会在亮部直接消失。"""
    d.text(xy, txt, font=fnt, fill=fill, stroke_width=sw, stroke_fill=stroke, anchor=anchor)


def strike(d, x0, y0, x1, y1, w=14, color=INK):
    """手绘感的划掉：三段轻微起伏的线，不是一条尺子线。"""
    n = 24
    pts = []
    for i in range(n + 1):
        p = i / n
        x = x0 + (x1 - x0) * p
        y = y0 + (y1 - y0) * p + math.sin(p * 7.3) * 5 + (i % 3 - 1) * 2.2
        pts.append((x, y))
    d.line(pts, fill=color, width=w, joint="curve")


def wriggle(d, seed, n_lines=5, span=(300, 1620), ymid=560):
    """"不着边际的矢量线条在屏幕上蠕动"：正弦叠加的开放曲线，粗黑描边。"""
    rng = np.random.default_rng(seed)
    for k in range(n_lines):
        amp = 40 + rng.random() * 90
        f1 = 1.1 + rng.random() * 2.2
        f2 = 3.0 + rng.random() * 4.0
        ph = rng.random() * 6.28
        y0 = ymid + (k - n_lines / 2) * (150 + rng.random() * 60)
        xs = np.linspace(span[0], span[1], 160)
        ys = y0 + np.sin(xs / 190 * f1 + ph) * amp + np.sin(xs / 63 * f2 + ph * 2) * amp * 0.22
        pts = list(zip(xs.tolist(), ys.tolist()))
        d.line(pts, fill=INK, width=26, joint="curve")
        d.line(pts, fill=CREAM, width=13, joint="curve")


def save(name, arr, frame_idx=42):
    """统一在最后一步上质感：字、线条、角色吃同一层抖动与纸纹。"""
    arr = Crayon(H, W).apply(np.asarray(arr, np.uint8), frame_idx, spot=0.35)
    Image.fromarray(arr).save(os.path.join(OUT, name))
    return arr


def cutout(rgb, want_h, hexcol=CREAM, tol=52):
    """从成片帧里把角色抠出来（带 alpha），而不是裁一个矩形贴上去。
    贴矩形必然留接缝：裁片自带的那块背景色和封面背景不一样。"""
    tgt = np.array(hexcol, np.int16)
    m = (np.abs(rgb.astype(np.int16) - tgt).max(axis=2) < tol)
    m[int(H * 0.78):, :] = False                      # 排除字幕带
    # 从米白主体做一次膨胀，把黑描边与腮红一起带上
    from scipy import ndimage
    m = ndimage.binary_dilation(m, np.ones((17, 17)))
    m = ndimage.binary_closing(m, np.ones((9, 9)))
    ys, xs = np.where(m)
    pad = 6
    y0, y1 = max(0, ys.min() - pad), min(rgb.shape[0], ys.max() + pad)
    x0, x1 = max(0, xs.min() - pad), min(rgb.shape[1], xs.max() + pad)
    crop = rgb[y0:y1, x0:x1].astype(np.float32)
    a = m[y0:y1, x0:x1].astype(np.float32)
    a = ndimage.gaussian_filter(a, 1.6)               # 边缘羽化，避免硬锯齿
    a = np.clip(a * 1.25, 0, 1)[..., None]
    sc = want_h / crop.shape[0]
    crop = np.asarray(Image.fromarray(crop.astype(np.uint8)).resize(
        (int(crop.shape[1] * sc), want_h), Image.LANCZOS), np.float32)
    a = np.asarray(Image.fromarray((a[..., 0] * 255).astype(np.uint8)).resize(
        (crop.shape[1], want_h), Image.LANCZOS), np.float32)[..., None] / 255.0
    return crop, a


def over(dst, src, alpha, cx, cy):
    h, w = src.shape[:2]
    x0, y0 = int(cx - w / 2), int(cy - h / 2)
    x1, y1 = x0 + w, y0 + h
    sx0, sy0 = max(0, -x0), max(0, -y0)
    dx0, dy0 = max(0, x0), max(0, y0)
    dx1, dy1 = min(dst.shape[1], x1), min(dst.shape[0], y1)
    tw, th = dx1 - dx0, dy1 - dy0
    if tw <= 0 or th <= 0:
        return dst
    reg = dst[dy0:dy1, dx0:dx1].astype(np.float32)
    aa = alpha[sy0:sy0 + th, sx0:sx0 + tw]
    dst[dy0:dy1, dx0:dx1] = reg * (1 - aa) + src[sy0:sy0 + th, sx0:sx0 + tw] * aa
    return dst


def crop_subject(t, share=0.68, head_only=False):
    f = RV.render_frame_rgb(t, int(round(t * RV.FPS)))
    tgt = np.array(CREAM, np.int16)
    m = (np.abs(f.astype(np.int16) - tgt).max(axis=2) < 40)
    m[int(H * 0.78):, :] = False          # 排除字幕带
    ys, xs = np.where(m)
    x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())
    if head_only:
        y1 = y0 + int((y1 - y0) * 0.52)   # 只留上半张头
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    want = (y1 - y0) / share
    ch = int(min(want, H)); cw = int(ch * 16 / 9)
    if cw > W:
        cw = W; ch = int(cw * 9 / 16)
    bx0 = int(min(max(cx - cw / 2, 0), W - cw)); by0 = int(min(max(cy - ch / 2, 0), H - ch))
    out = f[by0:by0 + ch, bx0:bx0 + cw]
    return np.asarray(Image.fromarray(out).resize((W, H), Image.LANCZOS))


def main():
    # ---- A 文字主导：涂改版 ----
    img = Image.fromarray(bg()); d = ImageDraw.Draw(img)
    f_big = font("kai", 168)
    f_sm = font("kai", 150)
    draw_text(d, (W // 2, 300), "这是一条广告", f_big)
    strike(d, W // 2 - 470, 300, W // 2 + 470, 300, w=17)
    draw_text(d, (W // 2, 500), "但是自我介绍", f_sm, fill=BLUSH)
    # 实测：74px 的字在 320px 宽下完全读不出。要么放大到 100+，要么删掉。
    # 标题里已经有"由代码生成"，封面不必重复 —— 按要素一，删。
    a = save("plan_A_text.png", np.array(img))

    # ---- B 概念主导：矢量线条蠕动 + 小角色 ----
    img = Image.fromarray(bg()); d = ImageDraw.Draw(img)
    wriggle(d, seed=7, n_lines=3)              # 5 条时 320px 下糊成噪声，减到 3 条
    a = np.array(img).astype(np.float32)
    src = RV.render_frame_rgb(5.0, 150).astype(np.float32)
    cut, alpha = cutout(src.astype(np.uint8), int(H * 0.50))
    a = over(a, cut, alpha, W * 0.5, H * 0.72)
    b = save("plan_B_concept.png", a.astype(np.uint8))

    # ---- C 角色主导：无壳，脸特写 ----
    c = save("plan_C_face.png", crop_subject(7.0, share=0.95, head_only=True))

    # ---- 小尺度实测条 ----
    names = ["plan_A_text.png", "plan_B_concept.png", "plan_C_face.png"]
    strip = Image.new("RGB", (320 * 3 + 40, 180 * 2 + 60), (24, 20, 15))
    dd = ImageDraw.Draw(strip)
    for i, n in enumerate(names):
        im = Image.open(os.path.join(OUT, n)).resize((320, 180), Image.LANCZOS)
        x, y = 10 + (i % 3) * 330, 10 + (i // 3) * 200
        strip.paste(im, (x, y))
        dd.text((x + 4, y + 182), n.replace("plan_", "").replace(".png", ""), fill=(220, 220, 220))
    strip.save(os.path.join(OUT, "plans_small.png"))
    print("已出：", "  ".join(names), " plans_small.png")


if __name__ == "__main__":
    main()
