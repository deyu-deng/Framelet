"""Plobi 头像：从生产路径的矢量帧里现栅格化一颗头，正方形，能扛住圆形裁切。

三个不显然的点：
1. **不走 render_frame_rgb**。它把 dpi 写死在 72，1920 宽的画面里头只有 ~300 px，
   放大到 1024 会糊。这里直接拿 generate_frame_svg 的高分辨率栅格，头是矢量重画的，
   1024 px 上每个边缘都是原生精度。
2. **B 站头像在多数位置是圆着显示的**，所以判据不是"正方形里放得下"，
   而是"内切圆里放得下"——头顶和两颊必须落在半径 512 的圆内。
3. 质感层只在最后过一次，和封面同一组参数，两个东西摆在一起看是同一只手画的。
"""
import os
import sys
import numpy as np
import fitz
from PIL import Image, ImageDraw
from scipy import ndimage

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import RenderVideo as RV
from Texture import Crayon

OUT = os.path.join(ROOT, "Ref", "StyleLab", "avatars")
CREAM = np.array((253, 237, 210), np.int16)
SIDE = 1024


def hi_res_frame(t, ss=3):
    """把 t 这一帧按 ss 倍分辨率重画一遍（矢量，不是放大）。"""
    svg = RV.generate_frame_svg(t)
    doc = fitz.open(stream=svg.encode("utf-8"), filetype="svg")
    pix = doc[0].get_pixmap(dpi=72 * ss, colorspace=fitz.csRGB)
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    return np.ascontiguousarray(a[:, :, :3]), ss


def _features(m, a):
    """脸上的五官：眼睛和嘴。

    判据是"一小坨暗色、整圈被奶油包住"——外描边虽然也是暗的，但它横跨整颗头，
    面积和长宽都远超五官，且它外面是背景不是奶油，两道都过不去。
    """
    lab, n = ndimage.label(a.astype(np.int16).sum(2) < 300)
    own = np.zeros(a.shape[:2], bool)
    out = []
    for i in range(1, n + 1):
        yy, xx = np.where(lab == i)
        if not (400 < len(yy) < 60000):
            continue
        if yy.max() - yy.min() > 400 or xx.max() - xx.min() > 400:
            continue
        y0, y1 = max(0, yy.min() - 40), min(m.shape[0], yy.max() + 40)
        x0, x1 = max(0, xx.min() - 40), min(m.shape[1], xx.max() + 40)
        own[:, :] = False
        own[y0:y1, x0:x1] = (lab == i)[y0:y1, x0:x1]
        # 只算"这颗以外"的那一圈：五官自己占掉框里近两成，
        # 把它算进分母的话均值最高只能到 0.81，判 0.9 就永远过不了。
        ring = m[y0:y1, x0:x1][~own[y0:y1, x0:x1]]
        if ring.mean() < 0.90:
            continue
        out.append((int(yy.min()), int(yy.max()), int(xx.min()), int(xx.max())))
    return out


def head_box(a):
    """头那颗球：头顶到嘴底，宽度只在这段里量。

    Plobi 是葫芦形，没有脖子。直接按奶油色整体外接框裁会把胳膊和壳一起带进来，
    头像就变成"一个人站在中间"；而嘴底往下没多少就是胳膊长出来的地方，
    所以拿**最低那颗五官**当头的下沿，是量得出来又有意义的切法。
    """
    m = (np.abs(a.astype(np.int16) - CREAM).max(axis=2) < 40)
    m[int(a.shape[0] * 0.78):, :] = False        # 脚下阴影与字幕区不算
    ys, xs = np.where(m)
    crown = int(ys.min())
    feats = _features(m, a)
    if not feats:
        raise SystemExit("没找到五官，别硬猜取景——先看这一帧脸是不是被挡住了")
    bottom = max(f[1] for f in feats)            # 嘴底
    seg = m[crown:bottom + 1]
    rows = np.where(seg.any(1))[0] + crown
    widths = seg.sum(1)
    widest = int(rows[np.argmax(widths)])
    xs2 = np.where(seg.any(0))[0]
    return dict(crown=crown, mouth_bottom=bottom, widest_y=widest,
                x0=int(xs2.min()), x1=int(xs2.max()),
                cx=(int(xs2.min()) + int(xs2.max())) / 2, n_feat=len(feats))


def build(t=7.0, fill=0.62, crown_frac=0.12, ss=3, tag="plobi", save=True):
    """fill：头最宽处占方框边长的比例。crown_frac：头顶离方框顶的距离。

    fill=0.62 不是随手取的：头像在评论区和动态里是**圆着显示**的，圆越往下越窄，
    而恰好是胳膊那一层最宽。0.62 是胳膊描边还落在内切圆里、留 ~100 px 余量的上限。
    """
    a, ss = hi_res_frame(t, ss)
    hb = head_box(a)
    hw = hb["x1"] - hb["x0"]
    side = int(hw / fill)
    y0 = int(hb["crown"] - side * crown_frac)
    x0 = int(hb["cx"] - side / 2)
    H, W = a.shape[:2]
    x0 = max(0, min(x0, W - side)); y0 = max(0, min(y0, H - side))
    crop = a[y0:y0 + side, x0:x0 + side]
    img = Image.fromarray(crop).resize((SIDE, SIDE), Image.LANCZOS)
    arr = Crayon(SIDE, SIDE).apply(np.asarray(img.convert("RGB")), 88, spot=0.3)
    if not save:
        return arr, hb, side, (x0, y0)
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, f"avatar_{tag}.png")
    Image.fromarray(arr).save(p)
    chk = Image.fromarray(arr).convert("RGB")
    d = ImageDraw.Draw(chk)
    d.ellipse([6, 6, SIDE - 6, SIDE - 6], outline=(90, 220, 130), width=6)
    chk.save(os.path.join(OUT, f"avatar_{tag}_circle.png"))
    Image.fromarray(arr).resize((96, 96), Image.LANCZOS).save(
        os.path.join(OUT, f"avatar_{tag}_96.png"))
    print(f"五官{hb['n_feat']}颗  头宽 {hw}  头顶 y{hb['crown']}  嘴底 y{hb['mouth_bottom']}  "
          f"方框边长 {side}  取窗 x{x0}..{x0+side} y{y0}..{y0+side}  -> {p}")
    return p


def _arg(name, cast, default):
    return cast(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


if __name__ == "__main__":
    build(t=_arg("--t", float, 7.0), fill=_arg("--fill", float, 0.62),
          crown_frac=_arg("--crown", float, 0.12), ss=_arg("--ss", int, 3),
          tag=_arg("--tag", str, "plobi"))
