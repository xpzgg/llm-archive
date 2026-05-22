# Linux 内核卡死检测机制速查手册

> 面向下游的快速参考，遇到 "lockup"、"stall"、"hung" 类告警时按此文档自查。

---

## TLDR

| 检测机制 | 一句话含义 | 典型根因 |
|---|---|---|
| **Hard Lockup** | CPU 长时间关中断（连 NMI 都进不来） | `spin_lock_irq` 死循环、IRQ handler 卡死 |
| **Soft Lockup** | CPU 在内核态长时间不调度 | 内核忙循环且无 `cond_resched()`、长时间 `preempt_disable` |
| **RCU Stall** | CPU 长时间不进入安静态（quiescent state） | 长关中断、长关抢占、内核死循环、长关 BH |
| **Hung Task** | 任务在 D 状态（不可中断睡眠）太久 | 等 I/O 无响应、等锁、等内核资源 |

**快速判断：**
- 有 `NMI` 字样 → Hard Lockup
- 有 `BUG: soft lockup` → Soft Lockup
- 有 `rcu_sched` / `rcu_preempt` detected stalls → RCU Stall
- 有 `INFO: task blocked for more than` → Hung Task

---

## 1. Hard Lockup

### 是什么

CPU 在某个核心上**关中断**时间过长，连 NMI（不可屏蔽中断）都无法正常触发 watchdog。此时该 CPU 完全无响应，外部看就是"死核"。

### 根因

- 在 `spin_lock_irq()` / `local_irq_disable()` 保护区内**死循环或长时间运算**
- IRQ handler 本身卡死
- 固件/硬件问题导致 CPU 卡在不可中断状态

### 接口

```bash
# 总开关（同时控制 hard 和 soft lockup detector）
cat /proc/sys/kernel/watchdog
# 0 = 全关, 1 = 全开

# hard lockup 独立开关
cat /proc/sys/kernel/nmi_watchdog
# 0 = 关闭, 1 = 开启（基于 PMU 的 perf hardlockup detector）

# 阈值：hard lockup 的检测间隔由 watchdog_thresh 间接控制，
# 实际 hard lockup 超时 = watchdog_thresh * 2（秒），默认 20s
cat /proc/sys/kernel/watchdog_thresh
# 默认 10，即 hard lockup 阈值约 20s

# 触发 panic 与否
cat /proc/sys/kernel/hardlockup_panic
# 0 = 不 panic（默认）, 1 = panic 并按 kernel.panic 策略处理
```

### 如何确认中断是否被屏蔽

Hard lockup 的核心特征是中断被关闭。可以从 stack trace 中直接看 PSTATE 寄存器的 DAIF 位：

```
# crash> bt 或 dmesg 中的 stack trace 里会显示 pstate
pstate: 804003c9 (exceptions masked)
```

DAIF 四位含义（ARM64）：

| 位 | 大写（=1，屏蔽） | 小写（=0，开启） | 含义 |
|---|---|---|---|
| **D** | D | d | Debug 异常屏蔽 |
| **A** | A | a | SError（系统错误）屏蔽 |
| **I** | I | i | **IRQ 屏蔽**（最关键） |
| **F** | F | f | FIQ 屏蔽 |

**判断方法：** 看 pstate 值中的 `I` 位。如果显示大写 `I`（如 `...c9` 中 I=1），说明中断被屏蔽了，符合 hard lockup 的特征。如果显示小写 `i`，说明中断是开着的，可能不是 hard lockup 而是其他问题。

### 排查思路

1. **先看是不是偶发：** 如果只出现一次且系统恢复正常，可能是瞬时压力。多次复现才有分析价值。
2. **加大阈值测试：** 把 `watchdog_thresh` 从 10 调到 30（hard lockup 阈值变 60s），如果不再报，说明**不是死锁，是关中断时间确实长**（如大规模内存操作、重 I/O 路径）。此时应优化代码而非调参。
3. **如果加大阈值仍报：** 大概率是真死锁或硬件问题，需要看 NMI stack trace 定位卡在哪里。
4. **perf hardlockup detector 与 PMU 相关：** 虚拟化/容器环境下可能不工作，此时可用 `hardlockup_detector_perf` 相关参数调整。

---

## 2. Soft Lockup

### 是什么

CPU 在**内核态**持续运行超过阈值，期间没有发生过**调度**（没调用 `schedule()`）。中断是开着的（所以不触发 hard lockup），但 CPU 被"霸占"了。

### 根因

- 内核代码中的**忙循环**（`while(1)` 式循环）没有插入 `cond_resched()` 或 `cpu_relax()`
- 长时间 `preempt_disable()` 区间
- 大数组遍历、复杂计算等耗时操作且未主动让出 CPU

### 接口

```bash
# soft lockup 阈值（秒），默认 = watchdog_thresh（10s）的 2 倍即 20s
# 实际 soft lockup 超时 = watchdog_thresh * 2
cat /proc/sys/kernel/watchdog_thresh
# 修改示例：设为 30（soft lockup 阈值变 60s）
echo 30 > /proc/sys/kernel/watchdog_thresh

# 触发 panic 与否
cat /proc/sys/kernel/softlockup_panic
# 0 = 不 panic（默认）, 1 = panic 并按 kernel.panic 策略处理

# 是否打印所有 CPU 的 backtrace（不只报卡死的那个）
cat /proc/sys/kernel/soft_lockup_all_cpu_backtrace
```

### 排查思路

1. **看 stack trace：** 日志中 `BUG: soft lockup - CPU#X stuck for Xs!` 后面跟着的就是卡死位置的调用栈。直接看最内层的函数。
2. **判断是忙还是死：**
   - 如果堆栈在循环类函数（`list_for_each_entry` 等）→ 大概率是数据量大、循环久，**加阈值能缓解**，根本解决要优化代码。
   - 如果堆栈在锁操作（`spin_lock` 等）→ 可能是真死锁。
3. **常见误报：** 虚拟机中宿主机调度延迟会导致 guest 的 soft lockup 误报。如果 stack trace 在 idle 相关函数，多半是宿主调度抖动，可忽略。

---

## 3. RCU Stall

### 是什么

RCU（Read-Copy-Update）机制要求每个 CPU 周期性地进入**安静态**（quiescent state），表示"我这边没有在读 RCU 保护的数据了"。如果某个 CPU 超过阈值不报告安静态，RCU 子系统就会报 stall。

### 根因

RCU stall **不是独立问题**，它是其他问题的**症状**：

- **长关中断** → CPU 不响应调度，也不报安静态
- **长关抢占**（`preempt_disable`）→ 同上
- **长关软中断**（`local_bh_disable`）→ 同上
- **内核死循环** → 同上
- **CPU 被拔出（hotplug）但 RCU 未感知** → 架构/固件问题

简单说：**凡是能让一个 CPU 长时间"消失"的，都能触发 RCU stall。**

### 接口

```bash
# RCU stall 超时（秒），默认 21
cat /sys/module/rcupdate/parameters/rcu_cpu_stall_timeout
# 设为 0 表示用编译默认值（21s）
# 设大可减少误报，但会延迟真问题的发现

# 抑制 stall 告警（调试用，生产勿开）
cat /sys/module/rcupdate/parameters/rcu_cpu_stall_suppress
# 默认 0 = 不抑制

# 启动参数也常用：
# rcupdate.rcu_cpu_stall_timeout=60
# rcupdate.rcu_cpu_stall_suppress=1   (临时抑制)
```

### 排查思路

1. **RCU stall 是果不是因：** 先看 dmesg 中是否有 hard lockup / soft lockup / 其他错误，RCU stall 往往跟着它们一起出现。
2. **看 stall 信息中的 CPU 和 stack trace：**
   - `rcu_sched kthread starved` → RCU 内核线程没被调度，检查 CPU 负载
   - 某个 CPU 的 stack trace 在关中断/关抢占区域 → 对应的锁或代码有问题
   - `Players:` 行列出哪些 CPU 没报安静态，精准定位
3. **增大超时验证：** 把 `rcu_cpu_stall_timeout` 调到 60-120s，如果不再报，说明是运算慢而非死锁。

---

## 4. Hung Task

### 是什么

某个进程在 **D 状态**（TASK_UNINTERRUPTIBLE，不可中断睡眠）超过阈值。D 状态意味着进程在等某个内核资源（锁、I/O、内存分配等），且**不能被信号打断**。

### 根因

- 等**磁盘 I/O**（存储设备无响应、SAN 故障、NFS 挂了）
- 等**内核锁**（mutex、rwsem 被其他路径长时间持有）
- 等**内存回收**（内存紧张，direct reclaim 卡住）
- 等**其他内核资源**（如 jbd2 日志提交）

### 接口

```bash
# 超时阈值（秒），默认 120
cat /proc/sys/kernel/hung_task_timeout_secs
# 设为 0 则完全禁用 hung task 检测

# 触发 panic 与否
cat /proc/sys/kernel/hung_task_panic
# 0 = 不 panic（默认）, 1 = panic 并按 kernel.panic 策略处理

# 检测的最大任务数（避免风暴式打印）
cat /proc/sys/kernel/hung_task_check_count
# 默认 4194304
```

### 排查思路

1. **看 waiting for 的资源：** hung task 日志会打印 `blocked for Xs` 和调用栈，看栈顶函数：
   - `io_schedule()` / `wait_on_page_bit()` → 等 I/O → **查存储**
   - `mutex_lock()` / `down_read()` → 等锁 → 查谁持锁（看其他 CPU 的栈）
   - `shrink_node_zones()` / `try_to_free_pages()` → 内存回收 → **查内存压力**
   - `jbd2` → 文件系统日志 → 查磁盘/存储
2. **先查外部依赖：** 存储/NFS/网络是否正常？很多 hung task 的根因不在内核。
3. **单次 vs 持续：** 单个任务 hung 一次后恢复 → 瞬时压力；大量任务同时 hung → 很可能是共享资源出了问题（如存储掉线）。

---

## 5. Panic 相关配置

各检测机制独立控制是否 panic，但 panic 后的行为由全局 `kernel.panic` 统一管理。调试时经常需要**关闭自动重启**，保留现场。

```bash
# panic 后是否自动重启，以及延迟秒数
cat /proc/sys/kernel/panic
# 0 = 挂住不重启（调试推荐）
# >0 = 等待该秒数后自动重启

# 各检测机制的 panic 开关汇总：
cat /proc/sys/kernel/hardlockup_panic    # hard lockup 触发 panic, 默认 0
cat /proc/sys/kernel/softlockup_panic    # soft lockup 触发 panic, 默认 0
cat /proc/sys/kernel/hung_task_panic     # hung task 触发 panic, 默认 0
# RCU stall 没有 panic 选项，只会打印告警

# 典型调试配置：关闭所有 panic，保留现场等手动查看
echo 0 > /proc/sys/kernel/hardlockup_panic
echo 0 > /proc/sys/kernel/softlockup_panic
echo 0 > /proc/sys/kernel/hung_task_panic
echo 0 > /proc/sys/kernel/panic

# 典型生产配置：开启 panic + 自动重启，缩短故障时间
echo 1 > /proc/sys/kernel/hardlockup_panic
echo 1 > /proc/sys/kernel/softlockup_panic
echo 1 > /proc/sys/kernel/hung_task_panic
echo 10 > /proc/sys/kernel/panic   # 10s 后重启
```

---

## 6. 通用排查流程

```
收到告警
  │
  ├─ 看 dmesg 确认类型（hard lockup / soft lockup / RCU stall / hung task）
  │
  ├─ 单次偶发？
  │    └─ 是 → 加大阈值观察，可能是瞬时压力，暂不深究
  │
  ├─ 可复现？
  │    └─ 是 → 收集完整 dmesg + stack trace
  │
  ├─ 加大阈值后消失？
  │    ├─ 是 → 负载/性能问题，非死锁。考虑优化代码或调整配置。
  │    └─ 否 → 很可能是真死锁或硬件问题，需深入分析。
  │
  └─ 虚拟机环境？
       └─ stack trace 在 idle 函数 → 大概率宿主调度抖动，可忽略
```

**经常一起出现的组合：**
- hard lockup + RCU stall → CPU 真卡死了，RCU stall 是附带症状
- soft lockup + RCU stall → 内核忙循环，同上
- hung task 单独出现 → 多半是等 I/O 或等锁
- RCU stall 单独出现 → 看具体 stack trace，可能是长关抢占等
