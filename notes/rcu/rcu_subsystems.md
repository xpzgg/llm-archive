# RCU 子模块/Feature 全景

> 本文目的：列出 RCU 是由哪些模块/特性组成的，每个特性解决什么问题、怎么解决。
>
> RCU 的核心机制（MVCC、QS、GP、回调）见 [rcu_overview.md](rcu_overview.md)。本文是"子系统分解"，回答"RCU 这套复杂代码里，每个开关/选项背后是哪一块"。

## 阅读方式

每个 feature 按三个问题展开：
1. **解决什么问题**（motivation）
2. **核心 Kconfig / API**
3. **怎么解决**（mechanism，简略）

---

## A. RCU Flavors —— 不同的 RCU 变体

不同场景对"reader 是否可以睡眠""QS 怎么判定"有不同需求，所以演化出多个 flavor。它们共享同一套 GP/QS 上报框架（rcu_state + rcu_node + rcu_data），但在 QS 判定粒度上有本质区别。

详细对比见 [rcu_overview.md](rcu_overview.md#rcu-flavors)。

| Flavor | Kconfig | 解决的问题 | 怎么解决 |
|---|---|---|---|
| **TREE_RCU**（rcu_sched） | 默认（SMP） | 经典 RCU，reader 不可抢占 | QS 粒度=CPU，context switch 即 QS |
| **TINY_RCU** | `CONFIG_TINY_RCU` | UP（单核）系统不需要 SMP 开销 | 去掉 rcu_node 树，单变量管理 QS |
| **PREEMPT_RCU**（rcu_preempt） | `CONFIG_PREEMPT_RCU` | reader 可被抢占 | QS 粒度=CPU+被阻塞任务，reader 挂到 `rnp->blkd_tasks` |
| **SRCU** | `CONFIG_SRCU` | reader 需要睡眠 | 不依赖 context switch，靠 lock/unlock 计数器 bank 切换判 QS |
| **Tasks RCU** | `CONFIG_TASKS_RCU` | 需要"任务粒度"的 QS（kprobe trampoline 回收） | 任务经过 voluntary context switch 或用户态切换即 QS |
| **Tasks Rude RCU** | `CONFIG_TASKS_RUDE_RCU` | 需要"每个 CPU 都经过 context switch"（static key / 文本段修改） | 最粗暴：向每个 CPU 发 IPI，等 reschedule |
| **Tasks Trace RCU** | `CONFIG_TASKS_TRACE_RCU` | BPF 程序安全回收（BPF trampoline） | 基于 SRCU 思路，reader 显式标记 trace |

---

## B. Callback Offloading（NOCB 体系）

**解决什么问题**：nohz_full / isolated CPU 不希望被 RCU callback 处理（softirq、kthread、cache miss）干扰。callback 处理是延迟敏感场景的大敌。

**怎么解决**：把 callback 的入队、移动、执行全部从原始 CPU 卸载到专门的 `rcuo/N` kthread。

| 子特性 | Kconfig | 说明 |
|---|---|---|
| **NOCB（offloaded callback processing）** | `CONFIG_RCU_NOCB_CPU` | 把 callback 处理卸载到 `rcuo/N` kthread |
| **NOCB default-all** | `CONFIG_RCU_NOCB_CPU_DEFAULT_ALL` | 默认所有 CPU 都启用 NOCB（6.x 主流默认） |
| **Lazy callbacks** | `CONFIG_RCU_LAZY`（合并了旧 `NOCB_CPU_LAZY`） | 回调延迟批量处理，减少 GP 次数和唤醒频率 |

这三个都属于"callback 不在原 CPU 执行"这一套，是同一设计思路的不同配置旋钮。

---

## C. GP 时长控制

**解决什么问题**：不同场景对 GP 速度的要求差异巨大——有时候要快速回收（不能等几十 ms），有时候要节流（减少 GP 开销）。

| Feature | Kconfig / API | 怎么解决 |
|---|---|---|
| **Expedited GP** | `synchronize_rcu_expedited()` / `CONFIG_RCU_EXPEDITED_*` | 通过 IPI 强制让每个 CPU 立刻经过 QS，毫秒级完成。代价是 IPI 开销 |
| **FQS（Force Quiescent State）** | 内核内部机制 | GP 跑太久时主动扫描所有 CPU 的 EQS 状态，兜底检测 |
| **GP throttling** | runtime 参数 / `gp_init_delay` | 控制 GP 启动节奏，避免 callback 太少时频繁起 GP |

---

## D. Context Tracking（EQS / DYNTICKS 都属于这套）

**解决什么问题**：CPU 进入 idle / user / guest 时，不可能处在 RCU read-side CS 里，理应是天然的 QS。但 RCU 怎么知道 CPU 处于这些状态？

**关键澄清**：EQS 不是独立 feature，是 RCU 在 Context Tracking 之上的概念。DYNTICKS 是历史名字。三者本质同一套机制。

**历史演进**：
- 早期：RCU 自己维护 `rcu_dynticks` 子系统
- 后来发现 vtime、nohz、调试子系统都需要"CPU 在 user/kernel/idle"
- 6.x 重构：`rcu_dynticks_*()` API 被通用 `ct_*()` API 替代，吸收进 `context_tracking`

| 概念 / 配置 | 角色 |
|---|---|
| **Context Tracking** | 底层子系统，追踪 CPU 在 user/kernel/idle/guest |
| **EQS（Extended Quiescent State）** | RCU 视角的解读：user/idle/guest 天然是 QS |
| **DYNTICKS** | 历史名字，已被 CT_RCU_WATCHING 等 bit 位替代 |
| `CONFIG_CONTEXT_TRACKING` / `CONTEXT_TRACKING_FORCE` | 启用 CT 子系统 |
| `CONFIG_NO_HZ_IDLE` / `NO_HZ_FULL` | tick shutdown 触发条件，依赖 CT |

---

## E. Priority Boosting

**解决什么问题**：PREEMPT_RCU 下，如果 reader 被低优先级任务阻塞（持有 read lock 的任务被低优先级 RT 任务长期抢占），GP 永远等不到 QS，造成 stall。

**怎么解决**：RCU 临时把阻塞 GP 的 reader 任务的优先级 boost 到高优先级，让它尽快被调度，释放 read lock。

| Kconfig | 含义 |
|---|---|
| `CONFIG_RCU_BOOST` | 启用 priority boosting |
| `CONFIG_RCU_BOOST_DELAY` | boost 延迟（ms），GP 等多久才开始 boost |
| `CONFIG_RCU_BOOST_PRIO` | boost 到的目标优先级 |

执行 boost 的是 `rcub/N`（rcu boost kthread）或 `rcuc/N`。

---

## F. 诊断与调试

| Feature | Kconfig | 说明 |
|---|---|---|
| **Stall detection** | `CONFIG_RCU_STALL_COMMON` / `CONFIG_RCU_CPU_STALL_TIMEOUT` | GP 超时检测 + 打印诊断信息。定位思路见 [rcu_overview.md](rcu_overview.md#rcu-stall) |
| **RCU trace events** | 无 Kconfig | tracepoint，详见 [rcu_trace_events.md](rcu_trace_events.md) |
| **debugfs 接口** | `CONFIG_RCU_EQS_DEBUG` 等 | `/sys/kernel/debug/rcu/` 暴露运行时状态 |
| **rcutorture** | `CONFIG_RCU_TORTURE_TEST` | RCU 压力测试框架，回归测试和长期稳定性验证 |

---

## G. 可扩展性配置

**解决什么问题**：CPU 数量从单核到几千核，RCU 的开销要可配置。

| Feature | Kconfig | 说明 |
|---|---|---|
| **rcu_node fanout** | `CONFIG_RCU_FANOUT` / `CONFIG_RCU_FANOUT_LEAF` | rcu_node 树层级配置。树越深并发越好但 QS 上报延迟越高 |
| **CPU hotplug 处理** | 内核机制 | CPU offline 时迁移回调、调整 `qsmaskinit` |
| **RCU_EXPERT** | `CONFIG_RCU_EXPERT` | 暴露 fanout 等 expert 选项给用户配置 |

---

## 新文章重点候选

rcu_overview 已覆盖：A（核心三个 flavor 的对比）、D（EQS/QS 机制）、F（stall 定位思路）。

新文章适合重点展开：
- **B. NOCB 体系** —— 用户点名要写，rcu_overview 只提了一句
- **E. Priority Boosting** —— rcu_overview 完全空白
- **D. Context Tracking 子系统** —— 从"子系统视角"重新讲（rcu_overview 是 RCU 视角）
