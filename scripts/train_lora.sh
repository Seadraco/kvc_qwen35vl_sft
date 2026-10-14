#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

if [[ $# -lt 1 || $# -gt 3 ]]; then
  echo "Usage: $0 TRAIN_JSONL [OUTPUT_DIR] [VAL_JSONL]" >&2
  exit 2
fi

if [[ -z "${KVC_ACTION_SCHEMA:-}" ]]; then
  echo "KVC_ACTION_SCHEMA is required for formal training; the smoke schema is never selected implicitly" >&2
  exit 2
fi

DATASET="$(realpath "$1")"
OUTPUT_DIR="$(realpath -m "${2:-$PROJECT_ROOT/outputs/train_$(date -u +%Y%m%dT%H%M%SZ)}")"
VAL_DATASET="${3:-}"
MODEL="${KVC_MODEL:-Qwen/Qwen3.5-9B}"
MODEL_REVISION="${KVC_MODEL_REVISION:-c202236235762e1c871ad0ccb60c8ee5ba337b9a}"
TRAIN_CONFIG="${KVC_TRAIN_CONFIG:-$PROJECT_ROOT/configs/train_lora.yaml}"
RESUME_CHECKPOINT="${KVC_RESUME_FROM_CHECKPOINT:-}"
ACTION_SCHEMA="$(realpath "$KVC_ACTION_SCHEMA")"
EXPECTED_FEATURES="${KVC_EXPECTED_FEATURES:-253}"
EXPECTED_FEATURE_SCHEMA="${KVC_FEATURE_SCHEMA:-pass2_253_v1}"
# Only the exact bundled legacy smoke fixture may use relative media paths and
# omit grouping IDs. Production and user-provided fixtures remain strict.
validation_args=(--require-absolute-video-paths --require-source-group)
if [[ "${KVC_BUNDLED_SMOKE:-0}" == "1" ]]; then
  if [[ "$DATASET" != "$PROJECT_ROOT/data/kvc_sft_smoke.jsonl" || \
        "$ACTION_SCHEMA" != "$PROJECT_ROOT/schemas/kvc_action_smoke_v0.schema.json" || \
        "$EXPECTED_FEATURES" != "16" || "$EXPECTED_FEATURE_SCHEMA" != "pass2_smoke_v0" || \
        -n "$VAL_DATASET" ]]; then
    echo "KVC_BUNDLED_SMOKE only applies to the exact bundled 16D smoke dataset/schema without validation data." >&2
    exit 2
  fi
  validation_args=()
fi
TOKEN_AUDIT_SAMPLES="${KVC_TOKEN_AUDIT_SAMPLES:-0}"
MANIFEST_TOKEN_AUDIT_SAMPLES="$TOKEN_AUDIT_SAMPLES"
if [[ "${KVC_SKIP_TOKEN_AUDIT:-0}" == "1" ]]; then
  MANIFEST_TOKEN_AUDIT_SAMPLES=-1
fi
kvc_configure_training_devices

if [[ -n "$RESUME_CHECKPOINT" ]]; then
  RESUME_CHECKPOINT="$("$PYTHON_BIN" -m kvc_sft.checkpoint "$RESUME_CHECKPOINT" --resume --world-size "$NPROC_PER_NODE")"
  if [[ "$(dirname "$RESUME_CHECKPOINT")" != "$OUTPUT_DIR" ]]; then
    echo "For complete-state resume, OUTPUT_DIR must be the checkpoint's parent directory." >&2
    exit 2
  fi
elif [[ -f "$OUTPUT_DIR/args.json" ]] || compgen -G "$OUTPUT_DIR/checkpoint-*" >/dev/null; then
  echo "Output already contains a training run; choose a new OUTPUT_DIR: $OUTPUT_DIR" >&2
  exit 2
fi

export PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"
export USE_HF="${USE_HF:-1}"
export TOKENIZERS_PARALLELISM="false"
export FORCE_QWENVL_VIDEO_READER="${FORCE_QWENVL_VIDEO_READER:-torchcodec}"
export TORCHCODEC_NUM_THREADS="${TORCHCODEC_NUM_THREADS:-4}"
export VIDEO_MAX_TOKEN_NUM="${VIDEO_MAX_TOKEN_NUM:-128}"
export VIDEO_MIN_TOKEN_NUM="${VIDEO_MIN_TOKEN_NUM:-32}"
export FPS_MAX_FRAMES="${FPS_MAX_FRAMES:-16}"
export MAX_RATIO="${MAX_RATIO:-200}"
export FRAME_FACTOR="${FRAME_FACTOR:-2}"
export FPS="${FPS:-2.0}"
export FPS_MIN_FRAMES="${FPS_MIN_FRAMES:-4}"
export SPATIAL_MERGE_SIZE="${SPATIAL_MERGE_SIZE:-2}"
export MODEL_SEQ_LEN="${MODEL_SEQ_LEN:-128000}"
export ROOT_IMAGE_DIR="${ROOT_IMAGE_DIR:-$(dirname "$DATASET")}" 

"$PYTHON_BIN" -m kvc_sft.validate_dataset \
  --dataset "$DATASET" \
  --schema "$ACTION_SCHEMA" \
  --expected-features "$EXPECTED_FEATURES" \
  --expected-feature-schema "$EXPECTED_FEATURE_SCHEMA" \
  --max-abs-feature "${KVC_MAX_ABS_FEATURE:-8}" \
  --max-feature-decimals "${KVC_MAX_FEATURE_DECIMALS:-6}" \
  "${validation_args[@]}" \
  --probe-video
# The default 40 GiB is a conservative free-memory admission threshold, not a
# promised training peak. Larger videos/batches and CUDA workspaces need more.
"$PYTHON_BIN" -m kvc_sft.check_env \
  --require-cuda \
  --physical-gpus "$CUDA_VISIBLE_DEVICES" \
  --min-free-gib "${KVC_MIN_FREE_GIB:-40}" \
  --dataset "$DATASET"

if [[ "${KVC_SKIP_TOKEN_AUDIT:-0}" != "1" ]]; then
  "$PYTHON_BIN" -m kvc_sft.audit_token_lengths \
    --dataset "$DATASET" \
    --model "$MODEL" \
    --model-revision "$MODEL_REVISION" \
    --max-length "${KVC_MAX_LENGTH:-8192}" \
    --video-backend "$FORCE_QWENVL_VIDEO_READER" \
    --fps-max-frames "$FPS_MAX_FRAMES" \
    --video-min-token-num "$VIDEO_MIN_TOKEN_NUM" \
    --video-max-token-num "$VIDEO_MAX_TOKEN_NUM" \
    --max-ratio "$MAX_RATIO" \
    --frame-factor "$FRAME_FACTOR" \
    --fps "$FPS" \
    --fps-min-frames "$FPS_MIN_FRAMES" \
    --spatial-merge-size "$SPATIAL_MERGE_SIZE" \
    --model-seq-len "$MODEL_SEQ_LEN" \
    --max-samples "$TOKEN_AUDIT_SAMPLES"
fi

command=(
  "$SWIFT_BIN" sft "$TRAIN_CONFIG"
  --model "$MODEL"
  --model_revision "$MODEL_REVISION"
  --dataset "$DATASET"
  --output_dir "$OUTPUT_DIR"
  --external_plugins "$PROJECT_ROOT/src/kvc_sft/torchcodec_compat.py" "$PROJECT_ROOT/src/kvc_sft/training_observer.py"
  --max_length "${KVC_MAX_LENGTH:-8192}"
  --per_device_train_batch_size "$KVC_PER_DEVICE_BATCH_SIZE"
  --gradient_accumulation_steps "$KVC_GRADIENT_ACCUMULATION_STEPS"
  --attn_impl "${KVC_ATTN_IMPL:-sdpa}"
  --gradient_checkpointing "${KVC_GRADIENT_CHECKPOINTING:-true}"
)
if [[ -n "$RESUME_CHECKPOINT" ]]; then
  command+=(--resume_from_checkpoint "$RESUME_CHECKPOINT" --resume_only_model false)
fi
if [[ -n "${KVC_MAX_STEPS:-}" ]]; then command+=(--max_steps "$KVC_MAX_STEPS"); fi
if [[ -n "${KVC_SAVE_STEPS:-}" ]]; then command+=(--save_steps "$KVC_SAVE_STEPS"); fi
if [[ -n "${KVC_LEARNING_RATE:-}" ]]; then command+=(--learning_rate "$KVC_LEARNING_RATE"); fi
if [[ -n "${KVC_LOGGING_STEPS:-}" ]]; then command+=(--logging_steps "$KVC_LOGGING_STEPS"); fi
if [[ -n "${KVC_WARMUP_RATIO:-}" ]]; then command+=(--warmup_ratio "$KVC_WARMUP_RATIO"); fi
if [[ -n "$VAL_DATASET" ]]; then
  VAL_DATASET="$(realpath "$VAL_DATASET")"
  "$PYTHON_BIN" -m kvc_sft.validate_dataset \
    --dataset "$VAL_DATASET" \
    --schema "$ACTION_SCHEMA" \
    --expected-features "$EXPECTED_FEATURES" \
    --expected-feature-schema "$EXPECTED_FEATURE_SCHEMA" \
    --max-abs-feature "${KVC_MAX_ABS_FEATURE:-8}" \
    --max-feature-decimals "${KVC_MAX_FEATURE_DECIMALS:-6}" \
    --require-absolute-video-paths \
    --require-source-group \
    --probe-video
  "$PYTHON_BIN" -m kvc_sft.compare_datasets --train "$DATASET" --val "$VAL_DATASET"
  if [[ "${KVC_SKIP_TOKEN_AUDIT:-0}" != "1" ]]; then
    "$PYTHON_BIN" -m kvc_sft.audit_token_lengths \
      --dataset "$VAL_DATASET" \
      --model "$MODEL" \
      --model-revision "$MODEL_REVISION" \
      --max-length "${KVC_MAX_LENGTH:-8192}" \
      --video-backend "$FORCE_QWENVL_VIDEO_READER" \
      --fps-max-frames "$FPS_MAX_FRAMES" \
      --video-min-token-num "$VIDEO_MIN_TOKEN_NUM" \
      --video-max-token-num "$VIDEO_MAX_TOKEN_NUM" \
      --max-ratio "$MAX_RATIO" \
      --frame-factor "$FRAME_FACTOR" \
      --fps "$FPS" \
      --fps-min-frames "$FPS_MIN_FRAMES" \
      --spatial-merge-size "$SPATIAL_MERGE_SIZE" \
      --model-seq-len "$MODEL_SEQ_LEN" \
      --max-samples "$TOKEN_AUDIT_SAMPLES"
  fi
  command+=(--val_dataset "$VAL_DATASET" --split_dataset_ratio 0 --eval_strategy steps)
fi

cd "$PROJECT_ROOT"
launch_contract_args=(
  --run "$OUTPUT_DIR" --schema "$ACTION_SCHEMA"
  --feature-schema "$EXPECTED_FEATURE_SCHEMA" --feature-count "$EXPECTED_FEATURES"
)
if [[ -n "$RESUME_CHECKPOINT" ]]; then launch_contract_args+=(--resume "$RESUME_CHECKPOINT"); fi
"$PYTHON_BIN" -m kvc_sft.launch_contract "${launch_contract_args[@]}" -- "${command[@]}"
"${command[@]}"
if [[ -n "${KVC_EXPECTED_STEPS:-}" ]]; then
  "$PYTHON_BIN" -m kvc_sft.inspect_run --run "$OUTPUT_DIR" --expected-steps "$KVC_EXPECTED_STEPS" --expected-world-size "$NPROC_PER_NODE"
fi

manifest_args=(
  write
  --run "$OUTPUT_DIR"
  --train-dataset "$DATASET"
  --schema "$ACTION_SCHEMA"
  --feature-schema "$EXPECTED_FEATURE_SCHEMA"
  --feature-count "$EXPECTED_FEATURES"
  --max-length "${KVC_MAX_LENGTH:-8192}"
  --token-audit-samples "$MANIFEST_TOKEN_AUDIT_SAMPLES"
  --model "$MODEL"
  --model-revision "$MODEL_REVISION"
  --video-backend "$FORCE_QWENVL_VIDEO_READER"
  --fps-max-frames "$FPS_MAX_FRAMES"
  --video-min-token-num "$VIDEO_MIN_TOKEN_NUM"
  --video-max-token-num "$VIDEO_MAX_TOKEN_NUM"
  --max-ratio "$MAX_RATIO"
  --frame-factor "$FRAME_FACTOR"
  --fps "$FPS"
  --fps-min-frames "$FPS_MIN_FRAMES"
  --spatial-merge-size "$SPATIAL_MERGE_SIZE"
  --model-seq-len "$MODEL_SEQ_LEN"
)
if [[ -n "$VAL_DATASET" ]]; then
  manifest_args+=(--val-dataset "$VAL_DATASET")
fi
"$PYTHON_BIN" -m kvc_sft.run_manifest "${manifest_args[@]}"
