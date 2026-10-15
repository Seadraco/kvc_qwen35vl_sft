# 在另一台服务器复现

目标是复现当前 Qwen3.5-9B 双卡 LoRA smoke / tiny-overfit 链路，不是正式业务效果验收。
源码、Dockerfile、依赖文件、smoke JSONL/schema、两个测试视频和修复 Decord wheel 均随 Git 克隆；模型权重另行下载。
经用户明确要求同步的脱敏聊天文字与项目摘要位于 [docs/ai_handoff/](ai_handoff/README.md)。原始后台记录、私密工具日志、原始业务数据、既有 checkpoint 和运行缓存不在 GitHub 中。

## 1. 克隆代码

在目标 Linux x86_64 服务器执行，不是在 Mac 本地终端安装训练环境：

```bash
git clone --branch main https://github.com/Seadraco/kvc_qwen35vl_sft.git
cd kvc_qwen35vl_sft
git log -1 --oneline
```

如果已经克隆，先确认没有需保留的本地改动，再用 `git pull --ff-only` 更新。
smoke 复现不需要下载额外视频、业务编码器、旧项目或 FlashAttention 包。

## 2. 安装环境：两种方式选一种

### A. 已进入项目镜像（推荐用于已有训练镜像的服务器）

先确认容器确实带有命名 Conda 环境。不要重复运行 `setup_env.sh` 替换镜像依赖。

```bash
source /opt/conda/etc/profile.d/conda.sh
conda activate kvc_qwen35_sft
export KVC_CONDA_BIN=/opt/conda/bin/conda
export KVC_SFT_ENV_PREFIX="$CONDA_PREFIX"
export CUDA_HOME=/usr/local/cuda
export PATH="$CUDA_HOME/bin:$PATH"
export LD_LIBRARY_PATH="$CONDA_PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
bash scripts/install_project.sh
conda env list
python -m pip check
```

若环境仍叫 `/opt/venv/ov2`，说明运行的是旧镜像，不能把该镜像当作本项目当前命名环境。
GitHub 只保存 Dockerfile，不保存构建好的镜像。若需在公司环境构建，按 [ENVIRONMENT.md](ENVIRONMENT.md) 提交/构建 Dockerfile；公司基础镜像需要公司网络权限。

### B. 普通服务器从零创建 Conda 环境

需要 NVIDIA 驱动、CUDA 12.4 **devel** 工具链（含 nvcc）、Linux x86_64 和可用的 GCC/G++；Conda 不负责安装宿主 GPU 驱动。
建议匹配 Dockerfile 的 Ubuntu 22.04 / GCC 11。先执行 `nvidia-smi`、`/usr/local/cuda-12.4/bin/nvcc --version` 和 `gcc --version`；按实际 CUDA 路径调整下面命令。
具体主机依赖和完整版本见 [ENVIRONMENT.md](ENVIRONMENT.md)。

Ubuntu 22.04 上缺少系统工具时，可以由有 sudo 权限的用户安装下面这些库；这组命令不安装或替换 GPU 驱动/CUDA：

```bash
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  build-essential gcc-11 g++-11 pkg-config git curl ca-certificates xz-utils rsync \
  libaio-dev libnuma-dev libgl1 libglib2.0-0 libsndfile1 ffmpeg
```

如果 `nvcc` 不存在，先使用带 CUDA 12.4 devel 的镜像，或请管理员安装匹配工具链；仅安装 cu124 的 PyTorch wheel 不能提供扩展编译器。

若已经安装 Conda/Miniforge，跳过下面的 Miniforge 安装，使用现有 Conda 的真实可执行路径。
尚未安装 Conda 时，可在不覆盖现有目录的前提下执行：

```bash
curl -fL --retry 5 --retry-delay 3 --retry-all-errors \
  https://github.com/conda-forge/miniforge/releases/download/25.3.0-3/Miniforge3-25.3.0-3-Linux-x86_64.sh \
  -o /tmp/kvc-miniforge-25.3.0-3.sh
echo '1b57f8cb991982063f79b56176881093abb1dc76d73fda32102afde60585b5a1  /tmp/kvc-miniforge-25.3.0-3.sh' | sha256sum -c -
bash /tmp/kvc-miniforge-25.3.0-3.sh -b -p "$HOME/miniforge3"
```

随后在项目根目录执行（安装过程跳过 GPU 运行检查，但仍需要 nvcc 编译扩展）：

```bash
export KVC_CONDA_BIN="$HOME/miniforge3/bin/conda"    # 改为你的实际 Conda 可执行路径
export CUDA_HOME=/usr/local/cuda-12.4              # 若 CUDA 链接位于 /usr/local/cuda，请修改
export PATH="$CUDA_HOME/bin:$PATH"
export CC=/usr/bin/gcc-11 CXX=/usr/bin/g++-11       # 必须已经安装；或指定 CUDA 支持的编译器
KVC_REQUIRE_CUDA=0 bash scripts/setup_env.sh

source "$("$KVC_CONDA_BIN" info --base)/etc/profile.d/conda.sh"
conda activate kvc_qwen35_sft
export KVC_SFT_ENV_PREFIX="$CONDA_PREFIX"
export LD_LIBRARY_PATH="$CONDA_PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
python -m pip check
```

默认回放 `environment/conda-explicit-linux-64.txt` 与 pip lock，不升级 base，不使用 `pip-freeze.txt` 中的旧绝对路径直接安装。
安装器使用官方 cu124 Torch，随后安装已校验的 Decord wheel、普通依赖、单独的 DeepSpeed、源码编译 causal-conv1d，最后安装本项目。
不会在这一步自动开始训练。新的服务器仍需执行下面的运行验收；本次没有在目标服务器从零安装过。

## 3. 下载固定版本的模型（约 19.3 GB）

已核对官方 [Qwen3.5-9B 固定 revision](https://huggingface.co/Qwen/Qwen3.5-9B/tree/c202236235762e1c871ad0ccb60c8ee5ba337b9a) 包含四个权重分片；不能只下载 tokenizer 或某一个分片。
在项目根目录、上述环境中执行：

```bash
export KVC_MODEL_REVISION=c202236235762e1c871ad0ccb60c8ee5ba337b9a
export KVC_MODEL="$PWD/models/Qwen3.5-9B/$KVC_MODEL_REVISION"
python scripts/download_model.py \
  --repo Qwen/Qwen3.5-9B \
  --revision "$KVC_MODEL_REVISION" \
  --destination "$KVC_MODEL" \
  --workers 4
```

脚本从 Hugging Face 官方源下载模型、配置和 tokenizer，逐文件检查大小与官方 SHA256/Git blob hash，生成 `download_manifest.json`。
中断后重复同一命令可续传 `.incomplete` 文件，并校验/复用已完成文件；不会覆盖已存在但校验失败的文件。
目录需容纳约 19.3 GB 模型，另为环境与多份 checkpoint 留出空间。网络较慢可改为 `--workers 1`。
如 Hugging Face 不通，可在可联网机器执行同一脚本后私下复制完整目录；不要替换成未经验证的第三方权重。

也可以选择官方 Hub CLI（与上述脚本二选一）：

```bash
hf download Qwen/Qwen3.5-9B \
  --revision "$KVC_MODEL_REVISION" \
  --local-dir "$KVC_MODEL"
```

## 4. 两卡运行检查与短训

先用 `nvidia-smi` 确认可用 GPU。下面的 `0,1` 必须替换为实际获准使用的两张卡；当前预检默认要求每卡至少 40 GiB 空闲显存，并支持 BF16。
GPU kernel 检查只使用其中一张，NCCL 检查和训练使用两张。

```bash
python -m pytest -q
CUDA_VISIBLE_DEVICES=0 python scripts/check_kernels.py
CUDA_VISIBLE_DEVICES=0,1 torchrun --standalone --nproc_per_node=2 scripts/check_distributed.py
CUDA_VISIBLE_DEVICES=0,1 bash scripts/preflight.sh

RUN_DIR="outputs/remote_smoke_$(date -u +%Y%m%dT%H%M%SZ)"
CUDA_VISIBLE_DEVICES=0,1 bash scripts/train_smoke.sh "$RUN_DIR"
CUDA_VISIBLE_DEVICES=0 KVC_ALLOW_INVALID=1 \
  bash scripts/infer_and_validate.sh "$RUN_DIR" 0
```

两步 smoke 验证加载、视频解码、两 rank 前反向、LoRA 更新与 checkpoint 保存，不能保证两步就学会 JSON 协议。
`KVC_ALLOW_INVALID=1` 只用于记录两步 smoke 的原始结果，不是将不合法输出当作验收成功。
训练 shell 启动器自身启动 ms-swift ranks，**不要再给 `train_smoke.sh` 套一层 torchrun**。

如需复现之前的 20-step tiny-overfit 与严格推理：

```bash
OVERFIT_DIR="outputs/remote_overfit_$(date -u +%Y%m%dT%H%M%SZ)"
CUDA_VISIBLE_DEVICES=0,1 KVC_SMOKE_STEPS=20 KVC_SMOKE_LR=0.0001 \
  bash scripts/train_smoke.sh "$OVERFIT_DIR"
CUDA_VISIBLE_DEVICES=0 bash scripts/infer_and_validate.sh "$OVERFIT_DIR" 0
CUDA_VISIBLE_DEVICES=1 bash scripts/infer_and_validate.sh "$OVERFIT_DIR" 1
```

严格推理通过证明这两条模拟样本能拟合，不等于正式业务泛化或 BD-Rate 收益；新服务器/GPU上的结果以实际运行输出为准。

## 5. 非必需的本地资源如何迁移

| 资源 | 当前 GitHub 是否包含 | 是否影响从头 smoke |
| --- | --- | --- |
| 源码、Dockerfile、依赖锁、smoke JSONL/schema、测试视频、修复 Decord wheel | 是 | 必需，直接 clone 获得 |
| Qwen3.5-9B 权重 | 否 | 必需，用第 3 节下载 |
| `data/raw/external_delivery/`，约 4.4G | 否 | 不影响 smoke；业务容量/正式数据加工需要时私下同步 |
| `outputs/.../train/`，含 adapter 和完整训练状态 | 否 | 从头训练不需要；复用 adapter/续训才需同步 |
| 详细 `reports/` 日志及容量 fixture | 部分，默认只提交摘要 | 不影响 smoke；目标机器重新运行可生成新证据 |
| 脱敏聊天文字与 AI 项目摘要 `docs/ai_handoff/` | 是 | 不影响训练，可用于新 AI 接续上下文 |
| 原始会话/私密工具日志、缓存、Conda 安装目录、已构建镜像 | 否 | 不作为 GitHub 下载内容 |

业务样例与既有 adapter 不是公开托管资源，当前没有可提供的公开下载 URL。需要它们时，在目标服务器使用受信任的 SSH/SFTP，替换下面的源机器地址：

```bash
SOURCE_SSH='用户名@源服务器'
SOURCE_PROJECT='/home/tianhl/compression/kuaishou/kvc_qwen35vl_sft'
mkdir -p data/raw/external_delivery
rsync -avP "$SOURCE_SSH:$SOURCE_PROJECT/data/raw/external_delivery/" data/raw/external_delivery/

# 可选：复制已有训练目录，保留 checkpoint 的父目录元数据和 launch contract。
mkdir -p outputs/dual_gpu_acceptance_20261013_s1HDjO/train
rsync -avP "$SOURCE_SSH:$SOURCE_PROJECT/outputs/dual_gpu_acceptance_20261013_s1HDjO/train/" \
  outputs/dual_gpu_acceptance_20261013_s1HDjO/train/
```

跨机器后的绝对模型/视频路径和 launch contract 需核对，不能认为复制后即可无条件原样续训，也不要删除恢复校验来绕过路径/配置差异。
正式业务训练还需要真实 measured GT、action schema 和按源视频分组的数据切分；当前附带的 smoke 标签不能替代它们。
