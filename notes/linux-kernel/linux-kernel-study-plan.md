# Linux 内核学习计划：整体认识 + IOMMU 深入

Oct 5, 2026 · @Jay

## 使用方法

两份计划互相独立，可以并行：计划一是广度，每周一个子系统；计划二是深度，按阶段推进。每个单元都分三步走：

1. **开始前看引导问题**：读书和读代码时都带着问题找答案，不要从头到尾平铺直叙地读。答不上来的问题，就是这一单元真正要学的东西。
2. **完成动手任务**：用来替代组里没有的实战机会。计划一以用工具观察运行中的系统为主；计划二要求亲手写代码、跑起来、观察现象。
3. **结束后找我验收**：把你对引导问题的回答写下来发给我，我来挑错、追问，再出一套新题，包括概念题和"给你一段代码或一份 oops，说出发生了什么"的场景题。

### 两个替代实战的通用方法

- **复盘 Fixes 提交**：主要用于计划二。每个阶段在对应目录里找 3 个近期带 `Fixes:` 标签的提交，例如 `git log --grep=Fixes -- drivers/iommu/`。先只看提交说明，自己推断 bug 出在哪、为什么会出、怎么修，再看 diff 对答案。这是最接近真实 debug 的练习。
- **跟邮件列表 review**：每周挑一组相关补丁，自己先写 review 意见，再和维护者的实际回复对照。

### 验收标准

一个单元算学完，需要做到三点：

- 能不看代码，用一张图或一段话讲清主路径
- 能说出 2 到 3 个关键设计取舍，以及为什么这样选
- 动手任务能跑通，并且能解释观察到的现象

## 计划一：Linux 整体认识（约 11 周）

这条线只读书和文章，不读源码。以 LKD 为骨架，用较新的材料补上现状。每周的流程是：读指定材料，回答引导问题，再做一个 30 分钟到 1 小时的观察实验。观察实验只用 ftrace、perf、bpftrace 等工具看运行中的系统，用来加深直观印象，没时间可以跳过。内存管理你已经熟，不单独安排。

### 主要材料

- **《Linux Kernel Development》第三版**（Robert Love）：骨架。注意它基于 2.6.34，部分内容已过时。
- **《Linux Kernel Programming》第二版**（Kaiwan Billimoria）：基于 6.1，用来补现状。
- **《Systems Performance》第二版**（Brendan Gregg）：从观察和性能的角度理解整个系统，和观察实验配合。
- **LWN Kernel Index**：每周的关键文章。
- **内核 Documentation/**：只读设计说明，不看代码。

### 第 1 周：系统调用

**材料**：LKD 第 5 章；LWN《Anatomy of a system call》两篇（以 x86 为例，概念通用）；ARM《Learn the architecture: AArch64 Exception Model》。

- arm64 上执行 `svc` 指令后，CPU 和内核分别做了什么？用户态寄存器保存在哪里？
- 系统调用返回用户态之前，内核要检查哪些事情？
- vDSO 为什么能让 `clock_gettime` 不进内核？

**观察**：用 `strace -c` 和 `perf trace` 看一个常用程序的系统调用分布；对比 vDSO 版和 `syscall()` 版 `clock_gettime` 的耗时。

### 第 2 周：进程生命周期

**材料**：LKD 第 3 章；《Linux Kernel Programming》进程相关章节。

- 线程和进程在内核眼里有什么区别？`clone` 的哪些 flag 决定了这一点？
- `exec` 做了哪些事？进程退出时资源按什么顺序释放？僵尸进程是什么状态？

**观察**：用 bpftrace 挂进程 fork、exec、exit 的 tracepoint，观察 shell 执行一条命令的全过程。

### 第 3 周：调度器

**材料**：LKD 第 4 章（CFS 部分当历史看）；LWN 关于 EEVDF 的系列文章；`Documentation/scheduler/sched-eevdf.rst`；LWN 关于 sched_ext 的文章。

- EEVDF 里的 lag、eligible、virtual deadline 分别是什么意思？和 CFS 有什么不同？
- 唤醒一个任务时如何选择 CPU？负载均衡在什么时候发生？
- 几种抢占模型（NONE、VOLUNTARY、FULL、LAZY）有什么区别？
- 各调度类（deadline、rt、fair、ext、idle）之间按什么顺序选任务？

**观察**：用 `perf sched` 观察两个不同 nice 值的忙循环，看 CPU 时间如何分配。

### 第 4 周：中断、延后执行与定时器

**材料**：LKD 第 7、8、11 章；`Docum``entation/core-api/genericirq.rst`；ARM《Learn the architecture: GICv3 and GICv4》；LWN 关于 tasklet 替换的文章。

- 一次中断从 GIC 到驱动 handler 经过哪些层？SPI、PPI、LPI 各是什么？
- softirq 什么时候执行？什么情况下交给 ksoftirqd？
- threaded irq、workqueue、tasklet 各适合什么场景？为什么 tasklet 在被淘汰？
- hrtimer 和普通定时器有什么区别？NO_HZ 解决了什么问题？

**观察**：在有网络或磁盘负载时观察 `/proc/interrupts` 和 `/proc/softirqs` 的变化。

### 第 5 周：同步与 RCU

**材料**：LKD 第 9、10 章；Paul McKenney《Is Parallel Programming Hard, And, If So, What Can You Do About It?》（免费，选读锁、RCU 和内存序几章）；LWN《What is RCU, Fundamentally?》。

- spinlock、mutex、rwsem、seqlock 各适合什么场景？PREEMPT_RT 下 spinlock 有什么变化？
- RCU 的 grace period 是什么？为什么读侧几乎没有开销？
- 什么情况下需要内存屏障？`READ_ONCE`/`WRITE_ONCE` 解决什么问题？

**观察**：用 `perf lock contention` 观察一个高并发负载下的锁竞争。

### 第 6 周：VFS

**材料**：LKD 第 13 章；`Documentation/filesystems/vfs.rst` 和 `path-lookup.rst`。

- inode、dentry、file、super_block 之间是什么关系？
- 路径查找的 RCU-walk 和 ref-walk 有什么区别？
- `open()` 从路径到返回 fd，大致经过哪些步骤？

**观察**：用 bcc 的 `dcstat` 和 `opensnoop` 观察 dentry cache 命中率和文件打开情况。

### 第 7 周：page cache 与块层

**材料**：LKD 第 14、16 章；LWN《The multiqueue block layer》；`Documentation/block/blk-mq.rst`。

- 一次 `read()` 在 page cache 命中和不命中时分别怎么走？预读如何触发？
- 脏页什么时候、由谁写回？
- blk-mq 的软件队列和硬件队列是什么关系？I/O 调度器在哪一层？

**观察**：用 `null_blk` 加 fio 制造负载，用 `biolatency`、`biosnoop`、`cachestat` 观察。

### 第 8 周：网络栈

**材料**：LKD 不覆盖这部分。读 packagecloud 博客《Monitoring and Tuning the Linux Networking Stack》收发两篇；`Documentation/networking/scaling.rst`。

- 一个包从网卡中断到 socket 经过哪些阶段（NAPI、GRO、协议栈）？
- `sk_buff` 为什么设计成 head、data、tail、end 四个指针？
- 发送路径上 qdisc 处在什么位置？

**观察**：用 netns 和 veth 搭一个两端环境，ping 的同时观察 `/proc/net/softnet_stat` 和网络 tracepoint。

### 第 9 周：设备模型与驱动

**材料**：LKD 第 17 章；《Linux Device Drivers》第三版第 14 章（较老但概念仍适用）；`Documentation/driver-api/driver-model/`。

- bus、device、driver 如何完成匹配和 probe？deferred probe 解决什么问题？
- 设备树中的节点如何变成内核里的 device？
- sysfs 的目录结构反映了什么？

**观察**：对照 `/proc/device-tree` 浏览 `/sys/bus` 和 `/sys/devices`；用 `udevadm monitor` 观察设备事件。

### 第 10 周：namespace 与 cgroup

**材料**：LWN《Namespaces in operation》系列（Michael Kerrisk）；`Documentation/admin-guide/cgroup-v2.rst`。

- `unshare` 和 `setns` 做了什么？各种 namespace 隔离的是什么？
- cgroup v2 和 v1 有什么不同？memory controller 如何把内存记账到 cgroup？

**观察**：不用 Docker，只用 `unshare` 和 cgroup v2 接口手工搭一个最小容器，限制它的内存和 CPU。

### 第 11 周：BPF 与可观测性

**材料**：Brendan Gregg《BPF Performance Tools》选读；`Documentation/bpf/`；ebpf.io。

- verifier 主要检查什么？为什么 BPF 程序能安全地运行在内核里？
- kprobe、tracepoint、fentry 有什么区别？

**观察**：前面各周用到的工具大多基于 BPF，这周回头挑一两个读懂它的实现原理。

### 收尾

选一个你觉得掌握最弱的子系统写一篇总结，假设读者是组里的新人。写不清楚的地方，就是需要回头补的地方。

## 计划二：IOMMU 深入（约 14 周）

分 7 个阶段，从 DMA API 一直推进到 iommufd 和 SVA。硬件以 ARM SMMUv3 为主线（spec 为 ARM IHI 0070），同时涉及 GICv3 ITS 以及设备树/IORT 绑定。你的 mm 背景在第 3、6、7 阶段最有用。

### 阶段 0：PCIe 与 DMA API（2 周）

- 在没有 IOMMU、强制 swiotlb、启用 SMMU 三种情况下，`dma_map_single` 分别做了什么？分派逻辑在哪里？
- coherent DMA 和 streaming DMA 有什么区别？为什么 streaming 需要 `dma_sync_*`？设备不是 cache coherent（设备树没有 `dma-coherent`）时，arm64 的 DMA API 额外做了哪些 cache 维护？
- 设备的 StreamID 从哪来？PCI 设备的 Requester ID 如何经设备树 `iommu-map` 或 ACPI IORT 映射成 StreamID？平台设备呢？
- ACS 是什么？它和 IOMMU group 的划分有什么关系？
- ATS、PRI、PASID 各自解决什么问题？PASID 在 SMMU 中对应什么（SubstreamID）？

**任务**：给 QEMU 的 `edu` 设备写一个 PCI 驱动，实现 MMIO、中断和 DMA，运行在 `-machine virt,iommu=smmuv3` 上。分别用 `iommu.passthrough=1`、`swiotlb=force` 和默认参数启动，用 ftrace 跟 `dma_map_page_attrs`，对比三条路径。

### 阶段 1：SMMUv3 spec 精读（2 到 3 周）

- 设备发起 DMA 后，SMMU 如何从 StreamID 找到 STE，再经 SubstreamID 找到 CD，最终找到页表？线性和二级 stream table 分别在什么情况下使用？
- STE 中 stage 1 和 stage 2 的配置分别在哪里？bypass 和 abort 状态如何表示？
- 软件如何通过命令队列下发配置失效（CFGI_*）和 TLB 失效（TLBI_*）？CMD_SYNC 怎样确认完成？
- 事件队列记录哪些 fault？常见事件（如 F_TRANSLATION、C_BAD_STE）分别说明什么问题？
- ARM 上 MSI 隔离由 GICv3 ITS 负责，而不是 IOMMU 的中断重映射。设备写 MSI doorbell 同样要经过 SMMU 翻译，Linux 如何在 domain 里为 doorbell 建立映射？

**任务**：QEMU 启动时加 `-trace 'smmuv3_*'`，观察 STE/CD 解析、命令处理和地址翻译的过程，与 spec 中的字段逐一对照。主线 arm-smmu-v3 驱动在表结构 dump 方面的支持比 Intel 少（以你的内核版本为准），所以 QEMU 侧的 trace 是主要观察手段。

### 阶段 2：IOMMU core 框架（2 周）

- `iommu_group` 是怎样形成的？PCI 设备和平台设备的分组方式有什么不同？为什么 group 是隔离的最小单位？
- 设备树的 `iommus`/`iommu-map` 或 ACPI IORT 怎样把设备关联到 SMMU 实例？`of_iommu_configure` 和 `iort_iommu_configure_id` 在什么时候执行？
- domain 有哪些类型？默认 domain 怎么选？`iommu.passthrough` 和 `iommu.strict` 启动参数影响的是什么？
- `probe_device` 什么时候被调用？设备 attach 到一个 domain 时，arm-smmu-v3 驱动如何构造并写入 STE？

**任务**：在 QEMU 中观察 `/sys/kernel/iommu_groups/`；用不同启动参数启动，确认默认 domain 类型的变化；在 attach 路径上加 tracepoint 或 printk，验证你的理解。

### 阶段 3：dma-iommu 与 IOVA（2 周）

- IOVA 分配器为什么要有 per-CPU 的 rcache？和 mm 中的 per-CPU 缓存相比有什么相似之处？
- strict 和 lazy（flush queue）两种失效模式各有什么取舍？lazy 模式有什么安全风险？
- 和 CPU 的 TLB shootdown 相比，IOTLB 失效有哪些不同的难点？

**任务**：用 `tools/testing/selftests/dma/` 下的 dma_map_benchmark，对比 strict 和 lazy 模式下的 map/unmap 延迟，并用 perf 找出热点。

### 阶段 4：IO 页表与硬件驱动（2 周）

- io-pgtable-arm 的页表格式和 CPU 的 AArch64 页表是什么关系？SMMU 为什么能直接沿用 VMSAv8 格式？
- map 一段 2MB 对齐的区域时，会不会用块映射？由谁决定（`pgsize_bitmap`）？
- unmap 时，页表页什么时候释放？`iommu_iotlb_gather` 如何批量失效？arm-smmu-v3 如何利用范围失效（RIL）减少命令数量？
- 多个 CPU 并发提交命令时，`arm_smmu_cmdq_issue_cmdlist` 是怎么工作的？为什么要设计成无锁？

**任务**：打开 `CONFIG_IOMMU_IO_PGTABLE_LPAE_SELFTEST` 跑 io-pgtable 自测；用 `iommu:map` 和 `iommu:unmap` tracepoint 观察一次大块 DMA 的映射粒度。

### 阶段 5：VFIO 与 iommufd（3 周）

- 为什么需要 iommufd 来替代 VFIO type1？
- IOAS、HWPT、device 三者是什么关系？
- 用户态内存被映射给设备时，内核如何 pin 住这些页（`pin_user_pages`）？这对 mm 有什么影响（例如和 THP、内存迁移、CMA 的冲突）？
- 嵌套翻译场景下，guest 的 stage 1（vSTE 和 CD）由谁管理？guest 下发的 TLBI 命令怎样传到物理 SMMU？iommufd 的 vIOMMU 对象解决了什么问题？

**任务**：

1. 跑 `tools/testing/selftests/iommu/` 中的 iommufd 测试，它带有 mock IOMMU 驱动，不需要真硬件；读测试代码，理解每个 ioctl 的用法。
2. 在 `-machine virt,iommu=smmuv3` 的 guest 里，把 `edu` 设备绑定到 vfio-pci，写一个用户态程序，用 iommufd 接口映射一段内存并让设备 DMA 进去。
3. 故意让设备访问一个未映射的 IOVA，观察 arm-smmu-v3 打印的事件日志，并在驱动的事件队列处理代码中找到这条日志的来源。

### 阶段 6：SVA 与 IO 缺页（2 周）

- SVA 如何让设备直接使用进程的页表？SMMU 的 CD 如何与 CPU 共享页表基址和 ASID？
- 进程地址空间变化时（munmap、页迁移、THP 拆分），SMMU 如何得到通知？（提示：看 mmu_notifier 和 `arch_invalidate_secondary_tlbs`）SMMU 的 BTM（广播 TLB 维护）特性在这里起什么作用？
- PCIe 设备通过 PRI 发起缺页，而 ARM 平台设备通常使用 stall 模型。两者在 SMMU 和内核中的处理有什么区别？和 CPU 缺页处理相比又有什么不同？

**任务**：精读 `iommu-sva.c` 和 `io-pgfault.c`，画出一次设备缺页从上报到完成的完整流程图；查看 iommufd selftest 中是否已有 IO 缺页相关的测试，如果有就跑通它。

### 收尾

订阅 `iommu@lists.linux.dev`，持续跟进补丁。从 review 小补丁开始，再尝试修复一个小问题或补一个测试用例。

## 实验环境与验收

两份计划共用一套 QEMU 环境，开始前花一两天搭好，后面会省很多时间。

- **内核**：用最新的 stable 或 mainline 编 arm64 内核，打开 `DEBUG_INFO`、`LOCKDEP`、`KASAN`、`FTRACE`、`BPF`，以及 `ARM_SMMU_V3`、`ARM_SMMU_V3_SVA`、`IOMMUFD`、`VFIO_PCI`。
- **主环境（arm64）**：`qemu-system-aarch64 -machine virt,gic-version=3,iommu=smmuv3 -cpu max -device edu`
- **观察 SMMU 行为**：QEMU 加 `-trace 'smmuv3_*'`，可以看到 STE/CD 解析、命令和翻译过程。
- **调试**：QEMU 加 `-s -S` 配合 gdb；需要嵌套场景时，在 guest 里再起一层 QEMU。
- **可选对照**：x86 加 `-device intel-iommu`。读完 SMMU 后对比一下 VT-d 的实现，有助于分清哪些是通用设计、哪些是架构特有。

### 验收方式

每个单元结束时，把这些发给我：

1. 你对该单元引导问题的书面回答
2. 动手任务的关键结果，比如 trace 输出或遇到的问题
3. 复盘的 Fixes 提交和你的分析

我会指出理解偏差、追问细节，再出一套新题。也可以直接说"出题"，我给你出一组选择题加场景分析题。
