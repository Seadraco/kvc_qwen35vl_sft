#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Use the image prefix, an activated named Conda env, or the detected Conda
# installation. No sibling source repository or author-specific home is needed.
CONDA_BIN="${KVC_CONDA_BIN:-${CONDA_EXE:-}}"
if [[ -z "$CONDA_BIN" ]]; then CONDA_BIN="$(type -P conda || true)"; fi
if [[ -n "${KVC_SFT_ENV_PREFIX:-}" ]]; then
  ENV_PREFIX="$KVC_SFT_ENV_PREFIX"
elif [[ "${CONDA_DEFAULT_ENV:-}" == kvc_qwen35_sft && -n "${CONDA_PREFIX:-}" ]]; then
  ENV_PREFIX="$CONDA_PREFIX"
elif [[ -n "$CONDA_BIN" && -x "$CONDA_BIN" ]]; then
  ENV_PREFIX="$("$CONDA_BIN" info --base)/envs/kvc_qwen35_sft"
else
  ENV_PREFIX=/opt/conda/envs/kvc_qwen35_sft
fi
ENV_PREFIX="$(realpath -m "$ENV_PREFIX")"
PYTHON_BIN="${KVC_SFT_PYTHON:-$ENV_PREFIX/bin/python}"
SWIFT_BIN="${KVC_SWIFT_BIN:-$ENV_PREFIX/bin/swift}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python environment not found at $PYTHON_BIN" >&2
  echo "Run: $PROJECT_ROOT/scripts/setup_env.sh" >&2
  exit 1
fi

if [[ ! -x "$SWIFT_BIN" ]]; then
  echo "ms-swift CLI not found at $SWIFT_BIN" >&2
  echo "Run: $PROJECT_ROOT/scripts/setup_env.sh" >&2
  exit 1
fi

export PYTHONPATH="$PROJECT_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PATH="$ENV_PREFIX/bin:$PATH"
export LD_LIBRARY_PATH="$ENV_PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export USE_HF="${USE_HF:-1}"
export PYTHONNOUSERSITE=1

# These launchers are control processes, not torchrun workers. All validation,
# token audits and manifests execute once here; only `swift sft` spawns ranks.
kvc_configure_training_devices() {
  if [[ -n "${LOCAL_RANK:-}" || -n "${RANK:-}" ]]; then
    echo "Run this shell launcher once, not inside torchrun/srun per-rank workers." >&2
    return 2
  fi
  # Stale worker-size variables are not valid in the single controller. torchrun
  # writes fresh values into each worker when it starts the actual SFT command.
  unset WORLD_SIZE LOCAL_WORLD_SIZE
  export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES-0,1}"
  if [[ ! "$CUDA_VISIBLE_DEVICES" =~ ^[0-9]+(,[0-9]+)*$ ]]; then
    echo "CUDA_VISIBLE_DEVICES must be comma-separated physical GPU indices, e.g. 0,1" >&2
    return 2
  fi
  local -a devices
  local device
  local -A seen=()
  IFS=',' read -r -a devices <<< "$CUDA_VISIBLE_DEVICES"
  for device in "${devices[@]}"; do
    if [[ -n "${seen[$device]:-}" ]]; then
      echo "Duplicate GPU index in CUDA_VISIBLE_DEVICES: $device" >&2
      return 2
    fi
    seen[$device]=1
  done
  if [[ -n "${NPROC_PER_NODE:-}" && "$NPROC_PER_NODE" != "${#devices[@]}" ]]; then
    echo "NPROC_PER_NODE must equal the number of visible GPUs (${#devices[@]})" >&2
    return 2
  fi
  if [[ "${NNODES:-1}" != "1" ]]; then
    echo "This launcher supports one node; use the documented one-node migration workflow." >&2
    return 2
  fi
  export NPROC_PER_NODE="${#devices[@]}"
  export NNODES=1 NODE_RANK=0 MASTER_ADDR="${MASTER_ADDR:-127.0.0.1}" MASTER_PORT="${MASTER_PORT:-29500}"
  export KVC_PER_DEVICE_BATCH_SIZE="${KVC_PER_DEVICE_BATCH_SIZE:-1}"
  export KVC_GRADIENT_ACCUMULATION_STEPS="${KVC_GRADIENT_ACCUMULATION_STEPS:-4}"
  if [[ ! "$KVC_PER_DEVICE_BATCH_SIZE" =~ ^[1-9][0-9]*$ || ! "$KVC_GRADIENT_ACCUMULATION_STEPS" =~ ^[1-9][0-9]*$ ]]; then
    echo "Batch size and gradient accumulation must be positive integers" >&2
    return 2
  fi
  echo "DDP: GPUs=$CUDA_VISIBLE_DEVICES ranks=$NPROC_PER_NODE micro_batch=$KVC_PER_DEVICE_BATCH_SIZE accumulation=$KVC_GRADIENT_ACCUMULATION_STEPS global_batch=$((NPROC_PER_NODE * KVC_PER_DEVICE_BATCH_SIZE * KVC_GRADIENT_ACCUMULATION_STEPS))"
}

kvc_configure_inference_device() {
  export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
  if [[ ! "$CUDA_VISIBLE_DEVICES" =~ ^[0-9]+$ ]]; then
    echo "Inference is single-GPU; set CUDA_VISIBLE_DEVICES to one physical GPU index." >&2
    return 2
  fi
  if [[ -n "${LOCAL_RANK:-}" || -n "${RANK:-}" ]]; then
    echo "Inference/reporting must be launched once, outside torchrun." >&2
    return 2
  fi
  unset NPROC_PER_NODE NNODES NODE_RANK MASTER_ADDR MASTER_PORT WORLD_SIZE LOCAL_WORLD_SIZE
}
