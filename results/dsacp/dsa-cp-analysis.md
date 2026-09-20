# DSA-CP 机制分析（vllm-ascend）

基于 `/home/yjc/project/dsacp/worktree/vllm-ascend` 代码走读的结论。核心实现：`vllm_ascend/attention/context_parallel/dsa_cp.py`。

## 关键结论（vllm-ascend 的 DSA-CP 设计，原文记录）

1. 目标：优化长上下文 prefill 的 TTFT。
2. 收益来源：主要是 indexer 打分 ÷N（indexer 权重是 ReplicatedLinear，纯 TP 下完全无法并行，O(S²) 随上下文增长）；core attention 相对纯 TP 是平移（head 切换 query 切，总量不变）；附带激活显存 ÷N。
3. 全量复制的内容：Q 投影（全 head）、wkv、compressor、indexer 权重；KV cache、indexer cache、compressor cache。例外：o_proj 仍是 TP 切分（所以 attention 输出后要有那次 all_to_all 换回 head 分片布局）；MoE 专家走 EP 切分，也不在此列。
4. "每卡只处理自己那部分序列"——只对 Q 侧成立：Q 投影、indexer 打分、core attention、MoE 都只过本地 S/N 个 token；但 K/V 侧是全序列冗余计算——每层 attention 先 all-gather 拼出全序列 hidden，每卡都算全序列的 KV/compressor/indexer-K 并写入本地全量 cache。这是为"attention 内部零通信 + indexer top-k 本地可选"付的冗余税。

一句话版：DSA-CP = 复用 TP 组、以 KV 全量复制 + KV 投影冗余为代价，把长上下文 prefill 的 query 侧计算（尤其是纯 TP 无法并行的 indexer 打分）按 token 维 ÷N，从而降低 TTFT 的过渡方案；o_proj 和 MoE 维持原有 TP/EP 切分不变。

## 一句话定义

DSA-CP = 复用 TP 组、以 KV 全量复制 + KV 投影冗余为代价，把长上下文 prefill 的 query 侧计算（尤其是纯 TP 无法并行的 indexer 打分）按 token 维 ÷N，从而降低 TTFT 的过渡方案。o_proj 和 MoE 维持原有 TP/EP 切分不变。

官方定位为 legacy：PCP 稳定后废弃（`vllm_ascend/ascend_config.py:601`）。开关：`--additional-config '{"enable_dsa_cp": true}'`，需配合 `use_sequence_parallel_moe`，仅用于带 indexer 的 DSA 稀疏注意力模型（DeepSeek-V4 / GLM-5.x）。

## 切分方式

- 切的是**整个 batch flatten 后的 token 流**，按 token 数均分成连续段（`tokens_per_rank = ceil(num_input_tokens / tp_size)`，rank r 拿 `[r*tpr, (r+1)*tpr)`），会切断单个请求。见 `dsa_cp.py:1078-1084`（`_local_token_range`）及 `dsa_cp.py:1086` docstring 的完整算例。
- 跨 rank 边界的因果性靠 `local_seq_lens = seq_lens - offset` 保证（offset = 落在后续 rank 上的 token 数，`dsa_cp.py:1153-1166`）。

## 每层的数据流（`_forward`，dsa_cp.py:1715 起）

1. SP 状态下各 rank 持本地 token 分片进入 attention；
2. `tensor_model_parallel_all_gather(hidden_states_local, dim=0)` 拼出全序列 hidden（dsa_cp.py:1737）；
3. **KV 侧全序列冗余**：每卡对全序列做 wkv 投影（+ compressor/indexer），按全局 slot_mapping 写本地 cache（dsa_cp.py:1783-1796）——每卡持有逐字节一致的全量 KV 副本；
4. **Q 侧只算本地**：Q 投影只过本地分片（dsa_cp.py:1753），且用全部 head（非 TP head 分片，dsa_cp.py:1770）；
5. attention：本地 query × 本地全量 KV 副本，内部零通信（dsa_cp.py:1877-1940）；
6. 输出 `[本地token, 全head]` 经 all_to_all 换回 `[全token, 本地head分片]` 的 TP 布局（`_restore_tp_head_layout`，dsa_cp.py:1942-1972），再走标准 TP o_proj，SP 下最后 `sp_reduce_scatter`（dsa_cp.py:1704）。prefill 可选 `enable_dsa_cp_full_o_proj`：临时 all-gather 全量 o_proj 权重、跳过 all_to_all。
7. decode 同构，"序列切分"退化为按请求分 batch。

## 复制 vs 切分的清单

| 内容 | 布局 |
|---|---|
| Q 投影（wq_a/wq_b）、wkv、compressor、indexer 权重 | 全量复制（indexer 本来就是 `ReplicatedLinear`，见 `models/deepseek_v4/indexer.py:295,309`） |
| KV cache / indexer cache / compressor cache | 全量复制（N 份一致副本） |
| o_proj | **TP 切分**（persistent 唯一真相是 TP 分片；full-weight 只是临时通信 buffer） |
| MoE 专家 | EP 切分 |

## 收益分析：为什么在 indexer，不在 core attention

每 token 乘加量（DeepSeek-V3 维度，S=上下文长度）：

| 部分 | 计算量 | 随 S 增长 | DSA-CP 下 |
|---|---|---|---|
| Q 投影 | ~49M | 否 | 按 token 切 ÷N |
| KV latent 投影 | ~4M（576 维输出） | 否 | 全序列冗余 ✗ |
| indexer 打分 | ~S×8k（S=100k 时 ~0.8G） | **O(S²)** | 本地 query × 全量 indexer cache，÷N ✓ |
| core attention（top-k 稀疏后） | ~84M | 否 | ÷N（但相对纯 TP 是平移：S×H/N → S/N×H，总量相同） |
| o_proj | ~117M | 否 | 维持 TP 切分 |

- **indexer 是 TP 的盲区**：top-k 需要把全部 indexer head 的分数聚合到每个 query，head 切分会引入跨 rank 的 S×S 分数归约，不可行 → 纯 TP 下 indexer 权重复制、每卡对全序列打分，O(S²) 开销完全无法并行。切 query 维是唯一让它 ÷N 的办法。
- core attention 被 top-k 稀疏后不随 S 增长，且纯 TP 的 head 切分已分摊，换成 query 切没有额外 FLOPs 收益。

## 显存估算（复制式 KV 的代价）

MLA latent KV：每 token 每层 512（kv_lora_rank）+ 64（rope）= 576 元素 = 1152 B bf16。注意**没有 K/V ×2、没有 head 维**——MLA 存共享 latent，K/V 计算时现算（`kv.view(-1, 1, nope+rope)`，dsa_cp.py:1786）。

- 61 层 → ~69 KB/token → **100k token ≈ 7 GB/条**（fp8 减半），加 indexer/compressor cache 总量级 7~8 GB/条
- 910B（64 GB HBM）扣掉 W8A8 671B 权重分摊（TP16 约 40+ GB/卡），KV 只剩 ~20 GB → 同时服务 2~3 条 100k 请求
- TP8 组存 8 份相同副本，集群视角显存利用率 1/8；DSA 稀疏只省 attention 计算，不省 KV 存储

## 与 DCP / PCP 的分工和演进

- **DSA-CP**：存储复制、计算切分，attention 零 KV 通信；省 TTFT，不省显存。
- **DCP（Decode Context Parallel）**：KV cache 按序列维交错切分（interleave 布局，`block_table.py`），每卡存 1/N，attention 靠 Q-gather + LSE merge 补齐；省显存。设计文档：`docs/source/developer_guide/Design_Documents/context_parallel.md`。
- **SFA DCP（GLM-5.2）**展示稀疏注意力的折中终态：大 KV cache 切分、小 indexer cache 复制（需全序列视图保证 top-k 一致）、prefill 只 all-gather 稀疏引用到的 KV block。
- 新 MRV2 DSA-PCP（`AscendDSAPCPImpl`，dsa_cp.py:2422 起）目前**仍是复制式 cache**（all-gather hidden → 更新 replicated cache → 本地 attention）；配置层面官方建议迁往 `--tensor-parallel-size 1 --prefill-context-parallel-size N`。

长期方向是"KV 切分 + 通信"（PCP/DCP 框架），DSA 因 compressor/indexer 的流式状态依赖和 top-k 不规则读取，至今两条路径都没走出复制式。

## 关键取舍总结

存储和 O(S) 计算全面冗余（全量 KV 副本、全序列 KV 投影、全 head Q 权重），换取：

1. attention/indexer 内部零通信（top-k 的不规则读取变本地操作）；
2. 随 S 增长的大头计算（indexer 打分 O(S²)）÷N；
3. compressor/indexer 带状态压缩路径无需改造（流式 state 无法按 token 切片独立计算），decode/prefill/chunked prefill 共用一条路径。
