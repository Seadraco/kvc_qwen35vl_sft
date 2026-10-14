"""Check host installation phases without downloading packages or using GPUs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


PROJECT = Path(__file__).resolve().parents[1]
WHEEL_NAME = "decord-0.6.0-1kvc-py3-none-manylinux2010_x86_64.whl"


@pytest.mark.parametrize("use_lock", ["0", "1"])
@pytest.mark.parametrize("fresh", [False, True])
def test_setup_install_order_and_native_isolation(tmp_path, use_lock, fresh):
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    (project / "environment/wheels").mkdir(parents=True)
    shutil.copy2(PROJECT / "scripts/setup_env.sh", project / "scripts/setup_env.sh")
    shutil.copy2(PROJECT / "environment/wheels" / WHEEL_NAME, project / "environment/wheels" / WHEEL_NAME)
    for path in ("requirements.txt", "constraints-tested.txt", "environment/requirements-lock.txt",
                 "environment/conda-explicit-linux-64.txt"):
        shutil.copy2(PROJECT / path, project / path)
    prefix = tmp_path / "conda/envs/kvc_qwen35_sft"
    calls = tmp_path / "calls.jsonl"
    python = tmp_path / "bootstrap-python"
    python.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
entry = {"args": args, "arch": os.environ.get("TORCH_CUDA_ARCH_LIST")}
if "--requirement" in args:
    entry["requirements"] = Path(args[args.index("--requirement") + 1]).read_text()
with open(os.environ["SETUP_TEST_CALLS"], "a") as stream:
    stream.write(json.dumps(entry) + "\\n")
''')
    python.chmod(0o755)
    if not fresh:
        (prefix / "bin").mkdir(parents=True)
        shutil.copy2(python, prefix / "bin/python")
    conda = tmp_path / "conda/bin/conda"
    conda.parent.mkdir(parents=True)
    conda.write_text('''#!/bin/bash
set -e
if [[ "$1" == info ]]; then
  dirname "$(dirname "$0")"
elif [[ "$1" == create ]]; then
  while [[ "$1" != --prefix ]]; do shift; done
  mkdir -p "$2/bin"
  cp "$SETUP_TEST_PYTHON_TEMPLATE" "$2/bin/python"
fi
''')
    conda.chmod(0o755)
    cuda = tmp_path / "cuda"
    (cuda / "bin").mkdir(parents=True)
    nvcc = cuda / "bin/nvcc"
    nvcc.write_text('#!/bin/bash\necho "Cuda compilation tools, release 12.4, V12.4.131"\n')
    nvcc.chmod(0o755)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("KVC_") and key not in {"CONDA_EXE", "TORCH_CUDA_ARCH_LIST"}}
    environment.update(KVC_CONDA_BIN=str(conda), KVC_SFT_ENV_PREFIX=str(prefix),
                       KVC_REQUIRE_CUDA="0", KVC_USE_LOCK=use_lock,
                       CUDA_HOME=str(cuda), SETUP_TEST_CALLS=str(calls),
                       SETUP_TEST_PYTHON_TEMPLATE=str(python))
    result = subprocess.run(["bash", str(project / "scripts/setup_env.sh")],
                            env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in calls.read_text().splitlines()]
    installs = [record for record in records if record["args"][:3] == ["-m", "pip", "install"]]
    torch, decord, ordinary, deepspeed, causal, editable = installs
    assert "--index-url" in torch["args"] and "torch==2.6.0+cu124" in torch["args"]
    assert decord["args"][-1].endswith(WHEEL_NAME) and "--force-reinstall" not in decord["args"]
    assert "--no-build-isolation" not in ordinary["args"]
    assert not any(line.startswith("deepspeed==") for line in ordinary["requirements"].splitlines())
    if use_lock == "0":
        assert "decord==0.6.0" in ordinary["requirements"]
    else:
        assert "decord==" not in ordinary["requirements"], "Repaired Decord is a separate lock phase"
    assert "--no-build-isolation" in deepspeed["args"] and "--no-deps" not in deepspeed["args"]
    assert "deepspeed==0.18.8" in deepspeed["args"] and "torch==2.6.0+cu124" in deepspeed["args"]
    assert "--no-build-isolation" in causal["args"] and "--no-deps" in causal["args"]
    assert causal["arch"] == "8.0;8.6;8.9;9.0"
    assert "--editable" in editable["args"] and "--no-deps" in editable["args"]
    assert "GPU checks skipped" in result.stdout
    assert not any("--require-cuda" in record["args"] for record in records)


def test_setup_uses_verified_tracked_decord_wheel():
    manifest = json.loads((PROJECT / "environment/wheels/decord-wheel-repair.json").read_text())
    digest = hashlib.sha256((PROJECT / "environment/wheels" / WHEEL_NAME).read_bytes()).hexdigest()
    assert digest == manifest["patched_sha256"]
    script = (PROJECT / "scripts/setup_env.sh").read_text()
    assert digest in script and "sha256sum -c -" in script
    assert "repair_decord_wheel.py\" --install" not in script
