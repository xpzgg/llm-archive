# Maple Tree

> Linux 6.1 引入，替换 VMA 管理中的 rb-tree + linked list。代码：`lib/maple_tree.c`，`include/linux/maple_tree.h`。

## Why：为什么需要 Maple Tree

rb-tree + linked list 的根本问题：`mmap_lock` 是一把全局读写锁，VMA 的任何增删改查都要拿它。多线程程序里大量 page fault 同时来，全部在 `mmap_lock` 上排队，成为瓶颈。

Maple tree 的设计目标：
- **RCU-safe**：读操作可以在不持锁的情况下并发进行（per-VMA lock 方案的基础）
- **范围查询原生支持**：存的就是区间 `[start, end]`，不需要 rb-tree 那样"找第一个 end >= addr 的节点"的技巧
- **gap 追踪内置**：`arange_64` 节点带 `gap[]`，O(log N) 找空闲区间，专门为 mmap 分配地址设计

---

## 数据结构

### 节点类型

树里有两种内部节点，叶子用哪种取决于树是否开了 `MT_FLAGS_ALLOC_RANGE`：

| 类型 | 用途 | slot 数 | pivot 数 | 特殊字段 |
|------|------|---------|---------|---------|
| `maple_range_64` | 普通内部/叶子节点 | 16 | 15 | 无 |
| `maple_arange_64` | 带 gap 追踪的节点 | 10 | 9 | `gap[10]` |

VMA 管理的 `mm->mm_mt` 开了 `MT_FLAGS_ALLOC_RANGE`，所以用 `maple_arange_64`（非叶子节点分支因子 10，叶子节点分支因子 16）。

### `maple_range_64`（叶子）

```c
struct maple_range_64 {
    struct maple_pnode *parent;
    unsigned long pivot[MAPLE_RANGE64_SLOTS - 1];  // pivot[15]
    union {
        void __rcu *slot[MAPLE_RANGE64_SLOTS];      // slot[16]
        struct {
            void __rcu *pad[MAPLE_RANGE64_SLOTS - 1]; // pad[15]
            struct maple_metadata meta;  // 和 slot[15] 共用内存
        };
    };
};
```

`meta` 和 `slot[15]` 通过 union 共用同一块内存，所以叶子节点只用 `slot[0..14]` 存数据，`slot[15]` 的位置存 `meta`（包含 `end`：实际使用的 slot 数，`gap`：最大空洞的 offset）。

### `maple_arange_64`（内部节点，带 gap）

```c
struct maple_arange_64 {
    struct maple_pnode *parent;
    unsigned long pivot[MAPLE_ARANGE64_SLOTS - 1];  // pivot[9]
    void __rcu *slot[MAPLE_ARANGE64_SLOTS];          // slot[10]
    unsigned long gap[MAPLE_ARANGE64_SLOTS];         // gap[10]
    struct maple_metadata meta;
};
```

`gap[i]`：`slot[i]` 指向的子树中，最大的空闲区间长度。

### pivot 与 slot 的对应关系

N 个 pivot 把地址空间切成 N+1 段，每段对应一个 slot：

```
     pivot[0]   pivot[1]   pivot[2]
        |          |          |
[slot0][   slot1   ][  slot2  ][  slot3  ]
```

- `pivot[i]` 是 `slot[i]` 的右边界（inclusive）
- `slot[i]` 的左边界 = `pivot[i-1] + 1`（或节点的 `min`，对 slot[0]）
- 最后一个 slot 的右边界 = 父节点传入的 `node_max`，不显式存储

叶子节点的 slot 存 VMA 指针（或 NULL 代表空洞）。内部节点的 slot 存指向子节点的指针。

---

## 查找：`mas_walk()` / `mtree_lookup_walk()`

从根到叶子，每层做同一件事：**找第一个 `pivot[i] >= X`，进 `slot[i]`**。

```c
do {
    pivots = ma_pivots(node, type);
    offset = 0;
    do {
        if (pivots[offset] >= mas->index)
            break;
    } while (++offset < end);

    slots = ma_slots(node, type);
    next = mt_slot(mas->tree, slots, offset);
} while (!ma_is_leaf(type));

return (void *)next;  // VMA 指针 or NULL
```

到叶子后直接返回 `slot[offset]`：
- 非 NULL → 找到 VMA，地址 X 在这个 VMA 的范围内
- NULL → 地址 X 在空洞里，没有 VMA 覆盖

---

## Gap 查找：`mas_empty_area()` / `mas_empty_area_rev()`

用途：mmap 分配地址时，找一段大小 ≥ N 的空闲区间。

ARM64 默认 top-down layout，走 `mas_empty_area_rev()`（从高地址向低地址找）。

核心是 `mas_anode_descend()`，在每个内部节点（`maple_arange_64`）上：

```c
for each slot[offset] in node:
    if gap[offset] < size:
        continue;           // 这棵子树里最大的空洞都不够，跳过
    if ma_is_leaf:
        found = true;       // 叶子节点，找到了
        break;
    // 非叶子：下沉到这个子树
    mas->node = slot[offset];
```

`gap[]` 使得不够大的子树整棵跳过，搜索复杂度 O(log N)。

找到叶子后，`mas->index` 里存的就是空闲区间的起始地址，由 `unmapped_area_topdown()` 返回给 `do_mmap()`。

---

## 插入：`mas_store_prealloc()`

插入一个 VMA `[X, Y)` 本质是把叶子里一个 NULL slot（空洞）拆成三段：

```
之前：[............NULL............]   pivot 覆盖整个空洞
之后：[..NULL..][VMA: X~Y][..NULL..]   多加 2 个 pivot，2 个 slot
```

### 三种主要情况

| `store_type` | 场景 | 操作 |
|---|---|---|
| `wr_append` | 新 VMA 紧贴末尾，叶子有空余 | 末尾追加 slot，调整最后 pivot |
| `wr_node_store` | 插入中间，叶子有空余 | 移动后续 slot，写入新 pivot |
| `wr_split_store` | 叶子已满（16 slot 全用完）| 叶子一分为二，向父节点推一个 pivot，可能递归向上 |

### Prealloc 机制

节点分裂需要分配新节点，最坏情况每层都分裂（树高 ≈ log N 次）。

流程分两步：
1. `vma_iter_prealloc()`：提前分配好所有可能需要的节点，存在 `mas->alloc` 链表里
2. `mas_store_prealloc()`：从链表取节点，直接用，**不再调 kmalloc，保证不因 OOM 失败**

### gap[] 维护

每次插入/分裂后，调 `mas_update_gap()` 从被修改的叶子向上回溯，更新沿途祖先节点的 `gap[i]`（取子节点 `gap[]` 的最大值）。额外开销 O(log N)，和插入本身同量级。

---

## 与 rb-tree 的关系

| | rb-tree（Linux < 6.1） | Maple tree（Linux ≥ 6.1） |
|---|---|---|
| 数据结构 | 二叉搜索树 | B 树变体（分支因子 10/16） |
| 范围查询 | 需要额外逻辑 | 原生支持 |
| Gap 查找 | O(N) 线性或额外维护 | O(log N)，gap[] 内置 |
| RCU 支持 | 困难 | 设计目标之一 |
| 缓存友好 | 差（指针跳转多） | 好（每个节点 256 字节，装满一个 cache line） |

rb-tree 同时还有一个 linked list 维护顺序遍历。Maple tree 统一了这两个结构。
