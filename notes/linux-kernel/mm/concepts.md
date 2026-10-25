# MM 基础概念

> 内核 Documentation/mm/concepts.rst 的要点（原 known-concepts）。

- **虚拟内存动机** — 物理内存不连续（有 hole）、不同架构/SoC 地址布局不同，虚拟内存把这些硬件复杂性藏起来，给软件一个统一连续的地址空间假象。
- **按需分页（demand paging）** — 虚拟内存允许只把需要的数据放在物理内存中，不常用的不占物理页。
- **页表层次结构** — 虚拟地址到物理地址的翻译通过多级页表完成。最高级页表指针存在 CPU 寄存器中，虚拟地址的高位索引各级页表，最低位是页内偏移。
- **TLB** — CPU 缓存地址翻译结果的硬件 cache。TLB 条目有限，大内存工作负载容易 TLB miss（代价高，要遍历多级页表）。
- **Huge Pages** — 用更高级别的页表项直接映射大页（x86 上 2MB 或 1GB），减少 TLB 压力。两种机制：hugetlbfs（预分配、显式、硬保证）和 THP（动态、透明、best-effort）。hugetlbfs 不可回收不可 swap，THP 不能完全取代 hugetlbfs（确定性需求、1GB 大页、KVM pin 等场景）。
- **Zones** — 硬件对物理内存访问有限制（设备 DMA 寻址范围、32 位架构内核地址空间不够映射全部物理内存等），Linux 按用途把物理页分组到不同 zone：ZONE_DMA、ZONE_NORMAL、ZONE_HIGHMEM 等。布局是硬件相关的。
- **Nodes（NUMA）** — 多处理器系统中内存按 bank 组织，不同 CPU 访问不同 bank 延迟不同。每个 bank 是一个 node，有独立的 zone、free list、统计计数器。内核为每个 node 构建独立的内存管理子系统。
- **Page Cache** — 文件数据的物理内存缓存。读文件时数据放入 page cache 避免重复磁盘 IO；写文件时数据先写 page cache（标记 dirty），后续由内核写回存储设备。
- **Anonymous Memory** — 不 backed by 文件系统的映射（栈、堆、mmap MAP_ANONYMOUS）。首次读访问映射到全零页（CoW），写时分配真实物理页并标记 dirty，回收时 swap out。
- **Reclaim** — 回收可回收的物理页重新利用。可回收页：page cache（磁盘有副本可直接丢弃）、anonymous memory（可 swap out）。不可回收页：内核数据结构、DMA buffer。两种模式：异步（kswapd 在 low watermark 唤醒）和同步（direct reclaim 在 min watermark 触发，分配阻塞直到回收完成）。
- **Compaction** — 解决外部碎片。把已占用的页从 zone 底部搬到顶部的空闲位置，腾出连续空闲块。触发场景：设备驱动需要大块连续 DMA buffer、THP 分配大页。异步（kcompactd）或同步（分配请求触发）。
- **OOM Killer** — 内存耗尽且无法回收足够内存时的最后手段。选择一个进程杀掉，释放内存保全系统。
- **物理连续内存需求场景** — 没有 IOMMU 的设备 DMA、huge page（2MB/1GB）、部分架构的内核栈、kexec/crash kernel。本质：谁在访问内存决定连续性要求，CPU 有 MMU 不需要物理连续，设备没有则必须物理连续。
