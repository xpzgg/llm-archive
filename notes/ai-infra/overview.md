# AI Infra 学习地图

> 目标：为 AI Infra 方向打基础。主线是 GPU 编程与架构，向外延伸到训练/推理系统、内核相关知识（已学的 MM 为底座之一）。
>
> 核心规划：**PMPP + 各代架构 whitepaper + 大量 profile 实践**。目标是 performance engineer 视角——熟悉硬件行为到能写出榨干性能的算子，不做 GPU 硬件设计，不啃微架构专著（Aamodt《General-Purpose Graphics Processor Architectures》仅作参考书，遇到书上模型解释不了的 profiling 现象时再查）。

## 当前主线：PMPP（CUDA 基础）

教材：*Programming Massively Parallel Processors: A Hands-on Approach*, **第四版**(2022, Hwu / Kirk / El Hajj)。第五版 2026-02 已出，但社区配套资源（讲座、习题解答、中文笔记）都按第四版做。

**结构：** 基础概念（Ch1-6)→ 并行模式（Ch7-15)→ 应用案例（Ch16-18)→ 进阶（Ch19-22)+ 附录 A 数值表示。

### 主线章节（按学习顺序）

标记：**【重点】**逐节精读 + 做 output；**【略看】**快速过一遍抓主线；**【跳过】**不读（按需回补的标注触发条件）。

| 顺序 | 章节 | 标记 | 为什么对 AI infra 重要 | 状态 |
|---|---|---|---|---|
| 1 | Ch1-3 简介 / 异构计算 / 多维 grid | 【重点】 | CUDA 编程模型本体：host/device、grid/block/thread、数据搬运 | ✅ [笔记](pmpp-ch1-5.md) |
| 2 | Ch4 计算架构与调度 | 【重点】 | SM、warp、占用率；看 Nsight 报告的前置 | ✅ [笔记](pmpp-ch1-5.md) |
| 3 | Ch5 内存架构与局部性 | 【重点】 | HBM/shared memory/tiling；GPU 性能问题多为访存问题，FlashAttention 的思想源头 | ✅ [笔记](pmpp-ch1-5.md) |
| 4 | Ch6 性能考虑 | 【重点】 | coalescing、latency hiding 定量分析、thread coarsening、roofline；从"会写"到"写得快" | ⬜ |
| 5 | Ch7 卷积 | 【重点】 | tiling + shared memory 练兵场，矩阵乘优化铺垫 | ⬜ |
| 6 | Ch9 Histogram | 【重点】 | atomic 操作与私有化；为 Ch10 跨 block 归约的 atomicAdd 铺路 | ⬜ |
| 7 | Ch10 Reduction | 【重点】 | softmax/LayerNorm/loss 求和；warp-level reduce；AllReduce 概念前身。**后半部最重要的一章** | ⬜ |
| 8 | Ch11 Scan | 【重点】 | 并行算法 work efficiency；cumsum、采样 | ⬜ |
| 9 | Ch16 Deep learning | 【重点】 | 用 CUDA 实现 NN 层，串起前面所有章 | ⬜ |
| 10 | Ch20 异构集群编程 | 【重点】 | MPI+CUDA，NCCL/TP/PP 概念前身，对口 vLLM 分布式 | ⬜ |
| 11 | 附录 A 数值表示 | 【略看】 | fp16/bf16/fp8 位布局与精度权衡；抓位布局与精度权衡的主线，不抠证明 | ⬜ |

### 学习方法：闭环 + 阶段性输出

只看书是只有 input 没有 output。性能直觉的真正闭环是：**写 kernel → Nsight Compute profile → 看 stall reason / memory throughput / achieved occupancy → 把计数器异常映射回架构模型 → 改代码再跑**。书给模型骨架，profiler 数据把骨架变成直觉。原则：每个阶段学完必须产出一个可验证的 output，证明真的会了，再进下一阶段。

各阶段的 output 建议（对应上表顺序）：

1. Ch1-3：从零写 vector add + 朴素 matmul kernel，Colab/本地跑通
2. Ch4：给朴素 kernel 做 occupancy 分析，解释限制因素（寄存器/block 大小）
3. Ch5：对比 coalesced vs 非合并访存的带宽；写一个 shared memory tiling 版 matmul
4. Ch6：给自己的 kernel 画 roofline，判断 compute-bound 还是 memory-bound
5. Ch7：tiled conv 或 matmul 与 cuBLAS 对比 benchmark，量化差距并解释
6. Ch9：写一个 histogram kernel，体会 atomic 竞争与 privatization 的收益
7. Ch10：写一个可用的 reduction kernel（如 softmax 分母），分块→warp shuffle 逐级优化
8. Ch11：写一个 scan kernel（Kogge-Stone 即可），测 work efficiency
9. Ch16：端到端实现一个 NN 层，串起前面所有章
10. Ch20：跑通一个多卡 allreduce（MPI+CUDA 或 NCCL demo）
11. 附录 A：写 demo 量化对比 fp32/fp16/bf16 的精度损失

### 略看

- Ch19 计算思维 — 全书方法论总结，读完 Ch16 后快读串珠子
- Ch22 进阶实践 — 统一内存/zero-copy/大地址空间，衔接 GPU 内核侧兴趣（`notes/linux-kernel/gpu/`），翻一遍建立印象即可

### 跳过，按需回补

现在不读，遇到对应问题再回来：

- Ch12 Merge — co-rank 分解技巧漂亮，但与采样无直接关系
- Ch13 Sorting — radix sort/select 思路与推理 top-k/top-p 采样相关，做采样优化时回补
- Ch14 稀疏矩阵 — SpMM/Embedding lookup/MoE 路由，碰稀疏化/MoE 时回补

### 跳过

Ch8 stencil（模式与 Ch7 重复）、Ch15 graph、Ch17 MRI、Ch18 静电势能图（HPC 领域案例）、Ch21 dynamic parallelism（生产用得少）。

### 配套资源

- 作者视频课（Izzat El Hajj 本人频道 [@ielhajj](https://www.youtube.com/@ielhajj)）：
  - [Spring 2021 GPU Computing](https://www.youtube.com/playlist?list=PLZrjSW9GrEZE07f8gLECc0tfJwNDm75RW)：24 讲，主线视频伴侣，覆盖 Ch1-15 + warp 同步/streams/dynamic parallelism；无 Ch16/20/附录A
  - [Spring 2026 GPU Computing](https://www.youtube.com/playlist?list=PLZrjSW9GrEZE_uL0qSjc7dzFwnI2AjPm1)：按四版章节号命名，目前只有 Ch11-15、Ch19
- [GPU MODE 社区](https://discord.gg/gpumode)（原 CUDA MODE）：讲座 + KernelBot 打榜，读 Ch7 起开始动手写 kernel；其 YouTube 讲座只有前几讲按书走（Lecture 2 = Ch1-3），后面是社区专题
- [junkin/pmpp-lectures](https://github.com/junkin/pmpp-lectures)：讲座资料，前半段对应 Ch1-6
- [tugot17/pmpp](https://github.com/tugot17/pmpp)：第四版习题完整解答
- [psmarter 中文笔记系列](https://smarter.xin)：第四版 22 章笔记
- 无本地 GPU 时用 Google Colab 免费 T4 跑书例

---

## 与已有知识的衔接

- `notes/linux-kernel/mm/`：页表/fault、buddy、SLUB、shmem（/dev/shm 跨进程共享是 DataLoader/NCCL SHM 的基础）。MM 回收链（rmap/LRU/swap）暂停，遇到 GPU 统一内存、 pinned memory、容器 OOM 再补。
- `notes/linux-kernel/gpu/`：AMD 侧已有笔记（amdkfd、GPU 虚拟地址空间等）。

## 后续候选方向（主线完成后评估）

- CUDA 进阶：Tensor Core / cuBLAS / Triton
- 推理系统：vLLM（paged attention、continuous batching）
- 训练系统：并行策略（DP/TP/PP）、NCCL、checkpoint
- GPU 内核侧：统一内存、mmu_notifier、GPU 驱动与 MM 的接口

## 阶段性输出：NVIDIA GPU vs 华为昇腾 NPU 架构评测

- [ ] 学完 GPU 与 NPU 架构后，完成一份 NVIDIA GPU 与华为昇腾 NPU 的横向评测：从关键组件的功能、性能、软件可编程性和设计取舍出发，说明双方各自擅长什么、限制在哪里，以及这些差异对训练、推理和算子开发有什么实际影响。

评测维度：

- **核心计算单元**：CUDA Core / Tensor Core 与 Vector / Cube 单元的职责划分；支持的数据类型、矩阵形状、峰值吞吐、通用性和利用率。
- **访存与片上搬运单元**：load/store、MTE/TMA 等单元如何在 HBM、L2、L1、shared memory/UB/L0 之间搬运数据；是否支持异步搬运、多维 tile、地址生成和同步。
- **Copy 单元**：Host↔Device、Device↔Device 和片内拷贝能力；能否与计算重叠；是否支持随路 layout conversion、transpose、padding、量化/反量化等转换，分别由哪个硬件单元完成。
- **HBM 与外部内存系统**：容量、带宽、memory controller/partition 组织、访问粒度，以及在真实工作负载中能够达到的有效带宽。
- **Cache 与片上存储**：L2/L1 与 shared memory、UB、L0A/L0B/L0C 的容量、带宽、延迟、作用范围、硬件或软件管理方式，以及典型命中率/复用模式。
- **互连与多卡扩展**：NVLink/NVSwitch 与昇腾互连方案的带宽、拓扑、集合通信支持和扩展效率。
- **编程模型与工具链**：CUDA/CUTLASS/Triton/Nsight 与 CANN/Ascend C/TBE/性能分析工具的抽象能力、成熟度、可观测性和调优成本。

评测原则：选择同代、同定位的数据中心产品；区分厂商标称峰值与实测性能；统一数据类型、矩阵规模、batch、功耗口径和通信拓扑；对每项结论记录官方资料、microbenchmark 或真实模型测试依据，避免仅根据规格表下结论。
