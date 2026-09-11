# vLLM 学习路线

> 目标：不钻代码，先把 vLLM 的概念地基打牢，能独立讲清 v1 架构各组件的职责边界与交互方式，再回到代码验证。
>
> 触发点：手绘 vLLM v1 架构图（`notes/ai_infra/vllm.excalidraw`）时发现对 KV Cache 的管理分层理解含糊——谁是簿记、谁是数据面。

## 核心主线：三层问题

vLLM 整体可以拆成三层，每层看住一个问题，串起来就通了：

| 层 | 回答的问题 | 对应组件 |
|---|---|---|
| 概念层 | **为什么**：为什么需要 KV Cache，为什么显存管理要用分页 | KV Cache、PagedAttention |
| 簿记层 | **怎么记**：每步推理哪些请求跑、KV 放到哪些 block | Scheduler、KVCacheManager |
| 执行层 | **怎么算**：拿到调度结果后怎么驱动 GPU 算 | Executor、Worker、GPUModelRunner |

## 已确立的关键认知（本路线的锚点）

- **KVCacheManager 只做簿记，不碰数据**：它管理空闲 block 队列、token → 物理 block 的 block table、prefix cache 命中，自始至终只操作 block id。
- **数据面完全在 Worker 本地**：真正的 KV cache tensor 在 Worker 初始化时按显存预算（`gpu_memory_utilization`）一次性静态分配，形状 `[num_blocks, block_size, num_kv_heads, head_size]`，初始内容是未初始化的垃圾值。
- **跨进程只传轻量元数据**：Scheduler 每步产出 `SchedulerOutput`（带分配好的 block id），GPUModelRunner 拿 block id 在本地显存读写。这就是调度和计算能分进程跑的原因。
- **与 MM 的直接类比**（`notes/mm/` 知识可迁移）：PagedAttention 把 KV cache 切固定大小 block = 分页；block table = 页表；动机也一致——动态增长的需求 vs 必须预分配的物理资源。

## 学习顺序与资源（英文）

按依赖顺序，每阶段有明确的"完成标志"再进下一阶段。

### 第 0 层：Attention 基础（按需）

| 资源 | 说明 | 完成标志 |
|---|---|---|
| [The Illustrated Transformer](https://jalammar.github.io/illustrated-transformer/) — Jay Alammar | self-attention 的 Q/K/V 可视化经典 | 能画出 attention 中 Q/K/V 各自的角色与计算流 |

### 第一层：KV Cache + 推理性能模型

| 资源 | 说明 | 完成标志 |
|---|---|---|
| [Transformer Inference Arithmetic](https://kipp.ly/transformer-inference-arithmetic) — kipply | 推理性能分析绕不开的一篇：KV cache 为什么必要、占多少显存、prefill 算力瓶颈 / decode 带宽瓶颈，全从基本原理推导 | 能解释为什么 decode 是 memory-bound，能估算 KV cache 显存占用 |
| Umar Jamil《KV Caching explained》（YouTube） | 动画演示，建立直觉用，读上面那篇之前看 | — |

### 第二层：PagedAttention + 调度

| 资源 | 说明 | 完成标志 |
|---|---|---|
| [vLLM: Easy, Fast, and Cheap LLM Serving with PagedAttention](https://blog.vllm.ai/2023/06/20/vllm.html) — vLLM 官方博客 | PagedAttention 原始科普，与 OS 虚拟内存的对应讲得最清楚。深入可续读 SOSP'23 论文 | 能讲清 block / block table 与页 / 页表的完整对应 |
| [How continuous batching enables 23x throughput](https://www.anyscale.com/blog/continuous-batching-llm-inference) — Anyscale | 调度核心思想：为什么 static batching 浪费，continuous batching 怎么按 iteration 粒度插拔请求 | 能解释 continuous batching 相对 static batching 的收益来源 |

### 第三层：整体串起来（Capstone）

| 资源 | 说明 | 完成标志 |
|---|---|---|
| [Inside vLLM: Anatomy of a High-Throughput LLM Inference System](https://www.aleksagordic.com/blog/vllm) — Aleksa Gordić（[vLLM 官方转载](https://blog.vllm.ai/2025/09/05/anatomy-of-vllm.html)） | 按 LLM → LLMEngine → EngineCore → Scheduler → Executor → Worker → ModelRunner 链路逐层解剖 v1 架构。长文，读到哪层就把架构图上对应框补注一次 | 重画架构图：标注"uses"语义图例、进程边界（EngineCore 独立进程，ZMQ 通信）、各组件职责一句话 |

## 后续：回到代码（前三层完成后）

- 簿记层：`vllm/v1/core/kv_cache_manager.py`、`kv_cache_coordinator.py`（分配 block、block table、prefix cache），`vllm/v1/core/sched/scheduler.py` 的 `schedule()`
- 执行层：`vllm/v1/worker/gpu_model_runner.py` 的 `execute_model()`，attention backend 按 block table 做 paged 读写的接口
- Config（`ParallelConfig` / `CacheConfig` / `SchedulerConfig`）不专门啃，遇到时顺藤摸瓜查字段含义
