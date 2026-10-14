#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="$(realpath -m "${1:-$PROJECT_ROOT/outputs/smoke_$(date -u +%Y%m%dT%H%M%SZ)}")"

"$PROJECT_ROOT/scripts/train_smoke.sh" "$OUTPUT_DIR"
INFER_GPU="${KVC_INFER_GPU:-${CUDA_VISIBLE_DEVICES:-0,1}}"
INFER_GPU="${INFER_GPU%%,*}"
for sample in 0 1; do
  CUDA_VISIBLE_DEVICES="$INFER_GPU" KVC_ALLOW_INVALID=1 \
    KVC_INFER_REPORT="$OUTPUT_DIR/infer_sample_${sample}.json" \
    "$PROJECT_ROOT/scripts/infer_and_validate.sh" "$OUTPUT_DIR" "$sample"
done
