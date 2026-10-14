#!/usr/bin/env python3
"""Make a synthetic-label 253D fixture for memory/interface testing ONLY.

WARNING: NOT a measured SFT dataset and NOT encoder-quality evidence.
The video and all 253 feature values come from external_delivery unchanged.
Anchor metrics and the action label are copied from the synthetic smoke sample;
they are NOT measurements or optimal decisions for this source video. Every
replica has the SAME source_group and video: eight replicas are not eight
independent examples. Never include this fixture in formal training/evaluation.

The CLI refuses to overwrite existing files. This script does not start training.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from kvc_sft.contracts import ContractError, load_schema, loads_strict
from kvc_sft.prepare_dataset import _render_record
from kvc_sft.validate_dataset import _jsonl_rows, _numeric_leaves, _probe_video, _tag_json, _validate_row


WARNING = (
    "CAPACITY/INTERFACE TEST ONLY: real video and raw 253D features, but synthetic "
    "smoke anchor metrics and action labels. NOT measured ground truth. "
    "Replicas share one source video. DO NOT use for formal training or evaluation."
)
FEATURE_SCHEMA = "external_delivery_capacity_v0"


def make_capacity_rows(
    *,
    delivery: Path,
    smoke_dataset: Path,
    action_schema: Path,
    copies: int = 8,
) -> list[dict[str, Any]]:
    if copies < 1:
        raise ContractError("copies must be positive")
    delivery = delivery.expanduser().resolve()
    video = delivery / "source/source.mp4"
    feature_path = delivery / "cae_predict/crf-34/cae-features.json"
    features = loads_strict(feature_path.read_text(encoding="utf-8"), what="raw capacity features")
    # Validate numerics, but NEVER normalize, clip, rename, or round these values.
    if len(_numeric_leaves(features)) != 253:
        raise ContractError("capacity fixture requires exactly 253 raw feature values")
    video_info = _probe_video(video)
    if video_info.frame_count is None or video_info.bit_depth is None:
        raise ContractError("ffprobe must report frame count and bit depth; no guessed video metadata is allowed")
    video_meta = asdict(video_info)
    _, smoke = next(_jsonl_rows(smoke_dataset.expanduser().resolve()))
    anchor_metrics = _tag_json(smoke["messages"][1]["content"], "anchor_metrics")
    target = loads_strict(smoke["messages"][-1]["content"], what="synthetic smoke action")
    schema = load_schema(action_schema)
    rows = []
    for replica in range(copies):
        record = {
            "sample_id": f"capacity_only_source_replica_{replica:04d}",
            "source_group": "capacity_only_external_delivery_source",
            "video": str(video),
            "decision_scope": "clip",
            "video_meta": video_meta,
            "anchor_metrics": anchor_metrics,
            "feature_schema": FEATURE_SCHEMA,
            "pass2_features": features,
            "target": target,
        }
        row = _render_record(
            record,
            source_path=feature_path,
            schema=schema,
            make_video_absolute=True,
            output_directory=PROJECT_ROOT / "reports",
        )
        row.update({
            "fixture_only": True,
            "label_origin": "synthetic_smoke",
            "anchor_metrics_origin": "synthetic_smoke",
            "feature_origin": str(feature_path),
            "fixture_warning": WARNING,
            "replica_index": replica,
            "replicas_same_source": copies,
        })
        # Also expose the synthetic nature in model input, not just metadata that
        # the ms-swift dataset loader may drop before template rendering.
        row["messages"][0]["content"] = WARNING + "\n" + row["messages"][0]["content"]
        _validate_row(
            replica + 1,
            row,
            dataset_path=PROJECT_ROOT / "reports/qwen35_capacity_fixture.jsonl",
            action_schema=schema,
            expected_features=253,
            max_abs_feature=None,
            max_feature_decimals=None,
            probe_video=False,
            require_absolute_video_paths=True,
            require_source_group=True,
        )
        rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delivery", required=True, type=Path,
                        help="Explicit raw-data directory; never inferred from a sibling project")
    parser.add_argument("--smoke-dataset", type=Path, default=PROJECT_ROOT / "data/kvc_sft_smoke.jsonl")
    parser.add_argument("--schema", type=Path, default=PROJECT_ROOT / "schemas/kvc_action_smoke_v0.schema.json")
    parser.add_argument("--copies", type=int, default=8)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "reports/qwen35_capacity_fixture.jsonl")
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    try:
        if output.exists():
            raise ContractError(f"refusing to overwrite existing fixture: {output}")
        rows = make_capacity_rows(
            delivery=args.delivery,
            smoke_dataset=args.smoke_dataset,
            action_schema=args.schema,
            copies=args.copies,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
    except (ContractError, OSError, StopIteration) as exc:
        print(f"CAPACITY FIXTURE FAILED: {exc}", file=sys.stderr)
        return 1
    print(WARNING, file=sys.stderr)
    print(json.dumps({"output": str(output), "rows": len(rows), "feature_schema": FEATURE_SCHEMA,
                      "feature_count": 253, "fixture_only": True}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
