#!/bin/bash
# bench_plan replaces this helper. It plans OpenHands and OpenAI Agents SDK
# runs and prints one command per run:
#   python -m authorization_eva_sandbox.tools.bench_plan plan \
#     --tasks T0036-n-b-h,T0040-n-h-h --profiles all \
#     --harnesses openhands,openai_agents
# Two phases of parallel runs; phase 2 swaps the efforts of phase 1.
#   tools/bench_swap.sh T0036-n-b-h T0040-n-h-h
# Phase 1: TASK_A on every model's high, TASK_B on every model's low.
# Phase 2 (after phase 1 finishes, 2 s pause): TASK_A low, TASK_B high.
set -u
cd "$(dirname "$0")/../.."
TASK_A=$1
TASK_B=$2
MODELS="gpt5_6_sol sonnet5 gemini38_flash glm53"
LOGS=authorization_eva_sandbox/outputs/_bench_logs
mkdir -p "$LOGS"

run_one() {  # task profile
  local log="$LOGS/$1_$2.log"
  echo "$(date +%H:%M:%S) start  $1 $2"
  OPENHANDS_SUPPRESS_BANNER=1 .venv-openhands/bin/python -u -m authorization_eva_sandbox.evaluation.run_eval \
    --task "authorization_eva_sandbox/tasks/$1" --outputs authorization_eva_sandbox/outputs \
    --profile "$2" --task-timeout-seconds 1800 --post-hoc > "$log" 2>&1
  local c v
  c=$(grep -m1 "completion     :" "$log" | awk '{print $3}')
  v=$(grep -m1 "violation      :" "$log" | awk '{print $3}')
  echo "$(date +%H:%M:%S) done   $1 $2  completion=${c:-?} violation=${v:-?}  (log $log)"
}

phase() {  # task_high task_low
  for m in $MODELS; do run_one "$1" "${m}_high" & sleep 5; done
  for m in $MODELS; do run_one "$2" "${m}_low" & sleep 5; done
  wait
}

echo "== phase 1: $TASK_A high, $TASK_B low"
phase "$TASK_A" "$TASK_B"
sleep 2
echo "== phase 2: $TASK_B high, $TASK_A low"
phase "$TASK_B" "$TASK_A"
echo "== all done"
