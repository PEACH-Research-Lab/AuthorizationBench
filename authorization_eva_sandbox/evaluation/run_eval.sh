#!/bin/bash
# Batch runner: loops through all tasks and runs each one.
#
# Usage:
#   bash run_eval.sh \
#     --agent-llm-config group1 \
#     --env-llm-config group2 \
#     --outputs-path ./outputs \
#     --server-hostname localhost \
#     --tasks-dir ../tasks

set -e

AGENT_LLM_CONFIG=""
ENV_LLM_CONFIG=""
OUTPUTS_PATH="./outputs"
SERVER_HOSTNAME="localhost"
TASKS_DIR="../tasks"
ONLINE_COMPLETION_CHECK=""
ONLINE_COMPLETION_PROFILE=""
ONLINE_COMPLETION_TRIGGER_ITER=""
ONLINE_COMPLETION_WINDOW=""
ONLINE_COMPLETION_TIMEOUT=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --agent-llm-config) AGENT_LLM_CONFIG="$2"; shift 2 ;;
        --env-llm-config) ENV_LLM_CONFIG="$2"; shift 2 ;;
        --outputs-path) OUTPUTS_PATH="$2"; shift 2 ;;
        --server-hostname) SERVER_HOSTNAME="$2"; shift 2 ;;
        --tasks-dir) TASKS_DIR="$2"; shift 2 ;;
        # In-task completion monitor (PR 2). Forward as-is to run_eval.py.
        --online-completion-check) ONLINE_COMPLETION_CHECK="--online-completion-check"; shift 1 ;;
        --online-completion-profile) ONLINE_COMPLETION_PROFILE="--online-completion-profile $2"; shift 2 ;;
        --online-completion-trigger-iter) ONLINE_COMPLETION_TRIGGER_ITER="--online-completion-trigger-iter $2"; shift 2 ;;
        --online-completion-window) ONLINE_COMPLETION_WINDOW="--online-completion-window $2"; shift 2 ;;
        --online-completion-timeout-seconds) ONLINE_COMPLETION_TIMEOUT="--online-completion-timeout-seconds $2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

if [ -z "$AGENT_LLM_CONFIG" ] || [ -z "$ENV_LLM_CONFIG" ]; then
    echo "Usage: run_eval.sh --agent-llm-config <group> --env-llm-config <group> [--outputs-path <path>] [--server-hostname <host>] [--tasks-dir <dir>]"
    exit 1
fi

for task_dir in "$TASKS_DIR"/*/; do
    task_name=$(basename "$task_dir")

    # Skip if no task_config.json (the single source of truth for a task)
    if [ ! -f "$task_dir/task_config.json" ]; then
        echo "Skipping $task_name (no task_config.json)"
        continue
    fi

    echo "=========================================="
    echo "Running task: $task_name"
    echo "=========================================="

    python run_eval.py \
        --task-path "$(realpath "$task_dir")" \
        --outputs-path "$OUTPUTS_PATH" \
        --server-hostname "$SERVER_HOSTNAME" \
        --agent-llm-config "$AGENT_LLM_CONFIG" \
        --env-llm-config "$ENV_LLM_CONFIG" \
        $ONLINE_COMPLETION_CHECK \
        $ONLINE_COMPLETION_PROFILE \
        $ONLINE_COMPLETION_TRIGGER_ITER \
        $ONLINE_COMPLETION_WINDOW \
        $ONLINE_COMPLETION_TIMEOUT \
        || echo "Task $task_name failed, continuing..."

    echo ""
done

echo "All tasks completed. Results in $OUTPUTS_PATH"
