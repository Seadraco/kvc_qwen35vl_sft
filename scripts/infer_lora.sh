#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
kvc_configure_inference_device

if [[ $# -lt 3 || $# -gt 4 ]]; then
  echo "Usage: $0 ADAPTER_OR_RUN_DIR DATASET_JSONL ACTION_SCHEMA [SAMPLE_INDEX]" >&2
  exit 2
fi

ADAPTER="$(realpath "$1")"
DATASET="$(realpath "$2")"
ACTION_SCHEMA="$(realpath "$3")"
SAMPLE_INDEX="${4:-0}"
GPU="${CUDA_VISIBLE_DEVICES:-0}"
MODEL="${KVC_MODEL:-Qwen/Qwen3.5-9B}"
MODEL_REVISION="${KVC_MODEL_REVISION:-c202236235762e1c871ad0ccb60c8ee5ba337b9a}"
REPORT="${KVC_INFER_REPORT:-$PROJECT_ROOT/reports/qwen35_infer_production_${SAMPLE_INDEX}.json}"
VIDEO_BACKEND="${FORCE_QWENVL_VIDEO_READER:-torchcodec}"
MAX_FRAMES="${FPS_MAX_FRAMES:-16}"
VIDEO_MIN_TOKENS="${VIDEO_MIN_TOKEN_NUM:-32}"
VIDEO_MAX_TOKENS="${VIDEO_MAX_TOKEN_NUM:-128}"
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

"$PYTHON_BIN" -m kvc_sft.check_env \
  --require-cuda \
  --physical-gpu "$GPU" \
  --min-free-gib "${KVC_INFER_MIN_FREE_GIB:-24}"

"$PYTHON_BIN" -m kvc_sft.validate_dataset \
  --dataset "$DATASET" \
  --schema "$ACTION_SCHEMA" \
  --expected-features "${KVC_EXPECTED_FEATURES:-253}" \
  --expected-feature-schema "${KVC_FEATURE_SCHEMA:-pass2_253_v1}" \
  --max-abs-feature "${KVC_MAX_ABS_FEATURE:-8}" \
  --max-feature-decimals "${KVC_MAX_FEATURE_DECIMALS:-6}" \
  --require-absolute-video-paths \
  --require-source-group

"$PYTHON_BIN" -m kvc_sft.infer_and_validate \
  --adapter "$ADAPTER" \
  --dataset "$DATASET" \
  --schema "$ACTION_SCHEMA" \
  --sample-index "$SAMPLE_INDEX" \
  --model "$MODEL" \
  --model-revision "$MODEL_REVISION" \
  --gpu "$GPU" \
  --video-backend "$VIDEO_BACKEND" \
  --video-max-token-num "$VIDEO_MAX_TOKENS" \
  --video-min-token-num "$VIDEO_MIN_TOKENS" \
  --fps-max-frames "$MAX_FRAMES" \
  --max-ratio "$MAX_RATIO_VALUE" \
  --frame-factor "$FRAME_FACTOR_VALUE" \
  --fps "$FPS_VALUE" \
  --fps-min-frames "$FPS_MIN_FRAMES_VALUE" \
  --spatial-merge-size "$SPATIAL_MERGE_SIZE_VALUE" \
  --model-seq-len "$MODEL_SEQ_LEN_VALUE" \
  --max-new-tokens "${KVC_MAX_NEW_TOKENS:-512}" \
  --report "$REPORT"
