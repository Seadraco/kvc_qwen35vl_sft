"""Write and verify the immutable preprocessing/model contract for a LoRA run."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .checkpoint import resolve_adapter
from .compare_datasets import _contract
from .contracts import ContractError, TEMPLATE_CONTRACT, loads_strict


MANIFEST_NAME = "kvc_run_manifest.json"
MANIFEST_VERSION = 4


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_bundle(model_dir: Path, *, adapter: bool) -> dict[str, Any]:
    """Hash every file that can affect model/adapter loading.

    Model cards, license documents and the explicitly named download proof
    files are excluded: they cannot alter inference and may differ between a
    verified HTTPS download and a Hugging Face snapshot of identical weights.
    For a base model, all remaining snapshot files are pinned, including every
    config, tokenizer, weight, chat template and Python source file. For
    an adapter, pin the adapter weights plus every JSON file consumed by PEFT or
    ms-swift (including adapter_config.json and args.json).
    """

    def stable_file(path: Path) -> bool:
        relative = path.relative_to(model_dir)
        return not any(part in {".cache", ".git", ".locks"} for part in relative.parts) and not path.name.endswith(
            ".lock"
        )

    if adapter:
        files = sorted(
            path
            for path in model_dir.rglob("*")
            if path.is_file()
            and stable_file(path)
            and (path.name.startswith("adapter_model.") or path.suffix == ".json")
        )
        if not any(path.name.startswith("adapter_model.") for path in files):
            raise ContractError(f"adapter weights are missing from {model_dir}")
        if not (model_dir / "adapter_config.json").is_file():
            raise ContractError(f"adapter config is missing from {model_dir}")
    else:
        files = sorted(
            path
            for path in model_dir.rglob("*")
            if path.is_file()
            and stable_file(path)
            and path.name not in {
                "README.md", "README", ".gitattributes", "LICENSE", "LICENSE.txt", "LICENSE.md",
                "download_manifest.json", "metadata_manifest.json",
            }
        )
        if not any(path.suffix in {".safetensors", ".bin"} for path in files):
            raise ContractError(f"base-model weights are missing from {model_dir}")
        if not (model_dir / "config.json").is_file():
            raise ContractError(f"base-model config is missing from {model_dir}")
    if not files:
        raise ContractError(f"load bundle is empty: {model_dir}")

    digest = hashlib.sha256()
    file_hashes: dict[str, str] = {}
    for path in files:
        relative = path.relative_to(model_dir).as_posix()
        file_hash = _sha256(path)
        file_hashes[relative] = file_hash
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(file_hash.encode("ascii") + b"\0")
    return {"sha256": digest.hexdigest(), "files": file_hashes}


def _git_commit(repo: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise ContractError(f"cannot resolve git commit for {repo}") from exc
    return result.stdout.strip()


def _swift_identity() -> dict[str, str]:
    """Fingerprint the installed framework, never an unrelated source checkout."""
    spec = importlib.util.find_spec("swift")
    if spec is None or spec.origin is None:
        raise ContractError("installed ms-swift package cannot be located")
    package_dir = Path(spec.origin).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(package_dir.rglob("*.py")):
        digest.update(path.relative_to(package_dir).as_posix().encode() + b"\0")
        digest.update(_sha256(path).encode() + b"\0")
    return {
        "version": importlib.metadata.version("ms-swift"),
        "source_sha256": digest.hexdigest(),
        "source_path": str(package_dir),
    }


def _run_dir(path: Path) -> tuple[Path, Path]:
    adapter = resolve_adapter(path)
    return adapter.parent, adapter


def _feature_info(dataset: Path) -> dict[str, Any]:
    schema, paths, _, _ = _contract(dataset)
    return {
        "schema": schema,
        "count": len(paths),
        "path_sha256": hashlib.sha256("\0".join(paths).encode("utf-8")).hexdigest(),
    }


def _sampling_contract(
    *,
    video_backend: str,
    fps_max_frames: int,
    video_min_token_num: int,
    video_max_token_num: int,
    max_ratio: int,
    frame_factor: int,
    fps: float,
    fps_min_frames: int,
    spatial_merge_size: int,
    model_seq_len: int,
) -> dict[str, Any]:
    values = {
        "backend": video_backend,
        "fps_max_frames": fps_max_frames,
        "video_min_token_num": video_min_token_num,
        "video_max_token_num": video_max_token_num,
        "max_ratio": max_ratio,
        "frame_factor": frame_factor,
        "fps": fps,
        "fps_min_frames": fps_min_frames,
        "spatial_merge_size": spatial_merge_size,
        "model_seq_len": model_seq_len,
    }
    if not video_backend:
        raise ContractError("video backend cannot be empty")
    numeric = {key: value for key, value in values.items() if key != "backend"}
    if any(value <= 0 for value in numeric.values()):
        raise ContractError("all video sampling limits must be positive")
    if video_min_token_num > video_max_token_num:
        raise ContractError("video_min_token_num cannot exceed video_max_token_num")
    if fps_min_frames > fps_max_frames:
        raise ContractError("fps_min_frames cannot exceed fps_max_frames")
    return values


def _sampling_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return _sampling_contract(
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
    )


def _load_manifest(path: Path) -> tuple[Path, dict[str, Any]]:
    run_dir, _ = _run_dir(path)
    manifest_path = run_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ContractError(f"run manifest is missing: {manifest_path}")
    manifest = loads_strict(manifest_path.read_text(encoding="utf-8"), what="run manifest")
    if not isinstance(manifest, dict) or manifest.get("manifest_version") != MANIFEST_VERSION:
        raise ContractError(f"unsupported run manifest: {manifest_path}")
    return manifest_path, manifest


def _write(args: argparse.Namespace) -> int:
    run_dir, adapter = _run_dir(args.run.expanduser().resolve())
    train_dataset = args.train_dataset.expanduser().resolve()
    schema_path = args.schema.expanduser().resolve()
    swift_runtime = _swift_identity()
    adapter_config = loads_strict(
        (adapter / "adapter_config.json").read_text(encoding="utf-8"),
        what="adapter config",
    )
    base_path_raw = adapter_config.get("base_model_name_or_path")
    base_path = Path(base_path_raw) if isinstance(base_path_raw, str) else None
    if base_path is None or not base_path.is_dir():
        raise ContractError("adapter does not point to an available base-model snapshot")
    base_bundle = _load_bundle(base_path, adapter=False)
    adapter_bundle = _load_bundle(adapter, adapter=True)
    feature = _feature_info(train_dataset)
    if feature["schema"] != args.feature_schema or feature["count"] != args.feature_count:
        raise ContractError("launcher feature settings disagree with the rendered training dataset")
    training_args = loads_strict((run_dir / "args.json").read_text(encoding="utf-8"), what="training args")
    for key, expected in TEMPLATE_CONTRACT.items():
        if training_args.get(key) != expected:
            raise ContractError(f"actual training args disagree with template contract for {key}")
    launch_path = run_dir / "kvc_launch_contract.json"
    launch_contract = loads_strict(launch_path.read_text(encoding="utf-8"), what="launch contract")
    events_path = run_dir / "kvc_resume_events.json"
    resume_events = loads_strict(events_path.read_text(encoding="utf-8"), what="resume events") if events_path.is_file() else []
    if resume_events and resume_events[-1].get("event") == "resume_requested":
        trainer_state = loads_strict((adapter / "trainer_state.json").read_text(encoding="utf-8"), what="trainer state")
        if trainer_state["global_step"] <= resume_events[-1]["completed_steps"]:
            raise ContractError("resume did not advance the optimizer global_step")
        resume_events.append({"event": "resume_completed", "checkpoint": adapter.name, "global_step": trainer_state["global_step"]})
        events_path.write_text(json.dumps(resume_events, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "model": {
            "id": args.model,
            "revision": args.model_revision,
            "base_path_at_train": str(base_path),
            "load_bundle": base_bundle,
        },
        "adapter": {
            "checkpoint": adapter.name,
            "load_bundle": adapter_bundle,
        },
        "action_schema": {
            "path_at_train": str(schema_path),
            "sha256": _sha256(schema_path),
        },
        "features": feature,
        "video_sampling": _sampling_from_args(args),
        "training": {
            "max_length": args.max_length,
            "token_audit_samples": args.token_audit_samples,
            "template_contract": TEMPLATE_CONTRACT,
            "args": training_args,
            "launch_contract": launch_contract,
            "resume_events": resume_events,
            "per_rank_runtime": [
                loads_strict(path.read_text(encoding="utf-8"), what="rank runtime")
                for path in sorted(run_dir.glob("runtime_rank_*.json"))
            ],
        },
        "data": {
            "train_path": str(train_dataset),
            "train_sha256": _sha256(train_dataset),
            "val_path": str(args.val_dataset.expanduser().resolve()) if args.val_dataset else None,
            "val_sha256": _sha256(args.val_dataset.expanduser().resolve()) if args.val_dataset else None,
        },
        "runtime": {
            "ms_swift_version": importlib.metadata.version("ms-swift"),
            "ms_swift_source_sha256": swift_runtime["source_sha256"],
            "ms_swift_source_path": swift_runtime["source_path"],
            "torch": importlib.metadata.version("torch"),
            "transformers": importlib.metadata.version("transformers"),
            "qwen_vl_utils": importlib.metadata.version("qwen-vl-utils"),
            "torchcodec": importlib.metadata.version("torchcodec"),
            "peft": importlib.metadata.version("peft"),
            "python": sys.version.split()[0],
        },
    }
    manifest_path = run_dir / MANIFEST_NAME
    temporary = manifest_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(manifest_path)
    print(json.dumps({"status": "written", "manifest": str(manifest_path)}, ensure_ascii=False))
    return 0


def verify_contract(
    *,
    run: Path,
    dataset: Path,
    schema: Path,
    model: str,
    model_revision: str,
    video_backend: str,
    fps_max_frames: int,
    video_min_token_num: int,
    video_max_token_num: int,
    max_ratio: int,
    frame_factor: int,
    fps: float,
    fps_min_frames: int,
    spatial_merge_size: int,
    model_seq_len: int,
    swift_repo: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Verify a run before any model is loaded, including direct Python inference."""

    run = run.expanduser().resolve()
    manifest_path, manifest = _load_manifest(run)
    _, adapter = _run_dir(run)
    dataset = dataset.expanduser().resolve()
    schema_path = schema.expanduser().resolve()
    feature = _feature_info(dataset)
    sampling = _sampling_contract(
        video_backend=video_backend,
        fps_max_frames=fps_max_frames,
        video_min_token_num=video_min_token_num,
        video_max_token_num=video_max_token_num,
        max_ratio=max_ratio,
        frame_factor=frame_factor,
        fps=fps,
        fps_min_frames=fps_min_frames,
        spatial_merge_size=spatial_merge_size,
        model_seq_len=model_seq_len,
    )
    expected = {
        "model.revision": (manifest["model"]["revision"], model_revision),
        "adapter.checkpoint": (manifest["adapter"]["checkpoint"], adapter.name),
        "adapter.load_bundle": (
            manifest["adapter"]["load_bundle"],
            _load_bundle(adapter, adapter=True),
        ),
        "action_schema.sha256": (manifest["action_schema"]["sha256"], _sha256(schema_path)),
        "features.schema": (manifest["features"]["schema"], feature["schema"]),
        "features.count": (manifest["features"]["count"], feature["count"]),
        "features.path_sha256": (manifest["features"]["path_sha256"], feature["path_sha256"]),
        "runtime.ms_swift_source_sha256": (
            manifest["runtime"]["ms_swift_source_sha256"],
            _swift_identity()["source_sha256"],
        ),
        "runtime.ms_swift_version": (
            manifest["runtime"]["ms_swift_version"],
            importlib.metadata.version("ms-swift"),
        ),
        "runtime.torch": (manifest["runtime"]["torch"], importlib.metadata.version("torch")),
        "runtime.transformers": (
            manifest["runtime"]["transformers"],
            importlib.metadata.version("transformers"),
        ),
        "runtime.qwen_vl_utils": (
            manifest["runtime"]["qwen_vl_utils"],
            importlib.metadata.version("qwen-vl-utils"),
        ),
        "runtime.torchcodec": (
            manifest["runtime"]["torchcodec"],
            importlib.metadata.version("torchcodec"),
        ),
        "runtime.peft": (manifest["runtime"]["peft"], importlib.metadata.version("peft")),
        "training.template_contract": (manifest["training"]["template_contract"], TEMPLATE_CONTRACT),
    }
    # Moving identical model weights to another mount on H800 is supported.
    # A local override is accepted only after all load-bundle hashes match.
    model_override = Path(model).expanduser()
    if not model_override.is_dir():
        expected["model.id"] = (manifest["model"]["id"], model)
    expected.update(
        {
            f"video_sampling.{key}": (manifest["video_sampling"][key], value)
            for key, value in sampling.items()
        }
    )
    mismatches = [
        f"{name}: trained={trained!r}, inference={current!r}"
        for name, (trained, current) in expected.items()
        if trained != current
    ]
    if mismatches:
        raise ContractError("run contract mismatch: " + "; ".join(mismatches))
    base_path_raw = str(model_override.resolve()) if model_override.is_dir() else manifest["model"].get("base_path_at_train")
    trained_bundle = manifest["model"].get("load_bundle")
    if not base_path_raw or not trained_bundle or not Path(base_path_raw).is_dir():
        raise ContractError(
            "the exact base-model snapshot recorded at training is unavailable; "
            "restore it before inference"
        )
    current_bundle = _load_bundle(Path(base_path_raw), adapter=False)
    if current_bundle != trained_bundle:
        raise ContractError("cached base-model load bundle differs from the training manifest")
    manifest["resolved_base_path"] = base_path_raw
    return manifest_path, manifest


def _check(args: argparse.Namespace) -> int:
    manifest_path, _ = verify_contract(
        run=args.run,
        dataset=args.dataset,
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
    print(json.dumps({"status": "ok", "manifest": str(manifest_path)}, ensure_ascii=False))
    return 0


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--model-revision", required=True)
    parser.add_argument("--video-backend", required=True)
    parser.add_argument("--fps-max-frames", required=True, type=int)
    parser.add_argument("--video-min-token-num", required=True, type=int)
    parser.add_argument("--video-max-token-num", required=True, type=int)
    parser.add_argument("--max-ratio", required=True, type=int)
    parser.add_argument("--frame-factor", required=True, type=int)
    parser.add_argument("--fps", required=True, type=float)
    parser.add_argument("--fps-min-frames", required=True, type=int)
    parser.add_argument("--spatial-merge-size", required=True, type=int)
    parser.add_argument("--model-seq-len", required=True, type=int)
    parser.add_argument("--swift-repo", type=Path, help="Deprecated compatibility option; uses installed ms-swift")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    writer = subparsers.add_parser("write")
    _common(writer)
    writer.add_argument("--train-dataset", required=True, type=Path)
    writer.add_argument("--val-dataset", type=Path)
    writer.add_argument("--feature-schema", required=True)
    writer.add_argument("--feature-count", required=True, type=int)
    writer.add_argument("--max-length", required=True, type=int)
    writer.add_argument("--token-audit-samples", required=True, type=int)
    checker = subparsers.add_parser("check")
    _common(checker)
    checker.add_argument("--dataset", required=True, type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return _write(args) if args.command == "write" else _check(args)
    except (ContractError, OSError, KeyError, ValueError) as exc:
        print(f"RUN MANIFEST FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
