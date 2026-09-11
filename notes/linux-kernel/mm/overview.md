# Linux MM 学习地图

---

## 子系统全貌

```
用户进程
    │  malloc / mmap / 栈增长
    ▼
虚拟内存层
    VMA 管理（Maple Tree）         ← 虚拟地址空间的"地图"
    页表（PGD/PUD/PMD/PTE）        ← 硬件 MMU 实际查的虚拟→物理映射
    Page Fault 处理                ← 懒分配的实现核心，fault 时补建页表项
    │
    │  缺页时才真正分配物理内存
    ▼
物理内存层
    Buddy System                   ← 物理页的分配与回收（粒度：页）
    Slab / SLUB                    ← 内核对象分配（粒度：字节，建在 buddy 上）
    │
    │  内存不足时触发回收
    ▼
内存回收层
    rmap（反向映射）               ← 从物理页找到所有映射它的 PTE
    LRU + kswapd                   ← 决定驱逐哪些页、何时回收
    Swap / 文件回写                ← 页驱逐的最终出口
    │
    │  所有回收都失败
    ▼
OOM Killer                         ← 最后兜底
```

---

## 笔记索引

### 已完成

| # | 文件 | 覆盖内容 | 状态 |
|---|------|---------|------|
| 1 | [page-fault.md](page-fault.md) | ARM64 异常向量表 → do_page_fault → handle_pte_fault → do_anonymous_page / do_wp_page → 物理页分配入口 | ✅ |
| 2 | [buddy.md](buddy.md) | 物理内存数据结构（zone/free_area）、PCP、watermark、分配路径、释放路径、延迟分析 | ✅ |
| 3 | [Maple_Tree.md](Maple_Tree.md) | Maple tree 数据结构、范围查询、gap 查找、插入，VMA 管理的底层实现 | ✅ |
| 4 | [boot-mem-init.md](boot-mem-init.md) | 启动期内存初始化：free_area_init、deferred init、buddy 建立过程 | ✅ |
| 5 | [oom-trigger-mechanism.md](oom-trigger-mechanism.md) | OOM 触发决策链、oom_score 计算、进程选择策略 | ✅ |
| 6 | [slub/slub.md](slub/slub.md) | SLUB 分层结构、两条 freelist、无锁快路径、分配/释放流程、与 buddy 的接缝 | ✅ |
| 6a | [slub/slub-sheaves.md](slub/slub-sheaves.md) | SLUB 7.x sheaves 新架构（补充），替代 kmem_cache_cpu 的每 CPU 指针数组缓存 | ✅ |
| 7 | [shmem/shmem.md](shmem/shmem.md) | shmem 设计本质（page cache 反转）、XArray 三态槽位、收敛取页、创建/访问/回收路径 | ✅ |

### 待完成

**回收链（按依赖顺序学）** — 回收一个物理页要经过的完整链路：先从 LRU 挑页，再经 rmap 清掉映射，最后换出/写回。memcg 是把这条链按 cgroup 维度再做一遍。

| # | 主题 | 前置知识 | 优先级 |
|---|------|---------|--------|
| 8 | rmap（反向映射） | page-fault、buddy | 高（回收的基础） |
| 9 | 内存回收（vmscan）：LRU 链表、kswapd/direct reclaim、workingset、shrinker/list_lru | rmap | 高（MM 最复杂的部分） |
| 10 | swap 完整路径（swap-out/in、swap cache、zswap） | LRU、shmem | 中  gpu |
| 11 | memcg（memcontrol） | LRU | 中（生产环境高价值） |

**文件侧骨架** — shmem 已学，这里补普通文件的另一半：磁盘 I/O 与缓存管理。

| # | 主题 | 前置知识 | 优先级 |
|---|------|---------|--------|
| 12 | page cache（filemap / readahead / truncate） | page-fault | 高 |

**大页与碎片** — THP 制造对连续大页的需求，compaction 负责满足它。

| # | 主题 | 前置知识 | 优先级 |
|---|------|---------|--------|
| 13 | THP + hugetlb | buddy、page-fault | 中 gpu |
| 14 | memory compaction | buddy、rmap | 中 |

**地址空间操作接口** — VMA 层的系统调用语义，衔接已学的 Maple Tree。

| # | 主题 | 前置知识 | 优先级 |
|---|------|---------|--------|
| 15 | mmap 系统调用层（mmap / mprotect / madvise / mremap） | Maple Tree | 中 gpu |

按需（不排入主线，用到再学）：vmalloc / percpu-vm、NUMA mempolicy、GPU 相关（mmu_notifier / hmm / migrate_device）、KSM、userfaultfd、CMA、memory hotplug、调试工具（kmemleak / page_owner / vmstat 等）。

### 参考资料

| 文件 | 用途 |
|------|------|
| [todo.md](todo.md) | Radix Tree / XArray / Maple Tree 学习资料链接 |
| [slub/](slub/) | SLUB 归档：笔记、sheaves 补充、分配/释放路径图、学习资料 |
| [mm-iommu-learning-guide.md](mm-iommu-learning-guide.md) | IOMMU 相关学习路径 |
| [mpi-latency-spike-initramfs.md](mpi-latency-spike-initramfs.md) | MPI 延迟问题排查案例（MM 实战） |
| [arm64-page-fault.drawio](arm64-page-fault.drawio) | Page fault 流程图（可视化） |
| DDI0487Mb_toc.txt | ARM 架构手册目录（查 spec 用） |

---

## 核心概念关系

```
VMA（虚拟）  ──── page fault ────▶  folio（物理）
    │                                    │
    │ maple tree 管理                    │ rmap 反向追踪
    │                                    │
    └──────────────────────────────── anon_vma
                                         │
                                         ▼
                                    LRU 链表 ──▶ kswapd ──▶ swap/回写
```

**几个容易混淆的边界：**
- `VMA` 是虚拟地址范围的描述，不持有物理内存
- `folio/page` 是物理内存，不知道自己被谁映射（靠 rmap 反查）
- `anon_vma` 是两者的桥梁，page fault 建立映射时同时建立 rmap
- buddy 管物理页的分配，slab 管内核对象（建在 buddy 上），两者都不涉及虚拟地址

---

## 学习路径建议

**当前进度：** page-fault、buddy、SLUB、shmem 已完成（见 [slub/slub.md](slub/slub.md)、[shmem/shmem.md](shmem/shmem.md)）。

**状态：MM 主线暂停（2026-07）。** 主干（分配：page-fault/buddy/SLUB；文件侧交汇：shmem）已学完，剩余主题（下方回收链 #8-11、page cache #12、大页 #13-14、mmap 接口 #15）全部转为**按需学习**——遇到具体问题再回来补，不再按顺序推进。当前学习重心转入 GPU/CUDA（PMPP 教材），学习计划见 [../ai-infra/overview.md](../ai-infra/overview.md)。

**若恢复 MM 学习，推荐入口：** 回收链 rmap → LRU/vmscan → swap（#8-10），它与已学的 shmem 换出路径直接衔接；page cache（#12）可穿插。
