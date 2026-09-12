# RCU Quiescent State 上报机制

> 定位 RCU stall 时的速查手册。覆盖 QS 来源、上报路径、关键数据结构和调试入口。

## 整体架构

RCU grace period 推进依赖两个问题：
1. **哪些 CPU 已经退出 read-side CS？** — QS 上报机制回答
2. **如何处理上报？** — `rcu_report_qs_rdp()` → `rcu_report_qs_rnp()` 沿 rcu_node 树向上传播

本文聚焦问题 1：QS 从哪里来，怎么上报的。

## QS 上报时机总览

| 时机 | 触发点 | 入口函数 | 适用配置 |
|------|--------|----------|----------|
| Context switch | `__schedule()` | `rcu_note_context_switch()` → `rcu_qs()` | 所有配置 |
| Idle 进入/退出 | idle loop | `ct_idle_enter/exit()` → `ct_kernel_exit/enter()` | 所有配置 |
| User 进入/退出 | 系统调用/中断返回用户态 | `__ct_user_enter/exit()` → `ct_kernel_exit/enter()` | nohz_full |
| IRQ/NMI 进入/退出 | 硬件中断 | `ct_irq_enter/exit()` → `ct_nmi_enter/exit()` | 所有配置 |
| 调度 tick 兜底 | `update_process_times()` | `rcu_sched_clock_irq()` → `rcu_flavor_sched_clock_irq()` | 所有配置 |
| CPU offline | hotplug | `rcutree_report_cpu_dead()` → `rcu_report_qs_rnp()` | hotplug |
| CPU online | hotplug | `rcutree_report_cpu_starting()` → `rcu_report_qs_rnp()` | hotplug |
| FQS 强制扫描 | GP kthread | `force_qs_rnp()` → `rcu_dynticks_fqs()` | 所有配置 |
| GP kthread 自身上报 | GP kthread 启动 GP 时 | `rcu_qs()` + `rcu_report_qs_rdp()` | 所有配置 |

## 调用链详解

### 1. Context Switch

最核心的 QS 来源。每次调度时，如果 CPU 不在 RCU read-side CS 里，就上报 QS。

```
__schedule()                                  // kernel/sched/core.c:7043
  └─ rcu_note_context_switch(preempt)         // kernel/rcu/tree_plugin.h:324 / 995
       ├─ [PREEMPT_RCU] 在 read-side CS 内被抢占:
       │    └─ rcu_preempt_ctxt_queue()       // 把 task 挂到 rnp->blkd_tasks
       │         └─ rcu_report_exp_rdp()      // expedited GP 立即上报
       ├─ [!PREEMPT_RCU 或不在 read-side CS]:
       │    └─ rcu_preempt_deferred_qs(t)     // 处理延迟的 deferred QS
       └─ rcu_qs()                            // kernel/rcu/tree_plugin.h:298 / 950
            └─ cpu_no_qs.b.norm = false       // 标记本 CPU 已经过 QS
                 // 后续由 rcu_check_quiescent_state() 上报
```

**PREEMPT_RCU vs 非 PREEMPT_RCU 的区别：**
- 非 PREEMPT_RCU：`rcu_qs()` 直接标记 QS，因为 read-side CS 不可能跨越 context switch
- PREEMPT_RCU：reader 可被抢占，所以被抢占时 task 被挂到 `rnp->blkd_tasks`，阻止 GP 完成，直到 task 退出 read-side CS

### 2. Idle / User / Guest（Extended Quiescent State）

这些状态下 CPU 不可能在 read-side CS 里，天然就是 QS。

```
                    ┌──────────────────────────────────────┐
                    │          Extended QS Sources          │
                    │                                      │
   ct_idle_enter()  │  ct_user_enter(CT_STATE_USER)       │  context_tracking_guest_enter()
   kernel/rcu/tree.c│  kernel/context_tracking.c:536       │  include/linux/context_tracking.h
   由 idle loop 调用 │  由 syscall return / irq return 调用  │  由 KVM 调用
         │          │           │                          │
         └──────────┴───────────┼──────────────────────────┘
                                │
                    ct_kernel_exit(user, offset)   // context_tracking.c:149
                      │
                      ├─ ct->nesting != 1?  → 只减 nesting，不进入 EQS
                      │
                      ├─ ct->nesting == 1:
                      │    ├─ rcu_preempt_deferred_qs()  // 清理延迟 QS
                      │    ├─ ct_kernel_exit_state(offset)
                      │    │    └─ ct_state_inc(offset)   // state += offset
                      │    │         // 高位 (CT_RCU_WATCHING) +1: 标记进入 EQS
                      │    │         // 低位: 设为 IDLE/USER/GUEST
                      │    └─ rcu_task_exit()             // tasks RCU
                      │
                      └─ [GP kthread 通过 ct_rcu_watching_cpu() 远程检测:
                          奇数 = 在 kernel，偶数 = 在 EQS]

                    ┌──────────────────────────────────────┐
                    │    从 EQS 返回 Kernel                  │
                    └──────────────────────────────────────┘
                    ct_kernel_enter(user, offset)    // context_tracking.c:181
                      │
                      ├─ ct->nesting != 0?  → 只加 nesting，不动 EQS
                      │
                      ├─ ct->nesting == 0:
                      │    ├─ rcu_task_enter()
                      │    ├─ ct_kernel_enter_state(offset)
                      │    │    └─ ct_state_inc(offset)   // state += offset
                      │    │         // 高位 (CT_RCU_WATCHING) +1: 标记退出 EQS
                      │    │         // 低位: 归零 → CT_STATE_KERNEL
                      │    └─ ct->nesting = 1
                      │    └─ ct->nmi_nesting = CT_NESTING_IRQ_NONIDLE
```

**EQS 上报的本质：** EQS 不是主动调用 `rcu_report_qs_rdp()`，而是通过修改 `ct->state` 的 CT_RCU_WATCHING 位。GP kthread 的 FQS 扫描通过 `rcu_dynticks_fqs()` 远程检测这个计数器的奇偶性来判断 QS。

### 3. IRQ / NMI 进入退出

EQS 期间来了中断，需要临时退出 EQS，让 RCU 看到这个 CPU 活跃。

```
ct_irq_enter()                     ct_irq_exit()
  └─ ct_nmi_enter()                  └─ ct_nmi_exit()
       │                                  │
       ├─ !rcu_is_watching_curr_cpu()?    ├─ ct_nmi_nesting != 1?
       │    └─ ct_kernel_enter_state()    │    └─ nmi_nesting -= 2
       │         // 临时退出 EQS           │
       │    └─ nmi_nesting += 1           ├─ ct_nmi_nesting == 1:
       │                                  │    └─ ct_kernel_exit_state()
       ├─ already watching:               │         // 恢复 EQS
       │    └─ nmi_nesting += 2           │
       └─ rcu_irq_enter_check_tick()      └─ rcu_task_exit()
            // 确保 tick 在运行
```

**nmi_nesting 的含义：**
- `CT_NESTING_IRQ_NONIDLE = LONG_MAX/2 + 1` — 在 kernel 时的大数基线
- IRQ 从 EQS 进入：`nmi_nesting = 0 + 1 = 1`（外层 IRQ，恢复时设回 0 重新进入 EQS）
- IRQ 从 kernel 进入：`nmi_nesting = (大数) + 2`（嵌套，不会触发 EQS 恢复）
- `nmi_nesting == 1` 意味着"这是打断 EQS 的最外层中断"，退出时需要恢复 EQS

### 4. 调度 Tick 兜底（rcu_sched_clock_irq）

tick 是最后的兜底。即使 context_tracking 关了，tick 还能上报 QS。

```
update_process_times(user_tick)              // kernel/time/timer.c:2468
  └─ rcu_sched_clock_irq(user_tick)          // kernel/rcu/tree.c:2696
       │
       ├─ rcu_urgent_qs?
       │    └─ !idle && !user → set_need_resched()
       │         // 制造 context switch 来获得 QS
       │
       ├─ rcu_flavor_sched_clock_irq(user)   // tree_plugin.h:810 或 1066
       │    │
       │    ├─ [PREEMPT_RCU]:
       │    │    ├─ 在 read-side CS 内?
       │    │    │    └─ deferred QS? → set_need_resched()
       │    │    │    └─ GP 太久? → need_qs = true (rcu_read_unlock 时上报)
       │    │    ├─ 有 deferred QS?
       │    │    │    └─ rcu_preempt_deferred_qs()
       │    │    └─ 都没有 → rcu_qs()   // 直接标记 QS
       │    │
       │    └─ [!PREEMPT_RCU]:
       │         └─ user || idle || 只有 hardirq?
       │              └─ rcu_qs()             // 直接标记 QS
       │
       ├─ rcu_pending(user)?
       │    └─ invoke_rcu_core()              // 唤醒 rcuc/N kthread
       │
       └─ user || idle?
            └─ rcu_note_voluntary_context_switch()
                 └─ rcu_tasks_qs()            // Tasks RCU 上报
```

**tick 什么时候能直接上报 QS？**
- `user=1`：从用户态来的 tick，CPU 天然在 QS
- `rcu_is_cpu_rrupt_from_idle()`：从 idle 来的 tick
- 非 PREEMPT_RCU：preempt_count 只有 HARDIRQ_OFFSET 时，说明没有 read-side CS

### 5. FQS 强制扫描（Force Quiescent State）

GP kthread 等了一段时间还有 CPU 没上报 QS，就主动扫描。

```
rcu_gp_fqs_loop()                             // kernel/rcu/tree.c
  └─ force_qs_rnp(f)                           // kernel/rcu/tree.c:2732
       │
       └─ 对每个 leaf rnp:
            └─ 对 qsmask 中每个还没上报的 CPU:
                 └─ f(rdp)                     // f = rcu_dynticks_fqs
                      │
                      └─ rcu_dynticks_fqs(rdp) // 检查 dynticks 计数器
                           │
                           └─ 比较当前 ct_rcu_watching_cpu() 与 GP 开始时记录的值
                                ├─ 变化了 → 该 CPU 经历过 EQS → 报告 QS
                                │    └─ rcu_report_qs_rnp()
                                └─ 没变化 → resched_cpu()  // 抖一下这个 CPU
```

### 6. CPU Hotplug

```
CPU offline:
  rcutree_report_cpu_dead()                    // kernel/rcu/tree.c:4392
    ├─ do_nocb_deferred_wakeup()
    ├─ rcu_preempt_deferred_qs()
    ├─ rnp->qsmask & mask?  (RCU 在等这个 CPU?)
    │    └─ rcu_report_qs_rnp()               // 直接上报，不等
    └─ rnp->qsmaskinitnext &= ~mask           // 从 mask 中移除

CPU online:
  rcutree_report_cpu_starting()                // kernel/rcu/tree.c:4335
    ├─ rcu_watching_online()                   // 设置 dynticks 为活跃
    ├─ rnp->qsmaskinitnext |= mask             // 加入 mask
    ├─ rnp->qsmask & mask?  (正好在等?)
    │    └─ rcu_report_qs_rnp()               // 直接上报
    └─ rdp->beenonline = true
```

### 7. QS 上报的最终汇总

不管 QS 从哪来，最终都汇入同一个上报路径：

```
rcu_qs()                          // 标记 cpu_no_qs.b.norm = false
  → (由 invoke_rcu_core() 唤醒 rcuc kthread)
    → rcu_core()                  // kernel/rcu/tree.c:2835
      → rcu_check_quiescent_state()  // kernel/rcu/tree.c:2497
        → rcu_report_qs_rdp()     // kernel/rcu/tree.c:2443
          → rcu_report_qs_rnp()   // 沿 rcu_node 树向上传播
            → ... 直到 root rnp
              → 唤醒 GP kthread 完成 GP

// EQS 路径特殊：不经过 rcu_report_qs_rdp()，
// 而是 GP kthread 通过 FQS 扫描 rcu_dynticks_fqs() 远程检测
```

## 关键数据结构

### context_tracking（per-CPU）

```c
// include/linux/context_tracking_state.h
struct context_tracking {
#ifdef CONFIG_CONTEXT_TRACKING_USER
    bool active;     // 本 CPU 是否启用 context tracking（nohz_full 决定）
    int recursion;   // 重入保护
#endif
#ifdef CONFIG_CONTEXT_TRACKING
    atomic_t state;  // 核心：状态 + dynticks counter
#endif
#ifdef CONFIG_CONTEXT_TRACKING_IDLE
    long nesting;       // 进程嵌套层级（kernel = 1+, EQS = 0）
    long nmi_nesting;   // IRQ/NMI 嵌套层级
#endif
};
```

**state 字段布局：**

```
    MSB                                      LSB
    [ RCU watching counter ][ context_state ]
    <--- CT_RCU_WATCHING --><-- CT_STATE --->
                             bits[1:0] = {0=KERNEL, 1=IDLE, 2=USER, 3=GUEST}
    bits[高位的部分] = 每次进出 EQS 都 +1 的计数器
```

- **CT_RCU_WATCHING 位（高位）**：每次进出 kernel↔EQS 边界都 `+CT_RCU_WATCHING`（一个 bit 偏移量），所以高位会单调递增
- **奇偶含义**：高位为奇数 → 在 kernel（RCU watching），高位为偶数 → 在 EQS（RCU not watching）
- 远程检测用 `ct_rcu_watching_cpu()` 只看高位部分

**nesting 字段：**
- `nesting > 0`：CPU 在 kernel（可能 1 或更大，支持嵌套的 kernel 进入）
- `nesting == 0`：CPU 在 EQS
- 进入 EQS 前必须是 `nesting == 1`，否则只减 nesting 不真正进入 EQS

**nmi_nesting 字段：**
- `CT_NESTING_IRQ_NONIDLE = LONG_MAX/2 + 1`：在 kernel 时的大数基线
- 从 kernel 进入 IRQ/NMI：`nmi_nesting += 2`（不会触发 EQS 恢复逻辑）
- 从 EQS 进入 IRQ/NMI：`nmi_nesting = 1`（退出时检测到 == 1，恢复 EQS）
- == 1 意味着"这是打断 EQS 的最外层中断"

### rcu_data（per-CPU）中的 QS 相关字段

```c
struct rcu_data {
    bool cpu_no_qs.b.norm;     // true = 还没经过 QS，false = 已经过 QS
    bool cpu_no_qs.b.exp;      // expedited GP 的 QS 标记
    bool core_needs_qs;        // GP 需要 这个 CPU 上报 QS
    unsigned long grpmask;     // 在 rnp->qsmask 中的 bit 位置
    struct rcu_node *mynode;   // 对应的 leaf rcu_node
    // ...
};
```

## rcu_note_context_switch 完整流程图

```
                          __schedule()
                               │
                    rcu_note_context_switch(preempt)
                               │
                    ┌──────────┴──────────┐
                    │ PREEMPT_RCU?        │
                    └──────────┬──────────┘
                               │
              ┌────────────────┼────────────────┐
              │                                 │
     [在 read-side CS 内]               [不在 read-side CS]
     rcu_preempt_depth() > 0            rcu_preempt_depth() == 0
              │                                 │
     task 加入 rnp->blkd_tasks          rcu_preempt_deferred_qs()
     阻止 GP 完成                        处理 deferred QS
              │                                 │
              └────────────┬────────────────────┘
                           │
                       rcu_qs()                // 标记 QS
                           │
                  cpu_no_qs.b.norm = false
                           │
              rcu_tasks_qs(current, preempt)   // Tasks RCU
                           │
                  后续 tick 或 rcu_core() 调用
                  rcu_check_quiescent_state()
                           │
                  rcu_report_qs_rdp()           // 真正上报到 rcu_node 树
```

## RCU Stall 调试入口

定位 stall 时，关键是搞清楚哪个 CPU 没上报 QS，以及为什么。

### stall 检测入口

```
rcu_check_gp_kthread_starvation()             // tree_stall.h:569
  // 检查 GP kthread 是否饿死（长时间没运行）

print_cpu_stall_info(cpu)                      // tree_stall.h:518
  // 打印每个 CPU 的 stall 信息：
  //   - ticks_this_gp: GP 开始以来经过的 tick 数
  //   - cpu_no_qs: 是否还没经过 QS
  //   - softirq: rcu_sched 是否 pending
  //   - ct_state (dynticks): 当前 context tracking 状态

rcu_dump_cpu_stacks(gp_seq)                    // tree_stall.h:396
  // dump 所有还没上报 QS 的 CPU 的 stack
```

### 常见 stall 场景和排查思路

| 现象 | 可能原因 | 排查方向 |
|------|----------|----------|
| CPU 长期在 kernel，没经过 context switch | 死循环 / 长时间关抢占 | 看 `rcu_dump_cpu_stacks` 的栈，找死循环 |
| CPU 在 EQS（idle/user）但没上报 | nohz_full 下 tick 关了 | 看 `ct_state` 是否真的是 EQS，检查 FQS 是否在扫描 |
| CPU 有 pending deferred QS | PREEMPT_RCU 下 read-side CS 太长 | 看 `rcu_preempt_depth`，找谁持有 read lock |
| GP kthread 饿死 | 优先级太低 / 被绑定 CPU | `rcu_check_gp_kthread_starvation` |
| CPU offline 但没清理 | hotplug 竞争 | 看 `rcutree_report_cpu_dead` 是否被调用 |

## CONFIG 依赖关系

```
CONFIG_NO_HZ_FULL
  → 强制开启 CONFIG_CONTEXT_TRACKING
    → 强制开启 CONFIG_CONTEXT_TRACKING_USER
      → 强制开启 CONFIG_CONTEXT_TRACKING_IDLE

非 nohz_full 配置下 context_tracking 可以不开，
RCU 退回到依赖 tick 上报 QS 的模式。
```

## 关键文件索引

| 文件 | 职责 |
|------|------|
| `kernel/context_tracking.c` | CT 状态机核心，EQS 进入/退出 |
| `kernel/rcu/tree.c` | RCU tree 核心逻辑，GP 管理，QS 上报 |
| `kernel/rcu/tree_plugin.h` | PREEMPT_RCU / !PREEMPT_RCU 的 flavor 差异 |
| `kernel/rcu/tree_stall.h` | stall 检测和报告 |
| `kernel/rcu/tree_nocb.h` | no-callback CPU（nocb）支持 |
| `kernel/rcu/tasks.h` | Tasks RCU 实现 |
| `kernel/sched/core.c` | 调度器，调用 `rcu_note_context_switch()` |
| `kernel/time/timer.c` | tick，调用 `rcu_sched_clock_irq()` |
| `include/linux/context_tracking.h` | CT 对外接口 |
| `include/linux/context_tracking_state.h` | CT 状态定义和访问器 |
