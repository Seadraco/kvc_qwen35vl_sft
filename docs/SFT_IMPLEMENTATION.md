# Qwen3.5-9B SFT 当前实现

本文对应 Qwen3.5-9B、ms-swift 4.5.3、BF16 LoRA、单机双卡 DDP。验收只引用当前模型的测试报告，不沿用其他模型的 token 数、参数量或显存数据。

## 1. 完整数据链路

```text
原视频与编码候选测量
        ↓ 上游选择唯一 GT，不在本工程中执行
measured-record JSONL
        ↓ prepare_dataset.py
ms-swift JSONL：messages + videos + 追踪字段
        ↓ schema / 媒体 / 特征 / 数据泄漏检查
真实 processor 与非思考模板 token 审计
        ↓ 每个 DDP rank 处理自己的 batch
视频张量 + 文本 input_ids + labels
        ↓ 冻结 Qwen3.5 基座 + 语言侧 LoRA
assistant-only 下一 token 交叉熵
        ↓ 反向、梯度累积、DDP 同步和 AdamW 更新
adapter + 优化器状态 + 运行 manifest
        ↓ 单卡重新加载
JSON 生成 → action schema 校验
```

上游候选搜索、真实编码器调用、候选排序、BD-Rate 计算均不在当前训练环内。当前代码学习模仿已选定的 GT，不在每个训练 step 重新编码视频。

## 2. 原始 record 与渲染数据

一条原始记录包含：

| 字段 | 模型是否直接消费 | 职责 |
| --- | --- | --- |
| `sample_id` | 否 | 唯一追踪样本 |
| `source_group` | 否 | 按原视频/内容族切分，阻止 train/val 泄漏 |
| `video` | 经视频 processor | 与特征、指标、GT 决策范围对齐的原视频片段 |
| `decision_scope` | 是，文本 | clip/GOP 决策范围，不会自动裁切长视频 |
| `video_meta` | 是，文本 | 宽高、帧率、帧数、位深和 HDR |
| `anchor_metrics` | 是，文本 | 当前 Anchor 点的 QP、BPP 和 LPIPS |
| `feature_schema` | 是，文本 | 特征字段语义版本，不会自动加载协议说明 |
| `pass2_features` | 是，文本 | 有序数值特征；当前没有专门的 253D 数值网络 |
| `target` | 是，监督文本 | 经过实际测量和选择得到的完整参数 JSON |

渲染后每行类似：

```json
{
  "sample_id": "sourceA_clip0001",
  "source_group": "sourceA",
  "messages": [
    {"role": "system", "content": "任务和合法 action 枚举……只输出 JSON。"},
    {"role": "user", "content": "<video>\ndecision_scope=clip\nvideo_meta={...}\nanchor_metrics={...}\nfeature_schema=pass2_253_v1\npass2_features={...}"},
    {"role": "assistant", "content": "{\"schema\":\"kvc_action_v1\",\"qp\":31,...}"}
  ],
  "videos": ["/data/sourceA_clip0001.mp4"]
}
```

上述省略号仅说明结构，不是可直接训练的完整样本。训练必须使用准备脚本实际渲染的数据。视频路径不作为自然语言内容传给模型，而用于媒体读取；`<video>` 是媒体占位符。

正式数据仍需提供真实 253 维字段语义、归一化规则和真实 action schema。`kvc_action_smoke_v0` 的测试字段不能自动映射为实际 CAE 的 28 个参数。

## 3. 视频和文本如何变成输入

1. ms-swift 读取每行 `messages/videos`，绑定唯一视频占位符。
2. qwen-vl-utils 经 TorchCodec 解码、按时间采样，并按视觉预算缩放。
3. processor 生成视频 patch 张量、时间/空间网格以及视频位置所需信息。
4. 文本 tokenizer 处理 system、特征、指标、assistant GT 和模板分隔符。
5. 视频占位位置根据实际视觉网格展开，形成与视觉张量一致的序列。
6. collator 按 batch 补齐文本序列，padding 的 label 同样为 `-100`。
7. 冻结视觉塔产生视觉表示，语言网络同时读取视觉表示和文本上下文。

典型字段包括 `input_ids`、`pixel_values_videos`、`video_grid_thw`、`labels`，额外位置字段以当前 processor/template 输出为准。模型不是把 MP4 二进制直接当 token，也不默认读取全部原始帧。

smoke 使用最多 8 帧、空间预算上限 64；formal 使用最多 16 帧、空间预算上限 128。预算值不等于最终每帧固定生成完全相同的 token 数，因为实际长宽比、时间合并和模板都会影响长度。

视频采样相关变量、模型和 schema 被写入 manifest。推理必须保持一致。`torchcodec_compat.py` 对 ms-swift 的 BytesIO 包装进行兼容转换，预检验证真实后端而不是静默改用 Decord。

## 4. 为什么正式长度是 8192

真实 253 维字段名和值本身可以超过 5,000 个文本 token，再加视频和输出标签，不能沿用旧的 4096 上限。formal 起始上限改为 8192，smoke 两条 16 维样本仍为 2048。

这不是断言任意 253 维样本都一定能装入 8192。`audit_token_lengths.py` 用实际模型 processor/template 处理每条样本，发现超长就失败，不静默截断特征或 target。

默认审计全量数据；`KVC_TOKEN_AUDIT_SAMPLES=N` 只覆盖前 N 条，适合开发阶段，不应作为大规模数据全量安全证明。

## 5. 非思考模板和监督范围

Qwen3.5 支持思考与非思考模式。当前任务只要求编码动作，因此统一配置：

```yaml
model_type: qwen3_5
template: qwen3_5
enable_thinking: false
add_non_thinking_prefix: true
loss_scale: ignore_empty_think
```

非思考模板可能插入空的思考段，`ignore_empty_think` 避免把该模板段作为业务 target。监督仍落在 assistant JSON 及有效响应结束 token 上；system、user、视频输入、非监督模板和 padding 使用 `-100`。

这里要检查真实 `labels` 解码，不能仅看 JSONL 的 assistant 字符串，也不能沿用旧模板的固定 token 偏移。训练和推理需要同一非思考协议，否则模型可能先输出 `<think>` 而不是预期 JSON。

当前推理设置 `response_prefix=None`，让模型自行生成包含 schema 字段的完整 JSON，不补入旧版 schema 前缀。非思考模板的空 `<think>` 段属于输入上下文，不应与模型新生成的内容混为一谈。

`reports/qwen35_inference_prefix_audit.json` 已逐 token 比较训练首次监督位置之前的 input IDs 与推理 prompt：两条样本分别为 714/720 token，完全一致；训练总长 771/777，均监督 57 个 token（JSON 加 `<|im_end|>\n`），空思考段被 mask。该报告使用 CPU processor/template 和合成解码探针，不是模型生成成功的替代证据。

## 6. 损失函数

当前使用传统的因果语言模型交叉熵，没有自定义编码损失。令模板序列为 \(x_1,\ldots,x_T\)，在需要监督的位置令 \(y_t=x_t\)，其他位置令 \(y_t=-100\)。logits 与标签错开一位：位置 \(t\) 预测 \(y_{t+1}\)。

\[
\mathcal{A}=\{t\mid y_{t+1}\ne -100\},\qquad
\mathcal{L}=-\frac{1}{|\mathcal{A}|}\sum_{t\in\mathcal{A}}
\log p_\theta(y_{t+1}\mid x_{\leq t}).
\]

上式描述有效监督 token 的均值目标；batch、梯度累积和多 rank 的实际归一化由锁定的 ms-swift/Transformers Trainer 实现完成，而不是在本工程另写一个 loss。有限 loss、非零 adapter 更新以及 label audit 都是训练验收的一部分。

注意：

- JSON 键名、标点、值和结束标记均可能参与监督，不是每个编码参数等权。
- QP 并未使用 MSE，工具开关也未使用单独的 BCE。
- Anchor BPP/LPIPS 是条件输入，不是损失权重或 reward。
- `label_smoothing_factor=0`，不使用标签平滑。
- teacher forcing 使用 GT 前缀；推理使用已生成前缀，两者的误差行为不同。
- 低交叉熵不能直接证明低 BD-Rate，需要独立真实编码评估。

## 7. LoRA 到底更新什么

对可适配的线性层，可以将权重写为：

\[
W'=W+\frac{\alpha}{r}BA.
\]

冻结基座 \(W\)，训练低秩矩阵 \(A,B\)。当前 rank=8、alpha=32、dropout=0.05，使用 `target_modules=all-linear`，同时冻结视觉塔和 aligner，只在语言侧注入适配。

Qwen3.5 含线性注意力与全注意力层，不能用旧 Qwen3-VL 的七个投影名称和 adapter tensor 数硬套。训练时记录实际 trainable 参数、参数分组与 adapter keys，并检查是否误更新视觉基座。

本次实际加载模型总参数为 **9,431,452,912**；可训练 LoRA 参数为 **21,639,168**，比例约 **0.2294%**；可训练视觉参数为 **0**。checkpoint 含 496 个 adapter tensor，其中 248 个零初始化的 LoRA B tensor 在训练后全部产生非零更新。模型发布权重还可能包含未由此训练路径加载的辅助权重，因此这里使用运行时统计，不用名称“9B”估算训练参数比例。

基座使用 BF16，不做 QLoRA。LoRA 参数/优化器状态的实际 dtype 由 PEFT 和优化器控制，不能因为基座 BF16 就假设所有训练张量都是 BF16。冻结基座仍需权重显存和反向相关激活。

## 8. DDP、batch 和显存

每个 rank 在一张 GPU 上保存一份完整 9B 基座和自己的 LoRA 副本，读取不同的数据 shard，再同步可训练参数梯度。两张卡并不是一块合并显存。

\[
B_{\mathrm{global}}=N_{\mathrm{GPU}}\times B_{\mathrm{micro}}\times N_{\mathrm{accum}}.
\]

formal 默认是 2×1×4=8。两条样本的 smoke 默认累积=1、全局 batch=2；重复 epoch 只是链路测试，不能把名义 batch 配置误报为独立视频数量。

初始设置：BF16、SDPA、梯度检查点、micro-batch 1、冻结视觉模块。未开启 packing/padding-free。FLA 用于线性注意力加速；SDPA 用于全注意力，两者同时存在并不矛盾。

环境已包含 DeepSpeed，但默认 YAML 不启用 ZeRO。若未来启用参数分片、CPU offload 或不同 attention kernel，必须重新测试，不作为当前 DDP 测试自动覆盖的功能。

当前训练预检的 `KVC_MIN_FREE_GIB` 默认为 40，要求每张选用卡都满足空闲门槛。两步 smoke 的每卡 PyTorch reserved 峰值为 35.25 GiB；6,907-token 容量测试的 allocated 峰值约 25.30 GiB，reserved 在两卡中的最大值为 35.3125 GiB。reserved 包含分配器缓存，allocated 是活跃张量分配，两者均不等于 `nvidia-smi` 看到的全部进程显存。门槛保留余量，但不能保证更长序列、更高分辨率或更大 batch 不会 OOM。

运行回调在 `on_train_begin` 后统计峰值，不完整覆盖模型加载前后的瞬时分配。尤其不能用 smoke 的约 19 GiB allocated 得出“20 GiB 显卡就能训练”的结论。NVML 运行期间观察约 37–38 GiB，仅为离散观测，不是连续记录得到的精确进程峰值。

## 9. 训练脚本与验收

`scripts/train_lora.sh TRAIN OUTPUT [VAL]` 在控制进程中依次执行：

1. 解析明确的输入/输出/schema 路径，检查输出目录是否可用。
2. 检查可见 GPU 列表无重复，设置正确的单机 rank 数。
3. 校验训练数据、视频属性、特征维数和合法 target。
4. 检查软件版本、每张卡空闲显存及真实视频后端。
5. 使用真实 Qwen3.5 模板执行 token/label 审计。
6. 对独立 val 重复上述审计，并检查 source-group/媒体泄漏。
7. 启动 ms-swift，只有训练部分生成多 rank 进程。
8. 记录 checkpoint、运行状态和 manifest；回载推理独立在单卡执行。

优化器使用 AdamW，学习率 `1e-4`、weight decay 0.1、betas 0.9/0.95、eps `1e-8`，梯度范数裁剪 1.0；formal 使用 cosine 和 5% warmup。smoke 为少量固定 step，不代表这些超参数适合正式数据。

`KVC_RESUME_FROM_CHECKPOINT` 用于加载完整训练状态续训。adapter-only 重新初始化优化器属于另一件事，不应叫精确 resume。继续同一 run 需要保留相应 optimizer/scheduler/trainer state，并保持数据和运行协议一致。

启动前保存 `kvc_launch_contract.json`，记录实际 YAML、CLI、数据、schema、模型和 plugin 内容摘要。恢复时拒绝不允许的契约漂移；延长 max_steps、调整记录/保存频率以及保持全局 batch 不变的 micro-batch/累积调整会单独记录在 `kvc_resume_events.json`。精确恢复中断任务应保持原定训练总步数；将两步预算延长为四步只是恢复与延长预算的链路测试，不能宣称等同于一开始就计划四步的逐位一致训练。

验收不仅检查 checkpoint 文件存在，还检查 adapter tensor 全部有限，以及原本零初始化的 LoRA B 矩阵实际产生非零更新。`KVC_LOGGING_STEPS` 和 `KVC_WARMUP_RATIO` 可显式覆盖，用于少量 step 的容量测试，不应把此类短程超参数当成正式效果实验设置。

## 10. 验收分层及现有数据限制

| 层次 | 检查内容 | 能证明什么 |
| --- | --- | --- |
| 静态/单元测试 | JSON、schema、路径、版本和 manifest | 代码契约可执行 |
| 两步双卡 smoke | 媒体、前反向、有限 loss、LoRA 更新和保存 | 训练主链路可运行 |
| resume + 回载 | 恢复 step、加载 adapter、生成 | checkpoint 可用 |
| tiny-overfit | 小样本协议学习、GT 复现 | 可以拟合测试监督，不证明泛化 |
| 253D 容量测试 | 实际长特征与视频预算的峰值显存 | 输入尺寸可训练，不证明标签可靠 |
| 正式 held-out | 合法率、字段准确率、实际多点编码 | 泛化及 BD-Rate 业务收益 |

容量测试可使用外部片段和真实提取的 253D 字段，但若 Anchor 指标/目标 action 为构造值，必须显著标注 `synthetic_smoke`。它不是完整真实 GT 数据，不能混入正式训练或当作真实率失真结果。

源视频不多时，即使有多条 CRF 扫描也不是同数量的独立内容。正式泛化必须按源视频/内容族切分。

本次已完成的证据：

- `reports/qwen35_smoke_step2_summary.json`：两卡两步，loss `0.4027758 → 0.2988180`，所有 adapter tensor 有限，248 个 B tensor 非零。
- `reports/qwen35_resume_verification.json`：恢复 optimizer/scheduler/RNG，step `2 → 4`，496 个 adapter tensor 全部继续变化；明确不是与不中断四步计划的逐位对照。
- `reports/qwen35_capacity_token_audit.json` 与 `reports/qwen35_capacity_summary.json`：真实提取的 253 维字段、外部视频、16 帧/128 空间预算，共 6,907 token；8 条同源重复 fixture，micro-batch 1、两卡、累积 4，全局 batch 8，完成两次更新。
- `reports/qwen35_tiny_overfit_summary.json`：独立 20-step、学习率 `1e-4`，loss 降至 `6.6085922e-05`；两条 `qwen35_tiny_infer_sample_*.json` 均为 `schema_valid=true`，真实生成 response/canonical_response 分别等于各自 GT，没有强制 schema 前缀或输出修补。

容量 fixture 的 Anchor 指标和 action 是构造值，标签仅用于压力测试；两步训练/恢复成功与独立 tiny-overfit 的协议学习分开验收。20-step 结果证明可以拟合两条模拟样本，但不能证明实际编码收益或泛化。全部证据见 [完整测试报告](../reports/QWEN35_SMOKE_TEST.md)。

## 11. 推理和编码器接入边界

当前推理工具读取带 assistant GT 的离线样本，但构造请求前删除最后一轮 GT。它返回原始生成文本、解析状态、预测值和对照标签；`KVC_ALLOW_INVALID=1` 只放宽 smoke 的验收退出码，不会把非法输出变成合法编码参数。

Qwen3.5 非思考模式下，ms-swift 4.5.3 会在 `message.content` 中重新拼回输入端的空思考前缀。因此报告明确区分：

| 字段 | 含义 |
| --- | --- |
| `generated_token_ids` | 模型实际新生成的 token IDs |
| `raw_generated_text_with_special_tokens` | 上述 IDs 直接解码，保留特殊 token |
| `response` | 从生成 IDs 解码、不重建输入前缀；仅按模板处理结束符，用于严格 action 校验 |
| `swift_response` | 框架返回的 message.content，可能包含重建的空思考上下文 |
| `non_thinking_context_prefix` | 框架拼回的输入上下文前缀，便于逐项核对 |

代码验证 `swift_response` 与“声明的上下文前缀 + response”一致，不靠字符串替换删除任意 `<think>` 内容。如果模型真的新生成思考段，该内容会保留并导致严格 JSON 校验失败。缺少 schema、字段错误等同样直接报错，不自动补字段、不进行 JSON repair。

生产接入仍需另外实现：无标签在线请求、超时/异常处理、跨字段约束、白名单 action-to-encoder 映射、CAE/Anchor 回退和业务评测。绝不能把模型文本直接拼成 shell 编码命令。

## 12. 复现实验

具体命令见 [工程 README](../README.md)，完整环境和镜像配方见 [ENVIRONMENT.md](ENVIRONMENT.md)。训练实际完成到哪个阶段、显存峰值和输出合法率，以新的 Qwen3.5 报告及 run 目录为准，不通过替换文档中的模型名称冒用历史实验。
