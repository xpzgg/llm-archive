# Page Fault — ARM64 笔记

> 内核版本：Linux 7.1-rc1。硬件：ARM64，4KB page，48-bit VA/PA，无虚拟化（EL1&0 only）。

---

## ARM64 PTE 布局（4KB granule，48-bit OA）

L3 页表项（Page descriptor）是 64-bit，bit[1:0]=0b11 固定，表示"有效的 Page descriptor"。

```
 63    55 54  53  52  51  50  49:48  47            12  11  10  9:8   7    6   5  4:2   1   0
 +------+---+---+---+---+---+------+--------------+---+---+---+----+---+---+--+----+---+---+
 |  sw  |UXN|PXN|Con|DBM| GP| RES0 |  OA[47:12]  | nG| AF | SH |AP2|AP1|NS |Attr| 1 | V |
 +------+---+---+---+---+---+------+--------------+---+---+---+----+---+---+--+----+---+---+
```

### 各字段说明

| Bit | 字段 | Linux 宏 | 说明 |
|-----|------|---------|------|
| [0] | **Valid** | — | 0 = 此表项无效 |
| [1] | Page desc | — | 固定 = 1（L3 标识） |
| [4:2] | AttrIndx[2:0] | — | 索引 MAIR_EL1，决定 Normal/Device 等内存类型 |
| [6] | **AP[1]** | `PTE_USER` | 1 = EL0（用户态）可访问 |
| [7] | **AP[2]** | `PTE_RDONLY` | 1 = 只读 |
| [9:8] | SH[1:0] | — | Shareability（0b11 = Inner Shareable，SMP 通常设此值） |
| [10] | **AF** | `PTE_AF` | Access Flag，0 = 从未被访问过 |
| [11] | nG | — | 1 = 进程私有 TLB 条目（带 ASID），0 = 全局 |
| [47:12] | OA[47:12] | pfn | 物理页帧号（36 bit） |
| [49:48] | RES0 | — | 48-bit OA 下保留，必须为 0 |
| [51] | **DBM** | `PTE_WRITE` | Dirty Bit Management，Linux 用作"可写"标记 |
| [52] | Contiguous | — | TLB 合并优化提示，与 fault 无关 |
| [53] | **PXN** | `PTE_PXN` | Privileged Execute Never，1 = 内核不可执行 |
| [54] | **UXN** | `PTE_UXN` | Unprivileged Execute Never，1 = 用户不可执行 |
| [58:55] | sw | — | 软件自定义位（Linux 用于 PTE_SPECIAL 等标记） |

---

## 和 Page Fault 直接相关的 Bit

### 1. Valid（bit[0]）— 触发 translation fault

```
PTE = 0x0          →  pte_none() = true   → 从未映射（匿名页首次访问 / file-backed 首次访问）
PTE ≠ 0，bit[0]=0  →  swap entry          → do_swap_page()（页已换出）
```

ARM64 MMU 只要在 page table walk 的任意一级遇到 valid=0，立即产生 **translation fault**，不再继续 walk。FSC 编码中 level 指示在哪一级失败：

| FSC[5:0] | 含义 |
|----------|------|
| 0b000100 | translation fault, level 0 |
| 0b000101 | translation fault, level 1 |
| 0b000110 | translation fault, level 2 |
| 0b000111 | translation fault, level 3 ← 最常见（PTE 缺失） |

### 2. AF（bit[10]）— 触发 access flag fault

AF = 0 时，CPU 首次访问该页触发 **access flag fault**（FSC = 0b001000~0b001011）。

Linux 建立 PTE 时会直接设 AF=1（`PTE_AF`），所以正常映射路径下不会产生 AF fault。AF fault 在页面回收场景下可能出现（内核主动清 AF 来追踪访问热度）。

### 3. AP[2:1]（bit[7:6]）— 触发 permission fault（读写权限）

AP[2:1] 是 2-bit 组合字段，定义 EL0/EL1 的读写权限（Table D8-63）：

| AP[2] | AP[1] | EL1（内核）| EL0（用户态）| Linux 场景 |
|-------|-------|-----------|------------|-----------|
| 0 | 0 | 读+写 | **无权限** | 内核私有页 |
| 0 | 1 | 读+写 | 读+写 | 普通用户页（malloc 后） |
| 1 | 0 | 只读 | **无权限** | 内核只读数据 |
| 1 | 1 | 只读 | 只读 | **CoW 页**（fork 后写触发 fault） |

表格读法：只列出被允许的权限，未列出 = 隐式拒绝。

**CoW 路径**：`fork()` 后内核将共享匿名页的 AP[2] 设为 1（PTE_RDONLY），任何一方写时触发 permission fault → `do_wp_page()` 分配新页、复制内容、将新页 AP[2] 改回 0。

**SIGSEGV 路径**：用户态访问 AP[1]=0 的页（无权限）→ permission fault → `bad_area()` → SIGSEGV。

permission fault 的 FSC 编码：0b001100~0b001111（level 0~3）。

### 4. UXN / PXN（bit[54] / bit[53]）— 触发 permission fault（执行权限）

| 页类型 | UXN | PXN | 原因 |
|--------|-----|-----|------|
| 用户代码段（.text） | 0 | 1 | 用户可执行，内核不应执行用户代码 |
| 用户数据/堆/栈 | 1 | 1 | 防代码注入（NX 保护） |
| 内核代码段 | 1 | 0 | 内核可执行，用户不可访问 |
| 内核数据段 | 1 | 1 | 数据不可执行 |

用户态对 UXN=1 的页取指 → permission fault → SIGSEGV。

### 5. DBM / PTE_WRITE（bit[51]）— 与 AP[2] 配合表达 dirty 状态

Linux ARM64 用 DBM + AP[2] 共同表达"可写"和"dirty"（来自 `arch/arm64/include/asm/pgtable.h` 注释）：

```
Dirty  Writable | PTE_RDONLY(AP[2])  PTE_WRITE(DBM)
  0      0      |   1                  0           ← 只读（CoW 前）
  0      1      |   1                  1           ← 可写但未写过
  1      1      |   0                  1           ← 可写且已脏
```

---

## PTE 状态 → Fault 类型 → 内核路径

```
PTE = 0（全零）
  → translation fault → pte_none()=true
  → anonymous VMA    → do_anonymous_page()
  → file-backed VMA  → do_fault()

PTE ≠ 0，bit[0]=0（swap entry）
  → translation fault → pte_present()=false
  → do_swap_page()

PTE valid，AP[2]=1，写操作
  → permission fault
  → do_wp_page()（CoW）

PTE valid，AP[1]=0，EL0 访问
  → permission fault
  → bad_area() → SIGSEGV

PTE valid，UXN=1，EL0 取指
  → permission fault
  → bad_area() → SIGSEGV
```

---

## 相关 ARM spec 章节（DDI0487M.b）

| 内容 | 章节 |
|------|------|
| PTE bit 布局（Page descriptor fields）| D8.3.1 |
| AP[2:1] 权限矩阵 | D8.4.1（Table D8-63） |
| UXN/PXN 执行权限 | D8.4.4 |
| Access Flag 机制 | D8.5.1 |
| Fault 类型和 FSC 编码 | D8.15.1 |
| ESR_EL1 寄存器定义 | D24.2.44 |
| FAR_EL1 寄存器定义 | D24.2.47 |

---

## 完整调用链（ARM64，内核态 page fault 主路径）

```
硬件触发异常（MMU walk 失败）
    ↓
vectors[EL1h sync]
    kernel_ventry 宏：检查栈溢出，b el1h_64_sync
    ↓
el1h_64_sync（汇编，entry_handler 宏展开）
    kernel_entry：保存所有寄存器到栈上（pt_regs）
    mov x0, sp → 以 pt_regs * 为参数
    bl el1h_64_sync_handler
    ↓
el1h_64_sync_handler()
    读 ESR_EL1，switch EC：
    DABT_CUR / IABT_CUR → el1_abort()
    ↓
el1_abort()
    读 FAR_EL1（出错虚拟地址）
    do_mem_abort(far, esr, regs)
    ↓
do_mem_abort()
    esr & FSC → 查 fault_info[] 表（函数指针数组）
    inf->fn(far, esr, regs)
    ↓
do_translation_fault()
    is_ttbr0_addr(addr)？
    ├─ 用户地址 → do_page_fault()
    └─ 内核地址 → do_bad_area()（内核 bug，oops/panic）
    ↓
do_page_fault()
    1. 前置检查：kprobe / 中断上下文 / 确定访问类型（读写执行）
    2. 快速路径：lock_vma_under_rcu → handle_mm_fault
    3. 慢速路径：lock_mm_and_find_vma（mmap_lock）→ handle_mm_fault
    4. 结果处理：OOM / SIGBUS / SIGSEGV / no_context（panic）
    ↓
handle_mm_fault()
    hugetlb？→ hugetlb_fault()
    否则    → __handle_mm_fault()
    ↓
__handle_mm_fault()
    软件页表 walk：
    pgd_offset → p4d_alloc → pud_alloc → pmd_alloc
    每级：不存在则分配，存在则直接取指针
    pud/pmd 若支持大页：create_huge_pud / create_huge_pmd
    最终 → handle_pte_fault()
    ↓
handle_pte_fault()
    读 PTE 当前值（lockless），按状态分发：
    ├─ pte_none → do_pte_missing()       页从未映射
    ├─ !pte_present → do_swap_page()     页在 swap
    ├─ pte_protnone → do_numa_page()     NUMA 迁移
    ├─ write + !pte_write → do_wp_page() CoW
    └─ 其他（AF=0）→ pte_mkyoung() 置位后返回
    ↓
do_pte_missing()
    vma_is_anonymous？
    ├─ 是 → do_anonymous_page()
    └─ 否 → do_fault()（文件映射，涉及 page cache）
```

---

## 关键函数分析

### 异常向量表（entry.S）

**ARM64 向量表结构：**
- 硬件强制 16 个槽，每槽 128 字节（`.align 7`），总计 2KB（`.align 11`）
- 16 = 4种EL/SP组合 × 4种异常类型（sync/irq/fiq/error）
- CPU 根据当前 EL 和 SPSel 自动计算偏移跳入对应槽，不查表，是加法

**EL1t vs EL1h：**
- `t`：使用 SP_EL0（Thread 栈指针，ARM 官方命名）
- `h`：使用 SP_EL1（Handler 栈指针，ARM 官方命名）← page fault 走这里
- Linux 启动时将 SPSel 设为 1 并保持，EL1t 永远不应触发
- **EL1t 为何还要存在：** 向量表 16 个槽是硬件强制布局，内核无法删掉这4个槽，只能填上 `UNHANDLED`——如果触发就 panic，做保险

**汇编宏语法（GNU AS `.macro`）：**
- `.macro` 是**纯文本替换**，等价于 C 的 `#define`，没有声明/定义之分，`.endm` 结束
- `\el`：引用名为 `el` 的参数
- `\()`：空分隔符，防止参数名和后续字符粘连（如 `el\el\ht\()_\regsize\()_\label` 拼出函数名）
- `\@`：展开时自动生成的唯一序号，防止局部标号冲突
- `.if \el == 0`：汇编时条件判断（不是运行时），用于宏内条件展开

**`kernel_ventry` 宏（向量槽内容）：**
- 检查内核栈是否溢出
- `b el\el\ht\()_\regsize\()_\label`（无条件跳到槽外真正处理函数，如 `b el1h_64_sync`）
- 之所以分两层：每槽只有 128 字节（32条指令），放不下完整处理逻辑

**`entry_handler` 宏（槽外的处理函数）：**
- `kernel_entry`：把 x0~x30、SP、PC、PSTATE 全部压栈，形成 `pt_regs`
- `mov x0, sp`：把 `pt_regs *` 作为第一个 C 参数（`asmlinkage` 约定）
- `bl el1h_64_sync_handler`：调 C 函数

---

### el1h_64_sync_handler（entry-common.c）

**功能：** 所有 EL1 同步异常的第一个 C 入口，读 ESR 判断异常类型，分发到具体处理函数。

**关键：** ESR_EL1[31:26] 是 EC 字段，CPU 硬件填好，`switch(EC)` 即可分发。
- `DABT_CUR`：访存触发（load/store page fault）
- `IABT_CUR`：取指触发（instruction page fault）
- 其他：breakpoint / undef / BTI / PAC 等

**`_CUR` 后缀：** 来自当前 EL（内核自己），区别于来自低 EL 的 `_LOW`（用户态）。

---

### do_mem_abort（arch/arm64/mm/fault.c）

**功能：** 根据 ESR 中的 FSC（低6位）查 `fault_info[]` 表，调对应处理函数。

**设计：** 函数指针数组，FSC 直接当下标，O(1) 分发，避免大 switch。

**关键表项：**
```
FSC 4~7（translation fault）→ do_translation_fault()  SIGSEGV/SEGV_MAPERR
FSC 8~11（access flag fault）→ do_page_fault()        轻量级，仅置 AF 位
FSC 12~15（permission fault）→ do_page_fault()        CoW / 权限错误
FSC 33（alignment fault）   → do_alignment_fault()    SIGBUS
```

**两个硬件寄存器：**
- `ESR_EL1`：异常类型（"发生了什么"）
- `FAR_EL1`：触发异常的虚拟地址（"在哪发生的"）

---

### do_page_fault（arch/arm64/mm/fault.c）

**功能：** 判断这次 fault 是否合法（VMA 是否存在、权限是否匹配），合法则交给 `handle_mm_fault` 处理，不合法则发信号或 panic。

**前置检查 — kprobe 拦截：**
```c
if (kprobe_page_fault(regs, esr)) return 0;
```
kprobe 单步执行原始指令时，若那条指令恰好触发 page fault，这个 fault 属于 kprobe 机制内部的，不走正常 page fault 路径，直接交回给 kprobe 处理。判断条件：内核态 + 不可抢占 + 当前 CPU 正在执行 kprobe，三者同时满足才可能是 kprobe 触发。

**两条路径（性能优化）：**
- 快速路径：`lock_vma_under_rcu`（RCU 无锁查找），大多数情况在此解决
- 慢速路径：`lock_mm_and_find_vma`（拿 mmap_lock 读锁），RCU 失败或 retry 时降级

VMA 查找底层：`mt_find(&mm->mm_mt, ...)`，maple tree 范围查找，O(log n)。

**错误出口统一：**
- 内核态 fault 且无法修复 → `__do_kernel_fault` → oops/panic
- 用户态 → OOM killer / SIGBUS / SIGSEGV

---

### __handle_mm_fault（mm/memory.c）

**功能：** 软件页表 walk，逐级找到（或分配）各级页表，最终到 PTE 层。

**两种 walk 的区别：**
| | 谁做 | 时机 | 目的 |
|--|------|------|------|
| 硬件 walk | MMU 自动 | 每次内存访问 | 翻译虚拟地址→物理地址 |
| 软件 walk | `__handle_mm_fault` | fault 发生后 | 找到缺失的那一级，补上映射 |

硬件 walk 失败触发 fault，内核软件再 walk 一遍，找到缺的那格填上，下次硬件 walk 就成功。

**页表层级与大页：**
```
PGD → P4D → PUD → PMD → PTE（4KB 普通页）
                ↑         ↑
           create_huge_pud  create_huge_pmd
           （1GB 大页）     （2MB THP）
```

每一级调 `xxx_alloc`，语义是"不存在就分配，存在就直接返回指针"，内部先检查是否为空再决定是否分配，不是无条件分配。

**vm_fault 结构体的设计：**
- `pgd`、`p4d` 是局部变量：只在 walk 过程中临时用一下，后续函数不需要
- `vmf.pud`、`vmf.pmd`、`vmf.pte` 存进 vm_fault：后续 `handle_pte_fault` 及更深层函数只接收一个 `vmf` 参数，需要从里面取这些值

**PTE 命名混淆（内核真实存在）：**
- `pte_t`（值类型）：一个 8 字节的页表项
- `pte_t *`（指针）：指向某个槽位的地址
- `pte_alloc()` / "PTE 页"：存放 512 个 `pte_t` 的那张物理页
- 靠上下文区分，是内核历史包袱

**两种"PMD 情况"进入 `handle_pte_fault`：**
- PMD 槽有值（指向 PTE 页）：PTE 页存在，但某格为空 → `do_pte_missing` 只分配物理页
- PMD 槽为空：PTE 页都还不存在 → `do_anonymous_page` 里先 `pte_alloc` 分配 PTE 页，再分配物理页

---

### handle_pte_fault（mm/memory.c）

**功能：** 读 PTE 当前状态，分发到对应处理路径。

**关键模式：lockless 读 → 加锁 → 再验证：**
```
ptep_get_lockless()        // 不加锁，快速读 PTE 值，做初步判断
pte_offset_map_lock()      // 加锁（锁粒度：整张 PTE 页，512个槽共用一把锁）
vmf_pte_changed()          // 验证 PTE 值是否被其他线程改过
// 确认未变 → 写入
```
这个模式在 MM 代码里到处都有，本质是乐观读：加锁有成本，先不加锁做判断，只在真正要写时加锁并验证。

**锁粒度：** 一张 PTE 页（512 个槽）共用一把 spinlock，存在 `struct page` 里，不额外占内存。粒度比整个 mm 细得多，但比单个 PTE 槽粗——工程权衡：per-PTE 锁内存开销太大，且同一张 PTE 页内并发 fault 的概率极低。

---

### do_anonymous_page（mm/memory.c）

**功能：** 为第一次访问的匿名虚拟地址（堆、栈、`mmap(MAP_ANONYMOUS)`）建立物理映射。

**核心设计：读懒，写实。**

**读路径（零页优化）：**
```
entry = pte_mkspecial(pfn_pte(zero_pfn, vma->vm_page_prot))
set_pte_at(...)   // PTE → 全局零页（只读）
```
- `malloc(1MB)` 后只读：所有页都映射到同一个全局零页，不分配任何物理内存
- `pte_mkspecial`：标记为 special，rmap 不追踪（零页永远不会被回收）
- 第一次写 → 触发 permission fault → `do_wp_page`（CoW）→ 才真正分配物理页

**写路径（真实分配）：**
```
vmf_anon_prepare()         // 确保 anon_vma 存在（rmap 基础结构）
alloc_anon_folio()         // 从 buddy 分配物理页（可能是 4KB 或小 THP）
__folio_mark_uptodate()    // 内存屏障，确保页内容对所有 CPU 可见
pte_offset_map_lock()      // 加锁 + 验证竞态
map_anon_folio_pte_pf()    // 建双向映射
```

**双向映射（`map_anon_folio_pte_nopf` 内）：**
```
folio_add_new_anon_rmap(folio, vma, addr, RMAP_EXCLUSIVE)
    // 反向：物理页 → VMA（内存回收时反查用）
set_ptes(mm, addr, pte, entry, nr_pages)
    // 正向：虚拟地址 → 物理页（硬件 MMU 翻译用）
folio_add_lru_vma(folio, vma)
    // 加入 LRU active 链表，参与页回收老化
```
两个方向都建立后，fault 才真正结束。

---

### do_wp_page（mm/memory.c）

**功能：** 处理写保护 fault（写只读 PTE）。判断能否复用当前页，或必须复制一份（CoW）。

**触发场景：**
1. `fork` 后父子共享页，PTE 被标只读，任意一方写
2. `do_anonymous_page` 读路径映射了零页，第一次写

**判断是否可复用（避免复制）：**
- 主要靠 `folio_ref_count == 1`：引用计数为 1 说明只有我一个人持有，直接改 PTE 为可写，不复制
- `PageAnonExclusive` flag：内核已确认独占的缓存结论，直接复用，不查引用计数
- 用引用计数而非 rmap 反查：O(1) vs O(n)，且计数是权威的

**两条出路：**
```
独占（ref_count==1）→ wp_page_reuse()：改 PTE 权限，省掉内存分配和复制
共享（ref_count>1）→ wp_page_copy()：alloc + memcpy + 更新 PTE + 更新 rmap
```

---

## 关键概念速查

| 概念 | 一句话 |
|------|--------|
| `pt_regs` | 异常发生时 CPU 所有寄存器的快照，`kernel_entry` 压栈，C 函数通过指针访问 |
| `ESR_EL1` | CPU 硬件填的"异常说明书"，EC 字段说明异常类型，FSC 说明具体错误 |
| `FAR_EL1` | CPU 硬件填的"出错地址"，即触发 fault 的虚拟地址 |
| `fault_info[]` | FSC 直接当下标的函数指针数组，O(1) 分发到具体处理函数 |
| `vm_fault` | 贯穿整个 fault 处理链的上下文包，pud/pmd/pte/folio 等沿途填进去往下传 |
| `folio` | 新的物理页抽象（替代 `page`），可以是一个或多个连续页，MM 代码正在迁移中 |
| `anon_vma` | rmap 的基础结构，记录匿名物理页被哪些 VMA 映射，内存回收时反查用 |
| AF bit | PTE bit[10]，**kswapd 主动清零**（不是自动）用于探测页是否还在使用，两阶段：①清 AF（探测）→ 等待 → ②还是 0 则回收，被访问则 access flag fault → 内核置 1。ARMv8.1 HAFDBS 让硬件自动置位 AF，彻底消除这个 fault |
| 零页（zero page）| 全局只读全零物理页，读 fault 的匿名页先映射到这里，写时才 CoW 分配真实页 |
| TLB flush | 修改已有 PTE 后必须做；新建 PTE（从无到有）不需要 flush，TLB miss 后自然填入 |
