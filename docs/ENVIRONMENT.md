# Qwen3.5-9B 环境、镜像和双卡迁移

本文件记录当前 Linux x86_64 SFT 软件栈的安装顺序、依赖用途和迁移验收边界。安装脚本不是“安装所有机器学习库”，而是覆盖当前训练、视频读取、数据校验、推理、测试、报告以及明确相邻的指标工具。另一套推理/RL 后端应另建环境，避免静默替换 PyTorch。

当前统一工作目录为 `kvc_qwen35vl_sft`，公司镜像以项目根目录 [Dockerfile](../Dockerfile) 为准。它创建命名 Conda 环境 `/opt/conda/envs/kvc_qwen35_sft`，固定全部项目直接依赖与关键约束，但普通间接依赖仍由解析器选择，不能声称与历史完整锁逐字节相同。

源码运行时挂载后执行 `bash scripts/install_project.sh`，该入口只做 `--no-deps --no-build-isolation` 的 editable 安装，不重装训练栈。根 Dockerfile 增加了工具链预检、实际 CPU 视频解码和关键版本/ABI/NCCL 编译能力检查；GPU 算子、双卡 NCCL、9B 权重加载和训练仍须在目标容器验收。

下文的宿主版本和 Qwen3.5 报告是既有实测证据，不能当作本次新镜像的运行证据。模型位于 `models/`，当前 checkpoint 位于 `outputs/`，历史 checkpoint 位于 `outputs/history/`；本目录不使用旧模型执行栈或旧镜像/codec 配方。

## 1. 环境身份与主机边界

| 项目 | 约定 |
| --- | --- |
| Conda 名称 | `kvc_qwen35_sft` |
| 本机前缀 | `/home/tianhl/anaconda3/envs/kvc_qwen35_sft` |
| 平台 | Linux x86_64，Python 3.12 |
| 本机驱动 | 550.54.14；本项目不升级或替换宿主驱动 |
| 本机 CUDA 编译工具链 | `/usr/local/cuda-12.4` |
| PyTorch 用户态 CUDA | cu124 wheel；不是依靠 `nvcc` 决定 wheel 运行版本 |
| 本机 GPU | 4090，其中多张单卡约 48 GiB；训练前实时检查空闲情况 |
| 远端目标 | 双 H800；容量、驱动、容器运行时须在目标主机确认 |
| 模型 | `Qwen/Qwen3.5-9B` |
| 模型 commit | `c202236235762e1c871ad0ccb60c8ee5ba337b9a` |

工程目录使用 `kvc_qwen35vl_sft`，Conda 环境名保持 `kvc_qwen35_sft`，无需因目录迁移重建环境。Python 发行包名称 `kvc-qwen3vl-sft` 仅保留为已有安装的兼容标识，不包含旧模型执行栈。迁移后重新执行 `scripts/install_project.sh`，并检查 `which python`、实际包路径和完整版本。

## 2. 为什么选这组版本

| 核心组件 | 固定版本 | 作用与选择依据 |
| --- | --- | --- |
| Python | 3.12.x | 新建独立环境；线性注意力栈推荐的 Python 分支 |
| torch / torchvision | 2.6.0+cu124 / 0.21.0+cu124 | 保持本机 CUDA 12.4 工具链与驱动条件；避免被旁支依赖拉到其他 CUDA 栈 |
| Triton | 3.2.0 | 与当前 torch wheel 配套，负责 FLA JIT kernel |
| ms-swift | 4.5.3 | 固定 PyPI 发布版，不使用相邻参考仓库的开发版源码 |
| Transformers | 5.9.0 | 包含 Qwen3.5 模型类与视频 processor |
| PEFT / TRL | 0.20.0 / 0.29.1 | LoRA 与 ms-swift 训练依赖 |
| Accelerate / Datasets | 1.14.0 / 4.8.4 | 分布式与数据加载；与选定 ms-swift 约束保持一致 |
| FLA / fla-core | 0.4.2 / 0.4.2 | Qwen3.5 线性注意力 kernel；不盲目升级到要求更高 torch 的版本 |
| causal-conv1d | 1.6.0 | 线性注意力中因果卷积加速，安装顺序依赖 torch |
| liger-kernel | 0.7.0 | 可用的训练 kernel 工具；安装不代表配置已启用 |
| DeepSpeed | 0.18.8 | 后续 ZeRO 路径的依赖；当前主线是 DDP |
| qwen-vl-utils | 0.0.14 | 视频采样及多模态输入处理 |
| TorchCodec / PyAV | 0.2.1 / 18.1.0 | 与 torch 2.6 配套的解码器与辅助媒体库 |
| Decord | 0.6.0 | 满足 ms-swift 模型注册的依赖检查；实际视频后端仍强制 TorchCodec |
| FFmpeg | conda 7.1.1 | 同时提供 `ffmpeg/ffprobe` 与 TorchCodec 所需共享库 |
| pip / setuptools / wheel | conda 26.2.1 / 82.0.1 / 0.48.0 | 保持 conda 文件所有权，避免打包时发现缺失文件 |

选版依据包括 [ms-swift Qwen3.5 实践](https://swift.readthedocs.io/en/latest/BestPractices/Qwen3_8-Best-Practice.html)、[FLA v0.4.2 依赖说明](https://github.com/fla-org/flash-linear-attention/tree/v0.4.2)、[TorchCodec 版本矩阵](https://github.com/pytorch/torchcodec#installing-torchcodec)。依赖元数据满足不等于 GPU 实测成功，运行证据仍以本工程对应报告为准。

### 2.1 所有 Python 库如何追踪

三层记录各司其职：

1. `requirements.txt`：人能维护的直接依赖、功能分类和少量可接受版本范围。
2. `constraints-tested.txt`：关键兼容边界，阻止 resolver 更换 torch、模型框架和视频栈。
3. `environment/requirements-lock.txt`：本次环境解析出的全部普通 pip 依赖精确版本。由环境导出；不是手工猜测所有间接依赖。

conda 管理的 pip/setuptools/wheel、torch/torchvision CUDA wheel、causal-conv1d 编译、Decord 修复 wheel 和本项目安装单独执行，不混入普通 lock 安装。DeepSpeed 仍记录在 lock 中，`setup_env.sh` 会将其从普通安装阶段过滤，再关闭构建隔离单独安装；其他普通依赖保持默认构建隔离。脚本先校验并安装仓库附带的修复 Decord wheel，避免先安装上游 wheel 再覆盖。`pip-freeze.txt` 若保留 editable 或本地 wheel 原路径，只用于审计，不直接拿它跨机器安装。

### 2.2 依赖覆盖范围

| 类别 | 库 | 当前用途 |
| --- | --- | --- |
| 训练 | ms-swift、Transformers、PEFT、TRL、Accelerate、Datasets、torch | SFT、LoRA、DDP、checkpoint |
| 模型和 token | huggingface-hub、ModelScope、tokenizers、sentencepiece、safetensors | 下载、序列化、模板和 adapter |
| 视频图像 | TorchCodec、qwen-vl-utils、PyAV、Pillow、OpenCV headless、imageio | 主路径解码以及辅助数据检查 |
| 数值和校验 | NumPy、pandas、SciPy、PyYAML、jsonschema | 数据准备、schema、统计 |
| 记录与测试 | TensorBoard、matplotlib、pytest、pytest-timeout、psutil、nvidia-ml-py | 测试、可视化、GPU/进程观测 |
| 相邻评估工具 | LPIPS、torchmetrics | 为后续离线指标实验准备；SFT 训练环并未调用它们 |
| 构建与迁移 | pip、setuptools、wheel、ninja、packaging、conda-pack | CUDA 扩展安装、打包和镜像 |

LPIPS 的网络权重可能在首次实例化时另行下载，安装 Python 包不等于指标模型权重已离线缓存。正式编码指标需另外固定网络权重及指标实现，不应混淆为已完成 BD-Rate evaluator。

以下不安装到当前主环境：vLLM、SGLang、Megatron、不同 CUDA 版 torch、bitsandbytes、未经验证的 FlashAttention 扩展。vLLM/RL/MoE 是另一条任务路径；盲目安装其最新版本可能替换 torch/Triton/NCCL。当前全注意力使用 PyTorch SDPA，FLA 是线性注意力 kernel，两者不是同一个包。

## 3. 从空环境重建

主机已有 conda 和 CUDA 12.4 devel 工具链时：

```bash
cd /path/to/kvc_qwen35vl_sft
CUDA_VISIBLE_DEVICES=4,5 KVC_CONDA_BIN=/path/to/conda/bin/conda ./scripts/setup_env.sh
source /path/to/conda/etc/profile.d/conda.sh
conda activate kvc_qwen35_sft
python -m pip check
python -c 'import torch, swift; print(torch.__version__, torch.version.cuda, swift.__file__)'
```

脚本按以下顺序执行：

1. 定位独立 env，不允许安装到 base 或 `/`；已有错误 Python 版本时失败，不覆盖旧环境。
2. conda 按二进制锁恢复 Python 3.12、FFmpeg 共享库、pkg-config、ninja 和固定版本的 pip/setuptools/wheel；首次无锁时用显式版本创建。
3. 从 PyTorch cu124 index 安装 torch 与 torchvision。
4. 用兼容约束安装普通 pip 锁文件；没有锁文件时使用直接依赖表。
5. 强制源码编译 Causal Conv1d，使用 `--no-build-isolation --no-deps` 保持已选 torch 不变。
6. 修复 Decord 官方 wheel 的内部标签并重算 RECORD；保留源码与二进制不变。
7. 安装本项目，运行 `pip check`、Qwen3.5 模型类导入及 GPU 预检。

全部 pip 操作关闭缓存，降低大 wheel 在磁盘上重复存储。脚本不会自动清空用户的全局 conda/pip cache，也不会删除旧模型。首次安装建议至少为环境、约 18 GiB 模型权重、编译临时文件与 checkpoint 留出足够空间；实际余量须先 `df -h` 检查。

脚本使用 `DS_BUILD_OPS=0` 避免一次编译所有 DeepSpeed 扩展。未来启用 ZeRO/offload 可能触发按需 JIT 编译，需要编译器、CUDA toolkit 和额外验证。

### 3.1 安装控制变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `KVC_CONDA_BIN` | `command -v conda` | 在 conda 被定义为 shell 函数时请传真实可执行文件 |
| `KVC_SFT_ENV_PREFIX` | conda base 下 `envs/kvc_qwen35_sft` | 可移植环境位置，训练脚本也需使用同一路径 |
| `KVC_USE_LOCK` | 1 | 优先使用完整锁；设 0 是重新解析依赖，不属于精确复现 |
| `KVC_INSTALL_CAUSAL_CONV1D` | 1 | 设 0 仅供 fallback 定位，不代表完整环境验收 |
| `KVC_REQUIRE_CUDA` | 1 | 无 GPU 的构建阶段可设 0；部署后仍须 GPU 预检 |
| `CUDA_VISIBLE_DEVICES` | `0,1`（未设置时） | 安装末尾预检显式传入同一物理 GPU 列表；按空闲情况覆盖，例如 `4,5` |
| `CUDA_HOME` | `/usr/local/cuda-12.4` | 编译工具链，不修改驱动 |
| `MAX_JOBS` | 4 | 限制扩展编译并发，减少 CPU/内存峰值 |

Triton 首次运行会编译适用于当前 GPU 的 kernel；4090 与 H800 分别初始化缓存。不要把一次 JIT 首轮耗时误当成稳态吞吐，也不要把另一种 GPU 的编译缓存当作可复现训练产物。

安装脚本的可见 GPU 与 `check_env --physical-gpus` 保持一致，不会在八卡主机上把“默认只检查一张卡”与“实际八张可见卡”混用。显式空列表不代表默认双卡；无 GPU 构建应设置 `KVC_REQUIRE_CUDA=0`。上述命令中的 4/5 是本次主机示例，执行前仍需检查空闲资源。

### 3.2 本次发现的安装兼容问题

**Causal Conv1d 不能只看 pip 的“构建成功”。**上游安装流程会猜测并下载预编译 wheel，本次出现导入时报 `torchCheckFail...__cxx11` 未定义符号，与当前 torch 的 C++ ABI 不匹配。安装脚本设置 `CAUSAL_CONV1D_FORCE_BUILD=TRUE` 强制源码编译；不要设置 `CAUSAL_CONV1D_FORCE_CXX11_ABI=TRUE` 改变 torch 的 ABI。本次 torch 的 `_GLIBCXX_USE_CXX11_ABI=False`。修复后仍须实际导入、前向和反向验证。

上游 Causal Conv1d 1.6 的 setup 硬编码 CUDA gencode，不能假设 `TORCH_CUDA_ARCH_LIST=8.9;9.0` 让它只编译两种架构。本次编译包含 sm80 与 sm90 等目标，sm80 二进制兼容本地 Ada，sm90 对应 Hopper；H800 仍需现场验收。

源码编译日志保存在 `reports/qwen35_causal_conv1d_build.log`，本次生成 wheel 的 SHA-256 为 `895fc8ac08dff0e3471e8d88939ee8818329a9e53dbd77e0e5967d45011404ff`。`reports/qwen35_kernel_check.json` 已记录 4090 上 FLA 0.4.2 与 Causal Conv1d 1.6.0 的 BF16 前向/反向通过；这比“包可安装”更强，但仍不替代完整 SFT 的测试报告。

**Decord 0.6.0 官方 wheel 有内部标签错误。**文件名是 `py3-none-manylinux2010_x86_64`，但 WHEEL 元数据错误标为 `cp36-cp36m`，新版 pip 在 Python 3.12 的 `pip check` 会拒绝。本次检查发现其 Python 包通过 `ctypes.CDLL` 调用唯一 `libdecord.so`，没有 CPython 扩展初始化入口。`scripts/repair_decord_wheel.py` 用固定官方下载 URL 和 SHA-256 校验原包，只修正 WHEEL 并重算 RECORD，程序源码和共享库全部逐字节保留。

- 原始 wheel SHA-256：`51997f20be8958e23b7c4061ba45d0efcd86bffd5fe81c695d0befee0d442976`。
- 修复 wheel SHA-256：`19c4411d79ff796a9d9e30d46ed6cd222b4023f467be9168f8f67a141583320d`。
- build 标识：`1kvc`；Python distribution 版本仍为 0.6.0。
- 证据：`environment/wheels/decord-wheel-repair.json`，含二进制 SHA、原始 URL 和变更文件列表。
- 已验证 Python 3.12 导入、250 帧视频首帧 CPU 解码及 `pip check`；实际训练仍强制 TorchCodec。

需要复现修复时执行 `python scripts/repair_decord_wheel.py --install`，不使用忽略 pip 检查的方式，也不直接手改已安装 WHEEL 而破坏 RECORD。离线修复可通过 `--original-wheel` 提供完全相同 SHA 的官方文件。

**pip 检查通过不代表 conda-pack 可以打包。**初始 conda 的 setuptools 84.0.0 被 pip 替换为 82.0.1 后，conda 记录中的旧 egg-info 消失，打包器会正确拒绝。安装脚本改为从一开始就由 conda 管理 setuptools 82.0.1，以及匹配的 pip/wheel，并将这三个包排除在普通 pip 锁之外。不能用 `ignore_missing_files` 隐藏这种问题；环境导出前应先做 conda-pack 文件收集预检。

本次已将 setuptools 恢复为 conda 管理的 82.0.1，其他运行库不变；重新检查只有一份 setuptools 元数据，`pip check` 通过。`CondaEnv.from_prefix(..., ignore_editable_packages=True)` 静态收集 72,036 个文件通过，未开启忽略缺失文件、未生成多 GiB 归档。此结果只证明打包收集阶段通过，不等于镜像已构建。

源码目录中不要保留过期的生成型 `*.egg-info`，避免与当前安装的发行包元数据重复。导出器会拒绝重复发行包，不靠忽略版本冲突生成锁文件。

## 4. 模型下载和离线使用

网络正常时使用官方 Hub CLI，revision 必须固定：

```bash
hf download Qwen/Qwen3.5-9B \
  --revision c202236235762e1c871ad0ccb60c8ee5ba337b9a \
  --local-dir /models/Qwen3.5-9B/c202236235762e1c871ad0ccb60c8ee5ba337b9a
```

本机网络曾导致 Hub HEAD 返回缺少仓库元数据，而 HTTPS GET 可用。兼容脚本不关闭 TLS 校验、不访问第三方权重、不执行远程 Python；它逐文件比对官方 LFS SHA-256 或 Git blob SHA-1：

```bash
python scripts/download_model.py \
  --destination models/Qwen3.5-9B/c202236235762e1c871ad0ccb60c8ee5ba337b9a
```

两种方法选一种，避免重复保存权重。以后统一：

```bash
export KVC_MODEL=/models/Qwen3.5-9B/c202236235762e1c871ad0ccb60c8ee5ba337b9a
export KVC_MODEL_REVISION=c202236235762e1c871ad0ccb60c8ee5ba337b9a
export HF_HUB_OFFLINE=1
```

模型、数据、特征协议及输出目录都不应写入镜像层。下载校验记录、模型 commit 和训练 manifest 一起保留。模型许可和再分发规则仍以官方模型仓库为准。

## 5. 从本地迁移到双 H800

本机已在 GPU 4/5 的两张 48 GiB 4090 上完成核心训练测试，见 [Qwen3.5 实测报告](../reports/QWEN35_SMOKE_TEST.md)：两步 smoke、完整状态恢复至 step 4，以及 253D/16 帧/6,907-token 的两步容量测试均通过。容量测试的 per-rank allocated 峰值约 25.30 GiB，reserved 最大 35.3125 GiB，因此训练入口默认每卡至少 40 GiB 空闲（`KVC_MIN_FREE_GIB=40`）。此门槛不能替代正式数据的逐条长度审计和实际峰值观测。

这里的 allocated 是 `on_train_begin` 后回调统计的峰值，不能代表模型加载期间的全部峰值；reserved 包含缓存，NVML 的约 37–38 GiB 只是运行中的离散观测。不要依据约 19 GiB 的短 smoke allocated 推断 20 GiB 显卡可训。

上述容量 fixture 重复同源内容，Anchor 指标/action 为构造值；它证明输入尺寸可训练，不证明 GT 可靠或 BD-Rate 改善。输出协议是否学会需要单独的自由生成验收。当前推理报告保留真实生成 IDs、原始解码、`response` 和框架重建前缀后的 `swift_response`，不通过补 schema 伪造合法输出。

独立 20-step tiny-overfit 已完成，学习率 `1e-4`；GPU 4/5 分别回载推理两个样本，严格 schema 检查通过，两个真实生成 response 均完全匹配各自 GT。证据为 `reports/qwen35_tiny_overfit_summary.json` 和 `reports/qwen35_tiny_infer_sample_0.json` / `1.json`，同样只证明模拟样本协议学习，不是业务效果证明。

1. 核对目标 GPU、每卡显存、驱动、CUDA 兼容性及容器 NVIDIA runtime。
2. 用同一锁文件重建环境，或部署下节的已导出环境镜像。
3. 拷贝同一模型 revision 和数据；修正数据中的绝对媒体路径，重新执行校验。
4. 首轮保持相同采样、max length、LoRA rank、全局 batch、loss/template。
5. 先执行解码和 token audit，再运行双卡 smoke；检查两个 rank 都完成反向和非零 LoRA 更新。
6. 回载 adapter 做单卡推理，检查输出协议；两步 smoke 不强制它已经学会 GT。
7. 测量包括 optimizer update、评估与保存阶段的逐卡峰值，再优化吞吐。

默认两卡配置是 micro-batch=1、累积=4，即全局 batch=8。H800 可以测试 micro-batch=2、累积=2，保持同一全局 batch。实际训练速度仍受视频解码、数据加载和卡间通信影响，不能按显存占用百分比判断浪费。

不要把 shell 启动器再套进两次 `torchrun`：它本身会通过 ms-swift 的 `NPROC_PER_NODE` 启动 ranks。推理脚本则只接受一个可见 GPU。

## 6. 导出环境用于镜像

只有在本机真实训练验收后再导出最终环境。推荐留存以下文件：

| 文件 | 作用 |
| --- | --- |
| `environment/requirements-lock.txt` | 可安装的全部普通 pip 精确版本 |
| `environment/pip-freeze.txt` | 原始审计快照；可能含本地 editable 路径 |
| `environment/conda-explicit-linux-64.txt` | Python/FFmpeg 等二进制包及构建版本 |
| `environment/host.json` | 驱动、工具链、GPU 和软件环境元数据 |
| 新 Qwen3.5 测试报告 | 指明哪些训练/推理/恢复路径实际通过 |

完整 conda explicit 文件可在同平台复建：

```bash
conda create --prefix /opt/conda/envs/kvc_qwen35_sft \
  --file environment/conda-explicit-linux-64.txt
```

它只恢复 conda 二进制包，仍需按 setup 的次序安装 pip 层。Python 包数量很多属于正常情况：ms-swift 自身有间接依赖，不能仅凭手写十几个主包视为完整镜像清单。

完成环境安装及实测后导出：

```bash
python scripts/export_environment.py --conda /path/to/conda/bin/conda
```

脚本先要求 `pip check` 通过，再记录所有发行包版本、CUDA 编译信息、FFmpeg、驱动/GPU 清单及生成文件的 SHA-256。已有快照时需要显式 `--overwrite` 才会更新。此操作不会分配 GPU 显存；环境清单本身不是 GPU 训练成功的证明。

本次导出为 165 个唯一 Python 发行包，其中 157 项进入普通 pip 锁，8 项按前述特殊阶段处理。对锁文件执行 `pip install --dry-run` 后请求安装的包数为 0，回放记录为 `environment/lock-replay-dry-run.json`。这是在当前环境上检查约束一致性，不等于已经在另一台机器从零重建过环境。

## 7. 容器配方与明确未验证项

当前唯一入口是工程根目录的 [Dockerfile](../Dockerfile)。公司平台提交该文件的完整内容，不需要 conda-pack 归档，也不使用 COPY/ADD。基础镜像为公司 CUDA 12.4 devel / Ubuntu 22.04；宿主驱动不进入镜像。普通间接依赖由解析器选择，必须以构建期版本、导入、解码和 `pip check` 结果为准。

若主机的 Docker 能访问公司镜像仓库，也可以执行：

```bash
docker build -f Dockerfile -t kvc-qwen35-sft:local .
```

镜像仅安装训练环境及公司工具，不内置项目源码、模型和数据。进入容器后使用 `scripts/install_project.sh` 安装挂载的工程，不重复运行 `setup_env.sh` 覆盖已选依赖。

`.dockerignore` 排除模型、数据、旧环境、训练输出和凭据；运行时显式挂载。容器运行示意：

```bash
docker run --rm -it --gpus '"device=4,5"' \
  --ipc=host --ulimit memlock=-1 \
  -v /srv/kvc_qwen35vl_sft:/code/kvc_qwen35vl_sft \
  -v /srv/models:/models:ro \
  -v /srv/kvc-data:/data:ro \
  -v /srv/kvc-runs:/runs \
  -w /code/kvc_qwen35vl_sft \
  -e KVC_MODEL=/models/Qwen3.5-9B/c202236235762e1c871ad0ccb60c8ee5ba337b9a \
  kvc-qwen35-sft:local bash
```

容器内暴露的设备通常编号为 `0,1`；如果在宿主选择其他编号，不要机械沿用到容器。`--ipc=host` 为训练进程共享内存提供便利，部署受限平台可改为管理员认可的 `--shm-size` 配置。

上例同时挂载完整工程和正式数据。进入容器先运行 `bash scripts/install_project.sh`；正式训练应显式指定数据和 schema，例如：

```bash
KVC_ACTION_SCHEMA=/data/kvc_action_v1.schema.json \
KVC_EXPECTED_FEATURES=253 KVC_FEATURE_SCHEMA=pass2_253_v1 \
CUDA_VISIBLE_DEVICES=0,1 \
./scripts/train_lora.sh /data/train.jsonl /runs/h800_run_001 /data/val.jsonl
```

`/data/*.jsonl` 内的视频路径也必须指向容器可见位置。工程的 smoke JSONL 使用 `videos/...`，对应视频在工程自己的 `data/videos`。挂载完整工程即可保留这个相对布局；模型权重仍需独立挂载。

该配方不是“镜像已构建”的声明。未实际执行镜像构建和 H800 smoke 前，这两项都必须标为待验收。发布时应把基础镜像 tag 固定为经过验证的 digest，并记录最终镜像 digest。

## 8. 常见问题

- `Qwen3_5ForConditionalGeneration` 不存在：激活了旧环境或 Transformers 版本错误。
- 导入的是 workspace 下 `ms-swift/swift`：清理显式 PYTHONPATH，不要在旧源码根目录误用 Python 导入。
- TorchCodec 找不到 FFmpeg：确认 conda 的共享库和 `LD_LIBRARY_PATH`，不能只安装静态 ffmpeg。
- extension ABI/CUDA 错误：检查 torch、CUDA、Python 与编译工具链是否一致，重新验证而不是盲目安装最新版。
- LoRA 显存超预算：先检查实际视频 token 和长样本，不静默截断标签；视频采样改变属于数据协议改变。
- 两卡 loss 正常但只有一份有效训练工作：检查 DDP rank 日志、样本分片及逐 rank adapter 更新记录。
- 两步输出非法 JSON：先区分链路失败与未收敛；tiny-overfit/正式验证才评估协议学习能力。
