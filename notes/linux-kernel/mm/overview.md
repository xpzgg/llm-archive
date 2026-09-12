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
| 6a | [slub-sheaves.md](slub-sheaves.md) | SLUB 7.x sheaves 新架构（补充），替代 kmem_cache_cpu 的每 CPU 指针数组缓存 | ✅ |

### 待完成

| # | 主题 | 前置知识 | 优先级 |
|---|------|---------|--------|
| 6 | Slab / SLUB | buddy | 高（内核对象分配，kmalloc 背后） |
| 7 | rmap（反向映射） | page-fault、buddy | 高（回收的基础） |
| 8 | LRU + kswapd + 内存回收 | rmap | 高（MM 最复杂的部分） |
| 9 | do_swap_page / do_fault | page-fault、LRU | 中（文件/swap 缺页） |
| 10 | THP（Transparent Hugepage） | buddy、page-fault | 中 |
| 11 | memory compaction | buddy、rmap | 中 |

### 参考资料

| 文件 | 用途 |
|------|------|
| [todo.md](todo.md) | Radix Tree / XArray / Maple Tree 学习资料链接 |
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

**当前进度：** page-fault（含物理分配入口）+ buddy 已完成。

**下一步：** Slab/SLUB → rmap → LRU/kswapd（按此顺序依赖最小）。
