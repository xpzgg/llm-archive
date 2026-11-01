# ARM64 启动流程：阅读清单与笔记

> 配套图：[arm64-boot-bringup.excalidraw.md](arm64-boot-bringup.excalidraw.md)。读的时候对照图里对应的那一列。
>
> 内核侧以主线 v7.0 为准；旧文章里的函数名和顺序以现在的源码为准。

## 阅读顺序

建议按 **4 → 1 → 6 → 5 → 7** 读，其余按需翻。

先读入口协议（4）：它规定了交接点两边各自的责任，之后读固件（1）和内核（6）的材料都能拿它当参照。再看 UEFI 路径（5）和 ACPI（7），补齐另一条启动路径和另一种硬件描述。

| #   | 材料                        | 对应阶段                 | 状态  |
| --- | ------------------------- | -------------------- | --- |
| 1   | TF-A Firmware Design      | BL1 / BL2 / BL31     | 已读  |
| 2   | TF-A：QEMU virt Armv8-A    | 整条启动链（动手）            | 未读  |
| 3   | U-Boot doc/arch/arm64.rst | BL33：U-Boot          | 未读  |
| 4   | Booting AArch64 Linux     | 交接点：入口要求             | 已读  |
| 5   | The EFI Boot Stub         | BL33：UEFI → EFI stub | 未读  |
| 6   | 蜗窝科技《ARM64 的启动过程》         | head.S               | 未读  |
| 7   | ACPI on Arm systems       | 硬件描述：ACPI            | 未读  |
| 8   | Bootlin boot time 课件      | 慢环境 / 启动时间           | 未读  |

---

## 1. TF-A Firmware Design

- 链接：<https://trustedfirmware-a.readthedocs.io/en/latest/design/firmware-design.html>
- 讲什么：冷启动时 BL1 → BL2 → BL31 → BL33 各自负责什么、怎么把下一级的参数交接过去、主核和从核分别怎么走。
- 怎么读：篇幅很长，先读 "Cold boot" 那一章就够了。

### 笔记 / 问题

> 读这篇为了定界：知道每一级固件做什么、留下什么给内核。对话中整理，未逐条核对原文。标 ⚠ 的是第一遍理解不到位的地方。

#### 名词表

| 缩写 | 全称 | 是什么 |
|---|---|---|
| EL | Exception Level | 特权级。EL0 用户态，EL1 OS 内核，EL2 hypervisor，EL3 固件常驻部分 |
| — | 安全世界 / 非安全世界 | 与 EL 正交的另一维度。部分内存和外设可设成只允许安全态访问；Linux 在非安全世界，访问不到安全内存 |
| S-EL1 | Secure EL1 | 安全世界里的 EL1，特权与内核相当，但能访问安全内存 |
| NS | Non-Secure | `SCR_EL3` 里的一位，决定降级后进哪个世界。只有 EL3 能切换世界 |
| VBAR | Vector Base Address Register | 异常向量表基地址（后缀 `_ELx` 表示第 x 级的那一份） |
| ELR | Exception Link Register | 异常返回地址，ERET 跳到这里 |
| SPSR | Saved Program Status Register | 保存的 PSTATE，ERET 从这里恢复，包括目标 EL |
| PSTATE | Process State | 当前 EL、中断屏蔽位等 |
| ESR | Exception Syndrome Register | 异常原因 |
| SCR | Secure Configuration Register | `SCR_EL3`，EL3 配置两个世界的行为 |
| DAIF | Debug / SError / IRQ / FIQ | 四个中断屏蔽位 |
| SVC / HVC / SMC | Supervisor / Hypervisor / Secure Monitor Call | 分别调用 EL1 / EL2 / EL3，都是触发同步异常 |
| ERET | Exception Return | 异常返回，降低 EL 的唯一途径 |
| TF-A | Trusted Firmware-A | Arm 开源参考固件，A 指 A-profile 架构 |
| BL | Boot Loader stage | 启动链第几级；BL3 下分 BL31 / BL32 / BL33 三个镜像 |
| TEE / OP-TEE | Trusted Execution Environment / Open Portable TEE | 安全世界的运行环境 / 它的一种开源实现 |
| PSCI | Power State Coordination Interface | CPU 上下电、关机重启的固件接口 |
| SMCCC | SMC Calling Convention | SMC 调用的寄存器约定 |
| SRAM | Static RAM | 片内小块内存，上电即可用 |
| TZC | TrustZone Address Space Controller | 位于 CPU 和 DRAM 之间，按物理地址拒绝非安全态访问安全区 |

#### 各级固件

为什么分级：复位时只有 ROM 和片内 SRAM 能用；信任链要从 ROM 开始逐级校验；EL3 要有一份小代码常驻。

- **BL1**（EL3，ROM）：判断冷 / 热启动，加载并校验 BL2。
- **BL2**（S-EL1，SRAM）：初始化 DDR，加载并校验 BL31 / BL32 / BL33。
- **BL31**（EL3）：常驻，切换世界、提供 PSCI 等服务，最后进 BL33。
- **BL32**（S-EL1）：安全世界 OS（如 OP-TEE），可选，常驻。
- **BL33**（非安全 EL2）：UEFI / U-Boot，启动内核。

启动后常驻的只有 BL31、BL32：内核起来以后的固件问题，只可能出在它们身上。

- ⚠ 异常是低 EL 请求高 EL 的唯一途径（SVC / HVC / SMC），降级只能靠 ERET，所以叫"异常级"。
- ⚠ TF-A 不是 OS：BL31 只在 SMC 或中断到来时运行。和 Linux 对应的是 OP-TEE，BL31 夹在中间切换世界。

#### 交接给内核的状态

- 只有 EL3 能改的状态归 BL31（BL1 也在 EL3，但最后配置的是 BL31），其余归 BL33。
- BL31 要对每颗核都配一遍：只有从核异常 → 查热启动路径。

#### 常驻内存

- ⚠ 安全区要加 `no-map`。不加时页表里有映射，推测执行也会访问到它，被 TZC 拒绝 → 随机 SError；加了，MMU 翻译不出这个地址，访问根本发不出去。

#### 运行时服务

- 一次 SMC 只有三种结果：返回错误码 → BL31 拒绝；不返回 → 卡在 EL3；返回成功但结果不对 → 比如 CPU_ON 成功、从核却没进内核，查热启动路径。

## 2. TF-A：QEMU virt Armv8-A

- 链接：<https://trustedfirmware-a.readthedocs.io/en/latest/plat/qemu.html>
- 讲什么：一套能在 QEMU 上实际跑起来的完整启动链，可以从 BL1 一路跑到 Linux。
- 怎么读：动手用。顺带看 `ARM_LINUX_KERNEL_AS_BL33`：跳过 U-Boot，由 BL31 直接启动内核，DTB 放在 x0 里。

### 笔记 / 问题

-

## 3. U-Boot doc/arch/arm64.rst

- 链接：<https://git.pikami.org/random-mirrors/smaeul-u-boot/src/branch/a33/doc/arch/arm64.rst>（镜像，原文在 U-Boot 源码树同名路径）
- 讲什么：U-Boot 应该在哪个 EL 进入、怎么交接给 OS、从核怎么用 spin-table 唤醒。
- 怎么读：短，通读。

### 笔记 / 问题

-

## 4. Booting AArch64 Linux

- 链接：<https://docs.kernel.org/arch/arm64/booting.html>
- 讲什么：内核的入口协议，图里"交接点的入口要求"出自这里。
- 怎么读：短，建议整篇读完。读的时候想清楚：每一条要求如果没满足，内核会在哪个阶段、以什么现象挂掉。

### 笔记 / 问题

复习笔记（整篇概述 + 理解不到位的地方）：[arm64-booting-rst-review.md](arm64-booting-rst-review.md)

bootloader's duty:
- 初始化RAM
- setup device tree
- 解压Image
- call kernel image

为什么要配置dt，怎么配置dt?
用dt静态描述soc上无法枚举的组件？例如内存控制器，gic等

**固件占用的内存必须标成保留（高概率问题）**

- 规则：描述给内核的内存，只要没标成保留，内核就当作可以随便用，包括内核镜像下方的内存（老内核用不了下方，新内核会用）。
- 要标保留的：固件常驻的内存（如 BL31、OP-TEE），以及启动前就在用、内核起来后还要接着用的区域（如帧缓冲）。
- 怎么标：DT 的 `/memreserve/` 或 `reserved-memory` 节点；UEFI 启动时在 EFI 内存表里标成不可用类型。
- 漏标的后果：启动时不报错；内核某次把那块内存分出去用了，覆盖了原来的内容：
  - BL31 被覆盖 → PSCI 调用没响应：拉不起从核、关机重启失败；
  - OP-TEE 被覆盖 → 安全世界崩溃，依赖它的功能失效；
  - 帧缓冲被覆盖 → 屏幕花屏。
  - 共同特点：现象离原因很远，而且时好时坏（取决于那块内存什么时候被分出去）。
- 排查：核对固件实际占用的内存，是否都出现在 DT / EFI 内存表的保留区里（对应图里的 ⑤）。

## 5. The EFI Boot Stub

- 链接：<https://docs.kernel.org/admin-guide/efi-stub.html>
- 讲什么：内核镜像怎么让 UEFI 当成 EFI 应用加载，即 UEFI 路径的那一半。
- 怎么读：对照图里 BL33 模块的"路径 B"。

### 笔记 / 问题

-

## 6. 蜗窝科技《ARM64 的启动过程》系列（共六篇）

- 系列入口：<http://www.wowotech.net/tag/ARM64%E5%90%AF%E5%8A%A8%E8%BF%87%E7%A8%8B>
  1. [内核第一个脚印](https://www.wowotech.net/armv8a_arch/arm64_initialize_1.html)
  2. [创建启动阶段的页表](http://www.wowotech.net/armv8a_arch/create_page_tables.html)
  3. [为打开 MMU 而进行的 CPU 初始化](https://www.wowotech.net/armv8a_arch/__cpu_setup.html)
  4. [打开 MMU](https://www.wowotech.net/armv8a_arch/turn-on-mmu.html)
  5. [UEFI](https://www.wowotech.net/armv8a_arch/UEFI.html)
  6. [异常向量表的设定](https://www.wowotech.net/armv8a_arch/238.html)
- 讲什么：中文里讲 head.S 讲得最透的一组文章。
- 怎么读：**基于 4.1 内核**，函数名和执行顺序和现在的主线差别很大（比如页表现在是在 `__pi_early_map_kernel` 里建的）。读它是为了理解为什么要先建恒等映射、为什么开 MMU 必须这样做；具体代码以现在的源码为准。

### 笔记 / 问题

-

## 7. ACPI on Arm systems

- 链接：<https://docs.kernel.org/arch/arm64/arm-acpi.html>
- 讲什么：arm64 上为什么只能经 UEFI 传 ACPI、内核在 DT 和 ACPI 之间怎么选、哪些表是必需的。
- 怎么读：配套读同目录下的 `acpi_object_usage`，它逐张表说明内核用了表里的哪些字段。对照图里的"ACPI 表↔DT"模块。

### 笔记 / 问题

-

## 8. Bootlin：Embedded Linux boot time optimization 课件

- 链接：<https://bootlin.com/doc/training/boot-time/boot-time-slides.pdf>
- 讲什么：怎么测出时间花在哪、怎么裁 bootloader、内核和用户态。
- 怎么读：按需翻，对应图里"慢环境"面板。它面向嵌入式板子，用在仿真器 / FPGA 上，收益得自己实测。

### 笔记 / 问题

-
