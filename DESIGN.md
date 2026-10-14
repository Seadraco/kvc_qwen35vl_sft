# Qwen3.5-9B SFT 设计记录

## 1. 模型与迁移原则

当前模型固定为 `Qwen/Qwen3.5-9B`，revision `c202236235762e1c871ad0ccb60c8ee5ba337b9a`。本地 2×4090 48 GiB 与远端 2×H800 使用同一个模型，不再采用 4B → 27B 的跨尺寸方案。

训练基线是 BF16、语言侧 rank-8 LoRA、冻结视觉塔/aligner、梯度检查点、SDPA、单机 DDP。9B 基座可按完整副本放入每张卡；DDP 用两张卡并行处理样本，不把显存相加。是否能容纳正式视频输入必须根据实际峰值判断。DeepSpeed 包提供后续 ZeRO 实验入口，但默认配置并不启用 ZeRO，安装成功不代表 ZeRO 已验收。

首轮远端复现不改变模型、视频采样、全局 batch 和损失。之后再用每卡 batch 与梯度累积优化吞吐，避免同时改变算法和硬件变量。

## 2. 数据协议继续沿用

每条样本对应对齐的 clip/GOP：原始视频、必要元数据、Pass2 动态特征、Anchor QP/BPP/LPIPS → 一条完整编码动作。固定 Anchor preset 不重复成为逐样本输入，应记入上游数据版本。

253 维特征仍序列化为结构化文本；`feature_schema` 只是版本标识，不会自动加载字段说明或外部知识。字段表、单位、排序、归一化统计必须由上游固定并随数据版本发布。示例只有 16 维模拟特征。

候选配置和候选 Pass3 指标只用于上游选择唯一 GT，不能泄漏进 prompt。同一输入不能无条件配上多个冲突的编码目标；多 RD 点训练需要显式目标条件和对应的协议升级。

## 3. Qwen3.5 特有处理

Qwen3.5 是原生 VLM，并使用混合线性/全注意力。旧 Qwen3-VL 模型类和环境不能直接复用，因此新环境固定 Transformers 5.9、ms-swift 4.5.3，并提供 FLA/Causal Conv1d 依赖。

任务不要求思考过程，训练和推理统一 `enable_thinking=false`。训练显式添加非思考前缀，并使用 `loss_scale=ignore_empty_think` 避免把模板插入的空思考段当作业务 GT。业务监督仍为 assistant JSON 和有效响应结束 token；必须以真实 token audit 结果确认，不沿用旧 2B 的 770-token 统计。

`target_modules=all-linear` 由当前模型实现解析。Qwen3.5 的 Gated DeltaNet 投影不能仅凭旧模型 q/k/v 名称假设已覆盖；训练产物要记录实际可训练参数和 adapter keys。

## 4. 损失不变，运行方式升级

SFT 使用下一 token 交叉熵。system/user/视觉输入和 padding 被 mask；QP 不单独用 MSE，开关不单独用 BCE，BPP/LPIPS 不是训练损失。

DDP 同步可训练参数梯度；完整基座冻结。全局 batch = ranks × 每卡 micro-batch × 累积次数。formal 默认为 2×1×4=8，smoke 可用更小累积完成链路验收。模型能生成合法 JSON 仅是协议正确，不代表参数带来更低 BD-Rate。

## 5. 可复现环境与验收证据

环境名为 `kvc_qwen35_sft`。运行时使用 PyPI 固定发布版 ms-swift，不导入相邻用于参考的开发版源码树。完整 Python 锁、conda 二进制清单、宿主机驱动/工具链和 Qwen3.5 GPU 报告共同构成可复现证据。

容器固定用户态环境，但不能固定宿主 NVIDIA 驱动。4090 和 H800 必须各做一次扩展加载、真实媒体解码、DDP 前反向和保存/回载验收；复制 Triton 缓存不等于完成迁移。

详细操作见 [ENVIRONMENT.md](docs/ENVIRONMENT.md)。本目录不再包含旧 2B 的文档与实测报告，验收只引用当前 Qwen3.5 证据。

## 6. 正式业务实验的前提

1. 真实 Pass2 字段及提取、归一化版本固定。
2. 动作与实际编码器 API 一一对应，合法值和跨字段约束明确。
3. GT 有候选实编码指标和可追溯选择规则。
4. 按源视频分组切分，不依靠逐行随机划分。
5. 对未见视频评估输出合法率、每字段效果与实际 RD 曲线。
6. 多点 BD-Rate 实验独立于 smoke/tiny-overfit；训练环当前不包含 RL。
