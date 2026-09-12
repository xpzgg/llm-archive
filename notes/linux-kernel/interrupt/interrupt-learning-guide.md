# Linux 中断子系统学习指南

> 目标：2-3 个月系统掌握 ARM64/GICv3 中断子系统的设计和工作原理。
> 前置知识：无（零基础友好），但了解 ARM64 异常模型有帮助。

主线：跟着一个中断信号走，从硬件到达 CPU 开始，每一步因为前一步的局限才引入新概念。每个阶段由一个具体问题驱动。

每个阶段的阅读节奏：

1. 读文件顶部注释和 `Documentation/`，理解设计意图和 tradeoff
2. 理解核心结构体为什么长这样，它抽象了什么
3. 跟踪一条最简单场景的完整路径，只走主干不进分支，遇到下一阶段的概念先标记"这里以后打开"
4. 用观测工具验证

---

## 阶段一：硬件中断到达 CPU 后发生了什么？

**驱动问题：** 网卡收到一个包，拉高了一根物理信号线 — CPU 怎么知道？内核怎么接管？

**核心认知：** 中断本质是硬件向 CPU 的"紧急呼叫"。整个过程分三层：GIC（中断控制器）接收和路由、CPU 异常入口机制、内核通用处理框架。三层各有分工，这是理解中断的第一幅全景图。

**阶段输出：** 能完整描述从一个硬件信号变成内核中断处理函数被调用的全过程。

### ARM64 异常模型基础

ARM64 CPU 有四个异常级别（EL0-EL3），中断在 EL1（内核态）处理。关键概念：

- **异常向量表（Exception Vector Table）**：CPU 硬件实现，`VBAR_EL1` 寄存器指向基地址。根据异常类型（IRQ/FIQ/SError/Sync）和来源（EL0/EL1、AArch32/AArch64）共 16 个入口。
- **IRQ vs FIQ**：ARM 有两条中断线。GIC 把 Group 0（安全）中断发给 FIQ，Group 1（非安全）中断发给 IRQ。Linux 正常运行只处理 IRQ。
- **中断不是函数调用**：CPU 没有主动"调用"中断处理。是硬件自动保存上下文（SPSR_EL1, ELR_EL1）、切换栈（SP_EL1）、跳转到向量表入口。这个过程没有软件参与。

### GIC 的角色：中断路由器

GIC (Generic Interrupt Controller) 不是简单的一根线连到 CPU。它做三件事：

1. **接收（Ack）**：来自设备的中断信号（电平触发或边沿触发）到达 GIC
2. **路由（Route）**：决定这个中断发给哪个 CPU（亲和性）
3. **优先级仲裁（Priority Arbitration）**：多个中断同时待处理时，选最高优先级的投递

GICv3 的四种中断类型：

| 类型 | ID 范围 | 来源 | 路由方式 | 典型例子 |
|------|---------|------|----------|---------|
| SGI (Software Generated Interrupt) | 0–15 | 软件写 `GICD_SGIR` | 指定目标 CPU | IPI（CPU 间通信） |
| PPI (Private Peripheral Interrupt) | 16–31 | CPU 本地设备 | 固定到当前 CPU | arch timer, PMU |
| SPI (Shared Peripheral Interrupt) | 32–1019 | 外部共享设备 | 可配置目标 CPU | 网卡、磁盘控制器 |
| LPI (Locality-specific Peripheral Interrupt) | 8192+ | 基于 MSI 的设备 | 通过 ITS 查表路由 | PCIe 设备 |

**为什么需要这四种？** 每种的引入都解决了前一种的局限：
- SGI：CPU 之间需要通信（如 TLB flush IPI），不能用外部中断线 → 软件触发
- PPI：每个 CPU 有自己的 timer、PMU，天然私有，不需要路由 → 固定绑定
- SPI：外部设备的中断需要能被任何 CPU 处理 → 可配置路由
- LPI：现代 SoC 有大量 PCIe 设备，SPI 的 ~1000 个 ID 不够用，且配置在寄存器中扩展成本高 → 配置放内存，ID 空间扩展到数万

### 核心结构体

| 结构体 | 文件 | 它抽象了什么 |
|--------|------|-------------|
| `irq_desc` | `include/linux/irqdesc.h` | 一个 Linux IRQ 号的完整描述（handler、chip、状态、统计） |
| `irq_data` | `include/linux/irq.h` | 中断控制器的视角（hwirq、chip、domain） |
| `irq_chip` | `include/linux/irq.h` | 中断控制器的操作接口（mask、unmask、ack、eoi 等） |
| `irq_domain` | `include/linux/irqdomain.h` | 硬件中断号到 Linux IRQ 号的映射关系 |

重点理解：
- `irq_desc` 是 Linux 内核对"一个中断"的完整抽象。它的 `irq_data` 嵌入在内部（不是指针），因为一个 desc 一定有一个 data
- `irq_chip` 是一套函数指针，每个中断控制器（GIC、GPIO expander 等）实现自己的版本。这是内核"用对象替代 switch-case"的经典手法
- `irq_domain` 解决的核心问题：硬件中断号（hwirq）和 Linux IRQ 号是两套编号，需要映射

### 主干路径：从硬件信号到内核 handler

```
设备发出中断信号
  → GIC 接收，根据亲和性和优先级选择目标 CPU
  → GIC 向该 CPU 发送 IRQ 信号
  → CPU 响应 IRQ 异常：
    → 硬件自动保存 SPSR_EL1（当前 PSTATE）和 ELR_EL1（返回地址）
    → 切换到 SP_EL1（内核栈）
    → 跳转到 VBAR_EL1 + 偏移（IRQ 向量入口）

  → 内核异常入口（arch/arm64/kernel/entry.S）
    → 保存通用寄存器到 pt_regs
    → 跳转到 C 函数 handle_arch_irq()

  → handle_arch_irq()          ← GIC 驱动在启动时注册的回调
    → gic_handle_irq()         ← GICv3 的处理入口
      → gic_read_iar()         ← 读取 ICC_IAR1_EL1，硬件返回最高优先级 pending 中断的 ID
                                 （读取 IAR 同时完成 ACK，告诉 GIC "我收到了"）
      → 根据 ID 范围分派：
        → SGI/PPI（ID 0-31）：通过 per-CPU 的 irq_domain 处理
        → SPI（ID 32-1019）：通过全局 irq_domain 处理
        → LPI（ID 8192+）：【边界】阶段五再打开
      → generic_handle_irq(linux_irq)
        → generic_handle_irq_desc()
          → desc->handle_irq(desc)   ← 调用 irq_desc 上的 high-level handler
            → handle_fasteoi_irq() 或 handle_level_irq() 等
              → handle_irq_event()
                → __handle_irq_event_percpu()
                  → 遍历 action 链表，调用每个已注册的 handler
                    → action->handler(irq, action->dev_id)  ← 驱动注册的中断处理函数

  → gic_eoi_irq()              ← 写 ICC_EOIR1_EL1，告诉 GIC "处理完了"
  → 恢复 pt_regs，返回被中断的上下文
```

这个路径要特别关注几个设计决策：
- **ACK 时机**：GIC 在读取 IAR 时就 ACK（不是在 EOI 时）。ACK 后 GIC 才会投递下一个同优先级的中断
- **handler 链表**：一个 IRQ 号可以有多个 handler（共享中断，`IRQF_SHARED`）。这是因为早期硬件多个设备只能共享一根中断线
- **high-level handler 的不同类型**：`handle_fasteoi_irq`（GIC 用这种）vs `handle_level_irq`，区别在于何时 mask/unmask 中断。这反映了不同硬件的 ACK 语义

观测：`cat /proc/interrupts` 看 CPU 上各 IRQ 的分布和计数。

---

## 阶段二：Linux 怎么找到正确的中断处理函数？

**驱动问题：** 驱动调用 `request_irq()` 注册了一个处理函数 — 系统怎么把硬件中断号和这个函数对应起来？

**核心认知：** 硬件中断号（hwirq）和 Linux IRQ 号是两套命名空间。irq_domain 就是它们之间的翻译层。现代系统有多个中断控制器级联，所以有多个 domain，形成一棵树。

**阶段输出：** 能画出一个典型 ARM64 系统的 irq_domain 层级结构，理解从设备树到 `request_irq()` 的完整映射链。

### 为什么要两套编号？

- **hwirq**：硬件视角的编号。GIC 的 ID 0-1019 是 GIC 自己的编号。但系统里还有 GPIO controller、级联的中断控制器，它们各自有自己的 hwirq 编号空间
- **Linux IRQ 号**：内核全局唯一编号。驱动只知道 Linux IRQ 号，不关心背后经过了几个 domain 的翻译

### irq_domain 的层级结构

一个典型的 ARM64 系统：

```
                    irq_domain (GIC)
                    hwirq: 0-1019
                    linux_irq: 0-1019（通常 1:1 映射）
                         |
                    +---------+---------+
                    |                   |
              irq_domain          irq_domain
              (GPIO controller)   (级联的中断控制器)
              hwirq: 0-127        hwirq: 0-31
              parent: GIC         parent: GIC
              parent_hwirq: 42    parent_hwirq: 43
```

当 GPIO 上的中断发生时：
1. GIC 看到 hwirq 42（连到 GPIO controller 的 SPI），投递给 CPU
2. GIC 的 handler 被调用，通过 GIC domain 翻译成 linux_irq
3. 该 linux_irq 的 handler 是 GPIO controller 驱动注册的"级联 handler"
4. 级联 handler 读取 GPIO controller 的状态，找到具体是哪个 GPIO（hwirq）
5. 通过 GPIO domain 翻译成另一个 linux_irq
6. 调用该 linux_irq 上注册的真正驱动 handler

### 核心结构体

| 结构体 | 文件 | 它抽象了什么 |
|--------|------|-------------|
| `irq_domain` | `include/linux/irqdomain.h` | 一个中断控制器的编号翻译层 |
| `irq_domain_ops` | `include/linux/irqdomain.h` | domain 的操作接口（map、translate、alloc 等） |
| `irq_fwspec` | `include/linux/irqdomain.h` | 中断的固件描述（来自设备树或 ACPI） |

### 主干路径：驱动注册中断

```
设备树中的中断描述：
  device@fe100000 {
      interrupts = <GIC_SPI 42 IRQ_TYPE_LEVEL_HIGH>;
      // 或者 interrupt-parent + interrupts 属性
  };

内核解析设备树，为设备分配 IRQ：
  of_irq_get()
    → of_parse_irq()                ← 解析设备树中的中断描述
      → of_irq_parse_raw()          ← 找到 interrupt-parent 和 interrupts 属性
    → irq_create_of_mapping()       ← 创建映射
      → irq_find_host()             ← 找到对应的 irq_domain
      → domain->ops->translate()    ← 把设备树描述翻译成 hwirq + trigger type
      → irq_create_mapping()        ← 分配 linux_irq 并建立 hwirq → linux_irq 映射
        → irq_domain_alloc_descs()  ← 分配一个空闲的 linux_irq 号
        → irq_domain_associate()    ← 在 domain 中建立映射关系
          → domain->ops->map()      ← 调用 domain 的 map 回调（GIC 驱动会在这里配置硬件）

驱动使用：
  request_irq(linux_irq, my_handler, flags, "my_device", dev_id)
    → request_threaded_irq()
      → irq_desc 找到对应的 desc
      → 创建 irqaction 并加入 desc->action 链表
      → irq_startup()               ← 启用该中断（unmask）
```

### 主干路径：中断到达时的翻译（反向）

```
硬件中断到达，GIC 驱动读出 hwirq
  → gic_handle_irq()
    → irq_find_mapping(domain, hwirq)    ← 在 domain 中查 hwirq → linux_irq
    → generic_handle_irq(linux_irq)
      → desc->handle_irq(desc)
        → handle_irq_event()
          → action->handler()            ← 调用驱动注册的 handler
```

如果是级联场景，上面 GIC domain 的 handler 不是最终驱动 handler，而是级联控制器的 handler，它会再走一轮翻译。

观测：`cat /sys/kernel/debug/irq/domains` 查看系统中的 irq_domain 层级。

---

## 阶段三：中断上下文的限制和下半部机制

**驱动问题：** 中断处理函数在网络中断里只把数据包从网卡拷到内存 — 剩下的协议栈处理为什么不在中断里做？

**核心认知：** 中断上下文是特权但不自由的执行环境。没有进程上下文、不能睡眠、不能调度。把工作分成"必须立即做"和"可以稍后做"两部分，是中断处理最核心的设计分界。

**阶段输出：** 能解释为什么需要 top-half/bottom-half 分离，能选择合适的 deferral 机制（softirq、tasklet、workqueue、threaded IRQ）。

### 为什么中断上下文有这些限制？

中断随时打断正在运行的进程。内核在中断处理期间：
- **没有进程上下文**：`current` 指向被中断的进程，但中断处理和它无关
- **不能睡眠**：因为无进程上下文，睡眠后无法被调度回来（没有对应的 task_struct）
- **关抢占（或部分关）**：中断处理期间不能被进程调度器抢占
- **关闭同级或更低级中断**（取决于硬件配置）

如果中断处理太慢，会导致：
- 其他中断被延迟（中断延迟）
- 系统看起来卡顿（用户进程得不到 CPU 时间）

### 四种 deferral 机制的演进

**为什么有四种？** 每种都是对前一种局限的修正：

1. **Bottom-half（BH，已废弃）**：最早的实现。全局一把大锁，同一时刻只能有一个 CPU 执行 bottom-half。SMP 时代成了瓶颈 → 废弃。

2. **Softirq**：取代 BH。per-CPU 执行，不同 CPU 可以并行。但接口不鼓励驱动直接用（只有网络、块设备等核心子系统用）。原因：softirq 在中断上下文执行，还是不能睡眠，且不能动态创建。

3. **Tasklet**：基于 softirq 实现的轻量封装。保证同一个 tasklet 不会在多个 CPU 上并行执行（不需要自己加锁）。但同样不能睡眠。适合简单的延迟工作。

4. **Workqueue**：完全在进程上下文执行。可以睡眠、可以调度、可以阻塞。本质是把工作交给一个内核线程去执行。代价是开销更大（需要线程调度）。

5. **Threaded IRQ**：把整个中断处理模型改成"hardirq 做最小工作 → 唤醒一个内核线程做剩下的"。是 workqueue 思想在 IRQ 层面的直接应用。`request_threaded_irq()` 一次注册两个 handler。

### 选择指南

| 场景 | 选择 | 原因 |
|------|------|------|
| 需要极低延迟，工作量很小 | 全在 hardirq 做 | 避免额外调度开销 |
| 网络收发协议栈 | softirq (NET_RX/TX) | 高频、per-CPU、不能睡眠但需要速度 |
| 简单延迟工作，不睡眠 | tasklet | 比 softirq 易用，有自动串行化 |
| 需要睡眠、拿 mutex、做 I/O | workqueue | 进程上下文，什么都能做 |
| 整个中断处理都比较慢 | threaded IRQ | 把 hardirq 降到最轻，主体在线程里 |

### 核心结构体

| 结构体 | 文件 | 它抽象了什么 |
|--------|------|-------------|
| `softirq_action` | `include/linux/interrupt.h` | 一个 softirq 的回调（全局静态数组） |
| `tasklet_struct` | `include/linux/interrupt.h` | 一个 tasklet 实例 |
| `work_struct` | `include/linux/workqueue.h` | 一个 work item |
| `irqaction` | `include/linux/interrupt.h` | 一个驱动注册的中断处理动作（handler + thread_fn） |

### 主干路径：softirq 执行

```
中断处理结束（irq_exit）
  → irq_exit()
    → 如果不在中断嵌套中且有 pending softirq：
      → __do_softirq()
        → 遍历 pending 位图
        → 对每个 pending 的 softirq：
          → softirq_action->action()    ← 执行回调
            → NET_RX: net_rx_action()
            → TASKLET: tasklet_action()
            → ...

如果 softirq 在 irq_exit 时没处理完，或者被频繁唤醒：
  → ksoftirqd 内核线程被唤醒
    → 在进程上下文执行剩余的 softirq（可以被调度器公平调度）
```

### 主干路径：threaded IRQ

```
硬件中断到达
  → hardirq handler（驱动的 primary handler）
    → 做最紧急的工作（如 ACK 硬件）
    → 唤醒内核线程（return IRQ_WAKE_THREAD）
  → 内核调度到 irq/<n>-<dev> 线程
    → 执行 thread_fn（驱动的 threaded handler）
      → 可以睡眠、拿锁、做耗时操作
    → 线程完成，被调度走
```

观测：`cat /proc/softirqs` 看 softirq 各类统计；`ps | grep ksoftirqd` 看 softirq 内核线程；`cat /proc/<pid>/comm | grep irq` 看 threaded IRQ 线程。

---

## 阶段四：中断优先级和饿死

**驱动问题：** 如果高优先级中断持续不断到来，低优先级中断会不会永远得不到处理？

**核心认知：** 优先级机制是一把双刃剑。它保证紧急事件优先处理，但在极端负载下会造成饿死。理解 GIC 的优先级仲裁逻辑，才能理解饿死的根本原因和可能的缓解手段。

**阶段输出：** 能解释 GIC 优先级的完整机制，能分析一个饿死场景的根因。

### GIC 优先级机制详解

GIC 的优先级是 **8-bit 无符号整数，数值越小优先级越高**（0 最高，255 最低）。涉及三个关键寄存器：

| 寄存器 | 作用 | 谁设置 |
|--------|------|--------|
| `GICD_IPRIORITYR` / `GICR_IPRIORITYR` | 每个中断的优先级值 | 驱动通过 `irq_set_priority()` 或默认值 |
| `ICC_PMR_EL1` (Priority Mask Register) | CPU 的优先级门槛，只接受 priority < PMR 的中断 | 内核在中断进入/退出时调整 |
| `ICC_BPR` (Binary Point Register) | 优先级分组（前 N bit 是 group priority） | 通常不用改 |

### 优先级仲裁过程

当多个中断同时 pending 时，GIC 做以下判断：

```
对每个 CPU：
  1. 从所有 pending 的中断中，选出 priority 值最小的（最高优先级）
  2. 检查该中断的 priority 是否 < 当前 CPU 的 PMR
     - 是：投递给该 CPU（CPU 读 IAR 时获得此中断）
     - 否：不投递，该 CPU 看不到这个中断
  3. CPU 在处理中断期间，其"运行优先级"(Running Priority) = 当前服务中断的 priority
     - 只有 priority < Running Priority 的新中断才能抢占
```

### 饿死是怎么发生的

场景：系统有两类中断在同一个 CPU 上竞争

```
高优先级中断 H：priority = 0xA0（如网络收包中断，每秒上万次）
低优先级中断 L：priority = 0xC0（如某个后台设备中断）

时间线：
  t0: CPU idle，Running Priority = 0xFF（空闲，最低）
  t1: H 到达，priority 0xA0 < PMR 且 < 0xFF → 投递，CPU 开始处理
  t2: CPU Running Priority = 0xA0
  t3: H 处理完，EOI，Running Priority 恢复
  t4: H 又到达（网络持续收包）→ 再次投递
  ...
  L 一直在 pending，priority 0xC0 > Running Priority 0xA0
  → L 永远得不到处理 → 饿死
```

**根本原因**：GIC 的优先级仲裁是**严格优先级调度**。只要高优先级中断的到达速率 ≥ 处理速率，低优先级就被完全压制。

### 饿死的缓解思路

| 策略 | 原理 | 局限 |
|------|------|------|
| **中断亲和性调整** | 把 H 分散到多个 CPU，降低单个 CPU 的中断密度 | 需要硬件支持多目标路由 |
| **调整优先级配置** | 缩小 H 和 L 的优先级差距 | 可能影响 H 的实时性 |
| **中断限流（rate limiting）** | 内核的 `IRQF_RATE_LIMIT` 或硬件限流 | 丢弃中断可能丢数据 |
| **NAPI（网络专用）** | 网络子系统用轮询替代中断，在高负载时自动切换 | 仅适用于网络场景 |
| **Threaded IRQ + RT 调度** | 把中断处理变成可调度的线程，用 CFS 保证公平 | 增加延迟，需要 PREEMPT_RT |

### Linux 内核中的优先级处理

Linux 在 ARM64/GICv3 上的中断优先级策略比较简单：

- **默认不使用 GIC 优先级做抢占**：内核通常在进入中断处理前设置 PMR 让所有中断都能进来（`GIC_PRIO_IRQON`），处理完后恢复。这意味着 Linux 的中断处理一般**不嵌套**（一个中断处理期间不会被打断进入另一个中断处理）
- **优先级主要用在 GIC 的路由仲裁中**：当多个中断同时 pending 时，GIC 选最高优先级的先投递
- **真正的"优先级"效果在 softirq/threaded IRQ 层面**：内核线程有 RT 优先级，调度器据此决定谁先运行

这个设计决策值得深思：**为什么 Linux 不用 GIC 的硬件优先级抢占？**
- 硬件中断嵌套使内核的锁和状态管理极其复杂
- 中断栈深度不可控，有栈溢出风险
- Linux 选择了"快速处理完当前中断，softirq 做剩下的"这条路

观测：`cat /proc/interrupts` 看各中断的计数分布；`watch -n1 "cat /proc/interrupts"` 实时观察中断频率。

---

## 阶段五：现代设备中断 — MSI/MSI-X 和 LPI/ITS

**驱动问题：** 一个支持 SR-IOV 的网卡可以有上百个虚拟功能，每个都需要独立中断。传统 SPI 只有 ~1000 个 ID，怎么够用？

**核心认知：** 传统中断是"一根线一个中断"。MSI 把中断变成了"写一个值到某个地址"的消息。这个转变让中断数量不再受物理引脚限制，也直接催生了 GICv3 的 LPI/ITS 机制。

**阶段输出：** 能描述 MSI/MSI-X 的工作原理，能追踪 GIC ITS 把一个 PCIe 设备的 MSI 翻译成 CPU 中断的完整路径。

### MSI（Message Signaled Interrupt）

传统中断：
```
设备 ---物理信号线---> 中断控制器 ---> CPU
```
限制：物理引脚数量有限，一个 PCI 插槽通常只有 4 根中断线（INTA-INTD），多个设备必须共享。

MSI 的工作方式：
```
设备通过 DMA 写一个 32/64-bit 值到一个特殊地址
  → 这个写操作被中断控制器截获
  → 控制器解析出中断信息
  → 投递给目标 CPU
```

MSI 写入的数据格式：
```
地址 = MSI Address（包含目标 CPU/Redirect hint 等信息）
数据 = MSI Data（包含中断 vector 等信息）
```

**MSI vs MSI-X：**

| 特性 | MSI | MSI-X |
|------|-----|-------|
| 最大中断数 | 32 | 2048 |
| 地址/数据表 | 一个（所有中断共享配置） | 每个中断独立（BAR 空间中的表） |
| 每个中断独立目标 CPU | 不支持（所有中断同目标） | 支持（每个中断独立 address） |
| 动态修改 | 困难 | 容易（写 table 即可） |

现代设备几乎都使用 MSI-X。

### GICv3 ITS：LPI 的翻译引擎

LPI 是 GICv3 为 MSI 场景设计的中断类型。ITS (Interrupt Translation Service) 是 LPI 的核心组件。

**ITS 解决的核心问题**：PCIe 设备发起 MSI 写时，它只知道自己要发中断（写一个 EventID 到 ITS 的翻译寄存器）。ITS 需要查表把这个 EventID 翻译成"哪个 CPU 上的哪个 LPI，优先级是多少"。

**ITS 的三级查找链：**

```
设备发起 MSI 写：
  写 GITS_TRANSLATER 寄存器（EventID 写入值）
  + 设备的 DeviceID（由系统分配，标识物理设备）

Level 1: Device Table（设备表）
  DeviceID → Interrupt Translation Table 的基地址
  （每个设备有自己的翻译表）

Level 2: Interrupt Translation Table（中断翻译表）
  EventID → { LPI INTID, Collection ID }
  （每个 EventID 对应一个 LPI 编号和一个集合）

Level 3: Collection Table（集合表）
  Collection ID → Target CPU（RDbase）
  （一个集合对应一个目标 CPU）
```

最终效果：`DeviceID + EventID → LPI INTID + 目标 CPU`

### LPI 的配置存在内存中

和 SPI/PPI 不同，LPI 的配置（priority + enable）不在 GIC 的硬件寄存器中，而是在**内存中的 LPI Configuration Table**：

```
每个 LPI 占 1 byte：
  bit [7]   : enable
  bit [6:0] : priority（数值越小优先级越高，和 GIC 一致）
```

**为什么放内存？** 因为 LPI 数量可能上万，放硬件寄存器面积和功耗都受不了。放内存的代价是每次查表多几次内存访问（latency 更高），但换来了几乎无限的可扩展性。这是一个经典的 **面积/功耗 vs 延迟** 的 tradeoff。

GIC 还有一个 **LPI Pending Table**，也是内存中的，记录哪些 LPI 正在 pending。这样中断状态在 power management 场景下可以容易地保存和恢复。

### 核心结构体

| 结构体 | 文件 | 它抽象了什么 |
|--------|------|-------------|
| `its_device` | `drivers/irqchip/irq-gic-v3-its.c` | ITS 中一个设备的上下文 |
| `its_collection` | 同上 | 一个 Collection（目标 CPU 的集合） |
| `msi_msg` | `include/linux/msi.h` | 一条 MSI 的地址+数据 |
| `msi_desc` | `include/linux/msi.h` | Linux 对一个 MSI 中断的描述 |

### 主干路径：PCIe 设备通过 ITS 发起中断

```
驱动侧（分配 MSI 中断）：
  pci_alloc_irq_vectors(dev, min, max, PCI_IRQ_MSIX)
    → pci_enable_msix_range()
      → 为每个 MSI-X entry 分配 irq
        → its_irq_domain_alloc()
          → 分配 LPI INTID
          → 创建 its_device（如果还没有）
          → 在 ITS 的 Device Table 中建立映射
          → 在 Interrupt Translation Table 中建立 EventID → LPI 映射
          → 在 Collection Table 中建立 Collection → Target CPU 映射
      → 返回 linux_irq 号数组

  request_irq(linux_irq, handler, ...)
    → 和阶段一/二一样的注册流程

设备侧（触发中断）：
  设备写 MSI-X table 中配置的 Address + Data
    → 写到达 GIC ITS
      → ITS 从写操作中提取 DeviceID 和 EventID
      → 查 Device Table → 找到该设备的 Translation Table
      → 查 Translation Table → 找到 LPI INTID + Collection ID
      → 查 Collection Table → 找到目标 CPU（Redistributor）
      → 将 LPI 标记为 pending，投递给目标 CPU

CPU 侧（处理 LPI）：
  → gic_handle_irq()
    → 读取 IAR 获得 INTID（8192+）
    → 判断是 LPI
    → 通过 irq_domain 查找 linux_irq
    → generic_handle_irq(linux_irq)
      → 调用驱动的 handler
    → EOI
```

### 和前面阶段的呼应

| LPI/ITS 概念 | 对应的传统概念 | 所在阶段 |
|-------------|--------------|---------|
| ITS 翻译链 | irq_domain 层级翻译 | 二 |
| LPI Configuration Table | GICD_IPRIORITYR 寄存器 | 一 |
| DeviceID + EventID | hwirq | 二 |
| MSI Address + Data | 物理中断线 | 一 |
| LPI 优先级 | GIC 中断优先级 | 四 |

观测：`ls /sys/kernel/debug/its/`（如果启用）；`cat /proc/interrupts` 中带 `[PCI-MSI]` 标记的行。

---

## 阶段六：中断亲和性和负载均衡

**驱动问题：** 多核系统里，所有中断都默认发到 CPU 0 — 怎么把中断分散开，避免一个 CPU 成为瓶颈？

**核心认知：** 中断亲和性（affinity）是操作系统控制中断路由的接口。负载均衡是上层策略。两者配合，让中断在多核系统上均匀分布。

**阶段输出：** 能解释中断亲和性的设置和传播路径，能分析一个 NUMA 系统中的中断分布是否合理。

### 亲和性（Affinity）

每个中断可以设置"允许投递的目标 CPU 集合"（cpumask）：

```
/sys/irq/<n>/smp_affinity    ← 用户态接口（cpumask 位图）
  → 写入后调用 irq_set_affinity()
    → irq_desc->irq_data.chip->irq_set_affinity()
      → 对于 GIC SPI：写 GICD_IROUTERn 寄存器（指定目标 CPU 的 affinity 值）
      → 对于 MSI-X：重新编程 MSI 的 Address 字段（改变目标 CPU）
      → 对于 LPI/ITS：更新 Collection Table 中的 Target CPU
```

### 中断负载均衡

**irqbalance 守护进程**：用户态程序，定期根据各 CPU 负载调整中断亲和性。策略包括：
- 把中断尽量绑定到离设备近的 NUMA 节点（减少延迟）
- 避免单个 CPU 上的中断密度过高
- 区分"性能优化"中断和"不关心延迟"中断

**内核侧的自动均衡**：

多队列设备（如现代网卡的 RSS）会自动把不同队列的中断绑定到不同 CPU：

```
网卡有 N 个收包队列
  → 每个队列有自己的 MSI-X 中断
  → 驱动初始化时把每个中断设到不同的 CPU
  → `irq_set_affinity_hint()` 告诉 irqbalance 不要打散
```

### 中断迁移

CPU 热插拔（offline）时，该 CPU 上的中断需要迁移：

```
cpu_hotplug: CPU N 即将 offline
  → irq_migrate_all_off_this_cpu()
    → 遍历该 CPU 上的所有 per-CPU 中断（PPI 等，无法迁移）和普通中断
    → 对每个可迁移的中断：
      → irq_set_affinity() → 选择一个新的目标 CPU
      → 对于 GIC SPI：写 GICD_IROUTERn 更新路由
      → 对于 LPI/ITS：更新 Collection Table
```

PPI 无法迁移（它天然绑定到特定 CPU），所以 PPI handler 需要处理 CPU offline 时的清理工作。

观测：`cat /proc/interrupts` 各列是每个 CPU 的计数，看分布是否均匀；`cat /sys/irq/<n>/smp_affinity` 看单个中断的亲和性设置。

---

## 学习节奏

| 时间 | 阶段 | 重点 | 核心文件 |
|------|------|------|---------|
| 第 1-2 周 | 一 | 中断从硬件到内核的全路径，GIC 基础 | `arch/arm64/kernel/entry.S`, `drivers/irqchip/irq-gic-v3.c` |
| 第 3 周 | 二 | irq_domain 映射，设备树到 `request_irq()` | `kernel/irq/irqdesc.c`, `kernel/irq/irqdomain.c` |
| 第 4 周 | 三 | softirq/tasklet/workqueue/threaded IRQ | `kernel/softirq.c`, `kernel/workqueue.c` |
| 第 5-6 周 | 四 | GIC 优先级，饿死分析 | `drivers/irqchip/irq-gic-v3.c`（PMR/priority 部分） |
| 第 7-8 周 | 五 | MSI/MSI-X, LPI, ITS 翻译链 | `drivers/irqchip/irq-gic-v3-its.c`, `drivers/pci/msi/` |
| 第 9 周 | 六 | 亲和性、负载均衡、中断迁移 | `kernel/irq/manage.c`, `kernel/irq/migration.c` |

---

## 观测工具速查

| 工具 | 用途 | 对应阶段 |
|------|------|---------|
| `cat /proc/interrupts` | 查看所有 IRQ 的 CPU 分布和计数 | 一 |
| `cat /proc/stat`（intr 行） | 中断统计 | 一 |
| `cat /sys/kernel/debug/irq/domains` | irq_domain 层级结构 | 二 |
| `cat /proc/softirqs` | softirq 各类统计 | 三 |
| `ps -eo pid,comm | grep irq` | threaded IRQ 内核线程 | 三 |
| `cat /proc/interrupts` + `watch` | 实时观察中断频率，判断饿死 | 四 |
| `ftrace -e irq:*` | 中断相关的 tracepoint | 四 |
| `perf stat -e irq:*` | 中断事件统计 | 四 |
| `ls /sys/kernel/debug/its/` | ITS 调试信息 | 五 |
| `lspci -vv`（MSI-X cap） | 设备的 MSI-X 能力和配置 | 五 |
| `cat /sys/irq/<n>/smp_affinity` | 单个中断的亲和性 | 六 |
| `irqbalance --debug` | irqbalance 的均衡策略 | 六 |

---

## 推荐阅读

| 资料 | 内容 | 适合阶段 |
|------|------|---------|
| ARM GICv3/GICv4 Architecture Specification | GIC 架构手册，优先级和 LPI 的权威参考 | 四、五 |
| ARM Cortex-A Series Programmer's Guide（异常处理章节） | ARM64 异常模型入门 | 一 |
| Linux kernel `Documentation/core-api/genericirq.rst` | Linux IRQ 框架设计文档 | 一、二 |
| Linux kernel `Documentation/core-api/irq/irq-affinity.rst` | 中断亲和性 | 六 |
| Linux kernel `Documentation/PCI/MSI-HOWTO.txt` | MSI/MSI-X 驱动接口 | 五 |
| `driver/irqchip/irq-gic-v3-its.c` 顶部注释 | ITS 设计和实现说明 | 五 |
