# TLBID / vTLBID Astra 静态审查

## 范围与结论

固定 HEAD `00ba9aa3d7f58091cb87f8da96599d6d72023ea0`，主范围为 MR !29666、merge `22c0d87189d873d55cc3c13a1c09ea3c12fa50cf` 相对第一父提交 `b7bdf19283f966c5931dfd4d02815a1b7a0eefae` 的 23 文件。关联检查 MR !29736、merge `7c3326bea85b9144fa44cca6b7d5ea9f772e1c03` 的 12 文件；该 MR 的问题独立归因，不计作 !29666 引入。引入提交列表、完整 diff 已在 evidence 中。

主范围提出 5 项候选，关联 vTLBID 提出 2 项高置信 C 代码候选，另列架构/配置条件尚需确认的风险。优先级是建议处理顺序，置信度仅指静态推理，均请开发人员 review 是否真实成立。本轮遵循用户要求，只读取代码、提交历史和已有资料；没有运行构建、测试、仿真、模型或复现脚本，没有修改源码。

## 一、!29666 引入的候选

### TLB-1 / P1：domain 个数与 domain ID 混用，ACPI 可触发越界写

**静态置信度：高。位置**：`arch/arm64/mm/tlbid.c:190-201,275-286,297-300`。引入 `2af4c40a55615339ab539794ec87b2d44da432e9`。

`add_target()` 将不同正 ID 的数量计入 `nr_acpi_domain`（外加预留 domain0），而建表只分配该数量的元素，再用原始 `target->domains[i]` 作数组下标。假设 NIS/NOS 足够容纳 ID7，固件报告的域集合为 `{0,7}`，数量是 2，访问的却是 `[7]`；即使表完全处于 ACPI 映射范围内，仍会越过分配对象写 cpumask。另一路是 `domains` 使用 s32、检查仅有 `id >= max_id`，负值可以通过，随后作为负下标。负值示例须同时有至少一个合法正域，使 `nr_acpi_domain > 1`，否则建表开头的保护会退出。

**影响**：启动时内核堆内存破坏或异常。触发输入来自 firmware/ACPI，不据此声称普通用户可直接利用。即使规范要求 ID 连续或非负，当前解析器也未在写内存前验证这个不变量。

**反证**：`max_id` 检查的是硬件位宽上界，不是数组长度；bitmap 计数未建立 ID 到密集索引的转换；`find_target()` 仅按 UID 找目标；写入后的空域检查来不及阻止越界。未见后续修复。

**修复方向**：使用明确的 ID 映射或按最大 ID+1 分配并处理空洞；独立验证非负及硬件范围。在任何 raw ID 索引前检查边界。开发需确认 ACPI ID 的连续性要求及负值/保留值语义，不能将表的合法性假定当成内存安全检查。

### TLB-2 / P1：16 位域数截断成零，bitmap 分配与访问范围不一致

**静态置信度：高。位置**：`arch/arm64/mm/tlbid.c:88,120-124,190-200`。同由 `2af4c40a5561` 引入。

`max_id` 是 u16，`1 << 16` 赋值后成为 0。代码的 `sanitised_ids()` 明确接受 NIS/NOS 的 `9 ... 16`，sysreg 描述中两者都是 5 位字段，因此这是实现声称支持的边界，不是仅依靠非法字段值才会发生。零长度 `kcalloc()` 可返回非 NULL 的 ZERO_SIZE_PTR；`if (!acpi_domain)` 不会拦截。之后 `add_target()` 用 s32 重新计算 65536，正 ID 如 1 被接受并执行 `test_and_set_bit()`，访问无有效存储的 bitmap。

**影响**：有非零 domain 条目的该类平台在初始化期间异常。即使分配器返回 NULL，arch_initcall 返回错误也未使 ACPI 初始化 gate 失效，错误状态仍须处理。

**反证**：正常 NIS/NOS<16 不触发本项；问题不依赖稀疏 ID，连续 `{0,1}` 即可。`system_supports_tlbid()` 仅检查 NIS/NOS>0，不排除 16。KUnit 的伪 bitmap 单独按 `1 << 16` 的 int 分配，没有覆盖实际 u16 分配路径。

**修复方向及开发确认**：用能表达 65536 的类型统一容量计算；验证字段范围并使用 bitmap 分配接口；初始化失败后阻止 ACPI 建表继续。请确认目标硬件是否报告 16，以及错误 fallback 的预期行为。

### TLB-3 / P1：新 ACPI parser 未验证完整结构与间接 payload 边界

**静态置信度：高。位置**：`drivers/acpi/arm64/tlbid.c:28-52`；通用入口 `drivers/acpi/tables.c:404-415`。引入 `2af4c40a5561`。

handler 仅检查 header 非 NULL，就读取 identifier/nr_domain/offset 并从 `table + offset` 复制 `nr_domain * sizeof(s32)` 字节；完全未使用传入的 end。通用迭代器此处只检查 `acpi_tlbi_header` 能否放入表内，不保证完整 `acpi_tlbi_subtable`（20 字节）存在，也不校验间接域列表。于是一个固定头可读、完整 body 被截断的条目，或一个 body 完整但 offset 指向表外的条目，都能到达越界读。

**影响**：firmware 表损坏时越界读取、启动异常，或将表外字节误当 ID 进一步进入 TLB-1。不将 checksum 视为结构合法性证明，也不将受控固件输入夸大为远程攻击面。

**反证**：`kcalloc()` 的乘法检查只保护目的分配大小；没有验证源 payload 的映射范围。通用迭代器在调用 handler 之后才取 entry length 推进，不能补救 handler 内访问。ACPI 表映射覆盖的是表宣称长度，不会为任意 offset 提供合法性保证。

**修复方向及开发确认**：先确认完整 fixed body 与条目长度，再以不会溢出的减法校验 offset、count、element size 和 table end；明确 offset 相对基址及 payload 是否允许在条目之外。需要开发对照实际 TLBI 表规范确认格式；本项的内存边界缺口不依赖 ID 连续性。

### TLB-4 / P1：16K/64K TLBIP 地址按 PAGE_SHIFT 编码，与字段契约不符

**静态置信度：中高；最终架构判定需一手规范复核。位置**：`arch/arm64/include/asm/tlbflush.h:454-463,505-507`，使用点 `__flush_tlb_range_domain_op()` 及 `__flush_tlb_page_nosync()`。helper 引入 `e28c4375a213dc04638bfd082fa8b51b25f19970`，page 使用由 `a43893f89baa70c06e94dcc2d2636bb8c5901eb0` 引入。

文件明确写 TLBIP 高操作数为 range 的 BaseADDR[55:12]、non-range 的 VA[55:12]，实现却统一 `addr >> PAGE_SHIFT`。在 PAGE_SHIFT=14/16 时比该契约多右移 2/4 位。例如 64K 配置下 VA 0x10000 被编码为 1，而按 VA[55:12] 应为 0x10。mm 在远端 CPU 上有执行历史时选 DOMAIN 分支，即使 fallback domain=0 也仍发 TLBIP，未要求找到非零优化域。

**影响（以该字段契约成立为前提）**：失效错误地址、目标地址保留旧 TLB 映射；这是页表更新正确性问题，而非仅性能差异。4K 配置不受本项影响。

**反证**：传统 `__TLBI_VADDR` 在 `61` 固定 >>12；传统单操作数 TLBI range 的 TG/压缩格式不能自动套用到 TLBIP 双操作数。本项对 range 和 non-range 分别依据文件所列字段，没有仅从旧 TLBI range 推导新编码。同步 DSB 和 USER_ASID_FLAG 不会纠正地址位移。

**修复方向及开发确认**：分别以 Arm TLBIP VAE1IS/VALE1IS、RVAE1IS/RVALE1IS 的规范确认高操作数定义；若如代码注释，统一使用 >>12，页数计算和 stride 仍按 PAGE_SHIFT。协作复核未能取得 Arm 官方页面全文，第三方镜像未作为最终架构依据，因此本项保留明确确认条件。

### TLB-5 / P1：MMFR4 未聚合 secondary CPU，TLBID matches 忽略 local scope

**静态置信度：高（代码契约）；平台触发条件待确认。位置**：`arch/arm64/kernel/cpufeature.c:1140,1452-1454,2582-2591,3863`。MMFR4 初始化引入 `3acc35564e3c2ddd8d54ec3abaa4c7b6b427cbe8`，TLBID matches 引入 `54b969b13247500ce3b18f1f1dc92b6781ced36e`。

初始化将 boot CPU 的 MMFR4 放入 sanitised registry，但 `update_cpu_features()` 没有对应的 `check_update_ftr_reg(SYS_ID_AA64MMFR4_EL1, ...)`；只有当 incoming CPU 自己声明 TLBID 时，才更新 TLBIDIDR。boot CPU 支持 TLBID、另一 CPU 不支持时，系统 MMFR4 不能降到安全共同值。更进一步，late CPU 校验框架明确用 `matches(..., SCOPE_LOCAL_CPU)`，而 `test_has_tlbid()` 忽略 scope，仍读取 system sanitised 值，不能识别这个 incoming CPU 缺少特性。

**影响条件**：系统有 TLBID 不一致的 CPU，且 SYSINSTR128 等其他 capability 检查没有先拒绝它。可能启用不被全部 CPU 支持的 domain invalidation；具体未支持 CPU 对 domain 位的解释及后果由架构确认。boot 不支持而 secondary 支持的反向组合至少会保守漏启用，但不与错误启用混为同一严重后果。

**反证**：ftr 表的 FTR_LOWER_SAFE/FTR_STRICT 是规则描述，需要 update 调用才能执行；它不会自行遍历新 CPU。通用 verify_local_cpu_caps 确实存在，但其输入谓词在这里读错 scope。SYSINSTR128 有独立通用检测，故不能无条件宣称任何不支持 TLBID 的 CPU 都会通过全部检查。

**修复方向及开发确认**：补齐 MMFR4 聚合；local scope 读取当前 PE 的 ID 和 TLBIDIDR，system scope 使用共同值。核查是否存在硬件契约保证完全同构；若有，也应说明为何 late capability 检查可以故意不读 local。关联 BBML2 复核另追查这些路径，没有运行验证。

## 二、!29736 关联 vTLBID 候选（不归因给 !29666）

### VTLB-1 / P1：CPUMASK_OFFSTACK 下写未初始化指针

**静态置信度：高。位置**：`arch/arm64/kvm/arm.c:879-886`；引入 `67197ff4b35dd750cc3e71b5bd9260088f14136c`。

`cpumask_var_t new_cpus;` 没有分配就传给 `cpumask_copy()`。`include/linux/cpumask.h:1020-1021` 在 CONFIG_CPUMASK_OFFSTACK=y 时将该类型定义为指针，因此写入目标未初始化。触发链为 KVM_ARM_VTLBID=y、host 支持 TLBID、VM 初始化成功、vCPU 首次加载/迁移进入 `update_domain_map()`。无需 guest 显式启用虚拟域，默认 domain0 优化也会走该路径。

**影响**：内核地址异常或内存破坏，具体指针值不作假定。OFFSTACK=n 时是一个内嵌数组，不应声称所有配置均触发；当前 defconfig 未显式开启该选项，但允许的配置组合需要正确。

**反证与修复**：分配 `vdomain_cpumasks[]` 不会给本函数局部变量分配空间。调用发生在 spinlock 内，不能随意补 GFP_KERNEL；可直接在锁内更新现有 mask，或选用适合栈大小/上下文的明确存储。开发确认目标配置、NR_CPUS 栈预算和预分配方案。

### VTLB-2 / P1：先按 32 位 int 左移，再赋 u64，破坏高半部映射

**静态置信度：高。位置**：`arch/arm64/kvm/arm.c:928,947-953`；引入 `67197ff4b35d`。

`pdomain` 为 int，`set = pdomain << offset` 的表达式先按 32 位 int 求值，之后才转换为 u64。8 位 entry 格式中 i=4 起 offset=32；16 位 entry 格式中 i=2 起 offset=32，均超出类型宽度。更低 offset 也可能把符号位移入并导致错误扩展。循环按硬件 NVIS 的全部项执行，未配置项还会回退为 domain_map[0]，所以不需要 userspace 明确启用高编号虚拟域才会遇到表达式。

**影响**：C 未定义行为；不能可靠地把指定 physical domain 放进高 32 位，可能保留/清除错误字段并使 guest TLBI 错投范围。仅从 C 源码不预测某一编译器的具体错误结果。

**反证与修复**：u64 的左值不改变右侧移位类型；GENMASK 在 ARM64 的 64 位 unsigned long 上构造 clear 掩码不是补救。应在移位前转换，例如 `(u64)pdomain << offset`，并按 field width 掩码/验证。开发需检查全部允许 NIS/NVIS 组合及 fallback 项，不只 domain0。

## 三、需进一步确认的关联风险

1. **nVHE 的 EL2 访问上下文**：`arm.c:969-976,1113-1115` 直接读写 HFGRTR2_EL2，`906-919` 直接写 VTLBIDn_EL2；调用来自普通 `kvm_arch_vcpu_load/put`，Kconfig 只依赖 KVM/ARM64_TLBID，初始化也没有 `has_vhe()` gate。非 VHE host 通常在 EL1，因此需开发核对目标运行模式和架构 trap/访问规则。若无额外平台保证，应移动到 hyp 上下文或明确限制功能。寄存器映射代码由 `67197ff4b35d` 引入，trap 代码由 `b3cd89c851cf69e2813d0e672333014cdb521ad2` 引入。固定 HEAD 的 PVM 初始化直接拒绝创建 VM，不能拿 PVM 作当前可达复现条件；普通 nVHE 需单独确认。
2. **16 位 entry 下虚拟域容量**：`arm.c:338-340` 一律将 NVIS cap 到 5（32 域），但 `933-948` 在 NIS=9..16 时每项 16 位，四个 64 位寄存器只能容纳 16 项；i>=16 得到 idx>=4，`921-922` 只 WARN 并不写寄存器。代码自身的 sanitised_ids 接受 NIS=9..16、NVIS=5。开发需对照实际 VTLBID 架构寄存器数量确认，应扩展实现寄存器覆盖，或按 entry width 限制对 guest 暴露的容量。不要只修改 GET ioctl 数字而不修改循环与 emulated ID。
3. **运行后 SET_VDOMAIN 与缓存映射**：`arm.c:2395-2401` 改 membership/num_domains，不持 tlbid_lock、不清 last_pcpu、不重建 map；`1008-1012` 在同一 pCPU 的下一次 load 只重写旧 map。`sys_regs.c:3835-3843` 明确容许运行后跳过 ID 更新而保持 bitmap 修改。已有域的 membership 动态增加后，其 physical domain 可能仍不包含新增 vCPU 所在 CPU；vm-wide num_domains/guest_tlbid_enabled 的不同 vCPU ioctl 也仅受各自 vCPU mutex 保护。开发需明确 API 是否仅能在所有 vCPU 首次运行前调用；若是应显式拒绝后期调用，否则需要映射失效、同步和重新加载协议。只验证 `num_vdomains <= max` 也未拒绝 0 或 bitmap 越界，须按 API 契约补齐。
4. **外部硬件语义**：host 的 `cpu_enable_tlbid()` 仍有 SCTLR2/HCRX 等 TODO；host 建表假设每域包含所有 SMMU，而只据 CPU 表选择域。是否由 firmware 保证、是否涵盖 IOMMU/SVA 的失效需求需维护者确认。不能仅因 TODO 或没有 SMMU 过滤就断言已存在失效丢失。
5. **范围优化/拓扑退化**：单 CPU 域留在 `cpu_domain[0]` 原始位置；搬走前面的多 CPU 域后留下 `id=-1` 空洞，查找遇到首个空洞停止，会跳过后面的单 CPU 域。对有效的覆盖全系统 domain0，通常退化成更大域而非错误遗漏失效。另需审查是否要显式验证 domain0 包含所有 CPU，避免畸形表令 sentinel 搜索假设不成立。
6. **后续并发及硬件状态**：reload request 路径未持 tlbid_lock，map writer 和 request reader 的完整时序需核对；不要仅凭 `KVM_REQUEST_WAIT` 名字推导等待处理函数结束或必然死锁。VM 初始化读 raw TLBIDIDR 而非 sanitised 值，CPU 迁移后的 virtual limits 一致性同样依赖硬件同构条件。

## 四、覆盖记录与已排除的简化判断

| 改动组 | 静态覆盖与判断 |
|---|---|
| MMFR4 / SYSINSTR128 / TLBID capability、cpuinfo、sysreg、cpucaps | 读取初始化、次级更新、local/system scope、常量 gate、capability enable；候选 TLB-5。新增 slot 对预留位的处理已浏览，没有以列表插入位置直接推导二进制 ABI 损坏。 |
| Kconfig / defconfig / Makefile | host 默认启用受 AS_HAS_TLBIP 约束；无汇编支持时 Kconfig 不允许启用，不能仅根据“raw fallback”的帮助文本断言当前构建必须执行不存在的 raw 分支。vTLBID 默认 n，问题需用户启用配置。 |
| ACPI TLBI 表定义、通用 parser、arm64 init、target 建表 | 覆盖固定头、payload、计数、ID、错误清理、处理器 UID、SMMU 前置假设；TLB-1/2/3。parse 失败会 reset 并回退 domain0，但越界访问发生在可返回错误之前。 |
| pick_best_domain | 使用 active cpumask 的超集域，而非注释所说必须 exact match；对 TLBI 安全性而言超集允许，只影响干扰范围。空 active mask、单一 domain 回退 0 已考虑。 |
| mm/context.c / smp.c | CPU 位设置在 `switch_mm_fastpath` 标签后，快慢路径都会执行，并不是只慢路径记录。ASID rollover 后按新 ASID 使用本地 flush；旧位保留通常导致过度广播。CPU dead 后清除任务 mask，未凭缺少本地锁就断言 bitmap 丢位。跨 CPU mask 首次加入与失效 ordering 仍应由内存管理维护者复核。 |
| tlbflush.h 全部改动 | LOCAL/DOMAIN/BROADCAST、IRQ 保存恢复、DSB、mm/page/range、user ASID twin、range fallback 和全 mm limit 均读取；TLB-4。nosync 现在也调用 flush_tlb_end 增加末尾同步，主要是性能/契约命名退化，不当作缺少完成屏障。 |
| TLBID KUnit 源码 | 只阅读；使用连续域 0/1/2、较大手工 bitmap，不能作为稀疏 ID、16 位实际初始化、畸形表或 16K/64K 编码的覆盖证据。其临时修改全局 topology/MADT 的并发安全未在本轮深入，不运行 suite。 |
| vTLBID VM/vCPU 生命周期 | VM 分配后才置 enable，失败释放已分配 mask；free 路径、xarray、vCPU 默认 membership、same-pCPU load、迁移链表、flush/request/reload、put 均追查；VTLB-1/2。VM init 忽略 feature init 返回值时其 enable 仍 false，是设计中的降级，不直接断言 use-after-free。 |
| vTLBID uapi、ioctl、ID emulation、trap、HCRX | GET/SET、全局和 per-VM capability、MMFR4 可见性、TLBIDIDR 只读模拟、DVMBM 禁用互斥均阅读。运行后配置、nVHE、容量为待确认项。sys_regs 表中添加 MMFR4 本身不证明所有其他字段可暴露，具体保留位/feature mask 需结合 ftr 表。 |
| trace、后续历史 | tracepoints 没有发现独立越界路径。检查 !29666/!29736 至 HEAD：核心 host TLBID 文件无候选修复；KVM 后续 PVM 改动未修复所列映射代码，但明确阻断当前 PVM VM 创建。 |

## 五、依据与限制

依据固定 HEAD 的源码及归档 diff、commit 列表、`git blame` 引入归因和相关路径历史；主代理初查线索经过独立源码核验，不作为结论本身。协作 BBML2 审查员补充核查 TLBIP 字段与 MMFR4 路径；最终硬件字段结论仍标明所需的一手规范。

没有执行任何测试或模型，未验证实际固件表、硬件异常行为、编译器生成指令、KVM guest 运行或性能。没有把条件性内存破坏候选标为已被实际利用，也没有把静态高置信候选标为已复现。建议开发首先复核 C 层确定性边界/类型问题，再确认页粒度和硬件运行模式相关候选。
