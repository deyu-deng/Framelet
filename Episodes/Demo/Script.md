# Demo —— 最小可跑样例（不含配音）

> 这份是给 clone 下来的人看的：契约长什么样、一集要声明哪些东西。
> 它**没有配音**，所以只够跑 `make check`（资产与动作质量门禁）和抽帧，
> 跑不了 `CheckSync` / `CheckRender` —— 那两个要真音轨。

```json
{
  "fps": 30,
  "duration": 12.0,
  "cue_window": 6.0,
  "speech": [
    {"id": "s1", "start": 1.5, "end": 11.0, "dur": 9.5,
     "en": "This line is placeholder voiceover for the demo timeline.",
     "zh": "这一句是样例时间线里的占位口播。",
     "subs": [
       {"start": 1.5, "end": 5.5,
        "en": "This line is placeholder voiceover",
        "zh": "这一句是样例时间线里的"},
       {"start": 5.5, "end": 11.0,
        "en": "for the demo timeline.", "zh": "占位口播。"}
     ]}
  ],
  "beats": [
    {"start": 0.0, "end": 4.5, "action": "entrance", "char_x": 960,
     "cues": [], "screen": null, "stage": "", "trigger": ""},
    {"start": 4.5, "end": 9.0, "action": "talk", "char_x": 960,
     "cues": [], "screen": null, "stage": "", "trigger": ""},
    {"start": 9.0, "end": 12.0, "action": "outro", "char_x": 960,
     "cues": [], "screen": null, "stage": "", "trigger": ""}
  ]
}
```

## 两层时间线

`speech[]` 是口播层（配音与字幕，时长来自实测音频），`beats[]` 是画面层（动作、道具、站位）。
分开的理由很简单：**口播段数不等于画面拍数**。一句话说完，画面可能演了三个动作。

## 校验规则（`Scripts/Contract.py`）

- 拍与拍之间不许有缝或重叠（>0.02 s 就报错）；
- 每拍的动作必须在 `episode.json` 声明过，且后端有实现 —— 否则打缺件报告；
- 站位变了，那一拍的动作必须会走路；
- 短于 0.9 s 的字幕不许出现（读不完）。
