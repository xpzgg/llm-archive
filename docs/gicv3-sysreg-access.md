# GICv3 系统寄存器在 Linux 内核中的访问方式（ARM64）

本文整理 Linux 内核中读取/写入关键 GICv3 系统寄存器和 DAIF 的方法，包括寄存器编码、内核访问器函数、底层指令及实际使用场景。

基于 ARM64 架构 + GICv3 中断控制器，源码路径基于 Linux 内核主分支。

---

## 1. 两类访问机制

GICv3 系统寄存器和 DAIF 使用不同的指令形式：

| 类型 | 指令 | 操作数形式 | 内核宏 | 适用寄存器 |
|------|------|-----------|--------|-----------|
| 编码访问 | `mrs_s` / `msr_s` | `sys_reg(op0, op1, crn, crm, op2)` 编码值 | `read_sysreg_s()` / `write_sysreg_s()` | 所有 ICC_* 寄存器 |
| 名称访问 | `mrs` / `msr` | 汇编器识别的寄存器名字符串 | `read_sysreg()` / `write_sysreg()` | DAIF 等 PSTATE 字段 |

GICv3 的 ICC_* 寄存器没有汇编器可见的名字，所以必须用编码形式访问。

---

## 2. ICC_HPPIR0_EL1 — 最高优先级待处理中断 (Group 0)

### 寄存器编码

```
sys_reg(3, 0, 12, 8, 2)
```

定义：`arch/arm64/include/asm/sysreg.h:373`

```
#define SYS_ICC_HPPIR0_EL1    sys_reg(3, 0, 12, 8, 2)
```

### 访问方式

无专用访问器函数。直接使用通用接口：

```c
u64 val = read_sysreg_s(SYS_ICC_HPPIR0_EL1);    // 读
write_sysreg_s(val, SYS_ICC_HPPIR0_EL1);         // 写（但此寄存器架构上只读）
```

展开为汇编：

```asm
mrs_s  %0, SYS_ICC_HPPIR0_EL1    // 读取 24-bit INTID
```

### 内核中的用途

**原生 GICv3 驱动不读 HPPIR0。** 驱动使用 ICC_IAR1_EL1（`gic_read_iar()`）来应答中断，IAR 返回的值与 HPPIR 类似但会隐式完成 ACK。

HPPIR0 仅在 **KVM 虚拟化** 中使用：

- `arch/arm64/kvm/hyp/vgic-v3-sr.c` — 拦截 guest 对 HPPIR0 的访问，通过扫描 List Registers 模拟返回值
- `arch/arm64/kvm/emulate-nested.c` — 嵌套虚拟化的 trap 配置
- `arch/arm64/kvm/sys_regs.c` — guest 直接访问时触发 `undef_access`，强制 trap 到 hypervisor

---

## 3. ICC_HPPIR1_EL1 — 最高优先级待处理中断 (Group 1)

### 寄存器编码

```
sys_reg(3, 0, 12, 12, 2)
```

定义：`arch/arm64/include/asm/sysreg.h:392`

```
#define SYS_ICC_HPPIR1_EL1    sys_reg(3, 0, 12, 12, 2)
```

### 访问方式

与 HPPIR0 对称，无专用访问器：

```c
u64 val = read_sysreg_s(SYS_ICC_HPPIR1_EL1);
```

### 内核中的用途

同 HPPIR0，仅在 KVM 中使用。HPPIR0 和 HPPIR1 在 KVM 中共用 `__vgic_v3_read_hppir()` 处理函数，区别在于 group 选择。

---

## 4. ICC_PMR_EL1 — 中断优先级掩码寄存器

### 寄存器编码

```
sys_reg(3, 0, 4, 6, 0)
```

定义：`arch/arm64/include/asm/sysreg.h:301`

```
#define SYS_ICC_PMR_EL1       sys_reg(3, 0, 4, 6, 0)
```

### 专用访问器

定义在 `arch/arm64/include/asm/arch_gicv3.h:128-136`：

```c
static inline u32 gic_read_pmr(void)
{
    return read_sysreg_s(SYS_ICC_PMR_EL1);
}

static __always_inline void gic_write_pmr(u32 val)
{
    write_sysreg_s(val, SYS_ICC_PMR_EL1);
}
```

展开为汇编：

```asm
mrs_s  %0, SYS_ICC_PMR_EL1     // 读
msr_s  SYS_ICC_PMR_EL1, %x0    // 写
```

### 屏障要求

写 PMR 后需要 `pmr_sync()`（定义在 `arch/arm64/include/asm/barrier.h`），确保中断控制器看到优先级变化：

```c
#define pmr_sync()                      \
    do {                                \
        asm volatile(                   \
        ALTERNATIVE_CB("dsb sy",        \
                       ARM64_HAS_GIC_PRIO_RELAXED_SYNC, \
                       alt_cb_patch_nops) \
        );                              \
    } while(0)
```

默认是 `dsb sy`；支持 relaxed sync 的 CPU 上会被 patch 成 NOP。

### 内核中的用途

**核心用途：pseudo-NMI 场景下替代 DAIF 做中断开关。**

当 `CONFIG_ARM64_PSEUDO_NMI` 启用时（通过 `system_uses_irq_prio_masking()` 判断）：

| 操作 | PMR 值 | 含义 |
|------|--------|------|
| 开中断 | `GIC_PRIO_IRQON` (0xe0) | 允许优先级 >= 0xe0 的中断 |
| 关中断 | `GIC_PRIO_IRQOFF` (0xc0) | 只允许优先级 < 0xc0 的中断（即 NMI） |

相关函数（`arch/arm64/include/asm/irqflags.h`）：

```c
// 开中断
static inline void __pmr_local_irq_enable(void)
{
    gic_write_pmr(GIC_PRIO_IRQON);
    pmr_sync();
}

// 关中断
static inline void __pmr_local_irq_disable(void)
{
    gic_write_pmr(GIC_PRIO_IRQOFF);
}

// 保存+关中断
static inline unsigned long __pmr_local_irq_save(void)
{
    unsigned long flags = gic_read_pmr();
    __pmr_local_irq_disable();
    return flags;
}

// 恢复
static inline void __pmr_local_irq_restore(unsigned long flags)
{
    gic_write_pmr(flags);
    pmr_sync();
}
```

---

## 5. ICC_RPR_EL1 — 运行优先级寄存器（只读）

### 寄存器编码

```
sys_reg(3, 0, 12, 11, 3)
```

定义：`arch/arm64/include/asm/sysreg.h:386`

```
#define SYS_ICC_RPR_EL1       sys_reg(3, 0, 12, 11, 3)
```

### 专用访问器

定义在 `arch/arm64/include/asm/arch_gicv3.h:138-141`：

```c
static inline u32 gic_read_rpr(void)
{
    return read_sysreg_s(SYS_ICC_RPR_EL1);
}
```

架构上只读，无 write 访问器。

### 内核中的用途

**唯一原生用途：NMI 检测**（`drivers/irqchip/irq-gic-v3.c:805`）：

```c
static bool gic_rpr_is_nmi_prio(void)
{
    if (!gic_supports_nmi())
        return false;
    return unlikely(gic_read_rpr() == GICV3_PRIO_NMI);  // 0x80
}
```

在 `__gic_handle_irq_from_irqson()` 中读取 IAR 后调用此函数，判断当前中断是普通 IRQ（运行优先级 0xc0）还是 pseudo-NMI（运行优先级 0x80），走不同的处理路径。

KVM 中也模拟了 RPR 的读取（`arch/arm64/kvm/hyp/vgic-v3-sr.c:1032`），从 List Registers 计算最高活跃优先级返回给 guest。

---

## 6. DAIF — PSTATE 异常掩码位

### 基本信息

DAIF 不是标准系统寄存器，没有 `sys_reg()` 编码。它是 PSTATE 的 bit[9:6]，通过汇编器识别的名字 `daif` 访问。

### 位定义

定义在 `arch/arm64/include/uapi/asm/ptrace.h:45-48`：

| 常量 | 值 | 位 | 含义 |
|------|----|----|------|
| `PSR_D_BIT` | 0x00000020 | 9 | Debug 异常屏蔽 |
| `PSR_A_BIT` | 0x00000010 | 8 | SError (异步异常) 屏蔽 |
| `PSR_I_BIT` | 0x00000008 | 7 | IRQ 屏蔽 |
| `PSR_F_BIT` | 0x00000004 | 6 | FIQ 屏蔽 |

常用掩码组合（`arch/arm64/include/asm/daifflags.h`）：

```c
#define DAIF_PROCCTX         0                              // 全开
#define DAIF_PROCCTX_NOIRQ   (PSR_I_BIT | PSR_F_BIT)       // 关 IRQ/FIQ
#define DAIF_ERRCTX          (PSR_A_BIT | PSR_I_BIT | PSR_F_BIT) // 关 SError/IRQ/FIQ
#define DAIF_MASK            (PSR_D_BIT | PSR_A_BIT | PSR_I_BIT | PSR_F_BIT) // 全关
```

### 三种访问方式

#### 方式一：完整读/写

用于保存和恢复场景：

```c
// 读 — 获取完整的 D/A/I/F 状态
unsigned long flags = read_sysreg(daif);     // mrs %0, daif

// 写 — 恢复之前保存的状态
write_sysreg(flags, daif);                   // msr daif, %x0
```

对应汇编：

```asm
mrs   x0, daif          // 读
msr   daif, x0          // 写
```

#### 方式二：只置位（屏蔽）

不影响其他位，只把指定位设为 1：

```c
asm volatile("msr daifset, #0xf");   // 屏蔽全部 D+A+I+F
asm volatile("msr daifset, #3");     // 只屏蔽 I+F（关中断）
```

立即数是 4-bit 掩码：bit3=D, bit2=A, bit1=I, bit0=F。`#3` = bit1+bit0 = I+F。

#### 方式三：只清位（开启）

不影响其他位，只把指定位清零：

```c
asm volatile("msr daifclr, #3");     // 开 I+F（开中断）
asm volatile("msr daifclr, #8");     // 只开 D（Debug）
```

### 内核中的典型用法

#### 汇编宏（`arch/arm64/include/asm/assembler.h`）

```asm
.macro disable_daif
    msr daifset, #0xf            // 屏蔽全部
.endm

.macro save_and_disable_daif, flags
    mrs  \flags, daif            // 保存
    msr  daifset, #0xf           // 屏蔽全部
.endm

.macro save_and_disable_irq, flags
    mrs  \flags, daif            // 保存
    msr  daifset, #3             // 只屏蔽 I+F
.endm

.macro restore_irq, flags
    msr  daif, \flags            // 恢复
.endm
```

#### C 层封装（`arch/arm64/include/asm/irqflags.h`）

```c
// 关中断（DAIF 方式）
static inline void __daif_local_irq_disable(void)
{
    asm volatile("msr daifset, #3");
}

// 开中断（DAIF 方式）
static inline void __daif_local_irq_enable(void)
{
    asm volatile("msr daifclr, #3");
}

// 保存中断状态
static inline unsigned long __daif_local_save_flags(void)
{
    return read_sysreg(daif);
}
```

#### GICv3 中断处理中开中断（`arch/arm64/include/asm/arch_gicv3.h:183`）

```c
static inline void gic_arch_enable_irqs(void)
{
    asm volatile("msr daifclr, #3" : : : "memory");
}
```

在中断处理函数 `__gic_handle_irq_from_irqson()` 中，NMI 处理完毕后调用此函数重新开启 IRQ/FIQ，再处理普通中断。

#### 内核入口（`arch/arm64/kernel/entry-common.c`）

```c
// 中断入口：立即屏蔽 I+F
write_sysreg(DAIF_PROCCTX_NOIRQ, daif);
```

---

## 7. 总结

| 寄存器 | 编码 / 形式 | 读方法 | 写方法 | 底层指令 | 内核中主要用途 |
|--------|------------|--------|--------|---------|--------------|
| ICC_HPPIR0_EL1 | `sys_reg(3,0,12,8,2)` | `read_sysreg_s()` | 只读 | `mrs_s` | KVM 虚拟化模拟 |
| ICC_HPPIR1_EL1 | `sys_reg(3,0,12,12,2)` | `read_sysreg_s()` | 只读 | `mrs_s` | KVM 虚拟化模拟 |
| ICC_PMR_EL1 | `sys_reg(3,0,4,6,0)` | `gic_read_pmr()` | `gic_write_pmr()` | `mrs_s`/`msr_s` | pseudo-NMI 中断开关 |
| ICC_RPR_EL1 | `sys_reg(3,0,12,11,3)` | `gic_read_rpr()` | 只读 | `mrs_s` | NMI vs IRQ 判断 |
| DAIF | PSTATE bit[9:6] | `read_sysreg(daif)` | `write_sysreg()` / `daifset` / `daifclr` | `mrs`/`msr` | 中断/异常屏蔽 |
