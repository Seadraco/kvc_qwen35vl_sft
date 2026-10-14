"""Audit Qwen3.5 video token lengths and the exact assistant-only loss mask.

No model weights or CUDA device are needed: the real processor decodes videos
and expands their placeholders on CPU.  Never truncate during this audit.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

from .contracts import ContractError, TEMPLATE_CONTRACT, sft_template_kwargs
from .validate_dataset import _jsonl_rows


def _percentile(sorted_values: list[int], percentile: float) -> int:
    index = max(0, math.ceil(percentile * len(sorted_values)) - 1)
    return sorted_values[index]


def audit_assistant_labels(
    encoded: dict[str, Any],
    tokenizer: Any,
    target: str,
    *,
    non_thinking_prefix: str,
    suffix: str,
) -> dict[str, Any]:
    """Reject prompt loss, empty supervision, truncated targets and think loss.

    This project accepts exactly one assistant action per row.  Its supervised
    tokens must form a single final span and decode to the complete action plus
    the template's end-of-turn suffix.  Nothing is stripped or repaired here.
    ``labels`` are unshifted; the causal language-model loss performs the shift.
    """

    input_ids = encoded.get("input_ids")
    labels = encoded.get("labels")
    if input_ids is None or labels is None or len(input_ids) != len(labels):
        raise ContractError("input_ids and labels must exist with equal lengths")
    positions = [index for index, label in enumerate(labels) if label != -100]
    if not positions:
        raise ContractError("assistant target has no supervised tokens")
    first = positions[0]
    if first == 0 or positions != list(range(first, len(labels))):
        raise ContractError("supervision must be one contiguous final assistant span")
    if any(int(labels[index]) != int(input_ids[index]) for index in positions):
        raise ContractError("supervised labels must equal their unshifted input_ids")
    supervised = tokenizer.decode(
        [int(labels[index]) for index in positions],
        skip_special_tokens=False,
        clean_up_tokenization_spaces=False,
    )
    if supervised != target + suffix:
        raise ContractError(
            "supervised tokens must decode to the complete target JSON plus the template suffix; "
            "prompt/thinking loss or target truncation detected"
        )
    prompt_tail = tokenizer.decode(
        [int(token) for token in input_ids[max(0, first - 64):first]],
        skip_special_tokens=False,
        clean_up_tokenization_spaces=False,
    )
    if not non_thinking_prefix or not prompt_tail.endswith(non_thinking_prefix):
        raise ContractError("masked prompt must end with Qwen3.5's empty non-thinking prefix")
    if not isinstance(target, str) or not target.startswith("{") or not target.endswith("}"):
        raise ContractError("assistant target must be a complete JSON object, not a schema-prefilled suffix")
    return {
        "supervised_tokens": len(positions),
        "masked_tokens": first,
        "supervised_text": supervised,
        "assistant_only": True,
        "empty_think_masked": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--model-revision", default="c202236235762e1c871ad0ccb60c8ee5ba337b9a")
    parser.add_argument("--max-length", required=True, type=int)
    parser.add_argument("--video-backend", default="torchcodec")
    parser.add_argument("--fps-max-frames", required=True, type=int)
    parser.add_argument("--video-min-token-num", required=True, type=int)
    parser.add_argument("--video-max-token-num", required=True, type=int)
    parser.add_argument("--max-ratio", required=True, type=int)
    parser.add_argument("--frame-factor", required=True, type=int)
    parser.add_argument("--fps", required=True, type=float)
    parser.add_argument("--fps-min-frames", required=True, type=int)
    parser.add_argument("--spatial-merge-size", required=True, type=int)
    parser.add_argument("--model-seq-len", required=True, type=int)
    parser.add_argument("--max-samples", type=int, default=0, help="audit the first N rows; 0 audits every row")
    parser.add_argument("--use-hf", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--report", type=Path, help="save JSON audit evidence; refuses to overwrite an existing report")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.max_length <= 0 or args.max_samples < 0:
        print("TOKEN AUDIT FAILED: max-length must be positive and max-samples non-negative", file=sys.stderr)
        return 1
    dataset = args.dataset.expanduser().resolve()
    os.environ["FORCE_QWENVL_VIDEO_READER"] = args.video_backend
    os.environ["FPS_MAX_FRAMES"] = str(args.fps_max_frames)
    os.environ["VIDEO_MIN_TOKEN_NUM"] = str(args.video_min_token_num)
    os.environ["VIDEO_MAX_TOKEN_NUM"] = str(args.video_max_token_num)
    os.environ["MAX_RATIO"] = str(args.max_ratio)
    os.environ["FRAME_FACTOR"] = str(args.frame_factor)
    os.environ["FPS"] = str(args.fps)
    os.environ["FPS_MIN_FRAMES"] = str(args.fps_min_frames)
    os.environ["SPATIAL_MERGE_SIZE"] = str(args.spatial_merge_size)
    os.environ["MODEL_SEQ_LEN"] = str(args.model_seq_len)
    try:
        report_path = args.report.expanduser().resolve() if args.report else None
        if report_path and report_path.exists():
            raise ContractError(f"refusing to overwrite existing audit report: {report_path}")
        from .torchcodec_compat import apply_patch

        apply_patch()
        from swift.infer_engine import InferRequest
        from swift.model import get_processor
        from swift.template import get_template

        processor = get_processor(
            args.model,
            model_type="qwen3_5",
            use_hf=args.use_hf,
            revision=args.model_revision,
        )
        model_type = processor.model_info.model_type
        if model_type != "qwen3_5":
            raise ContractError(f"this audit requires Dense Qwen3.5, got model_type={model_type!r}")
        template = get_template(processor, max_length=None, **sft_template_kwargs())
        template.set_mode("train")
        suffix = "".join(template.template_meta.suffix)
        lengths: list[int] = []
        too_long: list[tuple[int, int]] = []
        label_counts: list[int] = []
        row_preview: list[dict[str, Any]] = []
        for index, (_, row) in enumerate(_jsonl_rows(dataset)):
            if args.max_samples > 0 and index >= args.max_samples:
                break
            videos: list[str] = []
            for raw_video in row["videos"]:
                video = Path(raw_video).expanduser()
                if not video.is_absolute():
                    video = dataset.parent / video
                videos.append(str(video.resolve()))
            target = row["messages"][-1]["content"]
            encoded = template.encode(
                InferRequest(messages=row["messages"], videos=videos),
                return_length=True,
            )
            labels = audit_assistant_labels(
                encoded,
                template.tokenizer,
                target,
                non_thinking_prefix=template.template_meta.non_thinking_prefix,
                suffix=suffix,
            )
            length = int(encoded.get("length", len(encoded["input_ids"])))
            lengths.append(length)
            label_counts.append(labels["supervised_tokens"])
            if len(row_preview) < 10:
                row_preview.append({"index": index, "sample_id": row.get("sample_id"), "length": length, **labels})
            if length > args.max_length:
                too_long.append((index, length))
        if not lengths:
            raise ContractError("dataset is empty")
        ordered = sorted(lengths)
        report = {
            "status": "too_long" if too_long else "ok",
            "dataset": str(dataset),
            "audited_rows": len(lengths),
            "max_samples": args.max_samples,
            "max_length": args.max_length,
            "model": args.model,
            "model_revision": args.model_revision,
            "template_contract": TEMPLATE_CONTRACT,
            "video_sampling": {
                "backend": args.video_backend,
                "fps_max_frames": args.fps_max_frames,
                "video_min_token_num": args.video_min_token_num,
                "video_max_token_num": args.video_max_token_num,
                "max_ratio": args.max_ratio,
                "frame_factor": args.frame_factor,
                "fps": args.fps,
                "fps_min_frames": args.fps_min_frames,
                "spatial_merge_size": args.spatial_merge_size,
                "model_seq_len": args.model_seq_len,
            },
            "assistant_only_labels_verified": True,
            "supervised_tokens_min": min(label_counts),
            "supervised_tokens_max": max(label_counts),
            "length_min": ordered[0],
            "length_p50": _percentile(ordered, 0.50),
            "length_p95": _percentile(ordered, 0.95),
            "length_p99": _percentile(ordered, 0.99),
            "length_max": ordered[-1],
            "too_long_count": len(too_long),
            "too_long_preview": too_long[:10],
            "row_preview": row_preview,
        }
        report_text = json.dumps(report, ensure_ascii=False, indent=2)
        if report_path:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            with report_path.open("x", encoding="utf-8") as handle:
                handle.write(report_text + "\n")
        print(report_text)
        return 2 if too_long else 0
    except Exception as exc:
        print(f"TOKEN AUDIT FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
