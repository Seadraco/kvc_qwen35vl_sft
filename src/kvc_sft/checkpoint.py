"""Resolve an ms-swift LoRA output directory to a concrete adapter checkpoint."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from .contracts import ContractError


_CHECKPOINT_PATTERN = re.compile(r"^checkpoint-(\d+)$")


def resolve_adapter(path: str | Path) -> Path:
    candidate = Path(path).expanduser().resolve()
    if (candidate / "adapter_config.json").is_file():
        return candidate
    if not candidate.is_dir():
        raise ContractError(f"adapter/output directory does not exist: {candidate}")

    checkpoints: list[tuple[int, Path]] = []
    for child in candidate.iterdir():
        match = _CHECKPOINT_PATTERN.match(child.name)
        if match and (child / "adapter_config.json").is_file():
            checkpoints.append((int(match.group(1)), child))
    if checkpoints:
        return max(checkpoints)[1]

    # A production run normally has one versioned subdirectory because
    # add_version=true. Search those without silently choosing between runs.
    nested: list[Path] = []
    for child in candidate.iterdir():
        if not child.is_dir():
            continue
        try:
            nested.append(resolve_adapter(child))
        except ContractError:
            pass
    unique = sorted(set(nested))
    if len(unique) == 1:
        return unique[0]
    if len(unique) > 1:
        raise ContractError(
            f"multiple adapter runs found below {candidate}; pass one concrete run/checkpoint: "
            + ", ".join(str(item) for item in unique)
        )
    raise ContractError(f"no adapter_config.json or checkpoint-* adapter found below {candidate}")


def resolve_resume_checkpoint(path: str | Path, world_size: int = 2) -> Path:
    """Require full DDP optimizer/scheduler/RNG state, not just adapter weights."""
    if world_size < 1:
        raise ContractError("resume world_size must be positive")
    checkpoint = resolve_adapter(path)
    required = ["trainer_state.json", "optimizer.pt", "scheduler.pt"]
    required += ["rng_state.pth"] if world_size == 1 else [f"rng_state_{rank}.pth" for rank in range(world_size)]
    missing = [name for name in required if not (checkpoint / name).is_file()]
    if not any(checkpoint.glob("adapter_model.*")):
        missing.append("adapter_model.*")
    if missing:
        raise ContractError(f"checkpoint cannot resume complete DDP state; missing {missing}: {checkpoint}")
    state = json.loads((checkpoint / "trainer_state.json").read_text(encoding="utf-8"))
    if not isinstance(state.get("global_step"), int) or state["global_step"] < 1:
        raise ContractError("resume checkpoint has no positive completed global_step")
    rng_files = list(checkpoint.glob("rng_state_*.pth"))
    if world_size > 1 and len(rng_files) != world_size:
        raise ContractError("resume world_size differs from checkpoint RNG state; retain the same GPU count")
    return checkpoint


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--world-size", type=int, default=2)
    args = parser.parse_args()
    try:
        print(resolve_resume_checkpoint(args.path, args.world_size) if args.resume else resolve_adapter(args.path))
        return 0
    except (ContractError, OSError, ValueError) as exc:
        print(f"CHECKPOINT FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
