"""EP001 封面候选。三条规矩是这张脚本里能验证的，不是嘴上说的：

1. **不能直接拿成片帧当封面。** 成片是给 1080p 大屏构图的，角色只占画面约 1/4 高；
   缩略图在手机上只有 ~320px 宽，那么大的主体会糊成一粒。封面必须重新构图，
   把主体顶到画面高度的 60% 以上。
2. **质感必须在最终分辨率上现生成。** 先缩小再抖动，颗粒会变成脏；
   所以这里先裁切放大，最后一步才上 Crayon，让纸纹和线条抖动按最终像素算。
3. **文字位置只画框，不替你写文案。** 封面文案是你的话，我给的应该是"能放多大、放哪不被裁"。

产物：covers/cover_A_closeup.png / cover_B_suspense.png / cover_A_safearea.png
      covers/cover_small_strip.png（320px 宽，模拟手机信息流）
"""
import os
import sys
import numpy as np
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import RenderVideo as RV
from Texture import Crayon

OUT = os.path.join(ROOT, "Ref", "StyleLab", "covers")
os.makedirs(OUT, exist_ok=True)
W, H = 1920, 1080


def frame(t):
    return RV.render_frame_rgb(t, int(round(t * RV.FPS)))


def find_subject(rgb, hexcol, tol=40, y_max_frac=0.78):
    """按主色找包围盒。**必须排除字幕带**：字幕是近白色的，
    不设上限的话 bbox 会把整条字幕框进来，裁出来的封面就带着字幕和一条接缝。"""
    tgt = np.array([int(hexcol[i:i + 2], 16) for i in (1, 3, 5)], np.int16)
    m = (np.abs(rgb.astype(np.int16) - tgt).max(axis=2) < tol)
    m[int(rgb.shape[0] * y_max_frac):, :] = False
    ys, xs = np.where(m)
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def crop_169(rgb, cx, cy, want_h):
    """**只做裁切，不做粘贴**：从原帧取一个 16:9 窗口。
    上一版把裁出来的方块贴到新底色上，贴出来有一道明显的矩形接缝，
    因为裁片自带背景、和重铺的渐变对不上。纯裁切则背景天然连续。"""
    h, w = rgb.shape[:2]
    ch = int(round(want_h))
    cw = int(round(ch * 16 / 9))
    if cw > w:
        cw, ch = w, int(round(w * 9 / 16))
    x0 = int(min(max(cx - cw / 2, 0), w - cw))
    y0 = int(min(max(cy - ch / 2, 0), h - ch))
    out = rgb[y0:y0 + ch, x0:x0 + cw]
    return np.asarray(Image.fromarray(out).resize((w, h), Image.LANCZOS))


def canvas_like(base, w, h):
    """取参考帧的底色与地面色，铺一张同色系的空画布。"""
    sky = base[60, 60].astype(np.float32)
    gnd = base[-40, 60].astype(np.float32)
    arr = np.empty((h, w, 3), np.float32)
    yy = np.linspace(0, 1, h)[:, None, None]        # 必须是 (h,1,1)，否则乘不出 (h,w,3)
    arr[:] = (1 - yy) * sky + yy * gnd
    return arr.astype(np.uint8)


def paste_center(dst, src, cx, cy):
    x = int(cx - src.shape[1] / 2)
    y = int(cy - src.shape[0] / 2)
    h, w = src.shape[:2]
    dy0, dx0 = max(0, -y), max(0, -x)
    y0, x0 = max(0, y), max(0, x)
    dst[y0:y0 + h - dy0, x0:x0 + w - dx0] = src[dy0:dy0 + (dst[y0:y0 + h - dy0, x0:x0 + w - dx0].shape[0]),
                                                dx0:dx0 + (dst[y0:y0 + h - dy0, x0:x0 + w - dx0].shape[1])]
    return dst


def main():
    made = []
    for t, tag in [(5.0, "t5"), (7.0, "t7")]:
        f = frame(t)
        x0, y0, x1, y1 = find_subject(f, "#FDEDD2")
        subj_h = y1 - y0
        print(f"{t:4.1f}s  角色 {x1-x0}×{subj_h}，占成片高度 {subj_h/H:.0%}")
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        # 目标：主体占封面高度 68%。裁窗口高 want_h 放大到 H 后，主体变成
        # subj_h * H / want_h 像素，要它 = 0.68*H  →  want_h = subj_h / 0.68
        TARGET_SHARE = 0.68
        cv = crop_169(f, cx, cy, subj_h / TARGET_SHARE)
        cv = Crayon(H, W).apply(cv, int(t * RV.FPS), spot=0.55)
        outp = os.path.join(OUT, f"cover_{tag}.png")
        Image.fromarray(cv).save(outp)
        made.append(outp)

    # 悬念版：空壳单独成图
    g = frame(2.0)
    sx0, sy0, sx1, sy1 = find_subject(g, "#CD6B43", tol=70, y_max_frac=0.95)
    # 壳只占 46% 高，上方大片暗部就是留给文案的位置（不写文案也要把位子留出来）
    cvB = crop_169(g, (sx0 + sx1) / 2, (sy0 + sy1) / 2, (sy1 - sy0) / 0.46)
    cvB = Crayon(H, W).apply(cvB, 60, spot=0.5)
    Image.fromarray(cvB).save(os.path.join(OUT, "cover_B_suspense.png"))
    made.append(os.path.join(OUT, "cover_B_suspense.png"))

    # 安全区示意（只画框，不替你写文案）
    ann = Image.fromarray(np.asarray(Image.open(made[0]))).convert("RGB")
    d = ImageDraw.Draw(ann)
    d.rectangle([0, int(H * 0.0625), W, int(H * 0.9375)], outline=(90, 220, 130), width=6)
    d.rectangle([int(W * 0.05), int(H * 0.0625), int(W * 0.95), int(H * 0.9375)],
                outline=(240, 200, 70), width=5)
    d.rectangle([int(W * 0.56), int(H * 0.10), int(W * 0.94), int(H * 0.28)],
                outline=(230, 90, 90), width=6)
    ann.save(os.path.join(OUT, "cover_A_safearea.png"))

    # 小尺度实测：手机信息流大约 320px 宽
    strip = Image.new("RGB", (320 * 3 + 40, 180 * 2 + 50), (24, 20, 15))
    for i, p in enumerate(made):
        im = Image.open(p).resize((320, 180), Image.LANCZOS)
        strip.paste(im, (10 + (i % 3) * 330, 10 + (i // 3) * 195))
    strip.save(os.path.join(OUT, "cover_small_strip.png"))
    names = [os.path.basename(p) for p in made] + ["cover_A_safearea.png", "cover_small_strip.png"]
    print("已出：" + "  ".join(names))


if __name__ == "__main__":
    main()
