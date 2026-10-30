#!/usr/bin/env bash
# solution-radar/scripts/run.sh — 一条命令跑完整流程
#
# 不依赖当前工作目录：脚本自动定位自身所在位置。
# 无论从哪调用都可以：
#   bash ~/.workbuddy/skills/solution-radar/scripts/run.sh --topics ai-agent,mcp-server
#   bash /path/to/agent-radar/solution-radar/scripts/run.sh --topics ai-agent
#
# 产出（都在 skill 目录下的 out/ 里）：
#   out/entries.json   结构化清单
#   out/feed.json      订阅源
#   out/index.html     单页速览
#   out/YYYY-MM-DD.md  当日决策清单

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"

TOPICS="ai-agent,mcp-server,llm-agent"
MIN_STARS=800
LIMIT=40

while [[ $# -gt 0 ]]; do
  case "$1" in
    --topics=*)   TOPICS="${1#*=}"; shift ;;
    --topics)     TOPICS="$2"; shift 2 ;;
    --min-stars=*) MIN_STARS="${1#*=}"; shift ;;
    --min-stars)  MIN_STARS="$2"; shift 2 ;;
    --limit=*)    LIMIT="${1#*=}"; shift ;;
    --limit)      LIMIT="$2"; shift 2 ;;
    --help|-h)
      echo "用法: run.sh [--topics=ai-agent,mcp-server] [--min-stars=800] [--limit=40]"
      exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

OUT="$SKILL_DIR/out"
WORK="$OUT/.work"
mkdir -p "$WORK"

PY="${PYTHON:-python3}"
export PYTHONIOENCODING=utf-8

echo "=== 现成方案雷达 ==="
echo "垂类：$TOPICS ｜ 最小 star：$MIN_STARS ｜ 输出条数：$LIMIT"
echo ""

echo "[1/3] 采集"
"$PY" "$SCRIPT_DIR/fetch.py" --topics "$TOPICS" --min-stars "$MIN_STARS" --out "$WORK/raw.json"

echo "[2/3] 评分与四维补齐"
"$PY" "$SCRIPT_DIR/score.py" --in "$WORK/raw.json" --out "$OUT/entries.json" --limit "$LIMIT"

echo "[3/3] 渲染"
"$PY" "$SCRIPT_DIR/publish.py" --in "$OUT/entries.json" --outdir "$OUT" --reports "$OUT" --limit 20

# feed.json 也要在 out 根目录
if [[ -f "$OUT/feed.json" ]]; then :; fi

echo ""
echo "=== 完成 ==="
echo "结构化清单：$OUT/entries.json"
echo "订阅源：    $OUT/feed.json"
echo "单页速览：  $OUT/index.html"
echo ""
echo "提示：GitHub 上每天自动更新并部署，线上版本："
echo "  https://baoye-web.github.io/solution-radar/"
