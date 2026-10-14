# 当前公司镜像入口

以后只维护工程根目录的 [Dockerfile](../Dockerfile)，来源是已选定的 `Dockerfile.thl`。公司平台提交该文件全部内容即可；它没有 COPY/ADD，也不要求先打包 Conda 环境。不要再使用外层 `Dockerfile` 或 `docker_delivery/Dockerfile` 作为本工程入口。

当前配方包含命名 Conda `kvc_qwen35_sft`、Qwen3.5 LoRA/DDP 训练依赖、CUDA 源码扩展、FFmpeg 视频栈、Node/MyFlicker/code-server 和全局 nvitop。Conda 登录初始化补回 Node/CUDA PATH，code-server 忽略历史目录；没有 codec/LLaVA、额外 venv，也没有安装可选 Codex CLI。

镜像中的训练依赖按以下顺序安装：Conda → 官方 cu124 Torch/torchvision → 修复后的 Decord wheel → 普通项目依赖 → DeepSpeed → Causal Conv1d。Decord 只安装修复版一次；普通依赖中的 Torch/torchvision/Decord 固定版本是解析保护，已满足时不会重新安装。DeepSpeed 仍解析自己的运行依赖，只有它和 Causal Conv1d 的源码构建关闭隔离。Ninja 仅由 pip 显式安装，不再同时通过 apt 和 Conda 安装。

ABI 检查使用公开接口 `torch.compiled_with_cxx11_abi()`，预期值针对当前固定的 Torch cu124 构建；升级 Torch 时需重新验证扩展 ABI。登录 shell 和非登录交互终端分别保留 PATH 验收，构建时交互 Bash 的无 TTY job-control 警告不表示检查失败。主机安装脚本 `scripts/setup_env.sh` 的完整锁恢复流程独立于这份简化镜像配方，本次不修改该脚本或已有环境。

进入镜像并挂载工程后：

```bash
cd /code/kvc_qwen35vl_sft
bash scripts/install_project.sh
python -m kvc_sft.check_env
CUDA_VISIBLE_DEVICES=0 python scripts/check_kernels.py
CUDA_VISIBLE_DEVICES=0,1 torchrun --standalone --nproc_per_node=2 scripts/check_distributed.py
```

随后提供固定 revision 的模型，再运行 `scripts/preflight.sh` 和 `scripts/train_smoke.sh`。详细步骤见 [项目 README](../README.md)。软件安装成功、CPU 测试通过和目标 GPU 实际训练通过是不同的验收阶段。

`render_singlefile.py` 只输出本工程根目录 Dockerfile，不生成另一份镜像配方。需要单文件输出时执行：

```bash
python docker/render_singlefile.py
```

旧镜像配方、codec 工具和历次交付记录已移出本工程，不参与当前构建。最新镜像审计见 [IMAGE_AUDIT.json](../reports/IMAGE_AUDIT.json)，可恢复清理记录见 [CLEANUP_AUDIT.json](../reports/CLEANUP_AUDIT.json)。
