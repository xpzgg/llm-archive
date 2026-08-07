# NVIDIA GPU 架构学习资源汇总

> 链接均验证于 2026-08，HTTP 200 可直达。官方 PDF 偶尔会被挪位置，失效时用 Wayback Machine 按文件名搜。

## 架构白皮书（按年代）

学习主线：重点关注每代 SM 组织方式、memory hierarchy、调度模型的变化，而不是记参数。

| 架构 | 代表芯片 | Compute Capability | 白皮书 |
|---|---|---|---|
| Tesla | GT200 (GTX 280) | 1.3 | [GeForce GTX 200 GPU Technical Brief](https://www.nvidia.com/docs/IO/55506/GeForce_GTX_200_GPU_Technical_Brief.pdf) |
| Fermi | GF100 | 2.0 | [NVIDIA Fermi Compute Architecture Whitepaper](https://www.nvidia.com/content/PDF/fermi_white_papers/NVIDIA_Fermi_Compute_Architecture_Whitepaper.pdf) |
| Kepler | GK110/GK210 (K80) | 3.5/3.7 | [NVIDIA Kepler GK110/GK210 Architecture Whitepaper](https://www.nvidia.com/content/PDF/kepler/NVIDIA-Kepler-GK110-GK210-Architecture-Whitepaper.pdf) |
| Maxwell | GM107 (GTX 750 Ti) | 5.0 | [GeForce GTX 750 Ti Whitepaper](https://international.download.nvidia.com/geforce-com/international/pdfs/GeForce-GTX-750-Ti-Whitepaper.pdf) |
| Pascal | GP100 (P100) | 6.0 | [NVIDIA Pascal Architecture Whitepaper](https://images.nvidia.com/content/pdf/tesla/whitepaper/pascal-architecture-whitepaper.pdf) |
| Volta | GV100 (V100) | 7.0 | [NVIDIA Volta Architecture Whitepaper](https://images.nvidia.com/content/volta-architecture/pdf/volta-architecture-whitepaper.pdf) |
| Turing | TU102 (RTX 2080 Ti) | 7.5 | [NVIDIA Turing Architecture Whitepaper](https://images.nvidia.com/aem-dam/en-zz/Solutions/design-visualization/technologies/turing-architecture/NVIDIA-Turing-Architecture-Whitepaper.pdf) |
| Ampere（数据中心） | GA100 (A100) | 8.0 | [NVIDIA Ampere Architecture Whitepaper](https://www.nvidia.com/content/dam/en-zz/Solutions/Data-Center/nvidia-ampere-architecture-whitepaper.pdf) |
| Ampere（消费级） | GA102 (RTX 3090) | 8.6 | [NVIDIA Ampere GA102 GPU Architecture Whitepaper](https://www.nvidia.com/content/PDF/nvidia-ampere-ga-102-gpu-architecture-whitepaper-v2.pdf) |
| Ada Lovelace | AD102 (RTX 4090) | 8.9 | [NVIDIA Ada GPU Architecture Whitepaper](https://images.nvidia.com/aem-dam/Solutions/geforce/ada/nvidia-ada-gpu-architecture.pdf) |
| Hopper | GH100 (H100) | 9.0 | [NVIDIA H100 Tensor Core GPU Architecture](https://resources.nvidia.com/en-us-tensor-core/nvidia-h100-tensor-core-gpu-architecture)（官方下载页，需填表） |
| Blackwell（消费级） | GB202 (RTX 5090) | 12.0 | [NVIDIA RTX Blackwell GPU Architecture](https://images.nvidia.com/aem-dam/Solutions/geforce/blackwell/nvidia-rtx-blackwell-gpu-architecture.pdf) |
| Blackwell（数据中心） | GB100/B200 | 10.0 | [NVIDIA Blackwell Architecture Technical Brief 页面](https://www.nvidia.com/en-us/data-center/technologies/blackwell-architecture/) |

## 各代核心变更速览

按我自己的理解总结，一两句话说清每代"为什么变"：

- **Tesla (G80/GT200, 2006–2008)**：统一着色器架构 + 首次支持 CUDA。顶点/像素着色器合并为通用流处理器，GPU 从图形专用走向通用计算。
- **Fermi (2010)**：为通用计算彻底重构。引入真正的 L1/L2 cache 层次、ECC 显存、双 warp scheduler，确立此后所有 CUDA GPU 的基本形态。
- **Kepler (2012)**：主题是能效。SMX 把每 SM 的 CUDA core 提到 192 个，靠规模而非频率提性能；引入 Hyper-Q（多 CPU 提交队列）和 dynamic parallelism（kernel 内启动 kernel）。
- **Maxwell (2014)**：继续死磕每瓦性能。SM 内部重新划分为 4 个独立处理块，调度更细粒度、利用率更高；主要面向消费市场，双精度被大幅削减。
- **Pascal (2016)**：面向 HPC 和早期深度学习。引入 NVLink、HBM2、FP16 半精度（P100 上 2 倍吞吐），统一内存支持页迁移和系统级寻址。
- **Volta (2017)**：现代 GPU 的分水岭。首代 Tensor Core（矩阵乘加专用单元）；独立线程调度（每线程独立 PC 和调用栈），取代 warp 锁步执行，volta 之后 SIMT 语义彻底改变。
- **Turing (2018)**：把光线追踪做成硬件。引入 RT Core（BVH 遍历加速），Tensor Core 下放到消费级并支持 INT8/INT4 推理。
- **Ampere (2020)**：数据搬运和稀疏化。第三代 Tensor Core 支持 TF32 和结构化稀疏（2:4 sparsity）；async copy 允许 global→shared 数据搬运绕过寄存器；A100 引入 MIG（单卡切 7 个隔离实例）。
- **Ada Lovelace (2022)**：主要靠工艺（TSMC 4N）和规模取胜，架构增量不大。值得注意的点是 SER（Shader Execution Reordering，重排光追线程提高一致性）和 FP8 进入消费级。
- **Hopper (2022)**：为 Transformer/大模型重构。第四代 Tensor Core + Transformer Engine（FP8 逐层动态缩放）；TMA 硬件异步张量搬运；thread block cluster 让多个 SM 共享分布式 shared memory。编程模型从"单 SM"扩展到"SM 集群"。
- **Blackwell (2024)**：从单卡走向机柜级系统。两个 reticle 极限 die 用 10 TB/s 链路封成一颗逻辑 GPU；Tensor Core 支持 FP4/FP6 微缩放格式；NVLink 5 + NVL72 把 72 卡连成一个大内存域，性能叙事从"单 GPU FLOPS"转向"整机柜吞吐"。

补充说明：

- Maxwell GM204 (GTX 980) 白皮书官方链接已失效，用 Wayback Machine 搜 `NVIDIA_Maxwell_GM204_Architecture_Whitepaper.pdf` 可找到存档。
- Hopper 数据中心白皮书 NVIDIA 只放在 resources.nvidia.com 的表单页后面；配合下面的 "Hopper Architecture In-Depth" 博客读，信息量基本等价。
- 同代数据中心芯片（GA100/GH100/GB100）和消费芯片（GA102/AD102/GB202）架构有差异（如 FP64 单元配比、HBM vs GDDR），白皮书要分开看。

## SIMT 的官方定义出处

NVIDIA 对 SIMT（Single-Instruction Multiple-Threads）的正式定义在 CUDA 官方文档里，白皮书反而不作为重点：

- **现行版**：[CUDA Programming Guide §1.2.2.2 "Warps and SIMT"](https://docs.nvidia.com/cuda/cuda-programming-guide/01-introduction/programming-model.html#warps-and-simt)（CUDA 13 起重构后的新文档）。核心表述：32 个线程组成 warp，warp 内所有线程执行同一份 kernel 代码，但每个线程可以走不同的控制流路径。
- **经典版**：[CUDA C++ Programming Guide §4.1 "SIMT Architecture"](https://docs.nvidia.com/cuda/archive/13.0.0/cuda-c-programming-guide/index.html#simt-architecture)（CUDA 13.0 存档；非存档的最新 URL 已重定向到上面的新文档）。这一版论述最完整，值得细读：
  - "The multiprocessor creates, manages, schedules, and executes threads in groups of 32 parallel threads called *warps*."
  - warp 内线程"start together at the same program address, but they have their own instruction address counter and register state and are therefore free to branch and execute independently" —— 这句话就是 SIMT 与 SIMD 的本质区别，描述的是 Volta 之后的独立线程调度语义。
  - 分支分化（divergence）的行为定义：warp 一次执行一条公共指令，分化时串行执行各分支路径并禁用不在该路径上的线程。
  - 同章 §4.2 "Asynchronous SIMT Programming Model"（同页锚点 `#asynchronous-simt-programming-model`）定义了 Ampere 之后的异步 SIMT 模型（async copy、memcpy_async）。
- **执行模型演进的官方表述**：Volta 白皮书的 "Independent Thread Scheduling" 一节是对 SIMT 语义的最大一次官方修订（warp 锁步 → 线程级独立调度 + `__syncwarp()`），配合上面 §4.1 的措辞变化看最清楚。

## NVIDIA 官方博客（Technical Blog）

主站：[NVIDIA Technical Blog](https://developer.nvidia.com/blog/)，内容质量普遍很高，代码和实测数据齐全。

架构解析系列（和白皮书互补，更偏工程视角，必读）：

- [Inside Pascal](https://developer.nvidia.com/blog/inside-pascal/) — Pascal 新特性（NVLink、HBM2、统一内存改进）
- [Inside Volta](https://developer.nvidia.com/blog/inside-volta/) — 首代 Tensor Core、独立线程调度（independent thread scheduling），Volta 是现代 CUDA 执行模型的分水岭
- [NVIDIA Turing Architecture In-Depth](https://developer.nvidia.com/blog/nvidia-turing-architecture-in-depth/)
- [NVIDIA Ampere Architecture In-Depth](https://developer.nvidia.com/blog/nvidia-ampere-architecture-in-depth/) — 第三代 Tensor Core、稀疏化、async copy
- [NVIDIA Hopper Architecture In-Depth](https://developer.nvidia.com/blog/nvidia-hopper-architecture-in-depth/) — Transformer Engine、TMA、thread block cluster，做 LLM 相关必看

其他值得长期跟踪的：

- [CUDA Refresher 系列](https://developer.nvidia.com/blog/tag/cuda-refresher/) — CUDA 编程模型复习，适合入门和查漏补缺
- CUDA 官方文档的各架构 Tuning Guide（[CUDA Documentation](https://docs.nvidia.com/cuda/) 下搜 "Tuning Guide"）——写 kernel 优化时比白皮书更实用
