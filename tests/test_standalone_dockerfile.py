"""Audit the project's own image recipe without building it or using GPUs."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tomllib

import pytest
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

PROJECT = Path(__file__).resolve().parents[1]
DOCKERFILE = PROJECT / "Dockerfile"


def instructions():
    pending = []
    for line in DOCKERFILE.read_text().splitlines():
        if not pending and (not line.strip() or line.lstrip().startswith("#")):
            continue
        pending.append(line[:-1] if line.endswith("\\") else line)
        if not line.endswith("\\"):
            yield " ".join(pending)
            pending = []
    assert not pending, "Unterminated Dockerfile continuation"


def embedded_scripts():
    files = {}
    for instruction in instructions():
        if instruction.startswith("RUN printf '%s\\n'"):
            words = shlex.split(instruction[4:])
            end = words.index(">")
            files[words[end + 1]] = "\n".join(words[2:end]) + "\n"
    return files


def inline_python():
    for instruction in instructions():
        if instruction.startswith("RUN "):
            words = shlex.split(instruction[4:])
            for index, word in enumerate(words[:-1]):
                if word == "-c" and index and words[index - 1] in {"python", "python3"}:
                    yield words[index + 1]


def direct_pins():
    pins = {}
    for instruction in instructions():
        if "-m pip install" not in instruction:
            continue
        for word in shlex.split(instruction):
            if "==" not in word:
                continue
            try:
                requirement = Requirement(word)
            except InvalidRequirement:
                continue
            name = canonicalize_name(requirement.name)
            assert name not in pins or pins[name] == requirement, f"Conflicting pin: {name}"
            pins[name] = requirement
    return pins


def capture_launcher(source, args, *, password=True):
    env = {"PATH": "/usr/bin:/bin", "KVC_SFT_ENV_PREFIX": "/opt/conda/envs/kvc_qwen35_sft"}
    if password:
        env["PASSWORD"] = "test-only-placeholder"
    source = "exec() { printf '%s\\0' \"$@\"; };\n" + source
    return subprocess.run(["bash", "-c", source, "launcher-test", *args], env=env, capture_output=True)


def test_canonical_recipe_is_a_single_file_company_build():
    text = DOCKERFILE.read_text()
    assert text.startswith("FROM registry.corp.kuaishou.com/kml/v_cuda:12.4.0-devel-ubuntu22.04\n")
    assert sum(i.startswith("FROM ") for i in instructions()) == 1
    assert all(i.split()[0] not in {"COPY", "ADD"} for i in instructions())
    assert "python3 -m venv" not in text and "/opt/venv/" not in text
    assert "codec-video-prep" not in text and "lmms-eval" not in text
    assert sum(i.startswith("RUN printf ") for i in instructions()) == 3


def test_all_run_and_embedded_script_syntax():
    for instruction in instructions():
        if instruction.startswith("RUN "):
            result = subprocess.run(["bash", "-n"], input=instruction[4:], text=True, capture_output=True)
            assert result.returncode == 0, result.stderr
    for source in inline_python():
        ast.parse(source)
    scripts = embedded_scripts()
    assert set(scripts) == {"/etc/profile.d/kvc-qwen35-sft.sh", "/usr/local/bin/nvitop", "/usr/local/bin/run-code-server"}
    for source in scripts.values():
        assert subprocess.run(["bash", "-n"], input=source, text=True, capture_output=True).returncode == 0
    for instruction in instructions():
        if instruction.startswith("RUN printf '%s\\n'"):
            words = shlex.split(instruction[4:])
            end = words.index(">")
            result = subprocess.run(["/usr/bin/printf", *words[1:end]], text=True, capture_output=True, check=True)
            assert result.stdout == scripts[words[end + 1]]


@pytest.mark.parametrize("filename", ["requirements.txt", "constraints-tested.txt"])
def test_every_project_requirement_is_installed_and_compatible(filename):
    pins = direct_pins()
    for raw in (PROJECT / filename).read_text().splitlines():
        if not raw.strip() or raw.startswith("#"):
            continue
        requirement = Requirement(raw)
        name = canonicalize_name(requirement.name)
        assert name in pins, f"Missing {filename}: {requirement}"
        exact = list(pins[name].specifier)
        assert len(exact) == 1 and exact[0].operator == "=="
        assert requirement.specifier.contains(exact[0].version, prereleases=True), (requirement, pins[name])


def test_project_external_imports_have_image_dependencies():
    aliases = {"swift": "ms-swift", "yaml": "pyyaml", "fla": "flash-linear-attention",
               "PIL": "pillow", "cv2": "opencv-python-headless", "pynvml": "nvidia-ml-py",
               "conda_pack": "conda-pack", "causal_conv1d": "causal-conv1d"}
    pins = direct_pins()
    for folder in ("src", "scripts"):
        for path in (PROJECT / folder).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                    names = [node.module.split(".")[0]]
                for name in names:
                    if name in sys.stdlib_module_names or name in {"__future__", "kvc_sft"}:
                        continue
                    assert canonicalize_name(aliases.get(name, name)) in pins, (path, name)


def test_conda_name_bootstrap_and_paths():
    text = DOCKERFILE.read_text()
    create = next(i for i in instructions() if "conda create" in i)
    for expected in ("--name kvc_qwen35_sft", "--override-channels --channel conda-forge",
                     "python=3.12.15", "pip=26.2.1", "setuptools=82.0.1", "wheel=0.48.0",
                     "ffmpeg=7.1.1", "pkg-config", "sha256sum -c -"):
        assert expected in create
    assert 'PATH="/opt/conda/envs/kvc_qwen35_sft/bin:/opt/conda/bin:/usr/local/cuda/bin:${PATH}"' in text
    assert "ENV PATH=/opt/node/bin:${PATH}" in text
    hook = embedded_scripts()["/etc/profile.d/kvc-qwen35-sft.sh"]
    assert ". /opt/conda/etc/profile.d/conda.sh" in hook and "conda activate kvc_qwen35_sft" in hook
    assert 'export PATH=/opt/node/bin:/usr/local/cuda/bin:"$PATH"' in hook
    assert "'. /etc/profile.d/kvc-qwen35-sft.sh' >> /etc/bash.bashrc" in text
    assert "/bin/bash -lc" in text and "/bin/bash -ic" in text
    assert 'command -v conda swift torchrun nvcc ffprobe node npm nvitop run-code-server' in text


def test_toolchain_preflight_and_native_build_order():
    runs = [i for i in instructions() if i.startswith("RUN ")]
    for expected in ("source /etc/os-release", 'test "$ID" = ubuntu', 'test "$VERSION_ID" = 22.04',
                     "x86_64", "/usr/local/cuda/bin/nvcc", "release 12.4,"):
        assert expected in runs[0]
    assert "apt-get" not in runs[0] and "nvidia-smi" not in runs[0]
    torch = next(i for i in runs if "--index-url https://download.pytorch.org/whl/cu124" in i)
    core = next(i for i in runs if "ms-swift==4.5.3" in i)
    deepspeed = next(i for i in runs if "deepspeed==0.18.8" in i)
    causal = next(i for i in runs if "CAUSAL_CONV1D_FORCE_BUILD=TRUE" in i)
    repair = next(i for i in runs if "Decord upstream SHA256 mismatch" in i)
    assert runs.index(torch) < runs.index(repair) < runs.index(core) < runs.index(deepspeed) < runs.index(causal)
    assert "--no-build-isolation --no-deps" in causal
    assert "--no-binary causal-conv1d causal-conv1d==1.6.0" in causal
    assert "unset CAUSAL_CONV1D_FORCE_CXX11_ABI" in causal


def test_pip_isolation_is_scoped_and_torch_versions_stay_protected():
    pip_runs = [i for i in instructions() if i.startswith("RUN ") and "-m pip install" in i]
    core = next(i for i in pip_runs if "ms-swift==4.5.3" in i)
    deepspeed = next(i for i in pip_runs if "deepspeed==0.18.8" in i)
    non_isolated = [i for i in pip_runs if "--no-build-isolation" in i]
    assert len(non_isolated) == 2
    assert all("deepspeed==0.18.8" in i or "CAUSAL_CONV1D_FORCE_BUILD=TRUE" in i for i in non_isolated)
    assert "--no-build-isolation" not in core and "deepspeed==" not in core
    assert "torch==2.6.0+cu124" in core and "torchvision==0.21.0+cu124" in core
    assert "torch==2.6.0+cu124" in deepspeed and "nvidia-ml-py==13.595.45" in deepspeed
    assert "--no-deps" not in deepspeed, "DeepSpeed runtime dependencies must still be resolved"
    assert all("--force-reinstall" not in i and "--ignore-installed" not in i for i in pip_runs)


def test_ninja_has_only_one_explicit_install_source():
    runs = [i for i in instructions() if i.startswith("RUN ")]
    apt = next(i for i in runs if "apt-get install" in i)
    conda = next(i for i in runs if "conda create" in i)
    assert "ninja-build" not in shlex.split(apt)
    assert not any(word == "ninja" or word.startswith("ninja=") for word in shlex.split(conda))
    installers = [i for i in runs if "-m pip install" in i and "ninja==" in i]
    assert len(installers) == 1 and "ninja==1.13.2" in installers[0]
    causal = next(i for i in runs if "CAUSAL_CONV1D_FORCE_BUILD=TRUE" in i)
    assert runs.index(installers[0]) < runs.index(causal)


def test_decord_repaired_wheel_is_installed_once_before_dependency_resolution():
    runs = [i for i in instructions() if i.startswith("RUN ")]
    repairs = [i for i in runs if "Decord upstream SHA256 mismatch" in i]
    assert len(repairs) == 1
    repair = repairs[0]
    core = next(i for i in runs if "ms-swift==4.5.3" in i)
    assert runs.index(repair) < runs.index(core)
    assert "decord==0.6.0" in core, "Keep Decord's version guard after its repaired wheel is installed"
    assert "-m pip install --no-cache-dir --no-deps" in repair
    assert "--force-reinstall" not in repair
    assert "pip check" not in repair, "Defer dependency checks until the complete environment is installed"


def test_decord_repair_rejects_tampered_wheel(tmp_path):
    source = next(s for s in inline_python() if "Decord upstream SHA256 mismatch" in s)
    original, output = tmp_path / "tampered.whl", tmp_path / "patched.whl"
    original.write_bytes(b"not the reviewed upstream wheel")
    source = source.replace("/tmp/decord-upstream.whl", str(original))
    source = source.replace("/tmp/decord-0.6.0-1kvc-py3-none-manylinux2010_x86_64.whl", str(output))
    result = subprocess.run([sys.executable, "-c", source], text=True, capture_output=True)
    assert result.returncode != 0 and "Decord upstream SHA256 mismatch" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("args,port,workspace", [([], "16530", "/workspace"),
                         (["15248", "."], "15248", "."),
                         (["15248", "/path with spaces"], "15248", "/path with spaces")])
def test_code_server_requested_directory_and_history(args, port, workspace):
    result = capture_launcher(embedded_scripts()["/usr/local/bin/run-code-server"], args)
    assert result.returncode == 0
    assert result.stdout.decode().split("\0")[:-1] == ["code-server", "--bind-addr", f"0.0.0.0:{port}",
            "--auth", "password", "--disable-telemetry", "--ignore-last-opened", workspace]


def test_code_server_requires_password():
    result = capture_launcher(embedded_scripts()["/usr/local/bin/run-code-server"], [], password=False)
    assert result.returncode == 1 and b"code-server\0" not in result.stdout


def test_nvitop_is_global_without_an_extra_environment():
    pins = direct_pins()
    assert pins["nvitop"].specifier.contains("1.7.1") and pins["nvidia-ml-py"].specifier.contains("13.595.45")
    source = embedded_scripts()["/usr/local/bin/nvitop"]
    assert "unset PYTHONPATH PYTHONHOME" in source
    assert '${KVC_SFT_ENV_PREFIX}/lib:/usr/local/nvidia/lib:/usr/local/nvidia/lib64' in source
    result = capture_launcher(source, ["--version"])
    assert result.returncode == 0
    assert result.stdout.decode().split("\0")[:-1] == ["/opt/conda/envs/kvc_qwen35_sft/bin/nvitop", "--version"]


def test_gpu_free_build_checks_decode_real_frames():
    text = DOCKERFILE.read_text()
    for source in inline_python():
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("fla.ops")
        assert "init_process_group" not in source and "torch.cuda.set_device" not in source
    for expected in ("sys.version_info[:2] == (3, 12)", "torch.__version__ == '2.6.0+cu124'",
                     "not torch.compiled_with_cxx11_abi()", "torch.distributed.is_nccl_available()",
                     "Qwen3_5ForConditionalGeneration", "device='cpu'", "len(decoder) == 4",
                     "decoder[0].shape", "reader[0].asnumpy().shape", "python3 -m pip check"):
        assert expected in text
    assert "ffmpeg -version | head" not in text
    assert "torch._C._GLIBCXX_USE_CXX11_ABI" not in text


def test_native_video_and_distributed_support_libraries():
    text = DOCKERFILE.read_text()
    for name in ("gcc-11", "g++-11", "cmake", "pkg-config", "git-lfs",
                 "libavcodec-dev", "libavformat-dev", "libavutil-dev", "libswscale-dev",
                 "libswresample-dev", "libaio-dev", "libnuma-dev", "libibverbs1", "librdmacm1"):
        assert name in text
    assert "DS_BUILD_OPS=0" in text and "FORCE_QWENVL_VIDEO_READER=torchcodec" in text


def test_sdpa_config_does_not_require_external_flash_attn():
    import yaml
    for name in ("train_lora.yaml", "smoke_lora.yaml"):
        assert yaml.safe_load((PROJECT / "configs" / name).read_text())["attn_impl"] == "sdpa"
    assert "flash-attn" not in direct_pins()


def test_renderer_targets_this_project_not_the_parent_directory():
    result = subprocess.run([sys.executable, str(PROJECT / "docker/render_singlefile.py")],
                            capture_output=True, text=True, check=True)
    assert result.stdout == DOCKERFILE.read_text()


def test_source_installer_preserves_dependencies_and_rejects_root():
    text = (PROJECT / "scripts/install_project.sh").read_text()
    assert "--no-deps --no-build-isolation --editable" in text and "Conda base" in text
    result = subprocess.run(["bash", str(PROJECT / "scripts/install_project.sh")],
                            env={"PATH": "/usr/bin:/bin", "KVC_SFT_ENV_PREFIX": "/"}, capture_output=True)
    assert result.returncode == 1


def test_current_project_has_only_relevant_model_and_image_assets():
    obsolete = (
        "docs/legacy", "docker/legacy", "reports/SMOKE_TEST.md",
        "reports/infer_sample_0.json", "reports/infer_sample_1.json",
        "reports/DOCKERFILE_VALIDATION.json", "reports/DOCKERFILE_V4_VALIDATION.json",
        "reports/DOCKERFILE_V5_VALIDATION.json", "reports/DOCKERFILE_V6_VALIDATION.json",
        "reports/DOCKER_CODEC_RUNTIME.json", "reports/DOCKER_TOOLS_RUNTIME.json",
    )
    assert all(not (PROJECT / name).exists() for name in obsolete)
    retained = (
        "Dockerfile", "requirements.txt", "constraints-tested.txt",
        "environment/requirements-lock.txt", "environment/conda-explicit-linux-64.txt",
        "environment/wheels/decord-wheel-repair.json", "reports/QWEN35_SMOKE_TEST.md",
        "reports/qwen35_kernel_check.json", "reports/IMAGE_AUDIT.json",
        "data/kvc_sft_smoke.jsonl", "schemas/kvc_action_smoke_v0.schema.json",
    )
    assert all((PROJECT / name).is_file() for name in retained)
    import kvc_sft
    metadata = tomllib.loads((PROJECT / "pyproject.toml").read_text())["project"]
    assert kvc_sft.__version__ == metadata["version"]


def test_active_documentation_local_links_still_resolve_after_cleanup():
    documents = [PROJECT / name for name in ("README.md", "DESIGN.md", "docker/README.md")]
    documents.extend((PROJECT / "docs").glob("*.md"))
    for path in documents:
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", path.read_text()):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = target.split("#", 1)[0].strip("<>")
            assert (path.parent / target).exists(), (path, target)


def test_launchers_do_not_need_sibling_code_or_author_home_paths():
    for name in ("common.sh", "train_lora.sh", "infer_lora.sh", "infer_and_validate.sh"):
        source = (PROJECT / "scripts" / name).read_text()
        assert "SWIFT_REPO" not in source and "WORKSPACE_ROOT" not in source
        assert "/home/tianhl" not in source
    assert "PROJECT_ROOT.parent" not in (PROJECT / "scripts/make_capacity_fixture.py").read_text()
    from kvc_sft.infer_and_validate import build_parser
    option = next(action for action in build_parser()._actions if "--swift-repo" in action.option_strings)
    assert not option.required


def test_workspace_is_self_contained_after_project_move():
    workspace = next(PROJECT.glob("*.code-workspace"))
    settings = json.loads(workspace.read_text())
    assert settings["folders"] == [{"path": "."}]
    assert (PROJECT / "reports/PROJECT_MIGRATION.json").is_file()


def test_upload_package_excludes_runtime_artifacts_and_credentials(tmp_path):
    project = tmp_path / "isolated_project"
    (project / "scripts").mkdir(parents=True)
    shutil.copy2(PROJECT / "scripts/package_upload.sh", project / "scripts/package_upload.sh")
    (project / "README.md").write_text("self-contained project\n")
    (project / "docs").mkdir()
    (project / "docs/COMPANY_REPOSITORY.md").write_text("test-only-private-transfer-draft")
    for folder in ("models", "model_cache", "data/raw", ".cache", "codex_context", "outputs", "__pycache__"):
        (project / folder).mkdir(parents=True, exist_ok=True)
        (project / folder / "must-not-upload").write_text("test-only-runtime-placeholder")
    for name in (".env", ".env.local", ".hf_token", "credential.key", "credential.pem"):
        (project / name).write_text("test-only-secret-placeholder")
    archive = project / "outputs/uploads/code.tar.gz"
    result = subprocess.run(["bash", str(project / "scripts/package_upload.sh"), str(archive)],
                            text=True, capture_output=True, check=True)
    assert str(archive) in result.stdout
    with tarfile.open(archive) as bundle:
        files = {member.name for member in bundle.getmembers() if member.isfile()}
    assert files == {"isolated_project/README.md", "isolated_project/scripts/package_upload.sh"}
    before = archive.read_bytes()
    repeat = subprocess.run(["bash", str(project / "scripts/package_upload.sh"), str(archive)], capture_output=True)
    assert repeat.returncode == 2 and archive.read_bytes() == before
