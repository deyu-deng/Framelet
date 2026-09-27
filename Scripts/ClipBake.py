#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 SVG 的 clip-path 语义编译成后端真正支持的几何。

为什么需要：Docs/VibeMotionPlan.md §26 实测 PyMuPDF **完全忽略 clip-path**。
资产里的两色调阴影是按"会被裁进轮廓"写的（一大块切线 + 剪裁），直接加载就会有一块
棕色矩形糊在角色外面 —— 这是解耦时现场暴露出来的，不是新引入的 bug。

设计取舍：**不在资产文件里烘死**，而在装配时编译。
资产继续保留 `clipPath` + 贝塞尔的原始语义（人可编辑；将来换成支持剪裁的后端就自动正确），
本模块只负责给当前这个后端算出等价几何。

算法：Sutherland–Hodgman，要求裁剪多边形为凸。头/躯干实测严格凸；
龟壳轮廓只有 1/413 个顶点反号（0.24%），按凸处理，误差在亚像素级，由 SnapshotFrames 目视复核。
"""
import math
import re

_STEP = 1.5          # 采样步长（资产单位）。角色最终约 300 px 高 / 600 单位 → 1 单位 ≈ 0.5 px
_DECIMALS = 1


def flatten_path(d):
    """把只含 M/C/L/Z 的路径采样成闭合折线。（本项目资产只用这四种指令。）"""
    pts = []
    cur = start = None
    for cmd, nums in re.findall(r"([MCLZmc lz])([^MLCZmlcz]*)", d):
        v = [float(x) for x in re.findall(r"-?\d*\.?\d+(?:e[-+]?\d+)?", nums)]
        c = cmd.upper()
        if c == "M":
            cur = start = (v[0], v[1])
            pts.append(cur)
        elif c == "L":
            for i in range(0, len(v), 2):
                cur = (v[i], v[i + 1])
                pts.append(cur)
        elif c == "C":
            for i in range(0, len(v), 6):
                p0 = cur
                p1, p2, p3 = (v[i], v[i + 1]), (v[i + 2], v[i + 3]), (v[i + 4], v[i + 5])
                n = max(6, int(math.dist(p0, p3) / _STEP))
                for k in range(1, n + 1):
                    t = k / n
                    mt = 1.0 - t
                    pts.append((mt**3 * p0[0] + 3 * mt * mt * t * p1[0] + 3 * mt * t * t * p2[0] + t**3 * p3[0],
                                mt**3 * p0[1] + 3 * mt * mt * t * p1[1] + 3 * mt * t * t * p2[1] + t**3 * p3[1]))
                cur = p3
        elif c == "Z" and start is not None:
            pts.append(start)
            cur = start
    return pts


def _seg_intersect(p1, p2, a, b):
    """线段 p1→p2 与无限直线 a→b 的交点。调用方保证两侧异侧。"""
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = a
    x4, y4 = b
    den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(den) < 1e-12:
        return p2
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / den
    return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))


def _inside(p, a, b):
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]) >= 0.0


def sutherland_hodgman(subject, clip):
    """凸多边形裁剪。返回 subject ∩ clip 的顶点序列。"""
    out = list(subject)
    n = len(clip)
    for i in range(n):
        if not out:
            return []
        a, b = clip[i], clip[(i + 1) % n]
        inp, prev = [], out[-1]
        for cur in out:
            ci, pi = _inside(cur, a, b), _inside(prev, a, b)
            if ci:
                if not pi:
                    inp.append(_seg_intersect(prev, cur, a, b))
                inp.append(cur)
            elif pi:
                inp.append(_seg_intersect(prev, cur, a, b))
            prev = cur
        out = inp
    return out


def polygon_to_path(pts):
    if not pts:
        return ""
    r = ["M %.1f,%.1f" % pts[0]]
    r += ["L %.1f,%.1f" % p for p in pts[1:]]
    r.append("Z")
    return " ".join(r)


def area(pts):
    n = len(pts)
    if n < 3:
        return 0.0
    return abs(sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1]
                   for i in range(n))) / 2.0


def _signed_area(pts):
    n = len(pts)
    return sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1]
               for i in range(n)) / 2.0


def bake(subject_d, clip_d):
    """返回裁剪后的路径 d。面积为 0 时返回空串（调用方据此丢弃该元素）。

    Sutherland–Hodgman 依赖裁剪多边形的绕向：绕向反了，半平面判定整体取反，
    结果等于"取补集"，面积会莫名塌成极小值（实测反了时 21,155 → 537）。
    SVG 的 y 轴朝下，绕向直觉又与数学惯例相反，所以不猜——**两个方向都算，取面积大者**，
    并用 min(subject, clip) 做上界自检。
    """
    subj = flatten_path(subject_d)
    clip = flatten_path(clip_d)
    if len(subj) < 3 or len(clip) < 3:
        return polygon_to_path(subj)
    cap = min(area(subj), area(clip)) * 1.001
    best = []
    for cand in (sutherland_hodgman(subj, clip), sutherland_hodgman(subj, list(reversed(clip)))):
        a = area(cand)
        if a > area(best) and a <= cap:
            best = cand
    if not best:                      # 两向都超上界 = 算法没解出来，退回不裁，宁可溢出也不画错形
        return polygon_to_path(subj)
    return polygon_to_path(best)
