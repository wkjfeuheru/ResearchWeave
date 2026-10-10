#!/usr/bin/env bash
# 投研 Agent 自动化评测流程：数据集校验 -> 运行 agent + 独立裁判 -> 生成报告（可选与基线对比）。
#
# 关键约束：被测模型与裁判模型必须不同，否则 ExperimentRunner 拒绝启动。
# 默认：被测 deepseek（profile web-da8cda1a018e），裁判 qwen。
#
# 用法：
#   MODE=smoke          evals/run_research_eval.sh   # 17 条指标专项子集，低成本冒烟
#   MODE=representative evals/run_research_eval.sh   # 每类抽样，覆盖面更广
#   MODE=full           evals/run_research_eval.sh   # 全量 200 条
#   BASELINE=.researchx/evaluations/<旧实验> MODE=smoke evals/run_research_eval.sh
#
# 可用环境变量覆盖：AGENT_PROFILE / JUDGE_PROFILE / DATASET / OUTPUT / MODE / REPETITIONS / BASELINE / OH
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATASET="${DATASET:-$ROOT/evals/research_v1}"
SUBSET="${SUBSET:-$ROOT/evals/metrics_subset.txt}"
OUTPUT="${OUTPUT:-$ROOT/.researchx/evaluations}"
MODE="${MODE:-smoke}"
REPETITIONS="${REPETITIONS:-1}"
AGENT_PROFILE="${AGENT_PROFILE:-web-da8cda1a018e}"
JUDGE_PROFILE="${JUDGE_PROFILE:-qwen}"
# qwen 等部分 profile 未配置 context_window_tokens，且模型不在 MODEL_WINDOWS 中，
# 裁判会在首次调用前抛 ContextBudgetError；显式给出窗口（qwen-plus 支持 131072）。
JUDGE_CONTEXT_WINDOW_TOKENS="${JUDGE_CONTEXT_WINDOW_TOKENS:-131072}"

# 优先使用仓库内虚拟环境，避免依赖全局 PATH。
if [[ -z "${OH:-}" ]]; then
  if [[ -x "$ROOT/.venv/bin/oh" ]]; then OH="$ROOT/.venv/bin/oh"; else OH="oh"; fi
fi
if [[ -z "${PY:-}" ]]; then
  if [[ -x "$ROOT/.venv/bin/python" ]]; then PY="$ROOT/.venv/bin/python"; else PY="python3"; fi
fi

if [[ "$AGENT_PROFILE" == "$JUDGE_PROFILE" ]]; then
  echo "被测与裁判 profile 不能相同：$AGENT_PROFILE" >&2
  exit 2
fi

select_args=()
case "$MODE" in
  smoke)
    [[ -f "$SUBSET" ]] || { echo "缺少子集文件：$SUBSET" >&2; exit 2; }
    while read -r case_id; do
      [[ -z "$case_id" || "$case_id" == \#* ]] && continue
      select_args+=(--case-id "$case_id")
    done < "$SUBSET"
    ;;
  representative)
    select_args=(--representative)
    ;;
  full)
    ;;
  *)
    echo "未知 MODE：$MODE（可选 smoke|representative|full）" >&2
    exit 2
    ;;
esac

echo "== 1/3 数据集静态校验：$DATASET =="
"$OH" eval validate --dataset "$DATASET"

echo "== 2/3 运行评测：MODE=$MODE 被测=$AGENT_PROFILE 裁判=$JUDGE_PROFILE 重复=$REPETITIONS =="
"$OH" eval run \
  --profile "$AGENT_PROFILE" \
  --judge-profile "$JUDGE_PROFILE" \
  --judge-context-window-tokens "$JUDGE_CONTEXT_WINDOW_TOKENS" \
  --dataset "$DATASET" \
  --output "$OUTPUT" \
  --repetitions "$REPETITIONS" \
  "${select_args[@]}"

# 定位本次实验目录：run 会新建 research-<uuid>，取 OUTPUT 下最新目录。
dest="$OUTPUT/$(ls -t "$OUTPUT" | head -1)"
echo "== 3/3 实验目录：$dest =="

if [[ -n "${BASELINE:-}" ]]; then
  echo "-- 与基线对比：$BASELINE --"
  "$OH" eval report "$dest" --compare "$BASELINE"
fi

# 打印核心指标，便于 CI 日志直接判读。
if [[ -f "$dest/report.json" ]]; then
  "$PY" - "$dest/report.json" <<'PY'
import json, sys

report = json.load(open(sys.argv[1], encoding="utf-8"))
groups = report.get("groups", {})
rows = []
for group in ("environment:fixed", "environment:live"):
    metrics = groups.get(group)
    if not metrics:
        continue
    rows.append(f"[{group}]")
    for name in (
        "task_success",
        "path_correct",
        "gap_declaration",
        "unsupported_statement_rate",
        "citation_correctness",
        "citation_coverage",
        "total_tokens",
        "model_steps",
        "end_to_end_ms",
    ):
        item = metrics.get(name)
        if not item:
            rows.append(f"  {name}: 缺失")
            continue
        mean = "N/A" if item.get("mean") is None else round(item["mean"], 3)
        rows.append(f"  {name}: {mean}  ({item['count']}/{item['total']})")
print("\n".join(rows) if rows else "报告中暂无分组指标")
PY
fi

echo "完成。报告：$dest/report.md"