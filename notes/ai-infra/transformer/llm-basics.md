# LLM 架构基础

> 原 known-concepts 中的 AI Infra / LLM 架构部分。

- **Attention 变体谱系** — MHA（K/V 每头一份）→ GQA（分组共享）→ MQA（K/V 全局一份，Q 侧仍多头，多样性在 W_Q）→ MLA（不共享，压成 latent 重建，cache 接近 MQA、质量接近 MHA）。KV cache 大小正比于 K/V head 数。
- **MLA** — K/V 联合压成共享 latent（512 维 + decoupled RoPE key），每 head 各自上投影重建；推理用权重吸收（MQA mode），不真的展开 K/V。
- **DSA** — lightning indexer（小头数/FP8/ReLU，O(L²) 但常数极小）打分 → top-k（2048）→ 主 MLA 只算选中条目（O(Lk)）。是 gather 不是 mask；**省读取不省存储**（全量 KV 必须保留）。indexer 有独立的小 k^I cache。GLM-5.2 IndexShare：每 4 层共享一个 indexer。
- **DSA vs MoE** — DSA 在 attention 层（序列轴稀疏，候选集动态 L 个 token）；MoE 在 FFN 层（参数轴稀疏，候选集固定 256 专家 top-8 + 1 共享）。骨架同为 score+top-k。
- **decode 是 memory-bound** — attention 计算 <1ms，KV 搬运占 ~99% 时间。成本账（1M 上下文，MLA 加持）：~90GB 存储、DSA 后每步读 ~180MB。
- **Pre-Norm + RMSNorm** — Post-LN 深网络梯度衰减/需 warmup/易发散；Pre-Norm 保残差恒等通路；RMSNorm 砍均值居中省计算。
- **预训练 vs 后训练** — 分界不在算法而在数据（无偏好知识注入 vs 带偏好行为塑造）与起点（从随机/旧 checkpoint vs 从基座）。基座 = 预训练完成、后训练前的 checkpoint（V3/R1 同基座）。后训练改全部权重；注入新知识效率低（数据量差 3-4 个数量级 + 灾难性遗忘），RL 基本只塑形。
- **上下文上限** — RoPE 无表（现场算旋转角），`max_position_embeddings` 是训练担保范围非物理上限；YaRN 频率缩放 + 续训扩展。生效上限 = min(训练范围, max_model_len, 商业限制)，物理约束是 KV cache 显存。
