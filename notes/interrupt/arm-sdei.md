# SDEI (Software Delegated Exception Interface)

基于 Linux 内核主线源码 + openEuler 下游补丁整理。

---

## What — SDEI 是什么

SDEI 是 ARM 定义的**固件向操作系统投递事件的接口规范**（ARM DEN 0054A）。

它不是一种中断。不经过 GIC，不占用中断号，不受 `DAIF` 中断屏蔽位约束。固件通过**直接修改 EL1 的 PC 寄存器**，强行跳入 OS 预先注册的回调地址来投递事件。

```
 EL3  Secure Monitor  ← SDEI 事件的发起者（固件）
       ↕ SMC/HVC
 EL2  Hypervisor      ← 也可能作为发起者
       ↕
 EL1  OS (Linux)      ← 接收者：注册回调，处理事件
       ↕
 EL0  Userspace       ← 不参与
```

SDEI 的"不可屏蔽"性来自特权级差异：EL3 对 EL1 有完全控制权。EL1 的 `DAIF.I` 只能屏蔽 EL1 可见的中断，无法阻止 EL3 修改 EL1 的执行流。

源码位置：

| 文件 | 说明 |
|------|------|
| `drivers/firmware/arm_sdei.c` | 核心驱动：探测、注册、管理 |
| `arch/arm64/kernel/sdei.c` | 架构层：入口点、per-CPU 栈、`do_sdei_event()` |
| `arch/arm64/kernel/entry.S` | 汇编入口 `__sdei_asm_handler` |
| `arch/arm64/kernel/entry-common.c` | C 入口 `__sdei_handler()`：NMI 上下文管理 |
| `include/uapi/linux/arm_sdei.h` | ABI 函数 ID 定义 |
| `include/linux/arm_sdei.h` | 内核内部 API |
| `arch/arm64/include/asm/sdei.h` | 架构定义 |

---

## Why — 为什么需要 SDEI

EL3 固件能感知到 OS 无法直接感知的关键事件——硬件错误（RAS）、安全事件等。这些事件太紧急，不能因为 OS 关了中断就被延迟。

但 ARM64 上已有的通知机制都不够：

- **普通 IRQ**：受 `DAIF` 控制，`local_irq_disable()` 后就收不到
- **GIC Pseudo-NMI**：用中断优先级穿透 PMR 屏蔽，但需要 GICv3 硬件支持，不是所有平台都有
- **SMC/HVC**：OS 主动调固件，但固件无法主动通知 OS

核心矛盾：**需要一条从固件到 OS 的、不可被 OS 屏蔽的异步通知通道**。SDEI 填补的就是这个空白。

---

## How — SDEI 中断的完整机制

按一个 SDEI 事件从准备到处理完毕的时间线讲述。

### 1. 建立通道：OS 告诉固件"你跳到这里来"

在使用 SDEI 之前，OS 必须先告诉固件"你的入口点在哪里"。

`sdei_probe()` 在启动时执行（`drivers/firmware/arm_sdei.c`）：

1. **确定通信方式**：从 Device Tree 或 ACPI 读取固件用的是 `smc` 还是 `hvc`，设置 `sdei_firmware_call` 函数指针
2. **查询版本**：`SDEI_VERSION` 查询固件是否支持 SDEI 1.x
3. **获取入口地址**：`sdei_arch_get_entry_point(conduit)` 做两件事：
   - 为每个 CPU 分配两套独立的栈（normal 和 critical），因为 SDEI 可以在任意上下文中打断内核，不能借用被中断者的栈
   - 返回 `__sdei_asm_handler` 的地址（如果启用了 KPTI，则返回 trampoline 地址 `__sdei_asm_entry_trampoline`，因为此时内核映射可能被解除）
4. **注册 hotplug/低功耗/reboot 回调**

这个入口地址存入 `sdei_entry_point`。后续所有事件注册都把它传给固件——固件在事件到达时就跳到这里。

### 2. 注册事件：OS 告诉固件"这个事件发生时通知我"

事件来源有两种：

- **固件预定义的事件**：如 RAS 硬件错误通知，事件号由固件规范定义
- **OS 绑定的硬件中断**：通过 `SDEI_INTERRUPT_BIND` 把一个硬件中断（如 secure timer HWIRQ 29）绑定为 SDEI 事件，固件会返回一个事件号

OS 调用 `sdei_event_register(event_num, callback, arg)` 注册事件：

1. 内核分配 `sdei_registered_event` 结构体，记录回调函数和参数
2. 调用固件的 `SDEI_EVENT_REGISTER`，传入 `sdei_entry_point`（入口地址）和 `sdei_registered_event` 指针。固件记录下这两者
3. 调用 `SDEI_EVENT_ENABLE` 使能事件

注册时有两种事件类型：

- **Private 事件**：per-CPU，每个 CPU 独立注册。内核通过 IPI 在每个 CPU 上分别调用 `SDEI_EVENT_REGISTER`
- **Shared 事件**：全局注册一次，可设路由到特定 CPU

注册完成后，固件就知道"当事件 N 发生时，跳到入口地址，带上 registered_event 指针"。

### 3. 事件触发：EL3 什么时候、怎么打断 OS

#### 什么时候

由固件决定。典型场景：

- CPU 检测到 RAS 硬件错误（内存 CE/UCE），固件先做紧急处理，然后通过 SDEI 通知 OS
- OS 通过 `SDEI_INTERRUPT_BIND` 绑定的硬件中断发生（如 secure timer 到期）
- 固件检测到需要 OS 配合的安全事件

#### 怎么打断

固件**直接修改 EL1 的 CPU 状态**：

```
1. 保存 EL1 当前的 PC → x2, PSTATE → x3
2. 设置 x0 = 事件号, x1 = 注册时的 sdei_registered_event 指针
3. 保留 x4-x17 的原始值
4. 修改 EL1 的 PC = 注册时的入口地址（sdei_entry_point）
5. EL1 开始执行 __sdei_asm_handler
```

这不是中断——没有经过 GIC，没有走 exception vector。EL3 直接操作 EL1 的寄存器，EL1 的 `DAIF.I`（IRQ mask）对此毫无影响。

这就解释了为什么 SDEI 不可屏蔽：**打断能力来自特权级差异，不是硬件中断机制**。

#### 优先级：Critical 可以抢占 Normal

SDEI 定义两个优先级：

- **Normal**：不能打断另一个 Normal
- **Critical**：可以打断正在执行的 Normal 事件

内核为每个 CPU 准备了两套独立的栈和 shadow call stack，就是因为 Critical 可能抢占 Normal，需要独立的执行环境。

### 4. OS 处理：汇编入口 → C handler → 返回固件

#### 4.1 汇编入口 `__sdei_asm_handler`

`arch/arm64/kernel/entry.S:978`

这是固件跳入的第一个内核代码：

```
__sdei_asm_handler:
  1. 保存被中断的寄存器到 sdei_registered_event->interrupted_regs
     （x2-x29, lr, sp——固件已经把 PC 放到 x2、PSTATE 放到 x3）
  2. 根据 sdei_registered_event->priority 选择 normal 还是 critical 栈
  3. 切换到 SDEI 专用栈
  4. 记录当前活跃事件（sdei_active_normal_event / sdei_active_critical_event）
  5. 恢复 sp_el0 为当前 task
  6. 调用 __sdei_handler(regs, arg) 进入 C 代码
```

为什么必须用自己的栈？SDEI 可以在任意上下文中打断内核——可能打断其他 exception entry 路径中正在用 sp 做临时存储的代码。借用被中断者的栈不安全。

#### 4.2 C 入口 `__sdei_handler`

`arch/arm64/kernel/entry-common.c:968`

```c
__sdei_handler(regs, arg)
  ├── 修正 PSTATE.PAN（不同 SDEI 版本行为不一致，显式设置）
  ├── irqentry_nmi_enter(regs)         // 进入 NMI 上下文（更新 preempt_count 等）
  ├── ret = do_sdei_event(regs, arg)
  └── irqentry_nmi_exit(regs, state)   // 退出 NMI 上下文
```

#### 4.3 架构层处理 `do_sdei_event`

`arch/arm64/kernel/sdei.c:204`

```c
do_sdei_event(regs, arg)
  ├── 补全被固件 clobbered 的寄存器（x0-x3）
  │     固件用 x0-x3 传参，原始值丢失了
  │     通过 SDEI_EVENT_CONTEXT 向固件查询原始值，恢复到 regs 中
  ├── err = sdei_event_handler(regs, arg)   // 调用用户注册的回调
  │     这个回调就是 sdei_event_register() 时传入的 callback
  ├── 检查 elr_el1 是否被修改
  │     如果被修改，说明 handler 里触发了同步异常（page fault 等）→ 打印 unsafe 警告
  └── 决定返回方式：
       - 被中断时中断已屏蔽 → 返回 SDEI_EV_HANDLED，回到被中断点
       - 被中断时中断未屏蔽 → 返回 IRQ exception vector 地址
```

最后这个设计很重要：如果 SDEI 打断了开中断的内核代码，返回时不是直接回到被中断点，而是跳到 IRQ vector，让内核走正常的中断返回路径（处理 softirq、信号、重调度等）。如果打断了 KVM guest，则跳到 KVM world-switch 路径。

#### 4.4 返回固件

`__sdei_asm_handler` 在回调返回后，恢复被中断的寄存器（x18-x29, lr, sp），然后：

1. 根据返回值选择 `SDEI_EVENT_COMPLETE`（返回被中断点）或 `SDEI_EVENT_COMPLETE_AND_RESUME`（返回指定地址）
2. 通过 `SMC #0` 或 `HVC #0` trap 回固件
3. 固件恢复被中断的 EL1 上下文继续执行

```
  EL3 固件                      EL1 Linux
    |                              |
    |-- 修改 PC, 跳入 ------->  __sdei_asm_handler
    |                              |-- 保存寄存器
    |                              |-- 切栈
    |                              |-- __sdei_handler
    |                              |     |-- do_sdei_event
    |                              |          |-- callback
    |                              |          |-- 决定返回方式
    |                              |-- 恢复寄存器
    |<-- SMC #0 (COMPLETE) -----  |
    |                              |
    |-- 恢复 EL1 上下文 ------->  被中断的代码继续执行
```

### 5. Handler 的限制

SDEI handler 运行在一个极其受限的环境中，因为**它可以在任意时刻打断任意代码**：

- **不能睡眠**：没有进程上下文，调度器无法切换回来
- **不能持锁**：可能打断正在持同一把锁的代码，死锁
- **不能调度**：同上
- **不能触发同步异常**：page fault、alignment fault 等会导致 `elr_el1` 被修改，内核检测到会打印 `unsafe: exception during handler`。在 KVM 场景下甚至会 hyp-panic
- **必须尽快完成**：Normal handler 拖太久会影响 Critical 事件的延迟

内核用 `NOKPROBE_SYMBOL` 标记关键函数，防止 kprobe 在 SDEI 路径上插入断点（kprobe 本身会触发同步异常）。

### 6. Hotplug 和低功耗

- **CPU online**（`sdei_cpuhp_up`）：重新注册该 CPU 的 private 事件，unmask SDEI
- **CPU offline**（`sdei_cpuhp_down`）：注销 private 事件，mask SDEI
- **CPU suspend/resume**（`sdei_pm_nb`）：suspend 时 mask，resume 时 unmask 并 re-register/re-enable（固件可能在低功耗状态中丢失了注册信息）

---

## So What — 对系统的影响

### 1. GHES/RAS 的基石

SDEI 在主线 Linux 中的主要消费者是 **ACPI GHES**。ARM 的 firmware-first RAS 模型要求固件先处理硬件错误，然后通知 OS。SDEI 是这个通知通道。

```c
sdei_register_ghes(ghes, normal_cb, critical_cb)
  ├── event_num = ghes->generic->notify.vector  // ACPI 定义的事件号
  ├── 查询事件优先级，选择 normal_cb 或 critical_cb
  ├── sdei_event_register(event_num, cb, ghes)
  └── sdei_event_enable(event_num)
```

### 2. KPTI 安全

如果启用了 `CONFIG_UNMAP_KERNEL_AT_EL0`，SDEI 入口通过 trampoline 进入：

1. 固件跳到 `__sdei_asm_entry_trampoline`（在 tramp text 段，始终映射）
2. Trampoline 检查 `ttbr1_el1` 的 ASID 位，判断当前是否在用户态页表
3. 如果是，先 `tramp_map_kernel` 恢复内核映射
4. 然后跳到 `__sdei_asm_handler`
5. 退出时通过 `__sdei_asm_exit_trampoline` 恢复原始 ttbr1

### 3. openEuler SDEI Watchdog（下游应用）

openEuler 利用 SDEI 的不可屏蔽投递能力，实现了基于 SDEI 的 hardlockup 检测器。

源码：`arch/arm64/kernel/watchdog_sdei.c`（openEuler 下游，主线无此文件）

#### 核心思路：不可屏蔽时钟源看管可屏蔽时钟源

```
EL3 Secure Physical Timer (HWIRQ 29)  ← 通过 SDEI 投递，不可屏蔽（"检查者"）
EL1 hrtimer (普通 IRQ)                ← 可被 local_irq_disable 屏蔽（"被检查者"）
```

通过 `SDEI_INTERRUPT_BIND` 把 secure timer（HWIRQ 29）绑定为 SDEI 事件，设 10 秒周期。每次 SDEI callback 查看 EL1 hrtimer 的触发计数（`hrtimer_interrupts`）是否增长——没增长 = hrtimer 无法触发 = CPU 卡死在关中断路径。SDEI timer 单向检查 hrtimer，hrtimer 不检查 SDEI。

```
时间(秒):  0        10         20         30
           |         |          |          |
EL1 hrtimer:  tick tick tick     (停了，CPU 关中断卡死)
                                    ↑
EL3 SDEI:         ↑         ↑         ↑
                  OK       OK      LOCKUP!
               (hrt增加) (hrt增加)  (hrt没变)
```

`disable_sdei_nmi_watchdog` boot parameter 可禁用此方案，回退到主线 perf PMU 方案。适用于虚拟机（SDEI 不可用）、固件不支持、调试误报等场景。

---

## 附录：SDEI vs GIC Pseudo-NMI

两者都能在关中断时打断 CPU，但本质完全不同：

| | GIC Pseudo-NMI | SDEI |
|---|---|---|
| 本质 | 硬件机制（中断优先级） | 固件-OS 协议（特权级跳转） |
| 投递者 | GIC 中断控制器 | EL3 固件 |
| 打断原理 | 高优先级中断穿透 `ICC_PMR_EL1` 屏蔽 | 固件直接改 PC，绕过所有中断屏蔽 |
| 经过 GIC | 是 | 否 |
| OS 入口 | IRQ exception vector | 固件跳到 OS 注册的入口地址 |
| 依赖 | GICv3 硬件 | EL3 固件实现 SDEI 规范 |
| 主线内核用途 | hardlockup detector | GHES/RAS |
| 启用方式 | `irqchip.gicv3_pseudo_nmi=1` | DT `arm,sdei-1.0` |
