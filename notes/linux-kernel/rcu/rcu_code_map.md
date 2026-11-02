# RCU 代码地图（Tree RCU）

> 配合两张图使用：[rcu.excalidraw](rcu.excalidraw)（第一阶段：总览）、[rcu-lifecycle.excalidraw](rcu-lifecycle.excalidraw)（第二阶段：生命周期主线 + 数据结构总图 + 十个机制）。本文的"主线"和"机制 1~10"与第二张图一一对应。
> 本文是查代码用的索引：每个机制给出入口、关键函数链和"状态在哪一步改变"。行号基于 **v7.3-rc7**，换版本后函数名大多还在，行号会漂移，以函数名 grep 为准。

文件分工：

| 文件 | 内容 |
|---|---|
| `kernel/rcu/tree.c` | GP kthread、QS 上报、FQS、回调推进与执行、CPU 热插拔 |
| `kernel/rcu/tree_plugin.h` | 可抢占 RCU 的读侧、被抢占读者、优先级提升 |
| `kernel/rcu/tree_exp.h` | expedited GP |
| `kernel/rcu/tree_nocb.h` | NOCB（回调卸载） |
| `kernel/rcu/tree_stall.h` | stall 检测与输出 |
| `kernel/rcu/rcu_segcblist.c` | 回调分段链表 |
| `kernel/rcu/tree.h`、`kernel/rcu/rcu.h` | `rcu_state` / `rcu_node` / `rcu_data` 定义；gp_seq 工具函数 |

## 主线：一个 GP 的生命周期

### ① 登记

- 入口：`call_rcu()`
- 链：`__call_rcu_common()`（tree.c:3152）→ 卸载到 NOCB？`call_rcu_nocb()`（见机制 8）；否则 `call_rcu_core()`（tree.c:3059）→ `rcutree_enqueue()`
- 状态变化：回调挂进 `rdp->cblist` 的 NEXT 段
- 边界：关中断入队；积压超过 `qhimark`（10000）时立即加速或触发 FQS；`kvfree_rcu()` 在 `mm/slab_common.c` 攒批后再登记
- 观测：`rcu:rcu_callback`

### ② 请求 GP

- 入口：tick → `rcu_sched_clock_irq()`（tree.c:2729）→ `rcu_pending()`（tree.c:3734）→ `invoke_rcu_core()`（tree.c:2965）
- 链：`rcu_core()`（tree.c:2868）→ `rcu_accelerate_cbs()`（tree.c:1139）→ `rcu_segcblist_accelerate()`（rcu_segcblist.c:544）→ `rcu_start_this_gp()`（tree.c:1014）→ `rcu_gp_kthread_wake()`（tree.c:1115）
- 状态变化：回调被分配 GP 号；`gp_seq_needed` 从 leaf 往 root 逐层写入；root 处设 `RCU_GP_FLAG_INIT`
- 其他入口：`note_gp_changes()`（tree.c:1318）；`rcu_gp_cleanup()` 里的 needgp
- 观测：`rcu:rcu_future_grace_period`

### ③ GP 初始化（GP kthread）

- 链：`rcu_gp_kthread()`（tree.c:2304）→ `rcu_gp_init()`（tree.c:1831）
- 步骤与状态变化：
  1. `record_gp_stall_check_time()`（tree_stall.h:201）：设 `jiffies_stall`
  2. `rcu_seq_start(&rcu_state.gp_seq)`：GP 号进入"进行中"
  3. 遍历 leaf：`qsmaskinit = qsmaskinitnext`（吸收 CPU 上下线）
  4. 广度优先遍历：`rnp->qsmask = qsmaskinit`、`rnp->gp_seq = 新号`
  5. 本 CPU：`rcu_qs()` + `rcu_report_qs_rdp()`
- 观测：`rcu:rcu_grace_period`（start）、`rcu:rcu_grace_period_init`

### ④ 等待 QS

两条路并行。

**CPU 自己上报：**
`rcu_qs()`（tree_plugin.h:298，清 `cpu_no_qs.b.norm`）→ tick 时 `rcu_pending()` → `rcu_core()` → `rcu_check_quiescent_state()`（tree.c:2531）→ `rcu_report_qs_rdp()`（tree.c:2476，清 leaf 上本 CPU 的位）→ `rcu_report_qs_rnp()`（tree.c:2372，逐级向上）→ `rcu_report_qs_rsp()`（tree.c:2348，设 `RCU_GP_FLAG_FQS` 并唤醒 GP kthread）

**GP kthread 的 FQS：**
`rcu_gp_fqs_loop()`（tree.c:2091）→ `rcu_gp_fqs()`（tree.c:2055）→ `force_qs_rnp()`（tree.c:2765），对每个未报 CPU 调：
- 第一次 `rcu_watching_snap_save()`（tree.c:820）：此刻在 EQS 就替它报
- 之后 `rcu_watching_snap_recheck()`（tree.c:856）：进出过 EQS 就替它报，否则逐级催促（机制 4）

- 关键检查：`rdp->gp_seq != rnp->gp_seq` 时本次 QS 作废
- 观测：`rcu:rcu_quiescent_state_report`、`rcu:rcu_fqs`、`rcu:rcu_grace_period`（fqswait / fqsstart / fqsend）

### ⑤ GP 结束（GP kthread）

- 链：`rcu_gp_cleanup()`（tree.c:2177）
- 步骤与状态变化：
  1. 广度优先遍历：`rnp->gp_seq = 新号`；本 CPU `__note_gp_changes()`；`rcu_future_gp_cleanup()`；`rcu_nocb_gp_cleanup()`（tree_nocb.h:183）
  2. `rcu_seq_end(&rcu_state.gp_seq)`：GP 正式结束
  3. needgp → `gp_flags = RCU_GP_FLAG_INIT`
  4. `rcu_sr_normal_gp_cleanup()`（tree.c:1712）：直接唤醒 `synchronize_rcu()` 等待者
- 观测：`rcu:rcu_grace_period`（end）

### ⑥ 回调推进与执行

- 链：`rcu_pending()` → `invoke_rcu_core()` → `rcu_core()` → `note_gp_changes()` / `rcu_advance_cbs()`（tree.c:1215）→ `rcu_segcblist_advance()`（rcu_segcblist.c:501）→ `rcu_do_batch()`（tree.c:2573）→ `rcu_segcblist_extract_done_cbs()`（rcu_segcblist.c:375）
- 状态变化：到期的段并入 DONE；DONE 段被摘下执行
- 限流：`blimit`（默认 10，积压时 10000）、`rcu_resched_ns` 时间上限
- 执行上下文：`RCU_SOFTIRQ`，或 `rcuc/N`（`rcutree.use_softirq=0`）
- 观测：`rcu:rcu_batch_start`、`rcu:rcu_invoke_callback`、`rcu:rcu_batch_end`

## 机制

### 机制 1 · 回调管理

- 结构：`struct rcu_segcblist`（`include/linux/rcu_segcblist.h`）：`head` + `tails[4]` + `gp_seq[4]`（每项 `{norm, exp}`）+ `len` / `seglen[4]` + `flags`（`SEGCBLIST_ENABLED` / `SEGCBLIST_OFFLOADED`）
- 段：`RCU_DONE_TAIL` / `RCU_WAIT_TAIL` / `RCU_NEXT_READY_TAIL` / `RCU_NEXT_TAIL`；`tails[i]` 指向第 i 段最后一个回调的 `next` 字段
- 操作（`kernel/rcu/rcu_segcblist.c`）：
  - `rcu_segcblist_enqueue()`（:330）：接到 NEXT 段尾
  - `rcu_segcblist_accelerate()`（:544）：从后往前找第一个"非空且 GP 号小于需要号"的段，后面全部并成一段并写上需要号
  - `rcu_segcblist_advance()`（:501）：GP 号已完成（普通或 expedited 任一）的段并入 DONE，再 compact
  - `rcu_segcblist_extract_done_cbs()`（:375）/ `rcu_segcblist_insert_done_cbs()`（:432）：摘下 DONE 段执行 / 没执行完放回
  - `rcu_segcblist_entrain()`（:350）：接到最后一个非空段末尾（只给 `rcu_barrier` 用）
- 为什么回调要等"登记之后才开始"的 GP：`rcu_seq_snap()` 的取值规则（机制 2）
- 执行限流：`rcu_do_batch()`（tree.c:2573），`bl = max(blimit, pending >> rcu_divisor)`；`blimit` 10 / `qhimark` 10000 / `qlowmark` 100；softirq 中另有 `rcu_resched_ns`（3 ms）时间上限
- `rcu_barrier()`（tree.c:3893）→ 对在线 CPU 发 IPI `rcu_barrier_handler()`（tree.c:3867），离线 CPU 直接调用 → `rcu_barrier_entrain()`（tree.c:3830）塞屏障回调；`barrier_cpu_count` 归零即 complete
- lazy（`CONFIG_RCU_LAZY`，依赖 NOCB）：先进 bypass，最长 `LAZY_FLUSH_JIFFIES`（10 秒）

### 机制 2 · GP 的表示与推进

- 编码：`gp_seq = (计数 << RCU_SEQ_CTR_SHIFT) | 状态`，`RCU_SEQ_CTR_SHIFT = 2`（`include/linux/rcupdate.h`）；`rcu_seq_start / end / snap / done`（`kernel/rcu/rcu.h`）
- 三级副本：`rcu_state.gp_seq` → 每个 `rnp->gp_seq`（GP kthread 在 init / cleanup 中写）→ `rdp->gp_seq`（CPU 自己在 `note_gp_changes()` 里追）
- 追赶：`__note_gp_changes()`（tree.c:1269）：有 GP 结束 → `rcu_advance_cbs()`；有新 GP 开始 → 按 leaf `qsmask` 里自己的位重设 `cpu_no_qs.b.norm` / `core_needs_qs`；最后 `rdp->gp_seq = rnp->gp_seq`
- 漏斗请求：`rcu_start_this_gp()`（tree.c:1014），沿树写 `gp_seq_needed`，遇到已请求过同一个或更晚 GP 就停
- GP kthread 状态（`rcu_state.gp_state`，定义在 tree.h，stall 输出打印名字）：`RCU_GP_WAIT_GPS` → `RCU_GP_DONE_GPS` → `RCU_GP_ONOFF` → `RCU_GP_INIT` → `RCU_GP_WAIT_FQS` ⇄ `RCU_GP_DOING_FQS` → `RCU_GP_CLEANUP` → `RCU_GP_CLEANED`；`RCU_GP_IDLE` 表示没有 GP 在进行
- `gp_flags`：`RCU_GP_FLAG_INIT`（要开新 GP）/ `RCU_GP_FLAG_FQS`（要立即做 FQS）/ `RCU_GP_FLAG_OVLD`（回调积压）

### 机制 3 · QS 跟踪

- 一个 CPU 在一个 GP 里：还不知道新 GP → 欠 QS（`core_needs_qs = cpu_no_qs.b.norm = 1`）→ 已发现 QS（`rcu_qs()` 清 `cpu_no_qs.b.norm`）→ 已上报（`rcu_report_qs_rdp()` 清 leaf 位）；FQS 可以从"欠 QS"直接替它上报
- QS 来源：

| 来源 | 入口 |
|---|---|
| 上下文切换 | `rcu_note_context_switch()`（tree_plugin.h:324）→ `rcu_qs()` |
| tick | `rcu_sched_clock_irq()` → `rcu_flavor_sched_clock_irq()`（tree_plugin.h:836） |
| EQS（idle / 用户态 / guest） | context tracking 改 watching 计数，由 FQS 判定（机制 4） |
| 读侧退出补报 | `__rcu_read_unlock()`（tree_plugin.h:430）→ `rcu_read_unlock_special()`（tree_plugin.h:751）→ `rcu_preempt_deferred_qs_irqrestore()`（tree_plugin.h:478） |

- 上报：`rcu_report_qs_rnp()`（tree.c:2372）自下而上逐级换锁，同一时刻只持一把；root 清零 → `rcu_report_qs_rsp()`（tree.c:2348）
- 不变量：`qsmask` 只在 `rnp->lock` 下改；节点清零时子节点必然已清零；`rdp->gp_seq != rnp->gp_seq` 或 `gpwrap` 时上报作废

### 机制 4 · EQS 与 FQS

- 计数：`context_tracking.state` 的高位是 RCU watching 计数，最低一位为 `CT_RCU_WATCHING`（`include/linux/context_tracking_state.h`）；进出 EQS 时 `ct_kernel_exit_state()` / `ct_kernel_enter_state()` 加 `CT_RCU_WATCHING`
- 判定：`rcu_watching_snap_save()`（tree.c:820）此刻在 EQS 就替报，否则记快照；`rcu_watching_snap_recheck()`（tree.c:856）快照后变过就替报，否则按时间逐级催促（`rcu_urgent_qs` → `rcu_need_heavy_qs` → `resched_cpu()` → `irq_work`）
- 驱动：`rcu_gp_fqs_loop()`（tree.c:2091）按 `jiffies_till_first_fqs` / `jiffies_till_next_fqs` 唤醒 → `rcu_gp_fqs()`（tree.c:2055）→ `force_qs_rnp()`（tree.c:2765）

### 机制 5 · 可抢占读者

- 读侧：`__rcu_read_lock()`（tree_plugin.h:412）给 `current->rcu_read_lock_nesting` 加 1
- `union rcu_special`：`blocked` / `need_qs` / `exp_hint` / `need_mb`（`include/linux/sched.h`）
- 被抢占：`rcu_note_context_switch()` → `rcu_preempt_ctxt_queue()`（tree_plugin.h:162），挂到 `rnp->blkd_tasks`，按 4 种情况决定插入位置及是否移动 `gp_tasks` / `exp_tasks`
- 退出：`rcu_read_unlock_special()` 不能立即处理时用 `RCU_SOFTIRQ` / `need_resched` / `irq_work` 延迟（`rdp->defer_qs_pending`）；`rcu_preempt_deferred_qs_irqrestore()` 出链表，最后一个挡路者触发 `rcu_report_unblock_qs_rnp()`
- 提升：`rcu_initiate_boost()`（tree_plugin.h:1285）→ `rcub/N` → `rcu_boost()`（tree_plugin.h:1180），`rt_mutex_init_proxy_locked()` + `rt_mutex_lock()` 借 PI 提升读者
- 观测：`rcu:rcu_preempt_task`、`rcu:rcu_unlock_preempted_task`

### 机制 6 · 等 GP 的几种方式

- `synchronize_rcu()`（tree.c:3400）：`rcu_blocking_is_gp()` → 直接返回；`rcu_gp_is_expedited()` → 机制 7；否则 `synchronize_rcu_normal()`（tree.c:3327）
- 快速唤醒（默认，`rcu_normal_wake_from_gp = 1`；同时等待者超过 64 个时临时退回）：`rcu_sr_normal_add_req()` + `start_poll_synchronize_rcu()`，由 `rcu_sr_normal_gp_cleanup()`（tree.c:1712）在 GP 结束时直接唤醒
- 退回路径：`wait_rcu_gp(call_rcu_hurry)`，等回调 `wakeme_after_rcu()` 执行
- 轮询：`get_state_synchronize_rcu()` / `start_poll_synchronize_rcu()` / `poll_state_synchronize_rcu()` / `cond_synchronize_rcu()`；`*_full` 版本同时记普通和 expedited 的号
- 观测：`rcu:rcu_sr_normal`

### 机制 7 · expedited GP

`synchronize_rcu_expedited()`（tree_exp.h:929）→ `rcu_exp_gp_seq_snap()` + `exp_funnel_lock()` → `rcu_exp_gp_kthread_worker` → `sync_rcu_exp_select_cpus()`（tree_exp.h:505）→ `sync_rcu_exp_select_node_cpus()`（tree_exp.h:450，IPI）→ `rcu_exp_handler()`（tree_exp.h:754）→ `rcu_exp_wait_wake()`（tree_exp.h:691）/ `synchronize_rcu_expedited_wait()`（tree_exp.h:626）

独立字段：`rnp->expmask`、`rnp->exp_tasks`、`rcu_state.expedited_sequence`、`rdp->cpu_no_qs.b.exp`。观测：`rcu:rcu_exp_grace_period`、`rcu:rcu_exp_funnel_lock`

### 机制 8 · NOCB

`call_rcu_nocb()`（tree_nocb.h:659）→ `rcu_nocb_try_bypass()`（tree_nocb.h:485）→ `rcuog/N`：`nocb_gp_wait()`（tree_nocb.h:712）→ GP 结束时被 `rcu_nocb_gp_cleanup()`（tree_nocb.h:183）唤醒 → `rcuo<p|s>/N`：`nocb_cb_wait()`（tree_nocb.h:939）→ `rcu_do_batch()`。cblist 由 `rdp->nocb_lock` 保护。观测：`rcu:rcu_nocb_wake`

### 机制 9 · stall 检测

- 期限：`record_gp_stall_check_time()`（tree_stall.h:201），默认 21 秒（`CONFIG_RCU_CPU_STALL_TIMEOUT`）
- 检查：每个 tick 的 `rcu_pending()` → `check_cpu_stall()`（tree_stall.h:776）
- 输出：`print_cpu_stall()`（tree_stall.h:711，self-detected）/ `print_other_cpu_stall()`（tree_stall.h:630）；告警后期限变为 3 倍
- 其他：`rcu_check_gp_kthread_starvation()`（tree_stall.h:568）、`rcu_check_gp_start_stall()`
- 读输出：`fqs=` 是本轮 FQS 次数（为 0 先怀疑 GP kthread 没跑）；`gp_state` 名字告诉你 GP kthread 停在哪一步

### 机制 10 · CPU 上下线

- 上线：`rcutree_report_cpu_starting()`（tree.c:4411）置 `qsmaskinitnext`
- 下线：`rcutree_report_cpu_dead()`（tree.c:4468）清 `qsmaskinitnext`，必要时替它报 QS；`rcutree_migrate_callbacks()`（tree.c:4523）搬回调
- 生效：`rcu_gp_init()` 开头（持 `ofl_lock`，`gp_state = RCU_GP_ONOFF`）`qsmaskinit = qsmaskinitnext`

## 并发规则速查

- `rnp->lock`（raw spinlock，关中断持有）：本节点的 `qsmask`、`gp_seq`、`gp_seq_needed`、`blkd_tasks` 及其指针；root 的锁兼管 `gp_flags`
- `rcu_data` 多数字段只由本 CPU 修改（关中断 / 关抢占即可）；NOCB 模式下 cblist 用 `rdp->nocb_lock`
- `gp_seq` / `gp_state` 只有 GP kthread 写（`ASSERT_EXCLUSIVE_WRITER`）
- `ofl_lock`：GP 初始化 vs CPU 上下线；`exp_mutex`：expedited；`barrier_mutex` / `barrier_lock`：`rcu_barrier`
- 内存序设计：`Documentation/RCU/Design/Memory-Ordering`

## 工具

- trace 事件：`include/trace/events/rcu.h`，用法见 [rcu_trace_events.md](rcu_trace_events.md)
- rcutorture：`tools/testing/selftests/rcutorture/bin/kvm.sh --allcpus --duration 10 --configs TREE01`
- 设计文档：`Documentation/RCU/Design/`
