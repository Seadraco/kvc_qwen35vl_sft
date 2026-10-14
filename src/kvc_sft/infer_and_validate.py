"""Load a trained LoRA adapter, run deterministic video inference, validate JSON."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .checkpoint import resolve_adapter
from .contracts import (
    ContractError,
    TEMPLATE_CONTRACT,
    canonical_action,
    load_schema,
    loads_strict,
    validate_action_text,
)
from .run_manifest import verify_contract


def _read_row(path: Path, index: int) -> dict[str, Any]:
    if index < 0:
        raise ContractError("sample index must be non-negative")
    selected: str | None = None
    row_count = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                if row_count == index:
                    selected = line
                    break
                row_count += 1
    except OSError as exc:
        raise ContractError(f"cannot read dataset {path}: {exc}") from exc
    if selected is None:
        raise ContractError(f"sample index {index} is outside dataset")
    row = loads_strict(selected, what=f"dataset sample {index}")
    if not isinstance(row, dict):
        raise ContractError("dataset sample must be an object")
    return row


def decode_generated_response(template: Any, choice: Any) -> dict[str, Any]:
    """Separate generated tokens from ms-swift's reconstructed prompt prefix.

    ms-swift 4.5.3 prepends the non-thinking context to message.content while
    decoding, even though those prefix tokens were supplied in the prompt.
    Decode the returned generation IDs with first_token=False instead of
    stripping a string. Any <think> block genuinely generated remains intact
    and will fail strict JSON validation; missing schema fields are not repaired.
    """
    token_ids = getattr(choice, "token_ids", None)
    if not isinstance(token_ids, list) or any(type(token) is not int for token in token_ids):
        raise ContractError("inference must return genuine generated token IDs; enable return_details")
    response = template.decode_generate_ids(token_ids, first_token=False, skip_special_tokens=False)
    swift_response = choice.message.content
    context_prefix = template._get_response_prefix()
    if swift_response != (context_prefix or "") + response:
        raise ContractError("framework response does not match its declared prompt-prefix reconstruction")
    return {
        "response": response,
        "swift_response": swift_response,
        "generated_token_ids": token_ids,
        "raw_generated_text_with_special_tokens": template.tokenizer.decode(
            token_ids, skip_special_tokens=False, clean_up_tokenization_spaces=False,
        ),
        "non_thinking_context_prefix": context_prefix,
        "response_source": "generated_token_ids; framework prompt-prefix reconstruction disabled; no JSON repair",
        "response_termination_handling": "ms-swift removes terminal EOS/template suffix only; generated thinking tokens are preserved",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", required=True, type=Path, help="Checkpoint or run output directory")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--sample-index", type=int, default=0)
    parser.add_argument("--model", default="Qwen/Qwen3.5-9B")
    parser.add_argument("--model-revision", default="c202236235762e1c871ad0ccb60c8ee5ba337b9a")
    parser.add_argument("--attn-impl", choices=["sdpa", "flash_attn", "eager"], default="sdpa")
    parser.add_argument("--max-new-tokens", type=int, default=128)
    parser.add_argument("--gpu", default="0", help="Physical GPU list exposed to this process")
    parser.add_argument("--video-max-token-num", type=int, default=128)
    parser.add_argument("--video-min-token-num", type=int, default=32)
    parser.add_argument("--fps-max-frames", type=int, default=16)
    parser.add_argument("--max-ratio", type=int, default=200)
    parser.add_argument("--frame-factor", type=int, default=2)
    parser.add_argument("--fps", type=float, default=2.0)
    parser.add_argument("--fps-min-frames", type=int, default=4)
    parser.add_argument("--spatial-merge-size", type=int, default=2)
    parser.add_argument("--model-seq-len", type=int, default=128000)
    parser.add_argument("--swift-repo", type=Path, help="Deprecated compatibility option; uses installed ms-swift")
    parser.add_argument("--video-backend", choices=["torchcodec", "torchvision", "decord"], default="torchcodec")
    parser.add_argument("--use-hf", action="store_true", default=None, help="Download base model from Hugging Face instead of ModelScope")
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--no-response-prefix",
        action="store_true",
        help="Compatibility flag; Qwen3.5 always generates the complete JSON without a forced schema prefix",
    )
    parser.add_argument(
        "--allow-invalid",
        action="store_true",
        help="Exit zero after successful adapter reload/generation even when a two-step smoke model emits invalid JSON",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    os.environ.setdefault("USE_HF", "1")
    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu
    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    os.environ["FORCE_QWENVL_VIDEO_READER"] = args.video_backend
    os.environ["VIDEO_MAX_TOKEN_NUM"] = str(args.video_max_token_num)
    os.environ["VIDEO_MIN_TOKEN_NUM"] = str(args.video_min_token_num)
    os.environ["FPS_MAX_FRAMES"] = str(args.fps_max_frames)
    os.environ["MAX_RATIO"] = str(args.max_ratio)
    os.environ["FRAME_FACTOR"] = str(args.frame_factor)
    os.environ["FPS"] = str(args.fps)
    os.environ["FPS_MIN_FRAMES"] = str(args.fps_min_frames)
    os.environ["SPATIAL_MERGE_SIZE"] = str(args.spatial_merge_size)
    os.environ["MODEL_SEQ_LEN"] = str(args.model_seq_len)
    os.environ.setdefault("TORCHCODEC_NUM_THREADS", "2")

    try:
        adapter = resolve_adapter(args.adapter)
        dataset = args.dataset.expanduser().resolve()
        _, manifest = verify_contract(
            run=args.adapter,
            dataset=dataset,
            schema=args.schema,
            model=args.model,
            model_revision=args.model_revision,
            video_backend=args.video_backend,
            fps_max_frames=args.fps_max_frames,
            video_min_token_num=args.video_min_token_num,
            video_max_token_num=args.video_max_token_num,
            max_ratio=args.max_ratio,
            frame_factor=args.frame_factor,
            fps=args.fps,
            fps_min_frames=args.fps_min_frames,
            spatial_merge_size=args.spatial_merge_size,
            model_seq_len=args.model_seq_len,
            swift_repo=args.swift_repo,
        )
        row = _read_row(dataset, args.sample_index)
        messages = row.get("messages")
        videos = row.get("videos")
        if not isinstance(messages, list) or not messages or messages[-1].get("role") != "assistant":
            raise ContractError("dataset sample must end with an assistant target")
        if not isinstance(videos, list) or not videos:
            raise ContractError("dataset sample contains no videos")
        resolved_videos: list[str] = []
        for raw_video in videos:
            if not isinstance(raw_video, str) or not raw_video:
                raise ContractError("every video entry must be a non-empty path string")
            video_path = Path(raw_video).expanduser()
            if not video_path.is_absolute():
                video_path = dataset.parent / video_path
            video_path = video_path.resolve()
            if not video_path.is_file():
                raise ContractError(f"video does not exist: {video_path}")
            resolved_videos.append(str(video_path))
        prompt_messages = messages[:-1]
        label = messages[-1]["content"]
        schema = load_schema(args.schema)
        response_prefix = None
        base_path_raw = manifest.get("resolved_base_path")
        if not isinstance(base_path_raw, str) or not Path(base_path_raw).is_dir():
            raise ContractError("run manifest does not point to an available exact base-model snapshot")
        model_to_load = base_path_raw

        import torch
        if args.video_backend == "torchcodec":
            from .torchcodec_compat import apply_patch

            apply_patch()
        from swift.infer_engine import InferRequest, RequestConfig, TransformersEngine

        engine = TransformersEngine(
            model_to_load,
            model_type="qwen3_5",
            adapters=[str(adapter)],
            torch_dtype=torch.bfloat16,
            attn_impl=args.attn_impl,
            device_map="auto",
            use_hf=args.use_hf,
            revision=args.model_revision,
            max_batch_size=1,
            template_type=TEMPLATE_CONTRACT["template"],
        )
        engine.template.enable_thinking = False
        engine.template.response_prefix = None
        request = InferRequest(
            messages=prompt_messages,
            videos=resolved_videos,
            chat_template_kwargs={"enable_thinking": False},
        )
        request_config = RequestConfig(max_tokens=args.max_new_tokens, temperature=0.0, return_details=True)
        responses = engine.infer([request], request_config=request_config)
        generation = decode_generated_response(engine.template, responses[0].choices[0])
        response = generation["response"]

        validation_error: str | None = None
        prediction: dict[str, Any] | None = None
        try:
            # Whitespace inside a JSON object is semantically irrelevant.  The
            # executable boundary consumes the canonicalized representation,
            # while all structural checks (including key order) remain strict.
            prediction = validate_action_text(response, schema, require_canonical=False)
        except ContractError as exc:
            validation_error = str(exc)
        report = {
            "status": "valid" if validation_error is None else "generated_but_invalid",
            "adapter": str(adapter),
            "model": args.model,
            "model_revision": args.model_revision,
            "base_model_loaded_from": model_to_load,
            "sample_index": args.sample_index,
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
            "response_prefix": response_prefix,
            "template_contract": TEMPLATE_CONTRACT,
            **generation,
            "prompt_token_count": len(responses[0].prompt_token_ids or []),
            "prediction": prediction,
            "canonical_response": canonical_action(prediction, schema) if prediction is not None else None,
            "label": label,
            "schema_valid": validation_error is None,
            "validation_error": validation_error,
        }
        if args.report:
            report_path = args.report.expanduser().resolve()
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if validation_error is not None and not args.allow_invalid:
            return 2
        return 0
    except Exception as exc:
        print(f"INFERENCE FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
