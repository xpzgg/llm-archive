# RCU FQS 机制

> 目标：说明 FQS 在 RCU GP 中承担什么职责、按什么流程判断 QS、以及在 CPU 不配合时如何催促。
> 本文讨论普通 Tree RCU GP；具体阈值核对自 `/home/yjc/project/openeuler`（6.6）。urgent/heavy 的本地消费以 `PREEMPT_NONE` 为例。

## 关键变量速查

理解 FQS 主要关注扫描节奏、催促条件和待确认的 QS。时间变量以 jiffies 为单位，F/N/J 是下文公式的简写。

| 变量 | 作用 |
|---|---|
| `jiffies_till_first_fqs`（F） | 首次 FQS 的等待时间 |
| `jiffies_till_next_fqs`（N） | 后续 FQS 的等待间隔 |
| `jiffies_to_sched_qs`（J） | 开始请求 CPU 配合 QS 的 GP 年龄阈值 |
| `rcu_urgent_qs` | 请求目标 CPU 尽快配合产生或记录 QS |
| `rcu_need_heavy_qs` | 要求目标 CPU 提供远端 FQS 可检测的 QS 记录 |
| `qsmask` | 记录仍待上报的 CPU 或子节点；FQS 扫描 leaf 中仍置位的 CPU |
| `dynticks_snap` / `watching_snap` | 首次扫描保存的 EQS 计数快照，供后续扫描比较 |

## 0. 先搞清楚 FQS 在 GP 生命周期里的位置

一个 RCU Grace Period 大致经历三个阶段：

```text
1. GP init          —— 发布 gp_seq，用 qsmask 记录本次 GP 需要等待的 CPU
2. GP 等待期         —— GP kthread 睡眠/唤醒，反复检查 qsmask 是否清空
3. GP 结束           —— qsmask 清空且无阻塞本 GP 的 reader，完成 GP；回调随后执行
```

FQS 工作在第 2 阶段。GP kthread 按一定间隔主动巡检：

```text
第一次巡检的间隔：jiffies_till_first_fqs
之后每次巡检的间隔：jiffies_till_next_fqs
```

**FQS 负责扫描尚未确认 QS 的 CPU，并按检测结果上报 QS 或推动 CPU 提供 QS。** 扫描由等待超时、显式 FQS 请求或 callback overload 等条件触发；催促方式包括设置标志和请求重调度，后者可通过 IPI 通知目标 CPU。

### 首次 FQS 前，GP kthread 和各 CPU 分别在做什么？

GP kthread 在初始化后通常睡眠等待，超时参数为 `jiffies_till_first_fqs`。等待条件检查根节点 `qsmask`、阻塞 reader，以及 FQS 请求等。

与此同时，各 CPU 仍通过正常路径处理 QS：

```text
本地 RCU core 发现新 GP：note_gp_changes(rdp)
  → 初始化 core_needs_qs / cpu_no_qs.b.norm
调度、满足条件的 tick 等路径：rcu_qs()
  → 记录本 CPU 已经过 QS
本地 RCU core：rcu_check_quiescent_state(rdp)
  → 已记录 QS 就调用 rcu_report_qs_rdp()，向树上传播
  → 尚未记录 QS 就返回，等待后续 QS 记录
```

```text
GP 初始化 → 睡眠等待 jiffies_till_first_fqs
  ├─ QS 已报齐、无阻塞 reader → 提前醒来，直接完成 GP
  ├─ FQS 请求等              → 可以提前醒来扫描
  └─ 超时                    → 首次 FQS：取快照、确认当前 EQS
                                 ↓ 若 GP 未完成
                              等待 jiffies_till_next_fqs
                                 ↓
                              后续 FQS：比较快照，再检查催促条件
```

普通配置下，首次 FQS 前主要由各 CPU 的正常执行路径提供 QS；后续 FQS 根据年龄等条件执行催促。启用 `CONFIG_RCU_STRICT_GRACE_PERIOD` 时，GP 初始化会通过 `on_each_cpu()` 通知各 CPU。

### 三个时间变量是什么关系？

沿用开头速查表中的 F、N、J，默认公式为（单位都是 jiffies）：

```c
D = RCU_JIFFIES_TILL_FORCE_QS + nr_cpu_ids / RCU_JIFFIES_FQS_DIV;
F = N = D;
J = max(F + 2 * N, HZ / 10 + nr_cpu_ids / RCU_JIFFIES_FQS_DIV);
```

`F + 2*N` 对应第三次扫描的名义时间点，设计上先给 GP 线程机会识别 EQS，再采取主动措施。**F/N 决定扫描节奏，J 决定开始催促的 GP 年龄阈值。** 实际催促发生在严格超过阈值后的扫描中，具体时机受下限、扫描间隔和线程调度影响。

如果显式指定 `jiffies_till_sched_qs`，则 `J = jiffies_till_sched_qs`，以指定值作为阈值。F/N 也可被参数覆盖，strict GP 会改变首次等待默认值。计算采用运行时 CPU ID 范围 `nr_cpu_ids`；`CONFIG_NR_CPUS` 表示编译上限，在线 CPU 数表示当前在线数量。

## 1. FQS 要解决什么问题

必须区分“发生 QS”“本地记录 QS”和“向 RCU 树上报”。相关路径包括：

```text
context switch / 满足条件的 scheduler tick → 本地记录 QS
rcu_core()（软中断或线程）                → 处理本地记录、向树上报
进入 idle / 启用跟踪的 user / guest EQS   → 发布计数，供远端 FQS 检测
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

RCU 用一棵 `rcu_node` 树管理所有 CPU 的 QS 状态：

```text
                root rcu_node
                 /         \
          leaf rnp_0     leaf rnp_1
           /    \          /    \
        CPU0   CPU1     CPU2   CPU3
```

- 每个 leaf `rcu_node` 管一小组 CPU，用 `rnp->qsmask` 记录这组里哪些 CPU 还没报 QS。
- 某个 CPU 报了 QS，先清掉 leaf 里对应的 bit；等一个 leaf 下所有 CPU 都报完，再向 root 方向逐层汇报。
- root `rcu_node` 的 qsmask 清空表示 CPU 侧已完成汇总；`PREEMPT_RCU` 还必须等待阻塞本 GP 的 reader。

FQS 巡检 `rnp->qsmask` 中仍置位的 CPU，即本 GP 仍在等待的 CPU。

## 3. FQS 的公共框架

抽象成一句话：**遍历每个 leaf rcu_node，对其中还没报 QS 的 CPU 逐个做检测，根据检测结果决定是替它报 QS，是否请求重调度。**

```text
遍历 leaf rcu_node
  遍历 rnp->qsmask 中还没报 QS 的 CPU
    调用检测函数，得到三种结果之一：
      > 0  →  可以算 QS
        0  →  还不能算 QS；可能已置 urgent/heavy，但不请求远端重调度
      < 0  →  还不能算 QS，且需要请求重调度（resched）

批量处理：
  结果 > 0 的一批 CPU  →  rcu_report_qs_rnp() 统一上报
  结果 < 0 的 CPU      →  resched_cpu() 请求重调度
```

FQS 本身只是这套遍历+分派的框架，真正的判断逻辑在检测函数里，而且**第一次 FQS 和后续 FQS 用的检测函数不一样**：

```text
openEuler 6.6：
第一次 FQS: dyntick_save_progress_counter() —— 取快照、确认当前 EQS
后续 FQS:   rcu_implicit_dynticks_qs()       —— 比快照、按条件催促

本地较新代码的对应名称：
rcu_watching_snap_save() / rcu_watching_snap_recheck()
```

## 4. 第一次 FQS：拍快照

第一次巡检时，RCU 对每个还没报 QS 的 CPU 做两件事：

1. 读取该 CPU 当前的 "RCU watching" 状态（即是否处于 kernel 态 / 是否在 EQS），保存为 `watching_snap`；
2. 如果发现该 CPU **此刻已经处于 EQS**（idle/user/guest），直接判定为 QS，无需等待下一轮。用户态/guest 作为 EQS 的前提是对应跟踪已启用。

```text
CPU 此刻在 EQS  →  FQS 替它报 QS，对应 bit 从 qsmask 清掉
CPU 此刻在 kernel →  暂不报 QS，只留下 watching_snap，供下一轮比较
```

**首次 FQS 负责保存快照和确认当前 EQS；后续 FQS 负责比较快照并检查催促条件。**

**GP init 发布 GP 状态，首次 FQS 读取各目标 CPU 的 context tracking 状态。** 对从 GP 开始就持续处于 idle、等待远端确认的 CPU，首次 FQS 是确认其 QS 的时机；这会影响 GP 的完成延迟。

## 5. 后续 FQS：比较快照，按条件催促

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

> **对仍待上报的 CPU，watching 计数持续保持快照值，意味着 RCU 持续观察到活跃状态。** 排查时应关注长时间内核执行、调度点，以及抢占和中断状态。

### 5.2 urgent 与 heavy：请求配合与发布远端可见的 QS

后续 FQS 对仍未确认 QS 的 CPU，按以下常规阈值升级：

| 判断条件 | 动作 |
|---|---|
| `time_after(jiffies, gp_start + J)` | 设置 `rcu_urgent_qs` |
| `time_after(jiffies, gp_start + 2*J)` | 设置 `rcu_need_heavy_qs`，同时设置 urgent |

`J` 即 `jiffies_to_sched_qs`。callback overload、超过 `jiffies_resched` 等条件也可触发 heavy 分支；升级时机由这些条件和实际扫描时机共同决定。

- **urgent：请求目标 CPU 尽快配合。** tick 发现 urgent，且打断的是非 idle 的内核执行时，会设置 need-resched；`rcu_all_qs()`、调度路径也会消费 urgent。
- **heavy：要求留下远端可检测的 QS 证据。** 在 `PREEMPT_NONE` 的 `rcu_all_qs()` 或调度路径中，配合 urgent 调用 `rcu_momentary_dyntick_idle()`，推进 dynticks 计数，让后续 FQS 能直接确认。

```c
/* rcu_all_qs() 简化示意，省略抢占/中断控制和内存序 */
if (!rcu_urgent_qs)
    return;
rcu_urgent_qs = false;
if (rcu_need_heavy_qs)
    rcu_momentary_dyntick_idle();
rcu_qs();
```

`rcu_qs()` 主要清除本地 `cpu_no_qs.b.norm`，还需本地 RCU core 向树传播；momentary EQS 则让远端扫描能从 dynticks 变化直接确认。heavy 由目标 CPU 在满足 QS 条件的路径中消费。标志设置通过共享内存完成；IPI 通知由后面的重调度请求路径按条件执行。

### 5.3 nohz_full：何时恢复 tick，何时请求 IPI？

`nohz_full` CPU 的用户态 EQS 由远端 FQS 检测和报告。长期留在内核、尚未提供 QS 时，RCU 会按条件催促；从用户态进入内核通常保持原有的停 tick 状态。

设置 urgent 后，有三种配合机会：

1. CPU 自己执行到 `rcu_all_qs()` / 调度路径，消费标志。
2. 中断入口调用 `__rcu_irq_enter_check_tick()`，满足条件时设置 `TICK_DEP_BIT_RCU`，恢复 tick；tick 随后可以推动调度。确认 QS 后清除 RCU 的 tick dependency。
3. 远端扫描满足主动重调度条件，返回负值，由外层调用 `resched_cpu()`。

第三条的特殊条件为：

```c
tick_nohz_full_cpu(rdp->cpu) &&
(time_after(jiffies, rdp->last_fqs_resched + 3 * J) || rcu_state.cbovld)
```

**该分支以 `last_fqs_resched` 为时间起点，按上述时间或 overload 条件请求重调度。** 该字段会在本地认识新 GP 时初始化，并在请求重调度时更新。普通 CPU 通常由已有 tick 消费 urgent；上述特殊分支用于 nohz_full CPU。

`resched_cpu()` 请求目标 CPU 重调度，必要时发送 reschedule IPI。实际调度发生在目标 CPU 到达安全调度点之后。

### 5.4 接近 stall：加大力度 + 顺带诊断

超过 `rcu_state.jiffies_resched`（由 stall 检测时间推导，约在其一半处）后，普通 CPU 和 nohz_full CPU 都有进一步催促分支。重调度还检查 `time_after(jiffies, last_fqs_resched + J)`；irq_work 和信息采集也各有条件：

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

## 6. nohz_full 与 tick：哪些路径依赖本地 tick？

| 场景 | QS 检测与上报 |
|---|---|
| 普通 CPU 持续跑用户态，没有任务切换，且未启用用户态 EQS 跟踪 | 通常靠本地 tick 检测用户态 QS，再由 RCU core 上报 |
| nohz_full CPU 持续跑用户态 | context tracking 发布 EQS，远端 FQS 检测并报告 |
| CPU 在 idle | 远端 FQS 检测 EQS 并报告 |
| 内核发生调度切换 | 调度路径直接记录 QS，随后由本地 RCU core 上报 |
| 长时间内核执行，尚未确认 QS | urgent/heavy、恢复 tick 和有条件的重调度请求用于推动进展 |

`CONFIG_NO_HZ_FULL=y` 提供 full dynticks 支持，运行时 `tick_nohz_full_mask` 确定采用该模式的 CPU。启用 `CONFIG_CONTEXT_TRACKING_USER_FORCE` 等配置时，普通 CPU 的用户态跟踪也会不同。

Context tracking 使用**计数器**记录 EQS 的进入与退出。进入/退出 EQS 推进计数，所以即使 CPU 已回到内核，FQS 仍可从与快照的差异判断它曾经过 EQS。中断进入会临时恢复 watching，退出后才恢复相应 EQS。

```text
本地路径：rcu_qs() → 本地 RCU core → rcu_report_qs_rdp() → rcu_report_qs_rnp()
远端路径：context tracking 记录 EQS → FQS 检测 → rcu_report_qs_rnp()
```

## 7. 整体流程图（ASCII）

```text
                         GP kthread 被唤醒，执行 FQS
                                     │
                                     ▼
                       遍历 leaf rcu_node 中的 qsmask
                         检查仍待上报 QS 的 CPU
                                     │
                                     ▼
                            本次 GP 的首次 FQS？
                         ┌───────────┴───────────┐
                        是                       否
                         │                       │
                         ▼                       ▼
             rcu_watching_snap_save()  rcu_watching_snap_recheck()
                  保存计数快照                比较计数快照
                         │                       │
                         ▼                       ▼
                  当前处于 EQS？          快照后经过了 EQS？
                  ┌──────┴──────┐         ┌──────┴──────┐
                 是             否        是             否
                  │             │         │              │
                  ▼             ▼         ▼              ▼
               确认 QS       保留快照    确认 QS       检查催促条件
                  │          等待后续     │              │
                  │            扫描       │              ▼
                  │             │         │      按条件设置 urgent/heavy
                  │             │         │      满足重调度条件时
                  │             │         │        请求 resched_cpu()
                  │             │         │              │
                  ▼             ▼         ▼              ▼
                 (A)           (B)       (A)            (B)

       (A) → rcu_report_qs_rnp() 批量上报，沿 rcu_node 树清位
               → QS 汇总完成且 reader 条件满足，结束 GP
               → 仍有待完成条件，继续等待

       (B) → 继续扫描其他 CPU，随后等待 QS 上报或下一轮 FQS
```

图中 `(A)`、`(B)` 为流程连接点。首次扫描保存快照并确认当前 EQS，后续扫描比较计数，并对仍待上报的 CPU 按条件催促。具体阈值见前面的变量表和催促说明。

图中沿用较新代码的函数名；openEuler 6.6 对应 `dyntick_save_progress_counter()` / `rcu_implicit_dynticks_qs()`。各 CPU 的本地 QS 上报与 GP kthread 的等待并行进行，可推动 GP 提前完成。

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
  → CPU 已经给出了 QS 的证据，FQS 会替它清位，继续处理其他待确认 CPU

一直没变化，且升级信号逐步出现：
  → 从 RCU 视角看，这个 CPU 一直没经过 EQS
  → 大概率是：长时间纯 in-kernel 运行、缺少调度点、
    关抢占/关中断时间过长，或者 nohz_full 下没有 tick 配合
  → 这才是排查 RCU stall 时应该重点盯的 CPU
```

> 提示：`rcu_watching_snap_save` / `rcu_watching_snap_recheck` 这套函数命名对应的是内核 context_tracking 子系统从 `dynticks` 重命名为 `watching` 之后的版本。如果你在自己内核源码里搜不到这两个函数名，大概率是版本较旧，可以按同样逻辑找 `dyntick_save_progress_counter` / `rcu_implicit_dynticks_qs` 之类的旧函数名对照。


## 9. 用于 synchronize_rcu() 延迟分析

`synchronize_rcu()` 普通路径通过 `wait_rcu_gp(call_rcu_hurry)` 等待；调用耗时还包括 GP 请求、等待、回调处理和调用线程恢复运行。分析时分别记录这些阶段的时间。

延迟分析应定位耗时所在的阶段：GP 尚未启动、等待某 CPU 的 QS、GP 线程未及时运行、以及 GP 完成后的回调/唤醒延迟。记录实际 F/N/J 参数，再结合最后一个 QS 和 GP 完成时间判断。

代码索引（openEuler 6.6）：

| 文件与函数 | 核对内容 |
|---|---|
| `kernel/rcu/tree.h`：`RCU_JIFFIES_TILL_FORCE_QS`、`RCU_JIFFIES_FQS_DIV` | 默认等待参数 |
| `kernel/rcu/tree.c`：`rcu_init_geometry()`、`adjust_jiffies_till_sched_qs()` | F/N/J 默认公式 |
| 同文件：`rcu_gp_init()`、`rcu_gp_fqs_loop()`、`rcu_gp_fqs_check_wake()` | 初始化、睡眠与提前唤醒 |
| 同文件：`rcu_gp_fqs()`、`force_qs_rnp()`、`rcu_implicit_dynticks_qs()` | 首次/后续扫描、催促和重调度条件 |
| 同文件：`rcu_sched_clock_irq()`、`__rcu_irq_enter_check_tick()` | urgent 的 tick 消费与恢复 tick |
| `kernel/rcu/tree_plugin.h`：`rcu_qs()`、`rcu_all_qs()`、`rcu_note_context_switch()` | 本地 QS 与 heavy 消费 |
| `kernel/rcu/tree_stall.h`：`zero_cpu_stall_ticks()` | `last_fqs_resched` 初始化 |
| `kernel/context_tracking.c`、`kernel/time/tick-sched.c` | 用户态 EQS 与 nohz_full mask |

## 附录：当时环境的默认参数计算（原文）

以下仅为当时 `HZ=250`、运行时 `nr_cpu_ids=384` 且相关参数采用默认值的实例。其他环境按各自配置和运行时参数计算。计算原文保留如下：

```text
按代码默认公式：

  RCU_JIFFIES_TILL_FORCE_QS = 1 + (HZ > 250) + (HZ > 500)

  HZ=250：

  RCU_JIFFIES_TILL_FORCE_QS = 1

  CPU=384：

  nr_cpu_ids / RCU_JIFFIES_FQS_DIV = 384 / 256 = 1

  所以：

  first_fqs = 1 + 1 = 2 jiffies
  next_fqs  = 1 + 1 = 2 jiffies

  jiffies_to_sched_qs：

  first_fqs + 2 * next_fqs = 2 + 2*2 = 6 jiffies

  但有下限：

  HZ / 10 + nr_cpu_ids / 256
  = 250 / 10 + 384 / 256
  = 25 + 1
  = 26 jiffies

  取较大值：

  jiffies_to_sched_qs = 26 jiffies

  换成时间，HZ=250 时：

  1 jiffy = 4 ms

  最终：

  first_fqs          = 2 jiffies  = 8 ms
  next_fqs           = 2 jiffies  = 8 ms
  jiffies_to_sched_qs = 26 jiffies = 104 ms
```
