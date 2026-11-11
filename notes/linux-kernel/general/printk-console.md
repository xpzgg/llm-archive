# printk：log buffer 与 console 输出

> 依据本地树 `/Volumes/LinuxCode/kernel`（6.6，openEuler）。这个版本没有专门的打印线程，console 输出由调用 `printk()` 的上下文同步完成。mainline 后来改成了线程化输出，文末有简单说明，但没有在本树核实。
> 面向下游的 lockup 排障步骤在 [docs/printk-console-hardlockup-guide.md](../../../docs/printk-console-hardlockup-guide.md)，本文只讲机制。

这篇笔记想回答三个问题：

1. 一条 `printk()` 从调用到出现在串口上，中间经过了什么？
2. 为什么日志打多了会 soft lockup，而且被报 lockup 的往往不是打日志的那段代码？
3. bring-up 时看不到日志，应该怀疑哪一段？

主线是第 2 到 4 节：ring buffer、console 输出、不同上下文的打印。第 5、6 节用这些机制分析日志风暴和系统启动。第 7 节补充日志级别、生产者、其他消费者这些次要内容。第 8 节是 bring-up 常见现象的速查表。

![[printk-console-l1.excalidraw]]

## 1. Overview

### 要解决的问题

内核要求 `printk()` 在任何上下文里都能调用，包括中断、NMI、持有自旋锁的时候、调度器内部和 panic 过程中。但日志的输出设备很慢：115200 波特率的串口每秒只能发送约 11 KB，一行 80 字节的日志要占用串口约 7 ms。

所以核心问题是：怎样保证记录一条日志不会阻塞调用者，同时又让日志尽快出现在串口上？

### 写入和输出分开

printk 的做法是把一次打印分成两步。

第一步是写入：把日志存进内存里的 ring buffer。这一步在任何上下文里都能做，耗时在微秒级。

第二步是输出：把 ring buffer 里的记录发送到 console 设备。这一步很慢，而且有些上下文里不能做，所以它可能被推迟，极端情况下甚至一直没有机会执行。

因此 `printk()` 返回只说明日志已经进了 ring buffer，不说明它已经出现在串口上。后面的内容都建立在这一点上。

### 整体结构

```
调用 printk() 的代码 ──写入──▶ printk ring buffer ──读取──▶ console（串口等）
                                                  └─────▶ 其他读者（dmesg、journald、pstore …）
```

ring buffer 位于中间。写入的一侧是内核里所有调用 `printk()` 的代码。读取的一侧最重要的是 console，它负责把日志发送到串口；另外还有 `dmesg`、journald、pstore 等读者，它们放在 7.3 节介绍。

## 2. ring buffer

### 存的是什么

ring buffer 里存的是一条一条的记录，每次 `printk()` 调用产生一条。一条记录由两部分组成：

- 元信息：序号 `seq`、时间戳、日志级别、来源 CPU 或任务，以及这条记录当前的状态。
- 文本：格式化之后的日志内容。

ring buffer 内部相应地有两个环形数组。一个是描述符环，每个描述符对应一条记录，存放元信息和文本的位置。另一个是文本环，是一个字节数组，各条记录的文本依次存放在里面。

容量是固定的，默认大小由编译配置决定（本树是 1 MB），启动时可以用 `log_buf_len=` 加大。

### 写入一条记录的三个步骤

写入不使用自旋锁。原因是 NMI 可以打断任何代码，包括正在写入日志的代码；如果写入需要持锁，NMI 里再调用 `printk()` 就会在同一个 CPU 上等待自己已经持有的锁，造成死锁。

写入改用原子操作来保证多个 CPU 之间互不干扰，分三步进行。

**第一步，预留。** 写入者先在描述符环上取得下一个描述符，再在文本环上取得一段足够长的空间。这两个动作都是通过"比较并交换"完成的：读出当前的分配位置，算出新的位置，然后要求 CPU 原子地执行"如果分配位置还是我刚才读到的值，就把它改成新值"。如果在这期间别的 CPU 已经改动了分配位置，这次操作失败，写入者重新读取、重新尝试。

这样即使多个 CPU 同时预留，它们也一定是一个接一个地成功，每个写入者得到一个不同的描述符和一段与别人不重叠的文本空间。记录的 `seq` 也在这一步确定，它等于上一条记录的 `seq` 加一。

预留成功后，描述符的状态是"已预留"，表示有写入者正在填写这条记录。

**第二步，填写。** 写入者把元信息填入自己的描述符，把文本写入自己预留的那段空间。它只访问自己预留到的位置，所以这一步不需要任何同步。

**第三步，提交。** 写入者把描述符的状态原子地改为"已完成"。从这一刻起，这条记录才对读者可见。

从预留到提交的整个过程中，本地中断是关闭的，所以这三步在一个 CPU 上执行得很快，不会被普通中断打断。NMI 仍然可以打断它；NMI 里的 `printk()` 会独立地完成自己的三步，得到一个更靠后的 `seq`。

### 记录的顺序

`seq` 是按预留成功的先后分配的，全局唯一并且连续递增。日志的顺序就是 `seq` 的顺序。

提交的先后不一定和 `seq` 的先后相同。例如 CPU A 先预留到 `seq` 100，CPU B 随后预留到 `seq` 101；如果 B 的文本较短，或者 A 在填写时被 NMI 打断，B 可能比 A 先提交。

读者是按 `seq` 逐条读取的，并且只读状态为"已完成"的记录。读者读到一条尚未完成的记录时会停下来，不会越过它去读后面的记录。在上面的例子里，即使 101 已经提交，读者也要等 100 提交之后才能依次读到 100 和 101。所以读者看到的顺序始终是 `seq` 的顺序，也不会读到只写了一半的记录。

时间戳是另一回事。它是在 `printk()` 入口处读取的本 CPU 时钟，这个时间点在预留之前。两个 CPU 几乎同时调用 `printk()` 时，先读到时间戳的 CPU 不一定先预留成功，因此相邻两条记录的时间戳偶尔会出现几微秒的倒退。这是正常现象。

总结一下顺序方面的保证：

- 同一个 CPU 上先后打印的日志，`seq` 的顺序和调用顺序一致。
- 不同 CPU 上几乎同时打印的日志，`seq` 的顺序反映的是谁先预留成功，和调用 `printk()` 的先后可能有微小的出入。
- 所有读者看到的顺序都相同，都是 `seq` 的顺序。

### 写满之后

描述符环或文本环用完之后，预留新记录时需要先回收最旧的记录：把最旧的描述符标记为可回收，再把环的起始位置向前移动。因此新记录会覆盖最旧的记录，写入者不需要等待读者。

只有已完成的记录才能被回收。如果最旧的那条记录恰好还处于"已预留"状态，新的预留就会失败，这条新日志会被丢弃。这种情况很少发生，从预留到提交期间关闭中断，就是为了让记录停留在"已预留"状态的时间尽量短。

### 读取

每个读者自己保存一个 `seq`，表示下一条要读的记录。读取不会删除记录，读者之间互不影响。

如果读者要读的记录已经被覆盖，它会发现这个 `seq` 小于当前最旧的记录，于是从最旧的记录继续读，并且根据两个 `seq` 的差值知道自己丢失了多少条。这是日志丢失的主要方式：某个读者还没来得及读的记录被新记录覆盖了。

## 3. console 输出

这一节是整个 printk 里最重要的部分，soft lockup 的问题就出在这里。

### console 是什么

console 是驱动向 printk 注册的输出对象，它最主要的内容是一个 `write()` 回调，作用是把一段文本发送到设备。串口 console 的 `write()` 采用逐字符轮询的方式：等 UART 的发送 FIFO 有空位，写入一个字符，然后继续等。它不依赖中断，也不会睡眠，所以在任何上下文里都能调用，但速度完全取决于波特率。

系统里可以同时注册多个 console，例如串口、显示终端 `tty0`、netconsole。每个 console 是 ring buffer 的一个读者，用自己的 `seq` 记录输出进度。

### 由谁调用 write()

6.6 里没有专门负责打印的线程，console 的 `write()` 是由调用 `printk()` 的那个上下文自己调用的。也就是说，`printk()` 写完 ring buffer 之后，会在返回之前尝试把日志发送到 console。

这样设计是为了可靠：日志在 `printk()` 返回之前就已经尽量发送到了串口，即使系统随后崩溃、调度器不再工作，之前的日志也已经输出。代价是调用者要等待串口，这一节后面的内容和第 5 节讲的都是这个代价。

### console 锁

多个 CPU 可能同时调用 `printk()`，而向 console 设备写数据必须串行进行。console 锁用来保证同一时刻只有一个上下文在调用 console 驱动。下文把持有 console 锁、正在输出日志的上下文称为持锁者。

`printk()` 获取 console 锁用的是 trylock，不会等待。如果拿到了锁，当前上下文就成为持锁者，负责输出。如果没拿到，说明已经有别的持锁者正在输出，当前上下文直接返回，它刚写入的那条日志留在 ring buffer 里。

这样做的好处是 `printk()` 不会因为等锁而阻塞。但没拿到锁的 CPU 留下的日志需要有人输出，这个责任落在了持锁者身上。

### console_unlock() 批量输出

持锁者通过 `console_unlock()` 释放 console 锁。这个函数在真正释放锁之前，会把所有 console 还没有输出的记录全部输出，过程如下：

```
console_unlock():                      // 进入时已经持有 console 锁

    // 第一部分：持锁输出，直到没有任何积压
    重复：
        本轮有输出 = 否
        对每个 console：
            如果它的 seq 已经是 ring buffer 的最新位置：
                这个 console 没有积压，看下一个 console
            取出它的 seq 指向的那一条记录
            如果这条记录的级别达到了输出门槛：
                关闭本地中断，调用 write() 把这一条发送到设备，恢复中断
            否则：
                不发送
            它的 seq 加一                // 这个 console 前进一条
            本轮有输出 = 是
    直到 本轮有输出 == 否               // 所有 console 都没有积压了

    // 第二部分：到这里才释放锁
    释放 console 锁

    // 第三部分：检查释放锁的前后有没有新记录写入
    如果有新记录，并且 trylock 成功：
        回到第一部分
```

这段逻辑有三个要点。

第一，内层每次只让一个 console 前进一条记录，但外层的"重复"会一轮接一轮地执行，只要还有任何一个 console 有积压就不会停。假设串口 console 落后 3000 条，外层就要重复 3000 轮，全部输出完才结束。

第二，console 锁在整个第一部分期间一直被持有，不会在两条记录之间释放。持锁者在输出过程中写入 ring buffer 的新记录（包括其他 CPU 写入的），也会在后面的轮次里被输出，因为判断积压时比较的是 ring buffer 当时的最新位置。

第三，释放锁之后还要再检查一次。这是为了处理这样一种情况：第一部分刚结束、锁还没释放时，另一个 CPU 写入了一条记录并且 trylock 失败返回了。如果不再检查，这条记录就没有人输出。

其中的输出门槛是 console loglevel，7.1 节有说明。

所以 `console_unlock()` 输出的不只是调用者自己的那条日志，而是当前所有 console 的全部积压（backlog），其中包括其他 CPU 没拿到锁时留下的日志。

由此得到一个重要的结论：持锁者要输出多少日志，取决于它持锁期间整个系统产生了多少日志，和它自己打了几条没有关系，而且这个量没有上限。

### 锁的移交

为了避免同一个 CPU 一直输出下去，内核做了一个优化。如果某个 CPU 调用 `printk()` 时发现 console 锁被占用，并且此时还没有别的 CPU 在等，它可以自旋等待一小段时间。持锁者每输出完一条记录都会检查有没有 CPU 在等，如果有，就把锁直接移交给它，自己返回。接到锁的 CPU 成为新的持锁者，继续输出剩下的积压。

这个优化只是让输出的负担在不同 CPU 之间转移，需要输出的总量没有变。

### 输出过程中能否调度

进入 `console_unlock()` 有两条路径，区别在于输出过程中能不能让出 CPU。

| 路径 | 能否调度 | 典型场景 |
|---|---|---|
| `printk()` 内部，trylock 成功后立即 `console_unlock()` | 不能，整个过程关闭抢占 | 运行期间的所有日志 |
| 显式调用 `console_lock()`，之后 `console_unlock()` | 可以，每输出一条记录后可以让出 CPU | console 注册时补打历史记录，tty 和显示相关的代码 |

`printk()` 这条路径关闭抢占是有原因的：调用者可能正持有别的锁，或者处在对时间敏感的位置，内核希望它尽快输出完然后返回。如果它在输出中途被调度出去，console 锁会被一直占着，其他 CPU 的日志也都输出不了。

### 例子：一条日志的完整路径

假设 CPU0 上某个驱动在进程上下文里调用 `dev_err()`，此时系统空闲，没有其他日志。

1. 写入。内核按第 2 节的三个步骤在 ring buffer 里写入一条记录。从提交的那一刻起，`dmesg`、journald 等读者都能读到这条日志。
2. 获取 console 锁。锁没有被占用，trylock 成功，CPU0 成为持锁者。
3. 调用 `console_unlock()`。积压只有刚写入的这一条。内核关闭本地中断，串口驱动逐字符写 UART，耗时约 7 ms，然后串口 console 的 `seq` 加一。所有 console 都已输出到最新位置，释放锁。
4. 唤醒正在等待新日志的用户态进程，例如 journald。

`dev_err()` 返回。这次调用耗时约 7 ms，几乎都花在等待串口上。

## 4. 不同上下文的打印

console 驱动内部可能需要获取自己的锁。如果 `printk()` 发生在可能已经持有这些锁的位置，或者发生在不能安全获取锁的位置，立即调用 console 驱动就可能死锁。因此不同的上下文有不同的处理方式。三种情况下日志都会正常写入 ring buffer，区别只在输出。

| 调用上下文 | 输出方式 |
|---|---|
| 普通上下文（进程上下文、中断处理） | 立即尝试输出，过程见第 3 节 |
| NMI、调度器内部（使用 `printk_deferred`）、printk 自身的临界区 | 推迟输出：登记一个 irq_work，等这个 CPU 之后能响应中断时，在中断上下文里按第 3 节的过程输出 |
| 发生 panic 的 CPU | 先停掉其他 CPU，然后不获取 console 锁，直接输出全部积压。原来的持锁者可能已经被停住，等它释放锁是等不到的 |

推迟输出有一个前提：这个 CPU 之后还能响应中断。如果它接下来在关中断的状态下死掉，这些日志就不会出现在串口上，只留在 ring buffer 里。

## 5. 日志风暴与 soft lockup

### 过程

假设一台 64 核的机器上某个网卡驱动出错，CPU1 到 CPU63 都在反复调用 `dev_err()`。这时 CPU0 上一个不相关的进程打了一条 `pr_info()`。

1. CPU0 写入自己的记录，trylock 成功，成为持锁者。
2. CPU1 到 CPU63 各自写入记录，trylock 失败后立即返回，继续执行，也继续产生新的日志。
3. CPU0 在 `console_unlock()` 里开始输出积压。串口每秒最多输出约 140 行。
4. 只要其他 CPU 每秒产生的日志合计超过 140 行，积压就会越来越多。CPU0 始终输出不完，也就一直无法从 `printk()` 返回。

CPU0 自己只打了一条日志，却要输出整个系统的日志。锁的移交可以把这项工作转给别的 CPU，但只要日志产生的速度超过串口输出的速度，总会有一个 CPU 停在 `console_unlock()` 里出不来。

### 为什么会报 soft lockup

先说明 soft lockup 检测的是什么。每个 CPU 上有一个高优先级的 watchdog 任务，它定期运行并更新一个时间戳。如果某个 CPU 的时间戳超过阈值（默认 20 秒）没有更新，说明这个 CPU 一直在执行某段内核代码而没有发生调度，内核就会报 `BUG: soft lockup`。

第 3 节提到，`printk()` 路径上的 `console_unlock()` 是在关闭抢占的状态下执行的。持锁者连续输出日志期间，watchdog 任务得不到运行。按默认阈值计算，连续输出 20 秒（大约 2900 行、230 KB）就会触发 soft lockup。

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

### 后续方向

mainline 在 6.6 之后逐步合入了新的 console 模型（nbcon），要解决的正是调用者同步输出带来的这些问题。在新模型里，每个 console 有自己的打印线程，平时由线程异步输出，`printk()` 的调用者不再需要等待串口。发生 panic 或 oops 时，内核通过一条不需要 console 锁的原子输出路径直接写设备，以保证可靠性。新模型要求串口驱动逐个适配，没有适配的驱动仍然按本文描述的方式工作。这部分内容没有在本树核实，具体的合入版本和各驱动的适配情况需要到对应版本的代码里确认。

## 6. 启动阶段的 console

### boot console 与正式 console

正式的串口 console 由完整的串口驱动注册，需要驱动模型、时钟、中断都初始化完成，这个时间点在启动过程中比较晚。在此之前的日志由 boot console 输出。

boot console 也就是 earlycon，是一个不依赖驱动模型的简化 console。它直接映射 UART 寄存器，用轮询方式写，波特率沿用固件配置好的值。在 cmdline 里加 `earlycon` 可以启用它，内核在解析早期参数时就会注册。

正式 console 是完整驱动注册的 console。它会根据固件描述的时钟频率重新配置波特率。

### 注册时补打历史日志

一个 console 注册时，它的 `seq` 可以设为 ring buffer 里最旧的记录，这样注册之前已经写入的日志会被补打出来。boot console 注册时就是用这种方式把更早的日志输出到串口的。

### 从 boot console 切换到正式 console

正式 console 注册并被内核选中之后，所有 boot console 都会被注销，正式 console 从 boot console 停止的位置继续输出，不会重复。cmdline 里加 `keep_bootcon` 可以保留 boot console。

内核按下面的顺序决定哪个设备作为正式 console。首先看 cmdline 的 `console=`，可以写多个，每个都会输出，其中最后一个同时作为 `/dev/console`。如果没有写 `console=`，就使用固件的描述，即 DT 的 `stdout-path` 或 ACPI 的 SPCR 表。如果这些都没有，第一个注册的 console 会被选中，通常是 `tty0`。

需要特别注意的是：只要有任意一个正式 console 被选中并注册，boot console 就会被注销，即使这个正式 console 并不是串口。

### 启动时间线

| 阶段 | 内核里发生的事 | 串口上的表现 |
|---|---|---|
| 内核入口到 earlycon 注册之前 | 还没有任何 console，日志只写入编译时分配的静态 buffer | 没有任何输出 |
| 解析早期参数（在 `setup_arch` 里） | boot console 注册，并补打 buffer 里已有的记录 | 之前的日志一次性输出，之后的日志实时输出 |
| `setup_log_buf` | 如果指定了 `log_buf_len=`，或者 CPU 数量很多，内核会分配一个更大的 buffer，并把旧记录拷贝过去 | 没有变化 |
| 正式串口驱动 probe | 正式 console 注册并被选中，boot console 被注销，正式 console 继续输出 | 出现 `console [ttyAMA0] enabled` 和 `bootconsole [pl011] disabled` |
| `late_initcall` | 如果某个 boot console 的代码位于 init 段，它会被强制注销，因为 init 段的内存即将释放 | 如果这时正式 console 还没有注册，输出会中断 |
| 进入用户态 | 启动脚本或 sysctl 可能修改 console loglevel；journald 开始读取内核日志 | 串口上的内核日志可能减少或消失 |

## 7. 补充内容

### 7.1 日志级别与 console loglevel

每条记录带有一个 0 到 7 的级别，数字越小越严重：0 EMERG、1 ALERT、2 CRIT、3 ERR、4 WARNING、5 NOTICE、6 INFO、7 DEBUG。

级别不影响写入，任何级别的日志都会进 ring buffer。级别只在 console 输出时起作用：`console_unlock()` 把记录的级别和 console loglevel 比较，级别数值小于 console loglevel 的记录才会发送到 console，其余的跳过。

本树默认的 console loglevel 是 7，也就是 0 到 6 级都输出；cmdline 里带 `quiet` 时是 4。运行时可以通过 `/proc/sys/kernel/printk` 的第一个数查看和修改。

console loglevel 只影响 console。`dmesg`、journald 等其他读者不受它限制，能读到所有级别的日志。所以串口上没有某条日志，不代表这条日志没有被打印，需要用 `dmesg` 确认。

### 7.2 日志的来源

所有来源最终都调用同一个写入入口。

| 来源 | 说明 | 是否写入 |
|---|---|---|
| 普通内核日志：`pr_err/pr_info`、`dev_err/dev_info`、`netdev_*` 等 | 带级别的 `printk` 封装 | 总是写入 |
| 不带级别的 `printk("...")` | 使用默认消息级别（本树配置为 4） | 总是写入 |
| 调试日志：`pr_debug`、`dev_dbg` | 默认被编译掉，或者由 dynamic debug 在运行时逐条开关 | 默认不产生 |
| 限速日志和一次性日志：`*_ratelimited`、`*_once` | 超出限额的调用在调用点就被丢弃 | 只写入一部分；被限速时会补一行 `callbacks suppressed` |
| 异常报告：`WARN`、oops、panic、soft/hard lockup、RCU stall、OOM | 一次输出几十到几百行（寄存器、调用栈、模块列表） | 总是写入，是日志积压的主要来源 |
| 用户态写 `/dev/kmsg` | systemd 和启动脚本用它把自己的消息加进内核日志，默认按进程限速 | 写入，并标记为来自用户态 |

有两类输出经常和内核日志混在一起，但它们不经过 printk。一类是固件和 bootloader 的串口输出，它们直接写同一个 UART，所以在 `dmesg` 里看不到。另一类是 `trace_printk` 和 tracepoint，它们写的是 ftrace 自己的 buffer，不会出现在 console 上。

### 7.3 console 之外的读者

除了 console，ring buffer 还有下面几类读者。它们都是在使用者需要时才来读取，不占用 `printk()` 调用者的时间。

| 读者 | 使用者 | 特点 |
|---|---|---|
| `/dev/kmsg` | journald、新版 `dmesg`、日志采集 agent | 每次 `open` 得到独立的读取位置；要读的记录被覆盖时返回 `EPIPE` |
| syslog 系统调用、`/proc/kmsg` | 老的 klogd、rsyslog 的内核日志输入 | `/proc/kmsg` 全系统共用一个读取位置，所以只能有一个读者 |
| kmsg_dump | pstore 的后端（ramoops、EFI 变量、ACPI ERST）、mtdoops | 在 panic、oops、重启的时刻由内核回调，把 buffer 尾部的内容保存到复位后不会丢失的介质上 |
| vmcore、内存镜像 | kdump 之后的 `crash`、`vmcore-dmesg`，以及 JTAG | 不经过内核接口，直接从内存里解析 ring buffer |

用户态的持久化路径是 journald 读 `/dev/kmsg` 再写到磁盘上，用 `journalctl -k` 查看。这条路径要求用户态已经启动、存储可用，所以启动早期的死机和 panic 的最后几行日志它是记录不到的，这两种情况只能依靠 console、kmsg_dump 和内存镜像。

不同情况下可以使用的日志获取途径如下。

| 情况 | 可用的途径 |
|---|---|
| 内核刚启动，驱动还没有初始化 | boot console；用 JTAG 读内存 |
| 正常运行 | console、`dmesg`、journald 或 rsyslog |
| 死机，但还能触发 panic | console；kmsg_dump 保存到 pstore；kdump 生成 vmcore |
| 完全挂死，没有 panic | ramoops 的 console 记录功能（它会额外注册一个 console，持续把日志写入保留内存，热复位后可以读出）；用 JTAG 读内存 |

## 8. Bring-up 常见现象速查

先确认 `dmesg` 或内存里的 log buffer 中有没有这条日志。有，说明问题在输出环节（第 3、4、6 节）；没有，说明日志没有产生或已被覆盖（第 2 节、7.2 节）。

| 现象                                                | 最可能的原因                                                    | 章节  |
| ------------------------------------------------- | --------------------------------------------------------- | --- |
| 从内核入口起完全没有输出                                      | 没开 `earlycon`，或它的 UART 地址、类型不对                            | 6   |
| 输出到 `bootconsole disabled` 附近停止                   | 被选中的正式 console 不是这个串口，或串口驱动 probe 失败。加 `keep_bootcon` 继续看 | 6   |
| 切到正式 console 后乱码                                  | 固件描述的 UART 时钟频率与实际不符                                      | 6   |
| 启动很慢，或 soft lockup、RCU stall，栈里有 `console_unlock` | 持锁者在批量输出积压。调低 loglevel，找出大量打印的代码                          | 3，5 |
| 死机，最后几条日志没出来                                      | 日志还在 ring buffer 里没来得及输出。用 JTAG、crash 或 ramoops 读取        | 3，4 |
| 日志中间缺一段，有 `printk messages dropped`               | 串口输出跟不上，记录被覆盖。加大 `log_buf_len=`                           | 2   |
| 进用户态后内核日志不上串口                                     | console loglevel 被调低，`dmesg` 里仍然有                         | 7.1 |
| 代码执行到了，但哪里都没有这条日志                                 | 用的是 `pr_debug`、`*_once` 或 `*_ratelimited`                 | 7.2 |
| 串口上有，`dmesg` 里没有                                  | 固件或 bootloader 的输出，不是内核日志                                 | 7.2 |
| 加了 `printk` 后 bug 消失                              | 同步写串口改变了时序。改用 `trace_printk`                              | 3   |

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

## 附：UART、串口、tty、console 的关系

这几个词经常混用，但它们处在不同的层次上。

### 从硬件到设备名

| 层次 | 名字 | 是什么 |
|---|---|---|
| 硬件类别 | UART | 一类串行收发控制器的统称 |
| 硬件型号 | PL011、8250/16550 | 具体的 UART 型号。PL011 是 ARM 设计的，ARM 服务器和开发板上最常见；8250/16550 常见于 x86 |
| 驱动 | amba-pl011、8250 | 内核里操作对应 UART 寄存器的驱动 |
| 设备名 | `ttyAMA0`、`ttyS0` | 驱动为它管理的第 0 个 UART 注册的设备 |

UART（Universal Asynchronous Receiver/Transmitter）是芯片里的一个硬件控制器。CPU 往它的数据寄存器写一个字节，它把这个字节拆成一位一位的电平信号，按约定的波特率从 TX 引脚发出去；RX 引脚收到的信号由它拼回字节。

串口是日常的叫法，和 UART 指的是同一个东西的不同侧面。说 UART 时强调芯片里的控制器，说串口时强调这条通信通道，包括引脚、线缆和对端的终端软件。

设备名里的 `tty` 表示它是一个终端类设备，`AMA` 来自 AMBA（PL011 挂在 ARM 的 AMBA 总线上），`0` 是编号。不同型号的 UART 驱动使用不同的前缀，所以从设备名可以看出背后是哪种 UART。

### tty 是什么

tty 是内核里终端设备的统一抽象，面向用户态。一个 tty 设备在 `/dev` 下有一个节点，进程可以对它读写：写进去的字符显示给人看，人输入的字符可以从它读出来。登录 shell 就运行在某个 tty 上。

### console 是什么

console 是内核里的另一个概念：注册给 printk、用来接收内核日志的输出对象（见第 3 节）。它只规定内核把日志文本交给它，由它负责发送出去，不规定发送到哪里。

console 和串口的关系有三种情况：

- 串口可以作为 console。这是最常见的情况，`console=ttyAMA0` 就是让这个串口承担 console 的角色。
- 不是串口的设备也可以作为 console，例如显示器上的虚拟终端 `tty0`、通过网络发送日志的 netconsole、虚拟机里的 hvc。
- 串口也可以不作为 console。一个 UART 可以只用来和别的设备通信，内核日志不往它上面发。

### 同一个名字，两个角色

串口驱动会向内核注册两样东西，它们共用 `ttyAMA0` 这个名字，背后是同一个硬件，但数据路径是独立的。

| 角色 | 使用者 | 收发方式 |
|---|---|---|
| tty 设备 `/dev/ttyAMA0` | 用户态进程。在串口上登录、输入命令走这条路径 | 中断驱动，有缓冲 |
| console `ttyAMA0` | printk。内核日志走这条路径 | 逐字符轮询，见第 3 节 |

`console=ttyAMA0` 匹配的是后者。即使不把它设为 console，`/dev/ttyAMA0` 这个 tty 设备仍然存在，仍然可以在上面登录，只是内核日志不会发到那里。

### tty0 和 ttyAMA0

它们是并列的两个 tty 设备，背后的硬件不同。

- `ttyAMA0` 背后是 UART，字符从引脚发出去，在线缆的另一端查看。
- `tty0` 背后是显示器和键盘，称为虚拟终端。字符由内核绘制到屏幕上（通过显卡或 UEFI 提供的帧缓冲），输入来自键盘。`tty1` 到 `tty6` 是可以用 Alt+F1 到 Alt+F6 切换的各个虚拟终端，`tty0` 指当前正在显示的那一个。

虚拟终端和串口一样，也同时注册了一个 console。`console=tty0` 表示把内核日志显示在屏幕上，`console=ttyAMA0` 表示发送到串口，两个都写则两处都输出。

这也是第 8 节里"输出到 `bootconsole disabled` 附近就停止"的原因之一：服务器没有接显示器，cmdline 里没有写 `console=ttyAMA0`，固件也没有指定时，内核会选择 `tty0` 作为 console，日志被绘制到没有人看的屏幕上，串口不再有输出。

### 一条日志经过的各层

`printk()` 把记录交给 console，这个 console 由串口驱动提供；串口驱动把字符逐个写入 UART 的数据寄存器；UART 把字符转换成 TX 引脚上的电平信号；对端的终端软件把信号还原成文字。

正文里说串口慢，慢在 UART 这一层：它按波特率一位一位地发送，115200 波特率下每秒约 11 KB，驱动必须等它发送完一个字符才能写入下一个。
