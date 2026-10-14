#!/usr/bin/env bash
# Install only into a dedicated environment; never upgrade the base environment.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONDA_BIN="${KVC_CONDA_BIN:-${CONDA_EXE:-$(command -v conda || true)}}"
if [[ ! -x "$CONDA_BIN" ]]; then
  echo "Set KVC_CONDA_BIN to the conda executable (not the shell function)." >&2
  exit 1
fi
CONDA_BASE="$("$CONDA_BIN" info --base)"
ENV_PREFIX="$(realpath -m "${KVC_SFT_ENV_PREFIX:-$CONDA_BASE/envs/kvc_qwen35_sft}")"
if [[ "$ENV_PREFIX" == "$(realpath -m "$CONDA_BASE")" || "$ENV_PREFIX" == / ]]; then
  echo "Refusing to install into conda base or the filesystem root." >&2
  exit 1
fi

if [[ "${KVC_REQUIRE_CUDA:-1}" == 1 ]]; then
  # Match the training launcher's default and keep logical visibility aligned
  # with the physical indices queried by check_env, even on an eight-GPU host.
  export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES-0,1}"
  if [[ ! "$CUDA_VISIBLE_DEVICES" =~ ^[0-9]+(,[0-9]+)*$ ]]; then
    echo "Set CUDA_VISIBLE_DEVICES to physical GPU indices, e.g. 4,5; use KVC_REQUIRE_CUDA=0 for a GPU-less build." >&2
    exit 1
  fi
fi

export PIP_NO_CACHE_DIR=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PYTHONNOUSERSITE=1
export MAX_JOBS="${MAX_JOBS:-4}"
export DS_BUILD_OPS=0

export CUDA_HOME="${CUDA_HOME:-/usr/local/cuda-12.4}"
if [[ "${KVC_INSTALL_CAUSAL_CONV1D:-1}" == 1 ]]; then
  if [[ ! -x "$CUDA_HOME/bin/nvcc" ]]; then
    echo "CUDA compiler not found at $CUDA_HOME/bin/nvcc; install CUDA 12.4 devel." >&2
    exit 1
  fi
  "$CUDA_HOME/bin/nvcc" --version | grep -F 'release 12.4,'
  export PATH="$CUDA_HOME/bin:$PATH"
fi

CONDA_LOCK="$PROJECT_ROOT/environment/conda-explicit-linux-64.txt"
if [[ ! -x "$ENV_PREFIX/bin/python" ]]; then
  if [[ "${KVC_USE_LOCK:-1}" == 1 && -s "$CONDA_LOCK" ]]; then
    "$CONDA_BIN" create --yes --prefix "$ENV_PREFIX" --file "$CONDA_LOCK"
  else
    "$CONDA_BIN" create --yes --prefix "$ENV_PREFIX" --channel conda-forge \
      python=3.12 pip=26.2.1 setuptools=82.0.1 wheel=0.48.0 ffmpeg=7.1.1 pkg-config ninja
  fi
else
  "$ENV_PREFIX/bin/python" -c \
    'import sys; assert sys.version_info[:2] == (3, 12), "Use a separate Python 3.12 environment"'
  if [[ "${KVC_USE_LOCK:-1}" == 1 && -s "$CONDA_LOCK" ]]; then
    "$CONDA_BIN" install --yes --prefix "$ENV_PREFIX" --file "$CONDA_LOCK"
  else
    "$CONDA_BIN" install --yes --prefix "$ENV_PREFIX" --channel conda-forge \
      pip=26.2.1 setuptools=82.0.1 wheel=0.48.0 ffmpeg=7.1.1 pkg-config ninja
  fi
fi

PYTHON_BIN="$ENV_PREFIX/bin/python"
export PATH="$ENV_PREFIX/bin:$PATH"
export LD_LIBRARY_PATH="$ENV_PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
# Reference source checkouts are not the runtime framework installation.
unset PYTHONPATH
# Keep these bootstrap packages conda-owned. pip replacing them can delete
# conda-tracked files and make conda-pack fail despite `pip check` succeeding.
"$PYTHON_BIN" -m pip install --no-cache-dir \
  --index-url https://download.pytorch.org/whl/cu124 \
  torch==2.6.0+cu124 torchvision==0.21.0+cu124

# Reuse the reviewed wheel included in Git, before the resolver sees Decord.
DECORD_WHEEL="$PROJECT_ROOT/environment/wheels/decord-0.6.0-1kvc-py3-none-manylinux2010_x86_64.whl"
printf '%s  %s\n' '19c4411d79ff796a9d9e30d46ed6cd222b4023f467be9168f8f67a141583320d' "$DECORD_WHEEL" | sha256sum -c -
"$PYTHON_BIN" -m pip install --no-cache-dir --no-deps "$DECORD_WHEEL"

INSTALL_REQUIREMENTS="$PROJECT_ROOT/requirements.txt"
if [[ "${KVC_USE_LOCK:-1}" == 1 && -s "$PROJECT_ROOT/environment/requirements-lock.txt" ]]; then
  INSTALL_REQUIREMENTS="$PROJECT_ROOT/environment/requirements-lock.txt"
fi
"$PYTHON_BIN" -m pip install --no-cache-dir \
  --constraint "$PROJECT_ROOT/constraints-tested.txt" \
  --requirement <(sed '/^[[:space:]]*deepspeed==/d' "$INSTALL_REQUIREMENTS")

# DeepSpeed reads the installed Torch during metadata/build generation.
"$PYTHON_BIN" -m pip install --no-cache-dir --no-build-isolation \
  --constraint "$PROJECT_ROOT/constraints-tested.txt" \
  deepspeed==0.18.8 torch==2.6.0+cu124 torchvision==0.21.0+cu124

# Extensions import torch at build time. Install separately, never replacing
# the already selected torch wheel. Keep this step explicit for image builds.
if [[ "${KVC_INSTALL_CAUSAL_CONV1D:-1}" == 1 ]]; then
  export TORCH_CUDA_ARCH_LIST="${TORCH_CUDA_ARCH_LIST:-8.0;8.6;8.9;9.0}"
  # Upstream may guess a prebuilt wheel with the wrong libstdc++ ABI. Compile
  # against the installed torch; never force its CXX11 ABI flag to a new value.
  export CAUSAL_CONV1D_FORCE_BUILD=TRUE
  unset CAUSAL_CONV1D_FORCE_CXX11_ABI
  "$PYTHON_BIN" -m pip install --no-cache-dir --no-build-isolation --no-deps \
    --force-reinstall --no-binary causal-conv1d \
    causal-conv1d==1.6.0
fi

"$PYTHON_BIN" -m pip install --no-cache-dir --no-deps --editable "$PROJECT_ROOT"
"$PYTHON_BIN" -m pip check
"$PYTHON_BIN" -c 'import sys; from conda_pack import CondaEnv; env = CondaEnv.from_prefix(sys.prefix, ignore_editable_packages=True); print("conda-pack collection OK:", len(env.files), "files; no archive created")'
"$PYTHON_BIN" -c 'from transformers import Qwen3_5ForConditionalGeneration; import swift; print("ms-swift:", swift.__file__)'
if [[ "${KVC_REQUIRE_CUDA:-1}" == 1 ]]; then
  "$PYTHON_BIN" -m kvc_sft.check_env --require-cuda \
    --physical-gpus "$CUDA_VISIBLE_DEVICES"
else
  echo "GPU checks skipped for image construction. Run scripts/preflight.sh on the target GPU host."
fi
echo "Environment ready: $ENV_PREFIX"
echo "Activate with: conda activate $ENV_PREFIX"
