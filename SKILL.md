---
name: video-qa-microscope-lite
description: AI 视频 0.1 秒级切镜与音量体检（FFmpeg 显微镜）+ 基础 3 条物理防穿模静态检查（开源 Lite 版）。适用于拆解对标视频切镜/声画节奏、排查 AI 生成视频跳帧/尾帧瑕疵，以及提交提示词前的快速静态体检。
---

# Video QA Microscope Lite（AI 视频 0.1 秒体检显微镜 · 开源精简版）

解决大模型看视频的两大常见盲区：
1. **1fps 抽帧盲区**：每秒只看 1 张图，容易漏掉 0.1–0.3 秒级的切口抽搐、瞬移换人与片尾废帧。
2. **盲猜声音卡点**：纯看画面或转写稿无法对齐 100ms 级的音峰重音与硬切秒数；提示词缺少确定性物理规则前置拦截。

本 Lite 版包含两个零 API 成本的本地 Python 工具：
- `scripts/video_microscope.py`：本地 FFmpeg + Pillow 0.1 秒级视频与音轨体检（支持 `--mode reference` 对标拆片 与 `--mode qa` 2fps–6fps 密集连环画质检 + 片尾废帧预警）。
- `scripts/prompt_physics_lint.py`：通用 3 条物理防穿模静态检查（紧闭门穿墙、静音镜头禁带引号台词、英文语速上限预警）。

> **Lite 免费版 vs 完整 Pro 版边界说明**：
> - **本开源 Lite 版**：保留完整本地 FFmpeg 切镜/音峰扫描 + 2fps/4fps 密集拼图 + 片尾废帧出点建议 + 3 条通用物理规则（纯自用开源脚本，不提供一对一装机答疑）。
> - **飞书永久买断知识库 Pro 版**：额外收录完整 6 条物理规则（含开口表情崩坏 / 水杯停唇安全距）、H3 引擎专属切镜与 `<d>` 台词语法校验、Gemini 4 原生看片两步法改造实录、短剧多硬切导演规范及 10 条脱敏实片逐帧批注案例。

## 环境要求

- Python 3.9+
- Pillow：`pip install pillow`
- 系统依赖：`ffmpeg` 与 `ffprobe`（macOS: `brew install ffmpeg`；Ubuntu/Debian: `apt install ffmpeg`）

## 快速上手

### 1. 拆解对标爆款原片（`--mode reference`）

```bash
python3 scripts/video_microscope.py /path/to/reference.mp4 --mode reference --out ./probe_ref
```

输出：
- `summary.json`：全部硬切秒数 `detected_cuts_sec`、黑场/字卡区间、每 100ms 音量炸响重音 `top_transient_peaks` 与骤降静音点 `cliff_drops`；
- `transition_pairs/cut_XX.jpg`：每个切点前 0.15s 与后 0.15s 并排对比图；
- `palette_5hex.png`：裁去黑遮幅后的 5 段代表色号。

### 2. 成片质检抓跳帧与烂尾（`--mode qa`）

```bash
python3 scripts/video_microscope.py /path/to/output.mp4 --mode qa --fps 2 --out ./probe_qa
```

输出：
- `dense_sheet.png`：带精确秒数角标的 2fps（可设 `--fps 4` 或 `6`）密集连环画；
- `summary.json`：自动检测片尾最后 `[总时长 - 2.0s, 总时长 - 0.15s]` 是否存在多余尾切，并给出 `suggested_safe_out_sec` 安全裁切出点。

### 3. 提交生成前跑 3 条通用物理体检

```bash
python3 scripts/prompt_physics_lint.py --duration 10 --prompt "He opens the closed door and walks through it."
```
