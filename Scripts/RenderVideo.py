import math
import subprocess
import os
import sys
import textwrap
import base64
import io
import json
import re
import argparse
import fitz  # PyMuPDF

# 共享层：资产装配器。按脚本自身位置定位同目录，不依赖调用方的 cwd
# （CheckMotion.py / SnapshotFrames.py 都以 importlib 方式加载本文件）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from Rig import Rig   # L1 Kit：rig.json + 资产 SVG → 帧内角色标记
from Texture import Crayon   # 蜡笔/纸纹/线条抖动：只作用在最终像素上
from Fonts import font_path, font_load   # 中英字幕/署名的字体候选，Windows 优先
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# =============================================================================
# 1. 渲染模式与画幅比例配置中心 (Configuration Center)
# =============================================================================
# 渲染模式: "release" (纯净成片模式，无调试HUD，角色居中，精美字幕与场景)
#          "debug"   (开发调试模式，包含右侧参数监视器、实时正弦公式、时间码)
RENDER_MODE = "release"

# =============================================================================
# 1. 渲染模式（后端自己的开关）。**画幅、时间线、词表、通道、编舞参数都在
#    Scripts/Contract.py** —— 那是契约层，Remotion 后端要 import 同一份。
#    按原名接回来是有意的：门禁按这些名字读渲染器（CheckMotion 读 REST / T_BOUNDS /
#    TOTAL_FRAMES，CheckAsset 读 REST / WIDTH / HEIGHT，CheckSync 读 _cue_at），
#    改名等于拆门禁。
# =============================================================================
import Contract as K
from Contract import (
    ACTIONS, MOVER_ACTIONS, KNOWN_PROPS, SCREENS, MIN_CAPTION_SEC,
    CREDITS, STILL_IMAGES, ASPECT_RATIO, PRESETS, ACTIVE_PRESET, WIDTH, HEIGHT,
    SCRIPT, SPEECH, BEATS, SCRIPT_SUBTITLES, TOTAL_DURATION, FPS, TOTAL_FRAMES,
    CUE_WINDOW, T_STAGE, T_BOUNDS, REST, CORE_CH, LAGGED,
    LAG_SHELL, LAG_HEAD, LAG_ARM, LAG_LEG,
    M_SQUASH, M_ANTICIP, M_OVERSHOOT, M_PART, M_STAGGER, M_HEST,
    EPISODE_ID, PROFILE_DIR, asset, blank as _blank,
    beat_index as _beat_index, beat as _beat, cue_at as _cue_at, cue_time as _cue_time,
)


# 手绘质感：线条抖动 + 纸纹 + 开场聚光灯。--no-texture 关掉对比。
TEXTURE = True

# --watchable：分片 mp4，渲染途中就能打开看。默认关，成品要的是普通 moov 索引。
WATCHABLE = False


# =============================================================================
# 2. 时间线在契约层（Contract.py 读 Script.md 顶部那段 ```json 并校验）。
#    两层分开的原因：**口播段数 != 画面节拍数** ——
#      speech[]  配音与字幕      —— 时长来自实测音频（timing.json）
#      beats[]   动画、道具、站位 —— 挂在他括号指示的相对位置上
#    本文件不硬编码任何 VO/时段/道具映射；要改内容改 Voice.md，别改这里。
# =============================================================================


# =============================================================================
# 1.5 中英字幕 PNG 预渲染（PIL 一次性生成 5 段，base64 嵌入 SVG）
#     原因：mupdf 解析 SVG <text> 不可靠（中文 ~165ms/frame，英文甚至静默失败），
#           全部用 image 标签绕开
# =============================================================================
def prebake_subs():
    """为 SCRIPT_SUBTITLES 中每条字幕预渲染一张 RGBA 胶囊（底卡 + 中英两行）。

    单行设计彻底消除长文本堆砌与换行截断问题。
    存成 PIL 图而不是 SVG 里的 <text>：mupdf 画中文既慢又不可靠，
    而且质感层贴完之后再贴字，字才不会被线条抖动带着晃。
    """
    # 字体候选在 Scripts/Fonts.py。原来这里内联四个 C:/Windows/Fonts 路径 +
    # os.path.exists 逐个试，全落空时退到 PIL 默认位图字体：中文变方框，字幕照样贴上去。
    zh_font = font_load("zh", 20, index=0)
    en_font = font_load("en", 22)

    sub_w = ACTIVE_PRESET.get("sub_width", 1100)
    sub_h = ACTIVE_PRESET.get("sub_height", 76)
    zh_color = (244, 232, 220, 255)   # #F4E8DC 暖米白
    en_color = (236, 239, 244, 255)   # #ECEFF4 纯净白

    def wrap(text, f, max_w):
        """按像素把中文折成最多两行，断点优先落在标点后面。

        行长与时间分配是两件事：BuildScript 只管"这条字幕占几秒"，
        装不下就换行。先前在这里做字数上限，切短的余量会挤到最后一条，
        实测反而从 35 字变成 44 字。
        """
        if not text or f.getbbox(text)[2] <= max_w:
            return [text]
        cuts = [i for i, ch in enumerate(text) if ch in "，、；：。！？" and 4 <= i <= len(text) - 4]
        if cuts:
            best = min(cuts, key=lambda i: abs(f.getbbox(text[:i])[2] - max_w / 2))
            return [text[:best].rstrip("，、；："), text[best:].lstrip("，、；：")]
        mid = len(text) // 2
        return [text[:mid], text[mid:]]


    def fit(path, size, en_text, zh_text, min_size=13):
        """字号缩到放得下为止（左右各留 26 px）。中文 38 字的长句会顶到边。"""
        for n in range(size, min_size - 1, -1):
            f = ImageFont.truetype(path, n, index=0)
            wide = max(f.getbbox(t)[2] for t in (en_text or " ", zh_text or " "))
            if wide <= sub_w - 52:
                return f
        return ImageFont.truetype(path, min_size, index=0)

    en_path = font_path("en")
    zh_path = font_path("zh")

    for sub in SCRIPT_SUBTITLES:
        en = sub.get("en", "")
        zh = sub.get("zh", "")
        img = Image.new("RGBA", (sub_w, sub_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        # 卡片画进同一张图：以前卡片是 SVG 里的 rect，字幕是 PNG，两层分开贴。
        # 现在整块字幕（含底卡）是一张图，质感层处理完之后才贴上去 ——
        # 抖动只抖画面、不抖字，不然字幕看起来像在筛糠。
        draw.rounded_rectangle((0, 0, sub_w - 1, sub_h - 1), radius=20,
                              fill=(15, 18, 26, 209),
                              outline=(136, 192, 208, 56), width=2)
        # 中文在上、英文在下：这个账号的观众是中文母语者，中文才是主字幕，
        # 英文是口播原文的对位参考。（他 2026-09-22 裁决）
        zh_lines = []
        if zh:
            f_zh = fit(zh_path, 27, "", zh)
            zh_lines = wrap(zh, f_zh, sub_w - 60)
            for n, ln in enumerate(zh_lines):
                draw.text((sub_w // 2, 26 + n * 30), ln, font=f_zh,
                          fill=zh_color, anchor="mm")
        if en:
            # 英文行里会夹中文词（"search its Chinese name, 气韵"）。
            # segoeui 没有汉字 → 上一轮我声称修好了方框，其实只换了中文行，
            # 英文行照样是 □□。含汉字就整条改用中文字体画，它自带拉丁字形。
            has_cjk = any("一" <= ch <= "鿿" for ch in en)
            f_en = fit(zh_path if has_cjk else en_path, 18, en, zh)
            draw.text((sub_w // 2, sub_h - 15), en, font=f_en,
                      fill=en_color, anchor="mm")
        sub["sub_img"] = img
    print(f"[*] Pre-baked {len(SCRIPT_SUBTITLES)} fine-grained subtitle PNGs ({sub_w}x{sub_h})")


# 5 阶段剧情道具 SVG（从 index.html 复刻，适配 1920x1080 release 视口）
# 每个 prop 在自己的阶段内 1s 淡入 / 1s 淡出
PROP_SVGS = {
    "propGuitar": """
        <g transform="translate(0, 0) scale(1)">
  
              <g id="guitarNeck">
                <path d="M 150,150 L 214,34 L 236,46 L 174,162 Z" fill="#AC512C" stroke="#241812" stroke-width="8" stroke-linejoin="round"/>
                <path d="M 208,22 L 246,44 L 234,68 L 196,46 Z" fill="#CD6B43" stroke="#241812" stroke-width="8" stroke-linejoin="round"/>
                <circle cx="216" cy="30" r="6" fill="#FDEDD2" stroke="#241812" stroke-width="4"/>
                <circle cx="232" cy="40" r="6" fill="#FDEDD2" stroke="#241812" stroke-width="4"/>
                <circle cx="222" cy="54" r="6" fill="#FDEDD2" stroke="#241812" stroke-width="4"/>
              </g>
              <g id="guitarBody">
                <path d="M 150,152 C 196,168 208,214 186,246 C 214,272 210,330 168,356
                         C 122,384 62,370 40,326 C 20,286 34,244 62,226
                         C 40,196 52,158 92,146 C 114,140 134,142 150,152 Z"
                      fill="#CD6B43" stroke="#241812" stroke-width="10" stroke-linejoin="round"/>
                <path d="M 150,152 C 196,168 208,214 186,246 C 200,262 202,290 194,312
                         C 176,268 148,224 116,196 C 100,182 118,158 150,152 Z"
                      fill="#E8814F" opacity="0.55"/>
                <circle cx="112" cy="278" r="34" fill="#241812" opacity="0.86"/>
                <path d="M 78,318 L 148,332 L 144,348 L 74,334 Z" fill="#AC512C" stroke="#241812" stroke-width="6" stroke-linejoin="round"/>
                <path d="M 120,168 C 108,206 100,246 98,286" fill="none" stroke="#FDEDD2" stroke-width="4" stroke-linecap="round" opacity="0.7"/>
                <path d="M 136,172 C 126,210 118,250 116,288" fill="none" stroke="#FDEDD2" stroke-width="4" stroke-linecap="round" opacity="0.7"/>
              </g>
        </g>
    """,
    "propComic": """
        <g transform="translate(0, 0) scale(1)">
  
              <g id="page">
                <path d="M 22,18 L 236,10 L 276,44 L 282,224 L 30,236 Z" fill="#FDEDD2" stroke="#241812" stroke-width="9" stroke-linejoin="round"/>
                <path d="M 236,10 L 276,44 L 232,52 Z" fill="#F5E3C6" stroke="#241812" stroke-width="7" stroke-linejoin="round"/>
                <g id="panels">
                  <path d="M 46,52 L 148,48 L 150,128 L 48,132 Z" fill="none" stroke="#241812" stroke-width="5"/>
                  <path d="M 164,50 L 262,46 L 264,124 L 166,128 Z" fill="none" stroke="#241812" stroke-width="5"/>
                  <path d="M 48,148 L 262,142 L 264,214 L 50,220 Z" fill="none" stroke="#241812" stroke-width="5"/>
                </g>
                <g id="stickFigure">
                  <circle cx="96" cy="78" r="15" fill="none" stroke="#241812" stroke-width="6"/>
                  <path d="M 96,93 L 96,118 M 96,102 L 76,114 M 96,102 L 118,110 M 96,118 L 82,130 M 96,118 L 112,130"
                        fill="none" stroke="#241812" stroke-width="6" stroke-linecap="round"/>
                  <path d="M 188,66 q 16,-12 30,2 q 10,12 -4,20 l -18,10 l 6,-18 q -12,-4 -14,-14 Z"
                        fill="#FAABA1" stroke="#241812" stroke-width="5" stroke-linejoin="round"/>
                  <path d="M 74,168 q 22,-14 44,0 M 132,164 l 14,10 M 158,160 q 20,-10 40,2"
                        fill="none" stroke="#241812" stroke-width="5" stroke-linecap="round"/>
                </g>
                <circle cx="26" cy="18" r="7" fill="#CD6B43" stroke="#241812" stroke-width="4"/>
              </g>
        </g>
    """,
    "propCelluloid": """
        <g transform="translate(0, 0) scale(1)">
  
              <g id="sheetBack">
                <path d="M 40,58 L 214,38 L 236,150 L 62,170 Z" fill="#F5E3C6" stroke="#241812" stroke-width="8" stroke-linejoin="round"/>
              </g>
              <g id="sheetMid">
                <path d="M 56,44 L 232,28 L 252,140 L 76,156 Z" fill="#FFF8EA" stroke="#241812" stroke-width="8" stroke-linejoin="round" opacity="0.95"/>
              </g>
              <g id="sheetTop">
                <path d="M 74,30 L 250,16 L 268,128 L 92,142 Z" fill="#FDEDD2" stroke="#241812" stroke-width="9" stroke-linejoin="round"/>
                <g id="celFigures">
                  <circle cx="128" cy="52" r="11" fill="none" stroke="#241812" stroke-width="5"/>
                  <path d="M 128,63 L 126,88 M 126,72 L 108,80 M 126,72 L 146,66 M 126,88 L 112,104 M 126,88 L 142,100"
                        fill="none" stroke="#241812" stroke-width="5" stroke-linecap="round"/>
                  <circle cx="198" cy="46" r="11" fill="none" stroke="#241812" stroke-width="5"/>
                  <path d="M 198,57 L 200,82 M 200,66 L 182,62 M 200,66 L 220,76 M 200,82 L 188,100 M 200,82 L 216,98"
                        fill="none" stroke="#241812" stroke-width="5" stroke-linecap="round"/>
                  <path d="M 226,96 q 14,6 26,-2" fill="none" stroke="#FAABA1" stroke-width="5" stroke-linecap="round"/>
                </g>
              </g>
              <g id="flipbookEdge">
                <path d="M 92,142 L 268,128 M 94,152 L 270,138 M 96,162 L 272,148" fill="none" stroke="#AC512C" stroke-width="4" stroke-linecap="round" opacity="0.8"/>
              </g>
        </g>
    """,
    "propBall": """
        <g transform="translate(0, 0) scale(1)">
  
    

              <g id="basketball">
                <circle cx="0" cy="0" r="36" fill="#CD6B43" stroke="#241812" stroke-width="8"/>
    
                <path d="M 31,0 A 31,31 0 0 1 0,31 C 12,26 26,12 31,0 Z" fill="#AC512C"/>
    
                <path d="M -31,0 L 31,0 M 0,-31 L 0,31 M -21,-21 C -6,-8 -6,8 -21,21 M 21,-21 C 6,-8 6,8 21,21"
                      fill="none" stroke="#241812" stroke-width="5" stroke-linecap="round"/>
    
                <path d="M -26,-9 Q -24,-21 -12,-25" fill="none" stroke="#FFF8EA" stroke-width="6" stroke-linecap="round"/>
              </g>
        </g>
    """,
    "propPhone": """
        <g transform="translate(0, 0) scale(1)">
  
              <g id="phoneBody">
                <path d="M 34,22 C 30,10 44,6 110,5 C 176,6 190,10 186,22
                         C 192,60 194,120 194,215 C 194,310 192,370 186,408
                         C 190,420 176,424 110,425 C 44,424 30,420 34,408
                         C 28,370 26,310 26,215 C 26,120 28,60 34,22 Z"
                      fill="#241812" stroke="#241812" stroke-width="6" stroke-linejoin="round"/>
                <path d="M 42,32 C 40,22 54,18 110,17 C 166,18 180,22 178,32
                         C 184,68 185,124 185,215 C 185,306 184,362 178,398
                         C 180,408 166,412 110,413 C 54,412 40,408 42,398
                         C 36,362 35,306 35,215 C 35,124 36,68 42,32 Z"
                      fill="#FDEDD2" stroke="#241812" stroke-width="5" stroke-linejoin="round"/>
              </g>
              <rect id="phoneScreen" x="48" y="44" width="124" height="342" rx="16" fill="#1B1410"/>
              <g id="phoneDetails">
                <path d="M 88,28 L 132,27" fill="none" stroke="#241812" stroke-width="6" stroke-linecap="round"/>
                <path d="M 190,120 L 198,122 L 198,168 L 190,166 Z" fill="#CD6B43" stroke="#241812" stroke-width="4" stroke-linejoin="round"/>
                <path d="M 108,400 q 6,8 12,0" fill="none" stroke="#AC512C" stroke-width="5" stroke-linecap="round"/>
              </g>
        </g>
    """,
    "propSeam": """
        <g transform="translate(600, 90) scale(1.8)">
  

  
              <rect x="-4" y="-16" width="408" height="532" fill="#151009"/>

  
              <g id="seamWarmGlow">
                <ellipse cx="200" cy="257" rx="40" ry="170" fill="#AC512C"/>
                <ellipse cx="200" cy="257" rx="27" ry="165" fill="#CD6B43"/>
                <ellipse cx="200" cy="257" rx="11" ry="158" fill="#FFF8EA"/>
              </g>

  
              <path id="curtainLeft"
                    d="M 226,-16 L 224,-2 C 210,80 166,160 166,265 C 166,360 208,430 226,502 L 228,516 L -4,516 L -4,-16 Z"
                    fill="#241812"/>
              <path d="M 226,-16 L 224,-2 C 210,80 166,160 166,265 C 166,360 208,430 226,502 L 228,516"
                    fill="none" stroke="#241812" stroke-width="8" stroke-linejoin="round"/>

  
              <path id="curtainRight"
                    d="M 174,-16 L 176,-2 C 190,80 234,160 234,265 C 234,360 192,430 174,502 L 172,516 L 404,516 L 404,-16 Z"
                    fill="#241812"/>
              <path d="M 174,-16 L 176,-2 C 190,80 234,160 234,265 C 234,360 192,430 174,502 L 172,516"
                    fill="none" stroke="#241812" stroke-width="8" stroke-linejoin="round"/>

  
              <g id="curtainFolds">
                <path d="M 62,-16 C 76,150 74,350 54,516 L 30,516 C 50,350 52,150 38,-16 Z" fill="#0C0806" opacity="0.95"/>
                <path d="M 122,-16 C 136,160 134,340 114,516 L 92,516 C 112,340 114,160 100,-16 Z" fill="#0C0806" opacity="0.95"/>
                <path d="M 338,-16 C 324,150 326,350 346,516 L 370,516 C 350,350 348,150 362,-16 Z" fill="#0C0806" opacity="0.95"/>
                <path d="M 278,-16 C 264,160 266,340 286,516 L 308,516 C 288,340 286,160 300,-16 Z" fill="#0C0806" opacity="0.95"/>
              </g>

  
              <path d="M 200,104 C 184,150 156,204 156,265 C 156,330 184,382 200,412
                       C 190,382 168,330 168,265 C 168,204 194,150 200,104 Z"
                    fill="#AC512C" opacity="0.26"/>
              <path d="M 200,104 C 216,150 244,204 244,265 C 244,330 216,382 200,412
                       C 210,382 232,330 232,265 C 232,204 206,150 200,104 Z"
                    fill="#AC512C" opacity="0.26"/>
        </g>
    """,
    "propHoop": """
        <g transform="translate(580, 0) scale(1.5)">
  
  
  
  
  
  

  
              <g id="hangerRig">
                <path d="M 120,0 L 120,44" fill="none" stroke="#FDEDD2" stroke-width="5" stroke-linecap="round"/>
                <path d="M 280,0 L 280,44" fill="none" stroke="#FDEDD2" stroke-width="5" stroke-linecap="round"/>
                <circle cx="120" cy="44" r="7" fill="#AC512C"/>
                <circle cx="280" cy="44" r="7" fill="#AC512C"/>
              </g>

  
              <g id="backboard">
                <rect x="40" y="40" width="320" height="190" rx="20" fill="#CD6B43" stroke="#241812" stroke-width="9" stroke-linejoin="round"/>
    
                <path d="M 300,46 L 340,46 A 14,14 0 0 1 354,60 L 354,210 A 14,14 0 0 1 340,224 L 292,224 Z" fill="#AC512C"/>
    
                <rect x="140" y="88" width="120" height="82" rx="10" fill="none" stroke="#FDEDD2" stroke-width="9" stroke-linejoin="round"/>
              </g>

  
              <g id="rimMount">
                <rect x="174" y="166" width="52" height="48" rx="11" fill="#AC512C" stroke="#241812" stroke-width="8" stroke-linejoin="round"/>
                <circle cx="187" cy="180" r="4.5" fill="#FDEDD2"/>
                <circle cx="213" cy="180" r="4.5" fill="#FDEDD2"/>
              </g>

  
              <g id="net">
                <path d="M 152,228 L 176,318  M 248,228 L 224,318
                         M 152,228 L 192,318  M 184,235 L 176,318
                         M 184,235 L 208,318  M 216,235 L 192,318
                         M 216,235 L 224,318  M 248,228 L 208,318"
                      fill="none" stroke="#FDEDD2" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>
                <path d="M 160,260 Q 200,270 240,260" fill="none" stroke="#FFF8EA" stroke-width="4" stroke-linecap="round"/>
                <path d="M 169,292 Q 200,301 231,292" fill="none" stroke="#FFF8EA" stroke-width="4" stroke-linecap="round"/>
              </g>

  
              <g id="rim">
                <ellipse cx="200" cy="220" rx="56" ry="17" fill="none" stroke="#241812" stroke-width="16"/>
                <ellipse cx="200" cy="220" rx="56" ry="17" fill="none" stroke="#CD6B43" stroke-width="8"/>
              </g>

  
        </g>
    """,
    "propTv": """
        <g transform="translate(1040, 403) scale(1.7)">
  
  
  
  
  
  

  
              <g id="antenna">
                <path d="M 188,40 L 132,14" fill="none" stroke="#AC512C" stroke-width="9" stroke-linecap="round"/>
                <path d="M 212,40 L 268,14" fill="none" stroke="#AC512C" stroke-width="9" stroke-linecap="round"/>
                <circle cx="132" cy="14" r="9" fill="#CD6B43" stroke="#241812" stroke-width="6"/>
                <circle cx="268" cy="14" r="9" fill="#CD6B43" stroke="#241812" stroke-width="6"/>
                <rect x="175" y="32" width="50" height="16" rx="8" fill="#AC512C" stroke="#241812" stroke-width="7" stroke-linejoin="round"/>
              </g>

  
              <g id="legs">
                <rect x="70" y="286" width="42" height="38" rx="12" fill="#AC512C" stroke="#241812" stroke-width="8" stroke-linejoin="round" transform="rotate(12, 91, 305)"/>
                <rect x="288" y="286" width="42" height="38" rx="12" fill="#AC512C" stroke="#241812" stroke-width="8" stroke-linejoin="round" transform="rotate(-12, 309, 305)"/>
              </g>

  
              <g id="tvBody">
                <rect x="25" y="45" width="350" height="260" rx="36" fill="#FDEDD2" stroke="#241812" stroke-width="10" stroke-linejoin="round"/>
    
                <path d="M 300,51 L 339,51 A 30,30 0 0 1 369,81 L 369,269 A 30,30 0 0 1 339,299 L 292,299 Z" fill="#F5E3C6"/>
              </g>

  
              <g id="tvBezel">
                <rect x="46" y="66" width="243" height="218" rx="30" fill="#AC512C" stroke="#241812" stroke-width="9" stroke-linejoin="round"/>
              </g>

  
              <g id="crtScreen">
                <rect x="58" y="78" width="219" height="194" rx="22" fill="#241812"/>
                <text x="74" y="182" font-family="'Courier New', monospace" font-size="26" font-weight="900" fill="#4EEDA4" letter-spacing="3">&gt; NIU LAI!</text>
              </g>

  
              <g id="controlPanel">
                <rect x="302" y="80" width="58" height="9" rx="4.5" fill="#CD6B43" stroke="#241812" stroke-width="4" stroke-linejoin="round"/>
                <rect x="302" y="97" width="58" height="9" rx="4.5" fill="#CD6B43" stroke="#241812" stroke-width="4" stroke-linejoin="round"/>
                <rect x="302" y="114" width="58" height="9" rx="4.5" fill="#CD6B43" stroke="#241812" stroke-width="4" stroke-linejoin="round"/>

                <circle cx="331" cy="162" r="25" fill="#CD6B43" stroke="#241812" stroke-width="8"/>
                <circle cx="331" cy="162" r="16" fill="#AC512C"/>
                <rect x="327" y="148" width="8" height="15" rx="4" fill="#FDEDD2"/>

                <circle cx="331" cy="224" r="21" fill="#CD6B43" stroke="#241812" stroke-width="7"/>
                <circle cx="331" cy="224" r="13" fill="#AC512C"/>
                <rect x="327.5" y="213" width="7" height="13" rx="3.5" fill="#FDEDD2"/>

                <circle cx="331" cy="272" r="8" fill="#FAABA1" stroke="#241812" stroke-width="5"/>
              </g>
        </g>
    """,
    "propJellyfish": """
        <g transform="translate(144, 10) scale(1.6)">
  

  
              <g id="sparkles">
                <path d="M 60,76 C 56,66 44,65 40,73 C 36,65 24,67 20,77 C 16,89 30,102 40,110 C 50,102 64,89 60,76 Z"
                      fill="#FAABA1" stroke="#241812" stroke-width="7" stroke-linejoin="round"/>
                <path d="M 292,84 Q 297,101 313,106 Q 297,111 292,128 Q 287,111 271,106 Q 287,101 292,84 Z"
                      fill="#CD6B43"/>
              </g>

  
              <g id="backTentacles">
                <path d="M 88,170 C 76,206 68,232 56,258 C 50,270 58,280 68,274 C 76,268 74,258 76,248 C 86,224 96,200 108,172 Z"
                      fill="#F5E3C6" stroke="#241812" stroke-width="7" stroke-linejoin="round"/>
                <path d="M 212,172 C 224,200 234,224 244,248 C 246,258 244,268 252,274 C 262,280 270,270 264,258 C 252,232 244,206 232,170 Z"
                      fill="#F5E3C6" stroke="#241812" stroke-width="7" stroke-linejoin="round"/>
              </g>

  
              <g id="mainTentacles">
                <path d="M 118,168 C 112,214 104,252 88,290 C 82,304 88,316 100,312 C 110,308 110,296 108,286 C 122,250 132,212 142,170 Z"
                      fill="#FDEDD2" stroke="#241812" stroke-width="8" stroke-linejoin="round"/>
                <path d="M 148,170 C 144,220 148,262 146,300 C 145,314 156,320 163,312 C 168,304 162,294 162,282 C 164,248 168,212 172,170 Z"
                      fill="#FDEDD2" stroke="#241812" stroke-width="8" stroke-linejoin="round"/>
                <path d="M 178,170 C 188,212 196,250 212,286 C 216,296 214,308 224,312 C 236,316 240,304 234,290 C 218,252 208,214 202,168 Z"
                      fill="#FDEDD2" stroke="#241812" stroke-width="8" stroke-linejoin="round"/>
              </g>

  
              <g id="jellyBell">
                <path d="M 58,155 C 50,84 98,42 160,42 C 222,42 270,84 262,155
                         C 262,180 240,196 220,182
                         C 200,168 186,192 160,192
                         C 134,192 120,168 100,182
                         C 80,196 58,180 58,155 Z"
                      fill="#FDEDD2" stroke="#241812" stroke-width="9" stroke-linejoin="round"/>

    
                <path d="M 206,58 C 238,82 251,118 246,152 C 242,160 234,158 231,148 C 232,110 224,82 206,58 Z"
                      fill="#F5E3C6"/>

    
                <path d="M 98,98 C 110,68 134,52 162,47 C 155,63 146,72 134,80 C 121,89 109,98 104,110 Z"
                      fill="#FFF8EA"/>

    
                <g id="jellyEyes">
                  <ellipse cx="130" cy="140" rx="10" ry="12" fill="#241812"/>
                  <ellipse cx="190" cy="140" rx="10" ry="12" fill="#241812"/>
                </g>

                <ellipse cx="98" cy="158" rx="15" ry="9" fill="#FAABA1" opacity="0.9"/>
                <ellipse cx="222" cy="158" rx="15" ry="9" fill="#FAABA1" opacity="0.9"/>

                <path d="M 150,158 Q 160,170 170,158 Q 160,164 150,158 Z" fill="#241812"/>
              </g>
        </g>
    """,
    "propBadges": """
        <g transform="translate(560, 920) scale(1.6)">
  
  
  
  
  
  

  
              <g id="badgeLike" transform="translate(75, 80)">
                <circle cx="0" cy="0" r="46" fill="#CD6B43" stroke="#241812" stroke-width="9"/>
                <path d="M -33.8,-12.3 Q -28,-28 -12.3,-33.8" fill="none" stroke="#FFF8EA" stroke-width="7" stroke-linecap="round"/>
                <g transform="translate(2, 0)">
                  <rect x="-26" y="1" width="15" height="24" rx="5" fill="#FDEDD2" stroke="#241812" stroke-width="6" stroke-linejoin="round"/>
                  <path d="M -11,1 C -11,-7 -7,-18 -1,-22 C 5,-25 13,-21 13,-14 C 13,-9 11,-5 9,-1 L 15,-1
                           C 21,-1 23,3 21,8 L 16,20 C 15,23 12,25 8,25 L -11,25 Z"
                        fill="#FDEDD2" stroke="#241812" stroke-width="6" stroke-linejoin="round"/>
      
                  <path d="M 9,7 L 20,8 M 9,16 L 18,16" fill="none" stroke="#241812" stroke-width="4" stroke-linecap="round"/>
                </g>
              </g>

  
              <g id="badgeCoin" transform="translate(225, 80)">
                <circle cx="0" cy="0" r="46" fill="#FDEDD2" stroke="#241812" stroke-width="9"/>
                <path d="M -33.8,-12.3 Q -28,-28 -12.3,-33.8" fill="none" stroke="#FFF8EA" stroke-width="7" stroke-linecap="round"/>
                <g transform="translate(0, 3) scale(1.06)">
                  <path d="M -6,-10 L -15,-24" fill="none" stroke="#AC512C" stroke-width="6" stroke-linecap="round"/>
                  <path d="M 6,-10 L 15,-24" fill="none" stroke="#AC512C" stroke-width="6" stroke-linecap="round"/>
                  <rect x="-20" y="-10" width="40" height="32" rx="10" fill="#AC512C" stroke="#241812" stroke-width="6" stroke-linejoin="round"/>
                  <rect x="-13" y="-4" width="26" height="20" rx="6" fill="#FDEDD2"/>
                  <circle cx="-6" cy="6" r="3" fill="#AC512C"/>
                  <circle cx="6" cy="6" r="3" fill="#AC512C"/>
                </g>
              </g>

  
              <g id="badgeFav" transform="translate(375, 80)">
                <circle cx="0" cy="0" r="46" fill="#FAABA1" stroke="#241812" stroke-width="9"/>
                <path d="M -33.8,-12.3 Q -28,-28 -12.3,-33.8" fill="none" stroke="#FFF8EA" stroke-width="7" stroke-linecap="round"/>
                <polygon points="0,-24 7.1,-7.7 24.7,-6 11.4,5.7 15.3,23 0,14 -15.3,23 -11.4,5.7 -24.7,-6 -7.1,-7.7"
                         fill="#FDEDD2" stroke="#241812" stroke-width="6" stroke-linejoin="round"/>
              </g>
        </g>
    """,
}

def get_active_subtitle(t):
    for sub in SCRIPT_SUBTITLES:
        if sub["start"] <= t < sub["end"]:
            return sub
    return None


# 模块加载时一次性预渲染短句双语字幕 PNG
prebake_subs()

# =============================================================================
# 3. 动作状态机：事件姿态 Vibe-Motion 物理计算 (Event-Pose Motion State Machine)
# =============================================================================

# ---- 缓动工具 (Easing)：替代全局 sin 噪声，用事件驱动的缓动曲线 ----
def _clamp01(x):
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)

def ease_out_cubic(p):
    p = _clamp01(p)
    return 1.0 - (1.0 - p) ** 3

def ease_in_cubic(p):
    p = _clamp01(p)
    return p ** 3

def ease_in_out_quad(p):
    p = _clamp01(p)
    return 2.0 * p * p if p < 0.5 else 1.0 - (-2.0 * p + 2.0) ** 2 / 2.0

def ease_out_quad(p):
    p = _clamp01(p)
    return 1.0 - (1.0 - p) ** 2

def ease_in_quad(p):
    p = _clamp01(p)
    return p * p

def ease_in_out_sine(p):
    p = _clamp01(p)
    return -(math.cos(math.pi * p) - 1.0) / 2.0

def ease_out_back(p, s=1.70158):
    """越过目标值再回弹（overshoot），用于弹回/起跳的弹性感。"""
    p = _clamp01(p)
    return 1.0 + (s + 1.0) * (p - 1.0) ** 3 + s * (p - 1.0) ** 2

def _impulse(t, tc, amp, decay, freq, lag=0.0):
    """事件触发后的阻尼余振（follow-through）：tc 之后 lag 秒起，按 exp 衰减正弦衰减为 0。
    用于壳/头/手臂在碰撞、落地等事件后的滞后摆动，天然收敛到静止，无常驻抖动。"""
    tt = t - (tc + lag)
    if tt <= 0.0:
        return 0.0
    return amp * math.exp(-decay * tt) * math.sin(freq * tt)

def _bump(t, tc, half, peak):
    """以 tc 为中心的局部隆起（raised-cosine），宽度 2*half，峰值 peak。
    用于接触帧 squash & stretch —— 只在加速段/落地瞬间出现并快速恢复。"""
    d = t - tc
    if abs(d) >= half:
        return 0.0
    return peak * 0.5 * (1.0 + math.cos(math.pi * d / half))

def _breathe(t_rel, period=3.8, amp=0.03):
    """极缓呼吸（±amp），提供生命感而非机械抖动；周期 ~4s，远低于抖动频率。"""
    return math.sin(2.0 * math.pi * t_rel / period) * amp


# =============================================================================
# 3.4 运动参数 token —— 出处：Assets/Characters/Plobi/CharacterProfile.md「运动参数」节
#     改数值先改那份文档，再改这里；不要在任何分支里写死第二个值。
# =============================================================================





# =============================================================================
# 3.3 编舞：一拍一个动作，动作由**画面层**的 action 选，不再按"第几段"分支
#     旧实现是 5 段各写一段身体语言，段表一改（口播 5→4 段、画面 9 拍）整块就废。
#     现在的三条硬约定：
#       * 动作函数只认 (t, b0, b1) —— 拍边界传进来，不在函数里查表写死秒数；
#       * 每个动作**从静止起、回静止落**（窗口首尾各项归零），所以拍与拍接缝
#         天然连续，不再需要 §20.2 那套边界交叉淡化；
#       * 关键帧时刻跟着道具记号走（篮筐在哪一秒碎，手在哪一秒扣到），
#         而不是"段长的 9/12"—— 段长变了，动作不会跟画面对不上。
#     常驻正弦噪声一律禁止：只允许 _impulse 阻尼余振 + _breathe 极缓生命感。
# =============================================================================












def _rel(t, a, b):
    return _clamp01((t - a) / max(1e-6, b - a))


def _core_blank():
    m = dict(REST)
    m.pop("mouthAperture")
    return m


def _blend_rest(m, w):
    """整份姿态按 w 从 REST 插值 —— 给"这一拍的基调"加进出坡用。"""
    for ch in CORE_CH:
        m[ch] = REST[ch] + (m[ch] - REST[ch]) * w
    return m


def _core_entrance(t, b0, b1, b):
    """龟壳先丢在地上晃住，Plobi 从画左小跑进来，站定把壳套上。

    人物设定里的"固定开场"是扒缝钻出来（CharacterProfile.md「招牌动作与开场」），
    这一集 Voice.md 改成了丢壳 → 走出来穿好。按**本集原稿**编舞，
    两份对不上已进矛盾清单，不替他合并。
    """
    m = _core_blank()
    span = b1 - b0
    w0, w1 = b0 + 0.12 * span, b0 + 0.78 * span          # 走位窗口
    d0 = w1                                             # 站定 → 套壳
    d1 = w1 + 3.3 * M_PART                              # 三个部件依次归位
    # --- 小跑：窗口首尾 env 归零，接缝上不出现"突然迈步" ---------------------
    env = math.sin(_rel(t, w0, d0) * math.pi)
    u = (t - w0) / 0.55                                 # 步频 0.55 s/步
    sw = math.sin(2.0 * math.pi * u) * env
    m["legLeftRot"] = 14.0 * sw
    m["legRightRot"] = -14.0 * sw
    m["legLeftSquash"] = 1.0 - 0.05 * max(0.0, -sw)
    m["legRightSquash"] = 1.0 - 0.05 * max(0.0, sw)
    m["rootY"] = -4.0 * env * abs(math.sin(2.0 * math.pi * u))
    m["rootRot"] = 3.0 * env + 2.0 * sw                 # 小跑前倾
    m["armLeftRot"] = -11.0 * sw
    m["armRightRot"] = 11.0 * sw
    m["headRot"] = (4.0 + 2.0 * math.sin(math.pi * u)) * env
    # --- 套壳：一次屈膝下蹲，双手举过头顶把壳按下来 --------------------------
    p = _rel(t, d0, d1)
    if 0.0 < p < 1.0:
        dip = math.sin(p * math.pi)                     # 0→1→0
        m["bodyScaleX"] = 1.0 + 0.12 * dip
        m["bodyScaleY"] = 1.0 - 0.12 * dip
        m["rootY"] = 10.0 * dip
        m["legLeftSquash"] = 1.0 - M_SQUASH * dip
        m["legRightSquash"] = 1.0 - M_SQUASH * dip
        m["armLeftRot"] = -140.0 * dip
        m["armRightRot"] = 140.0 * dip
        m["headY"] = -6.0 * dip
        m["headRot"] = -8.0 * dip
        m["shellRot"] = 6.0 * math.sin(p * 2.0 * math.pi)   # 套歪一下再正
    return m


def _talk_gesture(k, t, gs, ge):
    """站桩说话时的一次手势：抬起—落回，窗口首尾回到静止。

    三种轮换（抬手解释 / 挠头 / 摊手）按事件序号取确定性余数 ——
    不用 random：同一帧号必须永远同一姿态，否则两次渲染对不上就没法回归。
    """
    p = _rel(t, gs, ge)
    if not 0.0 < p < 1.0:
        return {}
    bump = math.sin(p * math.pi)
    slow = math.sin(p * math.pi * 2.0)
    kind = (k * 5) % 3
    if kind == 0:                                   # 抬手解释
        return {"armRightRot": 46.0 * bump, "headRot": 5.0 * bump,
                "bodyRot": 2.5 * slow, "legLeftSquash": -0.03 * bump}
    if kind == 1:                                   # 挠头：他"零零散散"的招牌迟疑
        return {"armRightRot": 52.0 * bump, "headRot": 9.0 * bump,
                "headY": -9.0 * bump, "legLeftSquash": -0.05 * bump,
                "legRightSquash": 0.04 * bump}
    return {"armLeftRot": 34.0 * bump, "armRightRot": 34.0 * bump,
            "bodyScaleX": 0.03 * bump, "headRot": -4.0 * bump,
            "rootY": 4.0 * slow}


def _core_talk(t, b0, b1, b, i=0):
    """站定说话。相邻 talk 拍首尾相接，所以基调用**绝对时间**做周期：
    两侧各自求值结果相同，接缝天然连续（换成拍内比例就会"重置一次"）。
    只有邻拍不是 talk 时，才在首尾各 0.8 s 从静止进出。
    """
    m = _core_blank()
    br = _breathe(t, period=3.9, amp=0.022)
    w = _breathe(t, period=6.4, amp=1.0)
    m["bodyScaleX"] = 1.0 + br
    m["bodyScaleY"] = 1.0 - br
    m["rootY"] = br * 3.0
    m["headRot"] = 2.0 + 1.5 * w
    m["armLeftRot"] = 5.0 - 4.0 * w
    m["armRightRot"] = 5.0 + 4.0 * w
    m["legLeftSquash"] = 1.0 + 0.012 * w
    m["legRightSquash"] = 1.0 - 0.012 * w
    m["legLeftRot"] = 1.6 * w
    m["legRightRot"] = -1.6 * w
    m["rootRot"] = -1.2 * w
    k = int(math.floor(t / 7.2))
    gs, ge = k * 7.2 + 2.3, k * 7.2 + 4.0
    if gs >= b0 + 0.15 and ge <= b1 - 0.15:            # 整段放得下才做，跨拍会硬切
        for ch, v in _talk_gesture(k, t, gs, ge).items():
            m[ch] = m.get(ch, 0.0) + v
        m["headRot"] += _impulse(t, ge, 3.0, 4.0, 9.0, lag=0.0)
        m["armRightRot"] += _impulse(t, ge, 5.0, 4.0, 8.0, lag=0.0)
    ramp = 1.0
    if i <= 0 or BEATS[i - 1]["action"] != "talk":
        ramp = min(ramp, ease_in_out_sine(_rel(t, b0, b0 + 0.8)))
    if i >= len(BEATS) - 1 or BEATS[i + 1]["action"] != "talk":
        ramp = min(ramp, 1.0 - ease_in_out_sine(_rel(t, b1 - 0.8, b1)))
    return _blend_rest(m, ramp)


def _seg_val(t, times, eases, vals):
    """一条通道在一拍里的一生：节点值 + 每段缓动 → 连续曲线。

    为什么不用 if/elif 各写一段：相邻两段的端值对不齐就是一帧硬跳。
    实测踩过——蹬地末帧腿 1.14、落地首帧 0.80，一帧差 0.34，
    等于该通道全振幅的 100%（CheckMotion 直接判死）。
    节点插值从结构上排除这类错：每一段都从上一段的终值出发。
    """
    if t <= times[0]:
        return vals[0]
    for i in range(len(times) - 1):
        if t < times[i + 1]:
            return vals[i] + (vals[i + 1] - vals[i]) * eases[i](_rel(t, times[i],
                                                                     times[i + 1]))
    return vals[-1]


def _dunk_times(b):
    """暴扣的时间网格。编舞、走位、口型三处共用一份，别各算各的。"""
    span = b["end"] - b["start"]
    tc = _cue_time(b, "propHoop", "break") or (b["start"] + 0.62 * span)
    # 动作时长跟着台词给的时间走，不假设固定秒数：
    # 起势/挂筐/落地/回正各段按"这一拍还剩多少"整体缩放。
    # 不缩放时，扣篮被改到 40.9 起、拍只有 3.1 秒，回正跑到拍外，
    # 腿在 44.03 秒一帧跳 45%（CheckMotion 判硬跳变）。
    pre_need, post_need = 0.86 + 2 * M_ANTICIP, 0.30 + 0.34 + 1.05
    kp = min(1.0, max(0.25, (tc - b["start"]) / pre_need))
    kq = min(1.0, max(0.25, (b["end"] - tc) / post_need))
    a1 = tc - M_ANTICIP - 0.86 * kp     # 预备完成：压到底这一刻起跳
    a0 = a1 - M_ANTICIP * kp            # 起势：M_ANTICIP（220 ms）里慢慢沉下去
    hang = tc + 0.30 * kq               # 威亚上挂一下（他原话"其实是吊着威亚"）
    land = hang + 0.34 * kq             # 落回地面
    return tc, a0, a1, hang, land, min(b["end"] - 0.02, land + 1.05 * kq)


def _core_dunk(t, b0, b1, b):
    """暴扣：压 → 弹起 → 挂一下 → 落 → 吸收 → 回正。

    tc = "篮筐碎"那个记号的时刻：扣到 = 筐碎 = 同一帧。
    横向换边由 travel_offset() 在同一段时间里完成，落地时正好站在右侧。
    """
    m = _core_blank()
    tc, a0, a1, hang, land, rec = _dunk_times(b)
    T = (a0, a1, tc, hang, land, rec)
    E = (ease_in_quad, ease_out_quad, ease_in_out_sine, ease_in_quad,
         ease_in_out_sine)   # 末段用对称缓动：ease_out_cubic 首帧斜率是均速 3 倍，单帧就迈过 25% 振幅
    KEYS = {
        "rootY":          (0.0, 16.0, -92.0, -86.0, 12.0, 0.0),
        "bodyScaleX":     (1.0, 0.93, 0.98, 1.02, 1.0 + (M_OVERSHOOT - 1.0), 1.0),
        "bodyScaleY":     (1.0, 1.09, 1.04, 1.00, 0.86, 1.0),
        "bodyRot":        (0.0, -3.0, 6.0, 4.0, -2.0, 0.0),
        "rootRot":        (0.0, -2.5, 8.0, 5.0, -4.0, 0.0),
        "armLeftRot":     (0.0, -18.0, -55.0, -50.0, 8.0, 0.0),
        "armRightRot":    (0.0, -35.0, -88.0, -80.0, 18.0, 0.0),
        "headRot":        (0.0, -4.0, 6.0, 4.0, -3.0, 0.0),
        "legLeftSquash":  (1.0, 0.84, 1.14, 1.10, 1.0 - M_SQUASH * 0.9, 1.0),
        "legRightSquash": (1.0, 1.0 - M_SQUASH, 1.14, 1.10, 1.0 - M_SQUASH, 1.0),
        "legLeftRot":     (0.0, -5.0, 8.0, 10.0, 6.0, 0.0),
        "legRightRot":    (0.0, 5.0, 12.0, 14.0, 9.0, 0.0),
    }
    for ch, vals in KEYS.items():
        m[ch] = _seg_val(t, T, E, vals)
    # 事件余振（follow-through）：只在"碎筐"与"落回"两点施加，之后自然衰减到 0
    m["shellRot"] += _impulse(t, tc, 7.0, 5.0, 9.0)
    m["headRot"] += _impulse(t, hang, -5.0, 5.0, 10.0)
    m["armLeftRot"] += _impulse(t, land, 8.0, 4.0, 8.0)
    m["armRightRot"] += _impulse(t, land, -6.0, 4.0, 8.0, lag=M_PART * M_STAGGER)
    m["legLeftRot"] += _impulse(t, land, 3.0, 3.5, 7.0)
    m["legRightRot"] += _impulse(t, land, 4.0, 3.5, 7.0, lag=M_PART * M_STAGGER)
    return m
def _core_point(t, b0, b1, b):
    """指向电视机介绍 Aura：看到才抬手指过去、hold、偶尔点头强调、句尾收回。

    抬手时刻 = "水母出现"的记号；基调同样从静止进出，接缝不弹一下。
    """
    m = _core_blank()
    span = b1 - b0
    tj = _cue_time(b, "propJellyfish", "show") or (b0 + 0.25 * span)
    rel_end = b1 - 1.2
    br = _breathe(t, period=4.2, amp=0.02)
    m["bodyScaleX"] = 1.0 + br
    m["bodyScaleY"] = 1.0 - br
    m["rootY"] = br * 2.5
    m["rootRot"] = -1.5
    m["headRot"] = 5.0
    m["armLeftRot"] = 10.0
    hold = 0.0
    if tj + 0.7 <= t < rel_end:
        hold = 1.0
    elif tj <= t < tj + 0.7:
        hold = ease_out_back(_rel(t, tj, tj + 0.7))
    elif t >= rel_end:
        hold = 1.0 - ease_in_out_sine(_rel(t, rel_end, b1 - 0.02))
    m["armRightRot"] = 52.0 * hold                  # 教鞭式指向
    m["bodyRot"] = -2.0 * hold
    m["headRot"] += 6.0 * hold                      # 侧头看它
    m["legLeftSquash"] = 1.0 - 0.014 * hold
    m["legRightSquash"] = 1.0 + 0.014 * hold
    k = int(math.floor(t / 9.0))                    # 偶尔点头强调，绝对网格
    ns, ne = k * 9.0 + 3.4, k * 9.0 + 4.4
    if ns >= b0 + 0.2 and ne <= rel_end and hold > 0.5:
        bump = math.sin(_rel(t, ns, ne) * math.pi)
        m["headRot"] += 12.0 * bump
        m["headY"] -= 5.0 * bump
        m["legLeftSquash"] -= 0.03 * bump
        m["legRightSquash"] -= 0.03 * bump
        m["armRightRot"] += 6.0 * bump
        m["shellRot"] += _impulse(t, ne, 2.0, 4.5, 8.0)
    w = min(ease_in_out_sine(_rel(t, b0, b0 + 0.8)), 1.0)
    return _blend_rest(m, w)


def _core_outro(t, b0, b1, b):
    """谢幕：挥手 → 缩进壳只留眼睛 → 抬头鞠躬。最后一拍，收尾不必回静止。"""
    m = _core_blank()
    span = b1 - b0
    w_end, h_end = b0 + 0.36 * span, b0 + 0.66 * span
    if t < w_end:                                   # 挥手
        p = _rel(t, b0, w_end)
        wave = math.sin(p * math.pi)                # 首尾都是静止
        m["rootX"] = 18.0 * wave
        m["rootY"] = 5.0 * wave
        m["rootRot"] = -4.0 * wave
        m["bodyRot"] = 12.0 * wave
        m["headRot"] = -10.0 * wave
        m["headY"] = 3.0 * wave
        m["armLeftRot"] = 35.0 * wave
        m["armRightRot"] = (30.0 + 26.0 * math.sin(p * math.pi * 3.0)) * wave
        m["bodyScaleX"] = 1.0 + 0.05 * wave
        m["shellRot"] = 4.0 * wave
        m["legLeftSquash"] = 1.0 - 0.03 * wave
        m["legRightSquash"] = 1.0 + 0.03 * wave
    elif t < h_end:                                 # 缩壳：四肢收进去，只留眼睛
        p = ease_in_out_quad(_rel(t, w_end, h_end))
        q = math.sin(p * math.pi)
        m["bodyScaleX"] = 1.0 - 0.20 * p
        m["bodyScaleY"] = 1.0 - 0.20 * p
        m["rootY"] = 8.0 * p
        m["headY"] = -5.0 * p + 3.0 * q
        m["armLeftRot"] = 20.0 * p
        m["armRightRot"] = -80.0 * p
        m["legLeftSquash"] = 1.0 - 0.12 * p
        m["legRightSquash"] = 1.0 - 0.12 * p
        m["legLeftRot"] = 6.0 * p
        m["legRightRot"] = -6.0 * p
    else:                                           # 抬头鞠躬
        p = ease_in_out_sine(_rel(t, h_end, b1))
        bow = math.sin(p * math.pi) * 8.0
        m["bodyScaleX"] = 0.8 + 0.2 * p
        m["bodyScaleY"] = 0.8 + 0.2 * p
        m["rootY"] = 8.0 * (1.0 - p) + bow * 0.5
        m["headY"] = -5.0 + 10.0 * p
        m["armLeftRot"] = 20.0 + 10.0 * (1.0 - p * 0.4)
        m["armRightRot"] = -80.0 + 50.0 * (1.0 - p * 0.4)
        # 鞠躬拆给 root 与 body 两层：整具装配（含刚体龟壳）一起前倾，
        # 否则只有躯干弯、龟壳不跟着转，看着像断了腰
        m["bodyRot"] = bow * 0.45
        m["rootRot"] = bow * 0.55
        m["legLeftSquash"] = 0.88 + 0.12 * p - 0.05 * math.sin(p * math.pi)
        m["legRightSquash"] = m["legLeftSquash"]
        m["legLeftRot"] = 6.0 * (1.0 - p)
        m["legRightRot"] = -6.0 * (1.0 - p)
    return m


CORE_FN = {"entrance": _core_entrance, "talk": _core_talk, "dunk": _core_dunk,
           "point": _core_point, "outro": _core_outro}


def _core(t, i):
    """第 i 拍的核心姿态（不含口型）。做什么动作由这一拍的 action 决定。"""
    b = BEATS[i]
    fn = CORE_FN[b["action"]]
    b0, b1 = b["start"], b["end"]
    m = fn(t, b0, b1, b, i) if b["action"] == "talk" else fn(t, b0, b1, b)
    # 龟壳 riding 在根上：这里只给"瞬时值"，真正的拖拽由 _pose_raw 回读 t-LAG_SHELL。
    # 各动作函数不必再写 shellY = rootY * 0.9 —— 那是乘系数，不是滞后，
    # 实测 r=0.996 被 CheckMotion 判齐步走。
    m["shellX"] = m["rootX"]
    m["shellY"] = m["rootY"]
    return m


def _pose_raw(t, stage):
    """第 stage **拍**在时刻 t 的完整姿态：核心 + 按 lag_ladder 回读的滞后。

    滞后是"同一函数在 t-lag 处再求一次值"，不是乘系数 —— 部件之间才是真拖拽，
    通道相关系数才掉得下 0.95（CheckMotion 的齐步走门槛）。
    越出拍区间求值安全：动作一律自回静止，p 越界即基准值。
    """
    m0 = _core(t, stage)
    m = dict(m0)
    for ch, lag in LAGGED.items():
        if lag > 0.0:
            m[ch] = _core(t - lag, stage)[ch]
    m["mouthAperture"] = 3.0
    return m


# 接缝邻域（秒）：CheckMotion 的"边界幻影"在这段窗口里要求姿态单调过渡。
# 取 0.25 而不是 0.5：呼吸周期 3.9 s 的正弦在 ±0.5 s 窗口里天然会越过两端点连线
# 约 1 度，那不是幻影（旧缺陷实测 36.6 度），把慢呼吸误判成缺陷的门禁等于没有门禁。
SEAM_WIN = 0.25

# ---- 横向走位（画布像素，与 rig 局部通道分开）--------------------------------
WALK_IN = 1450         # 开场从画外左边走进来要走这么多像素（角色半宽约 360）


def travel_offset(t):
    """把 char_x 的变化落成一段**位移过程**，不是瞬移。

    rig 的 rootX 在 500 局部空间里，1 单位≈1 画布像素，撑不起 370 px 的换边；
    所以换站位走这一层，直接加在角色容器的画布坐标上。
    """
    i = _beat_index(t)
    b = BEATS[i]
    b0, b1 = b["start"], b["end"]
    span = b1 - b0
    prev_x = BEATS[i - 1]["char_x"] if i > 0 else WIDTH / 2.0
    dx = prev_x - b["char_x"]
    if b["action"] == "entrance":
        return -WALK_IN * (1.0 - ease_in_out_sine(_rel(t, b0 + 0.12 * span,
                                                       b0 + 0.78 * span)))
    if abs(dx) < 1.0:
        return 0.0
    if b["action"] == "dunk":
        tc, a0, a1, hang, land, rec = _dunk_times(b)
        return dx * (1.0 - ease_in_out_sine(_rel(t, a1, hang)))
    return dx              # 到不了这里：加载时已拦住"不会走路却换站位"

MOUTH_RAMP = 0.18        # 起落音时长。再短就会被 CheckMotion 的"单帧不超 25% 振幅"判回阶跃


def _speech_windows():
    """把逐条字幕合并成连续"说话段"。

    段表里存在首尾相接的字幕（如 14.7 s 处两条紧挨），若逐条包络，
    接缝处嘴会硬闭一下——正是 §20.2 那一类"表面没坏、节奏已死"的缺陷。
    """
    wins = []
    for s in sorted(SCRIPT_SUBTITLES, key=lambda x: x["start"]):
        a, b = s["start"], s["end"]
        if wins and a - wins[-1][1] <= 2.0 * MOUTH_RAMP:
            wins[-1][1] = max(wins[-1][1], b)
        else:
            wins.append([a, b])
    return wins


SPEECH_WINDOWS = _speech_windows()


def _mouth_aperture(t):
    """口型幅度：由字幕活动窗口（VO）驱动，说话时开合、静音时闭合（技术债 #2）。

    刻意独立于姿态通道：它既不该参与段边界交叉淡化，也不该被姿态包络压回闭合值。
    只在说话段的首尾做 ease_in_out_sine 起落音——此前是无包络硬阶跃，
    实测单帧从 3.0 弹到 8.5（全片最大跳变），看着像嘴被掰开。
    """
    for a, b in SPEECH_WINDOWS:
        if a <= t < b:
            # 线性而非 ease_in_out_sine：4 帧的短包络上，正弦缓动峰值斜率是均速的 π/2 倍，
            # 中间那一帧反而迈过 40% 振幅 —— 又被判回硬跳变。短包络就该用短直线。
            env = _clamp01(min(t - a, b - t) / MOUTH_RAMP)
            carrier = 8.5 + 2.0 * math.sin(2.0 * math.pi * t / 0.7)   # ~1.4 Hz 柔和说话
            return 3.0 + (carrier - 3.0) * env
    return 3.0                                                       # 闭合（happy 态）


# =============================================================================
# 3.4 状态机出口：t → 全部通道。纯函数、与渲染顺序无关（§8 第 1 条）。
#     拍与拍的接缝不需要交叉淡化：动作本身从静止起、回静止落（见 §3.3 三条约定），
#     talk 的基调还用绝对时间做周期，所以两侧求值结果相同。
#     旧实现在这里对相邻两段同时求值再 smoothstep —— 那是给"段段不回落基准"
#     打的补丁，现在没有那个病根了，补丁一并删掉。
# =============================================================================
BLINK_EVERY = 3.4        # 秒。人放松时 3~5 秒一次，取中间值
BLINK_LEN = 0.18         # 一次闭眼时长。再短，CheckMotion 的"眨发 3~12 帧"就拦不住


def _eye_squash(t):
    """眨眼。整片 116 秒零次眨眼是朋友说"小臭龟没怎么动"的一半原因 ——
    2D 里最便宜的生命信号就是眼睛会闭。用绝对时间做，跨拍接缝不会断。

    每第四次改成双眨（合—开—合—开），这是人刚说完一句长话时的自然节奏。
    """
    k = int(t // BLINK_EVERY)
    # 每第四次补一次双眨（刚说完一句长话的节奏）。第二下用全闭：
    # 先前用 0.75 的半闭，实测那一发只有 2 帧低于半开，看着像抽搐。
    for extra in ((0, 1.0), (0.34, 1.0)) if k % 4 == 3 else ((0, 1.0),):
        off, depth = extra
        p = _rel(t, k * BLINK_EVERY + off, k * BLINK_EVERY + off + BLINK_LEN)
        if 0.0 < p < 1.0:
            return 1.0 - depth * math.sin(p * math.pi)
    return 1.0


def compute_vibe_motion(t):
    m = _pose_raw(t, _beat_index(t))
    m["mouthAperture"] = _mouth_aperture(t)
    m["eyeSquash"] = _eye_squash(t)
    return m


def hand_canvas(t, side="armR"):
    """这一帧他的手掌在画布上哪个像素。

    以前道具挂在 `char_x + 210` 这种手写偏移上，等于我闭着眼睛猜手在哪 ——
    篮球四轮都糊在他脸上就是这个原因。现在把装配用的那串 transform 复合成
    矩阵（Rig.part_matrix），从资产实测的锚点直接算出来。
    注意：它反映的是部件**真的**转到了哪儿，不是我以为它转到了哪儿。
    """
    return _arm_frame(t, side)[0]


def _arm_frame(t, side="armR"):
    """(掌心画布坐标, 从肩指向掌的单位向量)。

    方向也要，是因为球比手大得多：球心不该落在掌面上，而该落在掌面**朝外**
    的延长线上。手臂举起来时球在手上头，手垂着时球在手外头 —— 同一条规则，
    不用给每个姿势另配一个偏移数字。
    """
    s = ACTIVE_PRESET["char_scale"]
    cx = _beat(t)["char_x"] + travel_offset(t)
    cy = ACTIVE_PRESET["char_center_y"]
    pose = compute_vibe_motion(t)
    spec = RIG.spec["parts"][side]
    # 支点在 rig.json 里是资产 viewBox 坐标，和锚点同一套坐标系
    sh = RIG.part_point(side, spec["pivot"], pose)
    hd = RIG.anchor(side, pose)
    ox, oy = cx - 200 * s + s * hd[0], cy - 200 * s + s * hd[1]
    sx, sy = cx - 200 * s + s * sh[0], cy - 200 * s + s * sh[1]
    n = math.hypot(ox - sx, oy - sy) or 1.0
    return (ox, oy), ((ox - sx) / n, (oy - sy) / n)
# =============================================================================
# 3.5 角色几何 —— 来自资产文件，不再内联
#     Docs/VibeMotionPlan.md §4 债 #5 的修复点：渲染器过去对 Assets/ 的引用数为 0，
#     "换一个 SVG 就换成片角色"物理上不可能。现在几何与支点全部由
#     Assets/Characters/Plobi/rig.json + 2D/Plobi.svg 声明，本文件只负责按通道施加变换。
#     验收点：改 Plobi.svg 的颜色/路径、不改一行本文件，成片角色随之改变。
# =============================================================================
RIG_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 os.pardir, "Assets", "Characters", "Plobi", "rig.json")
)
RIG = Rig(RIG_PATH)
for _w in RIG.warnings:
    print(f"[!] rig 审计：{_w}", flush=True)


def _mouth_state(t, m):
    """4 态口型机：说话幅度决定 talk/happy，剧情事件另给 shock/smug。

    以前按"第几段"分派，段表一改就 unpack 崩；现在按**这一拍在做什么**。
    """
    b = _beat(t)
    if b["action"] == "dunk":
        tc = _cue_time(b, "propHoop", "break") or \
            (b["start"] + 0.62 * (b["end"] - b["start"]))
        if tc - 1.30 <= t < tc + 1.10:              # 起跳瞪眼，落地才收
            return "shock"
    if b["action"] == "point":
        if t >= (_cue_time(b, "propJellyfish", "show") or b["start"]):
            return "smug"                           # 看到自己的软件，得意坏笑
    return "talk" if m["mouthAperture"] > 6.0 else "happy"


# =============================================================================
# 3.6 道具层：记号驱动，不再"第几段挂哪个道具"
#     他在一拍括号里点了几件道具就有几个记号；at 是该词在括号里的字位置，
#     所以"篮筐出现 → 扣进去 → 篮筐碎掉 → 电视出现"各自落在该落的那一帧。
#
#     位置按**他要的画面**给，不沿用资产里烤好的那个 transform（那是作画坐标）。
#     两套坐标各记一行，换算只在 _place() 里做一次；CheckAsset 回头核对
#     PROP_BAKED 是否还对得上内联进来的外层 transform。
# =============================================================================
PROP_BAKED = {                       # id: (外层 translate, 外层 scale, viewBox 宽高)
    "propHoop": (580.0, 0.0, 1.5, 400.0, 360.0),
    "propTv": (1040.0, 403.0, 1.7, 400.0, 340.0),
    "propBadges": (560.0, 920.0, 1.6, 450.0, 160.0),
    "propJellyfish": (144.0, 10.0, 1.6, 320.0, 360.0),
    "propSeam": (600.0, 90.0, 1.8, 400.0, 500.0),
    "propGuitar": (0.0, 0.0, 1.0, 260.0, 430.0),
    "propComic": (0.0, 0.0, 1.0, 300.0, 250.0),
    "propCelluloid": (0.0, 0.0, 1.0, 300.0, 210.0),
    "propPhone": (0.0, 0.0, 1.0, 220.0, 430.0),
    "propBall": (0.0, 0.0, 1.0, 120.0, 120.0),
}
# 手机要画在角色**之前**（压在他身前），不然"从壳里掏出来"会被他自己的身子挡住
PROP_FRONT = {"propPhone", "propBall"}
PHONE_SCREEN_LOCAL = (48.0, 44.0, 124.0, 342.0)   # Phone.svg 里 id=phoneScreen 那块
PROP_TARGET = {                      # 画布像素：这件东西该出现在哪
    "propHoop": (1090, -20, 1700, 540),     # 右侧、高过头，够得着但必须跳
    "propTv": (140, 403, 820, 981),         # "画面左侧地面上出现一台电视机"
    "propBadges": (170, 250, 890, 506),     # 谢幕三连：左上，不压字幕
    "propGuitar": (150, 300, 470, 748),     # 爱好墙：靠左立着
    "propComic": (168, 108, 520, 292),      # 爱好墙：钉在左上，会垂下来
    "propCelluloid": (520, 556, 862, 798),  # 爱好墙：地上一叠赛璐珞
    "propPhone": (1352, 452, 1584, 902),    # 身前偏右下：再往右挪，别压住他的脸和嘴
}
TV_SCREEN_LOCAL = (58.0, 78.0, 219.0, 194.0)   # 资产里 crtScreen 那块玻璃
# 可见墨迹的包围盒（viewBox 单位）。按 viewBox 摆会小一圈：
# 水母这张资产左右只占画布的 4.9%~97.7%、上下 15.5%~83.6%，
# 剩下的空白让"放进屏幕"算出来只有预期的一半大。数值是 fitz 光栅化后
# 取 alpha>20 的像素范围实测的，不是目测。
PROP_INK = {"propJellyfish": (15.7, 55.8, 312.6, 301.0)}
PROP_FADE = 0.45                                # 道具淡入（秒）
SHARD_T = 1.35                                  # 篮筐碎片飞散时长


def _phone_pull(prop, t, tc, box):
    """从壳里掏出手机：先藏在背后（小、偏后、略斜），1.1 秒里抬到手边。

    起点故意压在龟壳那一侧 —— 观众要看见"东西是从壳里出来的"，
    那枚壳顺手成了一个百宝袋，这是他认可的说法。
    """
    p = _clamp01((t - tc) / 1.1)
    e = ease_out_back(p) if p < 0.98 else 1.0
    x0, y0 = box[0] + 250.0, box[1] + 300.0        # 壳那一侧、偏下
    x = x0 + (box[0] - x0) * e
    y = y0 + (box[1] - y0) * e
    sc = 0.34 + 0.66 * e
    rot = -34.0 * (1.0 - e)
    return x, y, sc, rot


def _place(prop, target):
    """把"资产里烤好的作画位置"整体搬进 target 矩形 → 外层 translate/scale。

    只加一层外壳、不改资产内层那个 transform：内层是按 viewBox 标定的画法，
    外层才是"这件东西在画面里的位置"，两件事分开，重画道具不会带动站位。
    """
    bx, by, bs, vw, vh = PROP_BAKED[prop]
    tx0, ty0, tx1, ty1 = target
    s = min((tx1 - tx0) / (bs * vw), (ty1 - ty0) / (bs * vh))
    return tx0 - s * bx, ty0 - s * by, s


def _tv_scale():
    return _place("propTv", PROP_TARGET["propTv"])


def tv_screen_rect():
    """电视屏幕那块玻璃在**画布**上的位置，给屏幕里的内容用。"""
    sx, sy, ss = _tv_scale()
    bx, by, bs, _, _ = PROP_BAKED["propTv"]
    lx, ly, lw, lh = TV_SCREEN_LOCAL
    return (sx + ss * (bx + bs * lx), sy + ss * (by + bs * ly),
            ss * bs * lw, ss * bs * lh)


def _place_ink(prop, cx, cy, iw, ih):
    """把道具的**可见墨迹**（不是 viewBox）摆成 iw x ih，中心落在 (cx, cy)。"""
    bx, by, bs, vw, vh = PROP_BAKED[prop]
    x0, y0, x1, y1 = PROP_INK[prop]
    s = min(iw / (bs * (x1 - x0)), ih / (bs * (y1 - y0)))
    return (cx - s * (bx + bs * (x0 + x1) / 2.0),
            cy - s * (by + bs * (y0 + y1) / 2.0), s)


def _stage_box(prop):
    """道具落到画布上的外接框（碎裂的质心从这里算）。"""
    bx, by, bs, vw, vh = PROP_BAKED[prop]
    tx0, ty0, tx1, ty1 = PROP_TARGET[prop]
    s = min((tx1 - tx0) / (bs * vw), (ty1 - ty0) / (bs * vh))
    return (tx0, ty0, s * bs * vw, s * bs * vh)


def _shards(cx, cy, p):
    """篮筐碎掉：十二块篮板色的碎片飞散 + 落地。

    没有另画一套"碎筐"资产，所以用几何碎片表示。要换成真碎筐画法，
    得先有那张图 —— 这块先按现有色板顶住，已记进还不对清单。
    """
    out = []
    cols = ("#CD6B43", "#AC512C", "#CD6B43", "#FDEDD2")
    for k in range(12):
        a = (k / 12.0) * 2.0 * math.pi + 0.3
        sp = 260.0 + (k % 4) * 90.0
        x = cx + math.cos(a) * sp * p
        y = cy + (math.sin(a) * 210.0 - 130.0) * p + 900.0 * p * p      # 重力
        r = (13.0 + (k % 5) * 6.0) * (1.0 - 0.35 * p)
        rot = (k * 47.0 + 320.0 * p) % 360.0
        pts = "%.1f,%.1f %.1f,%.1f %.1f,%.1f %.1f,%.1f" % (
            x - r, y - r * 0.55, x + r * 0.8, y - r, x + r, y + r * 0.4,
            x - r * 0.3, y + r)
        out.append('<polygon points="%s" transform="rotate(%.1f, %.1f, %.1f)" '
                   'fill="%s" stroke="#241812" stroke-width="4" '
                   'stroke-linejoin="round" opacity="%.3f"/>'
                   % (pts, rot, x, y, cols[k % 4], max(0.0, 1.0 - p * p)))
    return "".join(out)


def _aura_icon():
    """Aura 应用图标：缩到屏幕尺寸后 base64 内嵌一次，全片复用。

    真资产在 Assets/Branding/AuraAppIcon.png（他给的鸿蒙图标）。
    读一次而不是每帧重开：1024x1024 直接内嵌会让每帧多 27 万字符。
    """
    global _AURA_ICON_B64
    if _AURA_ICON_B64 is None:
        path = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                             os.pardir, "Assets", "Branding",
                                             "AuraAppIcon.png"))
        if not os.path.exists(path):
            print("[!] 找不到 Aura 图标 %s，屏幕里只留水母" % path, flush=True)
            _AURA_ICON_B64 = ""
        else:
            im = Image.open(path).convert("RGBA").resize((256, 256), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            _AURA_ICON_B64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return _AURA_ICON_B64


_AURA_ICON_B64 = None


def _jellybeans(x, y, w, h, t):
    """屏幕里的小小糖豆人：三颗豆，一颗一颗把糖豆吞进去。

    他要"一个小小糖豆人一个一个吃糖豆的小动画"，没给现成资产，
    所以这是代码画的几何豆（项目同色系 + 黑描边），不是新美术。
    """
    out = []
    base = y + h * 0.78
    cols = ("#F4A6A0", "#9BD1A4", "#8FBFE8")
    hop = 0.62
    for k in range(3):
        ph = ((t / hop) + k * 0.37) % 1.0
        jy = base - 12.0 * math.sin(ph * math.pi)
        jx = x + w * (0.24 + 0.26 * k)
        rx, ry = w * 0.075, h * 0.085
        out.append('<ellipse cx="%.1f" cy="%.1f" rx="%.1f" ry="%.1f" fill="%s" '
                   'stroke="#241812" stroke-width="4"/>' % (jx, jy, rx, ry, cols[k]))
        for d in (-4.0, 4.0):
            out.append('<circle cx="%.1f" cy="%.1f" r="2.6" fill="#241812" '
                       'class="no-stroke"/>' % (jx + d, jy - ry * 0.35))
        eat = (t / hop) % 3.0
        if int(eat) == k:
            q = eat - int(eat)
            out.append('<circle cx="%.1f" cy="%.1f" r="4.5" fill="#FDEDD2" '
                       'stroke="#241812" stroke-width="2.5"/>'
                       % (jx, base - h * 0.42 + h * 0.36 * ease_in_quad(q)))
            if q > 0.90:
                out.append('<path d="M %.1f %.1f q %.1f %.1f %.1f 0" fill="none" '
                           'stroke="#241812" stroke-width="3" stroke-linecap="round"/>'
                           % (jx - 5, jy + 3, 5, 4, 10))
    return "".join(out)


def _vector_wriggle(x, y, w, h, t):
    """几条正弦线在屏幕里蠕动 —— 他那句"目前可能只是一些不着边际的矢量线条
    在屏幕上蠕动"说的就是这部片子本身，所以让电视里真的蠕给他看。"""
    out = []
    cols = ("#4EEDA4", "#8FBFE8", "#F4A6A0", "#FDEDD2")
    for k in range(4):
        pts = []
        for i in range(15):
            u = i / 14.0
            px = x + 20 + u * (w - 40)
            py = y + h * (0.20 + 0.19 * k) + math.sin(u * 5.4 + t * 1.9 + k * 1.3) * (9 + 3 * k)
            pts.append("%.0f,%.0f" % (px, py))
        out.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="4" '
                   'stroke-linecap="round" opacity="0.85"/>' % (" ".join(pts), cols[k]))
    return "".join(out)


def _niulai_shot():
    """《牛来》那一格：他 2026-09-22 自己给的剧照，直接读原件、不复制一份进仓库。

    原稿写的是"电视机留白，到时候我会把牛来地片段剪进去"，所以这里以前只闪一个
    开机光标。现在他把图给了，就换成真图。源文件路径来自本集档案的 stills 表
    （`Episodes/<EP>/episode.json`），读的是原件、不在仓库里另存副本。
    读不到就退回光标，不报错 —— 少一张图不该让整片渲不出来。
    """
    global _NIAULAI_B64
    if _NIAULAI_B64 is None:
        path = STILL_IMAGES.get("niulai", "")
        if not os.path.exists(path):
            print("[!] 没有《牛来》剧照 %s，那一格退回留白" % path, flush=True)
            _NIAULAI_B64 = ""
        else:
            im = Image.open(path).convert("RGB")
            # 屏幕那块玻璃只有 372×330，缩到两倍够用，base64 少一半字符
            im.thumbnail((760, 680), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            _NIAULAI_B64 = base64.b64encode(buf.getvalue()).decode("ascii"), im.size
    return _NIAULAI_B64


_NIAULAI_B64 = None


def _screen_content(t, b, rect):
    """电视机屏幕里的画面。内容一律画在玻璃矩形**内部**：
    PyMuPDF 不执行 clip-path，靠"画得下"代替"裁得下"（§26）。
    """
    x, y, w, h = rect
    kind = b["screen"]
    out = ['<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="16" '
           'fill="#1B1410" class="no-stroke"/>' % (x, y, w, h)]
    cx, cy = x + w / 2.0, y + h / 2.0
    if kind == "niulai":
        shot = _niulai_shot()
        if shot:
            pic, (iw, ih) = shot
            # contain 而不是 cover：这张是正脸特写，裁掉任何一边都会把角裁进脸里。
            # 4:3 的图放进 1.13:1 的玻璃里，是上下留黑边，不是左右。
            s = min(w / iw, h / ih)
            dw, dh = iw * s, ih * s
            out.append('<image href="data:image/png;base64,%s" x="%.1f" y="%.1f" '
                       'width="%.1f" height="%.1f"/>' % (pic, cx - dw / 2.0,
                                                         cy - dh / 2.0, dw, dh))
        else:
            bl = 1.0 if int(t * 1.4) % 2 == 0 else 0.25
            out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" '
                       'fill="#4EEDA4" opacity="%.2f" class="no-stroke"/>'
                       % (x + 0.10 * w, cy - 0.03 * h, 0.05 * w, 0.06 * h, bl))
    elif kind == "vector":
        out.append(_vector_wriggle(x, y, w, h, t))
    elif kind == "jellybean":
        out.append(_jellybeans(x, y, w, h, t))
    elif kind == "aura":
        icon = _aura_icon()
        tj = _cue_time(b, "propJellyfish", "show") or b["start"]
        tu = _cue_time(b, "propAuraUi", "show") or b["start"]
        rise = ease_out_cubic(_clamp01((t - tj) / 0.9))
        grow = ease_out_back(_clamp01((t - tu) / 0.7))
        # Aura 本尊从屏幕底下升上来，图标在她右边"点开"
        if rise > 0.02:
            jx, jy, jw, jh = w * 0.40, h * 0.50, w * 0.34, h * 0.46
            ax, ay, aS = _place_ink("propJellyfish", x + w * 0.28,
                                    y + h * 0.66 + jh * (1.0 - rise), jw, jh)
            out.append('<g opacity="%.3f" transform="translate(%.2f,%.2f) '
                       'scale(%.4f)">%s</g>' % (rise, ax, ay, aS,
                                                 PROP_SVGS["propJellyfish"]))
        if icon and grow > 0.02:
            # 图标本身也是只水母，和 Aura 并排放会被读成"屏幕里两只水母"。
            # 给它一张 App 卡片底：一看就是"软件界面"，不是另一只宠物。
            side = min(w * 0.40, h * 0.48) * grow
            ix, iy = x + w * 0.60, y + h * 0.66 - side / 2.0
            pad = side * 0.12
            out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="%.1f" '
                       'fill="#FDEDD2" stroke="#241812" stroke-width="5"/>'
                       % (ix - pad, iy - pad, side + 2 * pad, side + 2 * pad, pad * 1.6))
            out.append('<image href="data:image/png;base64,%s" x="%.1f" y="%.1f" '
                       'width="%.1f" height="%.1f"/>' % (icon, ix, iy, side, side))
    return "".join(out)


def _cues_abs(b):
    """这一拍的记号换算成绝对秒数（与 _cue_at 同一套算式，别各写一份）。"""
    return [(c["id"], c["kind"], _cue_at(b, c)) for c in b["cues"]]


def _phone_shot():
    """手机屏幕里的气韵截图：真资产，从 Assets/Branding 读一次、缩到屏幕尺寸。

    截图不画在 Phone.svg 里：那是位图，画进 SVG 只能内联 base64，
    每帧多几万个字符。和 Aura 图标同一个办法。

    用哪一张是**我替他挑的**：情绪热力图那张最能一眼看出"这软件在记录情绪"，
    设置页那张只能看出"这是个设置页"。换图只改上面那个文件名。
    """
    global _PHONE_SHOT_B64
    if _PHONE_SHOT_B64 is None:
        path = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                             os.pardir, "Assets", "Branding",
                                             "AuraScreenshotMood.png"))
        if not os.path.exists(path):
            print("[!] 没有气韵截图 %s，手机屏幕留黑" % path, flush=True)
            _PHONE_SHOT_B64 = ""
        else:
            im = Image.open(path).convert("RGBA")
            im.thumbnail((260, 720), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            _PHONE_SHOT_B64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return _PHONE_SHOT_B64


_PHONE_SHOT_B64 = None


def prop_layer(t, front=None):
    """这一帧该出现哪些道具、淡入到几、碎没碎。

    记号是**粘性**的：propTv 一旦出现就一直挂着，没有别的记号说它该走它就不走。
    这样"地面出现一台电视机"之后，电视不会在下一拍凭空消失。
    """
    on = {}                                   # id -> ("show", 首次时刻)
    broken = {}                               # id -> 碎掉时刻（有则到点演碎裂）
    tilted = {}                               # id -> 该垂下来的时刻（漫画稿）
    life = {}                                 # id -> 该收掉的时刻（口播道具）
    for b in BEATS:
        for c in b["cues"]:
            pid = c["id"]
            if c.get("from") != "口播":
                continue                      # 他括号里写的装置不回收
            life[pid] = max(life.get(pid, 0.0), b["end"] + 0.45)
        for pid, kind, tc in _cues_abs(b):
            if kind == "tilt":
                tilted.setdefault(pid, tc)
                continue
            if pid == "propAuraUi" or (pid == "propJellyfish"):
                on.setdefault("screen:" + pid, (kind, tc))
                continue
            if kind == "break":
                # 碎掉不能顶掉"出现"：先前两者共用一个键，结果篮筐直到碎的那一帧
                # 才第一次画出来 —— 观众看到的是一颗星凭空炸开，筐从来没挂在那儿过。
                on.setdefault(pid, ("show", tc))
                broken[pid] = tc
            elif pid in PROP_TARGET:
                on.setdefault(pid, ("show", tc))
    html = []
    for pid, (kind, tc) in on.items():
        if tc > t or pid.startswith("screen:"):
            continue                          # screen: 两件画在电视玻璃里，不上舞台
        if (pid in PROP_FRONT) != bool(front):
            continue                          # 手机压在人前，别的都在人后
        if pid == "propBall":
            continue                          # 球不在这里画，见下面
        if t >= life.get(pid, 1e9):
            continue                          # 说完就收：整片挂着一墙道具是噪音
        if pid in broken and t >= broken[pid]:
            p = _clamp01((t - broken[pid]) / SHARD_T)
            if p >= 1.0:
                continue
            x, y, w, h = _stage_box(pid)
            ax, ay, aS = _place(pid, PROP_TARGET[pid])
            _ = kind
            # 篮板先被砸歪再消失：0~12% 全形、12~40% 淡出，碎片继续飞
            html.append('<g opacity="%.3f" transform="translate(%.2f,%.2f) '
                        'scale(%.4f) rotate(%.1f, %.1f, %.1f)">%s</g>'
                        % (max(0.0, 1.0 - _clamp01((p - 0.12) / 0.28)),
                           ax, ay, aS, 9.0 * _clamp01(p / 0.25),
                           _stage_box(pid)[0] + _stage_box(pid)[2] * 0.5,
                           _stage_box(pid)[1] + _stage_box(pid)[3] * 0.5,
                           PROP_SVGS[pid]))
            html.append(_shards(x + w * 0.5, y + h * 0.5, p))
        else:
            ax, ay, aS = _place(pid, PROP_TARGET[pid])
            extra, shot = "", ""
            if pid == "propPhone":
                # 掏出来：从壳那一侧抬到手边，1.1 秒
                px, py, pSc, pRot = _phone_pull(pid, t, tc, (ax, ay, aS))
                ax, ay, aS, extra = px, py, pSc, ' rotate(%.1f, %.0f, %.0f)' % (
                    pRot, ax + 110 * aS, ay + 215 * aS)
                pic = _phone_shot()
                if pic and _clamp01((t - tc) / 1.1) > 0.55:
                    # 局部坐标！这一层外面已经有 translate+scale，
                    # 再塞画布坐标等于被缩放两次，实测截图飞出画面、屏幕全黑。
                    lx, ly, lw, lh = PHONE_SCREEN_LOCAL
                    shot = ('<image href="data:image/png;base64,%s" x="%.1f" y="%.1f" '
                            'width="%.1f" height="%.1f"/>' % (pic, lx, ly, lw, lh * 0.99))
            elif pid in tilted and t >= tilted[pid]:
                # 漫画稿垂下来：绕左上那枚图钉转，说"学业磨平了热爱"那一刻
                p = ease_in_out_sine(_clamp01((t - tilted[pid]) / 1.6))
                bx, by, bw, bh = _stage_box(pid)
                extra = ' rotate(%.1f, %.0f, %.0f)' % (-6.0 + 84.0 * p, bx + 8, by + 8)
            fade_out = 1.0
            if pid in life:
                fade_out = _clamp01((life[pid] - t) / PROP_FADE)
            html.append('<g opacity="%.3f" transform="translate(%.2f,%.2f) scale(%.4f)%s">'
                        '%s%s</g>' % (min(_clamp01((t - tc) / PROP_FADE), fade_out),
                                       ax, ay, aS, extra, PROP_SVGS[pid], shot))
    tv = on.get("propTv")
    if tv and tv[1] <= t and not (tv[0] == "break" and _clamp01((t - tv[1]) / SHARD_T) >= 1.0):
        html.append(_screen_content(t, _beat(t), tv_screen_rect()))
    # 篮球跟着"扣篮"这个动作走，不靠段表记号：它没有"他点名要一台篮球"这回事，
    # 是扣篮这个动作自带的道具。先前把它当普通道具遍历，段表里没它的记号，
    # 结果整条球路一帧都没画出来。
    # 顺序也重要：必须排在电视屏幕内容**之后**。先前排在前面，球滚到左边时
    # 被电视那块不透明的屏幕整个盖住 —— 实测 44.50 秒该有球的一帧，
    # 拿"有球/无球"两张图逐像素相减，差异像素为 0，等于球凭空消失了半秒。
    # 落点放在筐圈圆心往右 80 像素：圆心那一点正好在他头顶上，
    # 压到圆心就成了"球糊在他脸上"（实测 42.90 秒球心和他左眼上下齐平）。
    # 也试过让下落的球躲到人后面去，结果 43.0~43.4 秒球整整消失 10 帧，
    # 读起来像球被变没了 —— 所以改成把落点挪出他的头，球全程可见。
    db = _dunk_beat(t)
    if "propBall" in PROP_SVGS and bool(front) != _ball_behind(t, db):
        st = _ball_state(t, db)
        if st:
            bx, by, brot, bsc = st
            html.append('<g transform="translate(%.1f,%.1f) rotate(%.1f) scale(%.3f)">%s</g>'
                        % (bx, by, brot, bsc, PROP_SVGS["propBall"]))
    return "".join(html)


def _dress_time(b):
    """开场"站定 → 穿好"窗口。与 _core_entrance 用同一个算式，别各写一份。"""
    span = b["end"] - b["start"]
    w1 = b["start"] + 0.78 * span
    return w1, w1 + 3.3 * M_PART


# =============================================================================
# 3.7 特效层：漫画式符号，不画新角色、不加新剧情
#     只由他写过的指示驱动（"夸张地向右飞起来…扣进篮筐…篮筐碎掉"→
#     速度线 / 撞击星 / 落地尘土）。说到乐器飘音符那种要从**口播词**里推，
#     属于 AI 推断，没做；要做就得像三连图标那样标出来让他一眼能删。
#     为什么值得单独一层：这是"夸张"里最便宜的一档 —— 不动 rig、不动资产，
#     在任何尺寸下都读得出来，而且观众对这套符号的识别是即时的。
# =============================================================================
FX_INK = "#241812"
FX_HOT = "#F4A6A0"
FX_LINE = "#FDEDD2"


def _speed_lines(t, b, cx, cy):
    """起跳到挂筐之间：身后拖一束速度线，方向与飞行相反。"""
    tc, a0, a1, hang, land, rec = _dunk_times(b)
    p = _rel(t, a1, hang)
    if not 0.0 < p < 1.0:
        return ""
    grow = math.sin(p * math.pi)
    # 描边不能用深色：舞台是 #1a2030，暗线画上去等于没画（第一版实测几乎看不见）
    out = []
    for k in range(7):
        y = cy - 150 + k * 52
        ln = 90 + (k % 3) * 55
        x0 = cx - 120 - ln - (1.0 - p) * 60
        out.append('<line x1="%.0f" y1="%.0f" x2="%.0f" y2="%.0f" stroke="%s" '
                   'stroke-width="%.0f" stroke-linecap="round" opacity="%.2f"/>'
                   % (x0, y, x0 + ln * grow, y, FX_LINE, 7 + (k % 2) * 4, 0.5 * grow))
    return "".join(out)


def _impact_star(t, b, cx, cy):
    """触筐那一瞬：一颗多角撞击星 + 一圈冲击环，0.45 秒内收掉。

    中心取**篮筐筐圈**而不是角色中心 —— 第一版按角色坐标偏移，
    结果那颗星正正砸在他脸上，看着像被打了一拳。
    """
    tc = _cue_time(b, "propHoop", "break")
    if tc is None:
        return ""
    p = _rel(t, tc, tc + 0.45)
    if not 0.0 < p < 1.0:
        return ""
    grow = 1.0 - ease_in_quad(p)
    pts = []
    for k in range(12):
        a = k * math.pi / 6.0
        r = (78 if k % 2 == 0 else 34) * (0.5 + 0.9 * ease_out_back(p))
        pts.append("%.0f,%.0f" % (cx + r * math.cos(a), cy + r * math.sin(a)))
    return ('<polygon points="%s" fill="%s" stroke="%s" stroke-width="6" '
            'stroke-linejoin="round" opacity="%.2f"/>'
            '<circle cx="%.0f" cy="%.0f" r="%.0f" fill="none" stroke="%s" '
            'stroke-width="5" opacity="%.2f"/>'
            % (" ".join(pts), FX_HOT, FX_INK, grow,
               cx, cy, 60 + 150 * p, FX_INK, 0.5 * grow))


def _dust(t, b, cx, ground_y):
    """落地：四团尘土向两边弹开再落下。"""
    tc, a0, a1, hang, land, rec = _dunk_times(b)
    p = _rel(t, land, land + 0.55)
    if not 0.0 < p < 1.0:
        return ""
    out = []
    for k in range(5):
        side = -1.0 if k % 2 else 1.0
        r = 16 + (k % 3) * 9
        x = cx + side * (40 + 150 * p + k * 12)
        y = ground_y - r - math.sin(min(1.0, p * 1.6) * math.pi) * (26 + k * 8)
        out.append('<circle cx="%.0f" cy="%.0f" r="%.0f" fill="#3a4257" '
                   'opacity="%.2f"/>' % (x, y, r * (1.0 - 0.4 * p), 0.75 * (1.0 - p)))
    return "".join(out)


def rigging_hook(t, cx):
    """顶上那根威亚。他原话"其实是吊着威亚"——那就把威亚画出来。

    三个时刻：开场空钩子晃（顺便回答"开场画面为什么是空的"）；
    扣篮挂筐时一条线真的吊在他背上（这是笑点的兑现）；谢幕再晃一下。
    """
    b = _beat(t)
    hx = 1010
    swing = math.sin(t * 1.15) * 16
    top = '<g stroke="#FDEDD2" stroke-width="4" fill="none" opacity="0.7">'
    hook = ('<line x1="%d" y1="0" x2="%.0f" y2="66"/><circle cx="%.0f" cy="78" r="12" '
            'stroke-width="5"/>' % (hx, hx + swing * 0.35, hx + swing * 0.35))
    if b["action"] == "dunk":
        tc, a0, a1, hang, land, rec = _dunk_times(b)
        p = _rel(t, a1, land)
        if p > 0.02:
            ax, ay, aw, ah = _stage_box("propHoop")
            return (top + '<line x1="%.0f" y1="0" x2="%.0f" y2="%.0f"/>'
                    '<line x1="%d" y1="0" x2="%.0f" y2="66"/><circle cx="%.0f" cy="78" '
                    'r="12" stroke-width="5"/></g>'
                    % (ax + aw * 0.5, ax + aw * 0.5, ah * 0.1, hx, hx + swing * 0.35,
                       hx + swing * 0.35))
        return ""
    if b["action"] in ("entrance", "outro"):
        return top + hook + "</g>"
    return ""


# ---- 篮球：一条按真实参数算的弹道 -------------------------------------------
# 为什么不是随手写几个 sin：他要"尽量符合物理规律"。自由落体、每次弹起
# 按恢复系数衰减、滚动角速度 = 水平速度 / 半径，三个都是算出来的。
BALL_G = 2600.0          # px/s²。由本片的尺度反推：角色高约 450px ≈ 1.6 m
BALL_E = 0.45            # 恢复系数。真实篮球对硬地约 0.75，但它是**先穿过篮网**
                         # 再落地的：网和筐圈吃掉一大半能量。取 0.75 时实测
                         # 43.90 秒球弹到他胸口高度又横穿过去，读起来像他又抱上了；
                         # 0.45 之后第一次弹起点只到他腰，横穿时已经在滚了。
BALL_R = 52.0            # 球半径（画布像素），直径约为他身高的 0.23
BALL_VX = -520.0         # 扣完之后往左滚的水平速度 px/s。取这个数是为了
                         # "弹几下再从左侧离场"：1447px ÷ 520 ≈ 2.8 s，
                         # 正好落在弹跳还没停的区间里（再快就只弹一次就没了）
BALL_PUSH = 120.0        # 过筐下落这段的随势右偏 px/s（球从筐的另一侧掉出来）
BALL_SCALE = BALL_R / 36.0   # 资产里球半径 36 单位
PASS_LEAD = 0.70              # 传球段：拍开始前这么久就从画外飞进来
BALL_LIFE = 4.20              # 出筐后还要在画面里待多久（弹跳 + 滚出左侧）。
                              # 恢复系数降到 0.45 后弹跳更快停、剩下是贴地滚，
                              # 1485px ÷ 520 ≈ 2.9 s 落地 + 滚出，留一点余量
BALL_GRIP = 40.0              # 球心离掌面多远（沿肩→掌的延长线）。约 0.78 个球半径：
                              # 球的内缘还压在掌上，看着才是"握住"而不是"贴着"


def _dunk_beat(t):
    """t 时刻该不该有球、以及是哪一拍扣的篮。

    球的路径比"扣篮"这一拍长：他要求"扣篮之前有个篮球先从左边飞出来"，
    又要"扣完之后弹跳几次从左侧离场"，两头都超出这一拍。先前只在拍内画球，
    实测球在 40.90 凭空出现在 x=150（已经在画面里），又在 44.01 弹到一半
    （x=861，舞台正中间）直接消失。所以这里按球自己的时间窗找拍，不按拍找球。
    """
    for b in BEATS:
        if b["action"] != "dunk":
            continue
        tc, a0, a1, hang, land, rec = _dunk_times(b)
        if a0 - PASS_LEAD <= t < tc + BALL_LIFE:
            return b
    return None


def _bounce_y(t0, h0, t):
    """从 t0 起、以高度 h0 落向地面之后的一串衰减弹跳，返回离地高度（>=0）。"""
    if t < t0:
        return None
    T = t - t0
    # 第一段下落：h = h0 - g T²/2，落地时刻 sqrt(2 h0 / g)
    tfall = math.sqrt(2.0 * max(1.0, h0) / BALL_G)
    if T <= tfall:
        return max(0.0, h0 - 0.5 * BALL_G * T * T)
    T -= tfall
    h = h0 * BALL_E * BALL_E          # 第一次弹起的顶点高度 = e² h0
    while h > 6.0:
        period = 2.0 * math.sqrt(2.0 * h / BALL_G)
        if T <= period:
            # 抛物线：顶点在 period/2
            u = (T - period / 2.0) / (period / 2.0)
            return max(0.0, h * (1.0 - u * u))
        T -= period
        h *= BALL_E * BALL_E
    return 0.0


def _grip_point(t, side="armR"):
    """他这只手"抱着"球时，球心该落在画布哪个像素。

    球比手大得多，球心不该压在掌面上，而在"肩→掌"这条线的延长线上：
    手臂举起来球就在手上方，手垂着球就在手外侧，一条规则管所有姿势。
    以前是每个阶段手写一个偏移数字（cx+150、cx+210、筐心右 80…），
    那些数字谁也不代表，所以球才会一会儿糊在脸上、一会儿飘到别处。
    """
    (px, py), (ux, uy) = _arm_frame(t, side)
    return (px + ux * BALL_GRIP, py + uy * BALL_GRIP)


def _ball_geom(b):
    """球路用到的那几个量，只算一次。

    单独拎出来是因为"球在哪"和"什么时候落地"必须同一个来源；两处各算一份
    的话改一处就悄悄对不上（这个项目已经栽过几次）。
    """
    tc, a0, a1, hang, land, rec = _dunk_times(b)
    ground = ACTIVE_PRESET["char_center_y"] + 155 * ACTIVE_PRESET["char_scale"]
    rest = ground - BALL_R                 # 贴地时球心：在地面**上方**一个半径
    sx, sy = _grip_point(tc)               # 离手那一刻的位置，就是手到的地方
    tfall = math.sqrt(2.0 * max(40.0, rest - sy) / BALL_G)
    return dict(tc=tc, a0=a0, a1=a1, rest=rest, start=(sx, sy), tfall=tfall)


def _ball_behind(t, b):
    """球过筐之后往下掉那一段，画在角色后面。

    他站在筐的正前方（筐挂墙上，char_x 1330 vs 筐心 1395），所以球从筐里
    掉出来那一段本来就在人身后。实测 43.30 秒球落在他肚子前面，读起来像
    他又有球了 —— 那一球已经出手了。
    只遮"过筐之后"：按进筐那 0.16 秒是钱镜头，藏起来等于没扣篮。
    """
    if b is None or b["action"] != "dunk":
        return False
    g = _ball_geom(b)
    return g["tc"] + 0.16 <= t < g["tc"] + g["tfall"]


def _ball_state(t, b):
    """返回这一帧篮球的 (x, y, 角度, 缩放)，没有球就返回 None。

    三段：从画外左边被传进来接到手里 → 挂在手上跟着手臂走 → 离手直落、
    弹几下、往左滚出画面。

    中段没有任何自己的算式，位置完全等于 `_grip_point(t)`。这是这轮真正的修复：
    先前球走的是另写的一条弧线，手臂走的是另一条，两条谁也不知道对方在哪 ——
    所以球"跟着手"只是我这么说，量出来掌心在筐圈下方 189 像素，
    也就是他根本没够到筐，球是被一条公式抬上去的。
    """
    if b is None or b["action"] != "dunk":
        return None
    g = _ball_geom(b)
    tc, a0, rest = g["tc"], g["a0"], g["rest"]
    enter = a0 - PASS_LEAD
    # 传球是**回旋**（backspin），不是贴地前滚：按真实接触滚动算的话
    # 1700 px/s ÷ 52 px ≈ 5.2 转/秒，30 帧下会混叠成"乱飘"——正是他要修的那个毛病。
    BALL_PASS_SPIN = -190.0     # deg/s，负号 = 向右飞时逆时针回旋
    rot_catch = BALL_PASS_SPIN * max(0.0, a0 - enter)
    if t < enter or t >= tc + BALL_LIFE:
        return None
    if t < a0:                                   # ① 传球进来：贴地进来的低平球
        p = _rel(t, enter, a0)
        gx, gy = _grip_point(t)
        x = -90.0 + (gx + 90.0) * p
        # 起点压在地面附近：先前从 y=640 抛进来，弧线半途正好穿过他的脸
        # （实测 41.33 秒球心离右眼 21 像素，球半径 52）。
        y = 760.0 + (gy - 760.0) * p - 60.0 * math.sin(p * math.pi)
        rot = BALL_PASS_SPIN * (t - enter)      # 回旋：每秒不到一转，逐帧读得出转向
        return x, y, rot, BALL_SCALE
    if t < tc:                                   # ② 挂在手上
        # 接住之后球不再"滚"，但角度要接着传球那一刻的值，否则会在手里
        # 啪地转一下（先前这里另写了一个式子，实测跳了 220 度）
        gx, gy = _grip_point(t)
        return gx, gy, rot_catch, BALL_SCALE
    # ③ 出筐：从手里那个位置直落，落地之后才往左滚
    T = t - tc
    tfall = g["tfall"]
    sx, sy = g["start"]
    # 两个坑都踩过：落差写成负数被夹成 40 像素，球不弹、贴地滑行；
    # 贴地位置写成 ground + BALL_R 等于把球整个埋进地板底下。
    y = rest - (_bounce_y(tc, max(40.0, rest - sy), t) or 0.0)
    # 下落这段球往**右**偏一点：他从筐的左侧把球按进去，球穿过篮筐落到
    # 筐的另一侧，这是扣篮的随势方向。先前给的是向左 12%，结果球落在他
    # 脚边又弹起来挡在肚子前面（43.50 秒），读起来像他又有球了 —— 那一球已出手。
    Tr = max(0.0, T - tfall)
    x = sx + BALL_PUSH * min(T, tfall) + BALL_VX * Tr
    # 落地之后才真的"滚"：ω = v / r，方向与位移一致（向左 = 角度递减）
    rot = rot_catch + BALL_VX * Tr / BALL_R * 57.3
    return x, y, rot, BALL_SCALE


def fx_layer(t, cx, cy, ground_y, front=True):
    """front=False 画在角色之前（速度线在身后），True 画在之后（撞击星压在人上）。"""
    b = _beat(t)
    if b["action"] != "dunk":
        return ""
    if not front:
        return _speed_lines(t, b, cx, cy)
    hx, hy, hw, hh = _stage_box("propHoop")
    return _impact_star(t, b, hx + hw * 0.5, hy + hh * 0.58) + _dust(t, b, cx, ground_y)


def loose_shell_svg(t):
    """开场那枚被丢出来的壳：飞进来 → 落地晃一下停住 → 等他走过来穿上。

    独立容器绘制，不跟角色走 —— 角色此时还在从画外走进来的路上。
    穿好的瞬间交回给装配里的刚体壳（build_plobi_character 把 shell 件摘掉），
    两枚壳在同一位置接力。
    """
    b = BEATS[0]
    if b["action"] != "entrance" or t > _dress_time(b)[1] + 1e-6:
        return ""
    land = b["start"] + 0.30 * (b["end"] - b["start"])
    w0, w1 = _dress_time(b)
    scale = ACTIVE_PRESET["char_scale"]
    cx = b["char_x"]
    tx = cx - 200 * scale
    ty = ACTIVE_PRESET["char_center_y"] - 200 * scale
    if t < land:                                # 还在空中：从左上方抛进来
        p = _clamp01(t / max(1e-6, land))
        x = 60.0 + 140.0 * p
        y = -60.0 + 288.0 * ease_in_quad(p)
        rot = -260.0 * p
    else:
        q = _clamp01((t - land) / 0.62)
        # "晃了一下，停住了"：一次摆动只做到 M_HEST（60%）就收，不做完整来回
        rot = 20.0 * M_HEST * math.sin(q * math.pi) * math.exp(-2.2 * (t - land))
        drop = 26.0 * (1.0 - ease_out_cubic(_clamp01((t - land) / 0.5)))
        x, y = 200.0, 227.5 + drop
        if t >= w0:                             # 抬手到穿上：壳抬到背上
            k = ease_in_out_sine(_rel(t, w0, w1))
            y = 227.5 + drop * (1.0 - k)
            rot *= (1.0 - k)
    # 变换顺序：先把壳的锚点 (200,227) 挪到原点、转、再搬到 (x,y)。
    # rotate 一定不能再带圆心：写了 rotate(a,200,227) 等于把已经归零的点
    # 又绕 (200,227) 转一遍，实测开场那枚壳被甩到画面底部。
    return ('<g transform="translate(%.2f, %.2f) scale(%.2f)"><g transform="'
            'translate(%.1f, %.1f) rotate(%.2f) translate(-200, -227)">%s'
            '</g></g>' % (tx, ty, scale, x, y, rot, RIG.part_svg("shell")))


def build_plobi_character(m, t):
    """按 rig.json 契约把资产装配成一帧角色。几何不在本文件里。

    口型状态由 _mouth_state(t, m) 选出，Rig 侧一次只注入一个状态组 ——
    资产里那套 CSS display:none 切换在 PyMuPDF 下完全无效（§26），
    所以可见性必须由装配层显式解析，不能指望样式表。
    """
    skip = ()
    b = _beat(t)
    if b["action"] == "entrance" and t < _dress_time(b)[1]:
        skip = ("shell",)                # 壳还在地上（或还在空中），等下才穿
    return RIG.assemble(m, _mouth_state(t, m), skip=skip)

# =============================================================================
# 4. 生成 SVG 画面（支持 Release 纯净成片与 Debug 调试双模式）
# =============================================================================
def generate_frame_svg(t):
    b = _beat(t)
    # 压在人前面的道具（手机）**必须在这里算**：角色那段 f-string 比 release 分支
    # 先拼好，放在分支里赋值的话，模板取到的永远是空串 —— 手机怎么都画不出来。
    # debug 模式不走道具层，所以先给空串兜住。
    prop_front_html = prop_layer(t, front=True) if RENDER_MODE == "release" else ""
    m = compute_vibe_motion(t)

    scale = ACTIVE_PRESET["char_scale"]
    # 站位（画面层给的 char_x）+ 这一拍的走位过程。二者相加才是这帧的中心 x，
    # 所以换边是一段位移，不是"下一拍突然出现在另一边"。
    char_cx = b["char_x"] + travel_offset(t)
    char_cy = ACTIVE_PRESET["char_center_y"]

    # 角色在局部 400x400 画布中的中心为 (200, 200)
    tx = char_cx - 200 * scale
    ty = char_cy - 200 * scale

    mins = int(t // 60)
    secs = int(t % 60)
    millis = int((t % 1) * 100)
    time_str = f"{mins:02d}:{secs:02d}.{millis:02d}"

    # -------------------------------------------------------------------------
    # 角色主体 SVG 代码 (标准化几何体与刚体/柔性拓扑)
    # -------------------------------------------------------------------------
    character_svg = f"""
    <!-- 角色阴影 (贴地投影，跟着走位一起移动) -->
    <ellipse cx="{char_cx:.1f}" cy="{char_cy + 155 * scale}" rx="{110 * scale * m['bodyScaleX']:.1f}" ry="{22 * scale}" fill="rgba(0, 0, 0, 0.25)" class="no-stroke" />

    <!-- 角色主体容器：几何全部来自 Assets/Characters/Plobi/2D/Plobi.svg -->
    <g transform="translate({tx:.2f}, {ty:.2f}) scale({scale})">
      {build_plobi_character(m, t)}
    </g>

    <!-- 速度线在身后，所以排在角色之前 -->
    {fx_layer(t, char_cx, char_cy, char_cy + 150 * scale, front=False)}

    <!-- 开场那枚还没穿上的壳：画在角色之后（在前），所以他从它身后走出来 -->
    {loose_shell_svg(t) if RENDER_MODE == "release" else ""}

    <!-- 撞击星与尘土在身前 -->
    {fx_layer(t, char_cx, char_cy, char_cy + 150 * scale, front=True)}
    <!-- 手机压在他身前：他要"拿着"它 -->
    {prop_front_html if RENDER_MODE == "release" else ""}
    """

    if RENDER_MODE == "release":
        # ---------------------------------------------------------------------
        # 成片模式 (RELEASE MODE)：纯净演播厅背景 + 记号驱动的道具 + 双语字幕
        # ---------------------------------------------------------------------
        prop_html = prop_layer(t) + rigging_hook(t, char_cx)
        # 头尾各留一小段黑：开口前那 4.5 s 是"壳飞进来"，不该是干等
        fade = ""
        if t < 0.35:
            fade = ('<rect width="%d" height="%d" fill="#0f121a" opacity="%.3f"/>'
                    % (WIDTH, HEIGHT, 1.0 - t / 0.35))
        elif t > TOTAL_DURATION - 0.55:
            fade = ('<rect width="%d" height="%d" fill="#0f121a" opacity="%.3f"/>'
                    % (WIDTH, HEIGHT,
                       _clamp01((t - (TOTAL_DURATION - 0.55)) / 0.5) * 0.85))

        # 成片模式不在这里画字幕：胶囊要在质感层**之后**贴上来，
        # 否则线条抖动会连字一起抖，字幕看起来像在筛糠。见 render_frame_rgb()。

        svg = f"""<svg width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <!-- 角色几何与裁剪全部来自 Assets/Characters/Plobi/2D/Plobi.svg（经 rig.json 装配）。
         注意：PyMuPDF 不实现 clip-path，装配层已把它剥掉，明暗靠路径自身画对（§26）。 -->

    <!-- 背景流体渐变与环境光 -->
    <radialGradient id="studioGlow" cx="50%" cy="45%" r="65%">
      <stop offset="0%" stop-color="#2a3346" />
      <stop offset="60%" stop-color="#181d28" />
      <stop offset="100%" stop-color="#0f121a" />
    </radialGradient>

    <!-- 底部地板渐变 -->
    <linearGradient id="floorGrad" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#1b212e" stop-opacity="0.9" />
      <stop offset="100%" stop-color="#0c0e14" stop-opacity="1.0" />
    </linearGradient>

    <style>
      .plobi-part {{ stroke: #2C3A47; stroke-width: 8px; stroke-linecap: round; stroke-linejoin: round; }}
      .thin-line {{ stroke: #4A3423; stroke-width: 4px; stroke-linecap: round; stroke-linejoin: round; fill: none; }}
      .no-stroke {{ stroke: none; }}
      
      .subtitle-card {{ fill: rgba(15, 18, 26, 0.82); stroke: rgba(136, 192, 208, 0.22); stroke-width: 1.5; rx: 20; }}
      .watermark {{ fill: rgba(255, 255, 255, 0.35); font-family: sans-serif; font-size: 16px; font-weight: 700; letter-spacing: 2px; }}
    </style>
  </defs>

  <!-- 1. 背景空间 (Studio Atmosphere)：平涂 + 三层同心椭圆当环境光 -->
  <!-- 平涂一张就够：环境光交给质感层的连续光锥（同心椭圆叠出来是一圈一圈的
       硬边色带，比纯黑还难看）。 -->
  <rect width="{WIDTH}" height="{HEIGHT}" fill="#1a2030" />

  <!-- 地板：一条地平线 + 一块平涂，不用渐变 -->
  <path d="M 0 {HEIGHT * 0.72:.0f} L {WIDTH} {HEIGHT * 0.72:.0f} L {WIDTH} {HEIGHT} L 0 {HEIGHT} Z" fill="#10131b" />
  <line x1="0" y1="{HEIGHT * 0.72:.0f}" x2="{WIDTH}" y2="{HEIGHT * 0.72:.0f}" stroke="rgba(136, 192, 208, 0.12)" stroke-width="2" />

  <!-- 装饰微光网格粒子 (静态科技艺术感) -->
  <circle cx="{WIDTH * 0.15:.0f}" cy="{HEIGHT * 0.25:.0f}" r="3" fill="#88C0D0" opacity="0.3" class="no-stroke" />
  <circle cx="{WIDTH * 0.85:.0f}" cy="{HEIGHT * 0.30:.0f}" r="4" fill="#88C0D0" opacity="0.25" class="no-stroke" />
  <circle cx="{WIDTH * 0.82:.0f}" cy="{HEIGHT * 0.18:.0f}" r="2" fill="#EBCB8B" opacity="0.4" class="no-stroke" />

  <!-- 2. 阶段剧情道具（按段淡入淡出）—— 排在角色之前绘制。
         理由：这些是舞台件（篮板在身后、电视在地上、三连图标在背景），
         画在角色之上会让他穿过去。
         ⚠️ 我最初提交这行时写的理由是错的：我说"t=27 篮板把头整个盖掉、画面里
         零个五官像素"，那是 08:14 旧美术版视频的现象；新美术下实测改前瞳孔色
         1296 px、改后 1050 px，头一直都在。别把旧截图的观察当成当前事实。 -->
  {prop_html}

  <!-- 3. 居中角色渲染 -->
  {character_svg}
  <!-- 5. 头尾淡入淡出（只在边界那零点几秒存在，中间完全不画） -->
  {fade}
</svg>"""
        return svg

    else:
        # ---------------------------------------------------------------------
        # 调试模式 (DEBUG MODE)：保留参数监视面板、实时公式与时间码
        # ---------------------------------------------------------------------
        progress_w = int((t / TOTAL_DURATION) * 1200)
        svg = f"""<svg width="1280" height="720" viewBox="0 0 1280 720" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <style>
      .bg {{ fill: #12141a; }}
      .card-bg {{ fill: #1a1d26; stroke: #2b3040; stroke-width: 2; rx: 16; }}
      .title {{ fill: #ffffff; font-family: sans-serif; font-size: 22px; font-weight: 700; }}
      .badge-bg {{ fill: #203342; stroke: #88C0D0; stroke-width: 1.5; rx: 6; }}
      .badge-text {{ fill: #88C0D0; font-family: sans-serif; font-size: 13px; font-weight: 600; }}
      .time-text {{ fill: #EBCB8B; font-family: monospace; font-size: 18px; font-weight: 600; }}
      .hud-title {{ fill: #808B9F; font-family: sans-serif; font-size: 13px; font-weight: 600; }}
      .hud-val {{ fill: #ECEFF4; font-family: monospace; font-size: 16px; font-weight: 700; }}
      .vo-box {{ fill: #161b24; stroke: #88C0D0; stroke-width: 1.5; stroke-dasharray: 4 4; rx: 12; }}
      .vo-label {{ fill: #88C0D0; font-family: sans-serif; font-size: 12px; font-weight: bold; letter-spacing: 1px; }}
      .vo-content {{ fill: #ECEFF4; font-family: sans-serif; font-size: 20px; font-style: italic; }}
      .plobi-part {{ stroke: #2C3A47; stroke-width: 8px; stroke-linecap: round; stroke-linejoin: round; }}
      .thin-line {{ stroke: #4A3423; stroke-width: 4px; stroke-linecap: round; stroke-linejoin: round; fill: none; }}
      .no-stroke {{ stroke: none; }}
    </style>
  </defs>

  <rect width="1280" height="720" class="bg" />
  <rect x="40" y="24" width="1200" height="60" class="card-bg" />
  <text x="64" y="60" class="title">Plobi Vibe-Motion 动画系统 [DEBUG 视图]</text>
  <rect x="440" y="42" width="180" height="26" class="badge-bg" />
  <text x="452" y="60" class="badge-text">SVG 事件姿态驱动</text>
  <text x="1060" y="60" class="time-text">{time_str} / {int(TOTAL_DURATION // 60):02d}:{int(TOTAL_DURATION % 60):02d}.00</text>

  <rect x="40" y="100" width="680" height="470" class="card-bg" />
  <g transform="translate(180, 135)">
    <!-- 调试舞台内角色（真实暖桃版 Plobi） -->
    <g transform="scale(1.0)">
      {build_plobi_character(m, t)}
    </g>
  </g>

  <rect x="740" y="100" width="500" height="470" class="card-bg" />
  <text x="764" y="136" class="hud-title">CURRENT BEAT (当前拍：动作/站位/道具)</text>
  <rect x="764" y="150" width="452" height="44" fill="#242b38" rx="8" />
  <text x="780" y="178" fill="#FFFFFF" font-family="sans-serif" font-size="16px" font-weight="bold">{b['action']} @ x={b['char_x']} · {b['screen'] or '—'} · {len(b['cues'])} cues</text>

  <text x="764" y="226" class="hud-title">PHYSICS CONSTRAINTS HUD (物理约束监视)</text>
  <rect x="764" y="240" width="216" height="64" fill="#242b38" rx="8" />
  <text x="778" y="262" class="hud-title">龟壳刚体形变</text>
  <text x="778" y="290" class="hud-val" fill="#A3BE8C">0.00% (绝对刚体)</text>

  <rect x="1000" y="240" width="216" height="64" fill="#242b38" rx="8" />
  <text x="1014" y="262" class="hud-title">身体流体拉伸比</text>
  <text x="1014" y="290" class="hud-val" fill="#88C0D0">{m['bodyScaleY']:.2f}x (流体柔性)</text>

  <rect x="764" y="316" width="216" height="64" fill="#242b38" rx="8" />
  <text x="778" y="338" class="hud-title">左手挥舞角度</text>
  <text x="778" y="366" class="hud-val">{m['armLeftRot']:.1f}°</text>

  <rect x="1000" y="316" width="216" height="64" fill="#242b38" rx="8" />
  <text x="1014" y="338" class="hud-title">口型开合比</text>
  <text x="1014" y="366" class="hud-val">{int(((m['mouthAperture'] - 3.0) / 14.0) * 100):d}%</text>

  <text x="764" y="416" class="hud-title">VIBE-MOTION EQUATION (实时正弦公式)</text>
  <rect x="764" y="430" width="452" height="50" fill="#242b38" rx="8" />
  <text x="780" y="460" fill="#EBCB8B" font-family="monospace" font-size="14px">y(t) = sin(omega * t) * A | phase = {(t/TOTAL_DURATION)*100:.1f}%</text>

  <rect x="40" y="585" width="1200" height="110" class="card-bg" />
  <text x="64" y="618" class="vo-label">STAGE DIRECTION (他的括号原话)</text>
  <text x="64" y="654" class="vo-content">"{(b['stage'] or '（站定说话）')[:150]}"</text>
  <rect x="64" y="675" width="1152" height="6" fill="#2E3440" rx="3" />
  <rect x="64" y="675" width="{max(6, progress_w)}" height="6" fill="#88C0D0" rx="3" />
</svg>"""
        return svg

# =============================================================================
# 4.5 像素层：一帧的完整交付 = SVG 光栅化 → 手绘质感 → 贴字幕
#     顺序不能反：质感只该作用在"画"上，字幕是后贴的纸上的印刷字。
# =============================================================================
_TEX = None


def _tex():
    """质感层懒建：--no-texture 在 main() 里才改得动全局开关。"""
    global _TEX
    if TEXTURE and _TEX is None:
        _TEX = Crayon(HEIGHT, WIDTH)
    return _TEX if TEXTURE else None


BASE_LIGHT = 0.30        # 全场底光：中间亮、四角压暗，代替渲染器画不出来的径向渐变
SPOT_BOOST = 0.60        # 开场那束聚光灯额外加的量
# 按拍换色温：讲软件偏冷、讲运动偏暖、谢幕收成暗场。
# 渐变在这个光栅化器里不生效（§26），所以只能在像素层做，见 Texture.apply 的 tint。
GRADES = {"entrance": (1.00, 0.98, 0.94), "dunk": (1.06, 0.97, 0.90),
          "point": (0.94, 0.98, 1.07), "outro": (0.90, 0.92, 1.02),
          "talk": (1.00, 1.00, 1.00)}


def light_grade(t):
    """这一帧的光强与色温。色温按拍给，拍边界 0.6 秒交叉过渡，避免"啪"地换灯。"""
    i = _beat_index(t)
    act = BEATS[i]["action"]
    tint = GRADES.get(act, GRADES["talk"])
    if i:
        prev = GRADES.get(BEATS[i - 1]["action"], GRADES["talk"])
        w = ease_in_out_sine(_rel(t, BEATS[i]["start"], BEATS[i]["start"] + 0.6))
        tint = tuple(prev[k] + (tint[k] - prev[k]) * w for k in range(3))
    return spotlight_strength(t), tint


def spotlight_strength(t):
    """底光一直在；开场那束聚光灯在他穿上壳之后 1 秒内收掉。"""
    boost = 0.0
    if BEATS and BEATS[0]["action"] == "entrance":
        end = BEATS[0]["end"]
        if t <= end + 1.0:
            boost = _clamp01(1.0 - max(0.0, t - end) / 1.0) * SPOT_BOOST
    return BASE_LIGHT + boost


def _credit_overlay(t):
    """引用素材的署名条。只在屏幕里放别人作品的那一拍出现。

    为什么不是"素材来源于网络，如有侵权请联系删除"：那句没有法律效力，
    "通知—删除"安全港保护的是平台不是上传者，写它等于自认不确定有权用。
    《著作权法》二十四条"为介绍、评论某一作品"适当引用的条件里明确要
    **指明作者姓名、作品名称**，所以署名才是标准做法，也才是真正护身的那一行。
    这里署的是作品名《牛来》+ 用途（对比评论）。原作者名/链接他给了就替换
    "原作者"三个字，一个词的事。

    和字幕一样贴在质感层**之后**：写进 SVG 会被线条抖动带着晃，署名糊了等于没署。
    """
    b = _beat(t)
    txt = CREDITS.get(b.get("screen"))      # 本集档案里没给这一档的署名 -> 不贴
    if not txt:
        return None
    key = ("credit", txt)
    if key not in _CREDIT_CACHE:
        f = font_load("zh", 21, index=0)
        probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
        w = int(probe.textlength(txt, font=f)) + 26
        h = 21 + 20
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        # 深色描边而不是色块：色块会在画面上多出一块不属于场景的矩形
        d.text((13, 10), txt, font=f, fill=(232, 226, 214, 215),
               stroke_width=2, stroke_fill=(10, 12, 18, 200))
        _CREDIT_CACHE[key] = img
    return _CREDIT_CACHE[key]


_CREDIT_CACHE = {}


def render_frame_rgb(t, frame_idx):
    """t 时刻这一帧的最终像素（uint8, HxWx3）。渲染循环与抽帧工具共用这一份。"""
    svg = generate_frame_svg(t)
    doc = fitz.open(stream=svg.encode("utf-8"), filetype="svg")
    pix = doc[0].get_pixmap(dpi=72, colorspace=fitz.csRGB)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(HEIGHT, WIDTH, pix.n)
    if pix.n != 3:
        arr = arr[:, :, :3]
    arr = np.ascontiguousarray(arr)
    tex = _tex()
    if tex is not None:
        spot, tint = light_grade(t)
        arr = tex.apply(arr, frame_idx, spot=spot, tint=tint)
    sub = get_active_subtitle(t) if RENDER_MODE == "release" else None
    if sub is not None and sub.get("sub_img") is not None:
        img = Image.fromarray(arr).convert("RGBA")
        w, h = sub["sub_img"].size
        img.paste(sub["sub_img"],
                  ((WIDTH - w) // 2, HEIGHT - ACTIVE_PRESET["sub_bottom_margin"] - h),
                  sub["sub_img"])
        arr = np.asarray(img.convert("RGB"))
    credit = _credit_overlay(t) if RENDER_MODE == "release" else None
    if credit is not None:
        img = Image.fromarray(arr).convert("RGBA")
        img.paste(credit, (40, 46), credit)
        arr = np.asarray(img.convert("RGB"))
    return arr


# =============================================================================
# 5. 主渲染与 FFmpeg 管道合成
# =============================================================================
def _probe_duration(path):
    """用 ffprobe 读实际时长。读不出来就直接失败，不猜、不默认。"""
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path], stderr=subprocess.DEVNULL).decode().strip()
        return float(out)
    except Exception as exc:
        print(f"[-] 读不出音频时长：{path}（{exc}）", flush=True)
        sys.exit(1)


def _pick_audio(ep_dir, explicit):
    """音轨选择：--audio 显式指定优先；否则在本期 Audio/ 里挑时长最接近片长的一条。

    旧实现写死 vo_60s.mp3，而段表早已改成 77 s —— 结果 apad 把音轨补白，
    产出了后 17.1 s 完全静音的成片（Docs/VibeMotionPlan.md §21）。
    """
    if explicit:
        if not os.path.exists(explicit):
            print(f"[-] --audio 指定的文件不存在：{explicit}", flush=True)
            sys.exit(1)
        return explicit
    cand_dir = os.path.join(ep_dir, "Audio")
    best = None
    if os.path.isdir(cand_dir):
        for fn in sorted(os.listdir(cand_dir)):
            low = fn.lower()
            # seg*.wav 是分句素材、my_voice* 是本人音色采样，都不是成片音轨
            if not low.endswith((".mp3", ".wav", ".m4a")):
                continue
            if low.startswith("seg") or low.startswith("my_voice"):
                continue
            p = os.path.join(cand_dir, fn)
            d = _probe_duration(p)
            if best is None or abs(d - TOTAL_DURATION) < abs(best[1] - TOTAL_DURATION):
                best = (p, d)
    if best is None:
        print(f"[-] {cand_dir} 下找不到可用音轨", flush=True)
        sys.exit(1)
    return best[0]


# =============================================================================
# 0. 后端向契约注册自己的实现。声明（episode.json）与实现（这里）对不上账时
#    assert_complete() 会打**缺件报告**并 exit 1 —— 缺积木不该静默降级出厂。
# =============================================================================
for _a, _fn in CORE_FN.items():
    K.implement("action", _a, _fn)
for _s in ("niulai", "vector", "jellybean", "aura"):
    K.implement("screen", _s, _screen_content)
for _p in PROP_SVGS:
    K.implement("prop", _p, PROP_SVGS[_p])
# propAuraUi 没有独立 SVG：Aura 的界面是程序化画在电视玻璃里的
# （_screen_content 的 "aura" 档 + prop_layer 把记号转成 "screen:propAuraUi"）。
# 注册表按"能不能画"对账，不按"有没有一份 SVG"对账 —— 我第一版就把它误判成缺件。
K.implement("prop", "propAuraUi", prop_layer)
K.assert_complete()


def main():
    ap = argparse.ArgumentParser(description="EP001 逐帧渲染器（PyMuPDF + FFmpeg 管道）")
    ap.add_argument("--audio", default=None,
                    help="音轨路径。缺省则在本集档案的 Audio/ 里挑时长匹配片长的那条")
    ap.add_argument("--out", default=None,
                    help="输出文件名。缺省 plobi_intro_<画幅>_<模式>.mp4；试渲时务必另起名，别覆盖 release")
    ap.add_argument("--frames", default=None,
                    help="只渲若干帧存 PNG（逗号分隔秒数），用来目视检查，不进 ffmpeg")
    ap.add_argument("--no-texture", action="store_true",
                    help="关掉手绘质感层（线条抖动/纸纹/聚光灯），出干净矢量版做对比")
    ap.add_argument("--watchable", action="store_true",
                    help="分片 mp4：渲染途中就能打开预览。剪辑软件导入可能不认，成品别加")
    args = ap.parse_args()
    global TEXTURE, WATCHABLE
    TEXTURE = not args.no_texture
    WATCHABLE = args.watchable
    print(f"[i] 手绘质感：{'开（抖动 ±2px / 每 3 帧换一张）' if TEXTURE else '关'}", flush=True)

    # 在制哪一集由契约层定（--episode / PLOBI_EPISODE / Episodes/ 下唯一档案）
    base_dir = K.ROOT
    ep_dir = PROFILE_DIR
    render_dir = os.path.join(ep_dir, "Render")
    os.makedirs(render_dir, exist_ok=True)

    if args.frames:
        # 抽秒数出 PNG：目视检查用的，不进 ffmpeg、不需要音轨。
        out_dir = os.path.join(render_dir, "frames")
        os.makedirs(out_dir, exist_ok=True)
        for raw in args.frames.split(","):
            t = float(raw)
            from PIL import Image as _I
            path = os.path.join(out_dir, "frame_%07.2f.png" % t)
            _I.fromarray(render_frame_rgb(t, int(round(t * FPS)))).save(path)
            print("  [帧] t=%-7s → %s" % (("%.2f" % t), os.path.relpath(path, base_dir)),
                  flush=True)
        return 0

    audio_path = _pick_audio(ep_dir, args.audio)

    # 硬守卫：音轨时长必须贴合片长。过去这里静默 apad，把 60 s 音轨塞进 77 s 画面，
    # 后段全静音也没人报错（§4 债 8 / §21）。宁可 exit 1，不出"看起来成功"的废片。
    adur = _probe_duration(audio_path)
    if abs(adur - TOTAL_DURATION) > 1.0:
        print(f"[-] 音轨与片长不符：{os.path.basename(audio_path)} = {adur:.2f}s，"
              f"片长 = {TOTAL_DURATION:.2f}s，差 {abs(adur - TOTAL_DURATION):.2f}s。", flush=True)
        print("    拒绝渲染。改过 Script.md 段表或台词后，先重跑 Scripts/GenerateAudio.py。", flush=True)
        sys.exit(1)

    filename = args.out or f"plobi_intro_{ASPECT_RATIO.replace(':', 'x')}_{RENDER_MODE}.mp4"
    output_mp4 = os.path.join(render_dir, filename)

    print(f"==================================================", flush=True)
    print(f"[*] Plobi Video Rendering Pipeline", flush=True)
    print(f"[*] Mode: {RENDER_MODE.upper()} | Preset: {ASPECT_RATIO}", flush=True)
    print(f"[*] Resolution: {WIDTH}x{HEIGHT} @ {FPS} fps ({TOTAL_FRAMES} frames)", flush=True)
    print(f"[*] Audio: {audio_path}", flush=True)
    print(f"[*] Target File: {output_mp4}", flush=True)
    print(f"==================================================", flush=True)

    # 时长由 TOTAL_DURATION 决定（不随音频长度截断）：用 apad 把音频补到 TOTAL_DURATION，
    # 再用 -t TOTAL_DURATION 显式限定时长。音频是另一 worker 的活，渲染器对音频长度不敏感。
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-loglevel", "error",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-s", f"{WIDTH}x{HEIGHT}",
        "-pix_fmt", "rgb24",
        "-r", str(FPS),
        "-i", "-",
        "-i", audio_path,
        "-filter_complex", f"[1:a]apad=whole_dur={TOTAL_DURATION:.3f}[a]",
        "-map", "0:v",
        "-map", "[a]",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-t", f"{TOTAL_DURATION:.3f}",
    ]
    if WATCHABLE:
        # 普通 mp4 的索引（moov）写在文件末尾，所以渲染途中打开播放器只会报
        # "无法解析"——他踩过一次。分片写法则每帧都可播，代价是这类文件
        # 剪辑软件（剪映）导入不一定认，所以只在预览时用，成品仍走默认。
        ffmpeg_cmd += ["-movflags", "+frag_keyframe+empty_moov+default_base_moof"]
    ffmpeg_cmd.append(output_mp4)

    pipe = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ffmpeg_closed_early = False
    for frame_idx in range(TOTAL_FRAMES):
        t = frame_idx / float(FPS)
        data = render_frame_rgb(t, frame_idx).tobytes()

        # Windows 管道安全分块写入
        total_len = len(data)
        chunk_size = 65536
        offset = 0
        try:
            while offset < total_len:
                end = min(offset + chunk_size, total_len)
                pipe.stdin.write(data[offset:end])
                offset = end
        except BrokenPipeError:
            # 音频已用 apad 补到 TOTAL_DURATION，理论上不会提前关闭；此处仅作兜底保护。
            ffmpeg_closed_early = True
            print(f"[i] FFmpeg stopped reading at frame {frame_idx} (unexpected early close).", flush=True)
            break

        if frame_idx % 200 == 0 or frame_idx == TOTAL_FRAMES - 1:
            pct = (frame_idx + 1) / TOTAL_FRAMES * 100.0
            print(f"Rendering: Frame {frame_idx + 1}/{TOTAL_FRAMES} ({pct:.1f}%) | Time: {t:.2f}s", flush=True)

    try:
        pipe.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    pipe.wait()

    if pipe.returncode == 0 and os.path.exists(output_mp4):
        file_size_mb = os.path.getsize(output_mp4) / (1024 * 1024)
        print(f"\n[+] SUCCESS! Render complete:", flush=True)
        print(f"    Path: {output_mp4}", flush=True)
        print(f"    Size: {file_size_mb:.2f} MB", flush=True)

        # 成片门禁：编码成功 ≠ 片子能用。§21 那次"后 17.1s 全静音"就是时长全对、
        # 所有既有校验全绿，只有对成片做客观检查才拦得住。不过就 exit 1。
        gate = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "CheckRender.py"), output_mp4])
        if gate.returncode != 0:
            print("[-] 成片门禁未通过 —— 这个 mp4 不要发。", flush=True)
            sys.exit(1)
    else:
        print(f"[-] FFmpeg error (code {pipe.returncode})", flush=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
