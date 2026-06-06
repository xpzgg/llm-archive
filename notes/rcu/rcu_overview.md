# RCU 内部机制总览

> 定位 RCU 问题时，你需要理解的核心机制。API 用法见 [use_of_rcu.md](use_of_rcu.md)，QS 上报细节见 [rcu_qs_reporting.md](rcu_qs_reporting.md)。

## RCU 的本质：MVCC

RCU 本质上是一种**多版本并发控制（MVCC）**。理解这一点，就能把 RCU 内部复杂的子系统收拢到两个核心机制上：

```
                        RCU = MVCC

    writer 发布新版本 ──→ reader 读当前版本 ──→ reclaimer 回收旧版本
         │                                          │
         │  rcu_assign_pointer()                    │  kfree(old)
         │                                          │
         └────── 中间这段时间就是 Grace Period ───────┘
```

MVCC 要运转，需要回答两个问题：

1. **什么时候可以回收？** — Grace Period / Quiescent State 状态管理
2. **怎么执行回收？** — Callback 机制

RCU 内部的所有复杂性——rcu_node 树、context_tracking、FQS 扫描、NOCB kthread——都是在为这两个核心机制服务。读者零开销（`rcu_read_lock()` 只操作 preempt count）是 MVCC 给的，代价全转嫁给了 writer/reclaimer 端的这两个机制。

本文围绕这两个核心机制展开。先介绍承载它们的数据结构，再逐个展开。

## 核心数据结构

RCU 的运行时状态由三个结构体承载，它们的关系是：

```
struct rcu_state (全局唯一)
  ├── node[NUM_RCU_NODES]  →  rcu_node 树 (层次化的 QS 跟踪)
  ├── gp_kthread           →  GP 内核线程
  ├── gp_seq               →  当前 GP 序列号
  └── expedited_*           →  expedited GP 相关字段

struct rcu_node (树中的节点，per-node)
  ├── qsmask               →  哪些 CPU/子节点还没上报 QS
  ├── lock                  →  保护本节点及其子树的 QS 状态
  ├── blkd_tasks            →  被 preemptible RCU 阻塞的任务
  └── parent                →  指向父节点

struct rcu_data (per-CPU)
  ├── mynode                →  指向所属的 leaf rcu_node
  ├── cpu_no_qs.b.norm      →  本 CPU 是否还没经过 QS
  ├── core_needs_qs         →  GP 是否在等本 CPU 上报 QS
  ├── cblist                →  本 CPU 的回调列表
  └── watching_snap         →  FQS 扫描用的 dynticks 快照
```

### rcu_state — 全局状态

整个 RCU 子系统只有一个 `rcu_state` 实例（`kernel/rcu/tree.c`）。它持有：

- **rcu_node 树** — 以数组形式存储的层次结构，`level[]` 指向每一层的起始节点
- **GP kthread** — 管理 Grace Period 生命周期的内核线程
- **gp_seq** — 当前 GP 的序列号，单调递增，用于判断"这个 QS 属于哪个 GP"
- **expedited 字段** — 加速 GP 的专用路径（`expedited_sequence`, `expedited_need_qs`）
- **时间戳** — `gp_start`/`gp_end`（GP 起止时间）、`jiffies_force_qs`（下次 FQS 扫描时间）、`jiffies_stall`（stall 检测时间）

根据 `CONFIG_PREEMPT_RCU` 的配置，`rcu_state.name` 是 `"rcu_preempt"` 或 `"rcu_sched"`，这个名会出现在 trace 和 stall 日志里。

### rcu_node — QS 跟踪的层次节点

rcu_node 树解决的核心问题是**scalability**：如果有几千个 CPU，不能让它们同时争抢一个全局锁来上报 QS。

树形结构让 QS 上报在叶子节点局部汇聚。只有当一个叶子节点下所有 CPU 都报完了，才需要向上传播一次——这样锁争抢从 O(N) 降到 O(log N)。

关键字段：

| 字段 | 作用 |
|------|------|
| `qsmask` | 当前 GP 中还没上报 QS 的 CPU/子节点集合。逐 bit 清零，全零则向上传播 |
| `qsmaskinit` / `qsmaskinitnext` | 本 GP 初始化时的 qsmask 值。`next` 版本供 hotplug 修改，GP 开始时拷贝到 `qsmaskinit` |
| `expmask` / `expmaskinit` | expedited GP 的等价字段 |
| `gp_seq` | 本节点感知到的 GP 序列号，用于判断"我的 QS 还有效吗" |
| `blkd_tasks` | PREEMPT_RCU 下，在 read-side CS 中被抢占的任务链表 |
| `gp_tasks` / `exp_tasks` | 指向阻塞当前 GP / expedited GP 的第一个任务 |
| `lock` | raw_spinlock，保护本节点的所有字段。叶子节点的锁也保护其下属 rcu_data 的部分字段 |
| `parent` | 指向父节点，root 节点为 NULL |

### rcu_data — per-CPU 状态

每个 CPU 一个 `rcu_data`，记录本 CPU 的 QS 状态和回调。

关键字段：

| 字段 | 作用 |
|------|------|
| `mynode` | 指向所属的 leaf rcu_node |
| `grpmask` | 本 CPU 在 `mynode->qsmask` 中的 bit 位 |
| `cpu_no_qs.b.norm` | `true` = 还没经过 QS；`false` = 已经过 QS（等 `rcu_core()` 上报） |
| `core_needs_qs` | 当前 GP 是否需要本 CPU 上报 QS |
| `gp_seq` | 本 CPU 感知到的 GP 序列号 |
| `cblist` | 分段回调列表（segcblist），按等待的 GP 排列 |
| `ticks_this_gp` | GP 开始以来经过的 tick 数（stall 日志中会打印） |
| `watching_snap` | FQS 扫描时记录的 dynticks 快照，用于检测 EQS |

### 三者的协作关系

```
                      rcu_state (全局)
                        │ gp_seq, gp_kthread
                        │
              ┌─────────┴─────────┐
              │  rcu_node 树       │
              │  (层次化 QS 跟踪)   │
              │                    │
         root rnode                │
           │    │                  │
         ┌─┘    └─┐               │
       leaf     leaf              │
        │         │                │
    ┌───┤     ┌───┤               │
   rdp  rdp  rdp  rdp             │
   (per-CPU)                       │
                                    │
   rcu_data[cpu].mynode ──────────→│ 指向 leaf rnode
   rcu_data[cpu].grpmask           │ 本 CPU 在 qsmask 中的 bit

GP 推进过程:
  1. GP kthread 在 rcu_state 层面启动 GP
  2. 遍历 rcu_node 树，设置每个节点的 qsmask
  3. 各 CPU 的 rcu_data 通过 QS 上报清除 mynode->qsmask 的对应 bit
  4. qsmask 全零时向上传播，root 全零时 GP 完成
```

## Grace Period 生命周期

GP 是 RCU 运转的核心循环。理解 GP 的完整生命周期，就理解了 RCU 的大部分行为。

```
    ┌─────────────────────────────────────────────────────────────────────┐
    │                        Grace Period 生命周期                         │
    │                                                                     │
    │  1. 启动 GP                                                          │
    │     call_rcu() / synchronize_rcu()                                  │
    │       → 唤醒 GP kthread                                              │
    │       → rcu_gp_init(): 遍历 rcu_node 树, 设置 qsmask                │
    │         (qsmask 记录哪些 CPU 还没上报 QS)                             │
    │                                                                     │
    │  2. 等待 QS                                                          │
    │     各 CPU 通过各种时机上报 QS (见下方)                                 │
    │       → rcu_report_qs_rdp() → rcu_report_qs_rnp()                  │
    │       → 沿 rcu_node 树向上清除 qsmask bit                            │
    │                                                                     │
    │  3. GP 完成                                                          │
    │     root rnode 的 qsmask == 0                                        │
    │       → 执行回调 (call_rcu 注册的函数)                                │
    │       → 或唤醒 synchronize_rcu() 的等待者                             │
    └─────────────────────────────────────────────────────────────────────┘
```

三个关键问题：
1. **怎么启动 GP？** — `call_rcu()` 注册回调，触发 GP kthread
2. **怎么知道所有 reader 退出了？** — QS（Quiescent State）上报机制
3. **GP 怎么结束？** — 所有 CPU 都上报了 QS → 执行回调

## QS（Quiescent State）机制

QS 是 RCU 最核心的概念。一个 CPU 上报 QS，意味着这个 CPU 上所有**在 GP 开始之前进入的** read-side CS 已经退出。

### QS 来源

一个 CPU 不可能在所有地方都待在 read-side CS 里。以下时机天然是 QS：

```
                    QS 来源（从 RCU 视角）

    ┌─────────────────────────────────────────────────┐
    │  Context Switch                                  │
    │  __schedule() → rcu_note_context_switch()        │
    │  CPU 一定会退出 read-side CS 才能切换任务          │
    └─────────────────────────────────────────────────┘
                        │
    ┌───────────────────┼───────────────────┐
    │                   │                   │
    ▼                   ▼                   ▼
 Idle EQS           User EQS          调度 Tick
 ct_idle_enter/exit  __ct_user_enter/exit  rcu_sched_clock_irq
 CPU 在 idle 循环    nohz_full CPU        兜底机制
 没人在读            返回用户态没人在读      周期性检查
```

**Context Switch 是最核心的 QS 来源。** 每次调度，CPU 一定不在 read-side CS 里（非 PREEMPT_RCU），所以天然是 QS。

**EQS（Extended Quiescent State）是 idle/user/guest 的统称。** CPU 在这些状态下不可能在 read-side CS 里，天然就是 QS。RCU 通过 `context_tracking` 子系统追踪这些状态转换。

**Tick 是兜底。** 即使前两个机制都失效，tick 周期性调用 `rcu_sched_clock_irq()` 也能检测并上报 QS。

### QS 上报路径

QS 上报分两条路径，理解这两条路径的区别是定位 stall 的关键：

```
路径 1: 主动上报（Context Switch）
─────────────────────────────────
rcu_note_context_switch()
  → rcu_qs()                    // 标记 cpu_no_qs.b.norm = false
  → rcu_core() 被唤醒
    → rcu_check_quiescent_state()
      → rcu_report_qs_rdp()     // 检查 QS，上报到 rcu_node 树
        → rcu_report_qs_rnp()   // 沿树向上传播


路径 2: 被动检测（EQS — idle/user/guest）
──────────────────────────────────────────
ct_kernel_exit()                // 进入 EQS
  → ct_state_inc(offset)        // 修改 state 的 CT_RCU_WATCHING 位
  → (不主动上报，只是修改了状态)

GP kthread 的 FQS 扫描:        // 定期检查
  → rcu_dynticks_fqs(rdp)
    → 对比 CT_RCU_WATCHING 值是否变化
      → 变了 = 经历过 EQS = QS
        → rcu_report_qs_rnp()   // 远程上报
```

**为什么 EQS 不主动上报？** 因为 EQS 发生时 CPU 可能在 idle 或用户态，没有合理的执行上下文来获取锁、操作 rcu_node 树。所以只是改一个原子变量，让 GP kthread 远程检测。

**为什么 context switch 可以主动上报？** 因为此时 CPU 在内核态、中断已禁用、可以安全操作 rcu_node 树的锁。

## rcu_node 树

GP 需要等所有 CPU 上报 QS。如果用一个全局锁来管理，大量 CPU 同时上报会严重争抢。rcu_node 树就是来解决这个 scalability 问题的。

```
                    rcu_get_root() (root rnode)
                    qsmask = 0b11
                   ┌────────┴────────┐
              rnode[0]            rnode[1]
              qsmask = 0b0011     qsmask = 0b0101
             ┌────┴────┐         ┌────┴────┐
           CPU0  CPU1  CPU2    CPU3   (leaf nodes)
```

**设计 tradeoff：** 树的层级越深，并发性越好（锁争抢越少），但 QS 上报延迟越高（要逐级向上传播）。实际配置根据 CPU 数量自动选择层级。

**QS 上报传播：**
```
CPU0 上报 QS:
  rcu_report_qs_rdp()
    → 获取 rnode[0] 锁
    → 清除 qsmask 中 CPU0 的 bit
    → qsmask == 0? (这个 node 下所有 CPU 都报了?)
      → 是: rcu_report_qs_rnp() 向父节点传播
      → 否: 释放锁，等其他 CPU
```

**关键位掩码：**
- `qsmask` — 当前 GP 还没上报 QS 的 CPU 集合（随着 QS 上报逐步清零）
- `qsmaskinit` / `qsmaskinitnext` — 参与 GP 的 CPU 集合（hotplug 会修改）

## 回调机制

`call_rcu(&p->rcu_head, callback)` 注册的回调在 GP 结束后执行。回调管理是另一个影响性能的关键子系统。

```
call_rcu()
  → callback 放入 per-CPU 的 segcblist
    → 按"可能在哪个 GP 完成后执行"分段
      → RCU_NEXT_TAIL: 还没关联到任何 GP
      → RCU_NEXT_READY_TAIL: 关联到下一个 GP
      → RCU_WAIT_TAIL: 等待当前 GP 完成
      → RCU_DONE_TAIL: 已完成，待执行

GP 完成时:
  → 各段向前移动 (WAIT → DONE, NEXT_READY → WAIT, ...)
  → rcu_do_batch() 执行 DONE 段的回调
```

**NOCB（no-callback CPU）：** nohz_full CPU 为了避免在回调处理时产生抖动，把回调卸载给专门的 rcuo/N kthread 处理。

## RCU Flavors

Linux 内核不止一种 RCU。不同场景对"读侧是否可以睡眠"、"QS 怎么判定"有不同需求，所以演化出多个 flavor。它们共享同一套 GP/QS 上报框架（rcu_state + rcu_node + rcu_data），但在 QS 判定粒度上有本质区别。

### Classic RCU（rcu_sched / rcu_preempt）

最核心的 flavor，保护内核数据结构（`task_struct` 指针、文件系统 dentry、网络路由表等）。

```
CONFIG_PREEMPT_RCU=n  →  rcu_sched
  rcu_read_lock()   = preempt_disable()
  QS 判定: context switch / EQS / tick
  reader 不可被抢占 → 任何 context switch 都意味着退出 read-side CS

CONFIG_PREEMPT_RCU=y  →  rcu_preempt
  rcu_read_lock()   = preempt_count += RCU_READ_LOCK_NESTING
  QS 判定: 同上，但 reader 可以被抢占
  被抢占的 reader 挂到 rnp->blkd_tasks，阻止 GP 完成
```

两者的核心区别：**rcu_sched 的 QS 粒度是 CPU（这个 CPU 经过了 context switch 就行了），rcu_preempt 的 QS 粒度是 CPU + 被阻塞的任务（即使经过了 context switch，如果有任务还在 read-side CS 里被阻塞，也不行）。**

### SRCU（Sleepable RCU）

```
API: srcu_read_lock(&ssp) / srcu_read_unlock(&ssp, idx)
     synchronize_srcu(&ssp)
     call_srcu(&ssp, &rh, func)

特点: reader 可以睡眠！
代价: 每个 srcu_struct 独立维护自己的 GP 状态，开销更大
```

Classic RCU 的 reader 不能睡眠（不能被抢占或主动 sleep），因为 RCU 把 context switch 当 QS——如果 reader 睡了，RCU 就误以为 read-side CS 结束了。

SRCU 打破了这个限制。它的做法是：**不依赖 context switch 判定 QS，而是让 reader 显式切换锁计数器的 bank（idx）**。

```
struct srcu_struct (per-instance)
  ├── srcu_node 树  (类似 rcu_node 树)
  ├── srcu_data[]   (per-CPU)
  │     ├── srcu_lock_count[2]   // 两个 bank 的锁计数
  │     └── srcu_unlock_count[2]
  └── srcu_idx       // 当前活跃的 bank 索引

srcu_read_lock():
  idx = READ_ONCE(ssp->srcu_idx)        // 读当前 bank
  this_cpu_inc(srcu_lock_count[idx])    // 对应 bank 的 lock 计数 +1
  return idx

srcu_read_unlock(ssp, idx):
  this_cpu_inc(srcu_unlock_count[idx])  // 对应 bank 的 unlock 计数 +1

GP 检测:
  1. 切换 srcu_idx (0→1 或 1→0)
  2. 等旧 bank 的 lock_count == unlock_count (所有旧 reader 退出)
  3. GP 完成
```

**tradeoff：** 因为每个 `srcu_struct` 独立维护计数器和 GP，所以 SRCU 比 Classic RCU 开销更大。只在确实需要睡眠的读侧场景使用（如设备驱动 probe/remove）。

### Tasks RCU

Classic RCU 的 QS 判定基于 CPU 状态（context switch / EQS），但有些场景需要**基于任务**判定——即使 CPU 还在内核态运行，只要某个特定任务经过了 context switch 或用户态切换，就算 QS。

```
┌─────────────────┬──────────────────────────────────────────────────────┐
│ Flavor          │ 等什么                                               │
├─────────────────┼──────────────────────────────────────────────────────┤
│ RCU Tasks       │ 任务经过 context switch 或进入用户态                   │
│ (CONFIG_TASKS_RCU)│ 用于 trampoline (kprobe/optprobe) 回收               │
├─────────────────┼──────────────────────────────────────────────────────┤
│ RCU Tasks Rude  │ 每个 CPU 都经历过 context switch                      │
│ (CONFIG_TASKS_RUDE_RCU)│ 用于更新 static keys, 重构文本段等              │
│                 │ 最粗暴: 向每个 CPU 发 IPI，等它们都 reschedule         │
├─────────────────┼──────────────────────────────────────────────────────┤
│ RCU Tasks Trace │ 任务退出所有 srcu_read_lock 区域                      │
│ (CONFIG_TASKS_TRACE_RCU)│ 用于 BPF 程序安全回收（BPF trampoline）        │
│                 │ 基于 SRCU 机制实现，reader 标记 trace                  │
└─────────────────┴──────────────────────────────────────────────────────┘
```

**Tasks RCU 和 Classic RCU 的本质区别：** Classic RCU 的 QS 是 CPU 粒度（"这个 CPU 上没有旧 reader 了"），Tasks RCU 的 QS 是任务粒度（"这个任务已经离开了可能持有旧引用的代码路径"）。这意味着 Tasks RCU 可以跟踪可能在 CPU 间迁移的任务。

**触发时机：** Tasks RCU 的 QS 在 `rcu_note_voluntary_context_switch()` 中上报，这个函数被 `rcu_sched_clock_irq()` 在 `user || idle` 时调用，也在 `rcu_note_context_switch()` 中调用。

### 各 Flavor 对比

| | Classic RCU | SRCU | Tasks RCU |
|---|---|---|---|
| reader 可否睡眠 | 否（rcu_sched）/ 是（rcu_preempt，但会被阻塞跟踪） | 是 | N/A（基于任务） |
| QS 粒度 | CPU | per-srcu_struct + CPU | 任务 |
| 实例数量 | 全局 1 个 | 每个 srcu_struct 独立 | 全局各 1 个 |
| GP 机制 | rcu_node 树 + FQS | srcu_node 树 + bank 切换 | 扫描任务列表 |
| 典型用途 | 内核数据结构 | 设备驱动（可睡眠读侧） | kprobe/BPF 回收 |

## RCU Stall

GP 迟迟无法完成时，RCU 会打印 stall 警告。定位 stall 的核心思路：

```
              GP 卡住了
                  │
     ┌────────────┼────────────┐
     │            │            │
  某个 CPU      GP kthread    回调处理
  没上报 QS     没法运行      堵住了
     │            │            │
  为什么？      为什么？      为什么？
     │            │            │
  - 死循环       - 被绑核     - rcu_do_batch
  - 关抢占太久   - 优先级低     处理太多回调
  - IRQ storm   - 被饿死
  - EQS + tick 关了
```

**stall 信息中的关键字段：**

```
rcu: INFO: rcu_sched self-detected stall on CPU
rcu:    0-...!: (1 GPs behind) idle=... softirq=... ticks_this_gp=...

                ticks_this_gp: GP 开始以来经过的 tick 数
                               大 → CPU 在 kernel 里跑了很久
                               0 → tick 可能没在跑（nohz_full?）
```

## 子文档索引

| 文档 | 内容 |
|------|------|
| [use_of_rcu.md](use_of_rcu.md) | RCU API 用法、reader/writer/reclaimer 角色 |
| [rcu_qs_reporting.md](rcu_qs_reporting.md) | QS 上报的完整调用链、context_tracking 状态机、函数入口 |
