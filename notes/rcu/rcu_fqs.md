# RCU FQS 机制

> 目标：说明 FQS 在 RCU GP 中承担什么职责、按什么流程判断 QS、以及在 CPU 不配合时如何催促。

## 1. FQS 要解决什么问题

RCU GP 正常依赖 CPU 主动给 QS：

```text
context switch
tick
rcu_core()
idle/user/guest EQS
```

但有些 CPU 不会及时主动上报：

```text
CPU 已经在 idle/user，但只是更新了 context tracking 状态
CPU 在 nohz_full 上没有周期 tick
CPU 长时间跑在 kernel，不调度、不进 EQS
```

FQS（Force Quiescent State）的职责是：

```text
GP kthread 主动巡检还没报 QS 的 CPU
能确认 QS 的，替它上报
不能确认 QS 的，逐步催它
接近 stall 时，辅助收集诊断信息
```

## 2. FQS 的公共框架

FQS 只关心当前 GP 还在等的 CPU，也就是 `rnp->qsmask` 中的 bit。

抽象流程：

```text
遍历 leaf rcu_node
  -> 遍历 rnp->qsmask 中还没报 QS 的 CPU
  -> 对每个 CPU 调一个检测函数

检测结果：
  > 0  可以算 QS
   0  还不能算 QS
  < 0  还不能算 QS，但需要踢一下

最后：
  > 0 的 CPU 批量 rcu_report_qs_rnp()
  < 0 的 CPU 调 resched_cpu()
```

所以 FQS 本身是一个框架。真正差异在于检测函数：

```text
第一次 FQS: rcu_watching_snap_save()
后续 FQS: rcu_watching_snap_recheck()
```

## 3. 第一次 FQS：拍快照

第一次 FQS 做的是：

```text
force_qs_rnp(rcu_watching_snap_save)
```

它对每个还没报 QS 的 CPU 做两件事：

```text
1. 读取该 CPU 的 RCU watching 状态，保存为 watching_snap
2. 如果 CPU 此刻已经在 EQS，直接返回“可以报 QS”
```

结果分两种：

```text
CPU 此刻在 EQS:
  FQS 替它报 QS
  qsmask 对应 bit 被清掉

CPU 此刻还在 kernel:
  不报 QS
  只保存 watching_snap，留给后续 FQS 比较
```

关键点：

```text
GP init 不扫描 EQS
第一次 FQS 才开始读取 context tracking 状态
```

## 4. 后续 FQS：比快照，然后催促

后续 FQS 做的是：

```text
force_qs_rnp(rcu_watching_snap_recheck)
```

### 4.1 先判断是否经过 EQS

核心判断：

```text
current_watching != watching_snap
  => CPU 自第一次快照后经过 kernel <-> EQS 边界
  => 可以算 QS
```

结果分两种：

```text
watching 状态变过:
  FQS 替它报 QS

watching 状态一直没变:
  RCU 认为它还没经过 EQS，也没主动给 QS
  进入催促逻辑
```

这一步是理解 FQS 的核心：

```text
如果后续 FQS 一直看不到 watching 变化，
问题通常不是“idle 没被发现”，
而是目标 CPU 从 RCU 视角一直没进过 EQS。
```

### 4.2 普通催促：先留标记

如果 CPU 长时间没有 QS，RCU 先设置 per-CPU 标志：

```text
rcu_urgent_qs = true
```

含义：

```text
目标 CPU 下一次有机会时，请尽快给 QS。
```

普通 CPU 通常会在 scheduler tick 中看到它：

```text
timer tick
  -> 看到 rcu_urgent_qs
  -> 设置 need_resched
  -> 后续调度点产生 QS
```

如果拖得更久，RCU 还会设置：

```text
rcu_need_heavy_qs = true
```

含义是：轻量 QS 不够时，需要制造一个更强的 momentary EQS。

### 4.3 nohz_full：没 tick 就远程踢

`NO_HZ_FULL` CPU 可能没有周期 scheduler tick。

所以仅设置 `rcu_urgent_qs` 不一定够，因为目标 CPU 可能没有 timer tick 来消费这个标志。

RCU 的处理是：

```text
如果 nohz_full CPU 长时间没 QS:
  设置 rcu_urgent_qs
  返回“需要 resched”

FQS 框架收到后:
  resched_cpu(target_cpu)
```

`resched_cpu()` 的本质：

```text
给目标 CPU 当前任务设置 need_resched
如果目标 CPU 不是当前 CPU，通常发 reschedule IPI/kick
```

但这不是强制抢占一切。非抢占内核或不可调度区间里，目标 CPU 仍然要等到安全调度点。

### 4.4 接近 stall：更频繁踢 + 诊断

如果 GP 已经接近 stall timeout，FQS 会升级处理：

```text
更频繁 resched_cpu()
投递 irq_work 到目标 CPU
采集 cputime / irq / softirq / context switch 快照
```

区别：

```text
resched_cpu()
  目的：催 QS

irq_work
  目的：诊断目标 CPU 是否还能处理中断侧工作
```

IPI 是底层通知手段；`irq_work` 是“让目标 CPU 在中断侧执行一个 callback”的机制。

## 5. Context Tracking 补充

FQS 判断 EQS 依赖 context tracking。

RCU 视角下：

| CPU 状态 | RCU 视角 | 含义 |
|---|---|---|
| kernel | RCU watching | 可能存在 RCU read-side critical section |
| idle / user / guest | RCU not watching，也就是 EQS | 天然 QS |

Context tracking 记录 CPU 的 watching 状态。每次 kernel 和 EQS 边界切换，watching 状态会变化。

FQS 利用它做两类判断：

```text
第一次 FQS:
  当前是否已经在 EQS？

后续 FQS:
  watching 状态是否和快照不同？
```

重要边界：

```text
context tracking 只记录状态
它不主动完成 GP
真正清 qsmask 的是 FQS / QS 上报路径
```

## 6. 读 FQS 行为时看什么

围绕当前 GP 还没报 QS 的 CPU 看：

```text
1. 第一次 FQS 时，CPU 是否已经在 EQS？

2. 后续 FQS 中，watching 状态是否变化过？

3. 如果没变化，是否开始出现：
   rcu_urgent_qs
   resched_cpu()
   irq_work
   stall 诊断信息
```

判断方向：

```text
已经在 EQS / watching 变化过:
  说明 CPU 已经给出 QS 证据
  FQS 可以替它报 QS

一直没变化:
  说明 CPU 从 RCU 视角一直没经过 EQS
  更像是长时间 in-kernel、缺少调度点、关抢占/关中断、或 nohz_full 没 tick
```
