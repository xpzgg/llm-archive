# shmem

> 基于 Linux v6.6，代码主要在 `mm/shmem.c`。

## Overview

### 为什么存在

内核需要"可共享的匿名内存"：匿名页（`MAP_ANONYMOUS`）天然私有，不相关进程间无法共享；文件页天然可共享，但必须有后备磁盘。shmem 的解法：造一个没有后备设备的文件系统，**给一段匿名内存一个 inode 身份**。有了 inode 就自动获得文件语义：可按 (inode, offset) 索引、可 mmap、可多进程共享同一个对象。

`shm_open`、`memfd_create`、SysV shm、tmpfs、`MAP_SHARED|ANONYMOUS` 底下全是同一个东西：创建一个 shmem inode，共享它。

### 设计本质：page cache 反转

普通文件系统的 page cache 是**缓存**——真身在磁盘，页可丢弃、脏了写回。shmem 把这套机制反转过来用：

- XArray 里就是**真身**，没有磁盘，页不可丢弃、不可写回；
- 内存紧张要回收时，走**换出到 swap**——swap 扮演普通文件块设备的角色，是 shmem 唯一的"后备存储"。

推论：shmem 是唯一会产生"文件页被换出"的文件系统，所以它与匿名页共用大部分 swap 路径。

### 核心模型：XArray 三态槽位 + 单一取页函数

每个 shmem 文件的页由 `address_space->i_pages`（按页偏移索引的 XArray）统一管理，每个槽位只有三态：

| 槽位值 | 含义 | 判别 |
|---|---|---|
| 指向 folio 的指针 | 页在内存 | 最低位为 0 |
| swap entry（编码值） | 页已换出，记录 swap 位置 | 最低位为 1（`xa_mk_value`） |
| 空 | 空洞，从未写过 | NULL |

**所有取页路径（fault / read / write / fallocate / GPU 驱动）收敛到 `shmem_get_folio_gfp()`**：查槽位 → 是 folio 直接返回，是 swap entry 换入，是空洞按 `sgp_type` 意图决定分配或拒绝。上层 handler 因此都很薄，复杂度全在这一层。

### 两个世界与交接工单

```
进程侧 (MM)                        文件侧 (VFS)
────────────                      ──────────────────────────────
mm_struct
  └─ VMA ──vm_ops──→ shmem_vm_ops (.fault = shmem_fault)
       │──vm_file──→ file ──→ inode ─┬── i_fop ──→ shmem_file_operations (read/write/mmap)
       │──vm_pgoff                   │── i_data: address_space (内嵌)
       │                             │      ├── a_ops ──→ shmem_aops (.writepage = 换出)
       │                             │      ├── i_pages: XArray (三态槽位)
       │                             │      └── i_mmap: VMA 区间树 (回收时反查 PTE)
       │                             └── 外层 container_of → shmem_inode_info
页表 PTE ──PFN──→ 物理页 ←── XArray 槽位也指向同一页 (两条独立引用)

每个 tmpfs 挂载点: super_block → shmem_sb_info (max_blocks/used_blocks 限额)
```

- shmem 没有主动运行的代码，它是**三张回调表**，挂在 VFS（`file_operations`）、缺页层（`vm_ops`）、回收层（`a_ops`）三个通用框架上。
- 偏移换算由通用缺页层完成：`pgoff = (addr - vm_start)/PAGE_SIZE + vm_pgoff`，fault handler 直接拿到文件偏移。
- 填 PTE 也由通用层（`finish_fault`）完成；`vm_fault` 结构是两侧交接的工单：通用层填"缺哪个地址、哪个偏移"，文件系统填"用哪页"（`vmf->page`）。
- **PTE 只含 PFN + 权限位，不含任何指向 XArray 的信息。** PTE 与 XArray 槽位是指向同一物理页的两条独立引用，互不依赖。

### 关键数据结构

| 粒度 | 结构体 | 职责 |
|---|---|---|
| 每挂载点 | `shmem_sb_info` | 限额：`max_blocks`/`used_blocks`（`size=`、docker `--shm-size` 的落点）、`noswap` |
| 每文件 | `shmem_inode_info`（包住 inode） | `alloced`/`swapped` 页计数、`seals`、NUMA `policy`、`swaplist` |
| 每文件 | `address_space`（内嵌 inode） | `i_pages` 页仓库 + `i_mmap` VMA 区间树 + `a_ops` |
| 每页偏移 | XArray 槽位值编码 | 区分 folio / swap entry / 空洞 |
| 每次取页 | `enum sgp_type` | 调用方意图：`SGP_READ`/`NOALLOC`（不分配）、`SGP_CACHE`（不超 i_size 可分配）、`SGP_WRITE`/`FALLOC`（可超 i_size 分配） |

---

## 如何使用 shmem（入口与映射）

### 创建入口

所有接口收敛到 `__shmem_file_setup()`（`mm/shmem.c:4770`）：

```
memfd_create()          → shmem_file_setup         (mm/memfd.c:363)
SysV shmget()           → shmem_kernel_file_setup  (ipc/shm.c:767)
GPU 驱动 (i915/ttm GEM) → shmem_file_setup_with_mnt
                              │
                              ▼
                    __shmem_file_setup (shm_mnt 内部隐藏挂载点)
                    ① 预记账 ② shmem_get_inode (建 inode + 装回调表)
                    ③ i_size = size (只记数字, 不分配页) ④ alloc_file_pseudo

open("/dev/shm/x", O_CREAT) → VFS → shmem_create → 同样调 shmem_get_inode
                              (有路径名, 走 tmpfs 目录项)
```

**创建只建壳：inode + 空 XArray + 回调表，零页内存被分配。** GPU 相关：i915/ttm 的 GEM 对象后备存储就是 shmem（`shmem.c:4885` 注释），统一内存架构下 GPU buffer 的页本质是可换出的 shmem 页。

### 映射（mmap）

```
mmap(fd, ..., MAP_SHARED)
→ mmap_region (mm/mmap.c)
   ├─ 建 VMA: vm_file = file, vm_pgoff = offset, vm_page_prot = 权限模板
   ├─ file->f_op->mmap = shmem_mmap (shmem.c:2371)
   │    └─ vma->vm_ops = &shmem_vm_ops        ← 唯一实质动作: 挂回调
   └─ vma_link: VMA 同时挂进 mm 的 VMA 树和 address_space->i_mmap
```

`MAP_SHARED|ANONYMOUS`（无 fd）由 `shmem_zero_setup`（`shmem.c:4852`）现场创建隐藏 shmem 文件并挂到 VMA 上——共享匿名内存就是没名字的 shmem。

映射只建关联不建页。两个进程 mmap 同一文件后，各自 VMA 的 `vm_file` 指向同一 inode，fault 后两边 PTE 指向同一批物理页——这就是共享的全部。PyTorch DataLoader worker 间传 batch、NCCL SHM transport 走的就是这个结构。

---

## 创建路径（首次 fault 分配页）

```
*p = 1  (PTE 不存在)
→ handle_mm_fault (通用缺层, 算好 pgoff, 定位 PTE 槽位)
→ shmem_fault (shmem.c:2159)
   └─ shmem_get_folio_gfp(inode, pgoff, SGP_CACHE)  (shmem.c:1927)
       ├─ 查 XArray 槽位 → 空洞
       ├─ shmem_alloc_and_acct_folio (shmem.c:1664)
       │    ├─ shmem_inode_acct_block: 扣 used_blocks, 超限 → -ENOSPC
       │    ├─ vma_alloc_folio: 向 buddy 要页 (伪 VMA 携带 NUMA policy)
       │    └─ __folio_set_swapbacked  ★ 决定页挂 anon LRU, 回收走 swap
       ├─ shmem_add_to_page_cache: 插入 XArray
       ├─ folio_add_lru: 挂 LRU (页一出生即可回收)
       └─ 清零 + 标 uptodate (shmem 页生来是零页)
→ finish_fault (通用层, mm/memory.c:4390)
   └─ set_pte_range: mk_pte(页→PFN + 权限) → set_ptes 写入页表
→ CPU 重试触发 fault 的指令, 命中, 用户态无感
```

成功 = 两条独立引用都建立：XArray 槽位 → folio（文件侧持有），PTE → 物理页（进程侧访问通道）。

---

## 访问路径（页已存在）

- **页在内存（快路径）**：查槽位 → folio → 验证 `folio->mapping` 后直接返回；fault 填 PTE，read 走 `copy_to_user`。
- **页在 swap（换入）**，`shmem_swapin_folio`（`shmem.c:1813`）：

```
槽位是 swap entry → 解码出 swap 位置
→ 先查 swap cache (以 swap entry 为索引的 XArray)
   命中 → 直接用 (换出写盘途中 / 别的进程刚换回)
   未命中 → shmem_swapin 发 I/O 读盘, 标 VM_FAULT_MAJOR
→ 验证槽位没被并发改掉 (改了 → -EEXIST → 上层 repeat 重查)
→ shmem_add_to_page_cache: 槽位改写回 folio 指针  ★ 三态回转
→ delete_from_swap_cache + swap_free: 释放 swap 槽位, swapped--
   (一页在内存和 swap 中只居其一)
```

---

## 回收路径（换出到 swap）

触发：内存压力（kswapd 后台 / direct reclaim 同步），从 anon LRU 队尾选中。

```
回收框架: try_to_unmap — 经 rmap (i_mmap 区间树) 清掉所有映射该页的 PTE
→ shmem_writepage (shmem.c:1422)
   ├─ 守卫: VM_LOCKED / noswap 挂载 / 无 swap → redirty 放回 LRU
   ├─ folio_alloc_swap: 在 swap 区申请槽位
   ├─ add_to_swap_cache: 页转入 swap cache
   ├─ shmem_delete_from_page_cache(folio, swp_to_radix_entry(swap))
   │    ★ 三态转换: XArray 槽位从 folio 指针改写成 swap entry; swapped++
   └─ swap_writepage: 内容写到 swap 设备, 释放页
```

**无 swap 时 shmem 页钉死在内存**（不可回收），压力大了直接 OOM——tmpfs 要配 `size=`、docker 默认 `--shm-size=64M` 的原因。DataLoader 场景 `Bus error`（SIGBUS）则来自另一头：`shm` 太小，分配时 `-ENOSPC`。

三态闭环：空洞→指针（创建）、指针→swap entry（回收）、swap entry→指针（换入）。

---

## 易混点

- **rmap 是反向的**（页 → PTE），用在回收/unmap；fault 路径是正向的（地址 → 页），全程不碰 rmap。
- **顺序是先有 page cache 里的页，后有 PTE 映射**，不是"映射完页才进 page cache"。
- **`__folio_set_swapbacked` 是分组判据**：shmem 页形态是文件页，但挂 anon LRU、回收走 swap，凭的就是这个标志。
- **`-ENOSPC` 即 tmpfs"磁盘满"**：sparse 映射上体现为 SIGBUS 而非 OOM。
- **major fault = 缺页需要磁盘 I/O**（换入读盘时标记）；`maj_flt` 高说明 shmem 页在内存与 swap 间颠簸，是内存不足信号。
- **userfaultfd 不分配页**：`handle_userfault` 只挂起 fault 线程并通知监听线程；分配+填数据推迟到 `UFFDIO_COPY` ioctl（`shmem_mfill_atomic_pte`），内容来源换成用户态 buffer。
