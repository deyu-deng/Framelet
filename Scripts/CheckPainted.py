#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 9 道门禁：段表说该出现的东西，画面上真的有吗。

数值全对、画面没东西，是这个项目反复出现的一类 bug，而且前面八道门禁一道都拦不住：

  · 篮球第一版：球路算得完完整整，一帧没画 —— 段表里没有它的记号，
    道具层按记号遍历，遍历不到它。
  · 篮球第二版：球滚到左边时被电视那块不透明屏幕整个盖住约 0.5 秒 ——
    道具都在画，只是绘制顺序把它埋了。

两次都是把帧抽出来用肉眼看才发现的。所以这里把"看"变成量：
同一帧渲两遍，一遍把这件东西的图元清空，逐像素相减，
差集就是这件东西**真正负责的那部分画面**。差集为 0 = 它根本没出现。

用法：
    python Scripts/CheckPainted.py                 # 全段表记号 + 篮球全程
    python Scripts/CheckPainted.py --budget 8      # 只抽查，快一点
"""
import argparse
import io
import json
import math
import os
import re
import sys

import numpy as np

BASE = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
os.chdir(BASE)
sys.path.insert(0, os.path.join(BASE, "Scripts"))

import RenderVideo as R          # noqa: E402

MIN_PX = 4000                   # 少于这个像素数就算"没画出来"
AFTER = 1.20                    # 记号之后多久看：淡入 0.45 s 早走完，画面稳定了
BALL_STEP = 0.10                # 篮球对拍的采样步长（秒）。整段 5.6 s，
                                # 逐帧对要 300 多次渲染；0.1 秒足够抓住"整段没画"
                                # 和"画到别处去"这两类，而那才是它会犯的错。


def _load_script():
    txt = io.open(os.path.join("Episodes", "EP001_SelfIntro", "Script.md"), encoding="utf-8").read()
    return json.loads(re.search(r"```json\n(.*?)\n```", txt, re.S).group(1))


def _painted(rgb_a, rgb_b):
    """两张同帧图（有这件 / 没这件）差多少像素。"""
    d = np.abs(rgb_a.astype(np.int16) - rgb_b.astype(np.int16)).sum(axis=2)
    return int((d > 40).sum())


def _render(t):
    return np.asarray(R.render_frame_rgb(t, int(round(t * R.FPS))), dtype=np.uint8)


def check_props(data, budget):
    """每个 show 记号：把这件道具的图元清空再渲一遍，看画面少了多少。"""
    fails = []
    seen = []
    for b in data["beats"]:
        for c in b.get("cues", []):
            pid = c["id"]
            # break 记号不检：碎掉之后完整那件本来就不该再画，
            # 碎片是 _shards 另画的，不在这件资产的图元里，差集必然是 0。
            if c["kind"] == "break" or pid not in R.PROP_SVGS:
                continue
            t = b["start"] + c["at"] * (b["end"] - b["start"]) + AFTER
            if t >= data["duration"] - 0.2:
                continue
            seen.append((t, pid))
    if budget:
        seen = seen[::max(1, len(seen) // budget)]
    print("\n  道具是否真的画在画面：%d 处" % len(seen))
    for t, pid in seen:
        with_art = _render(t)
        # 不能从 PROP_SVGS 里摘掉：道具层是按表里的 id 取内容的，摘了直接
        # KeyError。换成"内容是空串"——分组照样输出、一个像素都不画。
        saved = R.PROP_SVGS[pid]
        R.PROP_SVGS[pid] = ""
        try:
            without = _render(t)
        finally:
            R.PROP_SVGS[pid] = saved
        n = _painted(with_art, without)
        flag = "ok" if n >= MIN_PX else "没画出来"
        print("    %-6.2f s  %-14s %8d px  %s" % (t, pid, n, flag))
        if n < MIN_PX:
            fails.append("%s 在 %.2f s 只贡献 %d 像素（阈值 %d）" % (pid, t, n, MIN_PX))
    return fails


def check_ball(data):
    """篮球：物理算出来的位置，和画面上真的出现的位置，对不对得上。

    球不是段表记号驱动的（它是"扣篮"这个动作自带的道具），所以上一条检不到它。
    """
    fails = []
    dbeats = [b for b in data["beats"] if b["action"] == "dunk"]
    if not dbeats:
        return fails
    b = dbeats[0]
    tc, a0, a1, hang, land, rec = R._dunk_times(b)
    edge = (R.ACTIVE_PRESET["width"] - 1600) / 2.0

    n = 0
    t = a0 - R.PASS_LEAD
    while t < tc + R.BALL_LIFE:
        want = R._ball_state(t, R._dunk_beat(t))
        n += 1
        if want is None:
            t += BALL_STEP
            continue
        # 过筐之后往下掉那一段是**故意**画在他身后的（筐挂在墙上，人站在筐前），
        # 整颗球被身体挡住是对的，不算失效；这一段的位置由 _ball_state 保证，
        # 画面差分测不到，就别假装能测。
        if R._ball_behind(t, R._dunk_beat(t)):
            t += BALL_STEP
            continue
        with_ball = _render(t)
        saved = R.PROP_SVGS.pop("propBall", None)
        try:
            without = _render(t)
        finally:
            if saved is not None:
                R.PROP_SVGS["propBall"] = saved
        d = np.abs(with_ball.astype(np.int16) - without.astype(np.int16)).sum(axis=2)
        ys, xs = np.where(d > 40)
        offstage = want[0] < edge + R.BALL_R or want[0] > R.ACTIVE_PRESET["width"] - edge - R.BALL_R
        if offstage:
            t += BALL_STEP
            continue
        if len(xs) == 0:
            fails.append("球在 %.2f s 该在 (%.0f,%.0f)，画面上一像素没变" % (t, want[0], want[1]))
        else:
            got = (0.5 * (xs.min() + xs.max()), 0.5 * (ys.min() + ys.max()))
            err = math.hypot(got[0] - want[0], got[1] - want[1])
            if err > 12:
                fails.append("球在 %.2f s 算在 (%.0f,%.0f) 画在 (%.0f,%.0f)，偏 %.0f px"
                             % (t, want[0], want[1], got[0], got[1], err))
        t += BALL_STEP
    print("  篮球：物理位置与画面位置逐帧对拍 %d 帧" % n)
    return fails


def check_grasp(data):
    """他"拿着"的东西，整段都必须贴着手。

    这条不渲染，纯算：球心到手掌锚点的距离。为什么要有 —— 篮球中段曾经
    走的是另写的一条弧线，和手臂各算各的，量出来掌心在筐圈下方 189 像素，
    也就是他根本没够到筐，球是被一条公式抬上去的。画面看着"差不多"，
    但那是两个东西在各自动。
    """
    fails = []
    held = {"propBall": "armR"}
    for b in data["beats"]:
        for pid, side in held.items():
            if b["action"] != "dunk":
                continue
            tc, a0 = R._dunk_times(b)[:2]
            worst = (0.0, None)
            t = a0
            while t < tc:
                want = R._ball_state(t, b)
                if want:
                    px, py = R.hand_canvas(t, side)
                    d = math.hypot(want[0] - px, want[1] - py)
                    if d > worst[0]:
                        worst = (d, t)
                t += 1.0 / data["fps"]
            # 40 px 是握持偏移 BALL_GRIP 的量级；再大就是"两件东西各动各的"
            if worst[1] is not None and worst[0] > 60.0:
                fails.append("%s 在 %.2f s 离%s %.0f px —— 没有挂在这只手上"
                             % (pid, worst[1], side, worst[0]))
            else:
                print("  握持：%s 整段离%s掌心最远 %.0f px（阈值 60）"
                      % (pid, side, worst[0]))
    return fails


def main():
    ap = argparse.ArgumentParser(description="检查段表里该出现的东西是否真的画在画面上")
    ap.add_argument("--budget", type=int, default=14,
                    help="道具抽查几处（默认 14；0 = 全检，慢）")
    ap.add_argument("--no-ball", action="store_true", help="跳过篮球逐帧对拍")
    args = ap.parse_args()

    data = _load_script()
    fails = check_grasp(data)
    fails += check_props(data, args.budget)
    if not args.no_ball:
        fails += check_ball(data)

    print()
    if fails:
        for f in fails:
            print("  [FAIL] %s" % f)
        print("\n未通过 %d 项：算到了不等于画到了" % len(fails))
        return 1
    print("全部通过：段表里该出现的东西都在画面上")
    return 0


if __name__ == "__main__":
    sys.exit(main())
