# 从 IRQ Line 到 GIC——中断控制器的演进

经典 PC 教材里讲的 "IRQ line" 和 ARM GIC 的 "INTID" 不是一回事。本文从中断线机制的演进角度，讲清 GIC 要解决什么问题、为什么 GIC 长成现在这个样子。

---

## 经典 "IRQ line" 的物理含义

经典 PC 教材里说的 "IRQ line"，特指**设备到中断控制器（PIC）之间的那根物理连线**，不是 PIC 到 CPU 的线。

8259A PIC 的设计本质是一个 **N 选 1 的汇聚器**：

```
Timer      ──┐ IRQ0
Keyboard   ──┤ IRQ1              ┌─────┐
Serial     ──┼─ IRQ2/3/...  ──→ │8259A│── INT ──→  CPU/INTR
RTC        ──┤ ...               │     │
...        ──┘ IRQ7              └─────┘
              (N 根 IRQ 线)               (只有一根汇总线)
```

设计 trade-off：
- **省 CPU 引脚**：CPU 只需 1 个 INTR + 1 个 INTA，而不是 N 个。早期 8086 引脚非常金贵
- **代价 1**：引入一次仲裁（PIC 内部 IRR + 优先级仲裁器）
- **代价 2**：IRQ 线数量被 PIC 引脚数硬限制（单 8259A 8 路，两级级联才到 15 路）

IRQ 编号在物理上就是 PIC 输入引脚的编号——这是个**硬件事实**。但 IRQ 编号 ≠ CPU 收到的向量号：向量号是 PIC 内部寄存器（ICW2）配置出来的，可以重映射。x86 实模式下 IRQ0~7 默认映到 INT 0x08~0x0F 会和 CPU 异常向量冲突，protected mode 启动时必须把 PIC 重定向到 0x20~0x2F。

**所有中断都同质**：8259A 输入端只有一种信号性质——外部设备拉高的电平/边沿信号。这是后续 SMP 时代最大的局限。

---

## 8259A 在 SMP 时代为什么不够用

| 问题 | 8259A 为什么做不到 | 是不是引脚数量问题 |
|------|------|------|
| 多 CPU 路由 | 只有一个 INT 输出，无法选目标 CPU | 不是。即使加到 1000 根 IRQ 输入，仍然不知道往哪个 CPU 发 |
| per-CPU 私有中断 | 没有 per-CPU 概念，所有 IRQ 全局共享 | 不是。需要 per-CPU 组件 |
| CPU 间通信（IPI） | 根本没有软件触发中断的机制 | 不是。需要新的信号路径（写寄存器） |
| PCIe MSI | 没有"消息接口" | 部分是。SPI 物理引脚扩展不动，但更本质的是寄存器配置空间撑不住 |

所以"引脚数量不够"只是表面问题，**根本驱动是中断控制器职责的彻底重新定义**：从"信号汇集器"演变为"中断路由 + 优先级仲裁 + 消息翻译"的复合体。

---

## ARM GIC 的角色

GIC 不是简单的一根线连到 CPU。它做四件事：

1. **接收（Ack）**：来自各种来源的中断（物理信号、软件触发、消息事务）
2. **路由（Route）**：决定这个中断发给哪个 CPU（亲和性）
3. **优先级仲裁（Priority Arbitration）**：多个中断同时 pending 时选最高优先级投递
4. **虚拟化支持**：给 hypervisor 注入虚拟中断的能力

**关键澄清**：所有中断都必须经过 GIC，没有"绕过 GIC"的路径。ARM CPU 接收中断的唯一硬件入口是 IRQ/FIQ 异常，而 CPU 的 `nIRQ` 信号就是 GIC 的 CPU Interface 发出来的。CPU 本身没有"直接接外设中断"的引脚。

---

## GICv3 的中断类型分类（按"信号源性质"）

| 类型 | ID 范围 | 来源 | 有无线 | 路由方式 | 典型例子 |
|------|---------|------|--------|----------|---------|
| SGI (Software Generated Interrupt) | 0–15 | 软件写 `GICD_SGIR`/`GICR_SGIR` | 无 | 指定目标 CPU | IPI（CPU 间通信） |
| PPI (Private Peripheral Interrupt) | 16–31 | CPU 本地设备 | 有（per-CPU 独立） | 固定到当前 CPU | arch timer, PMU |
| SPI (Shared Peripheral Interrupt) | 32–1019 | 外部共享设备 | 有（共享） | 可配置目标 CPU | 网卡、磁盘控制器 |
| LPI (Locality-specific Peripheral Interrupt) | 8192+ | 基于 MSI 的设备 | 无 | 通过 ITS 查表路由 | PCIe 设备 |

**核心认知**：GIC 里 INTID 编号范围划分的不是"哪根线"，而是"这个中断的物理来源是哪一类信号"。只有 SPI 和 PPI 才对应传统意义的"物理中断线"，SGI 和 LPI 根本无线可言。

**为什么需要这四种？** 每种的引入都解决了前一种的局限：
- **SGI**：CPU 之间需要通信（如 TLB flush IPI），不能用外部中断线 → 软件触发
- **PPI**：每个 CPU 有自己的 timer、PMU，天然私有，不需要路由 → 固定绑定
- **SPI**：外部设备的中断需要能被任何 CPU 处理 → 可配置路由
- **LPI**：现代 SoC 有大量 PCIe 设备，SPI 的 ~1000 个 ID 不够用，且配置在寄存器中扩展成本高 → 配置放内存，ID 空间扩展到数万

**SoC 上的 SPI 设备都在芯片内部**：UART、网卡 MAC、USB 控制器、磁盘控制器等在现代 ARM SoC 里都集成在一颗芯片上（这就是 SoC 名字的由来）。SPI 中断线是**芯片内部的金属走线**，不是芯片封装外面的引脚。设备树里写 `interrupts = <GIC_SPI 42 ...>` 的 42 是 SoC 设计阶段硬连到 GIC 的第 42 号 SPI 输入引脚，流片后改不了。

---

## GICv3 的内部组件结构

GICv3 把"中断控制器"拆成几个功能子组件：

| 组件 | 缩写 | 数量 | 职责 |
|------|------|------|------|
| Distributor | GICD | 全局唯一 | 接收 SPI，做优先级仲裁和路由决策 |
| Redistributor | GICR | 每 CPU 一个 | 管这个 CPU 私有的 PPI/SGI/LPI 状态 |
| CPU Interface | ICC | 每 CPU 一个 | CPU 通过系统寄存器（`ICC_*_EL1`）和 GIC 交互的接口（读 IAR、写 EOIR、设 PMR） |
| ITS | GITS | 可选，0 或多个 | 翻译 LPI（PCIe MSI） |

```
┌──────────────────────────── GIC（一整个中断控制器）────────────────────────────┐
│                                                                                │
│                          ┌─────────────────┐                                   │
SPI 输入引脚 ─────────────→ │   Distributor   │  (全局唯一，处理 SPI)              │
（来自 SoC 内部的 UART、    │   路由仲裁器     │                                   │
 网卡、USB 等）             └────────┬────────┘                                   │
                                    │                                            │
                                    │ Distributor 决定：这个 SPI 路由到 CPU0      │
                                    ↓                                            │
┌─────────────── Redistributor0 ─────────┐  ┌── Redistributor1 ─────────┐       │
│                                        │  │                            │       │
│ PPI 输入引脚（CPU0 私有）              │  │ PPI 输入引脚（CPU1 私有） │       │
│   ← arch timer 0  (PPI 30)             │  │   ← arch timer 1 (PPI 30)  │       │
│   ← PMU 0         (PPI 23)             │  │   ← PMU 1        (PPI 23)  │       │
│                                        │  │                            │       │
│  存这个 CPU 的 PPI/SGI/LPI 状态        │  │  存这个 CPU 的 PPI/SGI/LPI │       │
│  + 从 Distributor 接收路由过来的 SPI    │  │  + 从 Distributor 接收 SPI │       │
└────────────────┬───────────────────────┘  └─────────────┬──────────────┘       │
                 │                                          │                    │
                 ↓                                          ↓                    │
        ┌── CPU Interface0 ──┐                    ┌── CPU Interface1 ──┐          │
        │  ICC_*_EL1 寄存器   │                    │  ICC_*_EL1 寄存器   │          │
        │  (IAR/EOIR/PMR...) │                    │  (IAR/EOIR/PMR...) │          │
        └─────────┬──────────┘                    └─────────┬──────────┘          │
└─────────────────┼─────────────────────────────────────────┼────────────────────┘
                  │ nIRQ 输出                                │ nIRQ 输出
                  ↓                                          ↓
              ┌──────┐                                  ┌──────┐
              │ CPU0 │                                  │ CPU1 │
              └──────┘                                  └──────┘
```

**Redistributor 存在的原因（核心 trade-off）**：
- **核心矛盾**：PPI/SGI 是 per-CPU 的，但 Distributor 是全局唯一的
- 如果只用 Distributor 存 PPI 状态：CPU0 的 PPI 30 和 CPU1 的 PPI 30 是两根完全独立的物理线，状态必须分开存；全局状态空间会爆炸；每次访问本 CPU 的 PPI 都要走全局总线
- **GICv3 的解决方案**：把 per-CPU 的状态从 Distributor 剥离出来，做成 per-CPU 的 Redistributor
- **收益**：支持上千 CPU 的可扩展性
- **代价**：组件复杂度爆炸，GICv3 的 MMIO 区域里有一个 GICD + N 套 GICR + N 套 GICC

---

## 设计哲学："全局共享 + per-CPU 私有"

GICv3 的 Distributor + Redistributor 拆分是这个模式的经典案例，但绝不是唯一：

| 系统 | 全局共享部分 | per-CPU 私有部分 |
|------|------|------|
| 中断控制器 | Distributor（SPI 路由） | Redistributor + CPU Interface（私有中断） |
| 调度器 | 全局优先级 / 负载信息 | runqueue（每 CPU 自己的队列） |
| 内存管理 | 全局页表 | per-CPU TLB / cache |
| GIC ITS | ITS 翻译表（全局） | LPI Pending Table（每 CPU） |

**核心思想**：全局状态做最小化（只共享必须共享的），per-CPU 状态尽量多（队列、cache、私有中断状态），减少跨 CPU 同步开销。

---

## 全行业趋势：从中断线到中断消息

GIC 的演进路径是整个行业的共同选择：

| 架构 | "线"时代 | "线 + 消息"时代 |
|------|------|------|
| x86 | 8259A PIC | IO-APIC（线）+ LAPIC（IPI）+ MSI |
| ARM | 早期 GIC（只有 SPI/PPI/SGI） | GICv3 ITS（LPI/MSI） |
| RISC-V | PLIC | AIA（IMSIC，纯消息） |
| LoongArch | HTVEC/LIOINTC | 扩展消息中断 |

SMP + PCIe + 虚拟化这三股力量共同推动了中断控制器从"信号汇集器"演进成"中断路由 + 优先级仲裁 + 消息翻译"的复合体。**引脚数量只是这个演进过程中最不重要的瓶颈之一**。

---

## 记忆口诀

- x86 的 IRQ number 是"线号"（8259A 时代）
- ARM GIC 的 INTID 是"信号源类型 + 该类型下的编号"
- 看 INTID 先看范围，再读具体编号
