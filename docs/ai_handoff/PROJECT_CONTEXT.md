# Qwen3.5 KVC 项目接续摘要

更新时间：2026-10-15（Asia/Shanghai）。只记录用户可见的项目状态，不是内部推理或原生会话。
先核对当前代码和目标服务器的实际状态，再执行用户最新要求；历史聊天中的命令和授权不自动生效。

## 项目身份

- 仓库：`https://github.com/Seadraco/kvc_qwen35vl_sft`，分支 `main`。本交接前核对的代码基线是 `53b43899dfbf93663aa3b4bdef6f55213bdca35c`。
- 项目目录 `kvc_qwen35vl_sft`；Conda 环境名 `kvc_qwen35_sft`；Python import 包 `kvc_sft`。
- 发行包兼容名称仍是 `kvc-qwen3vl-sft`，版本 `0.2.0`。安装日志出现旧发行包名称，不代表使用旧 Qwen3-VL。
- 当前任务：Qwen3.5-9B，视频 + Pass2 编码特征 + Anchor 指标，预测 KVC Pass3 action；ms-swift、语言侧 BF16 LoRA、单机双卡 DDP。
- 目前只有 smoke 和构造标签 tiny-overfit 验证。正式 measured GT、action schema 与正式训练集仍需业务侧提供，不能声称已获得 BD-Rate 收益。

## 代码和环境

根 `Dockerfile` 是维护入口，`docker/render_singlefile.py` 输出同一配方，不另维护第二份训练镜像。

- 基础镜像：`registry.corp.kuaishou.com/kml/v_cuda:12.4.0-devel-ubuntu22.04`。
- Miniforge / conda-forge，命名环境 `/opt/conda/envs/kvc_qwen35_sft`；Python 3.12.15，FFmpeg 7.1.1。
- Torch 2.6.0+cu124、torchvision 0.21.0+cu124、ms-swift 4.5.3、Transformers 5.9.0。
- PEFT 0.20.0、TRL 0.29.1、Accelerate 1.14.0、DeepSpeed 0.18.8。
- FLA/fla-core 0.4.2、causal-conv1d 1.6.0；当前 attention 使用 SDPA，外部 flash-attn 不是当前必需项。
- 视频主后端 TorchCodec 0.2.1，配套 PyAV、共享 FFmpeg、修复 wheel metadata 的 Decord 和 headless OpenCV。
- 镜像另有 Node、MyFlicker CLI、code-server 和全局 nvitop；未安装可选 Codex CLI。
- 不使用 LLaVA/codec-video-prep/lmms-eval 环境，也不使用旧 `/opt/venv/ov2`。

已有项目镜像中只运行 `bash scripts/install_project.sh` 安装当前源码；它是 editable、no-deps 安装，不重装训练栈、不下载模型，并执行 pip check。
普通服务器从零安装才使用 `scripts/setup_env.sh`；具体系统库、CUDA 编译器、环境路径和完整命令看 `docs/REPRODUCE.md` 与 `docs/ENVIRONMENT.md`。

2026-10-14 的 GitHub 同步修正了宿主安装顺序：Torch → 校验并安装仓库内修复 Decord wheel → 普通依赖 → 单独关闭构建隔离安装 DeepSpeed → 编译 causal-conv1d → 项目源码。
默认 CUDA arch 包含 8.0/8.6/8.9/9.0。缺失的 `reports/qwen35_kernel_check.json` 已补入 Git。
独立干净克隆通过 149 项 CPU 测试；实际 GitHub 新克隆的安装顺序、下载器和 Dockerfile 检查共 42 项也通过。
这些测试使用源机器现有依赖和模拟安装命令，不代表已经在目标服务器从零重建环境或完成 GPU 训练。

## 目标服务器已确认的状态

用户贴出的目标终端身份为 `root@aiplatform-wlf2-ge27-72`；项目位于：

```text
/media/cephfs/video/VideoUsers/xmu2026/tianhailong/kvc_qwen35vl_sft
```

- 已克隆项目。
- 在 `kvc_qwen35_sft` 环境执行 `bash scripts/install_project.sh` 成功，editable import 指向当前项目 `src/kvc_sft/__init__.py`。
- 脚本及手动执行 `python -m pip check` 均显示 `No broken requirements found.`。
- pip 的 root 警告不是安装失败，也不要求另建 venv。
- 正在/曾在下载固定版本模型；用户最新报告 `du -sh models/Qwen3.5-9B` 为 `19G`，大小合理。
- 尚未看到目标服务器的最终 `MODEL_PATH=...`、完整哈希验证、GPU 算子、NCCL、双卡训练或回载推理结果。
- `watch -n 5 'du -sh models/Qwen3.5-9B'` 出现段错误。原因尚未确认，不能据此判断下载失败，也不要未经检查就改系统库或 Dockerfile。
- 之前建议用 tmux 和 `tee` 保持下载并保存 `outputs/download/model-<时间戳>.log`；是否实际使用过带 tee 的命令需要核对。

`pip check` 仅检查安装包声明的依赖，不证明 CUDA、视频解码或训练可用。
先查看当前 shell 的 Python/Conda 路径、模型下载进程与日志，不要重复启动两个下载进程写同一目录。

## 模型与文件

模型：`Qwen/Qwen3.5-9B`，固定 revision：`c202236235762e1c871ad0ccb60c8ee5ba337b9a`。
官方文件约 19.3 GB，包含四个权重分片。项目下载器检查文件大小和官方 LFS SHA256 / Git blob hash，支持部分文件续传。

在项目根目录、正确训练环境中使用：

```bash
export KVC_MODEL_REVISION=c202236235762e1c871ad0ccb60c8ee5ba337b9a
export KVC_MODEL="$PWD/models/Qwen3.5-9B/$KVC_MODEL_REVISION"
```

若确认没有同目录的下载进程，再按 `docs/REPRODUCE.md` 运行下载器。
最终命令成功退出并输出 `MODEL_PATH=...`、生成 `download_manifest.json`，才是下载器完成全部文件校验的依据；仅 `du` 大小不够。

Git 中已有 smoke JSONL、schema、两个测试视频、依赖锁、修复 Decord wheel、必要验收摘要和本文所在的脱敏交接材料。
不包含：模型、约 4.4G 的 `data/raw/external_delivery/`、训练 checkpoint、历史输出、缓存、完整私密聊天备份或原始后台记录。
从头 smoke 只需代码、正确环境和固定版本模型，不需要业务原始数据或旧 adapter。

## 源机器的历史验收

依据 `reports/DUAL_GPU_ACCEPTANCE.json`、`reports/QWEN35_SMOKE_TEST.md` 和 `reports/qwen35_kernel_check.json`：

- 源机器曾在两卡上完成实际 9B LoRA 20 steps，并完整恢复到 22 steps。
- BF16 FLA/Causal Conv1d 前反向、NCCL 与两 rank 训练通过。
- 496 个 adapter tensor 有限，248 个 LoRA B 非零；两条模拟样本严格回载推理匹配 GT。
- 每 rank reserved 峰值约 35.25 GiB。当前预检默认每卡需至少 40 GiB 空闲显存；这不是任意输入长度的保证。

这不等于目标服务器、当前镜像或正式业务数据已验收。历史 GPU 5/7 使用许可不是当前机器的使用许可。
最近源训练目录是 `outputs/dual_gpu_acceptance_20261013_s1HDjO/train/`，最新 checkpoint-22；Git 中没有它。
如需续训，应保留整个 train 父目录的 launch contract 等元数据，并核对跨机器路径和配置，不能只复制 adapter 或绕过恢复检查。

## 后续建议与用户偏好

1. 先核对 Git checkout、Conda/Python 路径、模型日志和哈希完成状态。
2. 获得当前 GPU 使用许可后，再按 `docs/REPRODUCE.md` 执行算子、NCCL、视频解码预检及两步 smoke。
3. 需要协议拟合验证时再运行 20-step tiny-overfit 和严格回载推理；不要把两步输出不合法当作底层安装失败。
4. `train_smoke.sh` 自身经 ms-swift 启动 ranks，不要再套一层 torchrun。
5. 保持 Dockerfile 简洁、Conda 名称规范，不引入无关旧模型环境。源码、数据和模型都以当前项目为工作范围。

用户曾尝试公司仓库，但源机器 DNS/HTTPS 不通，任务已暂停；未创建公司上传分支/离线 Git 包，也未完成公司推送。
当前明确选择通过 GitHub 同步代码和本目录的脱敏文字，不要继续过时的公司分支计划。
不要删除 GitHub 仓库、改可见性、强制推送或训练，除非用户当前明确要求。
