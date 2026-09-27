"""Plobi 头像（只有一颗头）：抠掉头以下的全部，装进一圈**手绘的圆**里。

为什么圆不能是 `draw.ellipse` 画出来的那种：整张片子的线都是蜡笔线，
套一个数学圆进去，就像在小孩画册上贴了一根尺子。所以这圈线走两步——
先在极坐标里给半径和下笔粗细各叠几个低频正弦（手抖和轻重），
再把它喂给探针验收过的那套 `crayon_ink`（位移毛边 + 沙眼 + 拖拽条纹）。
"""
import os
import sys
import numpy as np
import fitz
from PIL import Image, ImageDraw
from scipy import ndimage

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, os.path.join(ROOT, "Ref", "StyleLab"))
import RenderVideo as RV
from Texture import Crayon
from crayon import crayon_ink

OUT = os.path.join(ROOT, "Ref", "StyleLab", "avatars")
CREAM = np.array((253, 237, 210), np.int16)
SIDE = 1024


def hi_res_frame(t, ss=3):
    """把 t 这一帧按 ss 倍分辨率**重画**一遍（矢量，不是插值放大）。

    不走 render_frame_rgb：它 dpi 写死 72，一帧里头只占 ~300 px，拉到 1024 必糊。
    """
    svg = RV.generate_frame_svg(t)
    doc = fitz.open(stream=svg.encode("utf-8"), filetype="svg")
    pix = doc[0].get_pixmap(dpi=72 * ss, colorspace=fitz.csRGB)
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    return np.ascontiguousarray(a[:, :, :3])


def _features(cream, a):
    """脸上的五官：一小坨暗色、外面一圈全是奶油。外描边两道都过不去。"""
    lab, n = ndimage.label(a.astype(np.int16).sum(2) < 300)
    out = []
    for i in range(1, n + 1):
        yy, xx = np.where(lab == i)
        if not (400 < len(yy) < 60000):
            continue
        if yy.max() - yy.min() > 400 or xx.max() - xx.min() > 400:
            continue
        y0, y1 = max(0, yy.min() - 40), min(cream.shape[0], yy.max() + 40)
        x0, x1 = max(0, xx.min() - 40), min(cream.shape[1], xx.max() + 40)
        win = (lab == i)[y0:y1, x0:x1]
        if cream[y0:y1, x0:x1][~win].mean() < 0.90:
            continue
        out.append((int(yy.min()), int(yy.max()), int(xx.min()), int(xx.max())))
    return out


def head_only(a, neck_pad=34, bulge=0.115, outline_pad=34):
    """只留头那颗球，底下的缺口用一道弧收口。

    Plobi 是葫芦形、没有脖子，壳和胳膊就长在头底下，所以"只要头"不是裁一刀就完。
    这里不用逐行找连通段（眼睛和腮红会把那一行的奶油打断，行扫描会把脸切出横条，
    上一版就是这么坏的），改成几条几何条件取交，再按颜色把壳剔掉：
      头顶到嘴底这一段、横向不超过头最宽处、且不是背景蓝 —— 剩下就是头；
      嘴底以下再挂一个半椭圆当下巴，不然脑袋底下是一刀平切。
    """
    cream = (np.abs(a.astype(np.int16) - CREAM).max(axis=2) < 40)
    cream[int(a.shape[0] * 0.78):, :] = False
    feats = _features(cream, a)
    if not feats:
        raise SystemExit("没找到五官，别硬猜取景——先看这一帧脸是不是被挡住了")
    ys, xs = np.where(cream)
    crown = int(ys.min())
    mouth_bottom = max(f[1] for f in feats)
    cx = int(np.mean([(f[2] + f[3]) / 2 for f in feats]))     # 以脸为心，不以包围盒为心
    neck = mouth_bottom + neck_pad

    solid = ndimage.binary_fill_holes(cream)   # 眼睛和腮红是奶油里的洞，先填掉，
    top = max(0, crown - 90)                   # 否则逐行找连通段会在脸上断成横条
    keep = np.zeros(cream.shape, bool)
    half = 0
    for y in range(top, neck + 1):
        row = solid[y]
        idx = np.where(row)[0]
        if not len(idx):
            continue
        k = idx[np.argmin(np.abs(idx - cx))]
        if abs(k - cx) > 900:
            continue
        d = np.diff(np.concatenate(([0], row.astype(np.int8), [0])))
        st = np.where(d == 1)[0]
        en = np.where(d == -1)[0] - 1
        sel = (st <= k) & (en >= k)
        if not sel.any():
            continue
        lo, hi = int(st[sel][0]), int(en[sel][-1])
        keep[y, max(0, lo - outline_pad):hi + outline_pad + 1] = True
        half = max(half, (hi - lo) / 2 + outline_pad)

    navy = _navy(a)
    notbg = (np.abs(a.astype(np.int16) - navy.astype(np.int16)).max(2) > 34)
    # 壳是陶土橙，颜色上就独一份；它从头的左后方探出来，按颜色剔掉最干净。
    # 两条判据缺一不可，而且两边都要先转 int：
    #   uint8 直接 +46 会溢出回绕，237+46 变成 27 —— 上一版整颗头因此被自己剔没；
    #   只看"比绿暖"会把腮红 (250,171,161) 一起当壳吃掉，腮红得靠"绿比蓝高多少"
    #   和壳 (205,107,67) 分开：壳是橙，绿蓝差 40；腮红是粉，绿蓝只差 10。
    ai = a.astype(int)
    shell = (ai[:, :, 0] > ai[:, :, 1] + 60) & (ai[:, :, 1] > ai[:, :, 2] + 25)
    # 壳自己那圈暗描边不是橙色，光按颜色剔会留一道斜着的黑印子，
    # 所以把壳膨胀到描边厚度再一起挖掉。
    shell_zone = ndimage.binary_dilation(shell, iterations=outline_pad)
    keep &= ~shell_zone

    yy, xx = np.mgrid[0:a.shape[0], 0:a.shape[1]].astype(np.float32)
    sag = bulge * half * 2
    a_cap = half * 0.84          # 收口比头最宽处窄一点，不然把胳膊尖带进来
    cap = (yy > neck) & (((xx - cx) / a_cap) ** 2 + ((yy - neck) / sag) ** 2 <= 1.0)
    cap &= notbg & ~shell_zone
    keep |= cap

    # 下巴那道弧原本没有线（切面是奶油本色），补一条和头描边同厚的暗边
    k = outline_pad
    edge = (ndimage.distance_transform_edt(~keep) <= k) & ~keep & (yy > neck - k)
    out = a.copy()
    out[edge] = _ink_color(a, cream)
    top = max(0, crown - 90)
    rows = np.where(keep.any(1))[0]
    cols = np.where(keep.any(0))[0]
    return out, keep, dict(y0=int(rows.min()), y1=int(rows.max()),
                           x0=int(cols.min()), x1=int(cols.max()),
                           cx=int(cx), cy=int((rows.min() + rows.max()) / 2),
                           ink=_ink_color(a, cream))


def _navy(a):
    """片子里那块背景蓝：取最上面几行的中位色，不手搓。"""
    return np.median(a[:6].reshape(-1, 3), axis=0).astype(int)


def _ink_color(a, cream):
    """角色自己的描边色 = 紧贴奶油外侧那一圈的暗像素中位色。

    不能拿"所有暗像素"的中位数：背景蓝 (24,29,45) 亮度也很低，
    那样量出来的是背景色，上一版描边就画成了隐形。
    """
    ring = (ndimage.distance_transform_edt(~cream) <= 26) & ~cream
    d = a[ring]
    d = d[d.astype(np.int16).sum(1) < d.astype(np.int16).sum(1).mean()]
    return tuple(int(v) for v in np.median(d, axis=0))


def hand_circle(shape, cx, cy, r, thick, seed=7, wob=(0.022, 0.014, 0.008),
                n=1440):
    """一个**画出来的**圆：半径和粗细各叠三个低频正弦，返回 0..1 的墨迹场。

    wob 是抖幅。1.6% 这个量级是"看得出是手画的、但没人会说它不圆"的位置；
    再大就开始读成一个blob而不是circle。
    """
    rs = np.random.default_rng(seed)
    ph = rs.uniform(0, 2 * np.pi, 6)
    th = np.linspace(0, 2 * np.pi, n, endpoint=False)
    rr = r * (1 + wob[0] * np.sin(3 * th + ph[0]) + wob[1] * np.sin(5 * th + ph[1])
              + wob[2] * np.sin(9 * th + ph[2]))
    tt = thick * (1 + 0.30 * np.sin(2 * th + ph[3]) + 0.16 * np.sin(7 * th + ph[4]))
    img = Image.new("L", (shape[1], shape[0]), 0)
    d = ImageDraw.Draw(img)
    for i in range(n):
        a0, a1 = th[i], th[i] + 2 * np.pi / n * 1.9
        r0, r1 = rr[i] - tt[i] / 2, rr[i] + tt[i] / 2
        if r1 <= 0 or r0 < 0:
            continue
        d.pieslice([cx - r1, cy - r1, cx + r1, cy + r1],
                   np.degrees(a0) - 1, np.degrees(a1) + 1, fill=255)
        d.pieslice([cx - r0, cy - r0, cx + r0, cy + r0],
                   np.degrees(a0) - 1, np.degrees(a1) + 1, fill=0)
    return np.asarray(img, np.float32) / 255.0


def build(t=7.0, ss=3, fit=0.87, seed=7, tag="head", save=True):
    """fit：头占圆的比例。剩下的 10% 是留给"手画的圆比头略大"这件事的余量。"""
    a = hi_res_frame(t, ss)
    head, keep, info = head_only(a)
    R_OUT = SIDE * 0.44                       # 画布上圆的半径，留 6% 边给毛边
    thick = SIDE * 0.017

    # 缩放由"头到自己的远点距离"决定，不是由外接框对角线决定：
    # 用对角线的话圆会被脑袋两个尖角顶大，脸反而显小。
    yy, xx = np.where(keep)
    d = np.hypot(xx - info["cx"], yy - info["cy"])
    reach = float(d.max())
    side_src = int(reach * 2 / fit * (SIDE / (SIDE - 2 * 6)))
    x0 = int(info["cx"] - side_src / 2); y0 = int(info["cy"] - side_src / 2)
    x0 = max(0, min(x0, a.shape[1] - side_src)); y0 = max(0, min(y0, a.shape[0] - side_src))
    sc = SIDE / side_src
    head_rgb = np.asarray(Image.fromarray(head[y0:y0 + side_src, x0:x0 + side_src])
                          .resize((SIDE, SIDE), Image.LANCZOS).convert("RGB"))
    head_a = np.asarray(Image.fromarray(
        (keep[y0:y0 + side_src, x0:x0 + side_src] * 255).astype(np.uint8))
        .resize((SIDE, SIDE), Image.LANCZOS)).astype(np.float32) / 255.0

    ring_r = reach * sc * fit
    m = hand_circle((SIDE, SIDE), SIDE / 2, SIDE / 2, ring_r, thick, seed=seed)
    m = crayon_ink(m, 0, seed_salt=seed, scale=2.4, freq_fine=40)

    gy, gx = np.mgrid[0:SIDE, 0:SIDE].astype(np.float32)
    rad = np.hypot(gx - SIDE / 2, gy - SIDE / 2)
    disc = rad <= ring_r + thick / 2
    head_a = head_a * (rad <= ring_r + thick * 0.2)      # 万一有尖角，跟着圆切

    ink = np.array(info["ink"], np.float32)
    canvas = np.zeros((SIDE, SIDE, 3), np.float32)
    canvas[:, :, :] = _navy(a).astype(np.float32)
    for c in range(3):
        canvas[:, :, c] = canvas[:, :, c] * (1 - head_a) + head_rgb[:, :, c] * head_a
    canvas = canvas * (1 - m[..., None]) + ink[None, None, :] * m[..., None]
    arr = Crayon(SIDE, SIDE).apply(np.clip(canvas, 0, 255).astype(np.uint8), 88, spot=0.22)

    a_out = np.clip(disc + m, 0, 1)
    rgba = np.dstack([arr, (a_out * 255).astype(np.uint8)])
    if not save:
        return rgba, info, dict(ring_r=ring_r, reach=reach, sc=sc)
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, f"avatar_{tag}.png")
    Image.fromarray(rgba, "RGBA").save(p)
    for nm, base in (("white", np.full((SIDE, SIDE, 3), 255, np.uint8)),
                     ("dark", np.full((SIDE, SIDE, 3), 34, np.uint8))):
        o = base.astype(np.float32); f = a_out[..., None]
        Image.fromarray(np.clip(o * (1 - f) + arr * f, 0, 255).astype(np.uint8)
                        ).save(os.path.join(OUT, f"avatar_{tag}_on{nm}.png"))
    print(f"头 reach={reach:.0f}  取窗 {side_src}px  圆半径 {ring_r:.0f}/{SIDE/2:.0f}  "
          f"描边色 {info['ink']}  -> {p}")
    return p


if __name__ == "__main__":
    build(t=float(sys.argv[sys.argv.index("--t") + 1]) if "--t" in sys.argv else 7.0,
          seed=int(sys.argv[sys.argv.index("--seed") + 1]) if "--seed" in sys.argv else 7)
