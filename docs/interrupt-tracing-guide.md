# Linux 内核中断观测与 tracing 指南（ARM64）

本文面向下游内核用户，帮助你自助观测中断行为、定位中断相关性能问题。
基于 ARM64 架构 + GICv3/GICv4 中断控制器。

---

## 1. 中断生命周期与观测点

一次中断从硬件触发到处理完毕，经历以下阶段：

```
外设 ──IRQ信号──▶ GIC ──FIQ/IRQ──▶ CPU
                                    │
                                    ▼
                  ┌─── 阶段一：硬件响应与进入内核 ───┐
                  │ CPU 跳转异常向量 (el1_irq/el0_irq) │
                  │ GIC 应答: 读取 ICC_IAR1_EL1       │  不可观测
                  │   → 获得硬件中断号 (hwirq)        │  (硬件+汇编)
                  │ irq_enter(): 通知 RCU 进入中断上下文│
                  └──────────────────────────────────┘
                                    │
                                    ▼
                  ┌─── 阶段二：硬中断处理 (关中断) ──┐
                  │ 查找 irq_desc → 遍历 action 链    │
                  │                                    │
                  │  ┌─────────────────────────────┐   │
                  │  │ trace:irq_handler_entry      │   │
                  │  │   → 字段: irq, name          │   │  ★ 观测点
                  │  │ action->handler() 执行       │   │
                  │  │ trace:irq_handler_exit       │   │
                  │  │   → 字段: irq, ret(handled)  │   │  ★ 观测点
                  │  └─────────────────────────────┘   │
                  │                                    │
                  │ entry→exit 时间差 = handler 耗时   │
                  │ ret=unhandled 累计 → spurious      │
                  └──────────────────────────────────┘
                                    │
                                    ▼
                  ┌─── 阶段三：软中断处理 (开中断) ──┐
                  │ irq_exit() → 检查 pending softirq │
                  │ 在中断上下文中执行，可被新中断抢占 │
                  │                                    │
                  │  ┌─────────────────────────────┐   │
                  │  │ trace:softirq_raise          │   │  ★ 谁唤醒了 softirq
                  │  │ trace:softirq_entry          │   │
                  │  │   → 字段: vec (类型号)       │   │  ★ 观测点
                  │  │ softirq handler() 执行       │   │
                  │  │ trace:softirq_exit           │   │
                  │  │   → 字段: vec (类型号)       │   │  ★ 观测点
                  │  └─────────────────────────────┘   │
                  │                                    │
                  │ 处理不完 → 推给 ksoftirqd 线程     │
                  └──────────────────────────────────┘
                                    │
                                    ▼
                  ┌─── 阶段四：tasklet (softirq 的一种) ┐
                  │                                      │
                  │  ┌─────────────────────────────┐     │
                  │  │ trace:tasklet_entry          │     │
                  │  │   → 字段: tasklet, func      │     │  ★ 观测点
                  │  │ tasklet->callback() 执行     │     │
                  │  │ trace:tasklet_exit           │     │
                  │  │   → 字段: tasklet, func      │     │  ★ 观测点
                  │  └─────────────────────────────┘     │
                  │                                      │
                  │ 同一 tasklet 不会在多 CPU 并行执行   │
                  └──────────────────────────────────────┘
                                    │
                                    ▼
                          返回被中断的上下文
```

### 阶段说明

**阶段一：硬件响应与进入内核（不可观测）**

外设向 GIC 发送中断信号 → GIC 仲裁后向 CPU 发送 IRQ → CPU 在当前指令执行完后，跳转到异常向量表（`el1_irq` 或 `el0_irq`）→ CPU 保存上下文，调用 `irq_enter()` 通知 RCU 等子系统"现在在中断上下文中"。

这一段是硬件 + 汇编层面完成的，没有 tracepoint。如果这段出了问题（比如中断根本没送到 CPU），需要看 GIC 寄存器或用硬件调试工具。

**阶段二：硬中断处理（IRQ handler）**

`generic_handle_irq_desc()` 根据中断号找到 `irq_desc`，调用上面注册的 action handler 链。每个 handler 执行前后分别触发 `irq_handler_entry` / `irq_handler_exit`。这是**最核心的观测点**：entry 和 exit 的时间戳差值就是 handler 的处理时长，`ret` 字段标识该 handler 是否认领了这根中断。

一个 IRQ 号上可以挂多个 handler（action 链），内核会依次调用直到有一个返回 `IRQ_HANDLED`。如果所有 handler 都返回 `IRQ_NONE`，内核会累计 spurious 计数，超过阈值后可能禁用该 IRQ。

**阶段三：软中断处理（softirq）**

硬中断 handler 执行完后，`irq_exit()` 检查是否有被标记为 pending 的 softirq。如果有，在**中断上下文**（不是进程上下文）中执行 `__do_softirq()`，依次处理每种待处理的 softirq。`softirq_entry` / `softirq_exit` 标记每种 softirq handler 的执行边界，`softirq_raise` 标记谁唤醒了 softirq。

softirq 是中断处理"下半部"的主要机制。硬中断 handler 应尽量快（关中断状态下执行），耗时的收尾工作通过 `raise_softirq()` 推迟到 softirq 中处理。softirq 在开中断状态下执行，可以被新的硬中断抢占。如果 softirq 在本轮处理不完（超过时间或次数限制），剩余的会被推给 `ksoftirqd` 内核线程在进程上下文中继续处理。

**阶段四：tasklet（softirq 的一种）**

tasklet 是构建在 `TASKLET_SOFTIRQ` 和 `HI_SOFTIRQ` 之上的延期执行机制。`tasklet_entry` / `tasklet_exit` 标记每个 tasklet 回调的执行。同一个 tasklet 不会在多个 CPU 上并行执行（与 softirq 不同），适合不需要极致性能但需要简单同步的场景。

> **关于观测粒度**：`irq_handler_entry/exit` 只能看到"某个 IRQ 号的 handler 花了多久"。如果想深入到 handler 内部（比如某个驱动收到中断后访问寄存器、唤醒等待队列、触发 DMA 的各段耗时），需要用 `ftrace function_graph` 或在驱动代码中自行添加 tracepoint。

| tracepoint | 触发时机 | 关键字段 | 所需配置 |
|---|---|---|---|
| `irq:irq_handler_entry` | 进入 IRQ handler 前 | `irq` (IRQ号), `name` (handler名) | 无额外配置 |
| `irq:irq_handler_exit` | IRQ handler 返回后 | `irq` (IRQ号), `ret` (handled/unhandled) | 无额外配置 |
| `irq:softirq_raise` | softirq 被唤醒时 | `vec` (softirq类型号) | 无额外配置 |
| `irq:softirq_entry` | 进入 softirq 处理前 | `vec` (softirq类型号) | 无额外配置 |
| `irq:softirq_exit` | softirq 处理完后 | `vec` (softirq类型号) | 无额外配置 |
| `irq:tasklet_entry` | tasklet 执行前 | `tasklet` (指针), `func` (回调地址) | 无额外配置 |
| `irq:tasklet_exit` | tasklet 执行后 | `tasklet` (指针), `func` (回调地址) | 无额外配置 |

**ARM64 IPI tracepoint** (`include/trace/events/ipi.h`)：

| tracepoint | 触发时机 | 关键字段 |
|---|---|---|
| `ipi:ipi_raise` | 发送 IPI 时 | `target` (目标 CPU 列表), `reason` (IPI 类型名称) |
| `ipi:ipi_entry` | 进入 IPI handler 前 | `reason` (IPI 类型名称) |
| `ipi:ipi_exit` | IPI handler 返回后 | `reason` (IPI 类型名称) |

ARM64 的 IPI 类型：`Rescheduling`、`Function call`、`CPU stop`、`Timer broadcast`、`IRQ work`、`CPU backtrace` 等。

> **注意**：ARM64 没有类似 x86 `irq_vectors.h` 的架构级向量 tracepoint，也没有 `irq_matrix` tracepoint（那是 x86 APIC 独有的）。GIC 驱动本身不提供 tracepoint。

**softirq 类型号与名称对照**:

| 编号 | 名称 | 典型用途 |
|---|---|---|
| 0 | HI_SOFTIRQ | 高优先级 tasklet |
| 1 | TIMER_SOFTIRQ | 定时器 |
| 2 | NET_TX_SOFTIRQ | 网络发送 |
| 3 | NET_RX_SOFTIRQ | 网络接收 |
| 4 | BLOCK_SOFTIRQ | 块设备完成 |
| 5 | IRQ_POLL_SOFTIRQ | IRQ polling |
| 6 | TASKLET_SOFTIRQ | 普通 tasklet |
| 7 | SCHED_SOFTIRQ | 调度器负载均衡 |
| 8 | HRTIMER_SOFTIRQ | 高精度定时器 |
| 9 | RCU_SOFTIRQ | RCU 回调处理 |

---

## 2. 静态观测接口（不需要 trace）

### 2.1 /proc/interrupts — 中断计数总览

```bash
cat /proc/interrupts
```

输出示例（ARM64 + GICv3）：
```
           CPU0       CPU1       CPU2       CPU3
 25:       200        180        195        210   GICv3  25 Level     vgic
 35:    800000     750000     820000     780000   GICv3  35 Level     eth0
 62:    1000000     950000    1050000     980000   GICv3  62 Level     nvme0q0
IPI0:        0          0          0          0  Rescheduling interrupts
IPI1:      500        480        520        490  Function call interrupts
IPI2:        0          0          0          0  CPU stop interrupts
IPI3:        8          6          7          5  IRQ work interrupts
IPI4:        0          0          0          0  Timer broadcast interrupts
Err:         0
```

**看什么**：
- 每个 IRQ 在各 CPU 上的分布是否均衡（不均衡可能需要调 affinity）
- 某个 IRQ 计数是否在短时间内暴涨（中断风暴）
- `IPI0`~`IPI4` 是 ARM64 的核间中断，`IPI1 (Function call)` 高可能是频繁的 smp_call_function 导致
- `Err` 计数非零说明 GIC 处理了错误中断

### 2.2 /sys/kernel/irq/<N>/ — 单个 IRQ 详细信息

```bash
# 查看某个 IRQ 的每 CPU 中断计数
cat /sys/kernel/irq/24/per_cpu_count

# 查看触发类型、芯片名、handler 名
cat /sys/kernel/irq/24/type       # edge 或 level
cat /sys/kernel/irq/24/chip_name  # 如 GICv3
cat /sys/kernel/irq/24/actions    # handler 名称列表
cat /sys/kernel/irq/24/hwirq      # 硬件 IRQ 号
```

### 2.3 /proc/irq/<N>/ — affinity 与 spurious 信息

```bash
# 查看/修改 IRQ 亲和性
cat /proc/irq/24/smp_affinity_list    # 如 0-3
cat /proc/irq/24/effective_affinity_list  # 实际生效的亲和性

# 查看 spurious（虚假/未处理）中断统计
cat /proc/irq/24/spurious
# 输出: count 0 unhandled 0 last_unhandled_ms 0
```

**`spurious` 文件解读**：
- `count`：该 IRQ 总中断次数
- `unhandled`：没有 handler 处理的次数（handler 返回 `IRQ_NONE`）
- 如果 `unhandled` 持续增长，说明 handler 没有正确处理该中断，内核最终会禁用这个 IRQ

### 2.4 /proc/softirqs — softirq 计数

```bash
cat /proc/softirqs
```

**看什么**：
- `NET_RX` 在某个 CPU 上特别高 → 网络软中断集中在该 CPU，考虑 RPS 或 RSS
- `SCHED` 不均匀 → 调度器负载均衡可能有压力
- `RCU` 长时间不为零 → 正常，但集中在某个 CPU 可能有问题

### 2.5 /sys/kernel/debug/irq/irqs/<N> — IRQ 描述符完整状态

```bash
# 需要挂载 debugfs: mount -t debugfs none /sys/kernel/debug
cat /sys/kernel/debug/irq/irqs/24
```

输出包含：handler 地址、设备名、status 标志、ddepth（disable depth）、affinity mask 等。
当怀疑 IRQ 被 wrongfully disabled/masked/suspended 时查看此文件。

---

## 3. 中断处理时长 Tracing 方法

以下方法从简单到复杂排列，按需选用。

### 3.1 方法一：trace-cmd 录制 + 分析（推荐）

**最直接的方法**：利用 `irq_handler_entry` 和 `irq_handler_exit` 的时间戳差值。

```bash
# 录制所有 IRQ handler 的 entry/exit，持续 10 秒
trace-cmd record -e irq:irq_handler_entry -e irq:irq_handler_exit -p function_graph sleep 10

# 查看原始 trace 数据
trace-cmd report

# 只看某个 IRQ（如 IRQ 24）
trace-cmd report | grep "irq=24"
```

输出中每条记录带时间戳，entry 到 exit 的差值就是 handler 执行时长：
```
<idle>-0     [002] d..1  1234.567890: irq_handler_entry: irq=24 name=eth0-TxRx-0
<idle>-0     [002] d..1  1234.567895: irq_handler_exit: irq=24 ret=handled
```
handler 耗时 = 567895 - 567890 = **5 微秒**

### 3.2 方法二：ftrace（无需额外安装）

```bash
cd /sys/kernel/debug/tracing

# 启用 IRQ handler tracepoint
echo 1 > events/irq/irq_handler_entry/enable
echo 1 > events/irq/irq_handler_exit/enable

# 可选：只追踪特定 IRQ
echo 'irq == 24' > events/irq/irq_handler_entry/filter
echo 'irq == 24' > events/irq/irq_handler_exit/filter

# 开始录制
echo 1 > tracing_on

# ... 等待一段时间或触发负载 ...

# 停止并读取
echo 0 > tracing_on
cat trace

# 清理
echo 0 > events/irq/irq_handler_entry/enable
echo 0 > events/irq/irq_handler_exit/enable
echo > trace
```

### 3.3 方法三：perf stat — 快速统计中断频率

```bash
# 统计 10 秒内各 IRQ 的触发次数
perf stat -e 'irq:irq_handler_entry' -a sleep 10

# 按 IRQ 号过滤
perf stat -e 'irq:irq_handler_entry[irq==24]' -a sleep 10
```

### 3.4 方法四：perf record + perf script — 精确时间分析

```bash
# 录制所有 CPU 的 IRQ handler entry/exit 事件，持续 10 秒
perf record -e irq:irq_handler_entry -e irq:irq_handler_exit -a -- sleep 10

# 查看带时间戳的事件流
perf script

# 生成报告（按 handler 名称汇总）
perf report
```

### 3.5 方法五：bpftrace — 灵活的实时统计（推荐用于时长统计）

bpftrace 可以直接计算 entry/exit 时间差并做统计，是最适合观测"中断处理时长"的方法。

**5a. 统计每个 IRQ handler 的执行耗时分布**：

```bash
# bpftrace -e '
tracepoint:irq:irq_handler_entry
{
    @start_irq[args->irq] = nsecs;
}

tracepoint:irq:irq_handler_exit
/@start_irq[args->irq]/
{
    @usecs[args->irq, args->ret] = hist((nsecs - @start_irq[args->irq]) / 1000);
    delete(@start_irq[args->irq]);
}
'
```

输出直方图示例：
```
@usecs[24, handled]:
[4, 8)               1024 |@@@@@@@@@@@@@@@@@@@@                    |
[8, 16)              2048 |@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@  |
[16, 32)              512 |@@@@@@@@@@                              |
[32, 64)               64 |@                                       |

@usecs[27, handled]:
[64, 128)            8192 |@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@  |
[128, 256)           1024 |@@@@@                                   |
```

解读：IRQ 24 的大部分 handler 调用在 8-16 微秒内完成；IRQ 27 的 handler 需要 64-128 微秒。

**5b. 打印耗时超过阈值的 IRQ handler**：

```bash
# bpftrace -e '
tracepoint:irq:irq_handler_entry
{
    @ts[args->irq] = nsecs;
}

tracepoint:irq:irq_handler_exit
/@ts[args->irq] && (nsecs - @ts[args->irq]) > 100000/  // 超过 100us
{
    printf("IRQ %d %s on cpu %d took %d us\n",
           args->irq, args->ret, cpu,
           (nsecs - @ts[args->irq]) / 1000);
    delete(@ts[args->irq]);
}
'
```

**5c. 统计 softirq 处理耗时**：

```bash
# bpftrace -e '
tracepoint:irq:softirq_entry
{
    @si_ts[args->vec] = nsecs;
}

tracepoint:irq:softirq_exit
/@si_ts[args->vec]/
{
    @si_lat[args->vec] = hist((nsecs - @si_ts[args->vec]) / 1000);
    delete(@si_ts[args->vec]);
}
'
```

### 3.6 方法六：irqsoff tracer — 测量中断关闭的最大延迟

这个 tracer 不是测单个中断的处理时长，而是测**系统中中断被关闭（`local_irq_disable()`）的最长持续时间**。对实时性要求高的场景很有用。

```bash
cd /sys/kernel/debug/tracing

# 启用 irqsoff tracer
echo irqsoff > current_tracer
echo 1 > tracing_on

# ... 等待一段时间 ...

cat trace
echo nop > current_tracer
```

输出会显示最长中断关闭延迟的调用栈：
```
# tracer: irqsoff
#
# irqsoff latency trace v1.1.5
# ---------------------------------------------------------------------------
# latency: 1234 us, #4/4, CPU#2 | (M:preempt VP:0, KP:0, SP:0 HP:0 #P:4)
#    -----------------
#    | task: swapper/0-0 (uid:0 nice:0 policy:0 rt_prio:0)
#    -----------------
# => started at: some_function
# => ended at:   some_other_function
```

**需要开启配置**：`CONFIG_PREEMPTIRQ_TRACEPOINTS`、`CONFIG_IRQSOFF_TRACER`

### 3.7 方法七：osnoise tracer — 测量中断对系统的干扰

osnoise tracer 专门测量操作系统噪声（包括中断、softirq、NMIs 等）对实时任务的干扰。

```bash
cd /sys/kernel/debug/tracing

# 配置 osnoise
echo osnoise > current_tracer
echo 1 > tracing_on

# ... 等待 ...

cat osnoise/options         # 查看配置选项
cat trace
echo nop > current_tracer
```

它会生成 `irq_noise` 和 `softirq_noise` tracepoint，包含每次中断/softirq 的持续时间。

### 3.8 ARM64 特有：Pseudo-NMI 观测

ARM64 通过 `CONFIG_ARM64_PSEUDO_NMI` 支持伪 NMI（需要 GICv3+，且启动时加 `irqchip.gicv3_pseudo_nmi=1`）。伪 NMI 不受普通中断屏蔽影响，用于 backtrace、CPU stop、kgdb 等关键 IPI。

伪 NMI 没有独立的 tracepoint，但可以间接观测：

```bash
# 1. 检查是否启用了 pseudo-NMI
cat /sys/devices/system/cpu/cpu0/caps/pseudo_nmi
# 输出 "true" 或 "false"

# 2. 通过 osnoise tracer 观测 NMI 噪声
cd /sys/kernel/debug/tracing
echo osnoise > current_tracer
echo 1 > tracing_on
sleep 5
cat trace
echo nop > current_tracer

# 3. NMI 类型的 IPI 会走 ipi_entry/ipi_exit tracepoint
trace-cmd record -e ipi:ipi_entry -e ipi:ipi_exit sleep 10
trace-cmd report
```

---

## 4. 常见排查场景速查

### 场景：某个 IRQ 中断计数异常暴涨

```bash
# 1. 每秒采样 /proc/interrupts 的变化
watch -n1 "cat /proc/interrupts | grep -E '^\s+24:'"

# 2. 或者用 perf 实时统计
perf stat -e 'irq:irq_handler_entry[irq==24]' -a -I 1000 sleep 30
```

### 场景：中断处理耗时过长，影响系统响应

```bash
# 用 bpftrace 找出耗时 > 100us 的 handler（见 3.5b）
bpftrace -e '
tracepoint:irq:irq_handler_entry { @ts[args->irq] = nsecs; }
tracepoint:irq:irq_handler_exit /@ts[args->irq] && (nsecs - @ts[args->irq]) > 100000/ {
    printf("IRQ%d on cpu%d: %d us\n", args->irq, cpu, (nsecs - @ts[args->irq])/1000);
    delete(@ts[args->irq]);
}
'
```

### 场景：softirq 占用 CPU 过高

```bash
# 1. 查看 softirq 分布
cat /proc/softirqs

# 2. 用 bpftrace 统计各类 softirq 的耗时分布（见 3.5c）

# 3. 查看是否有 softirq 在硬中断上下文中处理不完被推到 ksoftirqd
ps -eo pid,comm | grep ksoftirqd
perf record -e irq:softirq_entry -e irq:softirq_exit -ag -- sleep 10
```

### 场景：中断分布不均（都在一个 CPU 上）

```bash
# 查看当前亲和性
cat /proc/irq/24/smp_affinity_list
cat /proc/irq/24/effective_affinity_list

# 查看每 CPU 分布
cat /sys/kernel/irq/24/per_cpu_count

# 修改亲和性（示例：绑定到 CPU 2-3）
echo "2-3" > /proc/irq/24/smp_affinity_list
```

### 场景：怀疑中断丢失或 spurious

```bash
# 查看 spurious 统计
cat /proc/irq/24/spurious

# trace 中查看 ret=unhandled 的记录
cd /sys/kernel/debug/tracing
echo 1 > events/irq/irq_handler_exit/enable
echo 'ret == 0' > events/irq/irq_handler_exit/filter
echo 1 > tracing_on
# ... 触发负载 ...
cat trace
```

### 场景：IPI（核间中断）频繁

```bash
# 1. 查看 /proc/interrupts 中 IPI0~IPI4 的计数变化
watch -n1 "cat /proc/interrupts | grep IPI"

# 2. 用 ipi tracepoint 追踪 IPI 发送和接收
trace-cmd record -e ipi:ipi_raise -e ipi:ipi_entry -e ipi:ipi_exit sleep 10
trace-cmd report

# 3. 用 bpftrace 统计各类型 IPI 频率
# bpftrace -e '
tracepoint:ipi:ipi_raise
{
    @ipi_count[args->reason] = count();
}
'
```

---

## 5. 所需内核配置

| 配置项 | 用途 |
|---|---|
| `CONFIG_TRACEPOINTS` | 所有 tracepoint 功能的基础 |
| `CONFIG_FTRACE` | ftrace 框架 |
| `CONFIG_FUNCTION_TRACER` | function/function_graph tracer |
| `CONFIG_IRQSOFF_TRACER` | irqsoff tracer（测量中断关闭延迟） |
| `CONFIG_PREEMPTIRQ_TRACEPOINTS` | preempt/irq flag 变化 tracepoint |
| `CONFIG_PERF_EVENTS` | perf 工具 |
| `CONFIG_DEBUG_FS` | debugfs（ftrace 依赖） |
| `CONFIG_TRACER_SNAPSHOT` | ftrace snapshot 功能 |
| `CONFIG_ARM64_PSEUDO_NMI` | ARM64 伪 NMI 支持（需 GICv3+） |
| `CONFIG_GENERIC_IRQ_DEBUGFS` | `/sys/kernel/debug/irq/` 详细调试信息 |

大多数发行版默认内核已开启 `CONFIG_TRACEPOINTS`、`CONFIG_FTRACE`、`CONFIG_PERF_EVENTS`、`CONFIG_DEBUG_FS`。
`irqsoff` tracer 和 `PREEMPTIRQ_TRACEPOINTS` 可能需要自行编译内核开启。
`ARM64_PSEUDO_NMI` 需要内核编译时开启，且启动参数加 `irqchip.gicv3_pseudo_nmi=1`。
