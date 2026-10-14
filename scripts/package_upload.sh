#!/usr/bin/env bash
# Package this project only; exclude runtime weights, caches and checkpoints.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_NAME="$(basename "$PROJECT_ROOT")"
if [[ $# -gt 1 ]]; then
  echo "Usage: $0 [PROJECT_ROOT/outputs/uploads/NAME.tar.gz]" >&2
  exit 2
fi
ARCHIVE="$(realpath -m "${1:-$PROJECT_ROOT/outputs/uploads/${PROJECT_NAME}-code-$(date -u +%Y%m%dT%H%M%SZ).tar.gz}")"
if [[ "$ARCHIVE" != "$PROJECT_ROOT/outputs/uploads/"* || "$ARCHIVE" != *.tar.gz || -e "$ARCHIVE" ]]; then
  echo "Choose a new .tar.gz file inside this project's outputs/uploads directory." >&2
  exit 2
fi
mkdir -p "$(dirname "$ARCHIVE")"
tar -czf "$ARCHIVE" \
  --exclude="$PROJECT_NAME/models" \
  --exclude="$PROJECT_NAME/model_cache" \
  --exclude="$PROJECT_NAME/data/raw" \
  --exclude="$PROJECT_NAME/.cache" \
  --exclude="$PROJECT_NAME/outputs" \
  --exclude="$PROJECT_NAME/.git" \
  --exclude="$PROJECT_NAME/.venv" \
  --exclude="$PROJECT_NAME/.conda-env" \
  --exclude="$PROJECT_NAME/.pytest_cache" \
  --exclude="$PROJECT_NAME/.ruff_cache" \
  --exclude='*/__pycache__' \
  --exclude='*.pyc' --exclude='*.pyo' --exclude='*.egg-info' \
  --exclude='*/.env' --exclude='*/.env.*' \
  --exclude='*/.hf_token' --exclude='*/.aws' --exclude='*/.ssh' \
  --exclude='*.pem' --exclude='*.key' \
  -C "$(dirname "$PROJECT_ROOT")" "$PROJECT_NAME"
printf '%s\n' "$ARCHIVE"
