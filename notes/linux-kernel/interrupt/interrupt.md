位置,标志字符,含义,详细解释
第 1 位,d / .,Disable IRQ,是否关闭了普通硬中断。• d: 当前代码关闭了硬中断（local_irq_disable）。• .: 硬中断是开启的。
第 2 位,n / .,Need Resched,是否设置了重调度标志。• n: 意味着当前任务的时间片用完了，或者有更高优先级的任务在等待，系统急需进行进程切换（Need resched）。• .: 不需要重调度。
第 3 位,h / H / .,Hard IRQ / Soft IRQ,当前处于什么中断上下文。• H: 正在执行硬中断处理函数（Hard IRQ）。• h: 正在执行软中断处理函数（Soft IRQ）。• .: 处于普通的进程上下文。
第 4 位,s / .,Softirq Disable,是否关闭了软中断。• s: 当前处于关闭软中断保护区（local_bh_disable）。• .: 软中断未被关闭。
