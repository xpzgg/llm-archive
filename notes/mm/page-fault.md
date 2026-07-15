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

## 异常分发路径

**设计动机：** ARM 硬件强制 16槽×128字节向量表，128字节放不下完整处理逻辑，只能分两层——槽内跳转，槽外处理。fault 种类多（translation/access flag/permission/alignment），用 FSC 直接当下标查函数指针数组，O(1) 分发。

```
硬件触发异常
└── vectors[EL1h sync]
    kernel_ventry（槽内）：检查栈溢出 → b el1h_64_sync
    entry_handler（槽外）：kernel_entry 保存寄存器（pt_regs）→ bl el1h_64_sync_handler

el1h_64_sync_handler
    switch(ESR_EL1.EC)
    DABT/IABT → el1_abort → do_mem_abort

do_mem_abort
    fault_info[ESR.FSC].fn()        ← FSC 直接当下标，O(1) 分发
    translation fault  → do_translation_fault → do_page_fault（用户地址）
    access flag fault  → do_page_fault
    permission fault   → do_page_fault
    alignment fault    → SIGBUS
```

**反直觉：** EL1t 的4个槽 Linux 永远不会触发（SPSel 固定为1），但硬件强制占位，填 UNHANDLED，触发即 panic。

---

## fault 处理路径

**设计动机：** 全局 mmap_lock 是多线程 fault 的扩展性瓶颈。引入 per-VMA lock 让不同 VMA 的 fault 并发。硬件 walk 失败后软件重走页表，找到缺失的那级补上。PTE 写操作需要加锁，但先乐观无锁读，只在真正要写时加锁验证。

```
do_page_fault
    快路径：lock_vma_under_rcu → per-VMA 读锁 → handle_mm_fault(FAULT_FLAG_VMA_LOCK)
            VM_FAULT_RETRY → 降级
    慢路径：lock_mm_and_find_vma → mmap_lock 读锁 → handle_mm_fault
            VM_FAULT_RETRY → 重试（带 FAULT_FLAG_TRIED）
    错误出口：内核态 → panic；用户态 → OOM / SIGBUS / SIGSEGV

handle_mm_fault → __handle_mm_fault
    软件页表 walk（不存在则分配，存在则取指针）：
    pgd → p4d → pud → pmd → handle_pte_fault
                ↑          ↑
          1GB 大页      2MB THP

handle_pte_fault
    ptep_get_lockless()             无锁读 PTE
    pte_offset_map_lock()           加锁（整张 PTE 页 512槽共用一把锁）
    vmf_pte_changed()               验证未被并发修改
    ├── pte_none      → do_pte_missing → do_anonymous_page / do_fault
    ├── !pte_present  → do_swap_page
    ├── pte_protnone  → do_numa_page
    └── write+!pte_write → do_wp_page
```

**反直觉：** per-VMA lock 下某些路径（如 do_swap_page 需要 IO）会主动返回 VM_FAULT_RETRY 降级，不是报错，是主动让步给慢路径。

---

## 匿名页 fault（do_anonymous_page）

**设计动机：** 读新匿名页内容一定是零，没必要分配物理内存——映射到全局零页即可，写时才真正分配。CoW 也不一定要复制：只有一个引用者时直接改权限，省掉分配和 memcpy。

```
读路径（零页优化）
    PTE → 全局零页（只读，pte_mkspecial 不被 rmap 追踪）
    写 → permission fault → do_wp_page → CoW

写路径
    vmf_anon_prepare()          确保 anon_vma 存在（rmap 基础）
    alloc_anon_folio()          分配物理页（优先 THP，回退单页）
    pte_offset_map_lock()       加锁验证竞态
    map_anon_folio_pte_pf()     建双向映射
        folio_add_new_anon_rmap   反向：物理页 → VMA（回收用）
        set_ptes                  正向：虚拟地址 → 物理页（MMU 用）
        folio_add_lru_vma         加入 LRU active 链表

do_wp_page（CoW / 写保护）
    folio_ref_count == 1 或 PageAnonExclusive
        → wp_page_reuse()       独占，直接改 PTE 权限，不复制
    否则
        → wp_page_copy()        alloc + memcpy + 更新 PTE + 更新 rmap
```

**反直觉：** 独占判断用引用计数 O(1)，不用 rmap 反查 O(n)。引用计数是权威的，rmap 只是辅助。

---

## 物理页分配

**设计动机：** 优先分配大页（THP）降低 TLB 压力；分配后过两道关：cgroup 配额检查 + swap 速率限流。NUMA policy 在此决定从哪个 node 分配。

```
alloc_anon_folio
    [THP] 过滤可用 order → 扫 PTE 确认范围对齐 → vma_alloc_folio(order>0)
    fallback: folio_prealloc(need_zero=true)
        vma_alloc_zeroed_movable_folio    分配 + 清零
        mem_cgroup_charge()               cgroup 配额（超限还页失败）
        folio_throttle_swaprate()         swap 压力大时 sleep 限流

vma_alloc_folio_noprof → alloc_pages_mpol（应用 NUMA policy）
    → __alloc_frozen_pages_noprof → buddy
```

**反直觉：** cgroup 记账在拿到物理页之后做——buddy 分配和 cgroup 配额是两个独立关卡，先过 buddy 再过 cgroup，失败则还页。

物理分配内部详见 [buddy.md](buddy.md)。

---

## 关键概念速查

| 概念 | 一句话 |
|------|--------|
| `pt_regs` | 异常时 CPU 寄存器快照，`kernel_entry` 压栈，C 函数通过指针访问 |
| `ESR_EL1 / FAR_EL1` | 硬件填的异常说明书（类型+FSC）和出错虚拟地址 |
| `fault_info[]` | FSC 直接当下标的函数指针数组，O(1) 分发 |
| `vm_fault` | 贯穿整个 fault 处理链的上下文包，各层沿途填入往下传 |
| `folio` | 新的物理页抽象，替代 `page`，可跨多个连续页 |
| `anon_vma` | 匿名页 rmap 的基础结构，记录物理页被哪些 VMA 映射，回收时用 |
| AF bit | kswapd 主动清零探测页冷热，被访问触发 access flag fault 重新置 1；ARMv8.1 HAFDBS 让硬件自动置位，消除此 fault |
| 零页 | 全局只读零页，读 fault 先映射到这里，写时 CoW 才分配真实页 |
| TLB flush | 修改已有 PTE 必须 flush；新建 PTE 不需要，TLB miss 后自然填入 |
| per-VMA lock | 细粒度锁，让不同 VMA 上的 fault 并发，避免全局 mmap_lock 成瓶颈 |
