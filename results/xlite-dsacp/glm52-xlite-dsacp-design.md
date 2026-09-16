# GLM-5.2 xlite DSACP 设计

## 一、概要设计

### 1. 目标与范围

降低 GLM-5.2 长上下文 prefill 的 TTFT，重点消除 TP 组内各 rank 重复执行的 indexer 打分和 top-k 计算。

本方案实现 **Indexer-only Context Parallel**：按 query token 切分 indexer，汇集完整 top-k 后继续执行现有 head TP 的主 MLA。本文简称 DSACP；它不包含主 attention 的序列并行或 KV cache 分片。

| 对象 | 本方案布局 |
|---|---|
| Indexer 权重、K cache | 每个 TP rank 完整持有 |
| Indexer K 投影、cache 更新 | 每卡处理完整输入，保持 cache 一致 |
| Indexer Q 投影、打分、top-k | 每卡仅处理分配到的 query |
| Top-k 输出 | TP all-gather 后，每卡持有全部 query 的 top-k |
| 主 MLA、主 KV cache、W_O、FFN/MoE | 沿用当前布局和执行流程 |

初版对长上下文 prefill 启用；decode-only、短输入、TP=1 走原路径。混合批次初版回退，单独验证后再扩展。

### 2. 执行流程

```text
每卡完整输入 → 现有 MLA 预处理
                      ↓
           完整 indexer K 投影与 cache 更新
                      ↓
           本地 query 的 Q 投影与预处理
                      ↓
           本地 query × 原始候选 K 范围
                      ↓
               本地 score / top-k
                      ↓
           TP all-gather top-k indices
                      ↓
       完整 query 顺序的 top-k → 原有 head TP MLA
```

GLM-5.2 的 full indexer 层执行计算和通信；shared indexer 层直接复用最近一次完整 top-k，不增加通信。

### 3. 收益与代价

设本批 query 数为 Q、历史长度为 S、TP 大小为 P、top-k 为 K：理想均衡时，单卡打分工作量由约 O(QS) 降至 O(QS/P)。全组消除了重复计算，但端到端 TTFT 不会等比例缩短。

每卡本地有效 top-k 约占 `4 × Q/P × K` 字节（INT32），汇集后的有效结果占 `4 × Q × K` 字节；实际通信还有 padding 和通信算法开销。收益条件是：**节省的 indexer 耗时超过新增通信、分片和同步耗时**。K 更新仍有重复计算，初版保留它以减少改动。

## 二、代码级详细设计

### 1. 当前代码基础

以下路径均相对 `GVirt/xlite/`。

| 位置 | 当前行为及改动职责 |
|---|---|
| `csrc/model.cpp::ForwardAttnIndexer` | 目前对全部 hiddenState 行计算 indexer；增加 CP 分支和结果汇集 |
| `csrc/model.cpp::ForwardAttnMLAV2` | 主 MLA 使用 `nHeads / defTpSize`；保持主计算，消费完整 top-k |
| `csrc/runtime.h/.cpp` | 增加分片计划、局部 metadata、通信缓冲及生命周期管理 |
| `csrc/model.h::XModelConfig`、`csrc/_C.cpp` | 增加配置和 Python 绑定；算子接口变更同步绑定 |
| `csrc/kernels/indexer_prepare.h`、`csrc/op.h/.cpp` | 分离完整 K/cache 处理与局部 Q/weight 处理 |
| `csrc/kernels/indexer_topk.h` | 解耦局部 query 布局与原请求的 KV 范围/位置 |
| `tests/models/deepseek_v3.py` | `ModelArgs`、`init_xlite_model` 传入配置；权重加载和 KV 分配保持现状 |

### 2. 配置与启用决策

建议增加 `enableIndexerCp` 和 `indexerCpMinSeqLen`（Python 使用 snake_case）；名称均为拟新增字段。默认关闭，门槛由性能测试确定。

在 runtime 准备 batch 时统一计算是否启用：DSA 模型、配置开启、TP>1、非 decode-only、非混合批次、存在足够长上下文和足够 query 工作量。仅历史长但 query 很少时，不应自动启用。该决策及通信调用次序在整个 TP 组必须一致。

沿用原有短序列跳过 top-k 的逻辑。full/shared 层定义由 `indexFullMask` 控制；每次 forward 重置复用状态，避免跨 batch 使用旧索引。

### 3. Query 分片和 metadata

初版按打包后的全局 query 行连续分片：每卡通信行数 `L = ceil(Q/P)`，rank r 负责 `[rL, min((r+1)L, Q))`。仅尾部 padding；算子只处理有效行。

连续分片便于张量取 view，rank 顺序拼接即可恢复全局行顺序。按每个原请求与该区间的交集生成本地片段，保存：

| 字段（拟新增） | 含义 |
|---|---|
| `localQueryStartLoc`、`localQueryLens` | 本地紧凑 query tensor 中的偏移和有效长度 |
| `requestId` | 原请求编号，用于选择原 block table |
| `queryOffsetInRequest` | 本地片段在该请求本次 query 中的起点 |
| `originalQueryLens`、`originalCachedLens` | 原请求本次 query 长度及历史 cache 长度 |
| `localPosition` | 原 position 的对应切片，保持绝对位置 |

**原始主 attention metadata 不修改。** 局部行布局、候选 KV 范围、causal 位置分别表达；不能简单把原 `queryLens` 改短后继续用 `cachedLen + queryLen` 推导候选范围。

当前 `indexer_topk.h::Run` 用 `cachedLen + queryLen` 决定扫描长度。CP 接口需显式传入原请求范围和 query 偏移，保证每一行与非 CP 路径选取同一候选集合。先核对现有 kernel 的逐 query mask 语义，再实施接口变更；若发现基线精度问题，应独立定位，避免 CP 修改暗中改变选择规则。

### 4. 拆分 indexer 预处理

当前 `ForwardAttnIndexer` 的 `kw` 同时包含 K 投影和 head weights；`XliteOpIndexerPrepare` 又融合 K norm/RoPE/cache 写入、Q RoPE 和 weights 缩放，不能只把 Q tensor 换成本地形状。

建议保留现有非 CP 接口，为 CP 拆出两个执行阶段：

1. **完整 K 阶段**：对完整 hiddenState 生成 `kw`，为全部真实 token 执行 K norm、RoPE、cache 写入。
2. **局部 Q 阶段**：对本地 `attnNormQc` 执行 `indexQB`；按原始 position 做 Q RoPE，对本地 `kw` 的 weights 部分执行原有缩放规则。

两阶段确保 weights 缩放恰好一次；K cache 更新先于本地打分。非 CP 路径保留现有融合调用。量化 Q 投影继续复用 `ForwardLinear`，无需改变 indexer 权重布局。

### 5. 本地 top-k 与 TP 通信

`XliteOpIndexerTopK` 使用局部 Q/weights 和新的 query metadata，读取完整 indexer cache；输出索引继续表示原请求内 KV 位置。

新增本地发送缓冲 `[L, K]` 和汇集缓冲 `[P×L, K]`，均为 INT32；有效计算只有本地真实行，其余行初始化为占位值。调用现有 `XliteOpAllGather(..., TP, ...)` 后，取前 Q 行形成完整结果。

首版将有效结果复制到现有 `_dsaTopkBuffer`，再设置 `dsaPerLayerTopk`，保持主 MLA 和 shared 层消费方式不变。后续可用统一缓冲减少复制。

通信缓冲优先使用 runtime 通信池，核对现有 all-gather 的 INT32、IPC/HCCL 路径和 stream 依赖。零有效 query 的 rank 跳过计算，但仍以等长缓冲参与通信。dummy/profiling runtime 和图模式使用稳定的容量及分支策略，不能在回放时临时改变 collective 次序。

### 6. 主 attention 与跨层复用

`ForwardAttnMLAV2` 仍使用完整 query metadata、本地 heads、原有 KV cache 和输出归约。只有 full indexer 层更新完整 `_dsaTopkBuffer`；shared 层直接读取它。短序列未生成 top-k 时，继续使用现有空指针语义。

本方案不修改 `defTpSize`，不调整 `mlaQB/mlaWUKT/mlaWUV/attnOut` 权重分片，也不改变 `ForwardLayersCommOptimize` 的 token/归约接口。

## 三、验证与交付

| 验证层级 | 关键用例与判断 |
|---|---|
| 分片/metadata | 多请求、跨请求边界、Q 非 P 整除、Q<P、非零 cached length；每个真实 query 恰好归属一次 |
| Kernel 精度 | 非 CP 与 CP 对齐 indexer score、有效 top-k；覆盖 top-k 和 KV tile 边界、chunked prefill、causal 边界；并列分数按有效集合及最终输出检查 |
| 集成精度 | GLM-5.2 full/shared 层链、prefill→decode、prefix cache；比较主 attention 输出和最终 logits |
| 通信/运行时 | 所有 TP rank 结果一致；padding 不进入主计算；覆盖零行、通信池、实际启用的图/多任务模式 |
| 性能 | 不同 TP、Q、历史长度；分别记录 K 准备、局部 Q、score/top-k、all-gather、主 MLA、端到端 TTFT |

扩展 `tests/kernels/indexer_prepare.py`、`indexer_topk.py`，增加 TP 集成用例，通过 `tests/generate.py` 的 GLM 路径验证端到端结果。

验收以精度一致、长上下文 TTFT 改善、关闭开关及回退路径无回归为准。连续分片的负载均衡由实测确认；若瓶颈在 rank 间不均衡，再增加按工作量分片及行恢复，不作为首版前置条件。

设计依据：当前本地 xlite 代码静态检查；尚未实现或完成 NPU 性能验证。
