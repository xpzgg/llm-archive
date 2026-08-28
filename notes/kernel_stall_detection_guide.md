# Linux 内核卡死检测机制速查手册

> 面向下游的快速参考，遇到 "lockup"、"stall"、"hung" 类告警时按此文档自查。

---

## TL;DR

| 检测机制 | 一句话含义 | 典型根因 |
|---|---|---|
| **Hard Lockup** | CPU 的普通定时器中断心跳长时间不前进 | 长时间关 IRQ、IRQ handler 卡死、CPU/中断硬件异常 |
| **Soft Lockup** | CPU 长时间没有调度 watchdog 的喂狗任务 | 内核忙循环且无 `cond_resched()`、长时间 `preempt_disable` |
| **RCU Stall** | 正在进行的 RCU 宽限期（grace period）长时间无法结束 | CPU 不报安静态、RCU 内核线程长期得不到运行、时钟或中断异常 |
| **Hung Task** | 不可中断任务长时间没有被调度过 | 等 I/O 无响应、等锁、等内核资源 |

**快速判断：**
- 有 `hard LOCKUP` / `Hard LOCKUP` → Hard Lockup
- 有 `BUG: soft lockup` → Soft Lockup
- 有 `rcu_sched` 的 `detected stalls` / `self-detected stall` → RCU Stall
- 有 `INFO: task ... blocked ... for more than` → Hung Task

判断时以完整告警签名为准。SysRq、panic 和性能监控等路径也可能使用 NMI，因此日志中出现 `NMI` 字样时，还要继续查找是否存在明确的 `hard LOCKUP` 报告。

---

## 1. Hard Lockup

### 是什么

Hard Lockup watchdog 会定期检查每个 CPU 的普通定时器中断（hrtimer）心跳。某个 CPU 的心跳长时间不再前进时，就报告 Hard Lockup。最常见的原因是本地 IRQ 被长时间屏蔽，或者 CPU 卡在无法运行定时器中断的上下文中。

常见实现也叫 NMI watchdog：内核使用 PMU 性能计数器周期性产生高优先级中断；ARM64 通常由 GIC 模拟 NMI。该中断进入 CPU 后检查 hrtimer 心跳计数，并在计数没有变化时打印寄存器和调用栈。

### 根因

- 在 `spin_lock_irq()` / `local_irq_disable()` 保护区内**死循环或长时间运算**
- IRQ handler 本身卡死
- 固件/硬件问题导致 CPU 卡在不可中断状态

### 接口

```bash
# 总开关（同时控制 hard 和 soft lockup watchdog）
cat /proc/sys/kernel/watchdog
# 写 0 = 全关，写 1 = 全开
# 读取值为 1 表示至少有一种 watchdog 已开启

# hard lockup 独立开关
cat /proc/sys/kernel/nmi_watchdog
# 0 = 关闭, 1 = 开启

# hard lockup 检测基准周期，默认 10s
cat /proc/sys/kernel/watchdog_thresh
# NMI watchdog 通常每 watchdog_thresh 秒检查一次心跳
# 实际报告延迟取决于故障发生在检测周期中的位置

# 触发 panic 与否
cat /proc/sys/kernel/hardlockup_panic
# 0 = 不 panic, 1 = panic 并按 kernel.panic 策略处理
```

### 如何确认中断是否被屏蔽

Hard Lockup 表示普通定时器中断心跳没有运行。长时间屏蔽 IRQ 是最常见原因，可以从 ARM64 stack trace 保存的 PSTATE 和 PMR 辅助判断：

```
# crash> bt 或 dmesg 中的 stack trace 里会显示 pstate
pstate: xxxxxxxx (... DAIF ...)
# 使用 GIC 中断优先级控制时还会显示：
pmr: xxxxxxxx
```

DAIF 四位含义（ARM64）：

| 位 | 大写（=1，屏蔽） | 小写（=0，开启） | 含义 |
|---|---|---|---|
| **D** | D | d | Debug 异常屏蔽 |
| **A** | A | a | SError（系统错误）屏蔽 |
| **I** | I | i | **IRQ 屏蔽**（最关键） |
| **F** | F | f | FIQ 屏蔽 |

**判断方法：**

```text
查看异常现场中的 pstate
│
├─ DAIF 中是大写 I（PSTATE.I = 1）
│    └─ 普通 IRQ 已被 PSTATE 屏蔽
│       例：pstate: ... daIf ...
│
└─ DAIF 中是小写 i（PSTATE.I = 0）
     │
     ├─ 日志没有 pmr: 行
     │    └─ 系统未使用 GIC 中断优先级屏蔽
     │       普通 IRQ 开启
     │
     └─ 日志有 pmr: 行
          │
          ├─ pmr: 000000c0（当前 master 的 IRQOFF 值）
          │    └─ 普通 IRQ 已被 PMR 屏蔽
          │
          ├─ pmr: 000000e0（当前 master 的 IRQON 值）
          │    └─ 普通 IRQ 开启
          │
          └─ 其他值
               └─ 查询对应内核的 PMR 定义后再判断
```

### 排查思路

1. **即使只出现一次也先保存现场：** 收集完整 dmesg、触发 CPU 和其他 CPU 的 stack trace。单次现场同样有分析价值，可以用于确认卡住位置和当时的系统状态。
2. **比较多次 stack trace：** 多次停在同一 PC/同一锁上，更像死循环或死锁；堆栈持续移动，可能是路径过长、活锁或 IRQ storm，但仍要结合上下文确认。
3. **调整阈值做持续时间对比：** 把 `watchdog_thresh` 从 10 调到 30 后不再报，说明当前延迟介于新旧阈值之间。继续结合多次调用栈判断代码是在缓慢前进、活锁还是卡在固定位置。修改阈值可能改变问题时序，测试前先保留原始现场。
4. **确认 watchdog 是否正常工作：** NMI watchdog 依赖硬件和虚拟化环境提供相应能力。综合启动日志和 `/proc/sys/kernel/nmi_watchdog` 判断是否成功启用。

---

## 2. Soft Lockup

### 是什么

Soft Lockup watchdog 会周期性调度一个喂狗任务来更新时间戳。该任务超过阈值仍未运行，说明 CPU 长时间没有提供正常调度机会。典型情况是 CPU 在内核态持续执行或长时间禁止抢占。

Soft Lockup 检查由 hrtimer 中断周期性执行，因此单独出现 Soft Lockup 时，该 CPU 通常仍能处理普通定时器中断。若同时出现 Hard Lockup 或中断错误，应将相关日志放在一起分析。

### 根因

- 内核代码中的**忙循环**（`while (1)` 式循环）没有插入 `cond_resched()` 等真正的调度点
- 长时间 `preempt_disable()` 区间
- 大数组遍历、复杂计算等耗时操作且未主动让出 CPU

`cpu_relax()` 只是给处理器的自旋提示；需要让其他任务获得运行机会时，应使用 `cond_resched()` 等真正的调度点。

### 接口

```bash
# soft lockup 阈值（秒），默认 = watchdog_thresh（10s）的 2 倍即 20s
# 实际 soft lockup 超时 = watchdog_thresh * 2
cat /proc/sys/kernel/watchdog_thresh
# 修改示例：设为 30（soft lockup 阈值变 60s）
echo 30 > /proc/sys/kernel/watchdog_thresh

# 触发 panic 与否
cat /proc/sys/kernel/softlockup_panic
# 0 = 不 panic
# N > 0 = soft lockup 持续达到 N 个 soft-lockup 阈值后 panic

# 是否打印所有 CPU 的 backtrace（不只报卡死的那个）
cat /proc/sys/kernel/softlockup_all_cpu_backtrace
```

### 排查思路

1. **看 stack trace：** 日志中 `BUG: soft lockup - CPU#X stuck for Xs!` 后面跟着采样时的调用栈。先从最内层函数看起，再结合多次堆栈判断代码是否在前进。
2. **判断是忙还是死：**
   - 堆栈在循环类函数（`list_for_each_entry` 等）→ 既可能是数据量大，也可能是链表损坏形成环，需要检查循环条件、对象数量，并比较多次 PC。
   - 堆栈在锁操作（`spin_lock` 等）→ 可能是锁竞争、锁 owner 不再运行或真死锁，需要同时看其他 CPU 的堆栈。
   - CPU 时间主要消耗在 hardirq → 可能是 IRQ storm；内核启用相应配置时还会打印高频 IRQ 统计。
3. **虚拟机环境检查宿主调度：** 宿主机长时间不调度 guest 可能造成 Soft Lockup。stack trace 位于 idle 函数时，继续检查宿主机 steal time、虚拟机暂停/迁移记录，以及同一时刻是否有多个 vCPU 一起告警。

---

## 3. RCU Stall

### 是什么

本文默认讨论 Tree RCU + 非抢占式 RCU。RCU（Read-Copy-Update）需要等待相关 CPU 都经过安静态（quiescent state），才能结束一个宽限期（grace period）。RCU Stall 表示一个**正在进行的宽限期长时间无法结束**：可能是某个 CPU 一直不报告安静态，也可能是负责推进宽限期的 RCU 内核线程得不到 CPU 时间。

RCU CPU stall 检测发生在宽限期进行期间。

### 根因

RCU stall 通常是以下问题表现出来的症状：

- **长关中断** → CPU 不响应调度，也不报安静态
- **长关软中断**（`local_bh_disable`）→ 同上
- **CPU 长时间停留在内核态且没有调度点** → 非抢占式 RCU 一直等不到该 CPU 经过安静态
- **内核死循环** → CPU 永远无法经过安静态
- **RCU 内核线程或 RCU softirq 被高优先级任务饿死** → RCU 自身无法推进
- **IRQ storm、慢 console、大量 printk** → RCU 线程或回调长期得不到运行
- **时钟跳变、timer wakeup 或 CPU hotplug/context tracking 异常** → stall 检测或安静态跟踪异常

简单说：**凡是能让 CPU 长时间不报告安静态，或让 RCU 推进机制得不到运行的，都可能触发 RCU Stall。** 根因既可能在业务驱动路径，也可能在 RCU、时钟或架构代码中。

### 接口

```bash
# RCU stall 超时（秒），默认 21
cat /sys/module/rcupdate/parameters/rcu_cpu_stall_timeout
# 普通 RCU stall timeout 的有效范围是 3-300s
# 写入小于 3（包括 0）的值后，内核会按 3s 处理；恢复常用默认值请写 21
# 设大可减少误报，但会延迟真问题的发现

# 抑制 stall 告警（调试用，生产勿开）
cat /sys/module/rcupdate/parameters/rcu_cpu_stall_suppress
# 默认 0 = 不抑制

# 启动参数也常用：
# rcupdate.rcu_cpu_stall_timeout=60
# rcupdate.rcu_cpu_stall_suppress=1   (临时抑制)

# RCU stall 是否触发 panic
cat /proc/sys/kernel/panic_on_rcu_stall
# 0 = 不 panic, 1 = panic

# 检测到多少次 RCU stall 后 panic（仅 panic_on_rcu_stall=1 时有效）
cat /proc/sys/kernel/max_rcu_stall_to_panic
```

### 排查思路

1. **先找同一时间的前置异常：** 查看 dmesg 中是否已有 Hard Lockup、Soft Lockup、中断、时钟或设备错误。RCU Stall 经常由这些异常连带触发。
2. **看 `detected stalls on CPUs/tasks` 后列出的 CPU 和 stack trace：**
   - `rcu_sched kthread starved` → 推进宽限期的 RCU 内核线程没被调度，检查 CPU 负载、实时任务优先级和调度状态
   - 某个 CPU 的 stack trace 在关中断区域或无调度点的内核循环中 → 对应的锁或代码有问题
   - `All QSes seen` → 各 CPU 的安静态已经收齐，但 RCU 内核线程的后续处理没有及时运行
3. **增大超时做持续时间对比：** 把 `rcu_cpu_stall_timeout` 调到 60-120s 后不再报，说明当前延迟低于新阈值。继续结合调用栈、调度延迟和时钟日志判断具体根因。

---

## 4. Hung Task

### 是什么

Hung Task watchdog 会检查处于 **D 状态**（TASK_UNINTERRUPTIBLE，不可中断睡眠）的任务。某个任务超过阈值一直没有被调度过，就会打印告警。D 状态通常意味着任务在等待某个内核资源（锁、I/O、内存分配等），且普通信号不能将它唤醒。

检查时会跳过可终止等待、空闲和冻结中的任务，所以 `ps` 中短暂出现 D 状态是正常现象；持续超过阈值且没有调度进展的任务才会触发告警。

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
# 0 = 不 panic
# N > 0 = 一次扫描发现第 N 个 hung task 时 panic

# 一轮扫描最多检查的任务数
cat /proc/sys/kernel/hung_task_check_count
# 默认 4194304

# 最多打印多少次 hung-task warning；-1 表示不限
cat /proc/sys/kernel/hung_task_warnings
```

### 排查思路

1. **看 waiting for 的资源：** hung task 日志会打印 `blocked for Xs` 和调用栈，看栈顶函数：
   - `io_schedule()` / `wait_on_page_bit()` → 任务在等待 I/O 或页状态，结合 block 层 timeout/error、任务的 `in I/O wait` 标记和设备日志确认是否为存储故障
   - `mutex_lock()` / `down_read()` → 等锁 → 查谁持锁（看其他 CPU 的栈）
   - `shrink_node_zones()` / `try_to_free_pages()` → 内存回收 → **查内存压力**
   - `jbd2` → 文件系统日志 → 查磁盘/存储
2. **先查外部依赖：** 存储/NFS/网络是否正常？很多 hung task 的根因不在内核。
3. **单次 vs 持续：** 单个任务只报一次后恢复，可能是瞬时压力，也可能是一次真实的长尾故障，仍应保留现场；大量任务同时 hung，通常说明共享资源出了问题（如存储掉线或关键锁 owner 卡住）。

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
cat /proc/sys/kernel/softlockup_panic    # N 个 soft-lockup 阈值后 panic, 0 为关闭
cat /proc/sys/kernel/hung_task_panic     # 一轮发现第 N 个 hung task 时 panic, 0 为关闭
cat /proc/sys/kernel/panic_on_rcu_stall  # RCU stall 触发 panic, 0/1
cat /proc/sys/kernel/max_rcu_stall_to_panic # 第几次 RCU stall 后 panic

# 典型调试配置：关闭所有 panic，保留现场等手动查看
echo 0 > /proc/sys/kernel/hardlockup_panic
echo 0 > /proc/sys/kernel/softlockup_panic
echo 0 > /proc/sys/kernel/hung_task_panic
echo 0 > /proc/sys/kernel/panic_on_rcu_stall
echo 0 > /proc/sys/kernel/panic

# 典型生产配置：开启 panic + 自动重启，缩短故障时间
echo 1 > /proc/sys/kernel/hardlockup_panic
echo 1 > /proc/sys/kernel/softlockup_panic
echo 1 > /proc/sys/kernel/hung_task_panic
echo 1 > /proc/sys/kernel/panic_on_rcu_stall
echo 1 > /proc/sys/kernel/max_rcu_stall_to_panic
echo 10 > /proc/sys/kernel/panic   # 10s 后重启
```

生产环境启用自动 panic 前，需要确认 kdump/pstore 能可靠保存现场，并结合服务恢复策略和误报风险评估。

---

## 6. 通用排查流程

```
收到告警
  │
  ├─ 按明确告警签名确认类型；“NMI”字样需要结合上下文判断
  │
  ├─ 立即保留完整 dmesg、时间戳和所有可用 stack trace
  │
  ├─ 可复现？
  │    └─ 是 → 比较多次 PC/堆栈是否固定，并关联负载、IRQ、锁和外设状态
  │
  ├─ 加大阈值后消失？
  │    ├─ 是 → 问题对持续时间敏感，结合多次堆栈判断代码是否在前进
  │    └─ 否 → 持续时间更长，继续根据堆栈、锁、IRQ 和硬件日志定位
  │
  └─ 虚拟机环境？
       └─ 结合 host 调度延迟、steal time、暂停/迁移记录确认是否为 guest 误报
```

**经常一起出现的组合：**
- hard lockup + RCU stall → 普通中断心跳停止，RCU stall 常是附带症状
- soft lockup + RCU stall → CPU 长期不给调度机会，RCU 同时无法推进
- hung task 单独出现 → 常见于等 I/O 或等锁，但仍以调用栈和依赖状态为准
- RCU stall 单独出现 → 可能是 RCU 内核线程饥饿、CPU 长时间不经过调度点、时钟/中断异常或 RCU 自身问题

---

## 7. 全 CPU backtrace 功能检测

如果目标只是确认 ARM64 的全 CPU backtrace 路径能否工作，优先使用内核已有 SysRq 接口：

```bash
# 触发所有活动 CPU 打印 backtrace
echo l > /proc/sysrq-trigger
```

ARM64 会向各 CPU 发送 backtrace IPI。启用 GIC pseudo-NMI 后，该 IPI 以高优先级中断发送；其他情况下使用普通 IPI。各 CPU 都能打印调用栈，表示全 CPU backtrace 路径工作正常。

验证 Hard Lockup watchdog 本身时，可在专用测试机上使用内核 LKDTM 的 `HARDLOCKUP` 测试项；Soft Lockup 可使用 `SOFTLOCKUP`。`HARDLOCKUP` 会关闭本地 IRQ 后永久自旋，可能导致系统卡死或 panic，测试前必须配置串口、kdump/pstore 和自动重启，严禁在生产环境运行。

周期性全 CPU backtrace 会产生大量日志，并且可能长时间等待没有响应的 CPU。日常检查使用一次 SysRq 即可；需要持续采样 PC 时，应使用 perf 等专用采样工具。
