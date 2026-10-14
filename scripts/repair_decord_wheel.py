#!/usr/bin/env python3
"""Repack the official Decord 0.6.0 wheel's incorrect internal Python ABI tag.

The published wheel filename is py3-none, but its WHEEL metadata says cp36-cp36m.
Its Python interface uses ctypes and the sole bundled extension is a C API shared
library, not a CPython extension. This script fixes packaging metadata only. It
does not change code, shared libraries, dependency checks, or runtime video backend.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import platform
from pathlib import Path
import subprocess
import sys
import urllib.request
import zipfile


UPSTREAM_URL = (
    "https://files.pythonhosted.org/packages/11/79/"
    "936af42edf90a7bd4e41a6cac89c913d4b47fa48a26b042d5129a9242ee3/"
    "decord-0.6.0-py3-none-manylinux2010_x86_64.whl"
)
UPSTREAM_SHA256 = "51997f20be8958e23b7c4061ba45d0efcd86bffd5fe81c695d0befee0d442976"
WHEEL_METADATA = "decord-0.6.0.dist-info/WHEEL"
RECORD = "decord-0.6.0.dist-info/RECORD"
OUTPUT_NAME = "decord-0.6.0-1kvc-py3-none-manylinux2010_x86_64.whl"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def repack(original: bytes) -> tuple[bytes, dict[str, object]]:
    if sha256(original) != UPSTREAM_SHA256:
        raise RuntimeError("Upstream wheel hash mismatch; refusing to patch different bytes")
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        entries = archive.infolist()
        files = {entry.filename: archive.read(entry.filename) for entry in entries}
    if len(files) != len(entries):
        raise RuntimeError("Duplicate ZIP members")
    binaries = [name for name in files if name.endswith((".so", ".pyd", ".dll"))]
    if binaries != ["decord/libdecord.so"]:
        raise RuntimeError(f"Unexpected binary interface: {binaries}")
    if b"PyInit_" in files["decord/libdecord.so"]:
        raise RuntimeError("Unexpected CPython extension marker in the C API library")
    if b"ctypes.CDLL" not in files["decord/_ffi/base.py"]:
        raise RuntimeError("Expected ctypes wrapper is missing")
    old_metadata = files[WHEEL_METADATA]
    old_tag = b"Tag: cp36-cp36m-manylinux2010_x86_64"
    new_tag = b"Tag: py3-none-manylinux2010_x86_64"
    if old_metadata.count(old_tag) != 1 or b"Build:" in old_metadata:
        raise RuntimeError("Unexpected WHEEL metadata; manual review required")
    files[WHEEL_METADATA] = old_metadata.replace(old_tag, new_tag).rstrip() + b"\nBuild: 1kvc\n\n"
    records = io.StringIO(newline="")
    writer = csv.writer(records, lineterminator="\n")
    for entry in entries:
        name = entry.filename
        if name == RECORD:
            continue
        payload = files[name]
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode().rstrip("=")
        writer.writerow([name, f"sha256={digest}", len(payload)])
    writer.writerow([RECORD, "", ""])
    files[RECORD] = records.getvalue().encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for entry in entries:
            archive.writestr(entry, files[entry.filename])
    patched = output.getvalue()
    with zipfile.ZipFile(io.BytesIO(original)) as before, zipfile.ZipFile(io.BytesIO(patched)) as after:
        changed = [name for name in files if before.read(name) != after.read(name)]
    if set(changed) != {WHEEL_METADATA, RECORD}:
        raise RuntimeError(f"Unexpected payload modifications: {changed}")
    return patched, {
        "package": "decord", "version": "0.6.0", "build": "1kvc",
        "reason": "Official py3-none wheel contains inconsistent cp36-cp36m WHEEL metadata",
        "upstream_url": UPSTREAM_URL, "upstream_sha256": UPSTREAM_SHA256,
        "wheel_filename": OUTPUT_NAME, "patched_sha256": sha256(patched),
        "old_tag": old_tag.decode().removeprefix("Tag: "),
        "new_tag": new_tag.decode().removeprefix("Tag: "),
        "changed_archive_members": sorted(changed),
        "unchanged_binary_sha256": sha256(files["decord/libdecord.so"]),
        "source_and_binary_payload_unchanged": True,
    }


def write_checked(path: Path, content: bytes) -> None:
    """An existing differing build is evidence, not something to overwrite."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise RuntimeError(f"Existing artifact differs; refusing to overwrite: {path}")
    else:
        path.write_bytes(content)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-wheel", type=Path, help="Offline copy of the exact official wheel")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "environment" / "wheels")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--video", type=Path, help="Optional one-frame CPU decode check after install")
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        parser.error("This reviewed repair applies only to Linux x86_64")
    if args.video and (not args.install or not args.video.is_file()):
        parser.error("--video requires --install and an existing media file")
    if args.original_wheel:
        original = args.original_wheel.read_bytes()
    else:
        with urllib.request.urlopen(UPSTREAM_URL, timeout=120) as response:
            original = response.read()
    patched, manifest = repack(original)
    wheel_path = args.output_dir.resolve() / OUTPUT_NAME
    manifest_path = args.output_dir.resolve() / "decord-wheel-repair.json"
    write_checked(wheel_path, patched)
    write_checked(manifest_path, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
    print(json.dumps(manifest, indent=2), flush=True)
    if args.install:
        subprocess.run([
            sys.executable, "-m", "pip", "install", "--no-cache-dir", "--no-deps",
            "--force-reinstall", str(wheel_path),
        ], check=True)
        import decord
        from importlib.metadata import distribution
        wheel_metadata = distribution("decord").read_text("WHEEL")
        if "Tag: py3-none-manylinux2010_x86_64" not in wheel_metadata:
            raise RuntimeError("Installed WHEEL metadata is not the repaired build")
        print(f"IMPORT_OK decord={decord.__version__}, python={sys.version.split()[0]}", flush=True)
        if args.video:
            reader = decord.VideoReader(str(args.video.resolve()), ctx=decord.cpu(0), num_threads=1)
            frame = reader[0].asnumpy()
            print(json.dumps({"cpu_decode": "passed", "frames": len(reader),
                              "first_frame_shape": list(frame.shape)}), flush=True)
    elif args.video:
        parser.error("--video requires --install")


if __name__ == "__main__":
    main()
