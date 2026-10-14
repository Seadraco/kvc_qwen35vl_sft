"""Compare train/validation feature contracts and reject exact media leakage."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .contracts import ContractError
from .validate_dataset import _jsonl_rows, _numeric_leaves, _resolve_media_path, _tag_json, _tag_string


def _contract(path: Path) -> tuple[str, tuple[str, ...], set[Path], set[str]]:
    feature_schema: str | None = None
    feature_paths: tuple[str, ...] | None = None
    media: set[Path] = set()
    source_groups: set[str] = set()
    for line_number, row in _jsonl_rows(path):
        try:
            messages = row["messages"]
            user_content = messages[1]["content"]
            row_schema = _tag_string(user_content, "feature_schema")
            row_paths = tuple(name for name, _ in _numeric_leaves(_tag_json(user_content, "pass2_features")))
            videos = row["videos"]
            media.update(_resolve_media_path(video, path) for video in videos)
            source_group = row.get("source_group")
            if isinstance(source_group, str) and source_group:
                source_groups.add(source_group)
        except (ContractError, KeyError, IndexError, TypeError) as exc:
            raise ContractError(f"{path}: line {line_number}: cannot read feature contract: {exc}") from exc
        if feature_schema is None:
            feature_schema = row_schema
            feature_paths = row_paths
        elif row_schema != feature_schema or row_paths != feature_paths:
            raise ContractError(f"{path}: line {line_number}: dataset is internally inconsistent")
    if feature_schema is None or feature_paths is None:
        raise ContractError(f"dataset is empty: {path}")
    return feature_schema, feature_paths, media, source_groups


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train", required=True, type=Path)
    parser.add_argument("--val", required=True, type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        train = args.train.expanduser().resolve()
        val = args.val.expanduser().resolve()
        train_schema, train_paths, train_media, train_groups = _contract(train)
        val_schema, val_paths, val_media, val_groups = _contract(val)
        if train_schema != val_schema:
            raise ContractError(f"feature_schema mismatch: train={train_schema!r}, val={val_schema!r}")
        if train_paths != val_paths:
            raise ContractError("train and val feature topology/order differ")
        overlap = sorted(train_media & val_media)
        if overlap:
            preview = ", ".join(str(path) for path in overlap[:3])
            raise ContractError(f"exact video path leakage between train and val: {preview}")
        group_overlap = sorted(train_groups & val_groups)
        if group_overlap:
            raise ContractError(
                "source_group leakage between train and val: " + ", ".join(group_overlap[:10])
            )
        digest = hashlib.sha256("\0".join(train_paths).encode("utf-8")).hexdigest()
        print(
            json.dumps(
                {
                    "status": "ok",
                    "feature_schema": train_schema,
                    "feature_values_per_row": len(train_paths),
                    "feature_path_sha256": digest,
                    "exact_video_overlap": 0,
                    "source_group_overlap": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (ContractError, OSError) as exc:
        print(f"DATASET COMPARISON FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
