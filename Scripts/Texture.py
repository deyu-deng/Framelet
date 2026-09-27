#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""蜡笔/手绘质感层：把干净矢量帧变成"画在纸上"的帧。

三件事，都只做在**最终像素**上（不碰 SVG —— PyMuPDF 不认 filter/clipPath，§26）：

  1. boil（线条抖动）  位移场按每 3 帧换一张、空间上低频、幅度 ±2 px。
     手绘赛璐璐的"boiling line"就是这个：同一格人物轮廓每 2~3 帧重画一次，
     线会轻微跳动。连续平滑的抖动反而像镜头抖动，不像手画。
  2. grain（纸纹）     一张固定的乘性噪点。固定不跟着抖是对的 ——
     纸是压在摄影机底下的那张，画在它上面的线才抖。
  3. spotlight（锥光） 开场那束打在龟壳上的光。径向渐变在 SVG 里不生效，
     所以只能在像素上做。

来源：Ref/StyleLab/crayon.py 是我在风格调研阶段写的实验代码，Ref/ 不进版本库，
渲染管线不能依赖它 —— 这里把用到的部分搬成正经模块（同一段算法，不是抄第三方）。

用法：
    from Texture import Crayon
    tex = Crayon(1080, 1920)
    rgb = tex.apply(rgb, frame_idx, spot=0.6)      # uint8 HxWx3 → 同形状
"""
import math

import numpy as np
from PIL import Image


def _noise_field(h, w, seed, cells, lo=-1.0, hi=1.0):
    """低频平滑噪声：先在 cells 网格上撒随机值，再双三次放大到 h x w。

    放大而不是逐像素噪声：线条抖动要的是"整段弧一起歪一点"，
    逐像素噪声会变成砂纸。
    """
    rng = np.random.default_rng(seed)
    cw, ch = max(2, cells), max(2, int(round(cells * h / max(1, w))))
    small = rng.uniform(lo, hi, size=(ch, cw)).astype(np.float32)
    im = Image.fromarray(small, "F").resize((w, h), Image.BICUBIC)
    return np.asarray(im, dtype=np.float32)


class Crayon:
    """一帧一帧贴质感。帧号决定用哪一张抖动场（q = frame // every）。"""

    def __init__(self, h, w, amp=2.0, every=3, grain_strength=0.038, seed=20260921):
        self.h, self.w = h, w
        self.amp = float(amp)
        self.every = int(every)
        self.seed = int(seed)
        self._boil = {}                       # q -> (yi, xi) 整数索引
        # 纸纹：一张固定图，全片复用。逐帧重算会白白多花几十毫秒。
        g = _noise_field(h, w, seed + 7, 640, -1.0, 1.0)
        self.grain = (1.0 + grain_strength * g).astype(np.float32)

    def _idx(self, q):
        got = self._boil.get(q)
        if got is None:
            if len(self._boil) > 6:           # 只缓存最近几张，别把内存吃掉
                self._boil.pop(next(iter(self._boil)))
            yy, xx = np.mgrid[0:self.h, 0:self.w].astype(np.float32)
            dx = _noise_field(self.h, self.w, self.seed + q * 31 + 3, 46) * self.amp
            dy = _noise_field(self.h, self.w, self.seed + q * 31 + 17, 46) * self.amp
            got = (np.clip(np.rint(yy + dy), 0, self.h - 1).astype(np.int32),
                   np.clip(np.rint(xx + dx), 0, self.w - 1).astype(np.int32))
            self._boil[q] = got
        return got

    def apply(self, rgb, frame_idx, spot=0.0, tint=(1.0, 1.0, 1.0)):
        """rgb: uint8 (h,w,3) → 同形状。spot: 聚光灯强度；tint: 按拍的色温三通道。

        色温放在像素层而不是 SVG：这个光栅化器不认渐变与滤镜（§26），
        在矢量里做"整幅压一层冷暖"要么不生效，要么得叠一堆半透明矩形。
        """
        yi, xi = self._idx(frame_idx // self.every)
        out = rgb[yi, xi].astype(np.float32)
        out *= self.grain[:, :, None]
        if spot > 0.001:
            out = out * (1.0 + spot * self._cone())[:, :, None]
        out *= np.asarray(tint, np.float32)[None, None, :]
        return np.clip(out, 0, 255).astype(np.uint8)

    _cone_cache = None

    def _cone(self):
        """柔和椭圆高光 + 四周压暗，模拟一束打在地面上的光。

        位置固定在画面中线：开场那枚壳就丢在那儿（char_x 960 / 1920）。
        要改位置得连 rig 的站位一起改，所以这里不做成参数 ——
        参数化的东西必须有一个真的会传别的值进来，否则就是骗人的开关。
        """
        if self._cone_cache is None:
            yy, xx = np.mgrid[0:self.h, 0:self.w].astype(np.float32)
            d = np.sqrt(((xx - 0.5 * self.w) / (0.30 * self.w)) ** 2 +
                        ((yy - 0.42 * self.h) / (0.34 * self.h)) ** 2)
            lit = np.clip(1.0 - d, 0, 1) ** 1.6
            dark = np.clip(d - 1.0, 0, 1) * 0.55
            self._cone_cache = (lit * 0.34 - dark * 0.30).astype(np.float32)
        return self._cone_cache


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    tex = Crayon(240, 427)
    base = np.zeros((240, 427, 3), np.uint8)
    base[:, :, :] = (30, 40, 60)
    base[60:180, 150:280] = (240, 200, 170)
    a = tex.apply(base, 0)
    b = tex.apply(base, 90)                    # 换了 1 张抖动场
    print("同一帧号两次结果相同：%s" % np.array_equal(tex.apply(base, 0), a))
    print("隔 3 帧后轮廓确实移动：%d px 变了" % int((a != b).any(axis=2).sum()))
    print("不改变像素总数：%s" % (a.shape == base.shape and a.dtype == base.dtype))
