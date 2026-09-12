# Buddy System（伙伴系统）

---

## 设计动机

物理内存需要按需分配任意大小的连续块，同时要能高效合并碎片。buddy 的核心约束：**每个块的大小是 2 的幂次，起始地址按块大小对齐**。这个约束保证了"buddy 一定在固定位置"——合并时不需要搜索，直接 XOR 计算 buddy 地址，O(1) 找到。

---

## 数据结构

```
pg_data_t（NUMA node）
└── struct zone
    ├── watermark[min/low/high]
    ├── per_cpu_pageset（PCP 缓存）
    └── free_area[0..MAX_PAGE_ORDER]
            └── free_list[MIGRATE_TYPES]
```

**MIGRATE_TYPES（迁移类型）：** 解决外部碎片。按页的可移动性分组（UNMOVABLE/MOVABLE/RECLAIMABLE），让 compaction 能把可移动页归拢成连续大块，不可移动页聚在一起不干扰。

**Watermark（水位线）：** 分配和回收需要提前协调，不能等到 0 才开始回收。
- 低于 low → 唤醒 kswapd 后台回收
- 低于 min → 只允许紧急分配
- 恢复到 high → kswapd 停止

**PCP（per-CPU pageset）：** 多核争 `zone->lock` 是瓶颈。每个 CPU 维护一个小页面缓存，分配/释放先走本 CPU 缓存，只有缓存空/满时才批量和 buddy 交换——把锁粒度从"每页"变成"每批次"。

---

## 分配路径

```
do_anonymous_page
└── alloc_anon_folio          优先尝试 THP（小大页），失败则单页
    └── folio_prealloc
        └── vma_alloc_folio_noprof   应用 VMA 的 NUMA policy
            └── __alloc_frozen_pages_noprof
                ├── get_page_from_freelist   快路径（watermark 检查 → rmqueue）
                └── __alloc_pages_slowpath   慢路径（回收 / compact / OOM）

rmqueue
├── rmqueue_pcplist    PCP 命中，直接返回，不碰 zone->lock
└── rmqueue_buddy      PCP 未命中，持 zone->lock 进 buddy freelist
    └── __rmqueue_smallest
        从请求 order 向上扫 free_area[]，找第一个非空 freelist
        └── expand（大块拆分）
            多余部分从高到低拆回各级 freelist
```

**关键反直觉：** `__alloc_frozen_pages_noprof` 分配出来的页引用计数是 0（frozen），调用方负责在合适时机 `set_page_refcounted`。

---

## 释放路径

```
folio_put / __free_pages
└── __free_frozen_pages
    ├── order 大或 ISOLATE → 直接进 buddy（跳过 PCP）
    └── 正常路径 → 还给 PCP
        PCP 满 → 批量 drain 到 buddy
        └── __free_one_page（buddy 合并）
            while (有空闲 buddy):
                从 freelist 摘出 buddy
                合并，order++
            加入对应 free_area[order]
```

**buddy 地址计算：** `buddy_pfn = pfn ^ (1 << order)`，合并后起始 = `pfn & buddy_pfn`。物理连续 + 对齐，合并后自然对齐到上一级。

---

## 延迟不确定性

```
PCP 命中   → ~ns（无锁）
buddy 分配 → ~百 ns（zone->lock）
触发回收   → ~μs（LRU 扫描）
swap IO    → ~ms（磁盘）
```

同一个 `malloc` 最快最慢差 5 个数量级。Linux 保吞吐量不保最坏延迟。延迟敏感场景靠 `mlock` + 预触发、hugetlbfs 预分配、用户态内存池来规避。
