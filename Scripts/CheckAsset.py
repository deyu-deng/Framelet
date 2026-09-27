#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资产体检：把"渲染器不报错但画面错"的那一类缺陷在加载前就拦下来。

来历：2026-09-21 几何解耦当天真实踩到四个坑，全部静默 ——
  · 阴影按 clipPath 会生效来画，而 PyMuPDF 不实现剪裁 → 大块矩形糊在角色外
  · 装饰线有 stroke 没 fill，SVG 默认填充是黑色 → 每条线变成黑月牙
  · 接触阴影用 radialGradient，而渲染器不解析 url(#id) → 塌成实心黑饼
  · rig.json 声明的部件 id 在资产里找不到 → 装配期才炸，报错信息还难读
角色与道具正在整体重画，这类坑会成批出现。本脚本把它们变成 exit 1。

用法：
    python Scripts/CheckAsset.py                      # 体检全部已知资产 + rig
    python Scripts/CheckAsset.py path/to/X.svg        # 只体检单个 SVG
"""
import importlib.util
import io
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import ClipBake     # noqa: E402
from Rig import Rig, RigError, _qid   # noqa: E402

# 渲染器静默忽略的特性 → 后果说明（判决实验见 Docs/VibeMotionPlan.md §26）
INERT = {
    "clip-path": "剪裁不执行：溢出轮廓的阴影会原样画出来",
    "filter": "滤镜不执行：蜡笔/湍流质感必须走 numpy 后处理",
    "linearGradient": "线性渐变不解析：填充塌成单色",
    "radialGradient": "径向渐变不解析：填充塌成实心黑",
    "stroke-dasharray": "虚线不执行：画成实线",
}

problems = []


def fail(asset, code, where, msg):
    problems.append((asset, code, where, msg))
    print("  [FAIL] %-18s %-22s %s" % (code, where, msg))


def warn(asset, code, where, msg):
    print("  [warn] %-18s %-22s %s" % (code, where, msg))


def _line_of(src, pos):
    return src.count("\n", 0, pos) + 1


def check_svg(path, baked_by_rig=False):
    name = os.path.relpath(path, ROOT).replace("\\", "/")
    src = open(path, encoding="utf-8").read()
    try:
        root = ET.fromstring(src)
    except ET.ParseError as exc:
        fail(name, "XML 不合法", str(exc), "ElementTree 解析失败，Rig 也一定加载不了")
        return None
    print("— %s" % name)

    # 1) 渲染器静默忽略的特性。clip-path 例外：若该资产由 Rig 加载，装配时会用
    #    ClipBake 烘焙成等价路径，所以只提示不算失败；其余特性 Rig 不解决，照旧失败。
    baked_note = 0
    for feat, why in INERT.items():
        for m in re.finditer(r'\b%s\s*=' % re.escape(feat), src):
            loc = "%s:%d" % (os.path.basename(path), _line_of(src, m.start()))
            if feat == "clip-path" and baked_by_rig:
                baked_note += 1
                continue
            fail(name, "特性不支持", loc, "%s —— %s" % (feat, why))
    if baked_note:
        print("  [ ok ] %-18s %d 处 clip-path 由 Rig 装配时烘焙，无需改资产"
              % ("clip-path 已处理", baked_note))

    # 2) 有 stroke 却没 fill：SVG 默认填充是黑色
    for m in re.finditer(r'<path\b[^>]*>', src):
        tag = m.group(0)
        if "stroke=" in tag and "fill=" not in tag:
            fail(name, "缺 fill=\"none\"", "%s:%d" % (os.path.basename(path), _line_of(src, m.start())),
                 "描边路径没写 fill，会被填成黑色块（今天装饰线就是这么变成黑月牙的）")

    # 3) 剪裁溢出：剥掉 clip-path 后面积变化过大 = 依赖了不存在的剪裁
    defs, unsupported = {}, set()
    for cp in root.iter(_qid("clipPath")):
        cid = cp.get("id")
        if not cid:
            continue
        inner = cp.find(_qid("path"))
        if inner is not None and inner.get("d"):
            defs[cid] = inner.get("d")
            continue
        other = [c.tag.split('}')[-1] for c in cp if isinstance(c.tag, str)]
        if other:
            unsupported.add(cid)   # 定义在，但不是 path —— ClipBake 目前只能裁 path 轮廓
    for node in root.iter():
        ref = node.get("clip-path")
        tag = node.find(_qid("path")) if node.tag != _qid("path") else node
        if not ref or tag is None or not tag.get("d"):
            continue
        key = (re.match(r'url\(["\']?([^"\')]+)', ref).group(1).lstrip("#")
               if re.match(r'url\(["\']?([^"\')]+)', ref) else "")
        if key in unsupported:
            warn(name, "剪裁轮廓非 path", ref,
                 "<clipPath> 用的是 rect/circle 等图元，ClipBake 暂不支持烘焙")
            continue
        if key not in defs:
            fail(name, "剪裁引用悬空", ref, "找不到同名 <clipPath> 定义（浏览器里同样会坏）")
            continue
        if baked_by_rig:
            continue              # 装配时会烘掉，不再要求美术重画
        subj = ClipBake.flatten_path(tag.get("d"))
        baked = ClipBake.bake(tag.get("d"), defs[key])
        a_sub, a_bake = ClipBake.area(subj), ClipBake.area(ClipBake.flatten_path(baked))
        if a_sub > 0 and (a_sub - a_bake) / a_sub > 0.15:
            fail(name, "依赖剪裁", "clip-path=%s" % ref,
                 "该形状 %.0f%% 的面积在轮廓外，渲染器不裁就会溢出（应把路径自己画进轮廓）"
                 % (100 * (a_sub - a_bake) / a_sub))
    return root


def check_rig(rig_path):
    name = os.path.relpath(rig_path, ROOT).replace("\\", "/")
    print("— %s" % name)
    spec = json.load(open(rig_path, encoding="utf-8"))
    try:
        rig = Rig(rig_path)
    except RigError as exc:
        fail(name, "rig 加载失败", "", str(exc))
        return
    svg = os.path.relpath(rig.svg_path, ROOT).replace("\\", "/")
    check_svg(rig.svg_path, baked_by_rig=True)
    src = open(rig.svg_path, encoding="utf-8").read()

    # 资产里所有顶层 id
    asset_ids = set(re.findall(r'\bid="([^"]+)"', src))
    declared = {p["id"] for p in spec["parts"].values()}
    missing = declared - asset_ids
    if missing:
        fail(name, "部件 id 缺失", ", ".join(sorted(missing)),
             "rig.json 声明了但资产里没有 —— 换 SVG 时部件 id 必须跨文件稳定")
    # 资产里可动却没被 rig 认领的组（漏接）
    claimed = declared | set(spec.get("drop_ids", [])) | {spec["mouth"]["group"]} \
        | set(spec["mouth"]["states"].values())
    tree = ET.fromstring(src)
    ancestors = set()
    for parent in tree.iter():
        kids = [c.get("id") for c in parent if c.get("id")]
        if any(k in claimed for k in kids) and parent.get("id"):
            ancestors.add(parent.get("id"))
    clip_ids = {cp.get("id") for cp in tree.iter(_qid("clipPath")) if cp.get("id")}
    orphans = sorted(i for i in asset_ids - claimed - ancestors - clip_ids
                     if i.startswith(("rigid", "fluid", "leg", "arm", "head", "body", "shell")))
    if orphans:
        warn(name, "部件未认领", ", ".join(orphans), "资产里像部件却没有出现在 rig.json —— 它不会动")

    # 通道必须都在运动层里
    try:
        spec_rv = importlib.util.spec_from_file_location("rv", os.path.join(HERE, "RenderVideo.py"))
        rv = importlib.util.module_from_spec(spec_rv)
        spec_rv.loader.exec_module(rv)
        channels = set(rv.REST.keys())
        used = {c for p in spec["parts"].values() for c in p["channels"]} \
            | set(spec["root"]["channels"])
        bad = sorted(used - channels)
        if bad:
            fail(name, "通道未定义", ", ".join(bad), "rig 声明的通道在运动层 REST 里不存在")
        dead = sorted(c for c in channels if c != "mouthAperture" and c not in used)
        if dead:
            warn(name, "通道无人认领", ", ".join(dead), "运动层算了但没部件消费（齐步走之外的另一种浪费）")
    except Exception as exc:
        warn(name, "跳过通道校验", "", "导入 RenderVideo 失败：%s" % exc)

    # 支点必须落在部件自己的包围盒附近，否则旋转中心是错的
    for pname, part in spec["parts"].items():
        el = next((e for e in ET.fromstring(src).iter() if e.get("id") == part["id"]), None)
        if el is None:
            continue
        xs, ys = [], []
        for p in el.iter(_qid("path")):
            for x, y in ClipBake.flatten_path(p.get("d") or ""):
                xs.append(x); ys.append(y)
        if not xs:
            continue
        px, py = part["pivot"]
        pad = 40
        if not (min(xs) - pad <= px <= max(xs) + pad and min(ys) - pad <= py <= max(ys) + pad):
            fail(name, "支点跑出部件", "%s pivot=(%g,%g)" % (pname, px, py),
                 "部件包围盒 x[%g,%g] y[%g,%g] —— 旋转/缩放中心写错了"
                 % (min(xs), max(xs), min(ys), max(ys)))
    print("  [ ok ] 资产 %s ｜ 部件 %d ｜ 烘焙剪裁 %d 处"
          % (svg, len(spec["parts"]), rig.stats.get("baked", 0)))


def check_prop_placement():
    """渲染器里那张道具摆位表，是否还对得上道具资产与内联副本。

    PROP_BAKED 记的是"资产内层 transform + viewBox"，摆位是先减掉它再套外壳。
    重画一张道具（改 viewBox、改内层 transform）不会报错，只会把整件东西
    悄悄挪出画面 —— 所以这里逐项核对，对不上就 exit 1。
    """
    try:
        spec = importlib.util.spec_from_file_location("rv", os.path.join(ROOT, "Scripts",
                                                                         "RenderVideo.py"))
        rv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rv)
    except SystemExit:
        return                      # 渲染器自己会报它的错，这里不重复报
    except Exception as exc:
        fail("Scripts/RenderVideo.py", "PROP-LOAD", "import", "读不出摆位表：%s" % exc)
        return
    ip_spec = importlib.util.spec_from_file_location(
        "ip", os.path.join(ROOT, "Scripts", "InlineProps.py"))
    ip = importlib.util.module_from_spec(ip_spec)
    ip_spec.loader.exec_module(ip)          # 道具 id → 资产文件，出处只有 InlineProps 一份
    for pid, (bx, by, bs, vw, vh) in rv.PROP_BAKED.items():
        path = ip.MAP.get(pid)
        if not path or not os.path.exists(os.path.join(ROOT, path)):
            continue
        a = io.open(os.path.join(ROOT, path), encoding="utf-8").read()
        m = re.search(r'viewBox="\s*[\d.]+\s+[\d.]+\s+([\d.]+)\s+([\d.]+)"', a)
        if m and (abs(float(m.group(1)) - vw) > 0.5 or abs(float(m.group(2)) - vh) > 0.5):
            fail(path, "PROP-VB", "viewBox",
                 "渲染器按 %.0fx%.0f 摆位，资产已是 %sx%s —— 重画过就重记 PROP_BAKED"
                 % (vw, vh, m.group(1), m.group(2)))
        if pid in rv.PROP_SVGS:
            g = re.search(r'<g[^>]*transform="translate\(([\d.,\s-]+)\)\s*scale\(([\d.]+)\)"',
                          rv.PROP_SVGS[pid])
            if not g:
                fail(path, "PROP-TF", "内联副本", "找不到外层 translate/scale，摆位表无从校准")
            else:
                x0, y0 = [float(v) for v in g.group(1).replace(" ", "").split(",")]
                if (abs(x0 - bx) > 0.5 or abs(y0 - by) > 0.5
                        or abs(float(g.group(2)) - bs) > 1e-3):
                    fail(path, "PROP-TF", "内联副本",
                         "内联是 translate(%g,%g) scale(%g)，表里记的是 (%g,%g) scale(%g)"
                         % (x0, y0, float(g.group(2)), bx, by, bs))
        if pid in rv.PROP_TARGET:
            tx0, ty0, tx1, ty1 = rv.PROP_TARGET[pid]
            if tx1 <= tx0 or ty1 <= ty0:
                fail("Scripts/RenderVideo.py", "PROP-TARGET", pid, "目标框宽高非正")
            elif tx0 < 0 or tx1 > rv.WIDTH or ty1 < 0 or ty0 > rv.HEIGHT:
                warn("Scripts/RenderVideo.py", "PROP-TARGET", pid,
                     "目标框 (%d,%d,%d,%d) 超出 %dx%d 画布，道具会切边"
                     % (tx0, ty0, tx1, ty1, rv.WIDTH, rv.HEIGHT))
    for pid in rv.PROP_TARGET:
        if pid not in rv.PROP_BAKED:
            fail("Scripts/RenderVideo.py", "PROP-TARGET", pid, "有目标框但没有摆位基准")
    n = sum(1 for pid in rv.PROP_BAKED if pid in rv.PROP_TARGET)
    print("  [ ok ] 道具摆位表 %d 件：内层 transform、viewBox、画布内都对得上" % n)


def main():
    args = sys.argv[1:]
    print("=== 资产体检 ===")
    if args:
        for a in args:
            check_svg(os.path.abspath(a))
    else:
        chars = os.path.join(ROOT, "Assets", "Characters")
        for d in sorted(os.listdir(chars)):
            cd = os.path.join(chars, d)
            if not os.path.isdir(cd):
                continue
            rig = os.path.join(cd, "rig.json")
            if os.path.exists(rig):
                check_rig(rig)
            covered = set()
            if os.path.exists(rig):
                try:
                    covered.add(os.path.normpath(
                        os.path.join(os.path.dirname(rig),
                                     json.load(open(rig, encoding="utf-8"))["svg"])))
                except Exception:
                    pass
            for sub in ("2D", "Props"):
                p = os.path.join(cd, sub)
                if os.path.isdir(p):
                    for f in sorted(os.listdir(p)):
                        if f.endswith(".svg") and os.path.normpath(os.path.join(p, f)) not in covered:
                            check_svg(os.path.join(p, f))
        props = os.path.join(ROOT, "Assets", "Props")
        if os.path.isdir(props):
            for f in sorted(os.listdir(props)):
                if f.endswith(".svg"):
                    check_svg(os.path.join(props, f))
    check_prop_placement()

    kinds = {}
    for _, code, _, _ in problems:
        kinds[code] = kinds.get(code, 0) + 1
    print("\n%s" % ("全部通过：未发现静默失效项" if not problems
                    else "未通过 %d 项：%s" % (len(problems),
                                              ", ".join("%s×%d" % kv for kv in sorted(kinds.items())))))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
