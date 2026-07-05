# RCU FQS 机制

> 目标：说明 FQS 在 RCU GP 中承担什么职责、按什么流程判断 QS、以及在 CPU 不配合时如何催促。

## 0. 先搞清楚 FQS 在 GP 生命周期里的位置

一个 RCU Grace Period 大致经历三个阶段：

```text
1. GP init          —— 记录当前所有 CPU 都必须报一次 QS，写入 qsmask
2. GP 等待期         —— GP kthread 睡眠/唤醒，反复检查 qsmask 是否清空
3. GP 结束           —— qsmask 清空，GP 完成，回调可以执行
```

FQS 工作在第 2 阶段。GP kthread 并不会傻等，而是按一定间隔主动巡检：

```text
第一次巡检的间隔：jiffies_till_first_fqs
之后每次巡检的间隔：jiffies_till_next_fqs
```

也就是说，FQS 不是"事件触发"的，而是 GP kthread 周期性醒来做的一件事：**看看还有哪些 CPU 没交作业（QS），能不能帮它们补上，交不上的就催一催。**

## 1. FQS 要解决什么问题

正常情况下，QS 是 CPU 自己主动上报的，途径包括：

```text
context switch
scheduler tick
rcu_core() 软中断处理
进入 idle / user / guest 等 EQS
```

但有些 CPU 不会及时主动上报，典型场景：

```text
CPU 已经在 idle/user，但只是更新了 context tracking 状态，没走上报路径
CPU 配置了 nohz_full，没有周期性 tick 来触发检查
CPU 长时间停留在 kernel 态，不调度、不进 EQS（比如死循环、长时间关抢占）
```

FQS（Force Quiescent State）的职责就是兜底：

```text
GP kthread 主动巡检还没报 QS 的 CPU
能确认它已经具备 QS 条件的，替它上报
不能确认的，逐步升级催促
接近 stall 超时时，顺带收集诊断信息
```

## 2. rcu_node 树：FQS 巡检的对象是谁

RCU 用一棵 `rcu_node` 树管理所有 CPU 的 QS 状态，而不是直接扫一个全局位图：

```text
                root rcu_node
                 /         \
          leaf rnp_0     leaf rnp_1
           /    \          /    \
        CPU0   CPU1     CPU2   CPU3
```

- 每个 leaf `rcu_node` 管一小组 CPU，用 `rnp->qsmask` 记录这组里哪些 CPU 还没报 QS。
- 某个 CPU 报了 QS，先清掉 leaf 里对应的 bit；等一个 leaf 下所有 CPU 都报完，再向 root 方向逐层汇报。
- root `rcu_node` 的 qsmask 清空，意味着全局所有 CPU 都报过 QS，GP 才能真正结束。

FQS 巡检时，只关心 `rnp->qsmask` 里还亮着的那些 bit——已经清掉的 CPU 不需要再看。

## 3. FQS 的公共框架

抽象成一句话：**遍历每个 leaf rcu_node，对其中还没报 QS 的 CPU 逐个做检测，根据检测结果决定是替它报 QS，还是踹它一脚。**

```text
遍历 leaf rcu_node
  遍历 rnp->qsmask 中还没报 QS 的 CPU
    调用检测函数，得到三种结果之一：
      > 0  →  可以算 QS
        0  →  还不能算 QS，先不动
      < 0  →  还不能算 QS，且需要踢一下（resched）

批量处理：
  结果 > 0 的一批 CPU  →  rcu_report_qs_rnp() 统一上报
  结果 < 0 的 CPU      →  resched_cpu() 踢一下
```

FQS 本身只是这套遍历+分派的框架，真正的判断逻辑在检测函数里，而且**第一次 FQS 和后续 FQS 用的检测函数不一样**：

```text
第一次 FQS: rcu_watching_snap_save()      —— 拍快照
后续 FQS:   rcu_watching_snap_recheck()   —— 比快照 + 催促
```

## 4. 第一次 FQS：拍快照

第一次巡检时，RCU 对每个还没报 QS 的 CPU 做两件事：

1. 读取该 CPU 当前的 "RCU watching" 状态（即是否处于 kernel 态 / 是否在 EQS），保存为 `watching_snap`；
2. 如果发现该 CPU **此刻已经处于 EQS**（idle/user/guest），直接判定为 QS，无需等待。

```text
CPU 此刻在 EQS  →  FQS 替它报 QS，对应 bit 从 qsmask 清掉
CPU 此刻在 kernel →  暂不报 QS，只留下 watching_snap，供下一轮比较
```

有个容易忽略但很关键的设计：**GP init 阶段本身不会去扫描谁在 EQS**，只有到了第一次 FQS 才开始读取 context tracking 状态。也就是说，即使某个 CPU 从 GP 开始时就一直在 idle，也要等到第一次 FQS 巡检到它才会被发现并清掉——这是理解 GP 延迟来源的一个细节。

## 5. 后续 FQS：比快照，不行就催

### 5.1 核心判断：watching 状态是否变化过

```text
current_watching != watching_snap
  →  说明该 CPU 自上次快照后，至少跨越过一次 kernel <-> EQS 边界
  →  期间必然经过了一次天然的 QS，可以放心替它上报
```

结果同样分两种：

```text
watching 变化过  →  FQS 替它报 QS
watching 没变化  →  从 RCU 视角看，这个 CPU 一直没经历过 EQS，
                    也没主动上报，进入催促逻辑
```

这是整个 FQS 机制里最值得记住的一句话：

> **如果后续 FQS 反复发现 watching 没有变化，问题往往不是"idle 没被 RCU 发现"，而是这个 CPU 从 RCU 的角度看，压根就没经过 EQS**——它可能一直死磕在内核态，缺少调度点，或者关了抢占/中断。

### 5.2 普通催促：先打个标记

如果 CPU 迟迟不给 QS，RCU 先在它身上设置一个软标志：

```c
rcu_urgent_qs = true   // 含义：下次你有机会时，尽快给个 QS
```

对普通（有周期 tick）的 CPU，这个标志会在下一次 scheduler tick 中被看到：

```text
timer tick 触发
  → 检查到 rcu_urgent_qs
  → 顺手设置 need_resched
  → 下一个调度点自然产生 QS
```

如果这样还是拖着不给，RCU 会进一步设置：

```c
rcu_need_heavy_qs = true   // 轻量提示不够用了，要求制造一次 momentary EQS
```

### 5.3 nohz_full：没 tick，只能远程踢

如果目标是 `NO_HZ_FULL` 的 CPU，它可能压根没有周期性 tick——单靠设置 `rcu_urgent_qs` 没人会去读它。这种情况下 RCU 采取更直接的手段：

```text
nohz_full CPU 长时间没给 QS：
  1. 设置 rcu_urgent_qs
  2. 检测函数直接返回"需要 resched"
  3. FQS 框架收到后调用 resched_cpu(target_cpu)
```

`resched_cpu()` 做的事情很直白：给目标 CPU 当前运行的任务打上 `need_resched`，如果目标不是当前 CPU，通常还会发一个 reschedule IPI 去踢它一下。

**但这不是强制抢占**。如果目标 CPU 正处于不可调度区间（比如关抢占、关中断），它仍然要等到下一个安全点才能响应。resched_cpu 只是把"请求"送到，不能越过内核的调度规则。

### 5.4 接近 stall：加大力度 + 顺带诊断

如果 GP 已经快撑到 stall 超时（`rcu_cpu_stall_timeout`），FQS 会进一步升级：

```text
更高频率地调用 resched_cpu()
向目标 CPU 投递 irq_work
顺带采集：cputime / irq 次数 / softirq 次数 / context switch 次数等快照
```

区分一下两个手段的目的：

```text
resched_cpu()  →  目的是"催"，让目标尽快调度产生 QS
irq_work       →  目的是"查"，让目标 CPU 在中断上下文里执行一段诊断代码，
                   确认它是否还能响应中断（如果连 irq_work 都跑不动，
                   说明问题可能比"没让出 CPU"更严重）
```

IPI 只是底层的"发个信号过去"的通知手段；irq_work 是建立在 IPI 之上的一种"让目标 CPU 在中断侧执行回调"的机制。

## 6. Context Tracking：FQS 判断 EQS 的依据

| CPU 状态            | RCU 视角               | 含义                                               |
| ------------------- | ---------------------- | -------------------------------------------------- |
| kernel 态           | watching（RCU 在盯着） | 可能存在 RCU read-side critical section，不能算 QS |
| idle / user / guest | not watching，即 EQS   | 天然没有 RCU 临界区，等同于给了一次 QS             |

Context tracking 只负责**记录**这个状态，每次 kernel 态和 EQS 之间切换时状态翻转一次。FQS 正是靠对比这个状态来做两类判断：

```text
第一次 FQS：此刻是否已经在 EQS？
后续 FQS：  watching 状态是否和上次快照不同？
```

要注意边界：**context tracking 本身不会主动完成 GP**，它只是一个状态记录器；真正把 qsmask 里的 bit 清掉、推进 GP 的，是 FQS 或者 QS 上报路径。

## 7. 整体流程图（ASCII）

```text
                         GP kthread 周期性唤醒
                                 │
                                 ▼
                  ┌───────────────────────────┐
                  │  遍历 leaf rcu_node,       │
                  │  取出 qsmask 中还没报QS的CPU│
                  └───────────────┬───────────┘
                                  │
                    是否是本次GP的第一次FQS？
                    ┌─────────────┴─────────────┐
                   是                             否
                    │                             │
                    ▼                             ▼
        rcu_watching_snap_save()      rcu_watching_snap_recheck()
        ┌─────────────────────┐      ┌───────────────────────────┐
        │ 此刻是否已在 EQS？    │      │ current_watching 是否      │
        │                     │      │ != watching_snap ？        │
        └──────┬───────┬──────┘      └──────┬─────────────┬──────┘
              是│       │否                 是│             │否
               ▼        ▼                    ▼             ▼
          可算QS    存 watching_snap     可算QS      进入催促逻辑
               │     留给下轮比较          │                │
               │                          │                ▼
               │                          │      ┌─────────────────────┐
               │                          │      │ 设 rcu_urgent_qs     │
               │                          │      │ (nohz_full 额外触发   │
               │                          │      │  resched_cpu)        │
               │                          │      └──────────┬──────────┘
               │                          │                 │
               │                          │       仍未报QS，且接近stall？
               │                          │        ┌────────┴────────┐
               │                          │       是                  否
               │                          │        ▼                  │
               │                          │  更频繁 resched_cpu()      │
               │                          │  + 投递 irq_work 诊断      │
               │                          │        │                  │
               ▼                          ▼         ▼                  ▼
          ┌─────────────────────────────────┐   ┌───────────────────────┐
          │ rcu_report_qs_rnp() 批量上报清位 │   │ 等下一轮 FQS 再检查     │
          └─────────────────────────────────┘   └───────────────────────┘
```

## 8. 排查向导：看 FQS 行为该关注什么

围绕"当前 GP 还没报 QS 的 CPU"依次检查：

```text
1. 第一次 FQS 时，这个 CPU 是否已经处于 EQS？
2. 后续 FQS 中，它的 watching 状态是否发生过变化？
3. 如果一直没变化，是否已经出现以下升级信号：
   rcu_urgent_qs 被设置
   resched_cpu() 被频繁调用
   irq_work 被投递
   开始打印 stall 诊断信息
```

对应的结论：

```text
已经在 EQS，或 watching 变化过：
  → CPU 已经给出了 QS 的证据，FQS 会替它清位，不是问题所在

一直没变化，且升级信号逐步出现：
  → 从 RCU 视角看，这个 CPU 一直没经过 EQS
  → 大概率是：长时间纯 in-kernel 运行、缺少调度点、
    关抢占/关中断时间过长，或者 nohz_full 下没有 tick 配合
  → 这才是排查 RCU stall 时应该重点盯的 CPU
```

> 提示：`rcu_watching_snap_save` / `rcu_watching_snap_recheck` 这套函数命名对应的是内核 context_tracking 子系统从 `dynticks` 重命名为 `watching` 之后的版本。如果你在自己内核源码里搜不到这两个函数名，大概率是版本较旧，可以按同样逻辑找 `dyntick_save_progress_counter` / `rcu_implicit_dynticks_qs` 之类的旧函数名对照。
