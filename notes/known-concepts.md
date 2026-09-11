# 已掌握概念索引

> 讲解新内容时，下列概念默认用户已理解，跳过详细解释或一句话带过。

## MM 基础概念（concepts.rst）

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

## GPU / AMD

- **`struct kfd_process` / amdkfd 驱动** — KFD（Kernel Fusion Driver）侧的计算进程抽象，管理队列、优先级、eviction fence。对应 `/dev/kfd` 设备。
- **`struct amdgpu_vm` / amdgpu 驱动** — GPU 虚拟地址空间（GPU 页表），管理 GPU 虚拟地址到物理 VRAM 的映射。类比 CPU 的 `struct mm_struct`。对应 `/dev/dri/renderD*` 设备。
- **`struct amdkfd_process_info`** — KFD 和 amdgpu 之间的桥梁对象，协调跨驱动的显存 eviction（腾退/恢复）。
- **`kfd_ioctl_acquire_vm`** — 将一个 `amdgpu_vm` 绑定到 `kfd_process` 的 ioctl，建立两者之间的桥梁。
- **`kfd_ioctl_create_process`** — 在同一 FD 上创建 secondary `kfd_process` 的 ioctl，用于同一进程内的多计算上下文隔离。
- **`file->private_data` 模式** — Linux 驱动通过 `struct file` 的 `private_data` 字段将驱动资源绑定到文件描述符的标准做法。
- **VFS 资源管理** — 借 open/close/fork 的文件生命周期管理驱动资源，免费获得自动清理、引用计数、权限检查。

### 关系与不变量

- **1:N 关系**：一个 `kfd_process` 可关联多个 `amdgpu_vm`（多卡场景），但一个 `amdgpu_vm` 应只被一个 `kfd_process` 管理。
- **secondary kfd_process 的动机**：同一用户态进程内创建多个隔离的计算上下文（独立队列、eviction fence、优先级），代价是引入并发管理复杂度。

## Locking / rtmutex / futex

### 核心问题

- **优先级反转 (Priority Inversion)** — 高优先级任务被低优先级任务间接阻塞。经典场景：低优先级持锁 → 高优先级等锁被阻塞 → 中优先级抢占低优先级 → 高优先级被中优先级间接卡住。
- **优先级继承 (Priority Inheritance, PI)** — 解决反转的机制：临时提升持锁低优先级任务的优先级到等待者同等水平，让其尽快释放锁。rtmutex 就是带 PI 的互斥锁。

### rtmutex 数据结构

- **`struct rt_mutex_base`** — rtmutex 核心结构，维护 `wait_list`（按优先级排序的等待队列）和 `owner`（`struct task_struct *`，当前持锁者）。
- **`struct rt_mutex_waiter`** — 抽象一次等锁事件。`waiter->task`（`struct rt_mutex_waiter` 的字段）指向等待者。挂到 `struct rt_mutex_base` 的 `wait_list` 上。
- **`pi_blocked_on`**（`struct task_struct` 的字段，类型 `struct rt_mutex_waiter *`）— 指向当前阻塞这个任务的 waiter，回答"这个任务在等啥"。为 NULL 表示未被任何 rtmutex 阻塞。
- **`pi_lock`**（`struct task_struct` 的字段，类型 `raw_spinlock_t`）— 保护该任务的 PI 相关状态（`pi_blocked_on` 等）。

### 锁设计

- **per-object 锁 + 固定加锁顺序** — rtmutex 不用全局锁，而是每个对象各持一把锁（`struct rt_mutex_base` 的 `wait_lock` 和 `struct task_struct` 的 `pi_lock`）。操作跨对象状态时按固定顺序同时持有：先 `wait_lock` 再 `pi_lock`。不同 CPU 上操作不同锁链的代码可并行，避免全局锁瓶颈。

### futex

- **futex (Fast Userspace Mutex)** — 用户态锁的内核支持机制。无竞争时纯用户态原子操作（不进内核），只有竞争时才通过系统调用进内核排队睡眠 / 唤醒。
- **`futex_requeue`** — 一个系统调用，把等在 futex A 上的任务直接搬到 futex B 的等待队列上，不唤醒。省掉 N 次无意义的唤醒-重阻塞（内核态↔用户态切换）。
- **proxy lock（代理加锁）** — `futex_requeue` 搬运任务时，需要让睡眠中的任务在新 rtmutex 上排队（保证 PI 链正确）。但任务在睡眠，不能自己操作，所以由调用 requeue 的任务（`current`）代替它完成入队。此时 `waiter->task`（被搬运的任务）≠ `current`（执行搬运的任务）。

## SLUB（v6.6 经典架构）

- **分层结构** — `kmem_cache`（一类对象的配置，本身不存对象）→ `kmem_cache_cpu`（per-CPU 分配状态，无锁快路径）→ `kmem_cache_node.partial`（per-node 共享层，`list_lock`）→ buddy。一张 slab 任意时刻只处于三种位置之一：某 CPU 的活跃 slab（frozen）、挂在某条 partial 链表上、全满不在任何链表上。
- **两条 freelist** — `cpu_slab->freelist` 是本 CPU 私有链；`slab->freelist` 收其他 CPU 释放回来的对象。frozen=1 表示该 slab 是某 CPU 的活跃 slab；deactivate 时两链合并。
- **快路径并发设计** — 不禁抢占；`this_cpu_cmpxchg` 同时校验 freelist+tid（tid 防 ABA、检测抢占/中断/迁移干扰），失败重试。不变量：只有本 CPU 上执行的代码会碰本 CPU 的 `cpu_slab->freelist`。PREEMPT_RT 下退化为 local_lock。
- **`struct slab`** — 复用首页 page 描述符，零额外内存；freelist 与 inuse/objects/frozen 打包成单字，供其他 CPU 释放时双字 cmpxchg；满的 slab 不在任何链表上。
- **布局围绕原子操作单元** — freelist+tid、freelist+counters、oo(order+objects) 三处字段打包。
- **释放路径分流** — 判据只有一个：所属 slab 是否本 CPU 活跃 slab。是 → push `c->freelist`（与分配 pop 对称）；否 → `__slab_free` 挂 `slab->freelist`，按 was_frozen / 释放前全满 / 释放后全空三分支处理。cpu partial 唯一入口在释放路径（`put_cpu_partial`），出口在分配路径。

详见 [mm/slub/slub.md](mm/slub/slub.md)（含分配/释放全流程与配套图）。

## shmem（v6.6）

- **定位** — 无后备设备的文件系统，给"可共享的匿名内存"一个 inode 身份。tmpfs、/dev/shm、SysV shm、memfd、`MAP_SHARED|ANONYMOUS` 全是它。
- **设计本质：page cache 反转** — XArray 里是真身不是缓存，无磁盘可写回；回收时换出到 swap，swap 扮演普通文件块设备的角色。
- **三态槽位** — `address_space->i_pages` 的槽位：folio 指针（在内存）/ swap entry（已换出，`xa_mk_value` 编码）/ 空（空洞）。所有取页路径收敛到 `shmem_get_folio_gfp`，按 `sgp_type` 意图决定空洞是否分配。
- **两张世界** — 进程侧（VMA/PTE）与文件侧（inode/XArray）靠 `vm_fault` 工单交接：shmem 填 `vmf->page`，通用层 `finish_fault` 填 PTE。PTE 与 XArray 槽位是指向同一物理页的两条独立引用。
- **`__folio_set_swapbacked`** — shmem 页挂 anon LRU、回收走 swap 的判据，page cache 反转落到代码就是这一个标志。
- **三态转换** — 空洞→指针（fault 分配）、指针→swap entry（`shmem_writepage` 换出）、swap entry→指针（`shmem_swapin_folio` 换入，先查 swap cache，读盘记 major fault）。
- **限额与故障语义** — `max_blocks`/`used_blocks` 是 tmpfs `size=` / docker `--shm-size` 落点；超限 `-ENOSPC` → SIGBUS（容器里 DataLoader 的 `Bus error`）；无 swap 时 shmem 页钉死不可回收。

详见 [mm/shmem/shmem.md](mm/shmem/shmem.md)。
