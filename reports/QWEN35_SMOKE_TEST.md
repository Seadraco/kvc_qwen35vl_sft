# Qwen3.5-9B 双卡 LoRA 验收报告

日期：2026-10-09 UTC。项目：`kvc_qwen3vl_sft`。本报告只描述当前 Qwen3.5 测试，旧 `SMOKE_TEST.md` 是 Qwen3-VL-2B 历史记录。

## 1. 当前结论与边界

在本机 GPU **4、5** 的两张单卡约 48 GiB RTX 4090 上，已完成：

- 新独立 conda 环境、完整依赖锁、真实 FLA/Causal Conv1d BF16 kernel 前反向。
- 双 rank NCCL 通信和 BF16 反向检查。
- 117 项 CPU 测试。
- 两条模拟样本的两步真实 DDP LoRA 训练、参数更新及 checkpoint 保存。
- 从 checkpoint-2 恢复完整优化器/调度器/RNG 状态并训练到 step 4。
- 真实 253 维特征 + 外部视频的 6,907-token 输入容量测试，两次优化器更新。
- 训练/推理非思考 prompt 逐 token 一致性审计，以及框架返回文本与真正生成 token 的解码区分。
- 独立 20-step tiny-overfit，两条样本单独回载、严格自由生成均合法并完全匹配各自 GT。

短训链路与协议学习分开验收：四步模型的两条样本早期推理不合法，原始诊断报告保留；独立 20-step 模型的两条严格推理通过，详见第 9 节。

本报告**不证明**真实 held-out 泛化、编码工具选择收益或 BD-Rate 降低。尚未实际在 H800 上运行，也未构建或运行 Docker 镜像。

## 2. 硬件、环境与权重

| 项目 | 实际配置 |
| --- | --- |
| 训练 GPU | 物理 4/5，RTX 4090，每卡 49,140 MiB（约 48 GiB），同 NUMA/PIX |
| 进程映射 | rank 0 → 可见 cuda:0 → 物理 4；rank 1 → 可见 cuda:1 → 物理 5 |
| 驱动 / CUDA toolkit | 550.54.14 / `/usr/local/cuda-12.4` |
| conda 环境 | `/home/tianhl/anaconda3/envs/kvc_qwen35_sft` |
| Python | 3.12.15 |
| PyTorch / torchvision | 2.6.0+cu124 / 0.21.0+cu124 |
| ms-swift / Transformers | 4.5.3 / 5.9.0 |
| PEFT / TRL / Accelerate | 0.20.0 / 0.29.1 / 1.14.0 |
| FLA / fla-core / Causal Conv1d | 0.4.2 / 0.4.2 / 1.6.0 |
| 视频读取 | qwen-vl-utils 0.0.14、TorchCodec 0.2.1、conda FFmpeg 7.1.1 |
| 分布式方法 | 单机双卡 DDP，无量化、无 ZeRO、无 CPU offload |
| 模型 | `Qwen/Qwen3.5-9B` |
| 模型 revision | `c202236235762e1c871ad0ccb60c8ee5ba337b9a` |
| 模型目录 | `/home/tianhl/compression/kuaishou/model_cache/Qwen3.5-9B/c202236235762e1c871ad0ccb60c8ee5ba337b9a` |

原计划使用的 0/1 在准备期间被其他任务占用，因此训练切换到空闲 4/5，未终止或改变其他用户任务。早期 kernel 检查使用过当时可用的 GPU 0，不应与后面的双卡训练设备混淆。

权重由固定 revision 下载并校验官方 LFS SHA-256/Git blob SHA-1；运行 manifest 再固定实际加载文件的内容摘要。旧 Qwen3-VL adapter 未用于此模型。

### 环境验收

- `pip check`：通过。
- 环境包含 165 个唯一 Python 发行包；普通 pip 锁包含 157 项精确版本，另 8 项按 conda/bootstrap/CUDA wheel/扩展/项目步骤安装。
- 普通锁文件回放 dry-run：请求安装 0 项，见 [lock-replay-dry-run.json](../environment/lock-replay-dry-run.json)。
- conda-pack 静态收集：72,036 文件通过，未忽略缺失文件，未生成归档。
- 完整环境快照：[host.json](../environment/host.json)、[requirements-lock.txt](../environment/requirements-lock.txt)、[conda explicit](../environment/conda-explicit-linux-64.txt)、[export-manifest.json](../environment/export-manifest.json)。

安装过程中处理了 Causal Conv1d 的预编译 ABI 不匹配、Decord wheel 内部错误 ABI 标签，以及 pip/conda 的 setuptools 文件所有权冲突。复现机制和原始 SHA 见 [环境文档](../docs/ENVIRONMENT.md)，没有通过忽略依赖检查掩盖这些问题。

## 3. CPU、kernel 和通信检查

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| CPU 单元测试 | 117 passed，最终回归 3.36 秒 | [qwen35_cpu_tests.log](qwen35_cpu_tests.log) |
| FLA BF16 前向/反向 | 有限输出和梯度，通过 | [qwen35_kernel_check.json](qwen35_kernel_check.json) |
| Causal Conv1d BF16 前向/反向 | 源码构建后通过 | 同上；[构建日志](qwen35_causal_conv1d_build.log) |
| GPU 4/5 双 rank NCCL | 两 rank 的 all_reduce 都为 3.0，BF16 梯度有限 | [qwen35_nccl_gpu45_check.log](qwen35_nccl_gpu45_check.log) |

NCCL 为 2.21.5，通信检查未依赖 `NCCL_P2P_DISABLE` 或 `NCCL_IB_DISABLE` 的强制禁用来通过。

## 4. 两步 smoke

运行目录：`outputs/qwen35_ddp_smoke_v2`。初始 checkpoint：`checkpoint-2`。

| 配置 | 值 |
| --- | --- |
| 样本 | 2 条，16 维模拟特征；视频内容有重叠 |
| 视频采样 | 最多 8 帧，每帧空间预算上限 64 |
| 实际总序列长度 | 771 / 777 token |
| 监督 token | 每条 57；JSON + 响应结束 suffix，空 think 已 mask |
| max length | 2048 |
| batch | 2 ranks × micro-batch 1 × 累积 1 = 2 |
| LoRA | rank 8、alpha 32、dropout 0.05，语言侧 all-linear |
| 精度与内存策略 | BF16、SDPA、梯度检查点，冻结 ViT/aligner |
| 优化器 | AdamW，学习率 `1e-4`，cosine，warmup 0 |

两步 loss 为 `0.4027758241 → 0.2988179624`，梯度范数为 `5.8316779 / 1.5457059`，均有限。496 个 adapter tensor 全部有限，248 个初始为零的 LoRA B tensor 全部非零。

运行时加载总参数 **9,431,452,912**，可训练参数 **21,639,168**，占比 **0.2294%**；可训练视觉参数 **0**。这里使用实际加载参数统计，不能拿旧 2B 的参数量或发布权重全部辅助参数数目替代。

证据：[两步 summary](qwen35_smoke_step2_summary.json)、[token/label 审计](qwen35_smoke_token_audit.json)、[训练日志](qwen35_smoke_train_v2.log)。

## 5. 完整状态恢复：step 2 → 4

使用同一 run 的 `checkpoint-2` 恢复，延长 `max_steps` 至 4，保存 `checkpoint-4`。验证结果：

- optimizer 496 项状态的 step 从 2 变为 4。
- scheduler `last_epoch` 从 2 变为 4。
- 两个 rank 的 RNG 文件均存在。
- 496 个 adapter tensor 全部继续变化，最大绝对变化 `1.4670717064e-05`。
- 新增两步 loss 为 `0.2214317471 / 0.2238210887`。
- 不是只加载 adapter 再初始化优化器，`resume_only_model=false`。

这验证了完整状态恢复与继续更新。由于将原来的两步预算延长为四步，**它不是与“一开始就计划四步”的不中断运行做逐位一致性对照**；正式恢复中断任务应保持原定 schedule。

证据：[resume_verification](qwen35_resume_verification.json)、[step4 summary](qwen35_smoke_step4_summary.json)、run 内 `kvc_resume_events.json`。

## 6. 253D 长输入容量测试

运行目录：`outputs/qwen35_capacity`，完成 2 step 并保存 `checkpoint-2`。

输入视频来自 `external_delivery/source/source.mp4`；253 个数值字段来自 `external_delivery/cae_predict/crf-34/cae-features.json`，没有为缩短序列而删除、舍入或归一化这些原始字段。为了凑满一次全局 batch，复制成 8 条同源记录。

**重要：Anchor 指标和 action 标签来自构造的 smoke 样本，并非该视频的实测指标或最优参数。**每条 fixture 标有 `fixture_only=true`、`label_origin=synthetic_smoke` 等警告，system prompt 也明确容量测试用途。8 条复制记录不是 8 个独立视频，不能用于正式训练、验证或业务效果统计。

| 配置 | 值 |
| --- | --- |
| 特征 | 真实 253 维原始字段，schema `external_delivery_capacity_v0` |
| 视频预算 | 最多 16 帧，空间预算上限 128 |
| 序列长度 | 每条 6,907 token，其中监督 57、mask 6,850 |
| max length | 8192 |
| batch | 2 ranks × micro-batch 1 × 累积 4 = 8 |
| 更新次数 | 2 |
| loss | `0.3880743086 → 0.2599624395` |
| 梯度范数 | `9.1390438 / 8.1540747`，有限 |

全部 adapter tensor 有限，248 个 LoRA B tensor 非零。证据：[capacity summary](qwen35_capacity_summary.json)、[capacity token audit](qwen35_capacity_token_audit.json)、[fixture](qwen35_capacity_fixture.jsonl)、[训练日志](qwen35_capacity_train.log)。

## 7. 显存口径与启动门槛

单位为 GiB；下表来自逐 rank 回调记录：

| 实验 | rank | allocated 峰值 | reserved 峰值 |
| --- | ---: | ---: | ---: |
| smoke step 2 | 0 | 19.0556 | 35.2500 |
| smoke step 2 | 1 | 19.0516 | 35.2500 |
| resume step 4 | 0 | 19.1544 | 35.2480 |
| resume step 4 | 1 | 19.1518 | 35.2500 |
| 253D 容量 | 0 | 25.3004 | 35.2539 |
| 253D 容量 | 1 | 25.3004 | 35.3125 |
| tiny-overfit step 20 | 0 | 19.0556 | 35.2500 |
| tiny-overfit step 20 | 1 | 19.0556 | 35.2500 |

解释时必须保留以下限制：

1. 回调在 `on_train_begin` 后统计 allocated 峰值，不完整覆盖此前模型加载过程中的瞬时分配。
2. reserved 是 PyTorch 分配器保留的显存，包含缓存，不等于活跃张量占用。
3. 两者都不包含所有 CUDA context/NCCL 等进程开销；运行时 NVML 曾观察约 37–38 GiB，但没有做连续采样，不能将它称为精确进程峰值。
4. 不能据 smoke allocated 约 19 GiB 推导“20 GiB 显卡能训”。
5. 当前训练预检默认每卡至少 **40 GiB 空闲**；这是保守启动保护，不保证更长输入或更大 batch 不会 OOM。

这些结果支持“本地双 48 GiB 卡可以运行当前 9B BF16 LoRA 流程”，不支持任意视频输入、全量微调或其他并行策略的显存承诺。

## 8. 推理诊断、原始输出与模板修复

四步模型的两条早期回载推理均完成生成，但未通过 action schema。诊断文件保留：[sample 0](qwen35_infer_sample_0.json)、[sample 1](qwen35_infer_sample_1.json)。两者返回的 JSON 部分均缺少必需的 `schema` 字段，不能算合法输出，也不应自动补字段。

这些早期报告是在解码核对前保存的，旧 `response` 直接使用 ms-swift 的 `message.content`，其中包含框架重新拼回的空 `<think>` 上下文。**不能据此把空思考段说成模型实际生成了思考过程。**原文件保留为诊断证据，不静默改写。

后续 CPU 审计使用同一 checkpoint 参数和真实视频，逐 token 证实训练/推理 prompt 一致：714/720 个前缀 token 完全相同。报告 [inference_prefix_audit](qwen35_inference_prefix_audit.json) 明确说明其中解码 probe 是合成输入，不是新的模型生成。

当前推理实现已经改为保存并区分：

- `generated_token_ids`：真实新增 token IDs。
- `raw_generated_text_with_special_tokens`：直接解码，保留特殊 token。
- `response`：从真实新增 IDs 解码，不重建输入上下文；仅按模板处理终止符，用于严格 schema 校验。
- `swift_response`：框架返回的文本，包括其重建的上下文。
- `non_thinking_context_prefix`：被重建的输入前缀。

代码核对框架返回值与“前缀 + response”一致，不靠任意删除 `<think>` 字符串。模型真正新生成的思考段会保留并判为非法，缺失 schema 也不会修补。当前不强制 schema 响应前缀，要求模型生成完整 JSON。

## 9. 独立 20-step tiny-overfit

状态：**训练、回载、严格 JSON/schema 校验及两条 GT 精确匹配均通过**。

独立目录为 `outputs/qwen35_tiny_overfit`，最终 checkpoint 为 `checkpoint-20`。使用两条原 smoke 样本、`learning_rate=1e-4`、两卡 micro-batch 1、累积 1，全局 batch 2；从基座开始独立训练，与前面的四步恢复 run 分开。

- 第一步 loss：`0.4027758241`。
- 最后一步 loss：`0.0000660859223`。
- 所有 loss、梯度和 adapter tensor 有限；248 个 LoRA B tensor 非零。
- 两个 rank 的 allocated 峰值均为 19.0556 GiB，reserved 均为 35.25 GiB；统计边界同第 7 节。
- sample 0 在物理 GPU 4、sample 1 在物理 GPU 5 单独回载；均未开启 `allow-invalid`，退出码为 0。

| 样本 | prompt token | 新生成 token（含 EOS） | schema_valid | response/canonical_response 与 GT |
| --- | ---: | ---: | --- | --- |
| 0 | 714 | 56 | true | 完全一致 |
| 1 | 720 | 56 | true | 完全一致 |

两条实际 `response`：

```json
{"schema":"kvc_action_smoke_v0","qp":31,"alf":1,"sao":1,"affine":4,"geo":4,"mip":1,"lfnst":1,"sbt":1,"ibc":0}
{"schema":"kvc_action_smoke_v0","qp":33,"alf":1,"sao":0,"affine":0,"geo":0,"mip":1,"lfnst":1,"sbt":0,"ibc":2}
```

报告保留真实生成 token IDs；原始直接解码等于上面 JSON 加 `<|im_end|>`，而 `swift_response` 另外包含输入端空思考上下文。未向生成结果补入 schema 字段，也未通过字符串清洗制造成功。

证据：[tiny-overfit summary](qwen35_tiny_overfit_summary.json)、[sample 0 真实生成报告](qwen35_tiny_infer_sample_0.json)、[sample 1 真实生成报告](qwen35_tiny_infer_sample_1.json)。这证明模型能够学习并复现两条模拟数据的输出协议，仍不证明 held-out 泛化或真实编码收益。全部训练和推理进程完成后，测试 GPU 已释放。

## 10. 复现命令

以下使用新输出目录避免覆盖本报告产物。先确认 GPU 4/5 空闲；H800 上通常改为 `0,1`。工作目录为工程根目录。

```bash
cd /home/tianhl/compression/kuaishou/kvc_qwen3vl_sft
source /home/tianhl/anaconda3/etc/profile.d/conda.sh
conda activate kvc_qwen35_sft
export KVC_MODEL=/home/tianhl/compression/kuaishou/model_cache/Qwen3.5-9B/c202236235762e1c871ad0ccb60c8ee5ba337b9a
export KVC_MODEL_REVISION=c202236235762e1c871ad0ccb60c8ee5ba337b9a
```

两步训练及恢复：

```bash
CUDA_VISIBLE_DEVICES=4,5 KVC_SMOKE_STEPS=2 KVC_SAVE_STEPS=2 \
KVC_SMOKE_LR=0.0001 KVC_MIN_FREE_GIB=40 \
./scripts/train_smoke.sh outputs/reproduce_qwen35_smoke

CUDA_VISIBLE_DEVICES=4,5 KVC_SMOKE_STEPS=4 KVC_SAVE_STEPS=2 \
KVC_SMOKE_LR=0.0001 KVC_MIN_FREE_GIB=40 \
KVC_RESUME_FROM_CHECKPOINT="$PWD/outputs/reproduce_qwen35_smoke/checkpoint-2" \
./scripts/train_smoke.sh outputs/reproduce_qwen35_smoke
```

四步模型的诊断推理允许未收敛输出非法；这两条命令不能作为协议通过判据：

```bash
CUDA_VISIBLE_DEVICES=4 KVC_ALLOW_INVALID=1 \
./scripts/infer_and_validate.sh outputs/reproduce_qwen35_smoke 0
CUDA_VISIBLE_DEVICES=4 KVC_ALLOW_INVALID=1 \
./scripts/infer_and_validate.sh outputs/reproduce_qwen35_smoke 1
```

复现已通过的独立 20-step 配置与严格推理，不设置 `KVC_ALLOW_INVALID`：

```bash
CUDA_VISIBLE_DEVICES=4,5 KVC_SMOKE_STEPS=20 KVC_SMOKE_LR=0.0001 \
./scripts/train_smoke.sh outputs/reproduce_qwen35_tiny_overfit
CUDA_VISIBLE_DEVICES=4 \
./scripts/infer_and_validate.sh outputs/reproduce_qwen35_tiny_overfit 0
CUDA_VISIBLE_DEVICES=5 \
./scripts/infer_and_validate.sh outputs/reproduce_qwen35_tiny_overfit 1
```

容量 fixture 生成及训练；放宽原始特征范围仅限此压力测试，不是正式归一化方案：

```bash
python scripts/make_capacity_fixture.py \
  --copies 8 --output reports/reproduce_capacity_fixture.jsonl

CUDA_VISIBLE_DEVICES=4,5 \
KVC_ACTION_SCHEMA="$PWD/schemas/kvc_action_smoke_v0.schema.json" \
KVC_EXPECTED_FEATURES=253 KVC_FEATURE_SCHEMA=external_delivery_capacity_v0 \
KVC_MAX_ABS_FEATURE=1e308 KVC_MAX_FEATURE_DECIMALS=100 \
KVC_MAX_LENGTH=8192 FPS_MAX_FRAMES=16 VIDEO_MAX_TOKEN_NUM=128 \
KVC_PER_DEVICE_BATCH_SIZE=1 KVC_GRADIENT_ACCUMULATION_STEPS=4 \
KVC_MAX_STEPS=2 KVC_EXPECTED_STEPS=2 KVC_SAVE_STEPS=2 \
KVC_LOGGING_STEPS=1 KVC_WARMUP_RATIO=0 KVC_MIN_FREE_GIB=40 \
./scripts/train_lora.sh reports/reproduce_capacity_fixture.jsonl outputs/reproduce_qwen35_capacity
```

上面未传 val，是因为 fixture 只有一个源视频，不做有泄漏的随机逐行验证划分。真实数据必须按 source_group 切分。

## 11. 未验证与下一阶段

- H800 运行：尚未实测；先保持模型、依赖、输入和全局 batch 不变复现，再调吞吐。
- 镜像：已有 Dockerfile/环境锁/conda-pack 静态预检，但没有生成归档、构建镜像或运行容器 GPU smoke。
- 其他训练方法：未验收全量微调、QLoRA、ZeRO/offload、FlashAttention 或不同模型规模。
- 业务数据：未完成真实候选 GT 筛选、正式 action table 与跨字段约束、完整独立测试集。
- 业务指标：未接入 Pass3 编码器闭环，没有 BD-Rate 改善结论。

本次交付目标是可复现、可迁移、能实际训练和恢复的 Qwen3.5-9B SFT 工程；后续效果结论需要真实标签及独立编码评估。
