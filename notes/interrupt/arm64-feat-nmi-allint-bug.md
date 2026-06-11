# PR #23676: irqchip/gic-v3: Fix ALLINT masking logic by decoupling from GIC NMI support

- PR: https://atomgit.com/openeuler/kernel/pulls/23676
- Commit: `884df83ea300` (merge: `f576f7127ee6`)
- 分支: OLK-6.6
- 作者: Jinjie Ruan (ruanjinjie@huawei.com)
- 关联 issue: https://atomgit.com/openeuler/kernel/issues/9343

## 问题是什么

修改 `drivers/irqchip/irq-gic-v3.c` 中 `gic_handle_irq_noack()` 函数，将 ALLINT 清除逻辑的守卫条件从 `has_v3_3_nmi()` 改为 `system_uses_nmi()`。

```c
// gic_handle_irq_noack() 中
-} else if (has_v3_3_nmi()) {
+} else if (system_uses_nmi()) {
 #ifdef CONFIG_ARM64_NMI
     _allint_clear();
 #endif
 }
```

同时在 `gic_init_bases()` 中新增诊断日志：

```c
gic_data.has_nmi = !!(typer & GICD_TYPER_NMI);
+pr_info("GICD_TYPER NMI is%s supported.\n", gic_data.has_nmi ? "" : " not");
```

## 问题成因

ARM64 的 FEAT_NMI 实现涉及两条独立的硬件验证路径：

| 路径 | 检测方式 | 含义 |
|------|---------|------|
| **PE 端** | `system_uses_nmi()` → 读 `ID_AA64PFR1_EL1.NMI` | CPU 支持 NMI 特性，启用 `SCTLR_EL1.NMI` 后，异常入口硬件自动设置 `PSTATE.ALLINT` |
| **GIC 端** | `gic_data.has_nmi` → 读 `GICD_TYPER.NMI` | 中断控制器支持 FEAT_GICv3_NMI |

关键设计约束：**`PSTATE.ALLINT` 是 PE 级寄存器，它的存在和行为完全独立于 GIC 是否支持 NMI**。只要 PE 支持 FEAT_NMI 且内核启用了 `SCTLR_EL1.NMI`，硬件在每次异常入口到 EL1/EL2 时就会**自动设置** `PSTATE.ALLINT = 1`，将 IRQ 和 NMI 全部屏蔽。

但原来的代码用 `has_v3_3_nmi()` 来守卫 ALLINT 的清除：

```c
static inline bool has_v3_3_nmi(void)
{
    return gic_data.has_nmi && system_uses_nmi();  // 要求 GIC 和 PE 同时支持
}
```

当平台 **PE 支持 FEAT_NMI 但 GIC 不支持 NMI** 时：
1. `system_uses_nmi()` = true → 内核已启用 `SCTLR_EL1.NMI`
2. `has_v3_3_nmi()` = false（因为 `gic_data.has_nmi = false`）
3. 结果：`_allint_clear()` 永远不被调用，ALLINT 始终为 1

后果：`local_irq_enable()` 只清除 `daifclr`（PSTATE.I/F），**不影响 ALLINT**。ALLINT 屏蔽所有 IRQ 和 NMI，导致 softirq 执行期间系统完全无法响应中断 → watchdog NMI 无法进入 → hard LOCKUP。

```
handle_softirqs()
   -> local_irq_enable()        // 只清除 PSTATE.I/F，ALLINT 仍为 1
       -> asm volatile("msr daifclr, #3")   // 对 ALLINT 无效
   -> do pending softirq action
   -> local_irq_disable()
```

## 根因：设置 ALLINT 的条件 vs 清除 ALLINT 的条件不一致

```
设置（硬件自动）：SCTLR_EL1.NMI = 1  →  只要 system_uses_nmi() = true，异常入口无条件设置
清除（软件主动）：has_v3_3_nmi()     →  要求 system_uses_nmi() && gic_data.has_nmi 同时为 true
```

当 PE 有 FEAT_NMI、GIC 没有 NMI 时：

1. 异常入口 → 硬件看到 `SCTLR_EL1.NMI = 1` → 自动 `ALLINT = 1`
2. 进入 `gic_handle_irq_noack()` → `has_v3_3_nmi()` = false（GIC 不支持） → **跳过** `_allint_clear()`
3. ALLINT 泄漏，永远不被清除
4. 后续 `local_irq_enable()` 只操作 DAIF，ALLINT 仍然为 1 → 所有中断被屏蔽 → hard LOCKUP

修复就是让清除也只看 PE，用 `system_uses_nmi()` 替换 `has_v3_3_nmi()`。

## 为什么硬件要在异常入口自动设置 ALLINT = 1

异常入口（exception entry）时，处理器正在做上下文切换：保存寄存器、切换栈、建立 `pt_regs`、设置内核上下文……这个过程本身**不是一个可以安全处理嵌套异常的状态**。硬件必须保证：在软件把上下文收拾好之前，不能再被打断。

没有 FEAT_NMI 时，硬件在异常入口自动设置 `PSTATE.I` 和 `PSTATE.F`（DAIF 中的 I/F 位），屏蔽 IRQ 和 FIQ。软件完成上下文建立后，再主动清除这些位来开中断。

但 NMI 的本质就是**不可屏蔽**——它能绕过 DAIF。一旦 CPU 支持 FEAT_NMI，传统的 DAIF 屏蔽就漏了一个口子：异常入口的临界窗口内，NMI 仍然可以闯进来。如果这时候栈还没切换完、`pt_regs` 还没建好，NMI handler 看到的是一个半成品的上下文，直接崩溃。

所以 ARM 引入 `PSTATE.ALLINT`——唯一能屏蔽 NMI 的 PE 级机制。当 `SCTLR_EL1.NMI = 1` 时，硬件把异常入口的屏蔽机制从 DAIF 升级为 ALLINT：

| | DAIF | ALLINT |
|---|---|---|
| 屏蔽 IRQ | I bit | ALLINT |
| 屏蔽 NMI | **不能** | ALLINT |
| 作用范围 | 普通 FIQ/IRQ/SError | **所有中断**，包括 NMI |

tradeoff：牺牲 NMI 响应有一个不可消除的盲区（entry window），换取异常入口的原子性保证。没有这个保证，所有 entry code 都得写成 NMI-safe 的，几乎不可能做到——不能在栈还没切换好时就处理嵌套异常。本质上和 CPU 流水线的精确中断（precise exception）是同一个思路：**中断/异常只能在一个定义好的、安全的边界上被响应**，不能在任意中间状态响应。

## 影响分析

1. **PR 已合入**：commit `884df83ea300` 通过 merge commit `f576f7127ee6` 合入 OLK-6.6，时间 2026-06-05。

2. **同源遗留问题**：`__gic_handle_irq_from_irqson()`（line 939）仍然使用 `has_v3_3_nmi()` 来守卫 ALLINT 清除，和 `gic_handle_irq_noack()` 之前的问题完全一样。PR #23676 只修了 `gic_handle_irq_noack()`（由 SMT QoS 特性 `d9526b277b6f` 引入），没有修原路径。在相同的硬件配置（PE 有 NMI、GIC 无 NMI）下，普通 IRQ 处理路径仍然存在同样的 hard LOCKUP 风险。

3. **受影响的硬件配置**：PE 支持 FEAT_NMI（`ID_AA64PFR1_EL1.NMI = 1`）但 GICv3 不支持 NMI（`GICD_TYPER.NMI = 0`）的平台。

## 相关 commits（时间线）

- `d4b035989a09` arm64/nmi: Add handling of superpriority interrupts as NMIs — FEAT_NMI 基础支持
- `3bd55c0cc9c2` irqchip/gic-v3: Implement FEAT_GICv3_NMI support — GIC 侧 NMI 支持
- `eefea6156921` irqchip/gic-v3: Fix hard LOCKUP caused by NMI being masked — 引入 `has_v3_3_nmi()` + `_allint_clear()`（本 PR 的 Fixes: 目标）
- `209fd542209d` irqchip/gic-v3: Fix one race condition due to NMI withdraw — NMI 撤回竞态修复
- `d9526b277b6f` arm64: Add arch code for SMT QoS — 引入 `gic_handle_irq_noack()`，复制了同样的 `has_v3_3_nmi()` 模式
- `884df83ea300` irqchip/gic-v3: Fix ALLINT masking logic by decoupling from GIC NMI support — **本 PR**，修 `gic_handle_irq_noack()`

