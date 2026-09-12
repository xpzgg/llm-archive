# vLLM / DSA_CP 阅读计划

> 背景：为 xlite 支持 DSA_CP 做准备（首期目标模型 GLM-5.2）。
> 学习路径四步：复习算法 → vllm 基本组件与工作流程 → vllm-ascend/xlite 与 vllm 的协同 → 设计 dsacp。
> 当前在读：The Illustrated Transformer（Jay Alammar）——已完成 encoder-decoder 部分复习。

## 算法线

### 必读

0. [The Illustrated GPT-2](https://jalammar.github.io/illustrated-gpt2/)（Alammar）
   decoder-only 架构图解，补 Illustrated Transformer（encoder-decoder）没覆盖的部分：
   masked self-attention 堆叠、逐 token 生成循环。同作者同画风，快速过。
1. [Transformer Inference Arithmetic](https://kipp.ly/blog/transformer-inference-arithmetic/)（Kipply）
   推理阶段的算术：KV cache 显存怎么算、decode 为什么是 memory bound、并行通信量怎么估。
   DSA_CP 本质上就是这笔账，这篇给的是算账方法。短文，一两个小时。
2. [DeepSeek-V3.2-Exp 技术报告](https://github.com/deepseek-ai/DeepSeek-V3.2-Exp)
   DSA / Lightning Indexer 一手出处，任务核心算法。GLM-5.2 的稀疏注意力是同一套方案
   （vllm-ascend 称 SFA），读它等于在读 GLM-5.2 的 attention。
   其中 MLA 部分读不动时再回头查 DeepSeek-V2 论文对应一节，不必单独通读。

### 选读

3. [Understanding Encoder And Decoder LLMs](https://magazine.sebastianraschka.com/p/understanding-encoder-and-decoder)（Raschka）
   encoder-only / encoder-decoder / decoder-only 三种架构的差异与适用任务，
   以及为什么 LLM 主流收敛到 decoder-only。Illustrated Transformer 同源作者风格，补架构演进视角。
4. [DeepSpeed Ulysses](https://arxiv.org/abs/2309.14509)
   只看核心思想两页：序列切分 + all-to-all 做 head↔序列维转置。
   DSA_CP 输出端恢复布局的设计就是它。设计卡住了再看。

## vllm 线

### 必读

1. [Inside vLLM: Anatomy of a High-Throughput LLM Inference System](https://vllm.ai/blog/2025-09-05-anatomy-of-vllm)
   覆盖"基本组件 + 工作流程"：EngineCore、Scheduler、KVCacheManager、continuous batching
   一路讲到分布式 serving。读完用它回看 vllm-ascend 的 NPUModelRunner，协同关系就清楚了。
   [中文编译版](https://www.gongjiyun.com/blog/2026/4/r1fawwgazibjjqk3kqwczqa6n9f/)。
2. [vLLM V1: A Major Upgrade to vLLM's Core Architecture](https://vllm.ai/blog/2025-01-27-v1-alpha-release)
   v1 重写动机：前后端进程拆分、CPU 开销治理。看完理解 LLMEngine / EngineCore 为什么拆开。

### 选读

3. [PagedAttention 论文](https://arxiv.org/abs/2309.06180)（SOSP'23）
   vllm 的根基：KV cache 分页管理。block_table 概念贯穿后续所有改动，设计阶段当参考查。
4. [Halfrost 的 vLLM V1 系列](https://github.com/halfrost/Halfrost-Field/blob/master/contents-en/LLM/vllm/03-v1-process-architecture.md)
   中文源码向，结论锚定具体 commit，适合配合代码读。

## 暂缓（将来扩展到对应路径再补）

- [Ring Attention](https://arxiv.org/abs/2310.01889) — DCP 的思想源头（KV 分片 + 环形通信）。
- [Megatron Sequence Parallelism](https://arxiv.org/abs/2205.05198)（论文 SP 一节）— SP/FlashComm 的理论来源。
- [Large Transformer Model Inference Optimization](https://lilianweng.github.io/posts/2023-01-10-inference-optimization/) — 全景综述，优先级低于 Inside vLLM。

## 参照实现（不是文章，但设计时必读代码）

- GLM-5.2 的 CP 参照实现是 vllm-ascend 的 `vllm_ascend/attention/context_parallel/sfa_cp.py`
  （SFA DSA-CP），不是 `dsa_cp.py`（那是 DeepSeek V4 路径）。
- GLM-5.2 独有机制 shared indexer topk：部分层跑完整 indexer，其余层复用共享 top-k
  （xlite 侧体现为 `indexFullMask` / `dsaPerLayerTopk`）。对 CP 的约束：层间 token 切片必须对齐。
