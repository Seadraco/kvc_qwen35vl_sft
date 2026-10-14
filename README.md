# KVC Qwen3.5-9B 双卡 LoRA SFT

当前主线使用 `Qwen/Qwen3.5-9B`、ms-swift Transformers 后端、BF16 语言侧 LoRA 和单机双卡 DDP。以后统一在 **`kvc_qwen35vl_sft`** 目录中维护代码和镜像；Conda 环境仍为 `kvc_qwen35_sft`，Python 包仍叫 `kvc_sft`，发行包名称保留兼容旧记录，不影响当前模型。

## 当前工作目录与镜像入口

工作根目录：`/home/tianhl/compression/kuaishou/kvc_qwen35vl_sft`。根目录的 [Dockerfile](Dockerfile) 是公司镜像配方；源码、配置、依赖、测试、模型和业务样例均在同一工程目录。IDE 打开 [项目工作区](kvc_qwen35vl_sft.code-workspace)。目录迁移与删除记录见 [PROJECT_MIGRATION.json](reports/PROJECT_MIGRATION.json)，早期清理记录见 [CLEANUP_AUDIT.json](reports/CLEANUP_AUDIT.json)。

```text
kvc_qwen35vl_sft/
├── Dockerfile                 # 公司单文件镜像，不需要 COPY/ADD
├── src/kvc_sft/               # 当前项目代码
├── scripts/                   # 安装、检查、训练和推理入口
├── configs/                   # Qwen3.5 LoRA / DDP / SDPA 配置
├── schemas/                   # 已提供的 smoke 协议
├── data/                      # smoke JSONL 与两条自包含测试视频
│   └── raw/external_delivery/  # 实际业务编码样例，不包含在代码上传包中
├── models/                    # 本地 Qwen3.5-9B 权重，不包含在代码上传包中
├── requirements.txt           # 当前训练直接依赖
├── constraints-tested.txt     # 关键版本兼容约束
├── environment/               # 原环境锁和导出证据，不代表新镜像已验收
├── tests/                     # CPU 单元测试及镜像完整性检查
├── reports/                   # Qwen3.5 测试及当前镜像审计
├── outputs/history/           # 保留的历史 checkpoint，不包含在代码上传包中
└── docker/                    # 当前镜像使用说明和单文件输出工具
```

公司平台提交本目录 `Dockerfile` 的完整内容。构建不需要 GPU；模型、正式数据和密码不要写入镜像。进入新镜像并挂载工程后执行：

```bash
cd /code/kvc_qwen35vl_sft                # 换成实际挂载路径
bash scripts/install_project.sh         # 只安装项目，不重新解析训练依赖
conda env list
python -m pip check
python -m kvc_sft.check_env              # 检查全部项目依赖与模型实现

# 在分配了 GPU 的容器中依次验证；这些命令不加载 9B 权重。
CUDA_VISIBLE_DEVICES=0 python scripts/check_kernels.py
CUDA_VISIBLE_DEVICES=0,1 torchrun --standalone --nproc_per_node=2 scripts/check_distributed.py
CUDA_VISIBLE_DEVICES=0,1 bash scripts/preflight.sh

# 提供固定 revision 的模型后，再进行实际两步训练。
export KVC_MODEL=/models/Qwen3.5-9B/c202236235762e1c871ad0ccb60c8ee5ba337b9a
CUDA_VISIBLE_DEVICES=0,1 bash scripts/train_smoke.sh outputs/container_smoke
```

镜像默认激活 `/opt/conda/envs/kvc_qwen35_sft`；初始化脚本补回 Node/CUDA PATH，nvitop 有固定使用该 Conda 的全局入口。`PASSWORD='你的强密码' run-code-server 15248 /code/kvc_qwen35vl_sft` 启动编辑器；浏览器 URL 中的旧 `folder/workspace` 参数仍需清除。

当前训练使用 SDPA，外部 `flash-attn`、vLLM、Megatron、LLaVA/codec 环境不是必需依赖，也未加入镜像。Codex CLI 属于可选开发工具，本配方尚未安装它；这不影响 Qwen3.5 SFT。

既有训练产物已迁入 `outputs/history/kvc_qwen3vl_sft/`，最近双卡验收仍位于 `outputs/dual_gpu_acceptance_20261013_s1HDjO/`。checkpoint 的模型与工作路径随目录迁移；原始元数据和旧源码存档位于 `outputs/migration_archive/`。历史报告和日志里的旧目录名是当时的真实路径，不代表运行依赖；这些实测记录不等同于新镜像验收。

实际业务样例位于 `data/raw/external_delivery/`，方案与背景资料位于 `docs/business/`。业务样例仍需按正式 action 协议加工，不能直接把容量测试中的模拟标签当作训练 GT。

旧项目目录 `kvc_qwen3vl_sft/` 和外层重复 `model_cache/` 已移入系统回收站，可恢复；当前运行不再依赖它们。模型的唯一工作副本位于本项目 `models/`。旧源码的独立备份为 `outputs/migration_archive/kvc_qwen3vl_sft-source-before-removal-20261013.tar.gz`，历史报告中的原始路径保留作实测记录。

本地两张 48 GiB 4090 与远端两张 H800 使用同一模型、模型 revision 和训练方法。先复现相同配置，再优化每卡 batch 和梯度累积。本机 GPU 4/5 已完成 9B 双卡 LoRA 两步训练、恢复至第 4 步、真实 253 维特征容量测试，以及独立 20-step tiny-overfit；后者两条样本严格推理均合法且完全匹配 GT。完整验收见 [Qwen3.5 测试报告](reports/QWEN35_SMOKE_TEST.md)。H800 与镜像尚未实际验收。

本目录只保留 Qwen3.5 的测试证据；SFT 链路通过不代表模型已降低 BD-Rate。

## 1. 文档导航

- [环境、依赖、镜像与迁移](docs/ENVIRONMENT.md)：从空环境安装、库分类、CUDA 扩展、下载、锁文件及 H800 验收。
- [本次镜像审计记录](reports/IMAGE_AUDIT.json)：依赖覆盖、CPU 检查、路径与尚未完成的容器/GPU 验收。
- [当前目录双卡验收记录](reports/DUAL_GPU_ACCEPTANCE.json)：GPU 5/7 的实际训练、回载推理及上传包验证状态。
- [当前 SFT 实现](docs/SFT_IMPLEMENTATION.md)：数据、视频 token、非思考模板、监督掩码、损失、LoRA 和 DDP。
- [设计记录](DESIGN.md)：为什么保留原协议、哪些地方升级、哪些功能仍未实现。
- [Qwen3.5 实测报告](reports/QWEN35_SMOKE_TEST.md)：双卡训练、完整状态恢复、长输入容量及推理的分项验收。

## 2. 环境与模型

在工程目录执行。以下是没有使用项目镜像时的宿主重建流程；在已有镜像中只运行上面的 `install_project.sh`，不要重复运行 `setup_env.sh` 覆盖镜像已选依赖。环境名是 `kvc_qwen35_sft`，不会覆盖 base 或旧环境：

```bash
cd /home/tianhl/compression/kuaishou/kvc_qwen35vl_sft
CUDA_VISIBLE_DEVICES=4,5 \
KVC_CONDA_BIN=/home/tianhl/anaconda3/bin/conda ./scripts/setup_env.sh
source /home/tianhl/anaconda3/etc/profile.d/conda.sh
conda activate kvc_qwen35_sft
conda env list
```

其他服务器请替换 conda 路径。需要手动指定环境时，所有入口均支持 `KVC_SFT_ENV_PREFIX`。TorchCodec 需要共享 FFmpeg 库，只有一个静态 `ffmpeg` 可执行文件不够。

固定模型 revision 为 `c202236235762e1c871ad0ccb60c8ee5ba337b9a`。若模型未缓存，可执行可恢复且校验上游哈希的下载器：

```bash
python scripts/download_model.py \
  --destination models/Qwen3.5-9B/c202236235762e1c871ad0ccb60c8ee5ba337b9a
export KVC_MODEL="$PWD/models/Qwen3.5-9B/c202236235762e1c871ad0ccb60c8ee5ba337b9a"
export KVC_MODEL_REVISION=c202236235762e1c871ad0ccb60c8ee5ba337b9a
```

设置 `KVC_MODEL` 使用本地目录可避免重复下载。不要用 Qwen3-VL 的旧 adapter 配合这个模型。

## 3. 双卡 smoke

先确认选用的 GPU 空闲；下面使用本次训练选择的 `4,5`，不是自动预约。启动器默认 `0,1`，可通过 `CUDA_VISIBLE_DEVICES` 覆盖；原计划的 0/1 被其他任务占用后，本次切换到同 NUMA 的空闲 4/5。脚本不会终止其他人的 GPU 进程。

训练预检默认要求每卡至少 **40 GiB 空闲显存**（`KVC_MIN_FREE_GIB=40`）。这是为当前约 35.3 GiB 的 PyTorch reserved 峰值及额外运行开销保留余量，不是任意视频长度的显存保证。

```bash
nvidia-smi
CUDA_VISIBLE_DEVICES=4,5 ./scripts/run_smoke.sh
```

只运行训练并显式设置新输出目录：

```bash
CUDA_VISIBLE_DEVICES=4,5 \
  ./scripts/train_smoke.sh outputs/qwen35_smoke_manual
```

默认两步训练仅用于验证视频解码、token 化、两 rank 前反向、LoRA 更新和 checkpoint 保存。两步后的输出不保证已学会 action schema，更不能证明实际编码收益。回载推理是单卡操作：

```bash
CUDA_VISIBLE_DEVICES=4 KVC_ALLOW_INVALID=1 \
  ./scripts/infer_and_validate.sh outputs/qwen35_smoke_manual 0
```

去掉 `KVC_ALLOW_INVALID=1` 会严格要求生成结果满足 JSON/action schema。当前推理不强制补入 schema 前缀；`response` 从实际新生成的 token IDs 解码，并保留 `swift_response` 与原始 token/text 供核对，不靠修补输出通过校验。

已验证的 tiny-overfit 使用 **20 step、学习率 `1e-4`**。以下用新目录复现相同配置，避免覆盖本次验收产物；两条推理都使用严格模式，不设置 `KVC_ALLOW_INVALID`：

```bash
CUDA_VISIBLE_DEVICES=4,5 KVC_SMOKE_STEPS=20 KVC_SMOKE_LR=0.0001 \
  ./scripts/train_smoke.sh outputs/reproduce_qwen35_tiny_overfit
CUDA_VISIBLE_DEVICES=4 ./scripts/infer_and_validate.sh outputs/reproduce_qwen35_tiny_overfit 0
CUDA_VISIBLE_DEVICES=5 ./scripts/infer_and_validate.sh outputs/reproduce_qwen35_tiny_overfit 1
```

回载推理使用本工程 `outputs/` 中本次生成的 run/checkpoint。历史 checkpoint 不属于上传代码，不能假设远端服务器存在原主机的旧目录。

示例包含 16 维模拟特征、两个重叠内容视频，只能验证工程和过拟合协议。不能作为真实 KVC 训练集或 BD-Rate 结论。

已完成的核心测试如下：

| 测试 | 已记录结果 | 边界 |
| --- | --- | --- |
| 双卡 2-step smoke | loss `0.4028 → 0.2988`；每卡 reserved 峰值 35.25 GiB | 证明前反向、更新和保存，不代表输出收敛 |
| 完整状态恢复至 step 4 | optimizer/scheduler step `2 → 4`，496 个 adapter tensor 全部继续变化 | 延长训练预算测试，不是逐位一致性实验 |
| 253D 输入容量 | 16 帧/空间预算 128、6,907 token、全局 batch 8，完成 2 step；allocated 峰值约 25.30 GiB、reserved 最大 35.3125 GiB | 8 条重复容量 fixture，Anchor/target 为构造值，不是实际编码 GT |
| 独立 20-step tiny-overfit | loss `0.4028 → 0.00006609`；两条严格自由生成均合法且完全等于 GT，每卡 reserved 峰值 35.25 GiB | 只证明能够拟合这两条模拟样本，不证明泛化 |

实际 LoRA 可训练参数为 21,639,168，占加载模型总参数 9,431,452,912 的约 0.2294%；可训练视觉参数为 0。

allocated 数值是回调从 `on_train_begin` 后统计的活跃张量峰值，不覆盖此前加载瞬时峰值；reserved 包含分配器缓存。不能依据 smoke 的约 19 GiB allocated 推断 20 GiB 显卡可训练。NVML 运行中仅观察到约 37–38 GiB，并非连续采样得到的精确进程峰值。

## 4. 正式数据和训练

保留既有 measured-record → ms-swift JSONL 流程。GT 必须由实际编码候选及统一选择规则得到；准备脚本不会根据 Anchor/CAE 指标自动猜测最优参数。

```bash
python -m kvc_sft.prepare_dataset \
  --input data/measured_train.jsonl \
  --output data/train.jsonl \
  --schema schemas/kvc_action_v1.schema.json
```

`kvc_action_v1.schema.json` 是正式协议的示意路径，当前仓库仅附带 smoke schema；请先提供真实 CAE action table。正式数据按原视频或内容族的 `source_group` 切分，不能让同源相邻片段跨 train/val/test。

```bash
KVC_ACTION_SCHEMA="$PWD/schemas/kvc_action_v1.schema.json" \
KVC_EXPECTED_FEATURES=253 \
KVC_FEATURE_SCHEMA=pass2_253_v1 \
CUDA_VISIBLE_DEVICES=4,5 \
KVC_PER_DEVICE_BATCH_SIZE=1 \
KVC_GRADIENT_ACCUMULATION_STEPS=4 \
./scripts/train_lora.sh data/train.jsonl outputs/qwen35_formal data/val.jsonl
```

全局 batch = `GPU 数 × 每卡 batch × 梯度累积`，上例为 8。H800 首次复现保持相同配置；随后可测试每卡 batch=2、累积=2，而不是只增大 batch 后无意改变全局 batch。

训练入口先做 schema/媒体/泄漏校验与真实 token 审计，然后启动 ms-swift。formal 默认 `max_length=8192`，因为真实 253 维字段文本可以超过 5,000 token；smoke 仍为 2048。默认不截断超长样本；不要为省显存默默截掉特征或标签。当前 formal 使用 16 帧/每帧空间预算上限 128，smoke 为 8 帧/64；这些是工程起点，不是最优视频采样方案。

## 5. 产物与边界

每次使用独立输出目录。LoRA checkpoint、优化器/调度器状态、逐 rank 运行记录、token audit 和 `kvc_run_manifest.json` 用于验收及回载。模型文件、采样配置、特征 schema 等必须保持一致；迁移路径时要遵守 manifest 的校验规则，不能直接手工删除安全检查。

两端是相同 9B 基座和 revision，因此 adapter 可以迁移加载；如果要精确续训，还必须迁移优化器、调度器及训练状态，而不只是 adapter。

尚未实现：真实 253 维字段治理、正式 action table、候选搜索/GT 选择器、生产编码器调用、批量业务评估和多点 BD-Rate。SFT 采用传统 assistant-only 自回归交叉熵，不在线调用编码器，也不直接对 BPP/LPIPS 求导。

本环境涵盖当前 SFT、媒体检查、离线推理、测试和报告所需依赖。为避免 CUDA/PyTorch 冲突，vLLM、Megatron 等另一套执行栈不预装；详见环境文档。

## 6. 只上传当前工程

只需上传本目录，不需要外层旧项目、相邻 `ms-swift` 源码仓库或外层 workspace 文件。训练框架使用目标 Conda 环境中安装的 ms-swift；脚本不再假定作者主目录下的 Conda 路径。

建议使用下面的入口生成上传包，而不是把整个含权重的工作目录直接压缩：

```bash
bash scripts/package_upload.sh
```

生成文件位于本工程 `outputs/uploads/`，解压后只有一个项目目录。包内包含 Dockerfile、源码、配置、schema、依赖文件、脚本、测试和自包含 smoke 视频；排除 `models/`、`data/raw/`、缓存、Conda/venv、checkpoint、其他 outputs 和常见凭据文件。这是代码与测试资源包，不是模型权重包或 Docker 镜像二进制。

远端构建并启动本目录的 Dockerfile，上传/挂载工程后执行：

```bash
cd /code/kvc_qwen35vl_sft               # 换成实际解压或挂载位置
bash scripts/install_project.sh
export KVC_MODEL=/models/Qwen3.5-9B/c202236235762e1c871ad0ccb60c8ee5ba337b9a
CUDA_VISIBLE_DEVICES=0,1 KVC_SMOKE_STEPS=20 \
  bash scripts/train_smoke.sh outputs/server_smoke
```

模型权重与正式数据需要单独提供。两条 smoke 数据不能替代正式训练集，也不能保证业务效果。
