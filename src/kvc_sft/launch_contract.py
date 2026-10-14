"""Pin the actual launch configuration before training and guard full-state resume."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any

import yaml

from .checkpoint import resolve_resume_checkpoint
from .contracts import ContractError, TEMPLATE_CONTRACT, loads_strict
from .run_manifest import _load_bundle, _sha256, _swift_identity


CONTRACT_FILE = "kvc_launch_contract.json"
EVENTS_FILE = "kvc_resume_events.json"
SAMPLING_KEYS = (
    "FORCE_QWENVL_VIDEO_READER", "VIDEO_MAX_TOKEN_NUM", "VIDEO_MIN_TOKEN_NUM", "FPS_MAX_FRAMES",
    "MAX_RATIO", "FRAME_FACTOR", "FPS", "FPS_MIN_FRAMES", "SPATIAL_MERGE_SIZE", "MODEL_SEQ_LEN",
)
OPERATIONAL_KEYS = ("max_steps", "save_steps", "logging_steps", "save_total_limit")


def effective_config(command: list[str]) -> dict[str, Any]:
    """Resolve the controlled launcher argv over YAML, matching last-option wins."""
    if len(command) < 3 or command[1] != "sft":
        raise ContractError("launch contract expects the complete swift sft YAML command")
    config = yaml.safe_load(Path(command[2]).read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ContractError("training YAML must be a mapping")
    config.pop("ENV", None)
    index = 3
    while index < len(command):
        flag = command[index]
        if not flag.startswith("--"):
            raise ContractError(f"unexpected training argument: {flag}")
        index += 1
        values = []
        while index < len(command) and not command[index].startswith("--"):
            values.append(command[index])
            index += 1
        if not values:
            raise ContractError(f"training option requires a value: {flag}")
        config[flag[2:]] = yaml.safe_load(values[0]) if len(values) == 1 else values
    for key, expected in TEMPLATE_CONTRACT.items():
        if config.get(key) != expected:
            raise ContractError(f"training/template audit mismatch for {key}: {config.get(key)!r} != {expected!r}")
    if config.get("model_type") != "qwen3_5" or config.get("tuner_type") != "lora":
        raise ContractError("this launcher requires Dense Qwen3.5 LoRA")
    if config.get("save_only_model", False):
        raise ContractError("save_only_model prevents complete-state resume")
    return config


def build_contract(command: list[str], *, schema: Path, feature_schema: str, feature_count: int) -> dict[str, Any]:
    config = effective_config(command)
    config.pop("output_dir", None)
    config.pop("resume_from_checkpoint", None)
    config.pop("resume_only_model", None)
    model = str(config["model"])
    model_path = Path(model).expanduser()
    model_bundle = _load_bundle(model_path.resolve(), adapter=False) if model_path.is_dir() else None
    if model_bundle is not None:
        config["model"] = "local_bundle:" + model_bundle["sha256"]
    datasets = {}
    for key in ("dataset", "val_dataset"):
        raw = config.pop(key, None)
        if raw is not None:
            paths = raw if isinstance(raw, list) else [raw]
            datasets[key] = [_sha256(Path(path).resolve()) for path in paths]
    plugins = config.pop("external_plugins", [])
    if isinstance(plugins, str):
        plugins = [plugins]
    plugin_hashes = [_sha256(Path(path).resolve()) for path in plugins]
    world_size = int(os.environ.get("NPROC_PER_NODE", "1"))
    micro_batch = int(config.pop("per_device_train_batch_size", 1))
    accumulation = int(config.pop("gradient_accumulation_steps", 1))
    operational = {key: config.pop(key, None) for key in OPERATIONAL_KEYS}
    return {
        "contract_version": 1,
        "immutable": {
            "training_config": config,
            "dataset_sha256": datasets,
            "action_schema_sha256": _sha256(schema),
            "feature_schema": feature_schema,
            "feature_count": feature_count,
            "feature_limits": {
                "max_abs": os.environ.get("KVC_MAX_ABS_FEATURE", "8"),
                "max_decimals": os.environ.get("KVC_MAX_FEATURE_DECIMALS", "6"),
            },
            "sampling": {key: os.environ.get(key) for key in SAMPLING_KEYS},
            "base_model_bundle": model_bundle,
            "plugin_sha256": plugin_hashes,
            "swift_source_sha256": _swift_identity()["source_sha256"],
            "world_size": world_size,
            "global_batch_size": world_size * micro_batch * accumulation,
        },
        "batch": {"micro_batch": micro_batch, "gradient_accumulation_steps": accumulation},
        "operational": operational,
        "locations": {"model": model},
    }


def verify_resume_contract(previous: dict[str, Any], current: dict[str, Any], completed_steps: int) -> None:
    """Only schedule extension / logging / saving / equivalent batching may vary."""
    if previous.get("contract_version") != 1 or current.get("contract_version") != 1:
        raise ContractError("unsupported launch contract version")
    before, after = previous.get("immutable", {}), current.get("immutable", {})
    changed = [key for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)]
    if changed:
        raise ContractError("resume contract mismatch: " + ", ".join(changed))
    old_max = previous.get("operational", {}).get("max_steps")
    new_max = current.get("operational", {}).get("max_steps")
    if isinstance(new_max, int) and new_max > 0:
        if new_max <= completed_steps:
            raise ContractError("resume max_steps must exceed completed global_step")
        if isinstance(old_max, int) and old_max > 0 and new_max < old_max:
            raise ContractError("resume may extend max_steps, not shorten the prior schedule")
    elif new_max != old_max:
        raise ContractError("resume cannot replace an explicit step schedule with an epoch schedule")


def verify_saved_training_args(saved: dict[str, Any], previous: dict[str, Any]) -> None:
    """Cross-check framework-written args against the original launcher contract."""
    expected = previous["immutable"]["training_config"]
    keys = set(TEMPLATE_CONTRACT) | {
        "model_type", "model_revision", "tuner_type", "lora_rank", "lora_alpha", "lora_dropout",
        "freeze_vit", "freeze_aligner", "torch_dtype", "max_length", "learning_rate", "optim",
        "weight_decay", "gradient_checkpointing", "target_modules",
    }
    for key in keys:
        want, actual = expected.get(key), saved.get(key)
        if key == "target_modules":
            want = [want] if isinstance(want, str) else want
            actual = [actual] if isinstance(actual, str) else actual
        if want != actual:
            raise ContractError(f"saved training args disagree with launch contract for {key}: {actual!r} != {want!r}")


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--feature-schema", required=True)
    parser.add_argument("--feature-count", required=True, type=int)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        run = args.run.expanduser().resolve()
        current = build_contract(command, schema=args.schema.resolve(), feature_schema=args.feature_schema, feature_count=args.feature_count)
        path = run / CONTRACT_FILE
        if args.resume:
            checkpoint = resolve_resume_checkpoint(args.resume, current["immutable"]["world_size"])
            if not path.is_file():
                raise ContractError("resume requires the original kvc_launch_contract.json; refuse an unverified restart")
            previous = loads_strict(path.read_text(encoding="utf-8"), what="launch contract")
            state = loads_strict((checkpoint / "trainer_state.json").read_text(encoding="utf-8"), what="trainer state")
            saved_args_path = checkpoint / "args.json"
            if not saved_args_path.is_file():
                saved_args_path = run / "args.json"
            saved_args = loads_strict(saved_args_path.read_text(encoding="utf-8"), what="saved training args")
            verify_saved_training_args(saved_args, previous)
            verify_resume_contract(previous, current, state["global_step"])
            new_max = current["operational"].get("max_steps")
            if (not isinstance(new_max, int) or new_max <= 0) and state["global_step"] >= state.get("max_steps", 0):
                raise ContractError("the epoch schedule already finished; explicitly extend KVC_MAX_STEPS to resume")
            events_path = run / EVENTS_FILE
            events = loads_strict(events_path.read_text(encoding="utf-8"), what="resume events") if events_path.is_file() else []
            events.append({
                "event": "resume_requested", "utc": datetime.now(timezone.utc).isoformat(),
                "checkpoint": str(checkpoint), "completed_steps": state["global_step"],
                "previous_operational": previous["operational"], "current_operational": current["operational"],
                "previous_batch": previous["batch"], "current_batch": current["batch"],
                "note": "Optimizer, scheduler, RNG and data position are restored; a longer max_steps is not an uninterrupted-run equivalence guarantee.",
            })
            _write_json(events_path, events)
        elif path.exists():
            raise ContractError(f"launch contract already exists; choose a fresh output or explicitly resume: {path}")
        run.mkdir(parents=True, exist_ok=True)
        _write_json(path, current)
        print(json.dumps({"status": "resume_verified" if args.resume else "launch_pinned", "contract": str(path)}))
        return 0
    except (ContractError, OSError, ValueError, KeyError) as exc:
        print(f"LAUNCH CONTRACT FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
