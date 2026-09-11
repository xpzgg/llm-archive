# PyTorch 学习规划（GPU 内存方向）

> 目标读者：Linux mm / lock / IOMMU 背景，从 CPU 场景转向 GPU 场景。
> 已完成：算子定义 → 注册 → 下发链路（见 `operator.md`）。
> 本文只做路线规划和重点标注；具体概念的理解另写专篇。

## 总目标

能独立分析和定位 GPU 显存问题（OOM、碎片、异常占用、抖动），理解 PyTorch 显存管理的分层全貌，为 GPU 场景的问题排查打底。

## 路线总览

```
①Tensor 存储抽象 → ②CUDACachingAllocator → ③观测与调试 → ④专题(按需) → ⑤自选研究(SMMU)
   半天              核心，投入最多           你的主场
```

---

## ① Tensor 存储抽象（半天）

**为什么学**：这是一切显存分配的入口层。不理解 Storage 与 Tensor 的分离，后面 view 共享显存、引用计数回收、allocator 插桩都无从谈起。

**重点**：
- `Tensor → TensorImpl → Storage → DataPtr → Allocator` 这条链各自的职责
- Storage/Tensor 分离的动机：view 只改元数据、共享同一块存储
- 所有 CUDA tensor 的显存都经过 `aten/src/ATen/cuda/EmptyTensor.cpp::empty_cuda()`——它是从算子链路跨进内存世界的门

**检验**：能空手画出这条链，说清它和你已学的算子 `meta()` 在哪接上。

## ② CUDACachingAllocator（核心阶段）

**为什么学**：GPU 显存问题的第一嫌疑人。它是 PyTorch 在驱动之上实现的用户态分配器，与内核 mm 同构（glibc malloc 之于 brk/mmap），你的存量经验迁移成本最低、收益最大。OOM、碎片、性能抖动，大半最终追到这里。

**重点**（按优先级）：
1. **缓存动机**：为什么不能让每次分配都进驱动（`cudaMalloc` 贵 + 隐式同步）
2. **分级与碎片的关系**：small/large 分池、best-fit + split + 合并——为什么显存还有富余却 OOM
3. **stream 归属与 `recordStream`**：free 不等于立即可复用，延迟复用保证并发正确性（RCU 宽限期的同构）
4. **OOM 重试链**：分配失败 → 回收未用缓存 → 重试 → 抛异常；`empty_cache()` 的语义边界
5. **`expandable_segments`**：VA 预留与物理提交分离，碎片问题的新解法及其代价（与旧 IPC、peer access 互斥）

**可以略过**：具体常量值、配置项全表——用到再查，不要在这里耗。

**检验**：能完整解释一条 "CUDA out of memory: X GiB reserved but unallocated" 报错背后的机制链，并给出两三种处理思路。

## ③ 观测与调试（你的主场）

**为什么学**：system engineer 的立身之本。前两阶段的理解必须通过观测手段落地，否则遇到问题还是没有抓手。

**重点**：
- `memory_allocated` vs `memory_reserved`：tensor 实际占用 vs allocator 向驱动持有的总量
- 三件套各自回答什么问题：`memory_stats()`（构成与碎片指标）、`memory_snapshot()`（时间线火焰图）、`_record_memory_history()`（谁在分配的调用栈）
- `PYTORCH_NO_CUDA_MEMORY_CACHING=1`：二分定位"是不是 allocator 的问题"的开关
- allocator 内置 USDT 探针，可接你已有的 uprobe 方法论

**检验**：拿到一个 OOM，能立刻说出排查顺序和每步用什么工具。

## ④ 专题（按需选学，不必按序）

- **pinned memory**：H2D 拷贝为什么要求 host 页钉住——原理你已懂，只看 PyTorch 的 host 缓存池怎么实现
- **Unified Memory**：GPU 缺页 + 迁移，对照你已熟的 HMM/SPM；注意 NVIDIA 侧是闭源黑盒
- **CUDA IPC**：多进程共享显存（vLLM 等推理框架的基础），类比 dma-buf；与 expandable_segments 的互斥关系
- **多卡**：peer access、NUMA 亲和、NCCL buffer 注册
- **`torch.cuda.MemPool`**：模块级显存预算，推理混部场景用

## ⑤ 自选研究（连接你的主方向）

GPU 经 PCIe 访问 host 内存时，SMMU 在 DMA 路径中负责哪一段翻译？GPU 自己的 MMU（驱动管 GPU 页表）与 SMMU（IO 翻译）的职责分界在哪？CXL 出现后怎么变？——这是把你 iommu/smmu 经验接进 GPU 场景最自然的桥。

---

## 学习方法

- **用内核概念做锚点**：每遇到一个 PyTorch 机制，先对号入座到对应的 mm 概念，再盯住两者的差异——差异就是设计意图所在。
- **每阶段配一个最小实验**：①画出抽象链 ②复现碎片型 OOM 并对比 expandable_segments ③用 snapshot + history 分析一次峰值 ④（进阶）`change_current_allocator` 挂一个自写的统计 allocator。
- **抓道放术**：缓存分级、stream 归属、延迟复用、VA/物理分离是稳定的道；常量值、环境变量名是易变的术，随版本漂移，用到再查。

## 建议节奏

① → ② → ③ 按序走，是主干；④ 由实际问题驱动挑选；⑤ 作为长期研究线索。② 值得投入总时间的一半。
