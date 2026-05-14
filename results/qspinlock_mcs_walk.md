# 从 Crash Dump 重建 Qspinlock MCS 等待队列

## 适用场景

某 CPU 发生 soft lockup，栈帧显示在 `queued_spin_lock_slowpath` 中自旋等待一把 spinlock。
你需要找出**谁在等这把锁**以及**等待的顺序**，以辅助定位持锁者。

> **提示**：如果内核开启了 `CONFIG_DEBUG_SPINLOCK`，可直接通过 `struct raw_spinlock <lock_addr>` 读取 `owner` 和 `owner_cpu` 字段，无需以下步骤。

---

## 1. 背景知识

### 1.1 Qspinlock 的 32 位值布局

x86 使用 queued spinlock（qspinlock），锁的状态存储在一个 32 位原子变量中。
对应的宏定义在 `include/asm-generic/qspinlock_types.h`，每种字段的偏移和宽度都由宏链式计算：

```c
_Q_LOCKED_OFFSET  = 0                                // bits 0-7
_Q_LOCKED_BITS    = 8
_Q_PENDING_OFFSET = _Q_LOCKED_OFFSET + _Q_LOCKED_BITS = 8   // bit 8
_Q_PENDING_BITS   = 8  (NR_CPUS < 16K)  或  1  (NR_CPUS >= 16K)
_Q_TAIL_IDX_OFFSET = _Q_PENDING_OFFSET + _Q_PENDING_BITS    // 跟随 pending 之后
_Q_TAIL_IDX_BITS   = 2
_Q_TAIL_CPU_OFFSET = _Q_TAIL_IDX_OFFSET + _Q_TAIL_IDX_BITS  // 跟随 idx 之后
_Q_TAIL_CPU_BITS   = 32 - _Q_TAIL_CPU_OFFSET
```

宏 `_Q_SET_MASK(type)` 根据上述偏移和宽度自动计算掩码，例如 `_Q_SET_MASK(LOCKED)` = `((1<<8)-1) << 0` = `0xFF`。

**当 CONFIG_NR_CPUS < 16384 时**（绝大多数系统）：

```
 31                18 17 16 15      9  8  7          0
 +-----------------+---+---+-------+---+-------------+
 |   tail cpu +1   |idx|   unused |pnd|    locked   |
 +-----------------+---+---+-------+---+-------------+
 |<--- _Q_TAIL_CPU_MASK -->|                       |
 |   _Q_TAIL_IDX_MASK |                             |
 |                  _Q_PENDING_MASK                  |
 |                         _Q_LOCKED_MASK            |

掩码:
  _Q_LOCKED_MASK    = 0x000000FF    (bits  0-7)
  _Q_PENDING_MASK   = 0x0000FF00    (bits  8-15, 其中只有 bit 8 有效, 9-15 unused)
  _Q_TAIL_IDX_MASK  = 0x00030000    (bits 16-17)
  _Q_TAIL_CPU_MASK  = 0xFFFC0000    (bits 18-31)
  _Q_TAIL_MASK      = 0xFFFF0000    (bits 16-31, = idx | cpu)
```

**当 CONFIG_NR_CPUS >= 16384 时**（特大系统）：

```
 31             11 10  9  8  7          0
 +----------------+---+---+---+-------------+
 |  tail cpu +1   | idx|pnd|    locked     |
 +----------------+---+---+---+-------------+

掩码:
  _Q_LOCKED_MASK    = 0x000000FF    (bits  0-7)
  _Q_PENDING_MASK   = 0x00000100    (bit   8)
  _Q_TAIL_IDX_MASK  = 0x00000600    (bits  9-10)
  _Q_TAIL_CPU_MASK  = 0xFFFFF800    (bits 11-31)
```

| 字段 | 位置 | 含义 |
|------|------|------|
| `locked` | bits 0-7 | 非零表示锁已被持有 |
| `pending` | bit 8 | 中间过渡状态，第二个等锁者使用 |
| `tail idx` | NR_CPUS<16K: bits 16-17; >=16K: bits 9-10 | MCS 节点的嵌套层级索引（0-3） |
| `tail cpu` | NR_CPUS<16K: bits 18-31; >=16K: bits 11-31 | MCS 队列末尾等锁者的 CPU 编号 + 1 |

关键字段含义：
- `tail`：**MCS 等待队列中最后一个（最后加入的）等锁者**。不是持锁者。
- 如果 `tail == 0`：没有 MCS 队列（可能无人等锁，或只有一个 pending 状态的等锁者）。

### 1.2 Tail 编码与解码

内核用 `encode_tail(cpu, idx)` 把 CPU 编号和嵌套索引打包进 `tail` 字段：

```c
// kernel/locking/qspinlock.h
static inline u32 encode_tail(int cpu, int idx)
{
    u32 tail;
    tail  = (cpu + 1) << _Q_TAIL_CPU_OFFSET;
    tail |= idx << _Q_TAIL_IDX_OFFSET;
    return tail;
}
```

CPU 编号存的是 `cpu + 1`（为了区分"无队列"和"CPU 0 在队尾"两种情况）。

**解码方法**：从 32 位锁值 `val` 中提取 tail cpu 和 tail idx：

```
# NR_CPUS < 16K (_Q_TAIL_CPU_OFFSET=18, _Q_TAIL_IDX_OFFSET=16):
tail_cpu = (val >> 18) - 1
tail_idx = (val >> 16) & 0x3

# NR_CPUS >= 16K (_Q_TAIL_CPU_OFFSET=11, _Q_TAIL_IDX_OFFSET=9):
tail_cpu = (val >> 11) - 1
tail_idx = (val >> 9) & 0x3
```

以下文档步骤以 NR_CPUS < 16K 为例。

### 1.3 MCS 等待队列

当多个 CPU 竞争同一把锁时，它们通过 MCS（Mellor-Crummey and Scott）队列排队：

```
持锁者(不在队列中)    Head(队首,等持锁者释放)    ...    Tail(队尾,last加入)
    │                      │                              │
    │ 释放时传给 head       │  next                        │  next = NULL
    └─────────────────────►├────────────► ... ────────────►┤
```

- 每个等锁 CPU 在 per-CPU 变量 `qnodes` 中分配一个 `mcs_spinlock` 节点。
- 节点通过 `next` 指针形成**单链表**，方向为 **Head → Tail**。
- 没有反向指针（`prev`），无法从 Tail 直接回溯到 Head。

### 1.4 mcs_spinlock 结构体（x86_64）

```c
// include/asm-generic/mcs_spinlock.h
struct mcs_spinlock {
    struct mcs_spinlock *next;   // offset 0,  8 bytes — 指向下一个等锁者
    int locked;                  // offset 8,  4 bytes — 1=已获得锁, 0=等待中
    int count;                   // offset 12, 4 bytes — 嵌套计数(idx=0节点复用)
};
// sizeof = 16 bytes
```

每个 CPU 有 4 个 qnode（对应 task / softirq / hardirq / nmi 四种上下文），存放在 per-CPU 数组 `qnodes[4]` 中。每个 qnode 大小：

- 无 `CONFIG_PARAVIRT_SPINLOCKS`：16 bytes
- 有 `CONFIG_PARAVIRT_SPINLOCKS`：32 bytes

### 1.5 为什么无法直接定位持锁者

持锁者通过 fast-path 原子操作（`cmpxchg`）将 `locked` 设为 1 即获得锁，**不进入 MCS 队列，不在锁结构中留下任何身份信息**。MCS 队列中只有等锁者。

---

## 2. 重建 MCS 队列的步骤

### 前置信息

从 soft lockup 的栈帧中获取**锁地址**（记为 `<LOCK_ADDR>`）。

**重建思路**：

整个 MCS 队列是一个由 `next` 指针串联的**单向链表**。每个 CPU 上有 4 个 MCS 节点（`qnodes[4]`，对应 task / softirq / hardirq / nmi 四种执行上下文）。等锁的 CPU 会从自己的 `qnodes` 中分配一个节点，通过 `next` 指针挂到链表尾部。

锁的 `tail` 字段编码了**链表末尾节点**所在的 CPU 编号和节点索引（idx）。由于链表是单向的（只有 `next`，没有 `prev`），从 Tail 无法直接回溯到前驱节点。

因此，重建完整链表的做法是：

1. 从锁值解码 `tail`，定位链表末尾节点（Tail）。
2. 扫描系统中所有 CPU 的所有 MCS 节点（最多 `CPU 数 × 4` 个），找出处于活跃状态的节点。
3. 根据每个活跃节点的 `next` 指针，确定节点之间的先后关系，从尾到头反推：谁的 `next` 指向 Tail，谁就是 Tail 的前驱；谁指向那个前驱，就是更前面的节点，依此类推。
4. 最终找到 Head（没有任何其他节点的 `next` 指向它）。

```
扫描范围: 所有 CPU 的 qnodes[0..3]

Tail ←── 谁的 next 指向我? ←── 谁的 next 指向我? ←── Head (没人指向它)
 (已知)       (扫描找到)            (扫描找到)           (扫描找到)
```

### 步骤 1：读取锁值

```bash
crash> struct qspinlock <LOCK_ADDR>
```

或直接读取 32 位值：

```bash
crash> rd -32 <LOCK_ADDR>
```

记下 `val` 的值（示例：`0x000b0501`）。

### 步骤 2：解码 tail

```bash
crash> eval (<VAL> >> 18) - 1    # 得到 tail_cpu
crash> eval (<VAL> >> 16) & 0x3  # 得到 tail_idx
```

示例：`val = 0x000b0501`

```
tail_cpu = (0x000b0501 >> 18) - 1 = 44 - 1 = 43
tail_idx = (0x000b0501 >> 16) & 0x3 = 11 & 0x3 = 3
```

最后一个等锁者：CPU 43, qnode index 3。

如果 `val >> 16 == 0`，说明没有 MCS 队列，以下步骤不适用。

### 步骤 3：获取 qnodes 的 per-CPU 地址

```bash
# 方法一：使用 crash 内置 per_cpu 命令
crash> per_cpu qnodes
# 列出所有 CPU 的 qnodes 起始地址

# 方法二：手动计算
crash> sym per_cpu__qnodes   # 得到静态 per-cpu 基址（记为 <BASE>）
crash> p __per_cpu_offset[<cpu>]  # 得到各 CPU 的 per-cpu 偏移
# 实际地址 = <BASE> + __per_cpu_offset[<cpu>]
```

### 步骤 4：确认 Tail 节点

用步骤 3 得到的地址验证 Tail 节点。每个 qnode 的偏移为 `idx * sizeof(qnode)`（无 PV 为 `idx * 16`，有 PV 为 `idx * 32`）：

```bash
# 无 PV 时：
crash> struct mcs_spinlock <CPU43_QNODES_ADDR> + 48    # idx=3, offset=3*16=48

# 有 PV 时：
crash> struct mcs_spinlock <CPU43_QNODES_ADDR> + 96    # idx=3, offset=3*32=96
```

预期输出：

```
struct mcs_spinlock {
  next = 0x0,       # 队尾，next 为 NULL
  locked = 0,       # 等待中
  count = 0x0,
}
```

`next == NULL` 确认这是队尾。

### 步骤 5：扫描所有活跃 MCS 节点

遍历关心的 CPU（可从 soft lockup 报错中涉及的 CPU 范围缩小），对每个 CPU 的 4 个 qnode 读取内容：

```bash
# 对每个 <CPU> 的 4 个 idx（以无 PV、sizeof(qnode)=16 为例）：
crash> struct mcs_spinlock <CPU_QNODES_ADDR> + 0      # idx=0
crash> struct mcs_spinlock <CPU_QNODES_ADDR> + 16     # idx=1
crash> struct mcs_spinlock <CPU_QNODES_ADDR> + 32     # idx=2
crash> struct mcs_spinlock <CPU_QNODES_ADDR> + 48     # idx=3
```

**活跃节点的判断条件**：`locked == 0` 且/或 `next != NULL`。

记录每个活跃节点的：
- 地址
- 所在 CPU 和 idx
- `next` 指针的值

### 步骤 6：重建链表

根据 `next` 指针将活跃节点串成链：

1. **找 Head**：在所有活跃节点中，没有任何其他节点的 `next` 指向它 → 即为 Head（队首）。
2. **从 Head 开始**，沿 `next` 指针依次走到 Tail（`next == NULL`）。

```
Head (cpu=X, idx=N)
  next → node (cpu=Y, idx=M)
    next → node (cpu=Z, idx=K)
      next → Tail (cpu=43, idx=3)
        next → NULL
```

### 步骤 7：解读结果

重建后的 MCS 队列含义：

```
持锁者（不在队列中，需通过遍历 CPU 栈帧定位）
  │
  │ 释放锁 → 传给 Head
  ▼
Head ──next──► ... ──next──► Tail
 (第一个等锁者)             (最后一个等锁者)
 (下一个获得锁的)
```

- **Head**：等待持锁者释放，是下一个获得锁的。
- **Tail**：最后加入队列的等锁者。
- **持锁者**：不在 MCS 队列中。需要遍历所有 CPU 的栈帧（`foreach bt`），找到正在该锁的临界区代码路径中执行的 CPU。

---

## 附：快速判断参考

| 锁值 (hex) | 含义 |
|-----------|------|
| `0x00000000` | 锁空闲 |
| `0x00000001` | 锁被持有，无等锁者 |
| `0x00000101` | 锁被持有，有 1 个 pending 等锁者，无 MCS 队列 |
| `0xXXXX0501` | 锁被持有，有 pending + MCS 队列，tail 在 `XXXX` |
