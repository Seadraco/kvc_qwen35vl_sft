"""Render measured KVC records into canonical ms-swift multimodal JSONL.

Input record format (one JSON object per line):
{
  "sample_id": "sourceA_clip0001",
  "source_group": "sourceA",
  "video": "/abs/path/sourceA_clip0001.mp4",
  "decision_scope": "clip",
  "video_meta": {...},
  "anchor_metrics": {...},
  "feature_schema": "pass2_253_v1",
  "pass2_features": {...},
  "target": {...}
}

The target must already be selected from actually encoded candidates.  This
program serializes it; it does not invent or rank ground truth.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterator

from .contracts import (
    ContractError,
    canonical_action,
    load_schema,
    loads_strict,
    required_key_order,
    validate_action_object,
)


def _rows(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                raise ContractError(f"line {line_number}: blank lines are forbidden")
            row = loads_strict(line, what=f"source record line {line_number}")
            if not isinstance(row, dict):
                raise ContractError(f"line {line_number}: source record must be an object")
            yield line_number, row


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _allowed_values_text(schema: dict[str, Any]) -> str:
    pieces: list[str] = []
    for name in required_key_order(schema):
        if name == "schema":
            continue
        definition = schema["properties"][name]
        if "enum" in definition:
            pieces.append(f"{name}={_compact(definition['enum'])}")
        elif "const" in definition:
            pieces.append(f"{name}=[{_compact(definition['const'])}]")
        else:
            raise ContractError(f"action property {name!r} must use a finite enum/const")
    return "；".join(pieces)


def _schema_name(schema: dict[str, Any]) -> str:
    definition = schema.get("properties", {}).get("schema", {})
    value = definition.get("const")
    if not isinstance(value, str) or not value:
        raise ContractError("action schema properties.schema.const must be a non-empty string")
    return value


def _render_record(
    record: dict[str, Any],
    *,
    source_path: Path,
    schema: dict[str, Any],
    make_video_absolute: bool,
    output_directory: Path,
) -> dict[str, Any]:
    required = {
        "sample_id",
        "source_group",
        "video",
        "decision_scope",
        "video_meta",
        "anchor_metrics",
        "feature_schema",
        "pass2_features",
        "target",
    }
    missing = sorted(required - set(record))
    if missing:
        raise ContractError(f"missing source fields: {missing}")
    if record["decision_scope"] not in {"clip", "gop"}:
        raise ContractError("decision_scope must be 'clip' or 'gop'")
    if not isinstance(record["source_group"], str) or not record["source_group"]:
        raise ContractError("source_group must be a non-empty string")
    if not isinstance(record["feature_schema"], str) or not record["feature_schema"]:
        raise ContractError("feature_schema must be a non-empty string")
    if not isinstance(record["video"], str) or not record["video"]:
        raise ContractError("video must be a non-empty path string")
    validate_action_object(record["target"], schema, require_order=False)

    video_path = Path(record["video"]).expanduser()
    if not video_path.is_absolute():
        video_path = source_path.parent / video_path
    video_path = video_path.resolve()
    if not video_path.is_file():
        raise ContractError(f"video does not exist: {video_path}")
    output_video = (
        str(video_path)
        if make_video_absolute
        else os.path.relpath(video_path, start=output_directory)
    )

    schema_name = _schema_name(schema)
    system = (
        "你是 KVC Pass3 编码配置预测器。根据原始视频片段、Pass2 编码特征和 Anchor 编码指标，"
        f"预测该片段的完整最终编码配置。只能输出一个符合 {schema_name} 的 JSON 对象，不得输出解释、"
        "注释、Markdown 或其他文本。所有字段都必须出现。合法取值："
        f"{_allowed_values_text(schema)}。"
    )
    user = "\n".join(
        [
            "<video>",
            f"decision_scope={record['decision_scope']}",
            f"video_meta={_compact(record['video_meta'])}",
            f"anchor_metrics={_compact(record['anchor_metrics'])}",
            f"feature_schema={record['feature_schema']}",
            f"pass2_features={_compact(record['pass2_features'])}",
            "输出完整配置。",
        ]
    )
    return {
        "sample_id": record["sample_id"],
        "source_group": record["source_group"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
            {"role": "assistant", "content": canonical_action(record["target"], schema)},
        ],
        "videos": [output_video],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Measured source records JSONL")
    parser.add_argument("--output", required=True, type=Path, help="ms-swift JSONL to create")
    parser.add_argument("--schema", required=True, type=Path, help="Action JSON Schema")
    parser.add_argument(
        "--relative-video-paths",
        "--keep-relative-video-paths",
        dest="relative_video_paths",
        action="store_true",
        help="Write paths relative to the output JSONL (formal distributed training requires absolute paths)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    source_path = args.input.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    if source_path == output_path:
        print("PREPARATION FAILED: input and output paths must differ", file=sys.stderr)
        return 1
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    rendered_count = 0
    try:
        schema = load_schema(args.schema)
        sample_ids: set[str] = set()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8") as handle:
            for line_number, record in _rows(source_path):
                sample_id = record.get("sample_id")
                if not isinstance(sample_id, str) or not sample_id:
                    raise ContractError(f"line {line_number}: sample_id must be a non-empty string")
                if sample_id in sample_ids:
                    raise ContractError(f"line {line_number}: duplicate sample_id {sample_id!r}")
                sample_ids.add(sample_id)
                try:
                    rendered = _render_record(
                        record,
                        source_path=source_path,
                        schema=schema,
                        make_video_absolute=not args.relative_video_paths,
                        output_directory=output_path.parent,
                    )
                except ContractError as exc:
                    raise ContractError(f"line {line_number}: {exc}") from exc
                handle.write(_compact(rendered) + "\n")
                rendered_count += 1
        if rendered_count == 0:
            raise ContractError("input dataset is empty")
        temporary.replace(output_path)
    except (ContractError, OSError) as exc:
        temporary.unlink(missing_ok=True)
        print(f"PREPARATION FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", "rows": rendered_count, "output": str(output_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
