"""Summarize an ms-swift training run and fail if the smoke contract was not met."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

from .checkpoint import resolve_adapter
from .contracts import ContractError, loads_strict


def _last_logging_record(path: Path) -> dict[str, Any]:
    records = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not records:
        raise ContractError(f"empty logging file: {path}")
    record = loads_strict(records[-1], what="ms-swift logging.jsonl")
    if not isinstance(record, dict):
        raise ContractError("last logging record is not an object")
    return record


def inspect_adapter_tensors(adapter: Path) -> dict[str, Any]:
    """Read on CPU: reject nonfinite weights and unchanged zero-initialized B."""
    import torch
    from safetensors import safe_open

    torch.set_num_threads(min(4, torch.get_num_threads()))
    files = sorted(adapter.glob("adapter_model*.safetensors"))
    if not files:
        raise ContractError("adapter verification requires safetensors weights")
    tensor_count = b_count = nonzero_b = 0
    max_abs_b = 0.0
    for path in files:
        with safe_open(path, framework="pt", device="cpu") as weights:
            for name in weights.keys():
                tensor = weights.get_tensor(name)
                tensor_count += 1
                if not bool(torch.isfinite(tensor).all()):
                    raise ContractError(f"adapter tensor contains NaN/Inf: {name}")
                if "lora_B" in name:
                    b_count += 1
                    maximum = float(tensor.abs().max()) if tensor.numel() else 0.0
                    max_abs_b = max(max_abs_b, maximum)
                    nonzero_b += int(maximum > 0)
    if nonzero_b == 0:
        raise ContractError("no nonzero LoRA B tensors: training has not demonstrated an adapter update")
    return {
        "all_tensors_finite": True, "tensor_count": tensor_count, "lora_B_tensors": b_count,
        "nonzero_B_tensors": nonzero_b, "max_abs_B": max_abs_b,
        "note": "For the default LoRA initialization, B starts at zero; this verifies a nonzero learned update.",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--expected-steps", type=int)
    parser.add_argument("--expected-world-size", type=int)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        run = args.run.expanduser().resolve()
        adapter = resolve_adapter(run)
        logging_file = run / "logging.jsonl"
        if not logging_file.is_file():
            # Accommodate a caller that supplied a concrete checkpoint.
            logging_file = adapter.parent / "logging.jsonl"
        record = _last_logging_record(logging_file)
        global_step = int(record.get("global_step", -1))
        if args.expected_steps is not None and global_step != args.expected_steps:
            raise ContractError(f"expected global_step={args.expected_steps}, got {global_step}")
        history = record.get("log_history")
        if not isinstance(history, list) or not history:
            raise ContractError("training log_history is missing or empty")
        losses = [item.get("loss") for item in history if isinstance(item, dict) and "loss" in item]
        if not losses or any(not isinstance(loss, (int, float)) or not math.isfinite(loss) for loss in losses):
            raise ContractError(f"no finite training losses found: {losses}")
        grad_norms = [item["grad_norm"] for item in history if isinstance(item, dict) and "grad_norm" in item]
        if args.expected_steps is not None and not grad_norms:
            raise ContractError("smoke inspection requires logged gradient norms; use KVC_LOGGING_STEPS=1")
        if any(not isinstance(norm, (int, float)) or not math.isfinite(norm) for norm in grad_norms):
            raise ContractError(f"non-finite gradient norms: {grad_norms}")
        rank_reports = [
            loads_strict(path.read_text(encoding="utf-8"), what="rank runtime")
            for path in sorted(logging_file.parent.glob("runtime_rank_*.json"))
        ]
        if args.expected_world_size is not None:
            ranks = sorted(report["rank"] for report in rank_reports)
            if ranks != list(range(args.expected_world_size)):
                raise ContractError(f"expected rank reports 0..{args.expected_world_size - 1}, got {ranks}")
            for report in rank_reports:
                if report["status"] != "completed" or report["global_step"] != global_step:
                    raise ContractError(f"rank did not complete all steps: {report}")
                if not 0 < report["trainable_parameters"] < report["total_parameters"]:
                    raise ContractError("LoRA requires a nonempty proper subset of trainable parameters")
                if report["trainable_visual_parameters"] != 0:
                    raise ContractError("the initial LoRA experiment must keep the vision tower frozen")
        adapter_weights = list(adapter.glob("adapter_model.*"))
        if not adapter_weights:
            raise ContractError(f"adapter weights are missing from {adapter}")
        summary = {
            "status": "ok",
            "run": str(run),
            "adapter": str(adapter),
            "global_step": global_step,
            "losses": losses,
            "gradient_norms": grad_norms,
            "adapter_weights": [str(path) for path in adapter_weights],
            "adapter_tensor_verification": inspect_adapter_tensors(adapter),
            "memory": record.get("memory"),
            "per_rank_runtime": rank_reports,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except (ContractError, OSError, ValueError) as exc:
        print(f"RUN INSPECTION FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
