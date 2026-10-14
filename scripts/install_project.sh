#!/usr/bin/env bash
# Install the mounted project only; do not replace the image's training stack.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_PREFIX="$(realpath -m "${KVC_SFT_ENV_PREFIX:-${CONDA_PREFIX:-/opt/conda/envs/kvc_qwen35_sft}}")"
CONDA_BIN="${KVC_CONDA_BIN:-${CONDA_EXE:-/opt/conda/bin/conda}}"
PYTHON_BIN="$ENV_PREFIX/bin/python"
if [[ "$ENV_PREFIX" == / || ! -x "$PYTHON_BIN" || ! -d "$ENV_PREFIX/conda-meta" ]]; then
    echo "Set KVC_SFT_ENV_PREFIX to the dedicated Python 3.12 Conda environment." >&2
    exit 1
fi
if [[ -x "$CONDA_BIN" && "$ENV_PREFIX" == "$(realpath -m "$("$CONDA_BIN" info --base)")" ]]; then
    echo "Refusing to install the project into Conda base." >&2
    exit 1
fi
"$PYTHON_BIN" -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version'
"$PYTHON_BIN" -m pip install --no-deps --no-build-isolation --editable "$PROJECT_ROOT"
"$PYTHON_BIN" -m pip check
"$PYTHON_BIN" -c 'import kvc_sft; print("Project installed:", kvc_sft.__file__)'
