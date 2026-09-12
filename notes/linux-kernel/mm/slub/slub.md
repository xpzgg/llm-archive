# SLUB（v6.6 经典架构）

> 基于 Linux v6.6（`mm/slub.c`）。SLUB 是 SLAB 系分配器的默认实现（SLOB 6.4 移除，SLAB 6.8 移除，此后 SLUB 是唯一实现）。
> 7.x 起 SLUB 的 per-CPU 层被 sheaves 新架构取代，见 [slub-sheaves.md](slub-sheaves.md)；本文是理解新架构的基础。
> 配套图：[slub-overview.svg](slub-overview.svg)（一张 slab 与三层结构）、[slub-alloc-path.svg](slub-alloc-path.svg)（分配全流程）、[slub-free-path.svg](slub-free-path.svg) / [slub-free-path.html](slub-free-path.html)（释放全流程）。

---

## 为什么需要 SLUB

buddy 的最小分配粒度是页（4KB），但内核自身大量需要几十到几千字节的对象（`task_struct`、`inode`、`dentry`、`kmalloc` buffer），且创建销毁极其频繁。直接按页分配：内部碎片浪费 90%+，且每次都要走 buddy（zone lock、拆页合页），速度不可接受。

SLUB 的思路：从 buddy 按页批发，切成等大对象零售，并缓存这些页复用。**一种对象类型（或尺寸档）对应一个 `kmem_cache`**。`kmalloc` 没有独立实现，就是一组固定尺寸档的 kmem_cache（8/16/32/…/8K，外加 96/192 两档）。

## 分层设计与核心数据结构

```
分配请求
  │
  ▼
kmem_cache_cpu（per-CPU，无锁）              ←→ buddy 的 PCP
  │ 没货
  ▼
kmem_cache_node.partial（per-node，list_lock） ←→ free_area
  │ 也没货
  ▼
buddy（new_slab 批发新页）
```

与 buddy 同一哲学：高频路径 per-CPU 无锁，共享层只做批量交接（锁粒度从"每次操作"变成"每批次"）。

角色边界：

- **`kmem_cache`**（slub_def.h:98）：一类对象的配置（`object_size`、`oo`={order, objects}、`min_partial`、`cpu_partial` 等）+ 指向两级缓存的指针（`cpu_slab[]`、`node[]`）。**本身不存任何对象，不是一层库存**——角色对应 buddy 的 `struct zone`。
- **`kmem_cache_cpu`**（slub_def.h:50）：per-CPU 分配状态。`freelist`（本 CPU 可分配的空闲对象链头）、`slab`（当前活跃 slab）、`tid`（版本号，与 freelist 打包成双字供 `this_cpu_cmpxchg`）、`partial`（本 CPU 的卸任 slab 链）。
- **`kmem_cache_node`**（mm/slab.h:776）：只有 `list_lock` + `nr_partial` + `partial` 链表。经典 SLAB 同位置有三条链表、着色、跨节点 alien cache 等一大堆字段——SLUB 刻意把 node 层做薄，复杂性压到 per-CPU 层。
- **`struct slab`**（mm/slab.h:42）：一张 slab 的元数据，**复用首页的 page 描述符，零额外内存**。关键字段：`slab_cache`（反查归属指针）、`freelist`（页内空闲对象链）、`inuse/objects/frozen`（打包进一个字长）。

## 两条 freelist 与 frozen

一张 slab 处于 frozen（某 CPU 的活跃 slab）期间，它的空闲对象分成两条互不相交的侵入式链：

- `cpu_slab->freelist` — 本 CPU 私有：分配 pop、释放 push，无锁
- `slab->freelist` — 公共接收端：**其他** CPU 释放回来的对象（它们不能碰别人的 `cpu_slab->freelist`）

搬运规则：激活时 `slab->freelist` 整条 → `c->freelist`（`inuse=objects`）；`c->freelist` 耗尽时 `get_freelist` 再收编一次；卸任时 `c->freelist` 拼回 `slab->freelist` 头部，两链合一。

**一张 slab 任意时刻只在三个位置之一**：某 CPU 的活跃 slab / 某条 partial 链表 / 全满不在任何链表（"无主"，靠对象释放时反查找回——所以 node 层不需要 full 链表）。

**布局围绕原子操作单元**（三处字段打包）：`cpu_slab` 的 freelist+tid、`struct slab` 的 freelist+counters、`kmem_cache` 的 oo/min。看到几个字段打包成一个字，就意味着那里有一次性原子读写的需求。

## 关键流程

### 分配

```
kmem_cache_alloc（:3500）
└─ 快路径（:3329 一带）        c->freelist 非空且 node 匹配？
   ├─ this_cpu_cmpxchg pop     [ALLOC_FASTPATH] 绝大多数分配在此结束
   └─ __slab_alloc → ___slab_alloc（:3095）
```

`___slab_alloc` 三阶段：

1. **挽留当前 slab**：拿 local_lock 重校验 → 重查 `c->freelist`（中断可能刚释放过）→ `get_freelist`（:3050）收编 `slab->freelist`［ALLOC_REFILL］。两条链都空 = slab 全满 → `get_freelist` 顺手置 frozen=0，摘下 `c->slab` 即完成卸任［DEACTIVATE_BYPASS］（满 slab 本来就无链表，零成本）。
2. **换一张 slab（成本递增）**：`cpu_slab->partial`［CPU_PARTIAL_ALLOC］→ `node->partial`（`get_partial`，持 list_lock）→ `new_slab`（buddy）。
3. **激活**：new_slab 来的新 slab 整链移交（`freelist=slab->freelist`、`slab->freelist=NULL`、`inuse=objects`、`frozen=1`）——它在挂到 cpu_slab 前对其他 CPU 不可见，是唯一不需要 cmpxchg 的窗口。partial 来的 slab 直接 load_freelist 装链。

设计要点：**`c->freelist` 空 ≠ slab 空**——先收编再换 slab，避免 slab 在活跃 ↔ partial 之间频繁倒手。

### 释放

```
kmem_cache_free（:3825）
└─ slab_free        virt_to_slab(x)：对象地址 → 页 → struct slab（反查归属）
   └─ do_slab_free（:3734）   所属 slab == c->slab？
      ├─ 是：this_cpu_cmpxchg push 到 c->freelist   [FREE_FASTPATH]
      └─ 否：__slab_free（:3600）  对象挂到 slab->freelist（slab 级双字 cmpxchg）
         ├─ was_frozen（别的 CPU 的活跃 slab）→ 挂完即完事 [FREE_FROZEN]
         ├─ prior==NULL（释放前全满）→ frozen=1，挂本 CPU 的 cpu partial
         │    [CPU_PARTIAL_FREE]；未配置 cpu partial 则挂 node partial
         └─ 释放后全空 → 从 partial 摘下，nr_partial ≥ min_partial 则还 buddy
```

要点：**释放的分流判据只有一个**（是不是本 CPU 的活跃 slab）；分配/释放快路径是同一 cmpxchg 原语的 pop/push 对称。释放路径没有失败概念，最重操作是还页给 buddy。

### 与 buddy 的接缝（new_slab:2063 → allocate_slab:1996）

- **两次尝试**：先用 `oo` 的 order + 悲观 flags（`__GFP_NORETRY` 等，让高阶分配快速失败）→ 失败则降级 `s->min` 的更小 order 再试［ORDER_FALLBACK］。对象分配是高频路径，不为高阶连续页阻塞在回收里——SLUB 对连续性的需求是弹性的（不同于设备 DMA、hugetlbfs 的硬性需求）。
- `__folio_set_slab`：folio 描述符从此被重新解读为 `struct slab`——"复用 page 描述符"的发生点。

## 疑难概念（Q&A 沉淀）

**无锁快路径为什么成立？** 不是因为没人竞争，而是不变量："只有本 CPU 上执行的代码会碰本 CPU 的 `cpu_slab->freelist`"（其他 CPU 的释放走 `slab->freelist`，另有 slab 级 cmpxchg）。竞争者只有同 CPU 的中断和被抢占/迁移的任务——SLUB 快路径**不禁抢占**，cmpxchg 检测干扰、失败重试，同时兼任"验证我还在原 CPU 上"。`tid` 防 ABA：freelist 指针可能绕一圈指回同一对象，tid 每操作必变。`CONFIG_PREEMPT_RT` 下这套前提不成立，无锁路径整体退化为 local_lock。

**cpu partial 和 node partial 的去向规则？** 入口分离：分配路径卸任（`deactivate_slab`:2492）只去 node partial 或 buddy，从不去 cpu partial；**cpu partial 的唯一入口在释放路径**（`put_cpu_partial`）——释放者把"因自己的释放而重新有空闲对象"的 slab 留在自己手上（刚释放过、大概率很快又要分配同类对象），下次分配零锁激活，也避免在释放热路径上拿 list_lock。超出 `s->cpu_partial` 上限则批量退回 node partial（`unfreeze_partials`）。

**一张 slab 会被多个 CPU 同时使用吗？** 不会同时作为活跃 slab（整链移交保证独占转移）。但"分配独占、释放开放"：frozen 期间其他 CPU 可释放属于它的对象（挂 `slab->freelist`，FREE_FROZEN），由持有 CPU 下次 `get_freelist` 收编。

**kmem_cache 是第一层缓存吗？** 不是。它不存对象，是管理结构；真正的层级是 `cpu_slab` → `cpu partial` → `node->partial` → buddy。

## 参考资料

- 代码：`mm/slub.c`、`include/linux/slub_def.h`、`mm/slab.h`（v6.6）
- Christoph Lameter（SLUB 作者）"Slab allocators in the Linux Kernel: SLAB, SLOB, SLUB"，LinuxCon 2014：<https://events.static.linuxfound.org/sites/events/files/slides/slaballocators.pdf>
- LWN "The SLUB allocator"（2007）：<https://lwn.net/Articles/229984/>
- Oracle "Linux SLUB Allocator Internals and Debugging" 四连篇（2022，v5.19 ≈ v6.6 架构）：<https://blogs.oracle.com/linux/linux-slub-allocator-internals-and-debugging-1>
