"""把重画后的道具资产**内联**进 Scripts/RenderVideo.py 的 PROP_SVGS 字典（出片用）。

为什么走内联而不是让渲染器读文件：用户裁决"可以内联"。渲染器架构不动、改动面最小，
也避开给渲染路径新增文件 IO。代价是资产又多了一份副本 —— 所以本脚本本身就是防漂移工具：
以后改完 Assets/Props/*.svg 重跑它即可，副本由脚本单向覆盖，不手改。

只替换外层 <g> 的**子节点**，外层 transform 原样保留（它是按资产 viewBox 坐标系标定的）。

index.html 那一半（预览用的 <g id="propXxx">）随 2026-09-26 删掉的预览页一起移除了；
不删的话 --apply 会在写完 RenderVideo 之后半路 FileNotFoundError。
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根，按脚本自身位置算
MAP = {
    "propSeam": "Assets/Props/ScreenBreachSeam.svg",
    "propGuitar": "Assets/Props/Guitar.svg",
    "propComic": "Assets/Props/ComicPage.svg",
    "propCelluloid": "Assets/Props/Celluloid.svg",
    "propPhone": "Assets/Props/Phone.svg",
    "propBall": "Assets/Props/Basketball.svg",
    "propHoop": "Assets/Props/BasketballHoop.svg",
    "propTv": "Assets/Props/RetroCRT_TV.svg",
    "propJellyfish": "Assets/Props/AuraJellyfish.svg",
    "propBadges": "Assets/Props/ReactionBadges.svg",
}


def asset_children(path):
    """取 <svg> 里的子节点，去掉 XML 注释与 <style>。"""
    s = io.open(os.path.join(ROOT, path), encoding="utf-8").read()
    inner = s[s.index(">") + 1:]
    inner = inner[:inner.rindex("</svg>")]
    inner = re.sub(r"<!--.*?-->", "", inner, flags=re.S)
    inner = re.sub(r"<style\b.*?</style>", "", inner, flags=re.S)
    return inner.strip("\n")


def match_g_close(text, open_idx):
    """从 <g  开头处往后数嵌套，返回 (子节点起点, 匹配 </g> 起点, 匹配 </g> 终点)。

    必须把"子节点起点"一起返回：早先版本只返回 </g> 的位置，替换时写成
    s[:start] + 新内容 + s[end:]，等于把新画**追加**在旧画后面而不是替换 ——
    旧的内联图形一行都没删掉，道具变成新旧两张叠在一起。
    """
    content_start = text.index(">", open_idx) + 1
    i = content_start
    depth = 1
    while depth:
        nxt_open = text.find("<g", i)
        nxt_close = text.find("</g>", i)
        if nxt_close < 0:
            raise ValueError("找不到匹配的 </g>")
        if 0 <= nxt_open < nxt_close:
            # 只有真正的开始标签才算（排除 <g/> 自闭合，本场景没有）
            depth += 1
            i = text.index(">", nxt_open) + 1
        else:
            depth -= 1
            if depth == 0:
                return content_start, nxt_close, nxt_close + 4
            i = nxt_close + 4
    raise ValueError("unreachable")


def patch_render_video(children_by_key):
    p = os.path.join(ROOT, "Scripts/RenderVideo.py")
    s = io.open(p, encoding="utf-8").read()
    for key, path in MAP.items():
        m = re.search(r'"%s": """' % key, s)
        if not m:
            raise SystemExit(f"[FAIL] PROP_SVGS 里没有 {key}")
        g_open = s.index("<g", m.end())
        cstart, cend, gend = match_g_close(s, g_open)
        kids = children_by_key[key]
        indented = "\n".join(("            " + ln if ln.strip() else ln) for ln in kids.splitlines())
        s = s[:cstart] + "\n" + indented + "\n        " + s[cend:]
        print(f"  RenderVideo {key:14s} 删旧 {cend-cstart:5d} 字符 → 写新 {len(indented):5d} 字符")
    io.open(p, "w", encoding="utf-8").write(s)


if __name__ == "__main__":
    children_by_key = {k: asset_children(v) for k, v in MAP.items()}
    for k, v in children_by_key.items():
        banned = [t for t in ("clip-path", "radialGradient", "linearGradient",
                              "stroke-dasharray", "filter=") if t in v]
        # fill 可以写在 stroke 前面，所以整条标签一起判，别只往后找
        tags = re.findall(r"<path[^>]*>", v)
        stroked = [t for t in tags if "stroke=" in t]
        missing = [t for t in stroked if "fill=" not in t]
        print(f"{k:14s} 子节点 {len(v):5d} 字符  禁用特性 {banned or '无'}  "
              f"带描边的 path {len(stroked)} 条，缺 fill 的 {len(missing)} 条")
    if "--apply" in sys.argv:
        patch_render_video(children_by_key)
        print("已写入 RenderVideo.PROP_SVGS")
    else:
        print("\n预演模式，未写入。加 --apply 才落盘。")
