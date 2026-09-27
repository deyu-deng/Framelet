# -*- coding: utf-8 -*-
"""契约层：一集"要渲什么"与后端"怎么画"之间的那道边界。

为什么单独一层：终局是**像搭积木一样装配动画**——稿子 → 分镜 → 动作库 → 出片。
积木要能拼，靠的不是动作数量，是接口标准。所以这一层只放三样后端无关的东西：

  1. **档期档案** `Episodes/<EP>/episode.json`：这一集用哪些动作、哪些屏幕内容、
     哪些道具、字幕下限多长、引用素材署谁的名。新增一集 = 写这个文件，不改代码。
  2. **时间线**：`Script.md` 顶部那段 ```json（`speech[]` 口播层 / `beats[]` 画面层）
     的读取与校验，以及时刻→拍→记号的纯查表。
  3. **通道词表**：19 个身体通道（`REST`）与拖拽滞后表。门禁按名字判动作质量，
     所以通道名是跨后端契约的一部分 —— 换 3D 也只换求值器，不改名字。

后端（`RenderVideo.py` 是第一个实现，Remotion 是第二个）把编舞**注册**进来：
`implement("dunk", fn)`。声明了却没实现的，`missing()` 会出**缺件报告**并 exit 1。
这一步是给"稿子里那一拍系统演不了"准备的：以前它会被无声降级（§41 手机悬在身侧
就是降级出厂的结果），现在它在装配前就打印成一张待办清单。
"""
import glob
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EPISODES_LOCAL = os.path.join(ROOT, "Episodes")
MIN_CAPTION_SEC = 0.9        # 短于此的字幕读不完
BEAT_SEAM = 0.02             # 拍与拍之间允许的缝隙（秒），再大就是 BuildScript 漏了空隙

# 画幅预设：构图数据，与后端无关 —— 两个后端都要按同一组 char_scale 摆人物
PRESETS = {
    "16:9": {
        "width": 1920, "height": 1080,
        "char_scale": 1.8, "char_center_x": 960, "char_center_y": 500,
        "sub_bottom_margin": 44, "sub_width": 1100, "sub_height": 104,
        "sub_font_size": 22,
    },
    "9:16": {
        "width": 1080, "height": 1920,
        "char_scale": 1.70, "char_center_x": 540, "char_center_y": 920,
        "sub_bottom_margin": 80, "sub_width": 920, "sub_height": 84,
        "sub_font_size": 24,
    },
    "1:1": {
        "width": 1080, "height": 1080,
        "char_scale": 1.35, "char_center_x": 540, "char_center_y": 500,
        "sub_bottom_margin": 40, "sub_width": 960, "sub_height": 76,
        "sub_font_size": 20,
    },
}

# 静止基准姿态（19 通道）。基准行由边界淡化与各段共用，不每帧重建。
REST = {
    "rootX": 0.0, "rootY": 0.0, "rootRot": 0.0,
    "shellX": 0.0, "shellY": 0.0, "shellRot": 0.0,
    "bodyScaleX": 1.0, "bodyScaleY": 1.0, "bodyRot": 0.0,
    "legLeftSquash": 1.0, "legRightSquash": 1.0,
    "legLeftRot": 0.0, "legRightRot": 0.0,
    "headRot": 0.0, "headY": 0.0,
    "armLeftRot": 0.0, "armRightRot": 0.0,
    "eyeSquash": 1.0,
    "mouthAperture": 3.0,
}
# 口型不参与滞后回读，其余通道都从核心姿态里取
CORE_CH = tuple(k for k in REST if k != "mouthAperture")

# 拖拽层级滞后（秒）：root → shell → head → arms → legs。腿排最末，见 §20.1
LAG_SHELL, LAG_HEAD, LAG_ARM, LAG_LEG = 0.08, 0.14, 0.20, 0.24
LAGGED = {"shellX": LAG_SHELL, "shellY": LAG_SHELL, "shellRot": LAG_SHELL,
          "headRot": LAG_HEAD, "headY": LAG_HEAD,
          "armLeftRot": LAG_ARM, "armRightRot": LAG_ARM,
          "legLeftSquash": LAG_LEG, "legRightSquash": LAG_LEG,
          "legLeftRot": LAG_LEG, "legRightRot": LAG_LEG}

# 编舞参数（CharacterProfile.md「运动参数」是它们的出处，改数值先改那份档案）
M_SQUASH = 0.22
M_ANTICIP = 0.220
M_OVERSHOOT = 1.10
M_PART = 0.180
M_STAGGER = 0.12
M_HEST = 0.60


def _fail(msg, hint=""):
    print(f"[ERROR] {msg}", flush=True)
    if hint:
        print(f"        {hint}", flush=True)
    sys.exit(1)


def _episode_roots():
    """内容住在哪：环境变量 STUDIO_ROOT 指过来的目录 + 工具自带的 Episodes/。

    分开是因为工具是公开的、内容不是 —— 一期片子的稿子和配音不该出现在 GitHub 上，
    而渲染又必须同时读到两边。所以这里只认"根目录"，不复制任何文件。
    """
    roots = []
    env = os.environ.get("STUDIO_ROOT")
    if env:
        roots.append(os.path.expanduser(env))
    roots.append(EPISODES_LOCAL)
    return [r for r in roots if os.path.isdir(r)]


def _episode_dir():
    """哪一集在制：--episode 显式给 > 环境变量 PLOBI_EPISODE > 唯一一份 episode.json。

    名字可以带一层账号目录（`plobi/EP001_SelfIntro`），因为内容仓库是按账号分树的。
    默认值刻意不做成常量：新一集一建出来，"当前在制"就该自动跟着变；
    而多集并存时逼他说清楚要渲哪一集 —— 猜错会渲出一部错的片子。
    """
    # 只认环境变量，不加命令行参数：渲染器和八道门禁各有各的参数表，
    # 给 --episode 挨个开口子会漏，而"在制哪一集"本来就是工作环境的事，不是某次命令的事。
    want = os.environ.get("PLOBI_EPISODE")
    if want:
        for r in _episode_roots():
            d = os.path.join(r, want)
            if os.path.isfile(os.path.join(d, "episode.json")):
                return d
        _fail(f"找不到 episode {want}",
              "在 %s 里找过 %s/episode.json" % ("、".join(_episode_roots()), want))
    found = []
    for r in _episode_roots():
        found += [os.path.dirname(p) for p in glob.glob(os.path.join(r, "*", "episode.json"))]
        found += [os.path.dirname(p) for p in glob.glob(os.path.join(r, "*", "*", "episode.json"))]
    if len(found) == 1:
        return found[0]
    _fail("在制的是哪一集说不确定（%d 个候选）" % len(found),
          "设 PLOBI_EPISODE=<目录>（可带一层账号名）；\n  候选：" + "\n  ".join(found))


PROFILE_DIR = _episode_dir()
EPISODE_ID = os.path.basename(PROFILE_DIR)
PROFILE = json.load(open(os.path.join(PROFILE_DIR, "episode.json"), encoding="utf-8"))

ASPECT_RATIO = PROFILE.get("aspect", "16:9")
ACTIVE_PRESET = PRESETS[ASPECT_RATIO]
WIDTH, HEIGHT = ACTIVE_PRESET["width"], ACTIVE_PRESET["height"]

ACTIONS = tuple(PROFILE["actions"])
MOVER_ACTIONS = tuple(PROFILE.get("mover_actions", ()))
SCREENS = tuple(PROFILE.get("screens", [None]))
KNOWN_PROPS = tuple(PROFILE.get("props", ()))
CREDITS = {k: v for k, v in PROFILE.get("credits", {}).items() if v}

# 素材按引用读原件，不在仓库里存副本（§41：读不到就退回光标，少一张图不该让整片渲不出来）
STILL_IMAGES = {k: os.path.join(PROFILE_DIR, "Picture", v)
                for k, v in PROFILE.get("stills", {}).items()}


def asset(rel):
    return os.path.normpath(os.path.join(ROOT, rel))


def _load_script(md_path):
    """读 Script.md 顶部的 ```json 围栏块，并把它当**契约**校验一遍。

    这里宁可退出也不"凑合渲染"：段表和渲染器对不上时片子照样出得来，
    错的地方只藏在某几秒里没人看得见 —— 那比失败更糟。
    """
    if not os.path.exists(md_path):
        _fail(f"Script.md 不存在: {md_path}",
              "渲染管线拒绝使用硬编码 fallback——时段必须以 Script.md 为准。")
    with open(md_path, "r", encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"```json\s*\n(.*?)\n```", text, flags=re.DOTALL)
    if not m:
        _fail(f"{md_path} 顶部未找到 ```json 围栏块（机器可读段表缺失）")
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        _fail(f"Script.md 机器可读段表 JSON 解析失败: {e}", "检查围栏块内引号/转义。")
        return
    if "segments" in data:
        _fail("Script.md 还是旧的单层段表（segments），渲染器已改吃两层时间线",
              "跑 python Scripts/BuildScript.py --write 重新生成。")

    for key in ("speech", "beats"):
        if not isinstance(data.get(key), list) or not data[key]:
            _fail(f"段表缺少非空的 {key}[] —— 生成器没跑，还是跑挂了？")
    for i, s in enumerate(data["speech"], 1):
        for k in ("start", "end", "dur", "en", "zh", "subs"):
            if k not in s:
                _fail(f"口播第 {i} 段缺字段 {k}")
        if s["end"] <= s["start"]:
            _fail(f"口播第 {i} 段 end({s['end']}) 不晚于 start({s['start']})")
        for c in s["subs"]:
            if c["end"] - c["start"] < MIN_CAPTION_SEC:
                _fail(f"口播第 {i} 段有一条字幕只有 {c['end'] - c['start']:.2f} s："
                      f"「{c['en'][:36]}」",
                      f"短于 {MIN_CAPTION_SEC} s 读不完，去 BuildScript.py 放宽切分。")
    for i, b in enumerate(data["beats"], 1):
        for k in ("start", "end", "action", "char_x", "cues", "screen"):
            if k not in b:
                _fail(f"第 {i} 拍缺字段 {k}")
        if b["end"] <= b["start"]:
            _fail(f"第 {i} 拍倒挂：{b['start']} -> {b['end']}")
        if b["action"] not in ACTIONS:
            _fail(f"第 {i} 拍动作 {b['action']!r} 本集档案没声明，后端也不会编舞",
                  f"episode.json 的 actions：{ACTIONS}")
        if b["screen"] not in SCREENS:
            _fail(f"第 {i} 拍屏幕内容 {b['screen']!r} 没有对应的画法", f"可选：{SCREENS}")
        for c in b["cues"]:
            if c["id"] not in KNOWN_PROPS:
                _fail(f"第 {i} 拍要点名道具 {c['id']!r}，本集档案里没有它",
                      f"已知：{KNOWN_PROPS}")
            if not 0.0 <= c["at"] <= 1.0:
                _fail(f"第 {i} 拍道具 {c['id']} 的 at={c['at']} 不在 0~1")
    beats = sorted(data["beats"], key=lambda x: x["start"])
    data["beats"] = beats
    if beats[0]["start"] > 1e-6:
        _fail(f"第 1 拍从 {beats[0]['start']} s 才开始，前面是空画面")
    for prev, nxt in zip(beats, beats[1:]):
        if abs(prev["end"] - nxt["start"]) > BEAT_SEAM:
            _fail(f"拍与拍之间有缝或重叠：{prev['end']} -> {nxt['start']}",
                  "重跑 BuildScript.py（它会把空隙并进前一拍）")
    prev_x = None
    for b in beats:
        if prev_x is not None and b["char_x"] != prev_x and b["action"] not in MOVER_ACTIONS:
            _fail(f"站位从 {prev_x} 换到 {b['char_x']}，但这拍的动作是 {b['action']}（不会走路）",
                  "换站位那拍要用会走路的动作，或把 char_x 写回上一个值")
        prev_x = b["char_x"]
    return data


SCRIPT = _load_script(os.path.join(PROFILE_DIR, PROFILE.get("script", "Script.md")))
SPEECH = SCRIPT["speech"]
BEATS = SCRIPT["beats"]
SCRIPT_SUBTITLES = [dict(c, seg=s["id"]) for s in SPEECH for c in s["subs"]]

# 时长与帧率也从段表读（确保 VO 时段与渲染时长同步）
TOTAL_DURATION = float(SCRIPT.get("duration", 60.0))
FPS = int(SCRIPT.get("fps", 30))
TOTAL_FRAMES = int(TOTAL_DURATION * FPS)
CUE_WINDOW = float(SCRIPT.get("cue_window", 6.0))
T_STAGE = [b["start"] for b in BEATS]
T_BOUNDS = T_STAGE + [BEATS[-1]["end"]]


def blank():
    """取一份基准姿态副本。分段函数一律从它开始填，避免漏通道。"""
    return dict(REST)


def beat_index(t):
    """时刻 t 落在第几拍。用拍表推导，不写死秒数。"""
    idx = 0
    for i, b in enumerate(BEATS):
        if t >= b["start"]:
            idx = i
    return min(idx, len(BEATS) - 1)


def beat(t):
    """这一拍（画面层的一格）。站位、动作、道具、口型都从这里取。"""
    return BEATS[beat_index(t)]


def cue_at(b, c):
    """记号的绝对时刻。指示自己的先后顺序最多铺 CUE_WINDOW 秒，之后是保持。

    拍可以很长（"介绍软件"那拍 28 秒），但那句括号里的"水母出现→展示界面"
    是几秒钟内的事；按整拍摊会把它们推到话都说完 5~10 秒之后。
    """
    if "at_s" in c:      # 口播认出来的记号：直接是"说到那个词的秒数"
        return b["start"] + float(c["at_s"])
    span = min(b["end"] - b["start"], CUE_WINDOW)
    return b["start"] + c["at"] * span


def cue_time(b, prop, kind):
    """道具记号落在哪一秒。编舞跟着它走，而不是跟着"段长的几分之几"。"""
    for c in b["cues"]:
        if c["id"] == prop and c["kind"] == kind:
            return cue_at(b, c)
    return None


# =============================================================================
# 注册表：声明（档案里）与实现（后端里）对账
# =============================================================================
_IMPL = {"action": {}, "screen": {}, "prop": {}}


def implement(kind, name, fn):
    """后端交出一个实现。编舞只认 (t, b0, b1, beat)，不在函数里查表写死秒数。"""
    _IMPL[kind][name] = fn
    return fn


def has(kind, name):
    return name in _IMPL[kind]


def impl(kind, name):
    return _IMPL[kind].get(name)


def missing():
    """声明了却没实现的东西 —— 装配前的缺件报告。

    为什么值得单独一关：缺积木时系统不会报错，它会**降级**（没有"抓"这个动作，
    手机就悬在身侧出厂）。降级不响，所以在这里响。
    """
    want = {"action": ACTIONS, "screen": tuple(s for s in SCREENS if s), "prop": KNOWN_PROPS}
    return {k: tuple(n for n in names if not has(k, n)) for k, names in want.items()}


def assert_complete():
    """后端装配完调用：有缺件就 exit 1，并把缺的按类别列成待办。"""
    gaps = {k: v for k, v in missing().items() if v}
    if gaps:
        lines = ["[FAIL] 本集档案声明了 %d 类共 %d 项，后端没有实现：" %
                 (len(gaps), sum(len(v) for v in gaps.values()))]
        for k in ("action", "screen", "prop"):
            if gaps.get(k):
                lines.append("  %-6s %s" % (k, " ".join(gaps[k])))
        lines.append("  要么补实现，要么从 %s/episode.json 里撤掉声明 —— 别让它静默降级。"
                     % EPISODE_ID)
        print("\n".join(lines), flush=True)
        sys.exit(1)
