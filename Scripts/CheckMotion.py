#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""动作质量门禁：不渲染、不开浏览器，直接对 compute_vibe_motion 的逐帧输出做统计。

用途：改完动画参数后 `python Scripts/CheckMotion.py`，任何一项 FAIL 即以退出码 1 结束。
存在的理由：机械感（齐步走、常驻抖动、边界幻影）用肉眼看抽帧很容易漏，
而均匀等间距抽帧会把交接缺陷完全藏起来（Docs/VibeMotionPlan.md §9 第 11 条）。

五项检查对应文档条目：
  1 死通道      —— 渲染端消费的通道却全程零振幅        （§20.1）
  2 单帧硬跳变  —— 一帧内变化超过自身振幅 25%          （§8 第 3 条 / §20.2）
  3 齐步走      —— 两通道的起止帧完全相同              （§9 第 8 条）
  4 边界幻影    —— 段边界处姿态跌破/越过两侧端点       （§20.2）
  5 clamp 守卫  —— 缩放为负、越出安全区间              （§8 第 3 条）
"""
import importlib.util
import math
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
# 默认检查同目录渲染器；也可传路径去检查另一份实现（例如改造前的版本）
RV_PATH = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(HERE, "RenderVideo.py")
FPS = 30.0

# 由 build_plobi_character 实际消费的通道；shellX 明确设计为常量（见 §20.1）
CONSUMED = ["rootX", "rootY", "rootRot", "shellX", "shellY", "shellRot",
            "bodyScaleX", "bodyScaleY", "bodyRot",
            "legLeftSquash", "legRightSquash", "legLeftRot", "legRightRot",
            "headRot", "headY", "armLeftRot", "armRightRot", "eyeSquash", "mouthAperture"]
WHITELIST_CONSTANT = {"shellX"}

fails = []


def load():
    spec = importlib.util.spec_from_file_location("rv", RV_PATH)
    rv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rv)
    return rv


def series(rv):
    n = rv.TOTAL_FRAMES
    frames = [rv.compute_vibe_motion(i / FPS) for i in range(n)]
    return {k: [f[k] for f in frames] for k in CONSUMED}


def report(name, ok, detail):
    print("[%s] %-12s %s" % ("PASS" if ok else "FAIL", name, detail))
    if not ok:
        fails.append(name)


# ---------------------------------------------------------------- 1 死通道
def check_dead(ch):
    dead = [k for k, v in ch.items()
            if (max(v) - min(v)) < 1e-9 and k not in WHITELIST_CONSTANT]
    report("死通道", not dead,
           "全部通道有振幅" if not dead else "零振幅: " + ", ".join(dead))


# ------------------------------------------------------------ 2 单帧硬跳变
# 眨眼不参与"单帧不超 25% 振幅"：那条规则是给**姿态通道**定的，
# 防的是无来由的硬跳。而生理性眨眼在 30 fps 下必须 2~4 帧合完，
# 用同一条尺子量，等于逼我把眨眼做成 0.4 秒的慢动作 —— 门禁反过来把片子改坏。
# 换成更贴身的形状判据：一次眨发必须落在 3~12 帧之间，且半闭状态只允许连续几帧。
BLINK_FRAMES = (3, 12)
SLOPE_EXEMPT = {"eyeSquash"}


def check_blink(ch):
    v = ch.get("eyeSquash")
    if not v:
        return
    closed = [i for i, x in enumerate(v) if x < 0.5]
    if not closed:
        report("眨眼", False, "全程没有一次闭眼")
        return
    runs, cur = [], [closed[0]]
    for a, b in zip(closed, closed[1:]):
        if b - a == 1:
            cur.append(b)
        else:
            runs.append(len(cur)); cur = [b]
    runs.append(len(cur))
    bad = [n for n in runs if not (BLINK_FRAMES[0] <= n <= BLINK_FRAMES[1])]
    report("眨眼", not bad,
           "%d 次眨发，闭眼时长 %d~%d 帧（生理区间）" % (len(runs), min(runs), max(runs))
           if not bad else "%d 次眨发时长越界（%s 帧，应在 %d~%d）"
           % (len(bad), sorted(set(bad))[:4], *BLINK_FRAMES))


def check_step(ch):
    bad = []
    for k, v in ch.items():
        if k in SLOPE_EXEMPT:
            continue
        amp = max(v) - min(v)
        if amp < 1e-9:
            continue
        worst_i = max(range(1, len(v)), key=lambda i: abs(v[i] - v[i - 1]))
        ratio = abs(v[worst_i] - v[worst_i - 1]) / amp
        if ratio > 0.25:
            bad.append("%s %.0f%%@%.2fs" % (k, 100 * ratio, worst_i / FPS))
    report("单帧硬跳变", not bad,
           "无通道单帧变化 >25% 振幅" if not bad else "; ".join(bad))


# ---------------------------------------------------------------- 3 齐步走
# 判据：两通道的"速度轮廓"是否同起同停同形状。
# 旧实现只比 (起始帧, 结束帧) 是否完全相等 —— 那是个几乎不可能命中的条件，
# 在改造前后都 PASS，等于没有检查（§22 已自记一笔）。现在改成三条件同时成立：
#   同起（±1 帧）、同停（±1 帧）、归一化速度轮廓相关系数 > 0.95。
LOCKSTEP_CORR = 0.95
LOCKSTEP_TOL_FRAMES = 1


def _activity_span(v):
    """首次/末次明显偏离最小值的帧号（±5% 振幅）。"""
    lo, hi = min(v), max(v)
    span = hi - lo
    if span < 1e-9:
        return None
    thr = 0.05 * span
    on = next((i for i, x in enumerate(v) if abs(x - lo) > thr), None)
    off = next((i for i in range(len(v) - 1, -1, -1) if abs(v[i] - lo) > thr), None)
    if on is None or off is None or off <= on:
        return None
    return (on, off)


def _velocity_profile(v):
    """带符号的速度轮廓（归一化到峰值 1）。

    必须带符号：squash & stretch 里 bodyScaleX 与 bodyScaleY 按体积守恒反向耦合，
    用绝对值轮廓会把它误判成齐步走。齐步走的定义是"同时、同向、同形状"，
    反向同步是正确行为，不是缺陷。
    """
    d = [v[i] - v[i - 1] for i in range(1, len(v))]
    peak = max(abs(x) for x in d) if d else 0.0
    return None if peak < 1e-12 else [x / peak for x in d]


def _corr(a, b):
    n = min(len(a), len(b))
    if n < 8:
        return 0.0
    a, b = a[:n], b[:n]
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    den = (sum((x - ma) ** 2 for x in a) * sum((x - mb) ** 2 for x in b)) ** 0.5
    return 0.0 if den < 1e-12 else num / den


def lockstep_pairs(ch):
    """返回疑似齐步走的通道对（按相关系数降序）。可被 --selftest 复用。"""
    live = {k: v for k, v in ch.items() if (max(v) - min(v)) > 1e-9}
    spans = {k: _activity_span(v) for k, v in live.items()}
    profs = {k: _velocity_profile(v) for k, v in live.items()}
    keys = [k for k in live if spans[k] and profs[k]]
    out = []
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = keys[i], keys[j]
            (a0, a1), (b0, b1) = spans[a], spans[b]
            if abs(a0 - b0) > LOCKSTEP_TOL_FRAMES or abs(a1 - b1) > LOCKSTEP_TOL_FRAMES:
                continue
            r = _corr(profs[a], profs[b])
            if r > LOCKSTEP_CORR:
                out.append((r, a, b, (a0, a1), (b0, b1)))
    out.sort(reverse=True)
    return out


def check_lockstep(ch):
    pairs = lockstep_pairs(ch)
    report("齐步走", not pairs,
           ("无两通道同起同停且速度轮廓相关 >%.2f" % LOCKSTEP_CORR) if not pairs
           else "; ".join("%s/%s r=%.3f 活动帧%s vs %s"
                          % (a, b, r, sa, sb) for r, a, b, sa, sb in pairs[:5]))


# ------------------------------------------------- 3b 部件时长分布（只报不判）
def report_part_timing(ch, fps):
    """测每个通道单次动作事件的持续时长分布。

    画风要求"形变幅度大但节奏慢"（Ref/vibe-motion pixel2motion §Step2：低能量档
    Part timing 400–800ms + extended soft settle）。这里只把实测值摆出来，
    等参数档位落到代码里再升级为门禁 —— 没有目标值就判 PASS/FAIL 是假检查。
    """
    import statistics
    print("\n  部件时长实测（按停下来切事件：两次静止之间算一个动作）：")
    rows = []
    for k, v in ch.items():
        span = _activity_span(v)
        if not span:
            continue
        d = [abs(v[i] - v[i - 1]) for i in range(1, len(v))]
        peak = max(d) if d else 0.0
        if peak < 1e-12:
            continue
        thr = 0.02 * peak                 # 低于峰值速度 2% 视为"这一动结束了"
        events, start = [], None
        for i, m in enumerate(d):
            if m > thr and start is None:
                start = i
            elif m <= thr and start is not None:
                if i - start >= 3:
                    events.append((i - start) / fps * 1000.0)
                start = None
        if start is not None and len(d) - start >= 3:
            events.append((len(d) - start) / fps * 1000.0)
        if events:
            rows.append((k, len(events), statistics.median(events), min(events), max(events)))
    rows.sort(key=lambda r: -r[2])
    in_band = sum(1 for r in rows if r[2] >= 400)
    for k, n, med, lo_, hi_ in rows[:8]:
        print("    %-16s 动作 %2d 次  中位 %6.0f ms  最短 %5.0f  最长 %6.0f"
              % (k, n, med, lo_, hi_))
    print("    —— 中位时长 >=400ms（画风档位：形变幅度大但节奏慢）的通道 %d/%d"
          % (in_band, len(rows)))


# -------------------------------------------------------------- 4 边界幻影
# 参与本检查的是姿态通道。mouthAperture 排除：它由字幕窗口驱动、刻意不参与
# 段边界交叉淡化，其 1.4 Hz 说话起伏落在边界窗口内会被误判成越界。
BOUNDARY_POSE = [k for k in CONSUMED if k != "mouthAperture"]
# 绝对下限（按单位给）：低于此量的越界在 1080p / 30fps 下不可见。
# 校准依据：基线缺陷 armRightRot 越界 36.6，噪声底 rootRot 越界 0.8。
ABS_FLOOR_DEG = 1.0      # 度 / 像素类通道
ABS_FLOOR_SCALE = 0.02   # 缩放类通道
SCALE_KEYS = {"bodyScaleX", "bodyScaleY", "legLeftSquash", "legRightSquash"}


def check_boundary(rv, ch):
    tw = getattr(rv, "SEAM_WIN", 0.25)   # 接缝邻域由渲染器声明，见 RenderVideo.py
    worst = []
    for b in list(rv.T_BOUNDS)[1:-1]:
        i0, i1 = int((b - tw) * FPS), int((b + tw) * FPS)
        for k in BOUNDARY_POSE:
            v = ch[k]
            amp = max(v) - min(v)
            if amp < 1e-9:
                continue
            left, right = v[i0], v[min(i1, len(v) - 1)]
            lo, hi = min(left, right), max(left, right)
            win = v[i0:i1 + 1]
            # 单调过渡要求窗口内全程落在两端点张成的区间里；
            # 跌破 lo 或越过 hi 的量就是"幻影幅度"（旧实现把全身拉向 REST 时此处可达数十度）
            exc = max(0.0, lo - min(win)) + max(0.0, max(win) - hi)
            if exc > max(0.05 * amp, ABS_FLOOR_SCALE if k in SCALE_KEYS else ABS_FLOOR_DEG):
                worst.append("%s@t=%.0f 越界 %.1f" % (k, b, exc))
    worst.sort(key=lambda s: -float(s.split("越界")[1]))
    report("边界幻影", not worst,
           "段边界均为单调过渡" if not worst else "; ".join(worst[:6]))


# ------------------------------------------------------------- 5 clamp 守卫
def check_clamp(rv, ch):
    bad = []
    for k in ("bodyScaleX", "bodyScaleY", "legLeftSquash", "legRightSquash"):
        v = ch[k]
        if min(v) <= 0.4:
            bad.append("%s 最小 %.3f（缩放接近翻面）" % (k, min(v)))
    lim = getattr(rv, "M_SQUASH", 0.22) + 0.10      # 接触帧允许略超人格档位，但不许失控
    for k in ("bodyScaleX", "bodyScaleY"):
        dev = max(abs(x - 1.0) for x in ch[k])
        if dev > lim:
            bad.append("%s 峰值偏离 1.0 达 %.3f（> %.2f 上限）" % (k, dev, lim))
    for k, v in ch.items():
        for x in v:
            if x != x or x in (float("inf"), float("-inf")):
                bad.append("%s 出现 NaN/Inf" % k)
                break
    report("clamp守卫", not bad, "数值全部在安全区" if not bad else "; ".join(bad))


def selftest_lockstep():
    """证明齐步走检测有鉴别力：造一组故意同模板的通道，必须报；再造一组错峰的，必须不报。

    没有反例的检查看不出真缺陷，只会给人一种虚假的安全感 —— 本项目的旧版
    齐步走检查就是这样在改造前后都 PASS 的。
    """
    n = 900
    same = [max(0.0, math.sin(math.pi * i / n)) for i in range(n)]
    locked = {"a": list(same), "b": [x * 2.0 for x in same], "c": [x * 0.5 + 10 for x in same]}
    hits = lockstep_pairs(locked)
    print("  反例（三通道同模板）→ 检出 %d 对  %s" % (len(hits), "PASS" if len(hits) >= 3 else "FAIL"))
    staggered = {}
    for k, off in (("a", 0), ("b", 60), ("c", 120)):
        staggered[k] = [max(0.0, math.sin(math.pi * max(0, min(n, i - off)) / (n - off)))
                        if i > off else 0.0 for i in range(n)]
    hits2 = lockstep_pairs(staggered)
    print("  正例（错峰 60/120 帧）→ 检出 %d 对  %s" % (len(hits2), "PASS" if not hits2 else "FAIL"))
    return len(hits) >= 3 and not hits2


# ------------------------------------------------- 6 参数层诚实性（token 有没有人读）
TOKEN_PREFIXES = ("M_", "LAG_")


def defined_tokens(src):
    """模块级赋值语句里的 motion token 名，含 `A, B, C = ...` 元组赋值。

    第一版写成 `^(M_|LAG_[A-Z_]+)\s*=`，两个错：交替优先级让 `M_SQUASH =` 匹配不上，
    元组赋值行（LAG_SHELL, LAG_HEAD, ... = ...）整行漏掉 —— 于是这条检查对
    已知的 6 个死 token 直接报 PASS。判据必须以真实反例验收，不能只看它不报错。
    """
    out = set()
    for line in src.splitlines():
        m = re.match(r"^((?:[A-Za-z_][A-Za-z0-9_]*\s*,\s*)*[A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)", line)
        if not m:
            continue
        for name in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", m.group(1)):
            if name.startswith(TOKEN_PREFIXES):
                out.add(name)
    return out


def check_token_honesty(path):
    """定义了却没人读的 motion token = 假出处。

    来历：§9.2 那张 token 表被文档写成"运动参数的唯一出处"，但实测 8 个里 6 个
    只有定义行、零引用（M_ANTICIP / M_OVERSHOOT / M_HEST / LAG_SHELL / LAG_HEAD /
    LAG_ARM），而代码里的滞后用的是同值字面量。文档说的出处并不是真正的出处。
    这类"参数看着在管、其实什么都没管"和空转的 clip-path 是同一种病。
    """
    src = open(path, encoding="utf-8").read()
    defined = defined_tokens(src)
    unused = [n for n in sorted(defined)
              if len(re.findall(r"\b%s\b" % n, src)) <= 1]
    # 滞后类字面量与具名 token 同值并存 = 两处真相。
    # 值必须按位置从赋值右侧取：`LAG_SHELL, LAG_HEAD, LAG_ARM, LAG_LEG = 0.08, 0.14, ...`
    # 是元组解包，取整行第一个数字会把四个名字都记成 0.08，报出假结论。
    lag_vals = {}
    for line in src.splitlines():
        m = re.match(r"^((?:[A-Za-z_][A-Za-z0-9_]*\s*,\s*)*[A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)(.+)$", line)
        if not m:
            continue
        names = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", m.group(1))
        vals = re.findall(r"[0-9.]+", m.group(2))
        if len(vals) == 1 and len(names) > 1:
            vals = vals * len(names)
        for n, v in zip(names, vals):
            if n.startswith("LAG_"):
                lag_vals.setdefault(v, []).append(n)
    dup = ["lag=%s 与 %s 同值" % (lit, ", ".join(v))
           for lit, v in sorted(lag_vals.items())
           if re.search(r"lag=%s\b" % re.escape(lit), src)]
    msg = []
    if unused:
        msg.append("零引用：%s" % ", ".join(unused))
    if dup:
        msg.append("字面量与具名 token 同值并存（两处真相）：%s" % "; ".join(dup[:4]))
    report("参数诚实", not msg,
           "每个 motion token 都真的被读到，且无同值字面量" if not msg else "；".join(msg))


def main():
    if "--selftest" in sys.argv:
        print("=== 检查器自检 ===")
        return 0 if selftest_lockstep() else 1
    rv = load()
    ch = series(rv)
    print("=== 动作质量门禁  %s ===" % os.path.basename(RV_PATH))
    print("时长 %s s / %s 帧 / 边界 %s\n"
          % (rv.TOTAL_DURATION, rv.TOTAL_FRAMES, list(rv.T_BOUNDS)))
    check_dead(ch)
    check_step(ch)
    check_blink(ch)
    check_lockstep(ch)
    report_part_timing(ch, FPS)
    print()
    check_boundary(rv, ch)
    check_clamp(rv, ch)
    check_token_honesty(RV_PATH)
    print("\n%s" % ("全部通过" if not fails else "未通过: " + ", ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
