# RCU trace events —— 定位"synchronize_rcu 慢"用哪些、怎么看

> 一句话:`rcudata` 静态,不绑 GP 时间线;trace event 把每次 GP 的 `gp_seq` 贯穿起来,**结合 `rcu_grace_period_init` 和 `rcu_quiescent_state_report` 就能精确指出哪个 CPU 没上报**。

所有 event 定义在 `include/trace/events/rcu.h`,触发点在 `kernel/rcu/tree.c` 和 `kernel/rcu/tree_plugin.h`。需要内核开启 `CONFIG_RCU_TRACE=y`。

事件名前缀都是 `rcu:`(ftrace 里就是 `rcu:rcu_grace_period` 这种)。

## 0. flavor 名字

源码(`kernel/rcu/tree.h:464-477`):

```c
#ifdef CONFIG_PREEMPT_RCU
#define RCU_NAME_RAW "rcu_preempt"     // 抢占内核
#else
#define RCU_NAME_RAW "rcu_sched"       // 非抢占内核
#endif
```

后续样例统一用 `rcu_sched`,实际看到什么取决于 `CONFIG_PREEMPT_RCU`。

## 0.5 必需的 CONFIG

源码依赖:

| 配置 | 作用 | 来源 |
|---|---|---|
| `CONFIG_RCU_TRACE=y` | **总开关**。`include/trace/events/rcu.h:11` 把所有 `TRACE_EVENT_RCU` 展开成 `TRACE_EVENT` 还是 `TRACE_EVENT_NOP`,取决于它 | `kernel/rcu/Kconfig.debug:178` |
| `CONFIG_DEBUG_KERNEL=y` | `RCU_TRACE` 的 `depends on`,必须先开 | `kernel/rcu/Kconfig.debug:180` |
| `CONFIG_TREE_RCU=y` | `rcu.h:44` 的 `#if defined(CONFIG_TREE_RCU)` 把 `rcu_grace_period` / `_init` / `rcu_quiescent_state_report` / `rcu_fqs` / `rcu_preempt_task` / `rcu_unlock_preempted_task` / `rcu_stall_warning` 全包住 | `kernel/rcu/Kconfig:8`,默认 `y if SMP` |
| `CONFIG_FTRACE=y` | tracing 基础设施(挂载 tracefs、ring buffer、event framework)。会自动 `select TRACING` → `select TRACEPOINTS` + `EVENT_TRACING` + `RING_BUFFER` + `TRACE_CLOCK` | `kernel/trace/Kconfig:204`,默认 `y if DEBUG_KERNEL` |

可选:

| 配置 | 作用 |
|---|---|
| `CONFIG_PREEMPT_RCU=y` | flavor 名变成 `rcu_preempt`;`rcu_preempt_task` / `rcu_unlock_preempted_task` 才会有实际触发 |
| `CONFIG_RCU_NOCB_CPU=y` | 只有开了它,`rcu_nocb_wake` 事件才会有意义(定义在 `#ifdef CONFIG_RCU_NOCB_CPU` 里) |

**发行版默认情况**:大多数发行版 SMP + DEBUG_KERNEL 都开,所以 `TREE_RCU`、`RCU_TRACE`、`FTRACE` 自动 `y`,直接能用。

**自定义精简内核 / 没开 DEBUG_KERNEL 的情况**:

```
CONFIG_DEBUG_KERNEL=y
CONFIG_RCU_TRACE=y
CONFIG_FTRACE=y
# 可选
CONFIG_PREEMPT_RCU=y           # 想看 preempt reader 阻塞
CONFIG_RCU_NOCB_CPU=y          # 想看 nocb 行为
```

**快速验证**:

```bash
# 内核构建时是否编译了这些事件
zcat /proc/config.gz | grep -E 'CONFIG_(RCU_TRACE|TREE_RCU|PREEMPT_RCU|FTRACE|DEBUG_KERNEL)='

# 运行时看事件是否真的存在
ls /sys/kernel/tracing/events/rcu/
```

如果 `/sys/kernel/tracing/events/rcu/` 不存在或为空,基本就是上面某个 config 没开。

## 1. 开启方法

### 方法 A:tracefs(最直接,适合"边跑边抓")

```bash
cd /sys/kernel/debug/tracing       # 或 /sys/kernel/tracing

# 清空 + 关掉所有事件
echo > trace
echo 0 > events/enable
echo nop > current_tracer
echo 1 > tracing_on              # 全局 ring buffer 开关,默认 1 但可能被前一个会话关过

# 开关键事件
echo 1 > events/rcu/rcu_grace_period/enable
echo 1 > events/rcu/rcu_grace_period_init/enable
echo 1 > events/rcu/rcu_quiescent_state_report/enable
echo 1 > events/rcu/rcu_fqs/enable
echo 1 > events/rcu/rcu_sr_normal/enable
echo 1 > events/rcu/rcu_stall_warning/enable

# PREEMPT_RCU 必加这两个
echo 1 > events/rcu/rcu_preempt_task/enable
echo 1 > events/rcu/rcu_unlock_preempted_task/enable

# 看 EQS 转换(哪个 CPU 一直没进 EQS)
echo 1 > events/rcu/rcu_watching/enable

# 触发负载,同时读流
cat trace_pipe | tee /tmp/rcu.trace
```

### 方法 B:perf(适合"事后回放")

```bash
perf record -a -e rcu:rcu_grace_period \
              -e rcu:rcu_grace_period_init \
              -e rcu:rcu_quiescent_state_report \
              -e rcu:rcu_fqs \
              -e rcu:rcu_sr_normal \
              -e rcu:rcu_stall_warning \
              -g -- sleep 30
perf script -i perf.data | less
```

### 方法 C:bpftrace(适合"按 GP 序号过滤")

```bash
bpftrace -e '
  tracepoint:rcu:rcu_grace_period     { printf("%-8d %-12s gp=%ld %s\n", pid, comm, args->gp_seq, args->gpevent); }
  tracepoint:rcu:rcu_quiescent_state_report {
    printf("qsrep gp=%ld mask=%#lx>qsmask=%#lx lvl=%d cpus=[%d..%d]\n",
           args->gp_seq, args->mask, args->qsmask, args->level, args->grplo, args->grphi);
  }
  tracepoint:rcu:rcu_fqs { printf("fqs gp=%ld cpu=%d %s\n", args->gp_seq, args->cpu, args->qsevent); }
'
```

## 2. 核心事件详解

### 2.1 `rcu_grace_period` —— GP 时间线主轴

**定义**(`include/trace/events/rcu.h:69`):

```c
TP_PROTO(const char *rcuname, unsigned long gp_seq, const char *gpevent)
TP_printk("%s %ld %s", __entry->rcuname, __entry->gp_seq, __entry->gpevent)
```

**三个字段**:`rcuname` `gp_seq` `gpevent`。

`gpevent` 全部取值(对应 `kernel/rcu/tree.c` / `tree_plugin.h` 里 `TPS("...")` 的位置):

| gpevent | 触发点 | 含义 |
|---------|--------|------|
| `newreq` | `tree.c:1074`,`2247` | 有新 GP 被请求(通常是 `synchronize_rcu` 来了) |
| `reqwait` | `tree.c:2278` | GP kthread 睡着等 `RCU_GP_FLAG_INIT` |
| `reqwaitsig` | `tree.c:2292` | 等待被信号打断(异常) |
| `start` | `tree.c:1859`,`rcu_gp_init()` | GP 真正开始 |
| `AccWaitCB` | `tree.c:1173` | 加速新 callback 到 `WAIT_TAIL` |
| `AccReadyCB` | `tree.c:1175` | 加速新 callback 到 `NEXT_READY_TAIL` |
| `cpustart` | `tree.c:1306` | CPU 首次注意到 GP 开始 |
| `cpuqs` | `tree_plugin.h:302`/`955`,`rcu_qs()` | **CPU 经过了 QS**(由 `rcu_qs()` 打) |
| `cpuonl` | `tree.c:4276` | CPU 上线 |
| `cpuofl` | `tree.c:4524` | CPU 下线 |
| `cpuofl-bgp` | `tree.c:4524` | CPU 下线且正在阻挡当前 GP |
| `fqswait` | `tree.c:2092` | GP kthread 进入等 FQS 周期的睡眠 |
| `fqsstart` | `tree.c:2115` | 开始 force quiescent state |
| `fqsend` | `tree.c:2123` | FQS 结束 |
| `fqswaitsig` | `tree.c:2134` | FQS 等待被信号打断(异常) |
| `cpuend` | `tree.c:1290` | CPU 首次注意到 GP 结束 |
| `end` | `tree.c:2220`,`rcu_gp_cleanup()` | GP 结束 |

**样例输出**(ftrace 默认行格式):

```
<idle>-0       [003] d.h. 12345.678901: rcu_grace_period: rcu_sched 1024 newreq
 rcuc/3-32      [003] .... 12345.679050: rcu_grace_period: rcu_sched 1024 start
 rcuc/3-32      [003] .... 12345.679200: rcu_grace_period: rcu_sched 1024 fqswait
 swapper/1-0    [001] ..s. 12345.700000: rcu_grace_period: rcu_sched 1024 cpuqs
 rcuc/3-32      [003] .... 12345.879050: rcu_grace_period: rcu_sched 1024 end
```

**关键**:同一个 `gp_seq` 的所有事件构成一个完整 GP。用 `awk '$5=="1024"'` 就能捞出一次 GP 的全部动作。

**为什么这个事件不可省** —— 它有四个独有角色:

1. **定义 GP 边界**:`start`/`end` 是 `gp_seq` 时间线的唯一定义,其他事件都带 `gp_seq` 但都不告诉你这次 GP 何时开始/结束。挑"慢 GP"也只能靠它。

2. **`cpuqs` 是最精细的 per-CPU QS 信号**。`rcu_qs()`(`tree_plugin.h:298`/`950`)在 CPU 本地标记 QS 的瞬间就打,看 `[CPU]` 列直接知道是哪个 CPU。这比 `rcu_quiescent_state_report` 强:
   - `rcu_quiescent_state_report` 是 **node 级**,沿树向上传播时每层才打一次;
   - 而且 `rcu_report_qs_rdp()`(`tree.c:2467`)里 `(rnp->qsmask & mask) == 0` 时直接返回,**根本不会调** `rcu_report_qs_rnp`,这种情况下 `rcu_quiescent_state_report` 不触发,但 `cpuqs` 照样打;
   - 所以**"这次 GP 哪些 CPU 通过了 QS"用 `cpuqs` 看最准**,没出现 `cpuqs` 的 CPU 就是元凶候选。

3. **GP kthread 状态演化**:`fqswait` → `fqsstart` → `fqsend` 告诉你 RCU 何时开始"觉得慢"。`fqswait` 一出现意味着已过了 `jiffies_till_first_fqs`(默认 ~200ms)。

4. **GP 请求来源**:`newreq` 的 `[pid]` `comm` 列能看到是谁触发的(通常就是 `synchronize_rcu` 调用者的进程)。

**分析手段**:
- `start` → `end` 的时间差 = 这次 GP 的实际耗时。
- grep 同 gp_seq 的 `cpuqs`,出现的 CPU 就是已通过 QS 的;online CPU 集合减去它 = **没上报的元凶**(最直接的判定方法)。
- 出现 `fqswait` + `fqsstart` 说明 GP kthread 已经等不下去要主动 force 了 —— **这就是慢的信号**。
- `cpuofl-bgp` 说明有 CPU 下线挡住了 GP。

---

### 2.2 `rcu_grace_period_init` —— GP 开始时要等哪些 CPU ★最关键★

**定义**(`include/trace/events/rcu.h:147`):

```c
TP_PROTO(const char *rcuname, unsigned long gp_seq, u8 level,
         int grplo, int grphi, unsigned long qsmask)
TP_printk("%s %ld %u %d %d %lx", ...)
```

**字段**:`rcuname gp_seq level grplo grphi qsmask`。

**触发点**(`tree.c:1954-1966`,`rcu_gp_init()` 里 `rcu_for_each_node_breadth_first(rnp)`)。GP 开始时,对**每一个 `rcu_node`** 都打一次,把它的等待 mask 写出来。

**字段含义**:
- `level`:这个 rcu_node 在树里的层数(0=root,叶子层数取决于 `RCU_FANOUT`)。
- `grplo` / `grphi`:这个 rcu_node 覆盖的 CPU 编号范围。
- `qsmask`:本节点上**还需要上报 QS 的 CPU 位图**。对于叶子节点,**第 `i` 位 = CPU `(grplo + i)`**。

**样例**(16 CPU 系统,叶子节点 = 16 个 CPU 一个,简化为单 root 节点):

```
 rcuc/3-32  [003] .... 12345.679060: rcu_grace_period_init: rcu_sched 1024 0 0 15 ffff
```

意思是:`gp_seq=1024`,root 节点覆盖 CPU 0~15,所有 16 个 CPU 都要上报(`0xffff`)。

**多级树的样例**(假设 16 CPU、每节点 4 子):

```
 rcuc/3-32 [003] .... 12345.679060: rcu_grace_period_init: rcu_sched 1024 0 0 15 f      # root,4 子节点都要
 rcuc/3-32 [003] .... 12345.679062: rcu_grace_period_init: rcu_sched 1024 1 0 3  f      # 中间节点 0: CPU 0~3
 rcuc/3-32 [003] .... 12345.679064: rcu_grace_period_init: rcu_sched 1024 1 4 7  f      # 中间节点 1: CPU 4~7
 rcuc/3-32 [003] .... 12345.679066: rcu_grace_period_init: rcu_sched 1024 1 8 11 f      # 中间节点 2: CPU 8~11
 rcuc/3-32 [003] .... 12345.679068: rcu_grace_period_init: rcu_sched 1024 1 12 15 f     # 中间节点 3: CPU 12~15
```

注意:**叶子节点 `qsmask` 的位才是 CPU**;中间节点 `qsmask` 的位对应子节点。

**分析手段**:
- 这次 GP 开始时,把所有**叶子节点**(`level` 最高的那批)的 `qsmask` 收集起来 —— 这就是 RCU 在等的完整 CPU 集合。
- 对每个叶子节点,`grplo` 起算 + `qsmask` 位索引 = 具体 CPU。
- 例:叶子节点 `grplo=4 qsmask=5`(`0b0101`)= CPU 4 和 CPU 6 要报。

> **重要前提**:GP 不会带着未清零的 mask 结束。`tree.c:2109-2111` 里 `rcu_gp_fqs_loop()` 退出循环(进入 cleanup)的唯一条件是 `rnp->qsmask == 0 && 无 reader 阻塞`。注释(tree.c:2104-2107)进一步说明:root 节点某一位被清,仅当对应子树的所有 CPU 都报过 QS。
>
> 所以**"GP 一直没结束" 等价于 "还有 mask 没清零"**。元凶的判定逻辑是反过来的:不是"GP 结束后看剩什么",而是"GP 卡着时,看当前 qsmask 剩的位"—— 那些位对应的 CPU 就是没上报的元凶。GP 卡住期间 RCU 会反复 FQS + `resched_cpu()`,超过 `RCU_STALL_TIMEOUT`(默认 21s)后触发 `rcu_stall_warning`。

---

### 2.3 `rcu_quiescent_state_report` —— QS 上报时 mask 的变化 ★最关键★

**定义**(`include/trace/events/rcu.h:368`):

```c
TP_PROTO(const char *rcuname, unsigned long gp_seq,
         unsigned long mask, unsigned long qsmask,
         u8 level, int grplo, int grphi, u8 gp_tasks)
TP_printk("%s %ld %lx>%lx %u %d %d %u", ...)
```

**字段**:`rcuname gp_seq mask>qsmask level grplo grphi gp_tasks`。

**触发点**(`tree.c:2362-2366`,`rcu_report_qs_rnp()` 内,**沿树向上传播 QS 时每一层都打一次**):

```c
WRITE_ONCE(rnp->qsmask, rnp->qsmask & ~mask);   // 清掉 mask 位
trace_rcu_quiescent_state_report(rcu_state.name, rnp->gp_seq,
                                 mask, rnp->qsmask, rnp->level,
                                 rnp->grplo, rnp->grphi,
                                 !!rnp->gp_tasks);
```

**字段含义**:
- `mask`:本次被清掉的位。
- `qsmask`:清掉之后,本节点**剩余**还没上报的位。
- `level grplo grphi`:同 init。
- `gp_tasks`:本节点是否有 PREEMPT_RCU 阻塞 reader(`rnp->gp_tasks` 非空)。非零说明有 reader 卡着 GP。

**样例**:

```
 swapper/2-0 [002] ..s. 12345.700100: rcu_quiescent_state_report: rcu_sched 1024 4>ffb 1 0 3 0
 swapper/2-0 [002] ..s. 12345.700101: rcu_quiescent_state_report: rcu_sched 1024 1>e 0 0 15 0
```

**解读**:
- 第一行:叶子节点(grplo=0..3)收到一个 QS(`mask=4`= CPU 2),清完剩 `qsmask=0xb`(CPU 0、1、3 还要等)。
- 第二行:本叶子 qsmask 归零,向上一层 root 报告,清掉 root 的位 `1`(对应这个叶子节点)。

**怎么定位元凶 CPU** —— 这是本文核心:

1. 从 `rcu_grace_period_init` 拿到这次 GP 所有叶子节点的初始 `qsmask`。
2. 跟踪同一个 `gp_seq` 的所有 `rcu_quiescent_state_report`,叶子层的 `mask` 解码出**已上报的 CPU**。
3. 初始 mask XOR 已上报 mask = **没上报的 CPU**。
4. 如果有 PREEMPT_RCU,还要看 `gp_tasks` 是否非零 —— 是的话是被 reader 卡住,不是 CPU 没动。

**举例**:叶子节点 `grplo=4 qsmask=0xf`(CPU 4~7 全要等),trace 里这一 GP 该节点出现:
```
rcu_quiescent_state_report: rcu_sched 1024 1>f 1 4 7 0   # CPU 4 上报
rcu_quiescent_state_report: rcu_sched 1024 4>b 1 4 7 0   # CPU 6 上报
rcu_quiescent_state_report: rcu_sched 1024 2>9 1 4 7 0   # CPU 5 上报
```
结束前 qsmask 停在 `0x9`(CPU 4 和 CPU 7 还没报) —— CPU 7 没报 → 元凶。

(其实 `0x9 = 0b1001` = bit 0 + bit 3 = CPU `(grplo+0)` 和 CPU `(grplo+3)` = CPU 4 和 7,但 CPU 4 已经通过 `mask=1` 上报,所以唯一没报的是 CPU 7。)

**简化口诀**:`mask` 位 → 清掉的 CPU,连续看一串,最后剩下的就是没报的。

---

### 2.4 `rcu_fqs` —— GP kthread 强行检测 QS

**定义**(`include/trace/events/rcu.h:411`):

```c
TP_PROTO(const char *rcuname, unsigned long gp_seq, int cpu, const char *qsevent)
TP_printk("%s %ld %d %s", ...)
```

**字段**:`rcuname gp_seq cpu qsevent`。

**触发点**(`tree.c:834`、`tree.c:870`,`rcu_dynticks_fqs()` 内):FQS 扫描每个 CPU,检测到 dyntick 计数器变化就认为该 CPU 经过 QS。

`qsevent` 当前只有一个值:
- `dti` —— dyntick-idle,远程检测到这个 CPU 在 EQS 了,代它上报。

**样例**:

```
 rcuc/3-32 [003] .... 12345.850000: rcu_fqs: rcu_sched 1024 7 dti
```

**含义**:GP `1024`,FQS 代 CPU 7 上报了 QS(因为它一直 idle/用户态,内核自动检测)。

**分析手段**:
- 出现 `rcu_fqs` 就说明 GP kthread 已经主动介入(过了 `jiffies_till_first_fqs` 时间,默认约 200ms)—— 慢的早期信号。
- 如果一个 CPU **反复出现** `rcu_fqs` 还没让 GP 结束,说明它**不在 EQS**(在 kernel 里跑),但又不主动 context switch。典型是死循环或长时间关抢占。

---

### 2.5 `rcu_sr_normal` —— 你的 `synchronize_rcu` 本身

**定义**(`include/trace/events/rcu.h:681`):

```c
TP_PROTO(const char *rcuname, struct rcu_head *rhp, const char *srevent)
TP_printk("%s rhp=0x%p event=%s", ...)
```

**触发点**(`tree.c:3281` 请求、`tree.c:3308` 完成):

| srevent | 触发点 | 含义 |
|---------|--------|------|
| `request` | `tree.c:3281` | `synchronize_rcu()` 被调用 |
| `complete` | `tree.c:3308` | 这次 `synchronize_rcu` 返回了 |

**样例**:

```
 my_app-1234 [005] .... 12345.678000: rcu_sr_normal: rcu_sched rhp=0xffff8881002a3f10 event=request
 my_app-1234 [005] .... 12345.879200: rcu_sr_normal: rcu_sched rhp=0xffff8881002a3f10 event=complete
```

**分析手段**:
- 同一个 `rhp` 指针,`request` 和 `complete` 之间的时间就是**这次 `synchronize_rcu` 的耗时**。
- 跨进程/多次调用时,按 `rhp` 比对即可。
- 把这个跟 `rcu_grace_period` 的 `gp_seq` 时间线对齐,就能看到你的 `synchronize_rcu` 卡在哪次 GP 上。

---

### 2.6 `rcu_watching` —— EQS 转换(原 `rcu_dyntick`)

**定义**(`include/trace/events/rcu.h:480`):

```c
TP_PROTO(const char *polarity, long oldnesting, long newnesting, int counter)
TP_printk("%s %lx %lx %#3x", ...)
```

**字段**:`polarity oldnesting newnesting counter`。

**触发点**:`kernel/context_tracking.c` 内 `ct_kernel_enter/exit` 等,标志 CPU 进入/退出 EQS。

`polarity` 取值:
- `Start` —— 进 EQS(kernel → idle/user)
- `End` —— 出 EQS
- `StillWatching` —— 嵌套,不影响 EQS 状态

`counter` 是 `ct->state` 的 RCU watching 计数(高位部分),**奇数在 kernel,偶数在 EQS**。

**样例**:

```
 swapper/2-0 [002] .... 12345.700010: rcu_watching: Start 1 0 0x2
 swapper/2-0 [002] .... 12345.700020: rcu_watching: End   0 1 0x3
```

**含义**:CPU 2 进 EQS(counter 0x1→0x2),然后又出 EQS(0x2→0x3)。

**分析手段**:
- 这个事件是 per-CPU 的,看 `[CPU]` 列就能知道是哪个 CPU。
- **某 CPU 在 GP 期间从来没出现过 `Start`** —— 它一直在 kernel,这就是 QS 上报不来的根本原因。
- 配合 `rcu_fqs`:`rcu_fqs` 在等 dyntick 变化,但 `rcu_watching` 没动 —— 这个 CPU 没经过任何 EQS。

---

### 2.7 `rcu_preempt_task` / `rcu_unlock_preempted_task` —— PREEMPT_RCU 阻塞 reader

**仅 `CONFIG_PREEMPT_RCU=y` 才有意义。**

**定义**(`include/trace/events/rcu.h:312` / `339`):

```c
rcu_preempt_task:            "%s %ld %d"          // rcuname gp_seq pid
rcu_unlock_preempted_task:   "%s %ld %d"          // rcuname gp_seq pid
```

**触发点**:
- `rcu_preempt_task`:`rcu_preempt_ctxt_queue()`(`tree_plugin.h`),reader 在 read-side CS 内被抢占,**阻塞当前 GP**。
- `rcu_unlock_preempted_task`:reader 退出 read-side CS。

**样例**:

```
 my_kthd-2048 [004] .... 12345.700100: rcu_preempt_task: rcu_preempt 1024 2048
 my_kthd-2048 [004] .... 12345.750200: rcu_unlock_preempted_task: rcu_preempt 1024 2048
```

**分析手段**:
- 配合 `rcu_quiescent_state_report` 的 `gp_tasks` 字段(非零 = 这个节点有 reader 阻塞)。
- 元凶 PID 直接给出 —— 不再是"CPU 没动",而是"这个 task 持 read lock 太久"。
- 看 `rcu_preempt_task` 到 `rcu_unlock_preempted_task` 的时间 = reader 阻塞 GP 的时长。

---

### 2.8 `rcu_stall_warning` —— stall detector 触发

**定义**(`include/trace/events/rcu.h:444`):

```c
TP_PROTO(const char *rcuname, const char *msg)
TP_printk("%s %s", ...)
```

`msg` 取值(`tree_stall.h`):
- `StallDetected` —— 调度 tick 检测到其他 CPU stall
- `SelfDetected` —— 检测到当前 CPU stall
- `ExpeditedStall` —— expedited GP stall

**出现就是严重情况**:`rcu_cpu_stall_timeout`(`RCU_STALL_TIMEOUT`)过期了。接下来 dmesg 里会打印栈和 `print_cpu_stall_info`。

---

## 3. 端到端:定位"哪个 CPU 没上报 QS"的完整流程

### 步骤

1. **开事件**(只开必要的):

   ```bash
   cd /sys/kernel/tracing
   echo 0 > events/enable; echo nop > current_tracer; echo > trace
   for e in rcu_grace_period rcu_grace_period_init rcu_quiescent_state_report \
            rcu_fqs rcu_sr_normal rcu_watching rcu_stall_warning; do
       echo 1 > events/rcu/$e/enable
   done
   # PREEMPT_RCU 加:
   echo 1 > events/rcu/rcu_preempt_task/enable
   echo 1 > events/rcu/rcu_unlock_preempted_task/enable
   echo 1 > tracing_on
   ```

2. **复现慢的场景**,同步抓 trace:

   ```bash
   cat trace_pipe > /tmp/rcu.trace &
   # 跑触发 synchronize_rcu 慢的负载
   ```

3. **挑一个慢的 GP 序号**:

   ```bash
   # 找 request → complete 间隔长的 synchronize_rcu
   grep rcu_sr_normal /tmp/rcu.trace
   # 看该时间段内的 gp_seq
   ```

4. **(快速路径)直接看 `cpuqs`**:同 GP 时间窗内,所有出现过 `cpuqs` 的 CPU 都已通过 QS。

   ```bash
   GP=1024
   # 圈出 GP 时间窗
   grep "rcu_grace_period: rcu_sched $GP start\$"   /tmp/rcu.trace    # 起点
   grep "rcu_grace_period: rcu_sched $GP end\$"     /tmp/rcu.trace    # 终点
   # 这个窗口里所有 cpuqs 涉及的 CPU(ftrace 行格式下 [NNN] 就是 CPU)
   awk -v s=$T0 -v e=$T1 '$1>=s && $1<=e && /rcu_grace_period:.*cpuqs/ {print $4}' /tmp/rcu.trace | sort -u
   ```

   online CPU 集合减去这个列表 = **元凶候选**。这是最快的判定方法。

5. **(精确路径)用 `rcu_grace_period_init` 拿叶子节点初始 mask 做交叉验证**:

   ```bash
   grep "rcu_grace_period_init:.* $GP " /tmp/rcu.trace | sort -k7 -n
   # 找 level 最大的一批(叶子节点)
   ```

   得到形如:
   ```
   ... rcu_grace_period_init: rcu_sched 1024 1 0  3  f       # CPU 0,1,2,3
   ... rcu_grace_period_init: rcu_sched 1024 1 4  7  f       # CPU 4,5,6,7
   ... rcu_grace_period_init: rcu_sched 1024 1 8  11 f       # CPU 8,9,10,11
   ... rcu_grace_period_init: rcu_sched 1024 1 12 15 f       # CPU 12,13,14,15
   ```

6. **用 `rcu_quiescent_state_report` 确认每个 CPU 的上报时刻**:

   ```bash
   grep "rcu_quiescent_state_report:.* $GP " /tmp/rcu.trace
   ```

   找 `gp_tasks=0`(无 reader 阻塞)的叶子层事件,逐步看每个叶子节点的 `mask>qsmask` 演化。对每个叶子节点,初始 `qsmask` 减掉所有 report 中被清的位,剩下的位 = 没上报的 CPU 编号。

   > 注:步骤 4 的 `cpuqs` 列表通常已经够用。步骤 5-6 的好处是能精确算出**每个 CPU 上报的先后顺序**,也能避免 `cpuqs` 在 GP 切换瞬间可能漏报的边角情况。

7. **验证元凶 CPU 卡在哪**:
   - `grep "$CULPRIT_CPU" /tmp/rcu.trace | grep rcu_watching` —— 该 CPU 在 GP 期间有没有进过 EQS?没进过 → 一直在 kernel。
   - `grep rcu_fqs /tmp/rcu.trace | grep " $CULPRIT_CPU "` —— FQS 替它报过吗?报过但 GP 还没结束说明它在 kernel 但不调度。
   - 抓 stack:`cat /proc/$PID/stack` 或 perf record -g -C $CULPRIT_CPU。

### 一个完整的简化样例

慢的 GP 序号 `1024`,4 CPU 系统(单 root 节点):

```
swapper/1-0 [001] ..s. 100.001: rcu_grace_period: rcu_sched 1024 newreq
 rcuc/0-12 [000] .... 100.002: rcu_grace_period: rcu_sched 1024 start
 rcuc/0-12 [000] .... 100.003: rcu_grace_period_init: rcu_sched 1024 0 0 3 f     # 等 CPU 0,1,2,3
 rcuc/0-12 [000] .... 100.004: rcu_grace_period: rcu_sched 1024 fqswait
swapper/0-0 [000] ..s. 100.500: rcu_quiescent_state_report: rcu_sched 1024 1>e 0 0 3 0   # CPU 0 报
swapper/2-0 [002] ..s. 100.510: rcu_quiescent_state_report: rcu_sched 1024 4>b 0 0 3 0   # CPU 2 报,剩 b=1011
 rcuc/0-12 [000] .... 101.000: rcu_grace_period: rcu_sched 1024 fqsstart          # 200ms 了,GP kthread 着急
 rcuc/0-12 [000] .... 101.001: rcu_fqs: rcu_sched 1024 1 dti                       # CPU 1 被 FQS 检测到 EQS
 rcuc/0-12 [000] .... 101.002: rcu_quiescent_state_report: rcu_sched 1024 2>9 0 0 3 0   # CPU 1 报,剩 9=1001
 rcuc/0-12 [000] .... 101.003: rcu_grace_period: rcu_sched 1024 fqsend
 ...                                                                       # CPU 3 一直不报
 swapper/3-0 [003] ..s. 105.000: rcu_quiescent_state_report: rcu_sched 1024 8>1 0 0 3 0   # 5s 后 CPU 3 终于报
 rcuc/0-12 [000] .... 105.001: rcu_grace_period: rcu_sched 1024 end
```

**结论**:CPU 3 卡了 4 秒。`mask=8`(bit 3)是最后才被清的位。配合 `rcu_watching`:

```
 grep "\[003\].*rcu_watching" /tmp/rcu.trace
```

如果 CPU 3 在 100.005~104.999 之间没有 `Start` 事件 → 它在 kernel 跑了 4 秒,没经过任何 EQS。抓它的栈,大概率是死循环或长关抢占的代码。

## 4. 快速查表

| 你想知道什么 | 看哪个事件 |
|---|---|
| GP 耗时 | `rcu_grace_period` 的 `start`/`end` 时间差 |
| GP kthread 何时开始着急 | `rcu_grace_period` 的 `fqswait`/`fqsstart` |
| 谁请求了 GP | `rcu_grace_period` 的 `newreq`,看 pid/comm |
| `synchronize_rcu` 自身耗时 | `rcu_sr_normal` 同 `rhp` 的 `request`/`complete` |
| GP 在等哪些 CPU | `rcu_grace_period_init` 叶子层 `qsmask` |
| 哪些 CPU 已经通过 QS(最快判定) | `rcu_grace_period` 的 `cpuqs`,看 `[CPU]` 列 |
| 哪些 CPU 已经上报到 node 树 | `rcu_quiescent_state_report` 叶子层 `mask` 字段 |
| 哪个 CPU 没上报 | online CPU 集合 减去 出现过 `cpuqs` 的集合 |
| RCU 是否在 force | `rcu_fqs` 出现 = 是 |
| CPU 是不是一直没进 EQS | 该 CPU 的 `rcu_watching` 期间有无 `Start` |
| PREEMPT_RCU 谁卡了 GP | `rcu_preempt_task` 的 pid |
| 是不是 reader 卡的 | `rcu_quiescent_state_report` 的 `gp_tasks` 字段 |
| 是否 stall | `rcu_stall_warning` |

## 5. 参考源码索引

| 文件:行 | 内容 |
|---|---|
| `include/trace/events/rcu.h` 全文 | 所有 event 定义 |
| `kernel/rcu/tree.c:1859` | `start` 事件 |
| `kernel/rcu/tree.c:1964` | `rcu_grace_period_init` 触发点 |
| `kernel/rcu/tree.c:2363` | `rcu_quiescent_state_report` 触发点 |
| `kernel/rcu/tree.c:834`、`870` | `rcu_fqs` 触发点 |
| `kernel/rcu/tree.c:3281`、`3308` | `rcu_sr_normal` 触发点 |
| `kernel/rcu/tree.c:2220` | `end` 事件 |
| `kernel/rcu/tree_plugin.h:298` | preempt RCU 的 `cpuqs` |
| `kernel/rcu/tree_plugin.h:950` | !preempt RCU 的 `cpuqs` |
| `kernel/rcu/tree.h:464-477` | flavor 名字定义 |
| `kernel/rcu/tree_stall.h` | stall 检测和打印 |
