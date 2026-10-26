# VIP-SMT（MR !28466）Astra 审查报告

## 结论与固定范围

固定 HEAD：`00ba9aa3d7f58091cb87f8da96599d6d72023ea0`（OLK-6.6）。审查 merge `6694569b2d6b5125f532a642ce50af4a1f241e47` 相对第一父提交 `7c21a9ad39dbc7f0445034baa6a79f0e2173f0ce` 的全部 10 个文件，及 HEAD 上的 idle、CPU hotplug、sysfs 和跨 CPU 调用契约。四个引入提交见 `evidence/vip-smt-commits.txt`。

提出三个供开发人员确认的 P2 正确性候选：本地恢复与配置写入竞争、共享影子在 owner 热拔除后陈旧、共享配置跨 CPU 调用失败却返回成功。三个问题均由 `598068dd2e4be39707904ce9960f8be2543a1eaa` 引入，在固定 HEAD 未修复。它们影响配置正确性及 QoS 行为；没有证据将影响升级为任意代码执行、内存破坏或系统死锁。以下行号均对应固定 HEAD。

用户最新要求仅提供分析、不做测试；收到该要求后已停止新增测试和模型执行。以下为源码路径分析，最终由开发人员 review 是否真实成立。此前已执行一次状态模型，未在 HIP13 硬件上复现。寄存器共享属性按该实现自身的契约解释；不额外假设未公开的硬件故障模式。

## 待开发人员 review 的问题（建议同为 P2，静态证据置信度高）

### VIP-1：idle 恢复可覆盖刚刚成功的本地寄存器写入

**位置**：`arch/arm64/kernel/vip_smt.c:407-408`，并对照 `261-272` 的写回调。引入提交：`598068dd2e4b`。

**触发条件和时序**：在支持 VIP-SMT、已初始化的 CPU C 上，从 idle 返回，同时另一 CPU 通过 C 的 `regs/vip-smt/ifu_actlr1` 或 `ooo_dec_dsp_ctlr` 写字段。

1. C 的 `vip_smt_exit_idle()` 从 per-CPU 影子读取旧值 A，尚未执行系统寄存器写。
2. C 收到 sysfs store 发出的同步 IPI；写回调屏蔽本地 IRQ，把硬件及影子均更新为 B，返回后写者可以收到成功。
3. C 回到被打断的 idle 恢复点，把已经读入通用寄存器的 A 写回硬件；影子仍为 B。

不变量应为“成功配置写入后的硬件值等于最新影子”；当前代码允许硬件退回 A。应用后续运行期间可使用旧 QoS 参数，直到另一次 idle 恢复或配置操作才纠正。两个寄存器均存在该窗口。

**反证核验**：`kernel/sched/idle.c:245-252` 保证 `cpuidle_idle_call()` 返回前 IRQ 已开启；`345` 随后调用 `arch_cpu_idle_exit()`。poll 路径也在 `72` 开 IRQ。`arch/arm64/kernel/idle.c:95-110` 未为 VIP 恢复屏蔽 IRQ。idle task 不迁移、preemption disabled 不会屏蔽 IPI。写回调自身的 `local_irq_save()` 只保护回调，无法保护被中断的旧值读取。共享寄存器的 `restore_shared_register()` 在 `394-397` 已使用 IRQ 临界区，因此该问题限于两个本地寄存器。

**修复方向**：对本地影子读取至硬件写入使用同一个本地 IRQ 临界区；在随后同步调用远端 shared restore 前恢复 IRQ。不要把整个函数连同步 `smp_call_function_single()` 一起包在关 IRQ 区间。

**开发确认重点**：确认 idle exit 的 IRQ 状态允许该 IPI 插入，并核对编译后影子读取与系统寄存器写入之间是否存在该窗口；共享寄存器已经单独保护，问题范围是两个本地寄存器。

### VIP-2：base CPU 热拔除后，剩余 sibling 用陈旧影子恢复共享配置

**位置**：`arch/arm64/kernel/vip_smt.c:176-187`（动态 owner）、`268-271`（只更新 owner 的影子）、`395-396,410-412`（恢复）。引入提交：`598068dd2e4b`。

**可达步骤**：选一对可热拔除的 sibling，例如 CPU2/CPU3；避开不支持 offline 的 boot CPU。

1. 两 CPU 初始化时，各自的 `vip_smt_ooo_dec_rob_sha_ctlr` 影子均为 A。
2. 任一 sibling 的 sysfs 写共享字段，最终在 `cpumask_first({2,3}) == 2` 执行。硬件及 CPU2 影子变为 B，CPU3 影子仍为 A。
3. 完整 offline CPU2。`arch/arm64/kernel/smp.c:311` 调用 `remove_cpu_topology()`；`drivers/base/arch_topology.c:876-877` 将 CPU2 从 CPU3 的 thread_sibling mask 移除。
4. CPU3 下一次 idle exit 选择自身为 base，`restore_shared_register()` 将 CPU3 的 A 写回共享寄存器。即使没有掉电，旧配置也会被主动写回。

此问题无需竞争窗口：在 hotplug 完成后同样稳定可达；若随后在 CPU3 写另一个共享字段，基于陈旧影子的 read-modify-write 同样会撤销之前已配置的其他字段。

**反证核验**：单 sibling 存活不使 `vip_smt_core_has_smt()` 变假。`include/linux/arch_topology.h:109-112` 检查的是 `thread_id != -1`，不是 online sibling 数量；`clear_cpu_topology()`（`drivers/base/arch_topology.c:836-852`）不改变 thread_id；`cpu_smt_possible()` 也不是实时 online sibling 计数。CPU3 不经历重新 online，因此没有重新初始化其影子。没有 owner 切换回调或 shadow 迁移代码。现有拓扑代码会更新 base，故“始终向 offline CPU 发 IPI”并非这个顺序场景的正确解释。

**影响**：正常 CPU 热拔除撤销已成功应用的共享 QoS 配置，且旧影子可持续成为后续写操作的基准。

**修复方向**：将共享配置存放在稳定的 per-core 状态对象中，独立于当前执行 CPU；为 online owner 选择和 hotplug 建立一致的生命周期与同步协议。若保留 per-CPU 影子，必须在 owner 更换前转移权威值，不能简单改成“选择任一 online CPU”。

**开发确认重点**：确认目标 HIP13 的共享寄存器属性，以及 base offline 后剩余 sibling 的 thread_id、sibling mask 和影子值的生命周期；是否存在本树外的 owner 状态迁移保证。

### VIP-3：共享寄存器目标 CPU 下线竞态被报告为成功写入

**位置**：`arch/arm64/kernel/vip_smt.c:283-287,339-340`。引入提交：`598068dd2e4b`。

**触发时序**：用户写 CPU3 的共享属性；`vip_smt_find_base_cpu()` 已选出 CPU2；调用线程在进入 `smp_call_function_single()` 前被抢占；CPU2 完成 offline；线程恢复，向 CPU2 发同步调用。`generic_exec_single()` 对 offline 目标返回 `-ENXIO`，但 `vip_smt_write_*()` 返回 void 并丢弃错误，store 无条件返回 `count`。请求的字段根本没有应用。

**反证核验**：CPU3 属性的 kernfs active 引用只保证该属性不会在回调进行时被移除；CPU2 的 sysfs 删除不会等待 CPU3 的回调。`smp_call_function_single()` 内部 `get_cpu()` 在目标选择之后才执行，不能回溯保护选择窗口；它也没有替调用者重选 online owner。与 VIP-2 不同，本项即使未来统一了共享 shadow，只要忽略该错误，仍可发生。

**修复方向**：写 helper 返回并传播 IPI 错误；建立与 hotplug 相容的 owner 选择/重试协议。不能不加分析地在 sysfs active 回调内直接获取 `cpus_read_lock()`：offline 路径可能持 hotplug 写锁并等待当前 kernfs 操作 drain，须遵循现有 CPU sysfs 的锁序或使用合适的非阻塞/重试方案。

**开发确认重点**：确认写共享属性的 CPU 与被 hotplug 的 base 可以不同，且 sysfs/kernfs 没有跨这两个 CPU 提供额外的生命周期保护；核对错误返回传播和锁序。

## 待验证风险与非阻断观察

- HIP13 的三个实现寄存器编码、所有 MIDR revision 的支持情况、EL3 配置前置条件、是否要求 ISB、深 idle 后何时必须恢复，需要硬件一手资料和真机。没有以猜测形成硬件缺陷结论。
- capability 注册为 SYSTEM_FEATURE，但 matches 读取当前 CPU 的 MIDR/SMT 属性；对启动阶段已 online 的异构 CPU，是否所有 CPU 均兼容，不能仅凭当前 probe 证明。未提供实际混合 HIP13/非 HIP13 平台，保留为条件风险。late CPU 有 capability 验证机制，不可笼统断言全部热插入 CPU 无检查。
- 每次非 base sibling idle exit 都发同步 IPI 给 base，包括没有丢寄存器状态的浅 idle，可能增加唤醒与延迟。需功耗和尾延迟测量；未量化为确认性能回归。
- sysfs 组创建错误在 `cpuinfo.c:215` 未传播，且影子 init 已置位。分配失败可导致单 CPU 接口缺失；这是错误诊断/降级不足，没有证据升级成泄漏或崩溃。
- Kconfig 帮助写 `/sys/devices/system/cpu/regs/vip-smt`，启动参数文档写 `cpuX/regs/vip_smt`；实现实际为 `/sys/devices/system/cpu/cpuX/regs/vip-smt/`。属于文档路径错误，不与运行时 P2 问题混列。

## 实质改动覆盖与排除

| 改动 | 核查结果 |
|---|---|
| Kconfig、defconfig、Makefile | 默认启用且依赖 ARM64/SMP/ARCH_HISI；无硬件时有 runtime gates；未见配置关闭时入口未定义问题，idle header 有 stubs。 |
| cpucaps、cpufeature 接入 | 新 capability 消耗一个预留 slot；按文件顺序排序生成方式需以生成脚本为准，未据源码列表位置断言 ABI 损坏。MIDR、EL2、SMT 和命令行 gate 均有覆盖。 |
| novipsmt early parameter | `f69e962c24ef` 禁用接口和运行时恢复，参数 handler 返回 0；force 与普通禁用均无重新启用接口，不构成缺陷。 |
| idle 重构 | `dae917e25852` 保留 ACTLR 条件分支，测量钩子当前均为空；VIP 本地恢复的并发问题见 VIP-1。同步 shared IPI 在 IRQ enabled 的 task context，不把它误判为必然关中断死锁。 |
| CPU sysfs 初始化 | CPUHP_AP_ONLINE_DYN callback 通过目标 AP hotplug thread 执行（`kernel/cpu.c:1116-1146,2346-2348`），所以初始化中的 `__this_cpu_*` 并非在任意发起 CPU 上写错槽。 |
| sysfs 读路径及撤销 | `work_on_cpu()` 本身要求目标保持 online；这里读目标就是拥有该属性的 CPU，offline callback 删除 kobject 并 drain active 操作。不能仅因缺少显式 cpus_read_lock 就断言读会跑到 offline CPU。kobject_del 递归撤销子节点，缺少单独 vip group remove 不足以证明泄漏。 |
| 字段解析、mask、输出 | 检查等号、未知字段、kstrtoull、字段上界、单字段 RMW；ARM64 的 unsigned long 为 64 位，`__ffs` 对这些非零掩码有效。字段行和首行的现有固定字符串长度小于 64，整体远小于 PAGE_SIZE，没有确认 snprintf 越界。strim 未使用返回指针使前导空格不被接受，仅是输入宽容度。 |
| 写入序列化 | 稳定 owner 情况下，多 sysfs 写均在同一目标 CPU 的 IRQ 临界区执行，未确认普通并发字段更新相互丢失；hotplug 改 owner 是独立破坏条件。 |
| 生命周期 | initialization、idle、CPU offline/online、共享 owner、IPI 错误传播均追查；VIP-2/3 的源码路径成立，实际平台表现待开发确认。没有完整 suspend/hibernate 或 firmware 寄存器保持契约，不能作全面保证。 |

## 实际检查、产物与限制

只读分析包括固定 HEAD 校验、MR 10 文件差异/4 提交读取、merge 至 HEAD 的相关路径历史检查、寄存器标识与全部 VIP 调用者搜索，并逐行追踪 scheduler idle、AP hotplug、topology、kernfs drain、workqueue 与 SMP IPI 实现。`vip_smt.c` / `vip_smt.h` 自 merge 至固定 HEAD 无后续差异；未发现随后修复覆盖上述问题。

在收到“不需要做测试”的补充要求前，已执行 `python3 llm-archive/2026-10-23-arm64-review/evidence/vip-smt-model.py`，成功输出三个反例及插入窗口前后的对照，结果保存在 `evidence/vip-smt-model.txt`。模型带少量源码锚点断言，但它是显式抽象的执行时序证明，不是内核测试或硬件仿真。可用交叉编译器不等于具备 HIP13 运行环境；本轮未做内核全量构建、真机 hotplug、寄存器读写、LPI、KCSAN/lockdep 或性能测试。

未修改内核源码、共享工作树状态或提交记录。未根据模型声称已实测确认问题；开发人员应重点核对中断可插入窗口、thread_id 在热拔除后的保持语义，以及共享寄存器硬件属性。上述范围内的静态审查并不构成对全部硬件状态和组合配置安全性的保证。
