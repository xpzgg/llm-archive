# Linux 启动期内存初始化：free_area_init → deferred init

> 内核版本：当前工作树（/home/yjc/project/linux）
> 适用场景：理解 boot 早期 buddy allocator 建立过程、定位大内存机器 boot 慢的根因

## 总览：两阶段结构

Linux boot 分两大阶段，mm 初始化的核心工作横跨两个阶段：

```
═══════════════════════════════════════════════════════════════════
阶段 1：start_kernel — boot CPU 单线程（init/main.c:860+）
═══════════════════════════════════════════════════════════════════
  setup_arch()
  mm_core_init_early()              ★ free_area_init 在这里
    └─ free_area_init()             建立 zone、初始化部分 struct page
         └─ memmap_init()
              └─ defer_init()→break 只 init 1 个 section，其余"打标记"
  ...
  mm_core_init()                    buddy + slab 起来
  rest_init() → fork kernel_init 线程

═══════════════════════════════════════════════════════════════════
阶段 2：kernel_init_freeable — SMP 多核（init/main.c:1674+）
═══════════════════════════════════════════════════════════════════
  smp_init()                        secondary CPUs 起来
  sched_init_smp()                  scheduler SMP 就绪
  padata_init()                     并行框架就绪
  page_alloc_init_late()            ★★★ deferred init 在这里做 ★★★
  do_basic_setup()                  跑所有普通 initcall
  run_init_process("/sbin/init")    → userspace
```

**关键认知**：free_area_init 只**记录** `first_deferred_pfn`（"剩下的 page 以后再 init"），page_alloc_init_late 才**真正初始化** deferred 部分。两阶段的设计目的是把"必须串行的早期 init"与"可以并行的大头"切分开。

---

## 1. 第一阶段：free_area_init

### 1.1 "On node N, zone X: pages in unavailable ranges" 日志

**位置**：`mm/mm_init.c:845`，函数 `init_unavailable_range()`（`mm/mm_init.c:831-847`）

**完整调用链**：
```
start_kernel (init/main.c:1040)
  → mm_core_init_early()           (mm/mm_init.c:2685)
      → free_area_init()           (mm/mm_init.c:1806)
          → memmap_init()          (mm/mm_init.c:952, 调用于 1913)
              → memmap_init_zone_range()
                  ├─ memmap_init_range()         // 正常 page 初始化（O(N) 大头）
                  └─ init_unavailable_range()    // ← 日志在此（处理 hole）
```

每个 zone 处理完后，对 zone 内属于"hole"的 pfn 段调用 `init_unavailable_range()`，对每个有效 pfn 执行 `__init_single_page()` + `__SetPageReserved()`，最后用 `pr_info` 打印这条日志报告 hole page 总数。

### 1.2 为什么需要这个流程（设计动机）

Buddy allocator 要求每个 zone 的 `[zone_start_pfn, zone_end_pfn)` 跨度内**每个 pfn 都有合法的 struct page**——即使该 pfn 实际没有物理内存 backing。

如果 hole 不初始化：
- vmemmap 数组对应位置是脏的 struct page
- 后续代码遍历 zone 会读到 garbage（use-before-init）
- buddy 误把 hole 当 free 分配出去 → 灾难

所以把 hole 标记为 `PG_reserved`——"存在但永久保留，绝不分配"。

**"unavailable ranges" = 落在 zone 跨度内但不属于 `memblock.memory`、`pfn_valid()` 为真的 pfn**。典型来源：
- 内存 bank 不对齐 sparsemem section 边界
- 早期 reserved 但未从 `memblock.memory` 摘出的区域
- flatmem 模型下连续映射的真正 hole

### 1.3 memmap_init 的核心机制与 tradeoff

`memmap_init` 遍历 zone 内每个 pfn，对每个 struct page：

```c
__init_single_page():
  memset(page, 0, sizeof(struct page))   // 64 字节
  set_page_links()                       // 写 node/zone
  init_page_count()                      // _refcount = 0
  INIT_LIST_HEAD(&lru)
  page_kasan_tag_reset()                 // 仅 KASAN 启用
```

每 pageblock（2MB）边界：`init_pageblock_migratetype()` + `cond_resched()`。

**核心 tradeoff**：O(N) 单线程串行初始化，N = 物理 page 数。

1000GB / 4KB page：
- N ≈ 256M pages
- vmemmap（struct page 数组）≈ 256M × 64B = **16GB**
- 仅 memset 一项就要写 16GB 内存

**这是设计选择**：早期 boot 阶段 SMP 还没起来、per-cpu allocator 还没就绪、中断也受限——只能单线程做"必须串行"的初始化。代价就是大内存机器 boot 慢。

### 1.4 free_area_init 阶段所有可见日志

| 位置 | 日志 | 时机 |
|------|------|------|
| `mm/mm_init.c:1840` | `Zone ranges:` + 每个 zone 的 `[mem ...]` | 函数开头 |
| `mm/mm_init.c:1857` | `Movable zone start for each node` | 函数开头 |
| `mm/mm_init.c:1869` | `Early memory node ranges` + 每个 node 的范围 | 函数开头 |
| `mm/mm_init.c:1716` | `Initmem setup node %d [mem %#018Lx-%#018Lx]` | `free_area_init_node` 每 node |
| `mm/mm_init.c:845` | `On node %d, zone %s: %lld pages in unavailable ranges` | `memmap_init` 每 zone |

**这条 `pages in unavailable ranges` 就是 `free_area_init` 阶段实际可见的最后一条日志**（最后一个 zone 处理完）。后面只剩 `set_high_memory()`，没输出。

### 1.5 free_area_init 函数本身**没有"结束"日志**

`free_area_init()`（`mm/mm_init.c:1806-1919`）最后两行是 `fixup_hashdist(); set_high_memory();`，默默返回。下一个跟内存直接相关的标志是 `mem auto-init: stack:..., heap alloc:..., heap free:...`（`mm/mm_init.c:2603`，由 `mm_core_init` 内的 `report_meminit()` 打印），但中间隔着其他子系统日志。

真正代表"buddy 就绪、内存管理完成"的日志：
- **`Memory: %luK/%luK available (...)`**（`mm/mm_init.c:2645`），由 `mem_init_print_info()` 打印，调用点在 `page_alloc_init_late()`（`mm/mm_init.c:2325`）——这是 initcall 阶段，比 free_area_init 晚很多。

---

## 2. 第二阶段：deferred init（CONFIG_DEFERRED_STRUCT_PAGE_INIT）

### 2.1 是默认配置吗？

**不是**。`mm/Kconfig:1130` 定义里**没有 `default y`**，所以 Kconfig 层面默认是 `n`。upstream 的 `x86_64_defconfig` 也没开。

但**主流发行版（Ubuntu、RHEL、Debian、SUSE）的发行版 config 都开**——因为他们要支持 TB 级内存的服务器。

**为什么默认 n**：设计哲学是"让小机器走最简路径，让大机器主动 opt-in"。
- 小内存机器（嵌入式、VM、小型服务器）开 defer 纯亏——boot 早期 fast path 多检查、语义复杂化、延迟抖动，但并行收益几乎没有
- 大内存机器用户/发行版知道自己的场景，会主动开
- Kconfig 默认应该让"绝大多数场景"无需思考就拿到最优行为

### 2.2 依赖与失效条件

`mm/Kconfig:1130-1136`：

```kconfig
config DEFERRED_STRUCT_PAGE_INIT
    bool "Defer initialisation of struct pages to kthreads"
    depends on SPARSEMEM         # 需要 section 概念做 grow 边界
    depends on !NEED_PER_CPU_KM  # 需要真正 per-cpu allocator
    depends on 64BIT             # 32 位机器内存小，没必要
    depends on !KMSAN            # KMSAN 要追踪 byte 初始化，deferred 破坏假设
    select PADATA                # 用 padata 做并行
```

**即使 config 开了，运行时还会被强制失效**：
- `page_ext` 启用（`early_page_ext_enabled()` 返回 true，mm/mm_init.c:724）
- KASAN 启用
- 部分 arch 不支持

### 2.3 在整体 boot 流程的位置

deferred 的**实际初始化**在 `kernel_init_freeable()` 里（init/main.c:1674+），位置是 **SMP 起来后、所有普通 initcall（do_basic_setup）之前**。

```
阶段 1：start_kernel — boot CPU 单线程
  mm_core_init_early() → free_area_init() → memmap_init()
    └─ defer_init() 记录 first_deferred_pfn，break

阶段 2：kernel_init_freeable — SMP 多核
  smp_prepare_cpus / workqueue_init / do_pre_smp_initcalls
  
  smp_init()                      ← secondary CPUs 真正起来
  sched_init_smp()                scheduler SMP 就绪
  workqueue_init_topology()
  async_init / padata_init        并行框架就绪
  
  page_alloc_init_late()          ★★★ deferred init 在这里做 ★★★
    ├─ kthread_run("pgdatinit0")  每 NUMA node 一个线程
    ├─ kthread_run("pgdatinit1")  线程绑定到本 node CPU
    └─ padata 多核并行 init
       日志：node N deferred pages initialised in Xms
       日志：Memory: xxxK/xxxK available (...)
  
  do_basic_setup()                跑所有 module_init / initcall
  run_init_process("/sbin/init")  → userspace
```

**为什么必须等到这个位置**：deferred init 是多核并行的，依赖三个前置条件：
| 依赖 | 提供者 | 用途 |
|------|--------|------|
| Secondary CPU online | `smp_init()` | 提供并行 worker |
| Scheduler 就绪 | `sched_init_smp()` | kthread 能被调度 |
| padata 框架 | `padata_init()` | chunk 切分 + 多线程编排 |
| NUMA CPU mask | early boot | 绑定本 node CPU |

**为什么放在 `do_basic_setup()` 之前**：`do_basic_setup()` 会跑所有普通 initcall——大量驱动初始化要分配内存。deferred init 必须在此之前完成（至少 grow_zone 兜底可用），否则驱动初始化时内存不够会 panic。

**dmesg 锚点**：
| 日志 | 含义 |
|------|------|
| `smp: Brought up N CPUs` | `smp_init` 完成 |
| `node N deferred pages initialised in Xms` | deferred init 完成（每 node 一条） |
| `Memory: xxxK available (...)` | buddy 全部就绪 |
| 后续的 driver initcall 日志 | `do_basic_setup` 开始 |

### 2.4 主路径：page_alloc_init_late()（mm/mm_init.c:2298）

```c
void __init page_alloc_init_late(void)
{
    atomic_set(&pgdat_init_n_undone, num_node_state(N_MEMORY));
    for_each_node_state(nid, N_MEMORY) {
        kthread_run(deferred_init_memmap, NODE_DATA(nid), "pgdatinit%d", nid);
    }
    wait_for_completion(&pgdat_init_all_done_comp);   // 等所有 node 完成
    ...
}
```

- **每个 NUMA node 起 1 个 `pgdatinit%d` 内核线程**（mm/mm_init.c:2308）
- 该线程通过 `set_cpus_allowed_ptr()` **绑定到本 node 的 CPU**（NUMA 亲和，避免跨 node 访问 vmemmap）
- 线程内用 `padata_do_multithreaded()` 把 node 的 range 切成多个 chunk，**node 内多 CPU 并行**（mm/mm_init.c:2135-2146）
- 每个 chunk 调 `deferred_init_pages()`（memset + 初始化）+ `deferred_free_pages()`（释放到 buddy）
- 完成后打印 `node %d deferred pages initialised in %ums`（mm/mm_init.c:2151）

### 2.5 defer 边界是怎么决定的

`defer_init()`（mm/mm_init.c:719-754）在 boot 早期 `memmap_init_range` 里判定：
- 只对 **node 的最高 zone**（`end_pfn == pgdat_end_pfn(nid)`）defer——低 zone（DMA/DMA32）必须全初始化（address-constrained 分配要用）
- 初始化超过 `PAGES_PER_SECTION`（128MB 或 512MB，取决于 SECTION_SIZE_BITS）后，记录 `first_deferred_pfn` 并 `break`

所以 defer 的是 **每个 node 最高 zone 中除一个 section 之外的全部 page**——对 1000G 机器来说就是绝大部分。

---

## 3. deferred 实际做的工作（重要：不是页表映射）

deferred 做的两件事在 `deferred_init_memmap_chunk`（`mm/mm_init.c:2044`）核心就两行：

```c
deferred_init_pages(zone, spfn, chunk_end);   // ① struct page 内容初始化
deferred_free_pages(spfn, chunk_end - spfn);  // ② 释放给 buddy allocator
```

**① `deferred_init_pages`（mm/mm_init.c:2018）**：对每个 pfn 调 `__init_single_page(page, pfn, zid, nid)`——memset 清零 struct page、设置 flags、refcount=0、INIT_LIST_HEAD(&lru)。**对象是 struct page 这个数据结构的内容**。

**② `deferred_free_pages`（mm/mm_init.c:1972）**：调 `__free_pages_core()` 把 page 放进 buddy free list——buddy 从此能管理它。

### 3.1 物理内存让 kernel 用上的四件事（不同阶段）

| 工作 | 含义 | 阶段 | 谁做 |
|------|------|------|------|
| **direct map 页表** | 物理内存线性映射到 kernel 虚拟地址（`__va`/`__pa` 可用） | boot 早期 | `setup_arch` → `init_mem_mapping`（x86） |
| **vmemmap 页表** | struct page 数组映射到 vmemmap 虚拟地址空间（`pfn_to_page` 可用） | `free_area_init` 开头 | `sparse_init` → `sparse_vmemmap_init_nid_*`（mm/mm_init.c:1814） |
| **struct page 内容初始化** | struct page 里的 flags/refcount/lru 有合法值 | `free_area_init` 内 + deferred | `memmap_init` + `page_alloc_init_late` |
| **buddy 接管** | page 进入 free list，可以被分配 | `free_area_init` 内 + deferred | `memmap_init` + `page_alloc_init_late` |

deferred 处理的是**下两行**——而且只在第 3、4 步的"最高 zone 大头"部分。**前两步（页表）在 deferred 之前就全部完成了**。

### 3.2 关键认知：deferred 触碰的不是地址映射，是数据结构

`pfn_to_page(pfn)` 这个调用在 deferred 之前就**已经能正确返回** struct page 指针了——因为 vmemmap 页表已经 populate 完。deferred 只是去**写那个 struct page 里的字段**。

从 `deferred_init_pages` 代码（mm/mm_init.c:2024）可以看出：
```c
struct page *page = pfn_to_page(pfn);    // 这里 pfn_to_page 已经能用
for (; pfn < end_pfn; pfn++, page++)
    __init_single_page(page, pfn, zid, nid);
```
如果 vmemmap 没映射好，`pfn_to_page` 直接 page fault，根本走不到 deferred。

### 3.3 易混淆点

- **虚拟化场景下的 `accept_memory`**：在 `deferred_free_pages` 里有 `accept_memory(PFN_PHYS(pfn), ...)`（mm/mm_init.c:1993）。这是 TDX/SEV-SNP 等 confidential computing 场景下"接受物理页"的机制——表面上像"建立映射"，实际是 guest 向 hypervisor 声明"这页我用了"。但这是物理层面的 accept，跟 kernel 页表是两回事。
- **vmemmap 的 sparse_init 太早了，容易被忽略**：vmemmap populate 发生在 `free_area_init` 一开头（line 1814），用户看到的"耗时"主要是 struct page 初始化那部分，所以容易把 vmemmap populate 误记成 deferred 的工作。

---

## 4. 误用防护：deferred page 在初始化前不会被使用

核心机制：**buddy allocator 根本"看不见"这些 page**，所以不会用它们。

deferred 范围的 page 处于三种"不存在"状态：

| 角度 | 状态 | 后果 |
|------|------|------|
| **buddy free list** | 不在任何 free list 上 | buddy 不会从这里分配 |
| **zone 计数** | `managed_pages` / `free_pages` 不包含它们 | watermark 检查时它们不算"可用" |
| **`first_deferred_pfn`** | 这个 pfn 是明确的"已知边界" | buddy 知道 `[start_pfn, first_deferred_pfn)` 可用，往后是"未初始化区" |

**关键代码事实**：在 `memmap_init` 阶段，deferred 范围的 page **既没调 `__init_single_page`，更没调 `__free_pages_core`**（后者是加入 buddy 的唯一入口）。所以从 buddy 视角，这些 page 就像不存在。

### 4.1 三层防护

**防护 1：分配时检查 watermark，自然失败**

buddy 分配路径（`get_page_from_freelist`）每步都检查 watermark：
```c
// mm/page_alloc.c:3900（简化）
if (zone_watermark_fast(...)) {
    // 分配
} else {
    // free 不够，进入兜底逻辑
}
```
deferred page 不算 free → watermark 不达标 → 分配失败 → 进入下一层。

**防护 2：watermark 失败时主动 grow zone（兜底）**

`mm/page_alloc.c:3911`：
```c
if (zone_watermark_failed) {
    if (deferred_pages_enabled()) {
        if (_deferred_grow_zone(zone, order))   // ★ 按需同步初始化
            goto try_this_zone;                  // 初始化完再分配
    }
}
```

- **触发条件**：buddy 分配失败 + 该 zone 有 deferred page
- **行为**：`deferred_grow_zone`（mm/mm_init.c:2169）按 **section 边界**同步初始化"刚好够用"的 page
- **这是 panic-safe 的核心**：boot 中途谁需要大量内存，buddy 就**现场初始化一段**给它。代价是同步、单线程、只够用——但绝不会卡死

调用点（mm/page_alloc.c）：
- line 3912 / 3955：`__alloc_pages_bulk` / 普通分配慢路径
- line 5127：单页分配 retry 路径

**防护 3：`pgdat_resize_lock` 保护边界**

`first_deferred_pfn` 是个会被并发修改的字段（grow_zone 改、page_alloc_init_late 也改），用 `pgdat_resize_lock` 序列化（mm/mm_init.c:2181）。`deferred_grow_zone` 还会 re-check：
```c
// mm/mm_init.c:2187
if (first_deferred_pfn != pgdat->first_deferred_pfn) {
    // 等锁时别人已经 grow 了，我不用再 grow
    return true;
}
```

### 4.2 场景串起来

假设 1000G 内存机器，boot 中途某个 driver initcall 要 alloc 100MB：

```
1. driver 调 __alloc_pages(100MB)
2. buddy 走到 get_page_from_freelist
3. 检查 zone watermark → 失败
   （因为 deferred 的 ~999GB 都不算 free）
4. 进入兜底：deferred_pages_enabled() → true
5. _deferred_grow_zone(zone, 100MB)
   ├─ 取 pgdat_resize_lock
   ├─ 从 first_deferred_pfn 开始
   ├─ 对齐到 section（512MB）
   ├─ 同步 __init_single_page × 512MB worth of pages
   ├─ __free_pages_core → 加入 buddy
   ├─ first_deferred_pfn += 512MB
   └─ 释放锁
6. goto try_this_zone → 这次 watermark 通过
7. 分配成功，driver 拿到 100MB
```

之后 `page_alloc_init_late` 跑时，已经 grow 过的部分就跳过，只初始化剩余的。

### 4.3 设计哲学

为什么用"不存在"代替"标记不可用"？
- 如果打标记，每个 page 都要在 buddy 的多重检查里加一个 `if (page_deferred(page)) skip`——每条 fast path 都加判断，性能损失
- 让它们"不存在"，buddy 的所有路径**零修改**，自然就不会碰——fast path 保持干净

这是 Unix 哲学的体现："把复杂状态隐藏在数据结构里，让算法本身保持简单"。`first_deferred_pfn` 这一个边界变量，把"未初始化"的复杂性完全局限在 grow_zone 慢路径里。

---

## 5. CONFIG_DEFERRED_STRUCT_PAGE_INIT 的影响

### 5.1 正面

boot 时间从 O(N) 单线程 → O(N/CPU) 多核并行，TB 级机器显著缩短。

### 5.2 负面：boot 早期性能抖动

Kconfig help 文本（mm/Kconfig:1142-1144）明说：
> "This has a potential performance impact on tasks running early in the lifetime of the system until these kthreads finish the initialisation."

具体含义：`free_area_init` 完成 → `page_alloc_init_late` 完成这段时间里，任何 task 触发 alloc 一旦 free 不够，就走 `deferred_grow_zone` 慢路径——**单线程、同步、按 section 粒度（128MB/512MB）**。这会引入**延迟不确定**。

### 5.3 负面：改变多个内存语义

| 改变点 | 关 defer | 开 defer |
|--------|----------|----------|
| `memblock_free_pages`（mm/mm_init.c:2471） | 直接加 buddy | deferred 范围直接 return，不加 |
| `MEMBLOCK_RSRV_NOINIT` 语义（memblock.c:1180） | 用默认值初始化但不 reserved | 完全跳过初始化 |
| `free_init_pages` 时机（x86 alternative.c:2461 注释） | 早 | 必须等 page_alloc_init_late |
| EFI reserved memory 处理（efi/quirks.c:491） | 直接处理 | 部分延后 |

### 5.4 负面：数据结构和代码开销

- 每个 pgdat 多两个字段：`first_deferred_pfn`（mmzone.h:1561）+ `pgdat_resize_lock`
- buddy fast path 多一个 `deferred_pages_enabled()` 静态键检查（page_alloc.c:3911/3954/5126）。完成后静态键被永久 disable，开销归零；但**完成前每次 alloc 都查**
- 整套 deferred 代码（mm/mm_init.c:1971-2217）的维护复杂度

### 5.5 实践指导

| 场景 | 建议 |
|------|------|
| 大内存机器（>32GB，尤其 TB 级） | **必开**，否则 boot 慢到不可接受 |
| 小内存机器（<几 GB） | 可关，省一点复杂度和 fast path 开销 |
| 实时/确定性要求 | 慎开——boot 早期 grow_zone 引入延迟不确定 |
| 调试场景（KMSAN/KASAN/page_ext） | 强制不开，无需关心 config |

---

## 6. 关键源码位置（快速查阅）

| 位置 | 函数 | 作用 |
|------|------|------|
| `mm/mm_init.c:831-847` | `init_unavailable_range()` | 日志输出点 + hole page 初始化 |
| `mm/mm_init.c:858-924` | `memmap_init_range()` | 每页初始化主路径（O(N) 大头） |
| `mm/mm_init.c:719-754` | `defer_init()` | deferred init 判定（关键优化） |
| `mm/mm_init.c:1806` | `free_area_init()` | zone 初始化入口 |
| `mm/mm_init.c:2685` | `mm_core_init_early()` | boot 早期入口 |
| `mm/mm_init.c:1972` | `deferred_free_pages()` | 释放给 buddy |
| `mm/mm_init.c:2018` | `deferred_init_pages()` | struct page 内容初始化 |
| `mm/mm_init.c:2044` | `deferred_init_memmap_chunk()` | 单 chunk 处理 |
| `mm/mm_init.c:2098` | `deferred_init_memmap()` | kthread 主函数 |
| `mm/mm_init.c:2169` | `deferred_grow_zone()` | 兜底按需 grow |
| `mm/mm_init.c:2298` | `page_alloc_init_late()` | deferred init 主入口 |
| `mm/mm_init.c:2325` | `mem_init_print_info()` 调用点 | "Memory: ... available" 日志 |
| `mm/page_alloc.c:3911/3954/5126` | `_deferred_grow_zone()` 调用点 | buddy 分配失败兜底 |
| `mm/Kconfig:1130` | Kconfig 定义 | 依赖与默认值 |
| `include/linux/mmzone.h:1561` | `first_deferred_pfn` 字段 | defer 边界 |
| `init/main.c:1040` | `mm_core_init_early()` 调用点 | start_kernel 阶段 1 |
| `init/main.c:1701` | `page_alloc_init_late()` 调用点 | kernel_init_freeable 阶段 2 |

---

## 7. 调试观察手段

- **看 config**：`grep CONFIG_DEFERRED_STRUCT_PAGE_INIT /boot/config-$(uname -r)`（应为 `y`）
- **看 cmdline**：`cat /proc/cmdline` 找 `page_ext / kasan`（这些会让 defer 失效）
- **看 dmesg**：
  - `dmesg | grep "deferred pages initialised"` —— 直接看每 node 耗时
  - `dmesg | grep "Memory:.*available"` —— buddy 全部就绪
- **boot timeline**：`initcall_debug` 内核参数 + bootgraph，定位 `page_alloc_init_late` / `deferred_init_memmap` 在整体 boot timeline 的位置
- **`pgdatinit%d` 线程**：在 `/proc/<pid>/comm` 或 `ps` 里可见（运行时极短）
- **如果看不到 deferred 日志**：说明 config 没开或被 `page_ext` / KASAN 绕过

---

## 8. 关键认知总结（道）

1. **两阶段切分**：boot CPU 单线程串行 init 与 SMP 多核并行 init 的边界由 `first_deferred_pfn` 划定。这是把"必须早做的小集合"和"可以晚做的大头"切分开的关键设计。

2. **"不存在"优于"标记不可用"**：deferred page 不进 buddy 的任何数据结构，让 buddy 的 fast path 零修改。把复杂性隐藏在边界变量 + 慢路径兜底里。

3. **layered initialization**：物理内存可用性是分层的（direct map → vmemmap → struct page 内容 → buddy 接管），每层独立、有明确先后。deferred 只动后两层。

4. **panic-safe fallback**：`deferred_grow_zone` 保证 boot 中途任何时刻需要内存都能拿到——按需同步、单线程、够用就好。代价是延迟不确定，但绝不会卡死。

5. **Kconfig 默认值的哲学**：让"小机器"零配置拿到最优，让"大机器"主动 opt-in。这种"默认是少数场景的最优"的设计，避免了大多数用户为新特性买单。
