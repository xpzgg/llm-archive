# SDEI (Software Delegated Exception Interface)

基于 Linux 内核主线源码 + openEuler 下游补丁整理。

源码版本：Linux mainline + openEuler `arch/arm64/kernel/watchdog_sdei.c`

---

## What — SDEI 是什么

SDEI 是 ARM 定义的一种**固件向操作系统投递事件的接口规范**（ARM DEN 0054A）。它不是一种中断——不经过 GIC 中断控制器，不占用中断号，不受 `DAIF` 寄存器的中断屏蔽位约束。固件通过**直接修改 EL1 的 PC 寄存器**，强行跳入 OS 预先注册的回调地址来通知 OS。

从 ARM 异常模型的角度看，SDEI 是一个**跨特权级的事件通知通道**：EL3（Secure Monitor）或 EL2（Hypervisor）向 EL1（OS）单向投递。OS 无法主动触发 SDEI 事件，只能被动接收和处理。

### SDEI 在 ARM 特权级模型中的位置

```
 EL3  Secure Monitor  ← SDEI 事件的发起者
       ↕ SMC/HVC
 EL2  Hypervisor      ← 也可能作为 SDEI 事件的发起者
       ↕
 EL1  OS (Linux)      ← SDEI 事件的接收者，注册回调，处理事件
       ↕
 EL0  Userspace       ← 不参与 SDEI
```

SDEI 的"不可屏蔽"性来自特权级差异：EL3 对 EL1 有完全的控制权。EL1 设置的 `DAIF.I`（IRQ mask）只能屏蔽 EL1 可见的中断，无法阻止更高特权级的固件修改 EL1 的执行流。

### 源码位置

| 文件 | 说明 |
|------|------|
| `drivers/firmware/arm_sdei.c` | SDEI 核心驱动：探测固件、事件注册/使能/管理 |
| `arch/arm64/kernel/sdei.c` | ARM64 架构层：入口点设置、per-CPU 栈分配、`do_sdei_event()` |
| `arch/arm64/kernel/entry.S` | 汇编入口：`__sdei_asm_handler`，保存/恢复寄存器、切换栈 |
| `arch/arm64/kernel/entry-common.c` | `__sdei_handler()`：irqentry NMI 上下文管理 |
| `include/uapi/linux/arm_sdei.h` | SDEI ABI 函数 ID 和常量定义 |
| `include/linux/arm_sdei.h` | 内核内部数据结构和 API |
| `arch/arm64/include/asm/sdei.h` | 架构相关定义：exit mode、栈大小、入口声明 |

---

## Why — 为什么需要 SDEI

### 问题背景

ARM 系统上，EL3 固件（Secure Monitor）能感知到一些 EL1 OS 无法直接感知的关键事件：

1. **硬件错误（RAS）**：CPU 检测到不可恢复的内存错误、cache 错误等。固件需要先做紧急处理（如 poison cache line），然后通知 OS 进行 further handling（如杀死受影响的进程、隔离故障页）
2. **安全事件**：固件检测到安全攻击或需要 OS 配合的安全策略变更
3. **固件需要不可屏蔽的通知通道**：这些事件太紧急，不能因为 OS 关了中断（`local_irq_disable`）就被延迟

### 为什么现有机制不够用

ARM64 上已有的通知机制都有局限：

- **普通 IRQ/FIQ**：受 `DAIF` 寄存器控制，OS 关中断后就收不到
- **GIC Pseudo-NMI**：利用 GICv3 中断优先级穿透 PMR 屏蔽，但需要硬件支持 priority masking，不是所有平台都可用
- **SMC/HVC 同步调用**：OS 主动调用固件，但固件无法主动通知 OS

核心矛盾：**需要一条从固件到 OS 的、不可被 OS 屏蔽的异步通知通道**。SDEI 就是为了填补这个空白。

### SDEI 的定位

SDEI 解决的是**固件向 OS 的事件投递**问题。它的设计约束：

- 事件由固件发起，OS 只能接收
- OS 通过 SMC/HVC 同步调用向固件注册回调、使能/禁用事件
- 事件到达时，固件直接修改 EL1 的 PC 跳入回调，不走中断路径
- 回调执行环境极其受限（类似 NMI），必须尽快完成并返回固件

---

## How — SDEI 的核心机制

### 1. OS 与固件的通信：SMC Calling Convention

SDEI 基于 ARM SMC Calling Convention（SMCCC，ARM DEN 0028B）。OS 通过 `SMC #0` 或 `HVC #0` 指令 trap 到固件，用寄存器传递参数：

```
r0       = 函数 ID（如 SDEI_1_0_FN_SDEI_EVENT_REGISTER = 0xC4000021）
r1 - r3  = 参数
r0 (返回) = 返回值
```

通信方式在 Device Tree 或 ACPI 里声明：

```dts
firmware {
    sdei {
        compatible = "arm,sdei-1.0";
        method = "smc";     // 或 "hvc"
    };
};
```

内核在 `sdei_probe()` 时根据 DT/ACPI 选择 `sdei_smccc_smc` 或 `sdei_smccc_hvc` 作为 `sdei_firmware_call` 函数指针（`drivers/firmware/arm_sdei.c`）。

### 2. SDEI ABI：核心函数

定义在 `include/uapi/linux/arm_sdei.h`：

| 函数 | 作用 |
|------|------|
| `SDEI_VERSION` | 查询固件 SDEI 版本 |
| `SDEI_EVENT_REGISTER` | 注册回调地址和参数 |
| `SDEI_EVENT_ENABLE` / `DISABLE` | 使能/禁用事件投递 |
| `SDEI_EVENT_CONTEXT` | 在回调中查询被中断时的寄存器值 |
| `SDEI_EVENT_COMPLETE` | 回调正常完成，返回被中断点 |
| `SDEI_EVENT_COMPLETE_AND_RESUME` | 回调完成，返回到指定地址（而非被中断点） |
| `SDEI_EVENT_UNREGISTER` | 注销回调 |
| `SDEI_EVENT_STATUS` | 查询事件当前状态 |
| `SDEI_EVENT_GET_INFO` | 查询事件属性（类型、优先级等） |
| `SDEI_EVENT_ROUTING_SET` | 设置 shared 事件的路由目标 CPU |
| `SDEI_PE_MASK` / `UNMASK` | 屏蔽/解除屏蔽当前 CPU 的所有 SDEI 事件 |
| `SDEI_INTERRUPT_BIND` / `RELEASE` | 把硬件中断绑定/解绑为 SDEI 事件 |

### 3. 事件类型

- **Private 事件**（`SDEI_EVENT_TYPE_PRIVATE = 0`）：per-CPU 事件，每个 CPU 独立注册。典型如 per-CPU 的 RAS 错误通知。内核通过 IPI（`smp_call_function_single`）在每个 CPU 上分别调用 `SDEI_EVENT_REGISTER`。

- **Shared 事件**（`SDEI_EVENT_TYPE_SHARED = 1`）：全局事件，注册一次，可设置路由到特定 CPU（`SDEI_EVENT_ROUTING_SET`）。

### 4. 事件优先级

- **Normal**（`SDEI_EVENT_PRIORITY_NORMAL = 0`）：不能打断另一个 Normal 事件
- **Critical**（`SDEI_EVENT_PRIORITY_CRITICAL = 1`）：可以打断正在执行的 Normal 事件

因为 Critical 可以抢占 Normal，内核为每个 CPU 准备了**两套独立的栈**：

```c
// arch/arm64/kernel/sdei.c
DEFINE_PER_CPU(unsigned long *, sdei_stack_normal_ptr);
DEFINE_PER_CPU(unsigned long *, sdei_stack_critical_ptr);
```

以及两套 Shadow Call Stack（如果启用了 `CONFIG_SHADOW_CALL_STACK`）。

同时跟踪当前正在处理的事件：

```c
DEFINE_PER_CPU(struct sdei_registered_event *, sdei_active_normal_event);
DEFINE_PER_CPU(struct sdei_registered_event *, sdei_active_critical_event);
```

### 5. 完整生命周期

#### 5.1 初始化：探测固件

```
sdei_probe()                              // drivers/firmware/arm_sdei.c
  ├── sdei_get_conduit()                  // 从 DT/ACPI 确定 SMC 还是 HVC
  ├── sdei_api_get_version()              // 查询固件 SDEI 版本
  ├── sdei_platform_reset()               // 重置所有事件到初始状态
  ├── sdei_arch_get_entry_point(conduit)  // 获取架构层入口地址
  │     └── 初始化 per-CPU 栈
  │     └── 返回 __sdei_asm_handler（或 KPTI trampoline 地址）
  ├── cpu_pm_register_notifier()          // 注册低功耗状态回调
  ├── register_reboot_notifier()          // 注册重启回调
  └── cpuhp_setup_state()                 // 注册 CPU hotplug 回调
```

`sdei_arch_get_entry_point()` 把内核的 SDEI 入口地址存入 `sdei_entry_point`。后续所有 `SDEI_EVENT_REGISTER` 调用都把这个地址传给固件——固件事件到达时就跳到这里。

如果启用了 KPTI（`CONFIG_UNMAP_KERNEL_AT_EL0`），入口地址指向 trampoline（`__sdei_asm_entry_trampoline`），因为正常内核映射可能被解除。

#### 5.2 注册事件

```
sdei_event_register(event_num, callback, arg)
  ├── sdei_event_create()                 // 分配 sdei_event + sdei_registered_event
  │     └── registered->callback = callback
  │     └── registered->callback_arg = arg
  ├── [Shared] sdei_api_event_register(sdei_entry_point, ...)
  │     // 直接调用固件，传入内核入口地址和 registered_event 指针
  └── [Private] sdei_do_cross_call()      // 通过 IPI 在每个 CPU 上分别注册
```

关键点：注册时传给固件的参数是 `sdei_entry_point`（内核入口地址）和 `sdei_registered_event` 结构体指针。固件在事件到达时会把后者原样传回，内核据此找到对应的回调。

#### 5.3 事件到达：固件跳入内核

当事件触发时，固件直接跳到 OS 注册的入口地址，寄存器布局：

```
x0 = 事件号 (event number)
x1 = sdei_registered_event 指针（注册时传入的）
x2 = 被中断的 PC
x3 = 被中断的 PSTATE
x4 - x17 = 固件保留的原始寄存器值
```

#### 5.4 汇编入口：`__sdei_asm_handler`

`arch/arm64/kernel/entry.S:978`

这是固件跳入的第一个内核代码。它做的事情：

```
__sdei_asm_handler:
  1. 保存被中断的寄存器到 sdei_registered_event->interrupted_regs
     （x2-x29, lr, sp）
  2. 根据 priority 字段选择 normal 还是 critical 栈
  3. 切换到 SDEI 专用栈（不借用被中断上下文的栈）
  4. 恢复 sp_el0 为当前 task
  5. 调用 __sdei_handler(regs, arg) 进入 C 代码
  6. 返回后，根据返回值选择 COMPLETE 或 COMPLETE_AND_RESUME
  7. 通过 SMC/HVC #0 返回固件
```

为什么需要自己的栈？因为 SDEI 可以在任意上下文中打断内核——可能打断正在用 sp 做临时存储的代码（比如其他 exception entry 路径）。借用被中断者的栈不安全。

#### 5.5 C 处理器：`__sdei_handler` → `do_sdei_event`

```c
// arch/arm64/kernel/entry-common.c
__sdei_handler(regs, arg)
  ├── 修正 PSTATE.PAN（不同 SDEI 版本行为不一致）
  ├── irqentry_nmi_enter(regs)           // 进入 NMI 上下文
  ├── ret = do_sdei_event(regs, arg)     // 架构层处理
  └── irqentry_nmi_exit(regs, state)     // 退出 NMI 上下文
```

`irqentry_nmi_enter/exit` 是内核的 NMI 上下文管理框架，处理 preempt_count、rcu 等簿记。

```c
// arch/arm64/kernel/sdei.c
do_sdei_event(regs, arg)
  ├── 补全被固件 clobbered 的寄存器（x0-x3，通过 SDEI_EVENT_CONTEXT 查询）
  ├── err = sdei_event_handler(regs, arg)  // 调用用户注册的回调
  ├── 检查 elr_el1 是否被篡改（SDEI handler 里不能触发同步异常）
  └── 根据返回值决定返回地址：
       - 被中断时中断已屏蔽 → 返回原地址（SDEI_EV_HANDLED）
       - 被中断时中断未屏蔽 → 返回 IRQ exception vector 地址
         （让内核正常处理 pending 的中断、信号等）
```

最后这个设计很关键：如果 SDEI 事件打断了开中断的内核代码，返回时不是直接回到被中断点，而是跳到 IRQ vector，让内核走一遍正常的中断返回路径（处理 softirq、信号、重调度等）。

#### 5.6 返回固件

`__sdei_asm_handler` 在回调返回后，通过 `SMC #0` 或 `HVC #0` 调用 `SDEI_EVENT_COMPLETE` 或 `SDEI_EVENT_COMPLETE_AND_RESUME`，把控制权交还固件。固件恢复被中断的 EL1 上下文继续执行。

### 6. 约束：SDEI handler 的执行环境

SDEI handler 运行在**比 NMI 更受限的环境**中：

- **不能睡眠**：没有进程上下文
- **不能持锁**：可能打断正在持同一把锁的代码，导致死锁
- **不能调度**：没有可调度的上下文
- **不能触发同步异常**：`do_sdei_event()` 里检查了 `elr_el1` 是否被修改，如果 handler 触发了 page fault 等同步异常会打印 `unsafe: exception during handler` 警告
- **不能长时间执行**：因为 Critical 事件可以抢占 Normal，但不能再被 Critical 抢占（没有第三套栈），所以 Normal handler 拖太久会影响 Critical 事件的延迟

内核用 `NOKPROBE_SYMBOL` 标记关键函数，防止 kprobe 在 SDEI 路径上插入断点（kprobe 本身会触发同步异常）。

### 7. CPU Hotplug 和低功耗

- **CPU online**（`sdei_cpuhp_up`）：重新注册该 CPU 的 private 事件，unmask SDEI
- **CPU offline**（`sdei_cpuhp_down`）：注销该 CPU 的 private 事件，mask SDEI
- **CPU suspend/resume**（`sdei_pm_nb`）：mask/unmask SDEI，shared 事件需要在 resume 后 re-register 和 re-enable（因为固件可能在低功耗状态中丢失了注册信息）

---

## So What — 对系统的影响

### 1. GHES/RAS 的基石

SDEI 在主线 Linux 中的主要消费者是 **ACPI GHES (Generic Hardware Error Source)**。ARM 的 firmware-first RAS 模型要求固件先处理硬件错误，然后通知 OS。SDEI 是这个通知通道。

```c
// drivers/firmware/arm_sdei.c
sdei_register_ghes(ghes, normal_cb, critical_cb)
  ├── event_num = ghes->generic->notify.vector
  ├── 查询事件优先级，选择 normal_cb 或 critical_cb
  ├── sdei_event_register(event_num, cb, ghes)
  └── sdei_event_enable(event_num)
```

没有 SDEI，firmware-first RAS 在 ARM 上就没有可靠的不可屏蔽通知通道。

### 2. 对 KVM 的影响

`do_sdei_event()` 特殊处理了 SDEI 事件打断 VCPU 运行的场景：如果事件打断了 guest（EL0 或 EL1），返回时会跳到 KVM 的 world-switch 路径（通过返回 `vbar + offset`），让 KVM 能把 pending 的中断/信号注入给 guest。

但 SDEI handler 里触发同步异常会导致 KVM hyp-panic，因为 KVM 不期望在 world-switch 过程中被同步异常打断。

### 3. KPTI 安全

如果启用了 `CONFIG_UNMAP_KERNEL_AT_EL0`，SDEI 入口通过 trampoline（`__sdei_asm_entry_trampoline`）间接进入内核：

1. 固件跳到 trampoline（在 tramp text 段，始终映射）
2. Trampoline 检查 `ttbr1_el1` 的 ASID 位判断当前是否在用户态页表
3. 如果是，先 `tramp_map_kernel` 恢复内核映射
4. 然后跳到 `__sdei_asm_handler`

退出时通过 `__sdei_asm_exit_trampoline` 恢复原始 ttbr1。

### 4. openEuler SDEI Watchdog（下游应用）

openEuler 利用 SDEI 的不可屏蔽投递能力，实现了基于 SDEI 的 hardlockup 检测器。

#### 核心思路：双时钟源交叉检测

```
EL3 Secure Physical Timer (HWIRQ 29)  ← 通过 SDEI 投递，不可屏蔽（"检查者"）
EL1 hrtimer (普通 IRQ)                ← 可被 local_irq_disable 屏蔽（"被检查者"）
```

Secure timer 每隔 `watchdog_thresh`（默认 10 秒）到期，固件通过 SDEI 跳入内核的 `sdei_watchdog_callback`。callback 检查 EL1 hrtimer 的触发计数（`hrtimer_interrupts`）是否增长——没增长说明 hrtimer 无法触发，即 CPU 卡死在关中断路径。

```
时间(秒):  0        10         20         30
           |         |          |          |
EL1 hrtimer:  tick tick tick     (停了，CPU 关中断卡死)
                                    ↑
EL3 SDEI:         ↑         ↑         ↑
                  OK       OK      LOCKUP!
               (hrt增加) (hrt增加)  (hrt没变)
```

初始化流程：

```
sdei_watchdog_hardlockup_probe()              // arch/arm64/kernel/watchdog_sdei.c
  ├── sdei_api_event_interrupt_bind(29)       // secure timer → SDEI 事件
  ├── sdei_api_set_secure_timer_period(10)    // 设 10 秒周期
  ├── on_each_cpu(sdei_nmi_watchdog_bind)     // per-CPU bind
  └── sdei_event_register(callback)           // 注册回调
```

这是对主线 perf-based hardlockup detector 的替代——不依赖 GIC pseudo-NMI，但依赖 EL3 固件支持 secure timer 周期配置。

#### disable_sdei_nmi_watchdog

Boot parameter，禁用 SDEI watchdog，回退到主线 perf PMU 方案。适用于：虚拟机（SDEI 不可用）、固件不支持、调试误报等场景。

---

## 附录：SDEI vs GIC Pseudo-NMI

两者都能在关中断时打断 CPU，但本质完全不同：

| | GIC Pseudo-NMI | SDEI |
|---|---|---|
| 本质 | 硬件机制（中断优先级） | 固件-OS 协议（特权级跳转） |
| 投递者 | GIC 中断控制器 | EL3 固件（Secure Monitor） |
| 打断原理 | 高优先级中断穿透 `ICC_PMR_EL1` 屏蔽 | 固件直接改 PC，绕过所有中断屏蔽 |
| 经过 GIC | 是，走正常 IRQ 路径 | 否，完全绕过 GIC |
| OS 入口 | IRQ exception vector | 固件跳到 OS 注册的入口地址 |
| 依赖 | GICv3 + priority masking | EL3 固件实现 SDEI 规范 |
| 主线内核用途 | hardlockup detector | GHES/RAS |
| 启用方式 | `irqchip.gicv3_pseudo_nmi=1` | Device Tree `arm,sdei-1.0` |

Pseudo-NMI 是纯硬件方案——不依赖固件，只要 GICv3 硬件支持就行。`local_irq_disable()` 把 `ICC_PMR_EL1` 设为 `IRQOFF` 阈值，但 pseudo-NMI 的中断优先级高于该阈值，所以 GIC 仍然会把它送到 CPU。

SDEI 是固件协作方案——依赖 EL3 固件实现 SDEI 规范。投递不经过 GIC，而是固件直接操作 EL1 的 PC。特权级差异保证了不可屏蔽性。
