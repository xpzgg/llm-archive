# printk：log buffer 与 console 输出

> 依据本地树 `/Volumes/LinuxCode/kernel`（6.6，openEuler）。这个版本没有专门的打印线程，console 输出由调用 `printk()` 的上下文同步完成。mainline 后来改成了线程化输出，文末有简单说明，但没有在本树核实。
> 面向下游的 lockup 排障步骤在 [docs/printk-console-hardlockup-guide.md](../../../docs/printk-console-hardlockup-guide.md)，本文只讲机制。

这篇笔记想回答三个问题：

1. 一条 `printk()` 从调用到出现在串口上，中间经过了什么？
2. 为什么日志打多了会 soft lockup，而且被报 lockup 的往往不是打日志的那段代码？
3. bring-up 时看不到日志，应该怀疑哪一段？

第 1 节是全貌，第 2 节逐个讲机制，第 3 到 5 节用这些机制分析三个场景，第 6 节是 bring-up 现象对照表。

![[printk-console-l1.excalidraw]]

## 1. 全貌

### 要解决的问题

内核要求 `printk()` 在任何上下文里都能调用，包括中断、NMI、持有自旋锁的时候、调度器内部和 panic 过程中。但日志的输出设备很慢：115200 波特率的串口每秒只能发送约 11 KB，一行 80 字节的日志要占用串口约 7 ms。

所以核心问题是：怎样保证记录一条日志既不会失败也不会阻塞，同时又让日志尽快出现在串口上？

### 写入和输出分开

printk 的做法是把一次打印分成两步。

第一步是写入：把日志存进内存里的 ring buffer。这一步不需要锁，任何上下文都能做，耗时在微秒级。

第二步是输出：把 ring buffer 里的记录发送到 console 设备。这一步很慢，而且有些上下文里不能做，所以它可能被推迟，极端情况下甚至一直没有机会执行。

因此 `printk()` 返回只说明日志已经进了 ring buffer，不说明它已经出现在串口上。后面的内容都建立在这一点上。

### 整体结构

```
生产者 ──写入──▶  printk ring buffer  ──读取──▶ 消费者
(内核代码、用户态)   (固定容量，按 seq 编号)      (console、journald、dmesg、pstore …)
```

生产者把日志写进 ring buffer，消费者从 ring buffer 里读。第 2 节按下面的顺序讲六个机制：

| 小节 | 机制 | 回答的问题 |
|---|---|---|
| 2.1 | 生产者与日志级别 | 日志从哪里来，带什么属性 |
| 2.2 | ring buffer | 日志存在哪里，什么情况下会丢 |
| 2.3 | 消费者 | 谁在读日志，各自怎么读 |
| 2.4 | console 输出与 console 锁 | 由谁把日志发到串口，一次发多少 |
| 2.5 | 上下文限制 | 什么情况下只能写入、不能立即输出 |
| 2.6 | boot console 与正式 console | 启动早期靠什么输出，什么时候切换 |

## 2. 机制

### 2.1 生产者与日志级别

所有生产者最终都调用同一个写入入口，把记录放进同一个 ring buffer。

| 生产者 | 说明 | 是否写入 |
|---|---|---|
| 普通内核日志：`pr_err/pr_info`、`dev_err/dev_info`、`netdev_*` 等 | 带级别的 `printk` 封装 | 总是写入 |
| 不带级别的 `printk("...")` | 使用默认消息级别（本树配置为 4） | 总是写入 |
| 调试日志：`pr_debug`、`dev_dbg` | 默认被编译掉，或者由 dynamic debug 在运行时逐条开关 | 默认不产生 |
| 限速日志和一次性日志：`*_ratelimited`、`*_once` | 超出限额的调用在调用点就被丢弃 | 只写入一部分；被限速时会补一行 `callbacks suppressed` |
| 异常报告：`WARN`、oops、panic、soft/hard lockup、RCU stall、OOM | 一次输出几十到几百行（寄存器、调用栈、模块列表） | 总是写入，是日志积压的主要来源 |
| 用户态写 `/dev/kmsg` | systemd 和启动脚本用它把自己的消息加进内核日志，默认按进程限速 | 写入，并标记为来自用户态 |

有两类输出经常和内核日志混在一起，但它们不经过 printk。一类是固件和 bootloader 的串口输出，它们直接写同一个 UART，所以在 `dmesg` 里看不到。另一类是 `trace_printk` 和 tracepoint，它们写的是 ftrace 自己的 buffer，不会出现在 console 上。

每条记录带有一个 0 到 7 的级别，数字越小越严重：0 EMERG、1 ALERT、2 CRIT、3 ERR、4 WARNING、5 NOTICE、6 INFO、7 DEBUG。

级别不影响写入，任何级别的日志都会进 ring buffer。级别只在 console 输出时起作用：内核把记录的级别和 console loglevel 比较，级别数值小于 console loglevel 的记录才会发送到 console（见 2.4）。本树默认的 console loglevel 是 7，也就是 0 到 6 级都输出；cmdline 里带 `quiet` 时是 4。

### 2.2 ring buffer

ring buffer 的容量是固定的，默认大小由编译配置决定（本树是 1 MB），启动时可以用 `log_buf_len=` 加大。

每条记录有一个递增的序号 `seq`，另外还带着级别、时间戳、来源 CPU 等信息。所有读者都用 `seq` 来定位记录。

buffer 写满之后，新记录会覆盖最旧的记录，因此写入不需要等待，也不会失败。写入过程不使用锁，所以在 NMI 里也可以写。

日志丢失只有一种情况：某个读者还没读到的记录被新记录覆盖了。这个读者下次读取时会发现自己要读的 `seq` 已经不存在，于是从当前最旧的有效记录继续读，同时能知道中间丢了多少条。

### 2.3 消费者

每个消费者各自记录自己读到了第几条。读取不会删除记录，消费者之间互不影响。

| 消费者 | 读取方式 | 使用者 | 特点 |
|---|---|---|---|
| console：串口、显示终端 `tty0`、netconsole 等 | 内核主动发送 | 实时查看、带外救援 | 每个 console 有自己的 `seq`；只有它受 console loglevel 过滤；只有它会拖慢 `printk()` 的调用者 |
| `/dev/kmsg` | 用户态进程主动读取 | journald、新版 `dmesg`、日志采集 agent | 每次 `open` 得到独立的读取位置；要读的记录被覆盖时返回 `EPIPE` |
| syslog 系统调用、`/proc/kmsg` | 用户态进程主动读取 | 老的 klogd、rsyslog 的内核日志输入 | `/proc/kmsg` 全系统共用一个读取位置，所以只能有一个读者 |
| kmsg_dump | panic、oops、重启时由内核回调 | pstore 的后端（ramoops、EFI 变量、ACPI ERST）、mtdoops | 在出事的时刻把 buffer 尾部的内容保存到复位后不会丢失的介质上 |
| vmcore、内存镜像 | 离线解析 | kdump 之后的 `crash`、`vmcore-dmesg`，以及 JTAG | 不经过内核接口，直接从内存里解析 ring buffer |

这些消费者里，只有 console 需要内核自己花 CPU 时间把记录发送出去，其余的都是使用者在需要时自己来读，不占用 `printk()` 调用者的时间。2.4 到 2.6 讲的都是 console。

用户态的持久化路径是 journald 读 `/dev/kmsg` 再写到磁盘上，用 `journalctl -k` 查看。这条路径要求用户态已经启动、存储可用，所以启动早期的死机和 panic 的最后几行日志它是记录不到的，这两种情况只能依靠 console、kmsg_dump 和内存镜像。

### 2.4 console 输出与 console 锁

这一节是整个 printk 里最重要的部分，soft lockup 的问题就出在这里。

#### console 是什么

console 是驱动向 printk 注册的输出对象，它最主要的内容是一个 `write()` 回调，作用是把一段文本发送到设备。串口 console 的 `write()` 采用逐字符轮询的方式：等 UART 的发送 FIFO 有空位，写入一个字符，然后继续等。它不依赖中断，也不会睡眠，所以在任何上下文里都能调用，但速度完全取决于波特率。

系统里可以同时注册多个 console，每个 console 用自己的 `seq` 记录输出进度。

#### 由谁调用 write()

6.6 里没有专门负责打印的线程，console 的 `write()` 是由调用 `printk()` 的那个上下文自己调用的。也就是说，`printk()` 写完 ring buffer 之后，会在返回之前尝试把日志发送到 console。

#### console 锁

多个 CPU 可能同时调用 `printk()`，而向 console 设备写数据必须串行进行。console 锁用来保证同一时刻只有一个上下文在调用 console 驱动。下文把持有 console 锁、正在输出日志的上下文称为持锁者。

`printk()` 获取 console 锁用的是 trylock，不会等待。如果拿到了锁，当前上下文就成为持锁者，负责输出。如果没拿到，说明已经有别的持锁者正在输出，当前上下文直接返回，它刚写入的那条日志留在 ring buffer 里。

这样做的好处是 `printk()` 不会因为等锁而阻塞。但没拿到锁的 CPU 留下的日志需要有人输出，这个责任落在了持锁者身上。

#### console_unlock() 批量输出

持锁者通过 `console_unlock()` 释放 console 锁。这个函数在真正释放锁之前，会把所有 console 还没有输出的记录全部输出，过程如下：

```
循环，直到所有 console 都输出到了 ring buffer 的最新位置：
    对每个 console：
        取出它的 seq 指向的下一条记录
        如果记录的级别小于 console loglevel：关闭本地中断，调用 write()
        否则跳过这条记录
        seq 加一
释放 console 锁
如果在这期间又有新记录写入：重新 trylock，成功则再循环一遍
```

所以 `console_unlock()` 输出的不只是调用者自己的那条日志，而是当前所有 console 的全部积压（backlog），其中包括其他 CPU 没拿到锁时留下的日志。

由此得到一个重要的结论：持锁者要输出多少日志，取决于它持锁期间整个系统产生了多少日志，和它自己打了几条没有关系，而且这个量没有上限。

#### 锁的移交

为了避免同一个 CPU 一直输出下去，内核做了一个优化。如果某个 CPU 调用 `printk()` 时发现 console 锁被占用，并且此时还没有别的 CPU 在等，它可以自旋等待一小段时间。持锁者每输出完一条记录都会检查有没有 CPU 在等，如果有，就把锁直接移交给它，自己返回。接到锁的 CPU 成为新的持锁者，继续输出剩下的积压。

这个优化只是让输出的负担在不同 CPU 之间转移，需要输出的总量没有变。

#### 输出过程中能否调度

进入 `console_unlock()` 有两条路径，区别在于输出过程中能不能让出 CPU。

| 路径 | 能否调度 | 典型场景 |
|---|---|---|
| `printk()` 内部，trylock 成功后立即 `console_unlock()` | 不能，整个过程关闭抢占 | 运行期间的所有日志 |
| 显式调用 `console_lock()`，之后 `console_unlock()` | 可以，每输出一条记录后可以让出 CPU | console 注册时补打历史记录，tty 和显示相关的代码 |

`printk()` 这条路径关闭抢占是有原因的：调用者可能正持有别的锁，或者处在对时间敏感的位置，内核希望它尽快输出完然后返回。如果它在输出中途被调度出去，console 锁会被一直占着，其他 CPU 的日志也都输出不了。

### 2.5 上下文限制

console 驱动内部可能需要获取自己的锁。如果 `printk()` 发生在可能已经持有这些锁的位置，或者发生在不能安全获取锁的位置，立即调用 console 驱动就可能死锁。因此不同的上下文有不同的处理方式，三种情况下日志都会正常写入 ring buffer，区别只在输出。

| 调用上下文 | 输出方式 |
|---|---|
| 普通上下文（进程上下文、中断处理） | 立即尝试输出，过程见 2.4 |
| NMI、调度器内部（使用 `printk_deferred`）、printk 自身的临界区 | 推迟输出：登记一个 irq_work，等这个 CPU 之后能响应中断时，在中断上下文里按 2.4 的过程输出 |
| 发生 panic 的 CPU | 先停掉其他 CPU，然后不获取 console 锁，直接输出全部积压。原来的持锁者可能已经被停住，等它释放锁是等不到的 |

推迟输出有一个前提：这个 CPU 之后还能响应中断。如果它接下来在关中断的状态下死掉，这些日志就不会出现在串口上，只留在 ring buffer 里。

### 2.6 boot console 与正式 console

正式的串口 console 由完整的串口驱动注册，需要驱动模型、时钟、中断都初始化完成，这个时间点在启动过程中比较晚。在此之前的日志由 boot console 输出。

boot console 也就是 earlycon，是一个不依赖驱动模型的简化 console。它直接映射 UART 寄存器，用轮询方式写，波特率沿用固件配置好的值。在 cmdline 里加 `earlycon` 可以启用它，内核在解析早期参数时就会注册。

正式 console 是完整驱动注册的 console。它会根据固件描述的时钟频率重新配置波特率。

一个 console 注册时，可以从 ring buffer 里最旧的记录开始输出，这样注册之前已经写入的日志会被补打出来。boot console 注册时就是用这种方式把更早的日志输出到串口的。

正式 console 注册并被内核选中之后，所有 boot console 都会被注销，正式 console 从 boot console 停止的位置继续输出，不会重复。cmdline 里加 `keep_bootcon` 可以保留 boot console。

内核按下面的顺序决定哪个设备作为正式 console。首先看 cmdline 的 `console=`，可以写多个，每个都会输出，其中最后一个同时作为 `/dev/console`。如果没有写 `console=`，就使用固件的描述，即 DT 的 `stdout-path` 或 ACPI 的 SPCR 表。如果这些都没有，第一个注册的 console 会被选中，通常是 `tty0`。

需要特别注意的是：只要有任意一个正式 console 被选中并注册，boot console 就会被注销，即使这个正式 console 并不是串口。

## 3. 场景一：一条日志的正常路径

假设 CPU0 上某个驱动在进程上下文里调用 `dev_err()`，此时系统空闲，没有其他日志。

1. 写入（2.2）。内核格式化文本，在 ring buffer 里写入一条记录，级别为 3，并分配 `seq`。从这一刻起，`dmesg`、journald、pstore 都能读到这条日志。
2. 判断上下文（2.5）。当前是普通上下文，可以立即输出。
3. 获取 console 锁（2.4）。锁没有被占用，trylock 成功，CPU0 成为持锁者。
4. 调用 `console_unlock()`（2.4）。积压只有刚写入的这一条。它的级别 3 小于 console loglevel 7，所以需要输出。内核关闭本地中断，串口驱动逐字符写 UART，耗时约 7 ms，然后串口 console 的 `seq` 加一。所有 console 都已输出到最新位置，释放锁。
5. 唤醒用户态读者（2.3）。阻塞在 `/dev/kmsg` 上的 journald 被唤醒，之后它自己读取这条记录。

`dev_err()` 返回。这次调用耗时约 7 ms，几乎都花在等待串口上。

## 4. 场景二：日志风暴与 soft lockup

### 过程

假设一台 64 核的机器上某个网卡驱动出错，CPU1 到 CPU63 都在反复调用 `dev_err()`。这时 CPU0 上一个不相关的进程打了一条 `pr_info()`。

1. CPU0 写入自己的记录，trylock 成功，成为持锁者。
2. CPU1 到 CPU63 各自写入记录，trylock 失败后立即返回，继续执行，也继续产生新的日志。
3. CPU0 在 `console_unlock()` 里开始输出积压。串口每秒最多输出约 140 行。
4. 只要其他 CPU 每秒产生的日志合计超过 140 行，积压就会越来越多。CPU0 始终输出不完，也就一直无法从 `printk()` 返回。

CPU0 自己只打了一条日志，却要输出整个系统的日志。锁的移交可以把这项工作转给别的 CPU，但只要日志产生的速度超过串口输出的速度，总会有一个 CPU 停在 `console_unlock()` 里出不来。

### 为什么会报 soft lockup

先说明 soft lockup 检测的是什么。每个 CPU 上有一个高优先级的 watchdog 任务，它定期运行并更新一个时间戳。如果某个 CPU 的时间戳超过阈值（默认 20 秒）没有更新，说明这个 CPU 一直在执行某段内核代码而没有发生调度，内核就会报 `BUG: soft lockup`。

2.4 里提到，`printk()` 路径上的 `console_unlock()` 是在关闭抢占的状态下执行的。持锁者连续输出日志期间，watchdog 任务得不到运行。按默认阈值计算，连续输出 20 秒（大约 2900 行、230 KB）就会触发 soft lockup。

同样的原因还会引起另外几种现象：

- RCU stall。这个 CPU 长时间没有经过 RCU 的静止状态。
- hard lockup。如果持锁者本来就在中断处理程序里，或者处在关中断的区域里，那么连时钟中断也无法响应。
- 日志越来越多。soft lockup 和 RCU stall 的报告本身有几百行，写入 ring buffer 后又成为新的积压，使情况进一步恶化。

### 如何识别

```
watchdog: BUG: soft lockup - CPU#0 stuck for 22s!
...
 pl011_console_write        串口驱动
 console_emit_next_record   输出一条记录
 console_flush_all          遍历所有 console
 console_unlock             批量输出积压
 vprintk_emit
 _printk
 some_function              调用 printk 的函数
```

如果调用栈的顶部是 console 驱动和 `console_unlock`，说明这个 CPU 没有死锁，它是在输出日志。

这种情况下，被报告 lockup 的函数通常不是问题的原因，它只是恰好拿到了 console 锁。真正要找的是大量产生日志的代码，可以看串口上输出的内容里哪一类日志最多。

### 处理方法

| 方法 | 原理 |
|---|---|
| 调低 console loglevel，例如 `loglevel=4` 或 `dmesg -n 4` | 被过滤掉的记录在 `console_unlock()` 里直接跳过，不占用串口时间。这些记录仍然在 ring buffer 里，用 `dmesg` 可以看到 |
| 对产生日志的代码限速（`*_ratelimited`），或者解决报错的根本原因 | 减少日志的产生量 |
| 提高波特率 | 加快输出速度 |
| 去掉串口 console，只保留 netconsole 或 pstore | 去掉了最慢的输出设备，但也失去了带外的实时日志，bring-up 阶段一般不这样做 |

## 5. 场景三：系统启动

下表把 2.6 的内容按启动的时间顺序列出来。

| 阶段 | 内核里发生的事 | 串口上的表现 |
|---|---|---|
| 内核入口到 earlycon 注册之前 | 还没有任何 console，日志只写入编译时分配的静态 buffer | 没有任何输出 |
| 解析早期参数（在 `setup_arch` 里） | boot console 注册，并补打 buffer 里已有的记录 | 之前的日志一次性输出，之后的日志实时输出 |
| `setup_log_buf` | 如果指定了 `log_buf_len=`，或者 CPU 数量很多，内核会分配一个更大的 buffer，并把旧记录拷贝过去 | 没有变化 |
| 正式串口驱动 probe | 正式 console 注册并被选中，boot console 被注销，正式 console 继续输出 | 出现 `console [ttyAMA0] enabled` 和 `bootconsole [pl011] disabled` |
| `late_initcall` | 如果某个 boot console 的代码位于 init 段，它会被强制注销，因为 init 段的内存即将释放 | 如果这时正式 console 还没有注册，输出会中断 |
| 进入用户态 | 启动脚本或 sysctl 可能修改 console loglevel；journald 开始读 `/dev/kmsg` | 串口上的内核日志可能减少或消失 |

不同情况下可以使用的日志获取途径如下。

| 情况 | 可用的途径 |
|---|---|
| 内核刚启动，驱动还没有初始化 | boot console；用 JTAG 读内存 |
| 正常运行 | console、`dmesg`、journald 或 rsyslog |
| 死机，但还能触发 panic | console；kmsg_dump 保存到 pstore；kdump 生成 vmcore |
| 完全挂死，没有 panic | ramoops 的 console 记录功能（它会额外注册一个 console，持续把日志写入保留内存，热复位后可以读出）；用 JTAG 读内存 |

## 6. Bring-up 现象与可能原因

排查时可以先确认一件事：`dmesg` 里（或者内存里的 log buffer 里）有没有这条日志。如果有，问题出在输出环节，对应 2.4 到 2.6。如果没有，问题出在写入环节：可能是日志根本没有产生（2.1），可能是已经被覆盖（2.2），也可能是代码确实没有执行到。

| 现象 | 可能原因 | 相关机制 | 确认和处理方法 |
|---|---|---|---|
| 从内核入口开始完全没有输出 | 没有启用 `earlycon`；earlycon 的 UART 基址或类型不正确；在 earlycon 注册之前就死机了 | 2.6 | 在 cmdline 里明确写出 `earlycon=<类型>,<地址>`。如果仍然没有输出，用 JTAG 读内存里的 log buffer（符号是 `__log_buf` 和 `log_buf`） |
| 输出到 `bootconsole disabled` 附近就停止了 | 被选中的正式 console 不是这个串口，例如 `console=` 没有写或者写错了，结果 `tty0` 被选中，boot console 随之被注销；也可能是串口驱动 probe 失败 | 2.6 | 加上 `keep_bootcon`，在后续日志里查找串口驱动的报错；核对 `console=ttyAMA0` 或 `ttyS0` 的设备名 |
| 切换到正式 console 之后输出乱码 | 正式驱动按固件描述的时钟频率重新配置了波特率，而描述的频率与实际不符 | 2.6 | 核对 UART 时钟的描述和 `console=...,115200` 里的波特率 |
| 启动早期的日志输出了两遍 | 使用了 `keep_bootcon`；或者内核没有识别出 boot console 和正式 console 是同一个物理设备 | 2.6 | 去掉 `keep_bootcon` |
| 启动非常慢，或者出现 soft lockup、RCU stall，调用栈里有 `console_unlock` | 持锁者正在批量输出积压。FPGA 和仿真平台的 UART 很慢，少量日志就可能触发。正式 console 注册时补打整个 buffer 也会造成长时间停顿 | 2.4，场景二 | 使用 `loglevel=4` 或 `quiet`，之后用 `dmesg` 查看完整日志；找出大量产生日志的代码；提高波特率 |
| 死机了，但最后几条日志没有输出 | 最后的日志是在只能推迟输出的上下文里产生的，还没来得及输出系统就死了；或者持锁者是另一个 CPU，它的输出进度还落后很多；或者 CPU 在关中断的状态下死掉了 | 2.5，2.4 | 日志仍然在 ring buffer 里，可以用 JTAG 或 crash 读取，也可以配置 ramoops。调低 loglevel 可以减少串口的积压 |
| 日志中间缺少一段，并且有 `** N printk messages dropped **` | 串口输出跟不上，还没来得及输出的记录已经被新记录覆盖 | 2.2 | 加大 `log_buf_len=`；调低 console loglevel；对产生日志的代码限速 |
| `dmesg` 的开头部分缺失 | buffer 太小，早期的记录已经被覆盖 | 2.2 | 加大 buffer，例如 `log_buf_len=16M` |
| panic 信息不完整 | 其他 CPU 被停掉时正输出到一半；panic 期间如果丢失的记录太多，非 panic CPU 的日志会被抑制；串口太慢，看门狗先复位了系统 | 2.5 | 配置 ramoops/pstore 或 kdump |
| 进入用户态之后，内核日志不再出现在串口上 | console loglevel 被 `quiet`、sysctl `kernel.printk` 或启动脚本调低了 | 2.1 | 查看 `/proc/sys/kernel/printk` 的第一个数。日志在 `dmesg` 里仍然能看到 |
| 代码确实执行到了，但串口和 `dmesg` 里都没有对应的日志 | 用的是 `pr_debug` 或 `dev_dbg`，默认不产生日志；或者用的是 `*_once`、`*_ratelimited`，已经打过一次或者被限速了 | 2.1 | 在 cmdline 里加 `dyndbg="file xxx.c +p"`，或者临时改成 `pr_info` |
| 串口上有某段输出，`dmesg` 里没有 | 这段输出来自固件或 bootloader，它们直接写 UART，不属于内核日志 | 2.1 | 看这段输出有没有内核的时间戳前缀 |
| 加了 `printk` 之后 bug 不再出现，或者时序发生了变化 | 每条日志同步写串口需要几毫秒，期间还关闭了中断，这足以改变竞争条件的时间窗口 | 2.4 | 改用 `trace_printk` 或 ftrace；或者调低 console loglevel，只通过 `dmesg` 查看 |

## 7. 设计取舍

| 设计选择 | 好处 | 代价 |
|---|---|---|
| 写入和输出分开 | 写入不会阻塞，任何上下文都可以调用 | 写入成功不代表已经输出。死机时最后几条日志可能只存在于内存里 |
| 由调用者同步输出，不使用专门的线程 | 崩溃发生之前，日志已经尽可能输出到了串口，不依赖调度器还能正常工作 | 持锁者要输出的日志量没有上限，会导致启动变慢、soft lockup、RCU stall 和 hard lockup |
| 固定容量，覆盖最旧的记录 | 内存占用有上限，写入不需要等待 | 读取速度慢的消费者会丢失记录 |
| console loglevel 只过滤输出，不影响写入 | 可以减少串口输出，同时不丢失信息 | 串口上没有某条日志，不代表它没有被打印，容易误以为代码没有执行到 |

第二行是可靠性和性能之间的取舍，传统的 printk 选择了可靠性。

mainline 在 6.6 之后逐步合入了新的 console 模型（nbcon），要解决的正是第二行的代价。在新模型里，每个 console 有自己的打印线程，平时由线程异步输出，`printk()` 的调用者不再需要等待串口。发生 panic 或 oops 时，内核通过一条不需要 console 锁的原子输出路径直接写设备，以保证可靠性。新模型要求串口驱动逐个适配，没有适配的驱动仍然按本文描述的方式工作。这部分内容没有在本树核实，具体的合入版本和各驱动的适配情况需要到对应版本的代码里确认。

## 附：常用参数

| 参数 | 作用 |
|---|---|
| `earlycon` 或 `earlycon=<类型>,<地址>` | 启用 boot console。不带值时从 `stdout-path` 或 SPCR 获取设备信息 |
| `keep_bootcon` | 正式 console 注册之后不注销 boot console |
| `console=<设备>,<波特率>` | 指定正式 console，可以写多个 |
| `loglevel=N`、`quiet`、`ignore_loglevel` | 设置 console loglevel。只影响 console 输出，不影响 ring buffer |
| `log_buf_len=<大小>` | 设置 ring buffer 的容量 |
| `/proc/sys/kernel/printk` | 运行时查看和修改 console loglevel（第一个数） |
| `dyndbg=` | 打开指定的 `pr_debug` 和 `dev_dbg` |
| ramoops、pstore | 把日志保存到复位后不会丢失的内存区域 |
