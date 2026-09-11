## 2.6.1 GPU as a Whole 总结

这一节的核心是：历代 NVIDIA GPU 的顶层组成其实很稳定，主要变化是各部分的规模、带宽和专业化程度。

```text
CPU
 │ PCIe / coherent NVLink
 ▼
Host Interface
 ├─ kernel launch → GigaThread Engine → SM
 └─ memcpy        → Copy Engine
                         │
                GPC → TPC → SM
                         │
                    L1 / Shared
                         │
                        L2
                         │
              Memory Controllers
                         │
                     GDDR / HBM
```

### 1. Host Interface

负责接收 CPU 提交的命令并分发：

- Kernel launch 交给 GigaThread Engine，再把 thread blocks 分配到 SM。
- 内存拷贝命令交给 Copy Engine。
- 负责 CPU、GPU 内部引擎和多个 GPU 之间的同步。

CUDA 的 stream 和 event，是这套硬件调度与同步能力的软件接口。

### 2. Copy Engine

专门进行数据搬运，不占用 SM 的计算执行单元：

```text
Host ↔ Device
Device ↔ Device
```

它可以与 SM 执行 kernel 并发，从而实现 computation/communication overlap。Copy Engine 的设计目标是尽量跑满 PCIe 或 NVLink，并可完成部分受支持的 layout conversion，例如 CUDA array 与 linear memory 之间的转换。

### 3. L2 Cache

L2 是所有 SM 和其他内存客户端共享的全芯片缓存，也是外部显存流量的汇聚点。

它有两个重要作用：

- 缓存被重复访问的数据，减少 HBM/GDDR 访问；
- 汇聚和整理大量 SM 的请求，再分发给多个 memory controller。

Fermi 开始引入统一 L2，并使用地址哈希把请求分散到不同 memory partition，解决早期 GPU 的 **partition camping**：

```text
大量 block 集中访问少数 memory controller
→ 其他 controller 空闲
→ 无法发挥总显存带宽
```

现代 GPU 的 L2 不断扩大，从早期不足 1 MB 增长到几十甚至上百 MB。

### 4. Host memory 的缓存一致性

GPU 自己的 HBM/GDDR 数据可以被 L2 缓存；CPU host memory 是否能缓存，取决于互连是否支持硬件一致性。

- **PCIe** 不是 coherency link：跨 CPU/GPU 的页面一致性由驱动、页表和 page fault 软件管理；GPU 经 PCIe 直接访问的 host memory 不缓存在 GPU L2。
- **Coherent NVLink** 可以提供硬件一致的共享地址空间，但实际缓存能力仍可能不对称。

### 5. DRAM Interface

负责把来自 SM 的访问转换成显存能够高效处理的事务，并把流量分布到多个 memory controller/channel。

Warp 访存首先在 SM 的 load/store 路径进行 coalescing；之后请求还会经过 L1/L2、全芯片互连和 DRAM controller 的汇聚与调度。

早期 GPU 对 coalescing 的要求非常严格，必须连续并满足对齐；后来只要地址具有局部性就能合并，但跨越更多 sector/cache line 仍会产生额外事务。

显存技术的演进是：

```text
GDDR3
→ 更高代际的 GDDR（主要用于消费卡）
→ HBM2（Pascal 数据中心 GPU 开始）
→ 数 TB/s 的 HBM（Hopper / Blackwell）
```

### 6. NVLink

Pascal GP100 开始，数据中心 GPU 引入 NVLink：

```text
GPU ↔ GPU
后来扩展到 CPU ↔ GPU
```

NVLink 使用多个专用高速端口，带宽远高于通用 PCIe。Hopper 又引入外部 NVLink Switch，把高带宽互连扩展到更多 GPU。

消费级显卡只在 Turing、RTX 3090 等产品上短暂提供过，后来取消。

### 7. GPC、TPC 与 SM

GPU 内部采用分级组织：

```text
GPU
└─ GPC
   └─ TPC
      └─ 通常 2 个 SM
```

- **SM**：执行 warp 和 thread block 的核心计算单元。
- **TPC**：少量 SM 的局部分组，历史上与纹理单元密切相关。
- **GPC**：更大的物理分区，组织多个 TPC/SM，以及局部互连和部分图形资源。

这样分层能够共享上层资源、降低片上布线复杂度，并让 NVIDIA 通过增减 GPC/TPC 构造不同规模的芯片。

过去 GPC/TPC 对 CUDA 程序基本不可见；Hopper 的 thread-block cluster 开始把 GPC 内的 SM 局部性暴露给软件。

### 整节最重要的认识

虽然 SM 数量从十几个增长到近两百个，L2 增长了两个数量级，并出现 HBM 和 NVLink，但 GPU 顶层数据流没有根本变化：

```text
CPU 提交工作
→ 前端分发计算和拷贝
→ 大量 SM 并行执行
→ L2 汇聚全芯片流量
→ 多个 memory controller 并行访问 HBM
→ NVLink/PCIe 连接 GPU 外部
```

因此，现代 GPU 性能的核心问题仍然是：

> 能否让前端持续提供工作、让 SM 保持繁忙，并让 L2、HBM、Copy Engine 和互连持续为它们供应数据。

原文：[CUDA Handbook §2.6.1](https://www.cudahandbook.com/book/ch2/gpu-architecture)
