#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资产装配层（L1 Kit 的第一块砖）：rig.json + 资产 SVG → 一帧内的角色标记。

存在理由：Docs/VibeMotionPlan.md §4 债 #5 —— 渲染器此前不读 `Assets/` 里任何 SVG，
几何全部内联在 Python 字符串里，"换一个 SVG 就换成片角色"物理上不可能。
本模块把几何搬回资产文件，Python 只按 rig.json 的契约施加变换。

依赖方向单一：本模块**不得** import RenderVideo（共享层零依赖，出片层依赖共享层）。
依赖：仅 stdlib。

直接运行可做冒烟测试：`python Scripts/Rig.py`
"""
import copy
import json
import math
import os
import re
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ClipBake

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace('', SVG_NS)          # 序列化时不出现 ns0: 前缀

# PyMuPDF 实测会静默忽略的特性（判决实验见 Docs/VibeMotionPlan.md §26）。
# 资产里一旦出现这些，构造时点名报告，不留静默降级。
INERT_FEATURES = {
    "clip-path": "剪裁不生效：阴影必须自己画对轮廓，或走后处理遮罩",
    "filter": "滤镜不生效（feTurbulence 等全部被丢）",
    "linearGradient": "线性渐变被拍平成单色",
    "radialGradient": "径向渐变被忽略，填充回落硬边纯黑",
    "stroke-dasharray": "虚线会画成实线",
    "display": "display:none 不生效，元素照样画",
    "visibility": "visibility:hidden 不生效",
}

MOUTH_RY_TOKEN = "__MOUTH_RY__"


class RigError(Exception):
    """契约不满足时直接失败，不做静默 fallback（对齐 §3 单一事实来源的处置方式）。"""


def _qid(tag):
    return "{%s}%s" % (SVG_NS, tag)


def _children_markup(el, clip_defs=None, stats=None):
    """元素内部序列化（跳过注释）。外层 <g> 由本模块重新生成，不沿用资产的那层。

    clip-path 不在这里简单丢弃，而是**编译**：PyMuPDF 完全不实现剪裁（§26），
    资产里"大块切线 + 裁进轮廓"的阴影照原样画就会溢出成矩形。
    ClipBake 用凸多边形裁剪把等价路径算出来，资产本身仍保留 clipPath 语义
    （人可编辑，将来换后端时自动正确）。
    """
    out = []
    for ch in el:
        if ch.tag is ET.Comment:
            continue
        for sub in ch.iter():
            sub.attrib.pop("filter", None)
            sub.attrib.pop(_qid("filter"), None)
        if clip_defs:
            _bake_clips(ch, clip_defs, stats)
        out.append(ET.tostring(ch, encoding="unicode"))
    return "".join(out)


def _bake_clips(el, clip_defs, stats=None):
    """就地把手上带 clip-path 的后代路径 d 换成裁剪后的等价路径，并去掉该属性。"""
    for node in list(el.iter()):
        ref = node.get("clip-path") or node.get(_qid("clip-path"))
        if not ref:
            continue
        m = re.match(r'url\(["\']?([^"\')]+)["\']?\)', ref)
        # url() 里带前导 '#'，而 clip_defs 的键是元素 id 本身 —— 不剥掉就永远查不到，
        # 于是静默退化成"只剥属性不裁剪"，正是本模块要修的那个病。
        key = (m.group(1) if m else "").lstrip("#")
        clip_d = clip_defs.get(key) if key else None
        # 自身就是被裁元素，或组内每个 path 各自被同一个轮廓裁
        targets = [node] if node.tag == _qid("path") else [d for d in node.iter() if d.tag == _qid("path")]
        if clip_d is None:
            raise RigError("clip-path 引用 '%s' 在资产里没有对应 <clipPath> 定义。"
                           "静默按不裁处理会让阴影溢出成矩形（本模块刚踩过的 bug），"
                           "所以这里直接失败。" % ref)
        for tgt in targets:
            d = tgt.get("d")
            if not d:
                continue
            baked = ClipBake.bake(d, clip_d)
            if ClipBake.area(ClipBake.flatten_path(baked)) <= 1e-6:
                tgt.set("d", "")              # 完全在轮廓外：留空路径而不是删元素，保持结构稳定
            else:
                tgt.set("d", baked)
        node.attrib.pop("clip-path", None)
        node.attrib.pop(_qid("clip-path"), None)
        if stats is not None and targets:
            stats["baked"] = stats.get("baked", 0) + len(targets)


def _find_by_id(root, eid):
    for el in root.iter():
        if el.get("id") == eid:
            return el
    return None


class Rig:
    """一份角色装配契约。构造时完成解析、口型展开与审计；assemble() 只做查表与拼装。"""

    def __init__(self, rig_path):
        self.path = os.path.abspath(rig_path)
        self.dir = os.path.dirname(self.path)
        with open(self.path, encoding="utf-8") as fh:
            self.spec = json.load(fh)

        self.svg_path = os.path.normpath(os.path.join(self.dir, self.spec["svg"]))
        if not os.path.exists(self.svg_path):
            raise RigError("rig.json 指向的资产不存在：%s" % self.svg_path)

        w = self.spec["wrap"]
        self.scale = float(w["scale"])
        self.tx, self.ty = [float(v) for v in w["translate"]]
        self.wrap_transform = "translate(%g, %g) scale(%g)" % (self.tx, self.ty, self.scale)

        src = open(self.svg_path, encoding="utf-8").read()
        root = ET.fromstring(src)
        self.viewbox = root.get("viewBox") or " ".join(str(v) for v in self.spec["viewBox"])

        # clipPath 定义表：id → 轮廓路径 d。装配时用它把阴影裁进轮廓（见 _bake_clips）。
        self.clip_defs = {}
        for cp in root.iter(_qid("clipPath")):
            inner = cp.find(_qid("path"))
            if cp.get("id") and inner is not None and inner.get("d"):
                self.clip_defs[cp.get("id")] = inner.get("d")
        self.stats = {"baked": 0}

        # ---- 审计：资产用了渲染器不支持的特性就点名。渐变尤其危险，它会回落成硬边纯黑 ----
        self.warnings = []
        for feat, why in INERT_FEATURES.items():
            n = len(re.findall(re.escape(feat), src))
            if n:
                self.warnings.append("%s ×%d —— %s" % (feat, n, why))
        for eid in self.spec.get("drop_ids", []):
            if _find_by_id(root, eid) is None:
                self.warnings.append("drop_ids 声明的 '%s' 在资产里不存在" % eid)

        # ---- 部件源码：id → 内部标记串。id 找不到就硬失败，不猜 ----
        self._parts = {}
        for name, part in self.spec["parts"].items():
            eid = part["id"]
            el = _find_by_id(root, eid)
            if el is None:
                raise RigError("部件 %s 声明的 id '%s' 在资产里找不到。"
                               "换 SVG 时部件 id 必须跨文件稳定（§7 前置条件）" % (name, eid))
            self._parts[name] = _children_markup(el, self.clip_defs, self.stats)

        # ---- 口型：PyMuPDF 不认 display/visibility，可见性在这里自己解析成 4 份模板 ----
        self._head_by_state = {}
        mouth = self.spec.get("mouth")
        if mouth:
            if "head" not in self._parts:
                raise RigError("mouth 契约要求 parts 里有 head")
            states = mouth["states"]
            head_el = _find_by_id(root, self.spec["parts"]["head"]["id"])
            mg_el = _find_by_id(head_el, mouth["group"])
            if mg_el is None:
                raise RigError("headGroup 里找不到 mouthGroup（id='%s'）" % mouth["group"])
            sync = mouth.get("sync")
            for state, eid in states.items():
                if _find_by_id(head_el, eid) is None:
                    raise RigError("口型状态 %s 指向的 id '%s' 不存在" % (state, eid))
                tree = copy.deepcopy(head_el)
                mg = _find_by_id(tree, mouth["group"])
                for ch in list(mg):
                    if ch.get("id") != eid:
                        mg.remove(ch)
                markup = _children_markup(tree, self.clip_defs, self.stats)
                if sync and state == "talk":
                    pat = r'(<ellipse[^>]*id="%s"[^>]*?ry=")[^"]*(")' % re.escape(sync["id"])
                    if not re.search(pat, markup):
                        raise RigError("sync 声明的 <%s> 不在 talk 口型里，无法按通道改写 ry"
                                       % sync["id"])
                    markup = re.sub(pat, r"\g<1>%s\g<2>" % MOUTH_RY_TOKEN, markup)
                self._head_by_state[state] = markup

    # ------------------------------------------------------------------ helpers
    def _local_pivot(self, pivot):
        nx, ny = pivot
        return (nx * self.scale + self.tx, ny * self.scale + self.ty)

    # ------------------------------------------------------- 世界坐标锚点
    # 为什么要这个：装配那串 transform 是给渲染器看的，运动层/道具层还需要知道
    # "这个姿态下他的手指尖在画布哪个像素"。以前是拿角色根坐标手写偏移去猜
    # （球挂在 cx+210），结果篮球四轮都糊在他脸上——系统里没有任何地方
    # 真的知道他的手在哪。
    # 实现上**不去另写一份变换公式**，而是把 assemble() 生成的那串
    # transform 字符串解析成矩阵。两处各写一遍算法迟早对不上，这个项目已经
    # 在"同一个量算两份"上栽过不止一次。

    @staticmethod
    def _mat(mult):
        """把一串 SVG transform（translate/scale/rotate[with圆心]）乘成一个矩阵。

        矩阵用 (a,b,c,d,e,f) 表示 x'=ax+by+c, y'=dx+ey+f，与 SVG 的
        "左边的变换作用于右边的结果"一致。
        """
        def mul(p, q):
            a1, b1, c1, d1, e1, f1 = p
            a2, b2, c2, d2, e2, f2 = q
            return (a1 * a2 + b1 * d2, a1 * b2 + b1 * e2, a1 * c2 + b1 * f2 + c1,
                    d1 * a2 + e1 * d2, d1 * b2 + e1 * e2, d1 * c2 + e1 * f2 + f1)

        out = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
        for fn, args in re.findall(r"(\w+)\(([^)]*)\)", mult or ""):
            v = [float(x) for x in args.replace(",", " ").split()]
            if fn == "translate":
                m = (1, 0, v[0], 0, 1, v[1] if len(v) > 1 else 0.0)
            elif fn == "scale":
                m = (v[0], 0, 0, 0, (v[1] if len(v) > 1 else v[0]), 0)
            elif fn == "rotate":
                deg = v[0]
                cx = v[1] if len(v) > 2 else 0.0
                cy = v[2] if len(v) > 2 else 0.0
                r = math.radians(deg)
                co, si = math.cos(r), math.sin(r)
                m = (co, -si, cx - co * cx + si * cy,
                     si, co, cy - co * cy - si * cx)
            else:
                raise RigError("锚点矩阵不支持变换 %r；assemble 里新增了"
                               "变换种类就要在这里补上，否则锚点会悄悄算错" % fn)
            out = mul(out, m)
        return out

    def part_matrix(self, name, pose):
        """部件源坐标 → 角色局部坐标（就是外层 translate(tx,ty) scale(S) 吃的那个空间）。"""
        if name not in self._parts:
            raise RigError("rig.json 里没有部件 %r，可选：%s"
                           % (name, sorted(self._parts)))
        part = self.spec["parts"].get(name, {})
        px, py = self._local_pivot(part["pivot"])
        m = dict(pose)
        root = self.spec["root"]["pivot"]
        rx, ry = self._local_pivot(root)
        chain = ("translate(%.2f, %.2f) rotate(%.2f, %.2f, %.2f) " % (
                    m.get("rootX", 0.0), m.get("rootY", 0.0), m.get("rootRot", 0.0), rx, ry)
                 + self._scale_str(part, px, py)
                 + self._transform(name, part, m, px, py)
                 + " " + self.wrap_transform)
        return self._mat(chain)

    def part_point(self, name, src_xy, pose):
        """部件锚点（资产 viewBox 坐标）在这个姿态下的局部坐标。"""
        a, b, c, d, e, f = self.part_matrix(name, pose)
        x, y = src_xy
        return (a * x + b * y + c, d * x + e * y + f)

    def anchor(self, name, pose):
        """rig.json 里给这个部件命名的锚点（如 armR 的 hand），返回局部坐标。"""
        anchors = self.spec.get("anchors", {})
        if name not in anchors:
            raise RigError("rig.json 的 anchors 里没有 %r，现有：%s"
                           % (name, sorted(anchors)))
        return self.part_point(name, anchors[name]["hand"], pose)

    # ------------------------------------------------------------------ assemble
    def _scale_str(self, part, px, py):
        """部件级缩放的变换串（绕支点）。没声明 scale 就返回空串。

        为什么放在 rig 而不是去改 SVG 路径：创作者要的是"视觉上更宽"，
        不是"重画这条轮廓"。一个数字随时能退回 1.0，几何一个字没动；
        改路径就把可逆的决定变成了不可逆的。
        """
        s = float(part.get("scale", 1.0) or 1.0)
        if abs(s - 1.0) < 1e-6:
            return ""
        return "translate(%g, %g) scale(%g, %g) translate(%g, %g) " % (px, py, s, s, -px, -py)

    def _scaled(self, part, base, px, py):
        return self._scale_str(part, px, py) + base

    def part_svg(self, name, transform=""):
        """单独取一个部件（已按 wrap 映射到渲染局部画布）。

        给"把龟壳从角色身上摘下来、单独丢在地上"这类画面用：
        同一个部件只有一份画法，不在渲染器里再抄一遍几何。
        部件级 scale 在这里同样生效 —— 地上那枚壳和背上那枚必须一样大。
        """
        if name not in self._parts:
            raise RigError("rig.json 里没有部件 %r，可选：%s"
                           % (name, sorted(self._parts)))
        part = self.spec["parts"].get(name, {})
        px, py = self._local_pivot(part.get("pivot", [250, 300]))
        t = ' transform="%s"' % transform if transform else ""
        sc = self._scale_str(part, px, py)
        return ('<g%s><g%s><g transform="%s">%s</g></g></g>'
                % (t, ' transform="%s"' % sc.strip() if sc else "",
                   self.wrap_transform, self._parts[name]))

    def assemble(self, pose, mouth_state="happy", skip=()):
        """pose = 运动层输出的通道字典 → 角色装配体 SVG 片段。

        skip 用来临时摘掉某个部件（开场的壳还没穿上身）。摘部件必须是**显式**的：
        资产里那套 display:none 在这个渲染器里不生效，藏东西只能不画。
        """
        m = dict(pose)
        inner = []
        for name in self.spec["z_order"]:
            if name in skip:
                continue
            part = self.spec["parts"][name]
            px, py = self._local_pivot(part["pivot"])
            inner.append('<g id="PLOBI_%s" transform="%s"><g transform="%s">%s</g></g>'
                         % (name,
                            self._scaled(part, self._transform(name, part, m, px, py),
                                         px, py),
                            self.wrap_transform, self._src(name, mouth_state, m)))
        rx, ry = self._local_pivot(self.spec["root"]["pivot"])
        return ('<g id="PLOBI_Root_装配体" transform="translate(%.2f, %.2f) '
                'rotate(%.2f, %.2f, %.2f)">%s</g>'
                % (m.get("rootX", 0.0), m.get("rootY", 0.0), m.get("rootRot", 0.0),
                   rx, ry, "".join(inner)))

    def _src(self, name, mouth_state, m):
        if name != "head" or not self._head_by_state:
            return self._parts[name]
        markup = self._head_by_state.get(mouth_state)
        if markup is None:
            raise RigError("未知口型状态 %r，可选：%s"
                           % (mouth_state, sorted(self._head_by_state)))
        sync = self.spec["mouth"].get("sync")
        if sync and MOUTH_RY_TOKEN in markup:
            ry = max(float(sync.get("min", 1.5)), float(m.get(sync["channel"], 3.0)))
            markup = markup.replace(MOUTH_RY_TOKEN, "%.2f" % ry)
        return markup

    def _transform(self, name, part, m, px, py):
        """各部件的变换组合。语义与运动层通道一一对应，改这里等于改角色 rig 定义。"""
        g = m.get
        if name == "shell":
            # 刚体：只平移 + 绕支点旋转，零形变
            return "translate(%.2f, %.2f) rotate(%.2f, %.2f, %.2f)" % (
                g("shellX", 0.0), g("shellY", 0.0), g("shellRot", 0.0), px, py)
        if name in ("legL", "legR"):
            key = "legLeft" if name == "legL" else "legRight"
            return "translate(%.2f, %.2f) scale(1, %.3f) rotate(%.2f) translate(%.2f, %.2f)" % (
                px, py, g(key + "Squash", 1.0), g(key + "Rot", 0.0), -px, -py)
        if name == "body":
            return "translate(%.2f, %.2f) scale(%.4f, %.4f) rotate(%.2f) translate(%.2f, %.2f)" % (
                px, py, g("bodyScaleX", 1.0), g("bodyScaleY", 1.0), g("bodyRot", 0.0), -px, -py)
        if name == "head":
            return "translate(0, %.2f) rotate(%.2f, %.2f, %.2f)" % (
                g("headY", 0.0), g("headRot", 0.0), px, py)
        if name in ("eyeL", "eyeR"):
            # 眨眼 = 眼睛绕自身中心竖向压扁。1.0 全开，0.06 基本闭上。
            return "translate(%.2f, %.2f) scale(1, %.3f) translate(%.2f, %.2f)" % (
                px, py, max(0.06, g("eyeSquash", 1.0)), -px, -py)
        if name in ("armL", "armR"):
            key = "armLeftRot" if name == "armL" else "armRightRot"
            return "rotate(%.2f, %.2f, %.2f)" % (g(key, 0.0), px, py)
        raise RigError("未实现部件 %r 的变换组合；新增部件要同时声明通道与变换语义" % name)


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    here = os.path.dirname(os.path.abspath(__file__))
    rig = Rig(os.path.join(os.path.dirname(here), "Assets", "Characters",
                           "Plobi", "rig.json"))
    print("[+] 资产：%s" % rig.svg_path)
    print("[+] viewBox=%s ｜ 部件=%s" % (rig.viewbox, ", ".join(rig.spec["parts"])))
    print("[+] 口型模板：%s" % ", ".join(sorted(rig._head_by_state)))
    print("[!] 资产里存在但渲染器不支持的特性：")
    for wn in rig.warnings:
        print("      %s" % wn)

    pose = {k: 0.0 for k in ("rootX", "rootY", "rootRot", "shellX", "shellY", "shellRot",
                             "bodyRot", "headRot", "headY", "armLeftRot", "armRightRot",
                             "legLeftRot", "legRightRot", "mouthAperture")}
    pose.update(bodyScaleX=1.0, bodyScaleY=1.0, legLeftSquash=1.0, legRightSquash=1.0)
    for st in sorted(rig._head_by_state):
        svg = rig.assemble(dict(pose, headRot=6.0, mouthAperture=9.0), st)
        print("[+] %-6s → %d 字符，部件组 %d 个，含 mouth_%s=%s"
              % (st, len(svg), svg.count('id="PLOBI_'), st, ("mouth_%s" % st) in svg))
