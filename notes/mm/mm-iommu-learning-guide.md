# Linux MM & IOMMU 学习指南

> 目标：2-3 个月系统掌握 Linux 内存管理子系统，为后续 GPU/infra 打下基础。
> 内核版本：Linux 7.1-rc1（当前工作树）

主线：跟着一条请求走，每一步因为前一步卡住了才引入新概念。每个阶段由一个具体问题驱动，不是"先学 X 再学 Y"。

每个阶段的阅读节奏：

1. 读文件顶部注释和 `Documentation/`，理解设计意图和 tradeoff
2. 理解核心结构体为什么长这样，它抽象了什么
3. 跟踪一条最简单场景的完整路径，只走主干不进分支，遇到下一阶段的概念先标记"这里以后打开"
4. 写程序触发对应场景，用观测工具验证

---

## 阶段一：malloc 之后发生了什么？

**驱动问题：** 用户态 `malloc(4096)` 返回了指针，但物理内存还没分配 — 谁在记这笔账？

**核心认知：** 虚拟地址是承诺，不是实物。内核用 VMA 描述"承诺了什么"，用页表描述"兑现了什么"。

**阶段输出：** 能画出一个进程完整的地址空间布局（代码段、堆、栈、mmap 区），能读懂 `/proc/self/maps`。

### 核心结构体

| 结构体 | 文件 | 它抽象了什么 |
|--------|------|-------------|
| `mm_struct` | `include/linux/mm_types.h` | 一个进程的完整地址空间 |
| `vm_area_struct` | `include/linux/mm_types.h` | 一段连续虚拟地址范围的属性（权限、映射方式） |
| `vm_operations_struct` | `include/linux/mm.h` | VMA 的行为接口（fault、open、close） |

重点理解：
- `mm_struct` 中 `mmap`（VMA 链表）、`mm_mt`（VMA maple tree）为什么同时维护两种数据结构 — 链表按地址顺序遍历，maple tree 按区间快速查找
- `vm_area_struct` 中 `vm_start`/`vm_end`/`vm_flags`/`vm_file` 各自的含义
- VMA 的合并规则（`vma_merge()`）

### 主干路径：mmap 系统调用创建 VMA

```
sys_mmap()
  (arch/x86/kernel/sys_x86_64.c 或类似)
  → vm_mmap_pgoff()
    → do_mmap()
      → mmap_region()
        → vma_merge()        ← 能和相邻 VMA 合并吗？
        → vm_area_alloc()    ← 分配新 VMA
        → vma_link()         ← 插入链表和红黑树
        → vm_ops->open()     ← 调用 VMA 的 open 回调
```

观测：运行任意程序，`cat /proc/self/maps`，对照代码理解每一行的含义。

### 推荐阅读

按顺序读：

1. **建立概念层**
   - Ulrich Drepper, [What Every Programmer Should Know About Memory](https://people.freebsd.org/~lstewart/articles/cpumemory.pdf) — 只读 Section 4（Virtual Memory）
   - wowotech, [Linux kernel内存管理的基本概念](http://www.wowotech.net/memory_management/concept.html) — 中文大图
   - LWN, [Memory part 3: Virtual Memory](https://lwn.net/Articles/253761/)

2. **VMA 结构本身**
   - LWN, [Not-so-anonymous virtual memory areas](https://lwn.net/Articles/867818/) — VMA 的 anonymous/file-backed 区分
   - Mel Gorman, [Understanding the Linux Virtual Memory Manager](https://www.kernel.org/doc/gorman/pdf/understand.pdf) — 只读 Chapter 4（Process Address Space），内核版本老，概念层仍然准确

3. **6.6 里的实际数据结构**
   - LWN, [Introducing the Maple Tree](https://lwn.net/Articles/892724/) — 为什么换掉红黑树，读完再看 `mm_mt` 有背景

4. **理解 process_addrs.rst 的锁模型**
   - LWN, [How to get rid of mmap_sem](https://lwn.net/Articles/787629/) — mmap_lock 为什么是瓶颈
   - LWN, [Concurrent page-fault handling with per-VMA locks](https://lwn.net/Articles/906852/) — per-VMA lock 的设计，对应 6.6 的锁模型

> LWN 文章发布一周后对公众免费开放。

---

## 阶段二：page fault — 兑现承诺的时刻

**驱动问题：** CPU 访问那个 malloc 来的地址，硬件和内核各做了什么？

**核心认知：** MMU 是硬件，页表是数据结构，page fault handler 是内核的兑现逻辑。三者协作完成"按需分配"。

**阶段输出：** 能完整描述从 CPU 发出访存到内核分配页面的全过程。

**GPU/IOMMU 锚点：** IOMMU 的页表结构和 CPU MMU 同构 — 同样是多级页表，同样有 IOVA（等价于虚拟地址），同样有 page fault（IOPF）。这里学明白，后面 IOMMU 直接迁移认知。

### 核心结构体

| 结构体/概念 | 文件 | 它抽象了什么 |
|------------|------|-------------|
| 页表项 `pte_t` | `arch/x86/include/asm/pgtable_types.h` | 一个虚拟页到物理页的映射关系 |
| `pgd_t` / `pud_t` / `pmd_t` | 同上 | 多级页表的各级目录项 |
| `vm_fault` | `include/linux/mm.h` | page fault 事件的所有上下文信息 |

重点理解：
- 为什么是四级页表（x86-64）而不是一级大数组 — 答案是稀疏地址空间省内存
- `pte` 中的权限位（R/W、U/S、NX）如何和 VMA 的 `vm_flags` 对应
- `vm_fault` 结构体把 fault 处理需要的所有信息打包在一起

### 主干路径：anonymous page 的首次写

这是最简单的 page fault 场景，只跟踪这一条：

```
CPU 访问未映射的虚拟地址
  → MMU 硬件查页表，发现 PTE 不存在
  → 触发异常，进入内核
  → do_page_fault()                     (arch/x86/mm/fault.c)
    → handle_mm_fault()                  (mm/memory.c)
      → __handle_mm_fault()
        → pud_alloc() / pmd_alloc()      ← 按需分配页表中间层级
        → handle_pte_fault()
          → pte_none() == true           ← PTE 全空，首次访问
          → do_anonymous_page()
            → alloc_pages()              ← 【边界】这里去拿物理页，阶段三再打开
            → set_pte_at()               ← 在页表中填入映射
  → 返回用户态，重新执行那条指令
```

同时理解 copy-on-write 路径（`fork` 后写共享页），它是 page fault 的另一条主线：

```
do_page_fault()
  → handle_pte_fault()
    → pte_present() == true && pte_write() == false  ← 页存在但只读
    → do_wp_fault()
      → do_cow_fault()                   ← 分配新页，复制内容，修改 PTE 为可写
```

观测：`perf record -e faults ./your_program`，观察 page fault 分布。

---

## 阶段三：物理页从哪来？

**驱动问题：** `do_anonymous_page()` 要调用 `alloc_pages()` 拿一个物理页 — 内核怎么管理物理内存？

**核心认知：** 物理内存的管理单位是阶（order），不是字节。Buddy 管大块，Slab 管小块，两层协作。

**阶段输出：** 理解 `alloc_pages()` 从 free list 取页到返回给调用者的完整过程。

**GPU/IOMMU 锚点：** GPU 驱动经常需要 contiguous 物理内存，Buddy 的阶分配直接决定 GPU 能拿到多大的连续物理块。`MIGRATE_MOVABLE` 类型是后面 `migrate_pages()` 的基础。

### 核心结构体

| 结构体 | 文件 | 它抽象了什么 |
|--------|------|-------------|
| `page` | `include/linux/mm_types.h` | 一个物理页的元数据（mm 中最重要的结构体） |
| `zone` | `include/linux/mmzone.h` | 一个物理内存区域（DMA、DMA32、NORMAL、MOVABLE 等） |
| `free_area` | `include/linux/mmzone.h` | Buddy allocator 的一个阶（order）的 free list |
| `pglist_data` (`pg_data_t`) | `include/linux/mmzone.h` | 一个 NUMA node 的所有内存 |
| `kmem_cache` | `mm/slab.h` (内部) / `include/linux/slub_def.h` | Slab allocator 中一种大小的对象缓存 |

重点理解：
- `page` 结构体的 union 设计 — 同一个结构体在不同状态下复用字段（LRU、mapping、slab 等），这是理解 mm 的关键
  > **[Linux ≥5.16]** 引入 `struct folio`，代表一个或多个连续物理页的逻辑单元。page cache、回收路径已大量用 folio 替换 page。学习时先以 `struct page` 建立认知，遇到 folio 可理解为"一个或多个 page 的包装，接口一致"。
- `zone` 的存在意义 — 硬件寻址限制（DMA zone）和内存可移动性（MOVABLE zone）是两套不同约束
- `free_area.free_list[MIGRATE_TYPE]` — buddy free list 按 migrate type 分组，直接影响后续迁移和回收
- Buddy 的 order 含义 — free_area[0] 是单页，free_area[1] 是 2 页连续块，以此类推

### 主干路径：alloc_pages 分配一个物理页

```
alloc_pages(gfp_mask, order)
  → __alloc_pages(gfp_mask, order, preferred_nid, nodemask)
    → get_page_from_freelist()
      → zone_watermark_ok()              ← 水位检查，当前 zone 够不够
      → rmqueue()
        → __rmqueue()                    ← 从 buddy free list 中摘取
          → 如果当前 order 不够，从更高 order 拆分（buddy split）
    → 如果 fast path 失败：
      → __alloc_pages_slowpath()
        → wake up kswapd                 ← 【边界】唤醒回收，阶段五再打开
        → direct reclaim                 ← 【边界】同步回收，阶段五再打开
        → 直接 compaction                ← 【边界】阶段六再打开
```

### 主干路径：kmalloc 分配小块内存

```
kmalloc(size, flags)
  → kmalloc_trace() 或直接调用 slab 分配器
    → slab_alloc_node()
      → 从 per-CPU freelist 取对象
        → 如果 per-CPU list 空，从 partial slab 补充
          → 如果 partial 也空，从 buddy 分配新 slab 页
```

观测：`cat /proc/buddyinfo` 看 buddy 各阶的 free 页数；`cat /proc/slabinfo` 看 slab 缓存状态。

---

## 阶段四：file-backed 页和 page cache

**驱动问题：** `mmap` 一个文件和 `malloc` 有什么区别？读文件时，数据缓存在哪？

**核心认知：** page cache 是文件系统和内存管理的交汇点。`struct page` 既是物理页的描述符，也是 cache 的载体。

**阶段输出：** 理解 `read()`/`mmap()` 文件时 page cache 的命中和未命中路径。

**GPU/IOMMU 锚点：** GPU 通过 `dma_buf` 共享内存时，底层就是在 page cache 中找到对应的 page 然后映射给设备。理解 address_space 和 page cache 是理解 `dma_buf` 的前提。

### 核心结构体

| 结构体 | 文件 | 它抽象了什么 |
|--------|------|-------------|
| `address_space` | `include/linux/fs.h` | 一个文件的 page cache 索引（host → inode） |
| `address_space_operations` | `include/linux/fs.h` | page cache 的行为接口（readpage、writepage、dirty 等） |

重点理解：
- `address_space.host` 指向 inode，`page.mapping` 指向 address_space — 这是 page 和文件的关联
- `page.mapping` 的最低位用来区分 anonymous page 和 file page（`PAGE_MAPPING_ANON`）
- `address_space.i_pages`（xarray）是 page cache 的核心索引结构：文件偏移 → page

### 主干路径：mmap 一个文件后首次访问

这里有两段独立的执行流，不要混在一起：

**① mmap 系统调用：建立 VMA，注册 fault handler**

```
mmap(MAP_SHARED, fd)
  → mmap_region()
    → call_mmap()                        ← 调用文件系统的 f_op->mmap()
      → filemap_mmap()（或文件系统自己的实现）
        → vma->vm_ops = &generic_file_vm_ops  ← 注册 .fault = filemap_fault
    ← 返回，此时尚未读任何数据，物理页也未分配
```

**② CPU 首次访问该地址：触发 page fault**

```
CPU 访问 mmap 区域 → MMU 发现 PTE 不存在
  → do_page_fault() → handle_mm_fault()
    → vm_ops->fault()                    ← 调用上面注册的 filemap_fault
      → filemap_fault()
        → page_cache_get_page()          ← 在 xarray 中查找 page cache
          → 如果命中：直接返回 page
          → 如果未命中：
            → page_cache_alloc()         ← 分配新 page
            → add_to_page_cache_lru()    ← 加入 page cache 并挂 LRU
            → mapping->a_ops->readpage() ← 从磁盘读入
        → 返回 page，建立 PTE 映射
```

> **[Linux ≥5.16]** `page_cache_get_page()` 已被 `filemap_get_folio()` 替代；`readpage()` 被 `read_folio()` 替代。

对比阶段二：anonymous page fault → `do_anonymous_page()` 直接分配；file page fault → `filemap_fault()` 先查 cache。

### 主干路径：read() 系统调用

```
sys_read()
  → vfs_read()
    → file->f_op->read_iter()
      → generic_file_buffered_read()     ← 概念路径；实际函数名见注
        → page_cache_get_page()          ← 同样的 page cache 查找
        → copy_page_to_iter()            ← 从 page 拷贝到用户 buffer
```

> **[Linux ≥5.18]** `generic_file_buffered_read()` 改名为 `filemap_read()`；`page_cache_get_page()` 替换为 `filemap_get_folio()`。

观测：`cat /proc/meminfo` 中 `Cached` 字段；`cat /proc/vmstat | grep pgpgin` 观察 page cache 读入量。

---

## 阶段五：内存不够了怎么办？

**驱动问题：** `alloc_pages()` 发现 free list 空了 — 系统怎么腾出空间？

**核心认知：** 内存不是消耗品，是可回收资源。回收的核心问题是：扔谁？扔了以后怎么找回来？

**阶段输出：** 能描述 kswapd 的完整工作流程，理解 LRU 链表驱动的页面淘汰策略。

**GPU/IOMMU 锚点：** GPU pin 住的页不能被回收，和 kswapd 的目标直接冲突。理解回收机制才能理解为什么 GPU 驱动需要 `migrate_pages()` 把可移动页挪走。

### 核心结构体

| 结构体/概念 | 文件 | 它抽象了什么 |
|------------|------|-------------|
| `lruvec` | `include/linux/mmzone.h` | 一组 LRU 链表（anon/file × active/inactive） |
| `scan_control` | `mm/vmscan.c`（内部） | 一次回收扫描的所有控制参数 |
| `reclaim_state` | `include/linux/swap.h` | 回收过程的统计信息 |

重点理解：
- LRU 的四个链表：anon active、anon inactive、file active、file inactive — 为什么要分 anon/file？因为 file page 可以直接丢弃（磁盘有副本），anon page 必须 swap out
- `PG_referenced` 和 PTE accessed bit — 硬件自动设置 accessed bit，内核定期采样它来维护 LRU 的排序
- 水位线：`min` / `low` / `high` — kswapd 在 low 唤醒，在 high 停止，min 是 direct reclaim 的触发点

### 主干路径：kswapd 回收一个 page

```
kswapd 内核线程被唤醒
  → balance_pgdat()
    → kswapd_shrink_node()
      → shrink_node()
        → shrink_lruvec()
          → shrink_active_list()         ← 从 active 移到 inactive（衰老）
          → shrink_inactive_list()       ← 从 inactive 中淘汰
            → shrink_folio_list()
              → folio_referenced()       ← 检查最近是否被访问
              → page_referenced()        ← 扫描反向映射查 PTE accessed bit
              → try_to_unmap()           ← 【边界】解除所有 PTE 映射，见下方
              → 如果是 file page：移除 page cache
              → 如果是 anon page：swap_writepage() → 写入 swap
              → free_unref_page()        ← 释放回 buddy
```

### 主干路径：反向映射（从 page 找到所有 PTE）

```
try_to_unmap()
  → rmap_walk()
    → 遍历 page 的 mapping 信息
    → 对每个映射了该 page 的进程：
      → page_vma_mapped_walk()           ← 遍历该 VMA 的页表
      → ptep_clear_flush()               ← 清除 PTE
      → TLB flush                        ← 刷新 TLB
```

### 主干路径：OOM killer（最后手段）

```
out_of_memory()
  → oom_kill_process()
    → 选择进程（oom_score_adj 为依据）
    → 发送 SIGKILL
```

观测：`cat /proc/zoneinfo` 看水位线；`tracepoint vmscan:mm_vmscan_direct_reclaim_begin` 观察 direct reclaim。

---

## 阶段六：高级机制 — compaction、migration、huge page

**驱动问题：** 回收之后 free 页很多，但全是碎片，拿不出大块连续内存 — 怎么办？

**核心认知：** 内存管理的终极矛盾是外部碎片。Compaction 和 migration 是应对这个矛盾的主动策略。

**阶段输出：** 理解 migrate type 如何贯穿分配、回收、迁移三个阶段。

**GPU/IOMMU 锚点：** `migrate_pages()` 是 GPU 驱动处理 longterm pin 冲突的核心机制。GPU pin 一个 `MIGRATE_MOVABLE` 页时，内核先迁移它到别处，腾出位置。这里的代码直接对应生产环境的 GPU 内存管理。

### 核心结构体

| 结构体/概念 | 文件 | 它抽象了什么 |
|------------|------|-------------|
| `compact_control` | `mm/compaction.c`（内部） | 一次 compaction 的控制参数 |
| `migration_target_control` | `mm/migrate.h`（内部） | 页迁移目标的控制参数 |
| `hstate` | `include/linux/hugetlb.h` | 一种 size 的 huge page 的全局状态 |

### 主干路径：内存规整（compaction）

```
compact_zone()
  → isolate_migratepages()               ← migrate scanner：从 zone 一端扫描可移动页
    → 沿页块（pageblock）扫描，跳过不可移动页
  → isolate_freepages()                  ← free scanner：从 zone 另一端扫描空闲页
  → migrate_pages()                      ← 把扫描到的可移动页迁到空闲位置
    → 对每个页：
      → move_to_new_page()
        → try_to_unmap()                 ← 解除旧映射
        → copy_page()                    ← 复制内容
        → set_pte_at()                   ← 建立新映射
```

两个 scanner 从 zone 两端向中间夹击，把可移动页集中到一端，腾出连续空闲块。

### 主干路径：页迁移（migrate_pages）

```
migrate_pages(from, get_new_page, ...)
  → 对 from 列表中的每个页：
    → unmap_and_move()
      → try_to_unmap()                   ← 解除所有 PTE 映射
      → move_to_new_page()
        → 分配目标页（从 free list）
        → 复制内容
        → 建立 PTE 映射（rmap）
        → 释放源页
```

### 主干路径：Transparent Huge Page（THP）合并

```
khugepaged 内核线程
  → khugepaged_scan_mm_slot()
    → khugepaged_scan_pmd()
      → 检查进程的一个 PMD 是否被拆成多个小页
      → 如果满足条件：
        → collapse_pte_mapped_thp()
          → 收集所有小页
          → alloc_transhuge_page()       ← 分配一个 2MB 大页
          → 复制内容
          → 替换 PMD 条目
```

观测：`cat /proc/vmstat | grep compact`；`cat /sys/kernel/mm/transparent_hugepage/enabled`。

---

## 阶段七：IOMMU — 给设备用的 MMU

**驱动问题：** CPU 通过页表访问内存，设备（网卡、GPU）做 DMA 时也有一套独立的地址翻译 — 这套机制是什么？

**核心认知：** IOMMU 就是给设备用的 MMU。核心模型和 CPU MMU 同构：IOVA = 设备侧虚拟地址，IOMMU 页表 = 翻译结构，DMA = 设备的内存访问。理解 CPU MMU 后，IOMMU 是认知的平移。

**阶段输出：** 能描述设备 DMA 时地址翻译的完整过程，理解 IOMMU 和 CPU MMU 的异同。

### 核心结构体

| 结构体 | 文件 | 它抽象了什么 |
|--------|------|-------------|
| `iommu_domain` | `include/linux/iommu.h` | 一个 IOMMU 翻译域（等价于 CPU 侧的 `mm_struct`） |
| `iommu_ops` | `include/linux/iommu.h` | IOMMU 硬件的操作接口 |
| `iommu_fwspec` | `include/linux/iommu.h` | 设备的 IOMMU 固件描述（ACPI/DT 信息） |
| `dma_attrs` | `include/linux/dma-mapping.h` | DMA 映射的属性控制 |

重点理解：
- `iommu_domain` vs `mm_struct` — 一个 domain 管理一组设备的地址翻译，等价于一个进程的地址空间
- IOVA 空间 — 设备看到的虚拟地址，由 `iova_allocator` 管理（等价于用户态的虚拟地址分配）
- IOMMU 页表格式 — Intel 的多级页表和 CPU 的四级页表结构类似，但格式不同

### 主干路径：设备通过 IOMMU 做 DMA

```
驱动调用 dma_map_page(dev, page, offset, size, direction)
  → dma_map_page_attrs()
    → 如果设备背后有 IOMMU：
      → iommu_dma_map_page()
        → iommu_dma_alloc_iova()         ← 分配一个 IOVA（设备侧虚拟地址）
        → iommu_map()                    ← 在 IOMMU 页表中建立 IOVA → PA 映射
          → domain->ops->map()           ← 硬件特定的页表操作
            → 分配页表页（用 buddy allocator）
            → 填写页表项
        → 返回 IOVA 给驱动
    → 设备使用 IOVA 做 DMA
      → 设备发出 IOVA
      → IOMMU 硬件查页表，翻译为物理地址
      → 访问物理内存
```

### 主干路径：dma_buf 共享内存（GPU 场景的基础）

```
生产者（exporter）：
  dma_buf_export(ops, size, flags)
    → 创建 dma_buf 和 dma_buf_file
    → 返回 fd

消费者（importer）：
  dma_buf_get(fd)
  → dma_buf_attach()
    → 调用 exporter 的 attach 回调
  → dma_buf_map_attachment()
    → 调用 exporter 的 map_dma_buf 回调
    → 返回 sg_table（散列表，描述物理页布局）
    → 如果需要 IOMMU：为 importer 设备建立 IOVA 映射
```

### 和前面阶段的呼应

| IOMMU 概念 | 对应的 CPU MMU 概念 | 所在阶段 |
|-----------|-------------------|---------|
| IOMMU 页表 | `pgd/pud/pmd/pte` | 阶段二 |
| `iommu_domain` | `mm_struct` | 阶段一 |
| IOVA | 虚拟地址 | 阶段一 |
| IOMMU 页表页分配 | `alloc_pages()` | 阶段三 |
| DMA pin 住页面 | 页面被映射不能回收 | 阶段五 |
| `dma_buf` 共享 page cache 页 | `address_space` | 阶段四 |
| `migrate_pages()` 为 DMA 腾位 | `MIGRATE_MOVABLE` | 阶段六 |

---

## 学习节奏

| 时间 | 阶段 | 重点 | 核心文件 |
|------|------|------|---------|
| 第 1-2 周 | 一 + 二 | 虚拟内存 + page fault | `mm/mmap.c`, `mm/memory.c` |
| 第 3-4 周 | 三 | 物理内存管理，`struct page` 吃透 | `mm/page_alloc.c`, `mm/slub.c` |
| 第 5-6 周 | 四 | page cache，连接文件系统和 mm | `mm/filemap.c` |
| 第 7-8 周 | 五 | 回收，kswapd 完整流程 | `mm/vmscan.c`, `mm/rmap.c` |
| 第 9-10 周 | 六 | compaction/migration/THP | `mm/compaction.c`, `mm/migrate.c` |
| 第 11-12 周 | 七 | IOMMU，认知从 CPU 侧迁移到设备侧 | `drivers/iommu/iommu.c`, `kernel/dma/` |

---

## 观测工具速查

| 工具 | 用途 | 对应阶段 |
|------|------|---------|
| `/proc/pid/maps` | 查看进程 VMA 布局 | 一 |
| `/proc/pid/smaps` | VMA 详细信息（RSS、PSS） | 一 |
| `pmap -x <pid>` | 进程内存映射 | 一 |
| `perf record -e faults` | 观察 page fault | 二 |
| `/proc/buddyinfo` | buddy 各阶 free 页数 | 三 |
| `/proc/slabinfo` | slab 缓存状态 | 三 |
| `/proc/meminfo` | 系统级内存统计（Cached、Buffers） | 四 |
| `cat /proc/vmstat \| grep pgpgin` | page cache 读入量 | 四 |
| `/proc/zoneinfo` | zone 水位线 | 五 |
| `/proc/vmstat` | pgscan/kswapd 等回收指标 | 五 |
| `tracepoint vmscan:*` | kswapd 和 direct reclaim 事件 | 五 |
| `/proc/vmstat | grep compact` | compaction 统计 | 六 |
| `/sys/kernel/debug/tracing/events/migrate/*` | 页迁移事件 | 六 |
| `/sys/kernel/iommu_groups/` | IOMMU group 状态 | 七 |
| `/sys/kernel/debug/dma_buf/bufinfo` | dma_buf 信息 | 七 |
