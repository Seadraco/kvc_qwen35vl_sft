#!/usr/bin/env python3
"""Download a pinned public HF checkpoint with upstream size/hash verification.

Uses HTTPS GET rather than relying on optional Hub HEAD response headers, which
some network proxies strip. No credentials, remote Python code, or TLS bypass.
Existing files are verified, never silently overwritten. Partial downloads resume.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path, PurePosixPath
import time
import urllib.parse
import urllib.request

DEFAULT_REVISION = "c202236235762e1c871ad0ccb60c8ee5ba337b9a"


def digest_file(path: Path, lfs: bool) -> str:
    digest = hashlib.sha256() if lfs else hashlib.sha1()
    if not lfs:
        digest.update(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch_file(item: dict, repo: str, revision: str, destination: Path) -> dict:
    relative = PurePosixPath(item["path"])
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Unsafe upstream filename: {relative}")
    destination = destination.resolve()
    target = destination.joinpath(*relative.parts)
    partial = target.with_name(target.name + ".incomplete")
    for candidate in (target, partial):
        if not candidate.resolve().is_relative_to(destination):
            raise ValueError(f"Download path escapes destination: {candidate}")
        if candidate.is_symlink():
            raise ValueError(f"Refusing symlink download target: {candidate}")
    size = item["size"]
    is_lfs = "lfs" in item
    expected = item["lfs"]["oid"] if is_lfs else item["oid"]
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.stat().st_size != size or digest_file(target, is_lfs) != expected:
            raise RuntimeError(f"Existing file failed verification; refusing overwrite: {target}")
        print(f"VERIFIED {relative}", flush=True)
    else:
        url = f"https://huggingface.co/{repo}/resolve/{revision}/{urllib.parse.quote(str(relative))}"
        for attempt in range(4):
            offset = partial.stat().st_size if partial.exists() else 0
            if offset == size:
                break
            if offset > size:
                raise RuntimeError(f"Oversized partial file: {partial}")
            headers = {"User-Agent": "kvc-qwen35-reproducible-download/1.0"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            try:
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=120) as response:
                    if offset and response.status != 206:
                        raise RuntimeError(f"Server did not honor resume Range for {relative}")
                    with partial.open("ab" if offset else "wb") as output:
                        while block := response.read(8 * 1024 * 1024):
                            output.write(block)
                if partial.stat().st_size != size:
                    raise RuntimeError(f"Incomplete response for {relative}")
                break
            except Exception as exc:
                if attempt == 3:
                    raise
                print(f"RETRY {relative}: {type(exc).__name__}: {exc}", flush=True)
                time.sleep(min(2 ** attempt, 8))
        if partial.stat().st_size != size or digest_file(partial, is_lfs) != expected:
            raise RuntimeError(f"Downloaded file failed upstream hash verification: {relative}")
        partial.rename(target)
        print(f"DOWNLOADED+VERIFIED {relative} ({size / 2**20:.1f} MiB)", flush=True)
    return {"path": str(relative), "size": size,
            "hash_algorithm": "sha256" if is_lfs else "git-blob-sha1", "hash": expected}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="Qwen/Qwen3.5-9B")
    parser.add_argument("--revision", default=DEFAULT_REVISION)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--metadata-only", action="store_true",
                        help="Fetch processor/tokenizer files without model tensors")
    args = parser.parse_args()
    if len(args.revision) != 40 or any(c not in "0123456789abcdef" for c in args.revision):
        parser.error("--revision must be an immutable 40-character commit SHA")
    endpoint = f"https://huggingface.co/api/models/{args.repo}/tree/{args.revision}?recursive=true"
    with urllib.request.urlopen(endpoint, timeout=60) as response:
        if response.headers.get("Link"):
            raise RuntimeError("Paginated repository listing: use huggingface_hub for this repository")
        listing = json.load(response)
    files = [item for item in listing if item["type"] == "file" and
             (Path(item["path"]).suffix in {".json", ".txt", ".jinja", ".safetensors"}
              or item["path"] in {"LICENSE", "README.md"})]
    if args.metadata_only:
        files = [item for item in files if not item["path"].endswith(".safetensors")]
    args.destination = args.destination.expanduser().resolve()
    print(json.dumps({"repo": args.repo, "revision": args.revision,
                      "destination": str(args.destination), "files": len(files),
                      "total_gib": sum(item["size"] for item in files) / 2**30}), flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(fetch_file, item, args.repo, args.revision, args.destination)
                   for item in files]
        verified = [future.result() for future in futures]
    manifest = {"repo": args.repo, "revision": args.revision, "files": verified}
    manifest_name = "metadata_manifest.json" if args.metadata_only else "download_manifest.json"
    (args.destination / manifest_name).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"MODEL_PATH={args.destination}", flush=True)


if __name__ == "__main__":
    main()
