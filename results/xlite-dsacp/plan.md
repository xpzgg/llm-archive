# xlite 支持 DSA_CP 计划

> 目标仓库：`~/project/GVirt/xlite`（master，HEAD `857a316`）
> 参考实现：vllm-ascend legacy DSA_CP（`vllm_ascend/attention/context_parallel/dsa_cp.py`、`sfa_cp.py`）
> 状态：框架搭建阶段（不要求精度验证，明天与相关人员对齐设计后再迭代）

## 1. 背景与目标

DSA_CP：在 TP 域内对 token 序列维切分，使 qkv proj、LightningIndexer、稀疏 attention
从张量并行变为序列并行。每卡处理 `1/tp` 的 token、使用全部 head；KV cache 全量复制
（不切分）；输出端通过 all-to-all（或全权重 o_proj + all-gather）恢复 TP 布局。

xlite 现状（探索结论）：

- **CP 完全未支持**：`doc/feature_matrix.md:17` 标注 ❌，全库无 cp_size/cp_rank 概念。
- **DSA（DeepSeek V3.2）已完整支持**：`XMODEL_ATTN_DSA`，组图在
  `ForwardAttnMLAV2`（`csrc/model.cpp:449-560`），含 indexer（full/shared 两级）、
  topk 稀疏、decode 长序列 gather+dense 优化。V4（CxA）进行中，indexer 还是 stub。
- **TP/DP/EP 通信齐备**：HCCL + 单机 xccl；`XliteOpAllGather/ReduceScatter/
  AllReduceSum/AlltoAllV`（`csrc/op.cpp`），通信域 `{TP, EP, DP}`，无 CP 域。
- **kernel 语义对 CP 友好**：attention kernel 用 per-request packed 布局
  （`queryStartLoc/lens/cachedLens`），causal 边界由 `cachedLens + 局部偏移` 决定。
  把 query 按 token 维切开、令 `cachedLens = 全局偏移`，kernel 本身无需改动。
- **metadata v2 路径**（`XRuntime::PrepareAttn`，`csrc/runtime.cpp:776-790`）：
  device tensor 由调用方（vllm-ascend / 测试脚本）预构建，xlite 只做校验+别名，
  是 CP 切片的天然注入点。

## 2. 设计决策（框架版）

与 vllm-ascend legacy DSA_CP 语义对齐，选**最小侵入方案**：

1. **CP 域复用 TP 域**：`cp_size == tp_size`，不新增通信域、不新增 HCCL comm。
2. **层间 hidden_states 保持复制布局**（naive 路径）：切分只发生在 attention 边界。
   - `wqkv_a` 在全量 token 上算（各 rank 冗余）→ 每 rank 自然写出**全量 KV cache**，
     副本一致性自动成立，避开"KV 写前 AllGather"这个最难的点。
   - 切出本 rank 的连续 token 段（`local_start = rank * ceil(num_tokens/tp)`，
     token 数 pad 到 tp 整数倍）→ `wq_b`/indexer/稀疏 attention 只算本地段、全头。
3. **输出恢复**：attention 输出（本地 token × 全头）经 **TP 域 all-to-all**
   转置为（全量 token × 本地 head），接原有 TP 切分的 `o_proj` + `AllReduceSum`，
   后续层（MoE/FFN）零改动。
4. **只支持 attnType = DSA（V3.2 路径）**：CxA（V4）indexer 尚未落地，暂不支持；
   MHA/MLA 非稀疏路径不在范围内。
5. **暂不做**：首尾负载均衡（zigzag/DualChunkSwap）、与 FlashComm（RS+AG token 切片）
   框架融合、prefill 全权重 o_proj 变体、decode gather+dense 路径的 CP 适配
   （先走通用 `XliteOpMLAV2`/flash 路径，dense 阈值分支在 CP 下禁用）。
   这些在计划末尾列为后续迭代项。

## 3. 改动清单

### 3.1 配置与绑定

| 文件 | 改动 |
|---|---|
| `csrc/model.h` | `XModelConfig` 新增 `bool dsaCp = false;`（CP 域即 TP 域，无需 cpSize 字段；放 `defTpSize` 附近，约 model.h:94-98） |
| `csrc/_C.cpp` | `ModelConfig` pybind 增加 `dsa_cp` 字段读写（ModelConfig 绑定区，约 _C.cpp:2393-2670） |
| `csrc/model.cpp` | `CheckForwardParam` 加约束：dsaCp 要求 `attnType==XMODEL_ATTN_DSA`、`tpSize>1`，暂禁 CxA/MHA/MLA |

### 3.2 metadata（Python 侧 helper，C++ v2 路径尽量不动）

| 文件 | 改动 |
|---|---|
| `tests/models/xlite_utils.py` | 新增 `prepare_xlite_attnmeta_dsacp_v2(...)`：输入全局 packed metadata + rank/tp_size，产出本 rank 切片版 `AttnMetaV2`——`lens` 为本 rank 各请求 token 数（clamp 求交集）、`cachedLens` 保持全局偏移、`queryStartLoc` 重算局部前缀和、`slotMapping`/`position` 取全局（KV 写全量 cache，slot 用全局值） |
| `csrc/runtime.cpp` | `PrepareAttn` v2 路径复核 host 派生状态（`_decodeStep`/`_hostLens`，runtime.cpp:687-691）在 CP 语义下的正确性；框架阶段加 dsaCp 分支注释/断言即可 |

注意：CP 下"全局 token 数"（KV 写、rope position）与"局部 token 数"（attention 计算）
需要两套视图，这是 metadata 设计的核心，需明天与设计方对齐确认。

### 3.3 组图核心（csrc/model.cpp）

- `ForwardAttnMLACommonV2`（model.cpp:410）：dsaCp 分支——`nLocalHeads = nHeads`
  （全头，不再 `/defTpSize`）；`wqkv_a` 保持全量 token 计算（cache 写全量）；
  `wq_b` matmul 改为全权重、只作用在本 rank token 切片上（切片在 metadata 构建后，
  用 `local_start/local_len` 对 q 做 narrow）。
- `ForwardAttnIndexer`（model.cpp:362-408）：dsaCp 分支——indexer q/k 均为 token 局部
  计算，天然兼容序列切分；`XliteOpIndexerPrepare` 目前 `tpSize!=1` 抛异常
  （`csrc/op.cpp:1199-1202`），CP 下语义即"整头跑"，需放开该限制或显式传 tpSize=1；
  `index_q_b` 本来就是全权重（pyi:515 已要求）。shared-indexer 跨层 topk 复用
  （`dsaPerLayerTopk`）要求层间 token 切片对齐——固定切分策略天然满足。
- `ForwardAttnMLAV2`（model.cpp:449-560）：dsaCp 分支——
  1. attention 只算本 rank token 段（局部 `queryStartLoc/lens` + 全局 `cachedLens`）；
  2. 禁用 decode dense gather 分支（`XLITE_MLA_DENSE_THRESHOLD` 路径）；
  3. 输出端新增 **TP 域 all-to-all**（token 维 ↔ head 维转置），再接原 TP `o_proj`
     + `XliteOpAllReduceSum`（替换掉 CP 下无意义的 ReduceScatter）。
- o_proj 权重：保持 TP row-parallel 切分不变（对应 vllm-ascend decode 路径），
  权重加载侧无改动。

### 3.4 通信原语

- 复用 `XliteOpAlltoAllV`（`csrc/op.cpp:472`），目前是 HCCL-only、仅用于 EP；
  框架阶段直接在 TP 域调用即可（等长 all-to-all 用 AlltoAllV 等长特化或新增
  `XliteOpAllToAll` 包装）。
- 性能敏感时后续补 xccl 快路径（对照 `csrc/kernels/all_gather.cpp` 等自研 kernel）。

### 3.5 DummyRun / 内存评估

- `XModel::DummyRun`（model.cpp:2109）与 tensor pool 估算：CP 下中间张量 shape
  变化（q 为 `[m/tp, nHeads*headDim]`），dsaCp 分支的 shape 推导必须与 forward 一致，
  否则 pool size 估错。框架阶段先保证 shape 推导自洽。

### 3.6 测试（框架占位，不验证）

- `tests/models/deepseek_v3.py`：`ModelArgs` 加 `dsa_cp` 开关；`init_xlite_model`
  传 `dsa_cp=True`；权重加载对 `wq_b` 不切分（CP 分支）——先留 TODO/分支框架。
- 新增 `tests/funcs/` 或 `tests/models/` 下 CP 切片 metadata helper 的纯 CPU 单测
  （切片/clamp/prefix-sum 逻辑可脱离 NPU 验证）。
- e2e 配置：`tests/run.sh` 参照 deepseek_v32 用例（:366）加 CP 变体（占位）。

## 4. 实施步骤

1. ✅ 仓库探索（本文件第 1 节结论）
2. 配置与绑定：`XModelConfig.dsaCp` + pybind + `CheckForwardParam` 约束
3. 通信：TP 域 all-to-all 包装
4. metadata：`prepare_xlite_attnmeta_dsacp_v2` helper + CPU 单测
5. 组图：`ForwardAttnMLACommonV2`/`ForwardAttnIndexer`/`ForwardAttnMLAV2` 的 dsaCp 分支
6. DummyRun shape 推导
7. 测试模型侧开关与权重加载分支
8. 明天对齐后：补精度对拍（对照 PyTorch 参考）、e2e、性能

## 5. 风险与开放问题（明天对齐用）

1. **两套 token 视图**：全局（KV 写/rope）vs 局部（attention 计算）的 metadata 语义，
   需要与 vllm-ascend 侧 AttnMetaV2 的生产方确认接口。
2. **`wqkv_a` 冗余计算**：复制布局下每 rank 对全量 token 算 qkv_a 投影，比
   vllm-ascend 的 SP 式（hidden 层间已切分 + KV AllGather）多算 tp 倍 qkv_a。
   框架阶段为正确性让步；后续可与 FlashComm token 切片区间融合消除冗余。
3. **负载均衡**：连续等长切分在 causal 下 rank 间算力不均（短 prefix 的 rank 算得少），
   vllm-ascend 新版用 DualChunkSwap 首尾配对；xlite 侧后续需在 metadata helper 里支持
   任意"token→rank"映射（切片逻辑参数化即可预留）。
4. **decode dense gather 路径**（长序列 `gather_sparse_kv_cache + mla_v2 dense`）在 CP 下
   的语义未推导，框架阶段禁用。
5. **indexer_prepare 的 tpSize 限制**（op.cpp:1199）放开前需确认 kernel 内部是否
   有隐含的整头假设。
6. CxA（V4）路径待 indexer 落地后再扩展。

## 6. 关键文件索引

| 位置 | 内容 |
|---|---|
| `csrc/model.h:23-117` | `XModelConfig`（并行配置在 :94-98） |
| `csrc/model.cpp:1897` | `XModel::Forward` 主链 |
| `csrc/model.cpp:410-560` | DSA 一层组图（Common/Indexer/MLAV2） |
| `csrc/model.cpp:1649-1712` | FlashComm 式 RS+AG 层间布局（后续融合参考） |
| `csrc/runtime.cpp:612-796` | `PrepareAttn`（v2 路径 :776-790） |
| `csrc/op.cpp:472` | `XliteOpAlltoAllV`（HCCL） |
| `csrc/_C.cpp:2393-2670` | pybind 绑定 |
| `tests/models/xlite_utils.py:13` | `prepare_xlite_attnmeta_v2`（CP 版参照物） |
| `tests/models/deepseek_v3.py:1703-1773` | 测试模型初始化与 DSA 配置 |
| vllm-ascend `dsa_cp.py:1342/1373/1754` | 参考实现：切片/输出 gather/all-to-all |

## 7. 实施记录（2026-09-10，框架 skeleton 落地）

工作区改动（未 commit，供评审）：

| 文件 | 改动 |
|---|---|
| `csrc/model.h` | `XModelConfig` 新增 `bool dsaCp = false;`（parallel config 区，约 :100）；`ConfigRtCommOptimize` 在 dsaCp 下强制关闭 `enableCommOptimize`（CP 要求层间 hidden 复制布局，与 RS+AG token 切片不兼容） |
| `csrc/_C.cpp` | ModelConfig pybind 加 `dsa_cp` 读写字段（moe_tp_size 之后，约 :2441） |
| `xlite/_C.pyi` | 同步补 `dsa_cp` 字段与 docstring（配合仓库"pyi 记录全部字段"的约定） |
| `csrc/op.h` / `csrc/op.cpp` | 新增 `XliteOpAllToAll`（HCCL 等长 all-to-all 薄包装，支持 TP/DP/EP 域，含 numel 整除校验与 dummy-runtime 短路）；未复用 AlltoAllV——等长场景无需 host 侧 counts/displs |
| `csrc/model.cpp` | 新增 `DsaCpTokenWindow` helper（:362 附近，等长连续切分 + ceil pad）；`CheckForwardParam` 加 dsaCp 约束（attnType==DSA 且 defTpSize>1，约 :2132）；`ForwardAttnMLACommonV2`/`ForwardAttnIndexer`/`ForwardAttnMLAV2` 加 dsaCp 分支（见下）；`Forward`/`ForwardWithInputsEmbeds`/`ForwardAndGetLogits` 三处在 dsaCp 下跳过 `input.View(batchedTokens)` 截断（CP 下 attnMeta 是局部视图而 input 是全量复制流） |
| `tests/models/xlite_utils.py` | 新增 `prepare_xlite_attnmeta_dsacp_v2(global_lens, global_cached_lens, rank, tp_size)`：**纯 Python 实现**（不依赖 numpy/torch，本机无这两个包，保证 CPU 自检可跑）；numpy/torch 的 import 改为 try/except 容错，`prepare_xlite_attnmeta_v2` 的 `torch.Tensor` 注解改为字符串形式（否则 import 失败时模块加载即崩）；文件尾 `if __name__ == "__main__":` 自检块（decode/均匀 prefill/混合长度/不整除/空尾 rank 共 8 组用例），`python3 xlite_utils.py` 已通过 |
| `tests/models/deepseek_v3.py` | `ModelArgs` 加 `dsa_cp: bool = False`；`init_xlite_model` 透传 `config.dsa_cp`；`load_weights` 中 `wq_b`（非 indexer）与 `wkv_b` 加 `if args.dsa_cp:` 不切分全量拷贝分支；`mla_wuv/mla_wuk_t` reshape 的 `n_local_heads` 在 dsaCp 下用全头数 |

dsaCp 分支数据流（每层 attention）：

1. `hiddenState` 全量复制 [M, hidden] → `wqkv_a`/`MlaPrepare` 全量计算、写全量 KV cache
   （slot_mapping/position 全局，metadata 校验的 `>=` 语义天然兼容）；
2. `attnNormQc` narrow 出本 rank 窗口 [localStart, localStart+localTokens) → `wq_b`（全权重）
   → `RopeComplex`（position/freqs 同步切窗）→ qAbsorb/qPe，全部只含本地 token、全 head；
3. indexer：kw 投影与 index-k cache 写在全量 token 上（副本一致），q 侧只算本地段，
   topk 用局部 lens/queryStartLoc + 全局偏移 cachedLens；
4. `XliteOpMLAV2`/`XliteOpFlashMLAV2` 只算本地段；decode dense-gather 分支在 CP 下禁用；
5. attnOutput pad 到 tokensPerRank 行（pad 行清零）→ View [tpr, tp, hD] → `Transpose_1_2`
   → TP 域 `XliteOpAllToAll` → [tp*tpr, nHeads/tp*vHeadDim]（全量 token × 本地 head，
   段连续故 token 序即全局序）→ 原 TP 切分 `o_proj` + `AllReduceSum` → memcpy 回 hiddenState
   前 M 行。

DummyRun：CP 分支的中间张量 shape 全部由 `hiddenState.shape[0]` 经 `DsaCpTokenWindow`
推导，dummy runtime 下各 kernel 短路、通信与 memcpy 均有保护，pool 估算与 forward 一致
（`maxBatchedTokens` 本就被 ROUND_UP 到 tp 倍数）。

留下的 TODO / 与设计不符处：

1. **indexer_prepare 的 q/kw 耦合**（`csrc/model.cpp` ForwardAttnIndexer 内 TODO）：kernel
   假设 q 与 kw 同 token 数，CP 下 q 是本地段、kw 是全量，需要 kernel 加独立 q token 数
   参数或把 q 的 norm/rope 拆出 prepare。plan 里担心的 `tpSize!=1` 抛异常实际不触发
   （ForwardAttnIndexer 本就不传 tpSize，默认 1），真正的耦合点在 token 数。
2. **等长 all-to-all 需要 pad**：尾 rank 本地 token 数 < tokensPerRank 时用零行补齐
   （attnOutput Memset）；后续可换 `XliteOpAlltoAllV` 免 pad（需要各 rank 本地长度，
   host 侧可得但需跨 rank 同步或随 metadata 传入）。
3. **localTokens==0 的空 rank**（globalTokens < tpSize）：matmul/attention 收到 m=0 的行为
   未验证，调用侧应保证 token 数 ≥ tp 或先 pad。
4. **运行时 host 一致性校验**：CommonV2 里加了 `rt.batchedTokens == localTokens` 检查
   （dummy runtime 豁免），用于尽早暴露 Python/C++ 两侧切片不一致。
5. **`VerifyAttnMetaV2`（XLITE_DEBUG_ON）在 CP 下会误报**：它按 v1 语义逐 token 校验
   position，与"position/slot_mapping 全局"的 CP 约定冲突，框架阶段未处理。
6. **`_tileSizeOfCachedKV` 仍按 nHeads/tp 估算**（PrepareAttn 内）：CP 下实际用全头，
   tile 估算偏小只会影响 paged/flash 分支选择（正确性无碍，性能待调）。
7. **权重加载**：wq_b/wkv_b 的 dsa_cp 全量分支未覆盖量化 scale/quant_bias 路径，且
   ColumnParallelLinear 的参数分配 shape 还是切分的，需要全 shape 变体才能真正跑通；
   参考模型侧（`deepseek_v3.py:754` 的 `self.n_local_heads`）对拍时同样要全头。
8. **编译未验证**：本机无 CANN/Ascend 环境（无 `/usr/local/Ascend`、无 ascend-toolkit
   环境变量），C++ 改动只做了人工 review，未过编译；明天在有环境的机器上先编一次。
9. runtime.cpp 未改动：v2 路径的 host 派生状态（`_hostLens`/`_decodeStep` 等）在 CP
   语义下即为"本 rank 局部视图"，与局部 lens 一致，无需改动。

验证情况：`python3 tests/models/xlite_utils.py` 自检通过（8 组切片用例全覆盖断言）；
`python3 -m py_compile` 通过（deepseek_v3.py、xlite_utils.py）；C++ 未编译（无环境，见上）。

提交拆分（2026-09-10，master 857a316 之上 7 个 commit，未 push；按"先原语、后组装"重写）：

| hash | 标题 |
|---|---|
| `f2bf15a` | xlite: add dsa_cp model config option（model.h 字段 + _C.cpp pybind + _C.pyi） |
| `5d361e7` | xlite: add XliteOpAllToAll collective op（op.h/op.cpp 等长 all-to-all 原语） |
| `b7e00c1` | xlite: add DsaCpTokenWindow sequence partition helper（model.cpp 仅切分原语，无调用点） |
| `73ac71d` | xlite: add DSA-CP attention metadata slicing helper（xlite_utils.py + CPU 自检） |
| `11e0001` | xlite: validate dsa_cp configuration（CheckForwardParam 约束 + ConfigRtCommOptimize 守卫） |
| `8138321` | xlite: add DSA-CP token-sliced attention forward path（model.cpp 三个 Forward* 分支 + input 截断守护 + DummyRun） |
| `27a2bc2` | xlite: wire dsa_cp into DeepSeek-V3.2 test model（deepseek_v3.py） |

拆分后验证：`git diff`（与重写前备份分支）为空，内容逐行一致；`git log 857a316..HEAD` 恰好
7 个 commit 且顺序如上；工作区干净；自检在最终 commit 状态下重跑通过。
