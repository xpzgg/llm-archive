# 串口 Console 打印导致 Hardlockup 排障指南

## TL;DR

串口 console 是低带宽的调试和救援通道，不适合作为生产环境的全量内核日志输出通道；当内核日志量很大时，`printk()` 可能在中断上下文里同步刷慢速串口，导致 CPU 长时间关中断忙等，最终被 hardlockup watchdog 判定为卡死。

推荐处置优先级：

| 优先级 | 操作 | 是否重启 | 说明 |
|---|---|---:|---|
| 1 | 去掉串口 console，例如移除 `console=ttyAMA0,115200`，保留 `console=tty0` | 是 | 最彻底，运行期不再同步刷 PL011 串口 |
| 2 | 降低 console loglevel，例如 `echo 4 > /proc/sys/kernel/printk` | 否 | 只让 `ERR` 及以上日志上 console，`dmesg` 仍可看 ring buffer |
| 3 | 持久降低 console loglevel，例如 `kernel.printk = 4 4 1 7` | 否 | 重启后保留，适合作为默认生产配置 |
| 4 | 排查并限速日志风暴源头 | 视情况 | 用 `*_ratelimited()`、tracepoint 或计数器替代高频 `printk()` |

串口的主要价值是早期启动、panic/oops、网络不可用时的带外救援和硬件 bring-up。生产环境通常不会让串口打印所有级别日志；全量日志应通过 `/dev/kmsg`、journald、rsyslog、日志 agent、kdump 或 pstore 保存。

## 背景

### 1. Console、printk Ring Buffer 和日志消费者

内核日志不是直接写串口。正常路径是：`printk()` 先把日志写入内核 printk ring buffer，然后不同消费者按自己的进度读取。

```
                         写入 printk ring buffer
                                      |
+-------------------------------------+-------------------------------------+
|                                                                           |
|  生产者                                                                    |
|                                                                           |
|  printk()/pr_err()/dev_warn()                                             |
|  WARN/OOPS/panic/RCU stall/watchdog                                       |
|  驱动、子系统、内核线程、中断处理                                          |
|  用户态写 /dev/kmsg                                                        |
|                                                                           |
+-------------------------------------+-------------------------------------+
                                      |
                                      v
                         +-------------------------+
                         | printk ring buffer      |
                         | 固定容量、按 seq 编号   |
                         +-------------------------+
                                      |
+-------------------------------------+-------------------------------------+
|                                                                           |
|  消费者                                                                    |
|                                                                           |
|  串口/VGA/netconsole 等 console driver：各自维护 con->seq                 |
|  /dev/kmsg reader：journald、rsyslog、日志 agent 各自维护读取位置          |
|  dmesg/syslog 接口：读取当前 ring buffer 视图                              |
|  kdump/vmcore、pstore/ramoops：崩溃时保存当时仍可获得的日志                |
|                                                                           |
+---------------------------------------------------------------------------+
```

每个消费者只是维护自己的读取进度。消费一条日志不会把它从 ring buffer 删除。旧日志只有在以下场景下才会不可读：

- 新日志继续写入，ring buffer 空间不够，旧 record 被覆盖；
- `dmesg -c` 等操作推进 clear 视图，让部分接口不再显示旧记录；
- 系统重启，内存里的 ring buffer 消失，除非已通过 kdump、pstore 或 journald 持久化。

如果消费者太慢，目标 `seq` 已经被覆盖，它会跳到当前最老的有效 record 继续读，并报告中间 dropped 了多少条。

### 2. 串口 Console 为什么慢

串口 UART 是低速接口。以常见的 `115200` 波特率估算：

- 1 个字节通常需要 10 bit 传输，实际吞吐约 `115200 / 10 = 11520` 字节/秒；
- 约等于 11 KB/s；
- 10 秒只能传约 112 KB；
- 一段 RCU stall、softlockup、modules list、寄存器和 backtrace 很容易达到数 KB 到数十 KB。

因此，串口可以兜底输出关键日志，但不适合承载大量 `INFO`、`DEBUG` 或日志风暴。

### 3. Console Unlock 的同步 Flush 机制

`printk()` 写入 ring buffer 后，某些上下文会尝试成为 console owner，调用 `console_unlock()` 把 console 尚未输出的日志补刷出去。关键点是：`console_unlock()` 不只打印当前这条日志，而是尽量把该 console 落后的 backlog 都刷完。

典型栈如下：

```
printk()
  vprintk_emit()
    console_unlock()
      console_flush_all()
        do while any_progress:
          for_each_console:
            console_emit_next_record()
              printk_get_next_message()      # 取一条 printk record
              local_irq_save()
              con->write()
                pl011_console_write()
                  local_irq_save()
                  uart_console_write()       # 逐字符写这一条 record
                    pl011_console_putchar()
                  wait TX busy clear
                  local_irq_restore()
              local_irq_restore()
```

这里有两个粒度需要区分：

- `console_unlock()` 粒度：批量 drain backlog，尽量把当前 ring buffer 中该 console 没输出的 record 都打印出去。
- `console_emit_next_record()` 粒度：每次取一条 record，关本地中断，调用 console driver 输出这一条 record，再恢复中断状态。

如果调用者原本就在中断上下文里，本地 IRQ 进入前已经是 disabled，那么每条 record 结束后的 `local_irq_restore()` 只会恢复到原来的 disabled 状态，并不会在两条日志之间真正打开普通 IRQ。

### 4. 慢串口如何放大成 Hardlockup

本问题常见于以下链路：

```
hrtimer_interrupt
  update_process_times
    rcu_sched_clock_irq
      check_cpu_stall
        print_other_cpu_stall
          printk
            console_unlock
              console_flush_all
                console_emit_next_record
                  pl011_console_write
                    uart_console_write
                      pl011_console_putchar
```

RCU stall 检查发生在 timer interrupt 路径中，本地 IRQ 已经关闭。如果它调用 `printk()` 后触发 `console_unlock()`，CPU 可能在 IRQ-off 状态下连续刷一批 console backlog。

PL011 串口 console 的逐字符输出会等待硬件 TX FIFO 有空间：

```c
while (pl011_read(uap, REG_FR) & UART01x_FR_TXFF)
        cpu_relax();
pl011_write(ch, uap, REG_DR);
```

一条日志输出结束后，driver 还会等 UART busy 状态清掉。日志量大或串口很慢时，CPU 虽然仍在执行 `cpu_relax()`，但普通 timer interrupt 和调度时钟无法推进。ARM64 上 SDEI/NMI-like watchdog 仍可打进来，发现 watchdog 进展停滞，于是报告 hardlockup。

这类 hardlockup 不一定表示 CPU 在业务代码里真正死锁，也不必然说明串口硬件损坏；它通常表示“慢 console 同步输出在不合适的上下文中占用了太久”。

### 5. 典型触发场景

- 硬件 RAS/CE 错误风暴，内核持续打印错误；
- 驱动进入异常状态，循环 `printk()` 或 `dev_err()`；
- 生产环境误开 debug、dynamic debug、`ignore_loglevel` 或高 console loglevel；
- RCU stall、softlockup、hardlockup 报告互相放大，stall 报告本身又制造大量 console 输出；
- 串口 console 低波特率，或 BMC SoL/串口链路非常慢。

## 排查地图

```
出现 hardlockup / panic
  |
  v
查看 hardlockup 栈里是否有 console/printk/pl011？
  |
  +-- 否 --> 优先按普通 hardlockup 排查：锁、关中断长路径、硬件/固件、NMI 屏蔽
  |
  +-- 是
       |
       v
   当前是否注册了串口 console？
       |
       +-- 否 --> 看是否是 VGA/netconsole/其他 console 慢或 printk 风暴
       |
       +-- 是
            |
            v
        是否存在日志风暴或大量 backlog？
            |
            +-- 是 --> 临时降低 console loglevel，排查日志源头，必要时禁用串口 console
            |
            +-- 不明显
                 |
                 v
             小量日志也卡在 pl011_console_putchar？
                 |
                 +-- 是 --> 怀疑串口控制器、时钟、固件描述、BMC SoL 或硬件链路异常
                 |
                 +-- 否 --> 继续观察触发窗口，重点统计 console flush 耗时和日志量
```

## 问题分析和操作步骤

### 1. 确认是否命中 Console 打印放大路径

查看 panic 或 hardlockup 栈。如果看到以下函数组合，基本可以确认 hardlockup 与 console flush 强相关：

```
console_unlock
console_flush_all
console_emit_next_record
pl011_console_write
uart_console_write
pl011_console_putchar
```

如果栈还包含：

```
hrtimer_interrupt
rcu_sched_clock_irq
check_cpu_stall
print_other_cpu_stall
printk
```

说明问题很可能是：RCU stall 检查在 timer interrupt 中打印日志，触发 `console_unlock()`，随后在 IRQ-off 上下文里批量刷慢串口。

### 2. 查看当前 Console 配置

查看 kernel cmdline：

```bash
cat /proc/cmdline
```

重点看是否有：

```text
console=ttyAMA0,115200
console=ttyS0,115200
console=tty0
ignore_loglevel
loglevel=
```

查看当前 active console：

```bash
cat /sys/class/tty/console/active
```

如果输出包含 `ttyAMA0`、`ttyS0` 等串口设备，说明运行期内核日志可能同步刷到串口。

### 3. 临时降低 Console 打印级别

查看当前设置：

```bash
cat /proc/sys/kernel/printk
```

输出示例：

```text
7 4 1 7
```

四个字段依次是：

| 字段 | 含义 |
|---|---|
| 第 1 个 | 当前 console loglevel，只有级别数字小于该值的日志会输出到 console |
| 第 2 个 | 未显式指定级别的 printk 默认级别 |
| 第 3 个 | 最低 console loglevel |
| 第 4 个 | 默认 console loglevel |

内核日志级别如下：

| 数字 | 宏定义 | 含义 |
|---:|---|---|
| 0 | `KERN_EMERG` | 系统不可用 |
| 1 | `KERN_ALERT` | 必须立即处理 |
| 2 | `KERN_CRIT` | 严重错误 |
| 3 | `KERN_ERR` | 错误 |
| 4 | `KERN_WARNING` | 警告 |
| 5 | `KERN_NOTICE` | 正常但值得关注 |
| 6 | `KERN_INFO` | 一般信息 |
| 7 | `KERN_DEBUG` | 调试信息 |

只让 `ERR` 及以上日志输出到 console：

```bash
echo 4 > /proc/sys/kernel/printk
```

更激进，只让 `CRIT` 及以上日志输出到 console：

```bash
echo 3 > /proc/sys/kernel/printk
```

也可以使用：

```bash
dmesg -n 4
sysctl -w kernel.printk="4 4 1 7"
```

注意：`echo 5 > /proc/sys/kernel/printk` 表示 `0..4` 都会输出到 console，会包含 `WARNING`，不是通用意义上的“调低”。只有当前值大于 5 时，它才是在降低输出量。

### 4. 持久降低 Console 打印级别

推荐在 `/etc/sysctl.d/` 下新增独立配置，避免直接追加污染 `/etc/sysctl.conf`：

```bash
cat >/etc/sysctl.d/99-kernel-printk.conf <<'EOF'
kernel.printk = 4 4 1 7
EOF
sysctl -p /etc/sysctl.d/99-kernel-printk.conf
```

如果仍然会被严重日志打爆 console，可临时评估：

```bash
sysctl -w kernel.printk="3 4 1 7"
```

### 5. 关闭串口 Console

如果生产环境不依赖串口实时看内核日志，推荐从 cmdline 移除串口 console。

将：

```text
console=ttyAMA0,115200 console=tty0
```

改为：

```text
console=tty0
```

或者根据现场情况保留非串口 console。修改内核启动参数后需要重启。

效果：

- `pl011_console_write()` 不再作为 console 输出路径被调用；
- `printk()` 仍写入 ring buffer；
- `dmesg`、`journalctl -k`、日志 agent 仍可读取内核日志；
- 失去串口实时救援日志，需要依赖 kdump、pstore、BMC 其他能力或远端日志。

### 6. 确认日志是否仍可查看

降低 console loglevel 或关闭串口 console 不等于删除内核日志。确认方式：

```bash
dmesg -T | tail -n 100
journalctl -k -b | tail -n 100
```

查看 journald 是否持久化：

```bash
grep -E '^\s*Storage=' /etc/systemd/journald.conf /etc/systemd/journald.conf.d/*.conf 2>/dev/null
```

崩溃场景建议同时启用或确认：

```bash
kdumpctl status
mount | grep pstore
ls -l /sys/fs/pstore 2>/dev/null
```

### 7. 查找日志风暴源头

实时观察内核日志：

```bash
dmesg -w
```

按关键词统计近期高频日志：

```bash
dmesg -T | sed -E 's/^\[[^]]+\] //' | sort | uniq -c | sort -rn | head -30
```

查看是否存在 RAS、MCE、EDAC、AER、驱动错误风暴：

```bash
dmesg -T | grep -Ei 'ras|mce|edac|aer|error|fail|timeout|reset|stall|lockup' | tail -n 200
```

如果是驱动或模块自身重复打印，长期修复应改为限速接口：

```c
dev_warn_ratelimited(dev, "...\n");
pr_err_ratelimited("...\n");
```

对于高频观测数据，优先使用 tracepoint、debugfs 计数器或 perf/ftrace，而不是在热路径直接 `printk()`。

## 常见问题

### Q1：调低 console loglevel 会不会影响 dmesg？

不会。console loglevel 只控制哪些日志实时输出到 console。`printk()` record 仍会写入 ring buffer，`dmesg` 和 `/dev/kmsg` reader 仍能读取。

边界条件是：如果日志太多导致 ring buffer 覆盖，旧日志仍会丢；如果 panic 后用户态没来得及持久化，journald 也可能缺最后几条。

### Q2：串口打印过的日志还会出现在 journald 吗？

会。串口 console、`dmesg`、journald 都是 printk ring buffer 的消费者。串口打印不会把 record 从 ring buffer 取走。

### Q3：为什么 softlockup 后又出现 hardlockup？

softlockup 或 RCU stall 报告本身会打印大量日志。如果这些日志触发 `console_unlock()`，并且 console 是低速串口，就可能在 IRQ-off 上下文里连续同步输出，最终被 hardlockup watchdog 检测到。

### Q4：这是不是串口硬件坏了？

不一定。大量日志通过 115200 串口同步输出，本身就可能超过 watchdog 阈值。只有在“小量日志也长时间卡在 `pl011_console_putchar()`，TX FIFO 长时间不释放空间”的情况下，才重点怀疑串口控制器、时钟、固件描述、BMC SoL 或硬件链路异常。

### Q5：生产环境是否应该完全禁用串口？

取决于救援要求。常见做法是：生产运行期不把所有日志刷到串口，串口只保留关键级别或救援用途；全量日志走 `/dev/kmsg` 到 journald、rsyslog 或日志 agent，崩溃证据走 kdump/pstore。

---

文档版本：1.0
