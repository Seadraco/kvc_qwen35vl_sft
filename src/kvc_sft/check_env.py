"""Preflight checks for the Qwen3.5 SFT runtime, every GPU and video decoder."""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.requirements import Requirement
from packaging.version import Version

from .contracts import ContractError, loads_strict


REQUIRED = {
    "torch": ">=2.6",
    "torchvision": ">=0.21",
    "transformers": ">=5.9,<6",
    "ms-swift": ">=4.5.3,<4.6",
    "qwen-vl-utils": "==0.0.14",
    "torchcodec": "==0.2.1",
    "av": "==18.1.0",
    "accelerate": "==1.14.0",
    "datasets": "==4.8.4",
    "modelscope": "==1.39.1",
    "peft": "==0.20.0",
    "trl": "==0.29.1",
    "jsonschema": "==4.26.0",
    "flash-linear-attention": ">=0.4.2",
}


def _requirements() -> dict[str, str]:
    """The environment requirements override compatibility floors when pinned."""
    result = dict(REQUIRED)
    root = Path(__file__).resolve().parents[2]
    visited: set[Path] = set()

    def read(path: Path) -> None:
        path = path.resolve()
        if path in visited or not path.is_file():
            return
        visited.add(path)
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.split(" #", 1)[0].strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("-r "):
                read(path.parent / line[3:].strip())
                continue
            if line.startswith("-"):
                continue
            try:
                requirement = Requirement(line)
            except ValueError:
                continue
            if requirement.marker is None or requirement.marker.evaluate():
                result[requirement.name] = str(requirement.specifier)

    read(root / "requirements.txt")
    read(root / "constraints-tested.txt")
    return result


def parse_physical_gpus(value: str) -> list[int]:
    fields = value.split(",")
    if any(not field.isdecimal() for field in fields):
        raise ValueError("physical GPUs must be comma-separated numeric indices")
    values = [int(field) for field in fields]
    if len(set(values)) != len(values):
        raise ValueError("physical GPU indices must be unique")
    return values


def _distribution_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError(f"required package is not installed: {name}") from exc


def _gpu_free_mib(index: int) -> tuple[str, int, int]:
    command = [
        "nvidia-smi",
        f"--id={index}",
        "--query-gpu=name,memory.free,utilization.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"cannot query physical GPU {index} with nvidia-smi") from exc
    fields = [field.strip() for field in result.stdout.strip().split(",")]
    if len(fields) != 3:
        raise RuntimeError(f"unexpected nvidia-smi output: {result.stdout!r}")
    return fields[0], int(fields[1]), int(fields[2])


def _decode_video(path: Path) -> dict[str, object]:
    os.environ.setdefault("FORCE_QWENVL_VIDEO_READER", "torchcodec")
    os.environ.setdefault("VIDEO_MAX_TOKEN_NUM", "64")
    os.environ.setdefault("VIDEO_MIN_TOKEN_NUM", "32")
    os.environ.setdefault("FPS_MAX_FRAMES", "8")
    try:
        # ms-swift applies these environment variables to qwen-vl-utils while
        # constructing the model processor.  Apply the same compatibility
        # patch here so preflight tests the exact training limits without
        # loading a multi-gigabyte model.
        from .torchcodec_compat import apply_patch
        from swift.model.models.qwen import compat_qwen_vl_utils, patch_qwen_vl_utils
        from qwen_vl_utils import process_vision_info
        from qwen_vl_utils import vision_process
    except ImportError as exc:
        raise RuntimeError("qwen_vl_utils cannot be imported") from exc
    apply_patch()
    compat_qwen_vl_utils(image_patch_size=16)
    patch_qwen_vl_utils(vision_process)
    messages = [{"role": "user", "content": [{"type": "video", "video": str(path)}]}]
    _, videos, kwargs = process_vision_info(
        messages,
        image_patch_size=16,
        return_video_kwargs=True,
        return_video_metadata=True,
    )
    if not videos or len(videos) != 1:
        raise RuntimeError("video preprocessor returned no video")
    video, metadata = videos[0]
    requested_backend = os.environ["FORCE_QWENVL_VIDEO_READER"]
    actual_backend = metadata.get("video_backend")
    if actual_backend != requested_backend:
        raise RuntimeError(
            f"requested video backend {requested_backend!r}, but qwen-vl-utils fell back to {actual_backend!r}"
        )
    return {
        "path": str(path),
        "tensor_shape": list(video.shape),
        "backend": actual_backend,
        "sampled_indices": [int(index) for index in metadata.get("frames_indices", [])],
        "video_kwargs": kwargs,
    }


def _first_dataset_video(dataset_path: Path) -> Path:
    try:
        with dataset_path.open("r", encoding="utf-8") as handle:
            first = next((line for line in handle if line.strip()), None)
    except OSError as exc:
        raise RuntimeError(f"cannot read dataset {dataset_path}: {exc}") from exc
    if first is None:
        raise RuntimeError(f"dataset is empty: {dataset_path}")
    try:
        row = loads_strict(first, what=f"first row of {dataset_path}")
    except ContractError as exc:
        raise RuntimeError(str(exc)) from exc
    videos = row.get("videos") if isinstance(row, dict) else None
    if not isinstance(videos, list) or not videos or not isinstance(videos[0], str):
        raise RuntimeError("first dataset row must contain a non-empty string 'videos' list")
    video_path = Path(videos[0]).expanduser()
    if not video_path.is_absolute():
        video_path = dataset_path.parent / video_path
    return video_path.resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-cuda", action="store_true")
    gpu = parser.add_mutually_exclusive_group()
    gpu.add_argument("--physical-gpu", type=int, default=None)
    gpu.add_argument("--physical-gpus", type=parse_physical_gpus)
    parser.add_argument("--min-free-gib", type=float, default=0)
    media = parser.add_mutually_exclusive_group()
    media.add_argument("--video", type=Path)
    media.add_argument("--dataset", type=Path, help="Decode the first video referenced by this JSONL")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report: dict[str, object] = {"python": sys.version.split()[0], "executable": sys.executable}
    errors: list[str] = []
    versions: dict[str, str] = {}
    for package, requirement in _requirements().items():
        try:
            installed = _distribution_version(package)
            versions[package] = installed
            if Version(installed) not in SpecifierSet(requirement):
                errors.append(f"{package}=={installed} does not satisfy {requirement}")
        except (RuntimeError, ValueError) as exc:
            errors.append(str(exc))
    report["packages"] = versions
    swift_spec = importlib.util.find_spec("swift")
    swift_file = Path(swift_spec.origin).resolve() if swift_spec and swift_spec.origin else None
    report["swift_module_path"] = str(swift_file) if swift_file else None
    if swift_file is None or not swift_file.is_relative_to(Path(sys.prefix).resolve()):
        errors.append(
            "ms-swift must be imported from the active conda environment, not a workspace/PYTHONPATH checkout; "
            f"resolved path: {swift_file}"
        )

    try:
        import torch

        report["torch_cuda_version"] = torch.version.cuda
        report["cuda_available"] = torch.cuda.is_available()
        report["bf16_supported"] = torch.cuda.is_available() and torch.cuda.is_bf16_supported()
        if args.require_cuda and not torch.cuda.is_available():
            errors.append("CUDA is required but torch.cuda.is_available() is false")
        if args.require_cuda and not torch.cuda.is_bf16_supported():
            errors.append("the selected CUDA device does not support bfloat16")
        if args.require_cuda:
            expected_count = len(args.physical_gpus) if args.physical_gpus else 1
            report["visible_cuda_devices"] = torch.cuda.device_count()
            if torch.cuda.device_count() != expected_count:
                errors.append(f"expected {expected_count} visible CUDA devices, got {torch.cuda.device_count()}")
            for index in range(torch.cuda.device_count()):
                with torch.cuda.device(index):
                    if not torch.cuda.is_bf16_supported():
                        errors.append(f"logical CUDA device {index} does not support bfloat16")
    except ImportError:
        errors.append("torch cannot be imported")

    physical_gpus = args.physical_gpus or [args.physical_gpu if args.physical_gpu is not None else 0]
    report["physical_gpus"] = []
    for index in physical_gpus:
        try:
            name, free_mib, utilization = _gpu_free_mib(index)
            report["physical_gpus"].append({
                "index": index, "name": name, "free_mib": free_mib, "utilization_percent": utilization,
            })
            if free_mib < args.min_free_gib * 1024:
                errors.append(f"GPU {index} has {free_mib / 1024:.1f} GiB free; at least {args.min_free_gib:.1f} GiB was requested")
        except RuntimeError as exc:
            if args.require_cuda:
                errors.append(str(exc))

    try:
        from transformers import Qwen3_5ForConditionalGeneration  # noqa: F401
        report["qwen35_model_class"] = "available"
    except Exception as exc:
        errors.append(f"Qwen3.5 model implementation cannot be imported: {exc}")

    if args.video or args.dataset:
        try:
            video_path = (
                args.video.expanduser().resolve()
                if args.video
                else _first_dataset_video(args.dataset.expanduser().resolve())
            )
            if not video_path.is_file():
                raise RuntimeError(f"video does not exist: {video_path}")
            report["video_decode"] = _decode_video(video_path)
        except Exception as exc:  # preflight must turn backend errors into a concise failure
            errors.append(f"video decode failed: {exc}")

    report["status"] = "failed" if errors else "ok"
    if errors:
        report["errors"] = errors
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
