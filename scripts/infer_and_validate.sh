#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
kvc_configure_inference_device

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "Usage: $0 ADAPTER_OR_RUN_DIR [SAMPLE_INDEX]" >&2
  exit 2
fi

ADAPTER="$(realpath "$1")"
SAMPLE_INDEX="${2:-0}"
GPU="${CUDA_VISIBLE_DEVICES:-0}"
MODEL="${KVC_MODEL:-Qwen/Qwen3.5-9B}"
MODEL_REVISION="${KVC_MODEL_REVISION:-c202236235762e1c871ad0ccb60c8ee5ba337b9a}"
REPORT="${KVC_INFER_REPORT:-$PROJECT_ROOT/reports/qwen35_infer_sample_${SAMPLE_INDEX}.json}"
VIDEO_MAX_TOKENS="${VIDEO_MAX_TOKEN_NUM:-64}"
VIDEO_MIN_TOKENS="${VIDEO_MIN_TOKEN_NUM:-32}"
MAX_FRAMES="${FPS_MAX_FRAMES:-8}"
MAX_RATIO_VALUE="${MAX_RATIO:-200}"
FRAME_FACTOR_VALUE="${FRAME_FACTOR:-2}"
FPS_VALUE="${FPS:-2.0}"
FPS_MIN_FRAMES_VALUE="${FPS_MIN_FRAMES:-4}"
SPATIAL_MERGE_SIZE_VALUE="${SPATIAL_MERGE_SIZE:-2}"
MODEL_SEQ_LEN_VALUE="${MODEL_SEQ_LEN:-128000}"
if [[ ! "$GPU" =~ ^[0-9]+$ ]]; then
  echo "This launcher is single-GPU; CUDA_VISIBLE_DEVICES must be one numeric physical GPU index" >&2
  exit 2
fi

allow_invalid=()
if [[ "${KVC_ALLOW_INVALID:-0}" == "1" ]]; then
  allow_invalid=(--allow-invalid)
fi

"$PYTHON_BIN" -m kvc_sft.check_env \
  --require-cuda \
  --physical-gpu "$GPU" \
  --min-free-gib "${KVC_INFER_MIN_FREE_GIB:-24}"

"$PYTHON_BIN" -m kvc_sft.infer_and_validate \
  --adapter "$ADAPTER" \
  --dataset "$PROJECT_ROOT/data/kvc_sft_smoke.jsonl" \
  --schema "$PROJECT_ROOT/schemas/kvc_action_smoke_v0.schema.json" \
  --sample-index "$SAMPLE_INDEX" \
  --model "$MODEL" \
  --model-revision "$MODEL_REVISION" \
  --gpu "$GPU" \
  --video-max-token-num "$VIDEO_MAX_TOKENS" \
  --video-min-token-num "$VIDEO_MIN_TOKENS" \
  --fps-max-frames "$MAX_FRAMES" \
  --max-ratio "$MAX_RATIO_VALUE" \
  --frame-factor "$FRAME_FACTOR_VALUE" \
  --fps "$FPS_VALUE" \
  --fps-min-frames "$FPS_MIN_FRAMES_VALUE" \
  --spatial-merge-size "$SPATIAL_MERGE_SIZE_VALUE" \
  --model-seq-len "$MODEL_SEQ_LEN_VALUE" \
  --max-new-tokens 128 \
  "${allow_invalid[@]}" \
  --report "$REPORT"
