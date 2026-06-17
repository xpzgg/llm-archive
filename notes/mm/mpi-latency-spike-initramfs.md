# MPI Latency Spike：6.6 内核 + initramfs 排查记录

## 问题描述

```bash
taskset -c 0 mpirun --allow-run-as-root --cpu-list 2,16 -np 2 \
  --bind-to cpu-list:ordered --report-bindings \
  -x UCX_TLS=posix -x UCX_RNDV_SEND_NBR_THRESH=8K \
  --use-hwthread-cpus ./osu_latency -x -i -m 2048:2048
```

- 传输：UCX POSIX shared memory（/dev/shm），2048 字节消息
- CPU 2 和 CPU 16，同一 NUMA 节点

**现象：**
- initramfs（无盘）环境：latency 不稳定，从 ~1μs 抖到 600+μs
- 带盘 full OS 环境：latency 稳定在 ~1μs

## 关键测试矩阵

| 内核版本 | 环境 | 结果 |
|---------|------|------|
| 5.10 | initramfs | OK |
| 5.10 | full OS | OK |
| 6.6 | initramfs | **BAD（~12s/次）** |
| 6.6 | full OS | OK |

emu 环境（无虚拟化，不同硬件平台）也能复现 → 排除特定硬件，确认为纯内核软件问题。

## osu_latency 测试机制说明

- 报告的是**单次运行所有迭代的均值**，不是逐迭代输出
- 2048 字节消息默认 **10,000 次迭代**
- "latency 600μs"意味着整次运行耗时约 **12 秒**（10,000 × 600μs × 2）
- 正常运行约 20ms（10,000 × 1μs × 2）
- **结论：坏的运行是整次运行全面降级，不是偶发 spike**

## 已排除的方向

- 内存 reclaim（pgreclaim vmstat 字段，好坏情况相近）
- 内存 compaction
- NUMA（CPU 2/16 同一 NUMA 节点）
- pgfault（好坏情况均约 1.5w 次）
- 物理硬件 thermal（emu 也复现）
- EL3 固件（视 emu 配置，但整体偏向软件侧）

## 根因假设

问题严格要求 **6.6 内核 + 无盘（initramfs）** 两个条件同时满足。

无盘环境的内核层差异：
- 无 block device，无 bdi writeback 线程
- kworker 没有磁盘 I/O 工作，空转后漂移到任意 CPU
- kswapd 无法实际回收文件页，MGLRU 扫描模式异常
- 系统整体更"安静"，调度行为与有盘时不同

6.6 vs 5.10 的内核变化：
- **EEVDF 替换 CFS**（6.6）：wakeup preemption 更激进
- **Unbound workqueue non-strict affinity**（commit `8639ecebc9b1`）：kworker 可自由迁移到测试 CPU
- **Workqueue idle timer → deferred work_struct**（commit `3f959aa3b338`）：kworker 唤醒行为改变
- **MGLRU**（6.1 合入，6.6 默认启用）：无盘环境下 kswapd 扫描行为异常

---

## 排查方案一：假设验证（逐一排除）

### Step 1：先观察，不要猜

在坏的运行期间（另一个终端）：

```bash
# 看进程状态：R=busy-polling，S=阻塞睡眠
watch -n 0.1 'ps -eo pid,psr,stat,comm | grep osu_latency'

# 看 CPU 2/16 上谁在跑（实时）
perf top -C 2,16
```

### Step 2：快速开关验证

```bash
# 2a. 禁用 MGLRU
echo 0 > /sys/kernel/mm/lru_gen/enabled
# 重跑测试，看是否恢复正常

# 2b. 还原 MGLRU，尝试 workqueue affinity（先确认参数存在）
echo 1 > /sys/kernel/mm/lru_gen/enabled
ls /sys/module/workqueue/parameters/
# 如果有 default_affinity_scope：
echo cpu > /sys/module/workqueue/parameters/default_affinity_scope

# 2c. 禁用 THP
echo never > /sys/kernel/mm/transparent_hugepage/enabled
```

### Step 3：definitive — perf sched

```bash
perf sched record -C 2,16 -o /tmp/sched.perf -- \
  taskset -c 0 mpirun --allow-run-as-root --cpu-list 2,16 -np 2 \
  --bind-to cpu-list:ordered -x UCX_TLS=posix \
  -x UCX_RNDV_SEND_NBR_THRESH=8K --use-hwthread-cpus \
  ./osu_latency -x -i -m 2048:2048

# 各任务最大调度延迟
perf sched latency -i /tmp/sched.perf --sort max

# 详细时间线
perf sched timehist -i /tmp/sched.perf 2>/dev/null | head -200
```

### Step 4：如果以上没结论 → ftrace

```bash
cd /sys/kernel/debug/tracing
echo 0 > tracing_on && echo > trace
echo 32768 > buffer_size_kb
echo "sched:sched_switch" > set_event
echo "irq:irq_handler_entry" >> set_event
printf '%x' $((1<<2 | 1<<16)) > tracing_cpumask
echo 1 > tracing_on

taskset -c 0 mpirun --allow-run-as-root --cpu-list 2,16 -np 2 \
  --bind-to cpu-list:ordered -x UCX_TLS=posix \
  -x UCX_RNDV_SEND_NBR_THRESH=8K --use-hwthread-cpus \
  ./osu_latency -x -i -m 2048:2048

echo 0 > tracing_on
cat trace > /tmp/bad_run_trace.txt
```

trace 结果解读：
- 有 `sched_switch`（osu_latency 被换出）→ 调度抢占，看换入的是谁
- 有 `irq_handler_entry` → 中断干扰
- 什么都没有 → 进程在跑但通信机制有问题（看排查方案二）

### Step 5：内核版本二分（如果需要定位 commit）

测试中间版本快速收敛：

```
5.10 (OK) → 5.15 → 6.1 → 6.4 → 6.6 (BAD)
```

- 6.1 BAD → MGLRU 是根因（6.1 引入）
- 6.1 OK，6.6 BAD → EEVDF 或 workqueue 变化（6.6 引入）

---

## 排查方案二：正向分析（直接测量时间花在哪里）

### 第一层：进程状态定性（1 分钟）

```bash
watch -n 0.1 'ps -eo pid,psr,stat,comm | grep osu_latency'
```

- 一直 `R` → busy-polling，时间丢在"跑着等不到数据"，走 **A 路径**
- 频繁 `S` → 进程阻塞睡眠，时间丢在"唤醒延迟"，走 **B 路径**

### A 路径：进程在跑但等不到数据

```bash
# 看 CPU 上时间分布（perf record）
perf record -C 2,16 -g -F 5000 -o /tmp/bad.perf -- \
  taskset -c 0 mpirun --allow-run-as-root --cpu-list 2,16 -np 2 \
  --bind-to cpu-list:ordered -x UCX_TLS=posix \
  -x UCX_RNDV_SEND_NBR_THRESH=8K --use-hwthread-cpus \
  ./osu_latency -x -i -m 2048:2048

perf report -i /tmp/bad.perf --no-children
```

主要看：是在内核函数还是 UCX 用户态代码里耗时。

```bash
# 详细时间线：每次任务切换的等待时间
perf sched record -C 2,16 -o /tmp/sched.perf -- ./test
perf sched timehist -i /tmp/sched.perf 2>/dev/null | grep osu_latency
```

### B 路径：进程在阻塞睡眠

```bash
# 看 syscall 模式和耗时
strace -p <pid_rank0> -p <pid_rank1> -T \
  -e trace=futex,epoll_wait,nanosleep,sched_yield 2>&1

# 确认 UCX 使用的传输和 async 模式
UCX_LOG_LEVEL=info taskset -c 2 ./osu_latency -x -i -m 2048:2048 2>&1 \
  | grep -iE "transport|posix|shm|async"
```

- 有 `futex(WAIT)` 且耗时 ~600μs → UCX 使用阻塞模式，唤醒慢
- 有 `sched_yield` → UCX 主动让出 CPU

### 第二层：MPI 函数级别计时

```bash
# bpftrace 测每次 MPI_Send 耗时分布（替换 libmpi 路径）
bpftrace -e '
uprobe:/path/to/libmpi.so:MPI_Send { @ts[tid] = nsecs; }
uretprobe:/path/to/libmpi.so:MPI_Send {
  @dist = hist((nsecs - @ts[tid]) / 1000);
  delete(@ts[tid]);
}'
```

输出直方图：是否有双峰（1μs 快路径 + 600μs 慢路径）。

---

## 执行决策树

```
第一层：watch ps stat
    │
    ├─ 一直 R（busy-polling）
    │       ↓
    │   perf top -C 2,16（看是否有内核线程抢占）
    │       ├─ 有 kswapd/kworker 大量出现 → 方案一 Step2（禁 MGLRU / workqueue affinity）
    │       └─ 只有 osu_latency → perf sched timehist 看时间线细节
    │
    └─ 频繁 S（阻塞）
            ↓
        strace 看 futex 耗时
            ├─ futex 耗时 ~600μs → EEVDF wakeup latency 问题
            │       → 试 chrt -f 99（SCHED_FIFO 绕过 EEVDF）
            └─ 无明显 syscall → UCX 内部问题，看 UCX_LOG_LEVEL=info
```

---

## 补充工具一：火焰图

坏的运行持续 ~12 秒，窗口足够，分两个终端操作：

```bash
# 终端1：跑测试
taskset -c 0 mpirun --allow-run-as-root --cpu-list 2,16 -np 2 \
  --bind-to cpu-list:ordered -x UCX_TLS=posix \
  -x UCX_RNDV_SEND_NBR_THRESH=8K --use-hwthread-cpus \
  ./osu_latency -x -i -m 2048:2048

# 终端2：发现是坏的运行（测试没有快速结束），立刻抓 5 秒
perf record -C 2,16 -g -F 999 -o /tmp/flame.perf -- sleep 5
```

生成火焰图（需要 FlameGraph 工具）：
```bash
perf script -i /tmp/flame.perf | \
  stackcollapse-perf.pl | flamegraph.pl > /tmp/flame.svg
```

没有 FlameGraph 工具时直接看：
```bash
perf report -i /tmp/flame.perf --no-children --stdio | head -60
```

**结论判断：**
- 时间集中在 UCX/用户态函数 → 通信机制问题，数据没到
- 时间集中在 kswapd/kworker/调度器内核函数 → 内核线程抢占

---

## 补充工具二：逐迭代 latency（替代 osu_latency）

osu_latency 只报均值，无法区分"每次都慢一点"还是"少数几次超长拉高均值"。用以下替代程序直接打印慢迭代：

```c
// ping_pong.c
#include <mpi.h>
#include <stdio.h>
#include <string.h>
/*
 * 调测一：精确拆分 Send 和 Recv 各自耗时
 * 启用方法：取消下面这行注释，并在循环体内替换对应代码块
 *
 * #include <time.h>
 *
 * 在迭代循环内替换为：
 *
 *     double t0 = MPI_Wtime();
 *     if (rank == 0) {
 *         MPI_Send(buf, MSG_SIZE, MPI_CHAR, 1, 0, MPI_COMM_WORLD);
 *         double t1 = MPI_Wtime();
 *         MPI_Recv(buf, MSG_SIZE, MPI_CHAR, 1, 0, MPI_COMM_WORLD, &st);
 *         double t2 = MPI_Wtime();
 *         if (i >= WARMUP)
 *             printf("iter %5d  send=%.1fus  recv_wait=%.1fus\n",
 *                    i - WARMUP, (t1-t0)*1e6, (t2-t1)*1e6);
 *     } else {
 *         MPI_Recv(buf, MSG_SIZE, MPI_CHAR, 0, 0, MPI_COMM_WORLD, &st);
 *         MPI_Send(buf, MSG_SIZE, MPI_CHAR, 0, 0, MPI_COMM_WORLD);
 *     }
 *
 * 用途：区分时间丢在发送侧还是等待回包侧。
 */

/*
 * 调测二：慢迭代写入 ftrace trace_marker，与 ftrace 时间线精确对齐
 * 启用方法：
 *   1. 取消下面的 #include 注释
 *   2. 在 main() 开头取消 marker_fd 初始化注释
 *   3. 在慢迭代打印处取消 write 注释
 *
 * #include <fcntl.h>
 * #include <unistd.h>
 *
 * main() 开头加：
 *     int marker_fd = -1;
 *     if (rank == 0)
 *         marker_fd = open("/sys/kernel/debug/tracing/trace_marker", O_WRONLY);
 *
 * 慢迭代处加（放在 printf 之后）：
 *     if (marker_fd >= 0) {
 *         char msg[64];
 *         snprintf(msg, sizeof(msg), "SLOW iter=%d lat=%.1fus", i - WARMUP, lat);
 *         write(marker_fd, msg, strlen(msg));
 *     }
 *
 * 用途：ftrace 里直接搜 SLOW 标记，找前后 sched_switch 事件，
 *       不用再猜"哪段时间是坏的迭代"。
 * 配合 ftrace 使用：
 *     echo "sched:sched_switch" > /sys/kernel/debug/tracing/set_event
 *     printf '%x' $((1<<2 | 1<<16)) > /sys/kernel/debug/tracing/tracing_cpumask
 *     echo 1 > /sys/kernel/debug/tracing/tracing_on
 */

#define MSG_SIZE    2048
#define WARMUP      100
#define ITERATIONS  10000
#define SLOW_US     10.0   // 超过此阈值才打印

int main(int argc, char *argv[]) {
    int rank;
    char buf[MSG_SIZE];
    MPI_Status st;

    MPI_Init(&argc, &argv);
    MPI_Comm_rank(MPI_COMM_WORLD, &rank);
    memset(buf, 0, MSG_SIZE);

    double total = 0;
    int slow = 0;

    for (int i = 0; i < WARMUP + ITERATIONS; i++) {
        double t = MPI_Wtime();

        if (rank == 0) {
            MPI_Send(buf, MSG_SIZE, MPI_CHAR, 1, 0, MPI_COMM_WORLD);
            MPI_Recv(buf, MSG_SIZE, MPI_CHAR, 1, 0, MPI_COMM_WORLD, &st);
        } else {
            MPI_Recv(buf, MSG_SIZE, MPI_CHAR, 0, 0, MPI_COMM_WORLD, &st);
            MPI_Send(buf, MSG_SIZE, MPI_CHAR, 0, 0, MPI_COMM_WORLD);
        }

        if (i < WARMUP) continue;

        double lat = (MPI_Wtime() - t) * 1e6 / 2.0;
        total += lat;

        if (rank == 0 && lat > SLOW_US) {
            printf("iter %5d: %.1f us\n", i - WARMUP, lat);
            slow++;
        }
    }

    if (rank == 0)
        printf("avg %.2f us | slow %d / %d\n", total / ITERATIONS, slow, ITERATIONS);

    MPI_Finalize();
    return 0;
}
```

```bash
mpicc -O2 -o ping_pong ping_pong.c

taskset -c 0 mpirun --allow-run-as-root --cpu-list 2,16 -np 2 \
  --bind-to cpu-list:ordered -x UCX_TLS=posix \
  -x UCX_RNDV_SEND_NBR_THRESH=8K --use-hwthread-cpus \
  ./ping_pong
```

**结论判断：**
- 打印大量慢迭代（每次都 >10μs）→ 整次运行持续降级，每个迭代都受影响
- 只打印 1-2 行但延迟是几百万 μs → 单次/少次灾难性停顿拉高均值，其余迭代正常

两者根因方向完全不同，此步最先跑，结论最直接。

---

## 参考：相关内核代码位置

- `kernel/workqueue.c`：non-strict affinity（行 1104-1116），idle_cull_work 初始化（行 3529）
- `kernel/sched/fair.c`：EEVDF wakeup_preempt_fair（行 8987-9195）
- `kernel/sched/features.h`：RUN_TO_PARITY, PREEMPT_SHORT, WAKEUP_PREEMPTION
- `mm/vmscan.c`：MGLRU reclaim throttle（行 5027-5042），kswapd 行为
- `mm/lru_gen.c`：MGLRU 主逻辑
