#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

DATASET="${1:-$PROJECT_ROOT/data/kvc_sft_smoke.jsonl}"
ACTION_SCHEMA="${KVC_ACTION_SCHEMA:-$PROJECT_ROOT/schemas/kvc_action_smoke_v0.schema.json}"
EXPECTED_FEATURES="${KVC_EXPECTED_FEATURES:-16}"
EXPECTED_FEATURE_SCHEMA="${KVC_FEATURE_SCHEMA:-pass2_smoke_v0}"
PHYSICAL_GPUS="${KVC_PHYSICAL_GPUS:-${KVC_PHYSICAL_GPU:-${CUDA_VISIBLE_DEVICES:-0,1}}}"
export CUDA_VISIBLE_DEVICES="$PHYSICAL_GPUS"
# Conservative admission threshold for 48/80 GiB cards, not a peak-memory
# guarantee: video length, batching, CUDA workspaces and other processes vary.
MIN_FREE_GIB="${KVC_MIN_FREE_GIB:-40}"

export MAX_RATIO="${MAX_RATIO:-200}"
export FRAME_FACTOR="${FRAME_FACTOR:-2}"
export FPS="${FPS:-2.0}"
export FPS_MIN_FRAMES="${FPS_MIN_FRAMES:-4}"
export SPATIAL_MERGE_SIZE="${SPATIAL_MERGE_SIZE:-2}"
export MODEL_SEQ_LEN="${MODEL_SEQ_LEN:-128000}"
export FORCE_QWENVL_VIDEO_READER="${FORCE_QWENVL_VIDEO_READER:-torchcodec}"
export VIDEO_MAX_TOKEN_NUM="${VIDEO_MAX_TOKEN_NUM:-64}"
export VIDEO_MIN_TOKEN_NUM="${VIDEO_MIN_TOKEN_NUM:-32}"
export FPS_MAX_FRAMES="${FPS_MAX_FRAMES:-8}"
export TORCHCODEC_NUM_THREADS="${TORCHCODEC_NUM_THREADS:-2}"

"$PYTHON_BIN" -m kvc_sft.validate_dataset \
  --dataset "$DATASET" \
  --schema "$ACTION_SCHEMA" \
  --expected-features "$EXPECTED_FEATURES" \
  --expected-feature-schema "$EXPECTED_FEATURE_SCHEMA" \
  --probe-video

decode_args=(--dataset "$DATASET")
if [[ -n "${KVC_PREFLIGHT_VIDEO:-}" ]]; then
  decode_args=(--video "$KVC_PREFLIGHT_VIDEO")
fi

"$PYTHON_BIN" -m kvc_sft.check_env \
  --require-cuda \
  --physical-gpus "$PHYSICAL_GPUS" \
  --min-free-gib "$MIN_FREE_GIB" \
  "${decode_args[@]}"
