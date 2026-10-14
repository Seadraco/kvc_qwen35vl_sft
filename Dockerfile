FROM registry.corp.kuaishou.com/kml/v_cuda:12.4.0-devel-ubuntu22.04

ARG NODE_VERSION=20.19.5
ARG CODE_SERVER_VERSION=4.140.0

# 公司网络代理
ENV http_proxy=http://oversea-squid1.jp.txyun:11080 \
    https_proxy=http://oversea-squid1.jp.txyun:11080 \
    HTTP_PROXY=http://oversea-squid1.jp.txyun:11080 \
    HTTPS_PROXY=http://oversea-squid1.jp.txyun:11080 \
    no_proxy=localhost,127.0.0.1,localaddress,localdomain.com,internal,.internal,corp.kuaishou.com,.corp.kuaishou.com,test.gifshow.com,.test.gifshow.com,staging.kuaishou.com,.staging.kuaishou.com \
    NO_PROXY=localhost,127.0.0.1,localaddress,localdomain.com,internal,.internal,corp.kuaishou.com,.corp.kuaishou.com,test.gifshow.com,.test.gifshow.com,staging.kuaishou.com,.staging.kuaishou.com \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TORCH_CUDA_ARCH_LIST="8.0;8.6;8.9;9.0" \
    MAX_JOBS=4 \
    PYTHONPATH=""

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# 基础镜像检查：Ubuntu 22.04 / x86_64 / CUDA 12.4。
RUN source /etc/os-release \
    && test "$ID" = ubuntu && test "$VERSION_ID" = 22.04 \
    && test "$(uname -m)" = x86_64 \
    && test -x /usr/local/cuda/bin/nvcc \
    && /usr/local/cuda/bin/nvcc --version | grep -F 'release 12.4,'

# 系统工具、Python、编译器与视频/分布式运行库
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-dev \
        python3-pip \
        build-essential \
        gcc-11 \
        g++-11 \
        cmake \
        pkg-config \
        git \
        git-lfs \
        curl \
        wget \
        ca-certificates \
        xz-utils \
        lsof \
        vim \
        less \
        tmux \
        htop \
        jq \
        ripgrep \
        unzip \
        zip \
        rsync \
        openssh-client \
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender-dev \
        libavcodec-dev \
        libavformat-dev \
        libavutil-dev \
        libswscale-dev \
        libswresample-dev \
        libaio-dev \
        libnuma1 \
        libnuma-dev \
        libibverbs1 \
        ibverbs-providers \
        librdmacm1 \
        ffmpeg \
    && git lfs install --system \
    && apt-get clean \
    && rm -rf \
        /var/lib/apt/lists/* \
        /var/cache/apt/archives/* \
        /tmp/* \
        /var/tmp/*

# Conda 训练环境：kvc_qwen35_sft（Python 3.12 / FFmpeg 7）
ARG MINIFORGE_VERSION=25.3.0-3
ARG MINIFORGE_SHA256=1b57f8cb991982063f79b56176881093abb1dc76d73fda32102afde60585b5a1
RUN curl -fL --retry 5 --retry-delay 3 --retry-all-errors \
        "https://github.com/conda-forge/miniforge/releases/download/$MINIFORGE_VERSION/Miniforge3-$MINIFORGE_VERSION-Linux-x86_64.sh" \
        -o /tmp/miniforge.sh \
    && echo "$MINIFORGE_SHA256  /tmp/miniforge.sh" | sha256sum -c - \
    && bash /tmp/miniforge.sh -b -p /opt/conda \
    && /opt/conda/bin/conda create --yes --name kvc_qwen35_sft \
        --override-channels --channel conda-forge \
        python=3.12.15 pip=26.2.1 setuptools=82.0.1 wheel=0.48.0 \
        ffmpeg=7.1.1 pkg-config \
    && /opt/conda/bin/conda clean --all --yes \
    && rm -f /tmp/miniforge.sh

ENV PATH="/opt/conda/envs/kvc_qwen35_sft/bin:/opt/conda/bin:/usr/local/cuda/bin:${PATH}" \
    LD_LIBRARY_PATH="/opt/conda/envs/kvc_qwen35_sft/lib:${LD_LIBRARY_PATH}" \
    KVC_SFT_ENV_PREFIX=/opt/conda/envs/kvc_qwen35_sft \
    KVC_CONDA_BIN=/opt/conda/bin/conda \
    PROJECT_ENV_PREFIX=/opt/conda/envs/kvc_qwen35_sft \
    PYTHONNOUSERSITE=1 \
    FORCE_QWENVL_VIDEO_READER=torchcodec \
    TOKENIZERS_PARALLELISM=false \
    CUDA_HOME=/usr/local/cuda \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    CC=/usr/bin/gcc-11 \
    CXX=/usr/bin/g++-11 \
    DS_BUILD_OPS=0

# 登录和交互终端自动激活 Conda，并恢复 Node/CUDA PATH。
RUN printf '%s\n' \
        '. /opt/conda/etc/profile.d/conda.sh' \
        'conda activate kvc_qwen35_sft' \
        'export PATH=/opt/node/bin:/usr/local/cuda/bin:"$PATH"' \
        > /etc/profile.d/kvc-qwen35-sft.sh \
    && printf '%s\n' '. /etc/profile.d/kvc-qwen35-sft.sh' >> /etc/bash.bashrc

# PyTorch 2.6.0 + CUDA 12.4 wheel
RUN python3 -m pip install --no-cache-dir \
        torch==2.6.0+cu124 \
        torchvision==0.21.0+cu124 \
        --index-url https://download.pytorch.org/whl/cu124

# 修复 Decord wheel 标签，仅修改 WHEEL/RECORD，保持二进制不变。
# 先安装修复版，避免普通依赖解析安装原始 wheel。
RUN curl -fL --retry 5 --retry-delay 3 --retry-all-errors \
        https://files.pythonhosted.org/packages/11/79/936af42edf90a7bd4e41a6cac89c913d4b47fa48a26b042d5129a9242ee3/decord-0.6.0-py3-none-manylinux2010_x86_64.whl \
        -o /tmp/decord-upstream.whl \
    && python3 -c 'import base64, csv, hashlib, io, sys, zipfile; from pathlib import Path; \
        src = Path("/tmp/decord-upstream.whl").read_bytes(); \
        hashlib.sha256(src).hexdigest() == "51997f20be8958e23b7c4061ba45d0efcd86bffd5fe81c695d0befee0d442976" or sys.exit("Decord upstream SHA256 mismatch"); \
        archive = zipfile.ZipFile(io.BytesIO(src)); entries = archive.infolist(); \
        files = {e.filename: archive.read(e.filename) for e in entries}; archive.close(); \
        wheel = "decord-0.6.0.dist-info/WHEEL"; record = "decord-0.6.0.dist-info/RECORD"; \
        old = b"Tag: cp36-cp36m-manylinux2010_x86_64"; new = b"Tag: py3-none-manylinux2010_x86_64"; \
        len(entries) == len(files) and files[wheel].count(old) == 1 or sys.exit("Unexpected Decord metadata"); \
        files[wheel] = files[wheel].replace(old, new).rstrip() + b"\nBuild: 1kvc\n\n"; \
        rows = io.StringIO(newline=""); writer = csv.writer(rows, lineterminator="\n"); \
        writer.writerows((name, "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(files[name]).digest()).decode().rstrip("="), len(files[name])) for name in files if name != record); \
        writer.writerow((record, "", "")); files[record] = rows.getvalue().encode(); \
        output = zipfile.ZipFile("/tmp/decord-0.6.0-1kvc-py3-none-manylinux2010_x86_64.whl", "w"); \
        [output.writestr(e, files[e.filename]) for e in entries]; output.close()' \
    && python3 -m pip install --no-cache-dir --no-deps \
        /tmp/decord-0.6.0-1kvc-py3-none-manylinux2010_x86_64.whl \
    && rm -f /tmp/decord-upstream.whl /tmp/decord-0.6.0-1kvc-py3-none-manylinux2010_x86_64.whl

# Qwen3.5 / ms-swift 训练与数据处理依赖
# Torch/torchvision/Decord 固定版本用于依赖解析保护，不强制重装。
RUN python3 -m pip install --no-cache-dir \
        pip==26.2.1 setuptools==82.0.1 wheel==0.48.0 \
        torch==2.6.0+cu124 torchvision==0.21.0+cu124 triton==3.2.0 sympy==1.13.1 \
        ms-swift==4.5.3 transformers==5.9.0 peft==0.20.0 trl==0.29.1 \
        accelerate==1.14.0 datasets==4.8.4 modelscope==1.39.1 \
        flash-linear-attention==0.4.2 fla-core==0.4.2 \
        liger-kernel==0.7.0 \
        qwen-vl-utils==0.0.14 torchcodec==0.2.1 av==18.1.0 decord==0.6.0 \
        jsonschema==4.26.0 PyYAML==6.0.3 \
        numpy==2.5.2 pandas==3.0.6 scipy==1.18.1 \
        Pillow==12.3.0 opencv-python-headless==4.14.0.94 \
        imageio==2.38.1 imageio-ffmpeg==0.6.0 lpips==0.1.4 torchmetrics==1.9.0 \
        safetensors==0.8.0 huggingface-hub==1.33.0 tokenizers==0.22.2 \
        sentencepiece==0.2.2 protobuf==7.36.2 packaging==26.3 einops==0.8.2 \
        ninja==1.13.2 tensorboard==2.21.0 matplotlib==3.11.2 \
        psutil==7.2.2 nvidia-ml-py==13.595.45 rich==14.3.4 tqdm==4.70.1 \
        pytest==8.4.2 pytest-timeout==2.4.0 conda-pack==0.9.2 \
        filelock==3.32.3 networkx==3.6.1 pydantic==2.14.0 nvitop==1.7.1

# 全局 nvitop 入口，使用训练 Conda 环境。
RUN printf '%s\n' \
        '#!/usr/bin/env bash' \
        'set -euo pipefail' \
        'unset PYTHONPATH PYTHONHOME' \
        'export LD_LIBRARY_PATH="${KVC_SFT_ENV_PREFIX}/lib:/usr/local/nvidia/lib:/usr/local/nvidia/lib64"' \
        'exec "$KVC_SFT_ENV_PREFIX/bin/nvitop" "$@"' \
        > /usr/local/bin/nvitop \
    && chmod +x /usr/local/bin/nvitop

# DeepSpeed：关闭构建隔离以使用当前 Torch，运行依赖仍由 pip 解析。
# DS_BUILD_OPS=0 不预编译可选算子；保留 Torch/NVML 版本保护。
RUN python3 -m pip install --no-cache-dir --no-build-isolation \
        deepspeed==0.18.8 torch==2.6.0+cu124 nvidia-ml-py==13.595.45

# Causal Conv1d：强制源码编译，匹配当前 Torch 的 C++ ABI。
RUN unset CAUSAL_CONV1D_FORCE_CXX11_ABI \
    && CAUSAL_CONV1D_FORCE_BUILD=TRUE python3 -m pip install \
        --no-cache-dir --no-build-isolation --no-deps \
        --no-binary causal-conv1d causal-conv1d==1.6.0 \
    && python3 -m pip check

# 安装挂载的项目：cd /code/kvc_qwen35vl_sft && bash scripts/install_project.sh

# Node.js 20
RUN curl -fL \
        --retry 5 \
        --retry-delay 3 \
        --retry-all-errors \
        "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
        -o /tmp/node.tar.xz \
    && mkdir -p /opt \
    && tar -xJf /tmp/node.tar.xz -C /opt \
    && ln -s "/opt/node-v${NODE_VERSION}-linux-x64" /opt/node \
    && rm -f /tmp/node.tar.xz

ENV NODE_HOME=/opt/node
ENV PATH=/opt/node/bin:${PATH}

# MyFlicker CLI（公司 npm 源）
RUN npm config set registry https://npm.corp.kuaishou.com/ --global \
    && npm config set @myflicker:registry https://npm.corp.kuaishou.com/ --global \
    && npm config set proxy "${http_proxy}" --global \
    && npm config set https-proxy "${https_proxy}" --global \
    && npm install --global @myflicker/cli \
    && npm list --global --depth=0 \
    && npm cache clean --force \
    && rm -rf /root/.npm/_cacache /tmp/*

# code-server
RUN curl -fL \
        --retry 5 \
        --retry-delay 3 \
        --retry-all-errors \
        "https://github.com/coder/code-server/releases/download/v${CODE_SERVER_VERSION}/code-server_${CODE_SERVER_VERSION}_amd64.deb" \
        -o /tmp/code-server.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        /tmp/code-server.deb \
    && code-server --version \
    && apt-get clean \
    && rm -rf \
        /tmp/code-server.deb \
        /var/lib/apt/lists/* \
        /var/cache/apt/archives/* \
        /tmp/* \
        /var/tmp/*

# code-server 启动脚本
# 用法：PASSWORD='你的密码' run-code-server [PORT] [WORKSPACE]
# 忽略历史工作区；切换目录时清除浏览器旧 folder/workspace 参数。
RUN printf '%s\n' \
        '#!/usr/bin/env bash' \
        'set -euo pipefail' \
        '' \
        'PORT="${1:-16530}"' \
        'WORKSPACE="${2:-/workspace}"' \
        '' \
        'if [[ -z "${PASSWORD:-}" ]]; then' \
        '    echo "ERROR: 请先设置 PASSWORD 环境变量"' \
        '    echo "示例：PASSWORD=your_password run-code-server ${PORT} ${WORKSPACE}"' \
        '    exit 1' \
        'fi' \
        '' \
        'exec code-server \' \
        '    --bind-addr "0.0.0.0:${PORT}" \' \
        '    --auth password \' \
        '    --disable-telemetry \' \
        '    --ignore-last-opened \' \
        '    "${WORKSPACE}"' \
        > /usr/local/bin/run-code-server \
    && chmod +x /usr/local/bin/run-code-server

# 构建检查：CPU 模块、视频解码、依赖一致性与终端 PATH（无需 GPU）。
RUN command -v conda \
    && conda env list \
    && /bin/bash -c '. /etc/profile.d/kvc-qwen35-sft.sh && test "$CONDA_DEFAULT_ENV" = kvc_qwen35_sft' \
    && python3 -c \
        "import os, sys, torch; \
         assert sys.version_info[:2] == (3, 12), sys.version; \
         assert sys.prefix == os.environ['KVC_SFT_ENV_PREFIX'], (sys.executable, sys.prefix); \
         assert torch.__version__ == '2.6.0+cu124' and torch.version.cuda == '12.4'; \
         assert not torch.compiled_with_cxx11_abi(), 'Expected the pinned Torch cu124 build with CXX11 ABI=0'; \
         assert torch.distributed.is_available() and torch.distributed.is_nccl_available(); \
         print('python=', sys.version); \
         print('torch=', torch.__version__); \
         print('torch_cuda=', torch.version.cuda)" \
    && python3 -c \
        "import transformers; \
         print('transformers=', transformers.__version__)" \
    && CUDA_VISIBLE_DEVICES="" python3 -c \
        "from transformers import Qwen3_5ForConditionalGeneration; \
         import fla, causal_conv1d, causal_conv1d_cuda, deepspeed; \
         print('Qwen3.5 / FLA / causal-conv1d import OK')" \
    && python3 -c \
        "import importlib; \
         modules = ('swift', 'peft', 'trl', 'accelerate', 'datasets', 'modelscope', \
                    'qwen_vl_utils', 'liger_kernel', 'torchvision', 'triton', \
                    'jsonschema', 'yaml', 'numpy', 'pandas', 'scipy', 'PIL', \
                    'imageio', 'imageio_ffmpeg', 'lpips', 'torchmetrics', \
                    'safetensors', 'huggingface_hub', 'tokenizers', 'sentencepiece', \
                    'google.protobuf', 'packaging', 'einops', 'ninja', 'tensorboard', \
                    'matplotlib', 'psutil', 'pynvml', 'rich', 'tqdm', 'pytest', 'conda_pack'); \
         [importlib.import_module(name) for name in modules]; \
         print('Training / data / monitoring imports OK')" \
    && ffmpeg -hide_banner -loglevel error -y \
        -f lavfi -i color=c=black:s=64x64:r=4 -frames:v 4 \
        -c:v mpeg4 -threads 1 /tmp/kvc-probe.mp4 \
    && python3 -c \
        "from torchcodec.decoders import VideoDecoder; \
         import decord, av, cv2; \
         decoder = VideoDecoder('/tmp/kvc-probe.mp4', device='cpu', num_ffmpeg_threads=1); \
         assert len(decoder) == 4 and tuple(decoder[0].shape) == (3, 64, 64); \
         reader = decord.VideoReader('/tmp/kvc-probe.mp4', ctx=decord.cpu(0), num_threads=1); \
         assert len(reader) == 4 and tuple(reader[0].asnumpy().shape) == (64, 64, 3); \
         print('TorchCodec / Decord CPU decode OK')" \
    && rm -f /tmp/kvc-probe.mp4 \
    && python3 -m pip check \
    && command -v swift \
    && command -v torchrun \
    && command -v ffprobe \
    && command -v nvcc \
    && command -v run-code-server \
    && nvitop --version \
    && node --version \
    && npm --version \
    && npm list --global @myflicker/cli --depth=0 \
    && code-server --version \
    && ffmpeg -version \
    && /bin/bash -lc 'test "$CONDA_DEFAULT_ENV" = kvc_qwen35_sft \
        && test "$(command -v python3)" = "$KVC_SFT_ENV_PREFIX/bin/python3" \
        && command -v conda swift torchrun nvcc ffprobe node npm nvitop run-code-server' \
    && /bin/bash -ic 'test "$CONDA_DEFAULT_ENV" = kvc_qwen35_sft \
        && test "$(command -v python3)" = "$KVC_SFT_ENV_PREFIX/bin/python3" \
        && command -v conda swift torchrun nvcc ffprobe node npm nvitop run-code-server'

# 清理缓存
RUN (python3 -m pip cache purge || true) \
    && (npm cache clean --force || true) \
    && rm -rf \
        /root/.cache/pip \
        /root/.npm/_cacache \
        /var/lib/apt/lists/* \
        /var/cache/apt/archives/* \
        /tmp/* \
        /var/tmp/*

WORKDIR /workspace

CMD ["/bin/bash"]
