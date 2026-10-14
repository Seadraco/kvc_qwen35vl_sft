#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ $# -gt 2 ]]; then
  echo "Usage: $0 [OUTPUT_DIR] [MODEL_OR_LOCAL_PATH]" >&2
  exit 2
fi
OUTPUT_DIR="$(realpath -m "${1:-$PROJECT_ROOT/outputs/qwen35_smoke_$(date -u +%Y%m%dT%H%M%SZ)}")"
export KVC_MODEL="${2:-${KVC_MODEL:-Qwen/Qwen3.5-9B}}"
export KVC_SMOKE_STEPS="${KVC_SMOKE_STEPS:-2}"
if [[ ! "$KVC_SMOKE_STEPS" =~ ^[1-9][0-9]*$ ]]; then
  echo "KVC_SMOKE_STEPS must be a positive integer" >&2
  exit 2
fi
export KVC_TRAIN_CONFIG="$PROJECT_ROOT/configs/smoke_lora.yaml"
export KVC_BUNDLED_SMOKE=1
export KVC_ACTION_SCHEMA="$PROJECT_ROOT/schemas/kvc_action_smoke_v0.schema.json"
export KVC_EXPECTED_FEATURES=16 KVC_FEATURE_SCHEMA=pass2_smoke_v0
export KVC_MAX_LENGTH="${KVC_MAX_LENGTH:-2048}"
export KVC_GRADIENT_ACCUMULATION_STEPS="${KVC_GRADIENT_ACCUMULATION_STEPS:-1}"
export KVC_MAX_STEPS="$KVC_SMOKE_STEPS" KVC_EXPECTED_STEPS="$KVC_SMOKE_STEPS"
export KVC_SAVE_STEPS="${KVC_SAVE_STEPS:-$KVC_SMOKE_STEPS}"
export KVC_LEARNING_RATE="${KVC_SMOKE_LR:-0.0001}"
export FORCE_QWENVL_VIDEO_READER=torchcodec
export TORCHCODEC_NUM_THREADS="${TORCHCODEC_NUM_THREADS:-2}"
export VIDEO_MAX_TOKEN_NUM="${VIDEO_MAX_TOKEN_NUM:-64}"
export VIDEO_MIN_TOKEN_NUM="${VIDEO_MIN_TOKEN_NUM:-32}"
export FPS_MAX_FRAMES="${FPS_MAX_FRAMES:-8}"

# A two-row smoke validates the software path, not coding gain or generalization.
exec "$PROJECT_ROOT/scripts/train_lora.sh" "$PROJECT_ROOT/data/kvc_sft_smoke.jsonl" "$OUTPUT_DIR"
