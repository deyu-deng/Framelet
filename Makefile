# 一条命令跑完该跑的检查。以前这些门禁只能手动一个个跑，没人记得全跑过没有。
PY ?= .venv/bin/python

check:  ## 仓库健康：资产 + 动作质量 + 出门前扫描
	$(PY) Scripts/CheckAsset.py
	$(PY) Scripts/CheckMotion.py
	$(PY) Scripts/CheckPublish.py

frames: ## 抽三帧看看（不需要音轨）
	$(PY) Scripts/RenderVideo.py --frames 2.0,6.5,10.5

episode: ## 起一集新壳：make episode NAME=EP002_xxx
	mkdir -p Episodes/$(NAME) && cp Episodes/Demo/episode.json Episodes/$(NAME)/ && \
	sed -i '' 's/"Demo"/"$(NAME)"/' Episodes/$(NAME)/episode.json && \
	echo "已建 Episodes/$(NAME)，改 episode.json 与 Script.md 就能跑"

venv:  ## 建环境装依赖（ffmpeg 要系统自己装：brew install ffmpeg）
	python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

.PHONY: check frames episode venv
