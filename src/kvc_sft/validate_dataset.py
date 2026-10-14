"""Validate KVC multimodal SFT JSONL before loading a large model."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .contracts import ContractError, load_schema, loads_strict, validate_action_text


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    fps: float
    frame_count: int | None
    bit_depth: int | None
    hdr: int


def _jsonl_rows(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        raise ContractError(f"cannot read dataset {path}: {exc}") from exc
    with handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                raise ContractError(f"line {line_number}: blank JSONL lines are forbidden")
            row = loads_strict(raw_line, what=f"dataset line {line_number}")
            if not isinstance(row, dict):
                raise ContractError(f"line {line_number}: row must be a JSON object")
            yield line_number, row


def _tag_json(content: str, tag: str) -> Any:
    prefix = f"{tag}="
    matches = [line[len(prefix) :] for line in content.splitlines() if line.startswith(prefix)]
    if len(matches) != 1:
        raise ContractError(f"user prompt must contain exactly one line beginning with {prefix!r}")
    return loads_strict(matches[0], what=tag)


def _tag_string(content: str, tag: str) -> str:
    prefix = f"{tag}="
    matches = [line[len(prefix) :] for line in content.splitlines() if line.startswith(prefix)]
    if len(matches) != 1 or not matches[0]:
        raise ContractError(f"user prompt must contain exactly one non-empty {prefix!r} line")
    return matches[0]


def _numeric_leaves(value: Any, prefix: str = "pass2_features") -> list[tuple[str, float]]:
    leaves: list[tuple[str, float]] = []
    if isinstance(value, dict):
        if not value:
            raise ContractError(f"{prefix} must not be empty")
        for key, child in value.items():
            if not isinstance(key, str) or not key:
                raise ContractError(f"{prefix} contains an invalid feature name")
            leaves.extend(_numeric_leaves(child, f"{prefix}.{key}"))
    elif isinstance(value, list):
        if not value:
            raise ContractError(f"{prefix} must not be empty")
        for index, child in enumerate(value):
            leaves.extend(_numeric_leaves(child, f"{prefix}[{index}]"))
    elif isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{prefix} must be numeric, got {type(value).__name__}")
    else:
        number = float(value)
        if not math.isfinite(number):
            raise ContractError(f"{prefix} is not finite")
        leaves.append((prefix, number))
    return leaves


def _probe_video(path: Path) -> VideoInfo:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,avg_frame_rate,nb_frames,pix_fmt,bits_per_raw_sample,color_transfer",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ContractError("ffprobe is required for --probe-video") from exc
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() or "unknown ffprobe error"
        raise ContractError(f"ffprobe failed for {path}: {message}") from exc
    payload = loads_strict(result.stdout, what=f"ffprobe output for {path}")
    streams = payload.get("streams", [])
    if len(streams) != 1:
        raise ContractError(f"expected one primary video stream in {path}")
    stream = streams[0]
    try:
        numerator, denominator = stream["avg_frame_rate"].split("/", maxsplit=1)
        fps = float(numerator) / float(denominator)
        frames = stream.get("nb_frames")
        raw_bits = stream.get("bits_per_raw_sample")
        bit_depth = int(raw_bits) if raw_bits not in (None, "", "0", "N/A") else None
        if bit_depth is None:
            pixel_format = str(stream.get("pix_fmt", ""))
            depth_match = re.search(r"p(?:lanar)?(9|10|12|14|16)(?:le|be)?$", pixel_format)
            bit_depth = int(depth_match.group(1)) if depth_match else (8 if pixel_format else None)
        hdr = int(stream.get("color_transfer") in {"smpte2084", "arib-std-b67"})
        return VideoInfo(
            width=int(stream["width"]),
            height=int(stream["height"]),
            fps=fps,
            frame_count=int(frames) if frames not in (None, "N/A") else None,
            bit_depth=bit_depth,
            hdr=hdr,
        )
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise ContractError(f"invalid ffprobe metadata for {path}: {exc}") from exc


def _resolve_media_path(raw_path: str, dataset_path: Path) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = dataset_path.parent / path
    return path.resolve()


def _validate_row(
    line_number: int,
    row: dict[str, Any],
    *,
    dataset_path: Path,
    action_schema: dict[str, Any],
    expected_features: int | None,
    max_abs_feature: float | None,
    max_feature_decimals: int | None,
    probe_video: bool,
    require_absolute_video_paths: bool = False,
    require_source_group: bool = False,
    probe_cache: dict[Path, VideoInfo] | None = None,
) -> tuple[tuple[str, ...], str, int]:
    messages = row.get("messages")
    videos = row.get("videos")
    if require_source_group:
        if not isinstance(row.get("sample_id"), str) or not row["sample_id"]:
            raise ContractError("formal datasets require a non-empty sample_id")
        if not isinstance(row.get("source_group"), str) or not row["source_group"]:
            raise ContractError("formal datasets require a non-empty source_group")
    if not isinstance(messages, list) or not messages:
        raise ContractError("'messages' must be a non-empty list")
    if not isinstance(videos, list) or len(videos) != 1 or not isinstance(videos[0], str):
        raise ContractError("v1 requires exactly one string path in 'videos'")

    roles = [message.get("role") if isinstance(message, dict) else None for message in messages]
    if roles != ["system", "user", "assistant"]:
        raise ContractError(f"roles must be ['system', 'user', 'assistant'], got {roles}")
    for message in messages:
        if set(message) != {"role", "content"} or not isinstance(message["content"], str):
            raise ContractError("every message must contain only string 'role' and 'content'")

    prompt_messages = messages[:-1]
    placeholder_count = sum(message["content"].count("<video>") for message in prompt_messages)
    if placeholder_count != len(videos):
        raise ContractError(
            f"number of <video> placeholders ({placeholder_count}) does not match videos ({len(videos)})"
        )
    if "<video>" in messages[-1]["content"]:
        raise ContractError("assistant target must not contain media placeholders")
    validate_action_text(messages[-1]["content"], action_schema, require_canonical=True)

    user_content = messages[1]["content"]
    if _tag_string(user_content, "decision_scope") not in {"clip", "gop"}:
        raise ContractError("decision_scope must be 'clip' or 'gop'")
    feature_schema = _tag_string(user_content, "feature_schema")
    video_meta = _tag_json(user_content, "video_meta")
    anchor_metrics = _tag_json(user_content, "anchor_metrics")
    features = _tag_json(user_content, "pass2_features")

    required_meta = {"width", "height", "fps", "frame_count", "bit_depth", "hdr"}
    if not isinstance(video_meta, dict) or set(video_meta) != required_meta:
        raise ContractError(f"video_meta keys must be exactly {sorted(required_meta)}")
    integer_meta = ("width", "height", "frame_count", "bit_depth", "hdr")
    for name in integer_meta:
        if isinstance(video_meta[name], bool) or not isinstance(video_meta[name], int):
            raise ContractError(f"video_meta.{name} must be an integer")
    if isinstance(video_meta["fps"], bool) or not isinstance(video_meta["fps"], (int, float)):
        raise ContractError("video_meta.fps must be numeric")
    if not math.isfinite(float(video_meta["fps"])):
        raise ContractError("video_meta.fps must be finite")
    if video_meta["width"] <= 0 or video_meta["height"] <= 0 or video_meta["fps"] <= 0:
        raise ContractError("video dimensions and fps must be positive")
    if video_meta["frame_count"] <= 0 or video_meta["bit_depth"] not in {8, 10, 12}:
        raise ContractError("video frame_count/bit_depth is invalid")
    if video_meta["hdr"] not in {0, 1}:
        raise ContractError("video_meta.hdr must be 0 or 1")

    required_metrics = {"base_qp", "bpp", "lpips_mean"}
    if not isinstance(anchor_metrics, dict) or set(anchor_metrics) != required_metrics:
        raise ContractError(f"anchor_metrics keys must be exactly {sorted(required_metrics)}")
    if isinstance(anchor_metrics["base_qp"], bool) or not isinstance(anchor_metrics["base_qp"], int):
        raise ContractError("anchor_metrics.base_qp must be an integer")
    if not 0 <= anchor_metrics["base_qp"] <= 63:
        raise ContractError("anchor_metrics.base_qp must be in [0, 63]")
    for name in ("bpp", "lpips_mean"):
        value = anchor_metrics[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ContractError(f"anchor_metrics.{name} must be finite numeric")
        if value < 0:
            raise ContractError(f"anchor_metrics.{name} must be non-negative")

    feature_leaves = _numeric_leaves(features)
    if expected_features is not None and len(feature_leaves) != expected_features:
        raise ContractError(f"expected {expected_features} feature values, got {len(feature_leaves)}")
    if max_abs_feature is not None:
        violations = [(name, value) for name, value in feature_leaves if abs(value) > max_abs_feature]
        if violations:
            preview = ", ".join(f"{name}={value}" for name, value in violations[:3])
            raise ContractError(f"normalized features exceed +/-{max_abs_feature}: {preview}")
    if max_feature_decimals is not None:
        tolerance = 10 ** (-(max_feature_decimals + 9))
        excessive_precision = [
            (name, value)
            for name, value in feature_leaves
            if abs(value - round(value, max_feature_decimals)) > tolerance
        ]
        if excessive_precision:
            preview = ", ".join(f"{name}={value}" for name, value in excessive_precision[:3])
            raise ContractError(
                f"features exceed {max_feature_decimals} decimal places: {preview}"
            )

    raw_video_path = Path(videos[0]).expanduser()
    if require_absolute_video_paths and not raw_video_path.is_absolute():
        raise ContractError("formal train/val/test datasets must use absolute video paths")
    video_path = _resolve_media_path(videos[0], dataset_path)
    if not video_path.is_file():
        raise ContractError(f"video does not exist: {video_path}")
    if probe_video:
        if probe_cache is not None and video_path in probe_cache:
            probed = probe_cache[video_path]
        else:
            probed = _probe_video(video_path)
            if probe_cache is not None:
                probe_cache[video_path] = probed
        for name in ("width", "height"):
            if int(video_meta[name]) != getattr(probed, name):
                raise ContractError(f"video_meta.{name} disagrees with ffprobe for {video_path}")
        if not math.isclose(float(video_meta["fps"]), probed.fps, rel_tol=1e-4, abs_tol=1e-4):
            raise ContractError(f"video_meta.fps disagrees with ffprobe for {video_path}")
        if probed.frame_count is not None and int(video_meta["frame_count"]) != probed.frame_count:
            raise ContractError(f"video_meta.frame_count disagrees with ffprobe for {video_path}")
        if probed.bit_depth is not None and video_meta["bit_depth"] != probed.bit_depth:
            raise ContractError(f"video_meta.bit_depth disagrees with ffprobe for {video_path}")
        if video_meta["hdr"] != probed.hdr:
            raise ContractError(f"video_meta.hdr disagrees with ffprobe for {video_path}")
    return tuple(name for name, _ in feature_leaves), feature_schema, video_path.stat().st_size


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--expected-features", type=int)
    parser.add_argument("--expected-feature-schema")
    parser.add_argument("--max-abs-feature", type=float, default=8.0)
    parser.add_argument("--max-feature-decimals", type=int, default=6)
    parser.add_argument("--probe-video", action="store_true")
    parser.add_argument("--require-absolute-video-paths", action="store_true")
    parser.add_argument("--require-source-group", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    dataset_path = args.dataset.expanduser().resolve()
    try:
        schema = load_schema(args.schema)
        row_count = 0
        feature_paths: tuple[str, ...] | None = None
        feature_schema: str | None = None
        media_bytes = 0
        sample_ids: set[str] = set()
        probe_cache: dict[Path, VideoInfo] = {}
        for line_number, row in _jsonl_rows(dataset_path):
            try:
                row_feature_paths, row_feature_schema, size = _validate_row(
                    line_number,
                    row,
                    dataset_path=dataset_path,
                    action_schema=schema,
                    expected_features=args.expected_features,
                    max_abs_feature=args.max_abs_feature,
                    max_feature_decimals=args.max_feature_decimals,
                    probe_video=args.probe_video,
                    require_absolute_video_paths=args.require_absolute_video_paths,
                    require_source_group=args.require_source_group,
                    probe_cache=probe_cache,
                )
            except ContractError as exc:
                raise ContractError(f"line {line_number}: {exc}") from exc
            if args.require_source_group:
                sample_id = row["sample_id"]
                if sample_id in sample_ids:
                    raise ContractError(f"line {line_number}: duplicate sample_id {sample_id!r}")
                sample_ids.add(sample_id)
            if args.expected_feature_schema and row_feature_schema != args.expected_feature_schema:
                raise ContractError(
                    f"line {line_number}: feature_schema must be {args.expected_feature_schema!r}, "
                    f"got {row_feature_schema!r}"
                )
            if feature_schema is None:
                feature_schema = row_feature_schema
            elif row_feature_schema != feature_schema:
                raise ContractError(
                    f"line {line_number}: feature_schema differs from the first row: "
                    f"{row_feature_schema!r} != {feature_schema!r}"
                )
            if feature_paths is None:
                feature_paths = row_feature_paths
            elif row_feature_paths != feature_paths:
                mismatch = next(
                    (
                        index
                        for index, (actual, expected) in enumerate(zip(row_feature_paths, feature_paths))
                        if actual != expected
                    ),
                    min(len(row_feature_paths), len(feature_paths)),
                )
                actual = row_feature_paths[mismatch] if mismatch < len(row_feature_paths) else "<missing>"
                expected = feature_paths[mismatch] if mismatch < len(feature_paths) else "<missing>"
                raise ContractError(
                    f"line {line_number}: feature topology/order differs at index {mismatch}: "
                    f"{actual!r} != {expected!r}"
                )
            row_count += 1
            media_bytes += size
        if row_count == 0:
            raise ContractError("dataset is empty")
        assert feature_paths is not None and feature_schema is not None
    except ContractError as exc:
        print(f"VALIDATION FAILED: {exc}", file=sys.stderr)
        return 1
    summary = {
        "status": "ok",
        "dataset": str(dataset_path),
        "rows": row_count,
        "feature_schema": feature_schema,
        "feature_values_per_row": len(feature_paths),
        "feature_path_sha256": hashlib.sha256("\0".join(feature_paths).encode("utf-8")).hexdigest(),
        "referenced_media_bytes": media_bytes,
        "video_probed": bool(args.probe_video),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
