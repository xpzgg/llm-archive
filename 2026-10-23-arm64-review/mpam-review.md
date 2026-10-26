# MPAM Narrow-PARTID 静态审查

## 结论与范围

审查 MR **!27488**，merge `62ae3d15d056815d4c27076508bc61f0b23b6f29`，基线第一父提交 `59b3a8b79178d05ef4bdf0c3266c6896efde5772`，最终代码固定为 `00ba9aa3d7f58091cb87f8da96599d6d72023ea0`。覆盖该 MR 的 31 个提交、13 个改动文件及任务调度、resctrl 生命周期、CPU/MSC 上下线、SMMU 和 debugfs 的相关调用者。`git log merge..HEAD --` 检查的 MPAM/resctrl/arm64 MPAM 路径没有后续修复。

本报告列出 **5 项有直接代码证据、建议开发人员优先确认的候选问题**，并单列硬件相关风险。严重度是可能影响的分级，置信度是静态证据强弱，两者不等同。遵照用户最新要求，不新增测试，不将分析称为已经复现，也不要求开发者接受未经硬件验证的推论。

所有行号均为固定 HEAD；定位代码摘录见 [mpam-source-evidence.txt](evidence/mpam-source-evidence.txt)。没有修改内核源码。

## 优先提交开发人员 review 的候选问题

### MPAM-1 / P1：错误停用期间先释放 reqPARTID 表，调度路径仍可访问

**静态置信度：高。引入提交：`d181cc9412cc`（映射分配/释放），与 `cb30664e92e8` 的调度查表结合。**

- 位置：`drivers/platform/mpam/mpam_resctrl.c:1409` 的 `reqpartid_exit()`，`:1563` 的 `mpam_resctrl_exit()`；`drivers/platform/mpam/mpam_devices.c:2647` 附近 `mpam_disable_thread()`；`arch/arm64/include/asm/mpam.h:151`。
- 条件：Narrow-PARTID 已启用，某任务持有大于等于 `num_closid` 的扩展 reqPARTID；MSC 错误 IRQ 或 broken work 进入停用路径。
- 时序：CPU A 在 `mpam_resctrl_exit()` 中调用 `kfree(reqpartid_map)`；CPU B 切换到该任务，`mpam_enabled` 尚未禁用，因此进入 `mpam_get_regval_partid()` → `req2intpartid()`，读取已释放映射。CPU A 随后才执行 `static_branch_disable(&mpam_enabled)`。
- 不变量/影响：调度可达的映射必须保持有效；当前存在 use-after-free 窗口，可能错误分类、KASAN 报告或崩溃。映射读取不持有 `rdtgroup_mutex`、MSC mutex 或 SRCU，因此这些锁不能关闭该窗口。
- 反证核验：`resctrl_exit()`（`fs/resctrl/rdtgroup.c:4289`）只移除 debugfs、注销 filesystem、移除 mountpoint、释放监控资源，没有清任务标签或 CPU 默认值，也没有等待调度读者。注销 filesystem 不等于卸载已有挂载。`resctrl_enabled=false` 并不是调度入口检查的静态 key。`cpuhp_remove_state()` 只移除 CPUHP 状态，不阻止其它 CPU 调度扩展标签任务。
- 修复方向：先阻止新的映射读者，并以适当同步等待在途读者，再释放表；同时处理已挂载 resctrl 的读写、现存任务和 CPU 默认标签，不能只交换两行而忽略其它读者。
- 开发确认重点：核对错误停用时调度及已有挂载的生命周期约定；若上层另有未在本树调用链中体现的“所有任务已清标签”约束，需要明确证据。

### MPAM-2 / P1：class debugfs 文件没有随 class 销毁，形成悬空内存读取

**静态置信度：高。引入提交：`cb17dafb1c6f`。**

- 位置：`drivers/platform/mpam/mpam_devices.c:2544` 创建 `class.N/{features,nrdy_usec,level,affinity}`；`:338` 的 `mpam_class_destroy()`；`:388` 的 `mpam_msc_destroy()`；`:2704` 的驱动 remove。
- 条件：启用 MPAM debugfs 后，unbind/remove 一个 MSC，且其 RIS 是某 class 的最后一个 component 的最后一个成员。平台 driver 没有设置禁止 bind/unbind 的标记。
- 触发链：remove → `mpam_msc_destroy()` → `mpam_ris_destroy()` → `mpam_comp_destroy()` → `mpam_class_destroy()` → `kfree(class)`。该 class 的 debugfs 文件 `private_data` 仍直接指向 `class->props`、`class->affinity` 等内存。
- 不变量/影响：debugfs 文件引用的对象必须存活到文件删除并排空读者。当前之后读取 `/sys/kernel/debug/mpam/class.N/affinity` 等文件可访问已释放对象；影响包括读取复用内存、KASAN 报错或内核故障。
- 反证核验：`debugfs_remove_recursive(msc->debugfs)` 仅删除 `mpam/msc.N`；`mpam/class.N` 是它的兄弟目录，不在该子树内。class destroy 的 `synchronize_srcu(&mpam_srcu)` 不覆盖 debugfs 文件访问，debugfs 使用自己的 active-users 保护。仅为 RIS/MSC 等待 SRCU 也不能解决 class 文件的持久悬空。
- 修复方向：在释放 class 之前移除其 debugfs 子树并排空访问；component 销毁时同步删除 component 目录/链接，避免残留拓扑。
- 开发确认重点：平台是否开放 unbind/remove；在完整拆除协议中检查 debugfs 删除必须发生在对象释放之前。该问题与更早存在的 resctrl 对象拆除风险分开，本项针对本 MR 新创建的文件引用。

### MPAM-3 / P2：根控制组监控任务的标签被 CPU 默认标签整体覆盖

**静态置信度：高。引入提交：`cb30664e92e8`。**

- 位置：`arch/arm64/include/asm/mpam.h:131`、`:151`；动态映射 `drivers/platform/mpam/mpam_resctrl.c:1443`；任务赋值 `:408` 附近。
- 条件：在根控制组的 `mon_groups` 创建监控组，并把原属根控制组的任务写入其 tasks。既可为 Narrow-PARTID 的扩展 reqPARTID（HIP13/PMG=0 情况尤其直接），也可为非零 PMG 的普通监控组。任务运行的 CPU 默认标签不等于这个监控组。
- 触发链：任务保存非零监控标签；调度时 `req2intpartid(req)` 得到父组 intPARTID 0；新增条件 `!mpam_get_regval_partid(regval)` 为真，完整 `regval` 被 `arm64_mpam_default` 替换。例如 req=8 → int=0、CPU 默认 req=0 时，任务实际流量标签为 req=0，监控组按 req=8 过滤无法计入。
- 不变量/影响：显式任务监控标签必须保留监控归属，不能仅因父控制组为根而被当作未指定标签。结果是 tasks 文件显示任务已入组，但监控组计数没有对应流量，流量进入 CPU 默认组的标签。PMG=1、req=0 的情况也会丢失 PMG，属于此前仅判断 `!regval` 不会发生的回归。
- 反证核验：`__rdtgroup_move_task()` 允许从根控制组移入根的子监控组；没有禁止在根目录创建监控组。任务迁移执行 `update_task_closid_rmid()` 也调用同一个架构调度逻辑，不能绕过覆盖。若 CPU 默认恰好也被分配到同一监控组，现象会被遮蔽，但这不是 tasks 接口的必要条件。
- 修复方向：区分“任务没有指定监控/控制标签”和“显式属于 intPARTID 0 的监控组”，保留后者的 reqPARTID/PMG；同时核对根任务继承 CPU 控制组的既定语义。
- 开发确认重点：结合根组任务优先级设计 review 此条件，避免只检查 intPARTID 而丢弃独立监控身份。

### MPAM-4 / P2：cpumask debugfs 读取分配失败后漏掉 active-users put，remove 可永久等待

**静态置信度：高。引入提交：`0eff08a2e543`。**

- 位置：`fs/debugfs/file.c:1006` 的 `debugfs_read_file_cpumask()`，重点 `:1022` 的分配失败分支与 `:1039`/`:1041` 两个清理标签；等待方为 `fs/debugfs/inode.c:747`。
- 条件：读取任一新增 affinity 文件时，`debugfs_file_get()` 成功，随后的 `kmalloc(PAGE_SIZE, GFP_KERNEL)` 失败。随后执行对应 debugfs 文件移除（例如移除 MSC）。
- 触发链：失败分支 `goto kfree` 跳过 `debugfs_file_put(dentry)`，成功 get 的计数永远未释放；remove 减去基准引用后 `wait_for_completion(active_users_drained)` 无法完成。
- 不变量/影响：每个成功 get 都必须配对 put。读取返回 ENOMEM 后仍留下永久活动读者，后续 driver remove/unbind 可挂住；MSC remove 持有全局 `mpam_list_lock`，会进一步阻塞相关管理路径。
- 反证核验：这是 `debugfs_create_mode_unsafe` 使用的自保护 read 实现；文件 close 不替此 read 配对 put。正常和缓冲区过长路径会走 `debugfs_put`，只有分配失败漏掉。
- 修复方向：让分配失败走 `debugfs_put`，然后 `kfree(NULL)`；或使用统一退出链配对引用。
- 开发确认重点：检查所有成功 get 后的退出边，确认移除等待语义即可，不依赖 MPAM 硬件寄存器。

### MPAM-5 / P2：多 RIS 探测时 intPARTID 上限被后续 RIS 覆盖，未取全体最小值

**静态置信度：中高（算术/控制流确定，硬件拓扑需开发确认）。引入提交：`26b0eaa72060`。**

- 位置：`drivers/platform/mpam/mpam_devices.c:788`、`:790`，循环 `:825` 附近，发布系统上限 `:855`。
- 条件：同一 MSC 的多个已识别 RIS 具有不同 intPARTID 上限，较小上限的 RIS 在前，后续 RIS 有更大上限或不支持 Narrow-PARTID；其它 MSC/requestor 没有把系统上限再次降到安全值。
- 代码证据：每次 RIS probe 都用 `min(msc->partid_max, 当前RIS的intpartid_max)` 覆盖 `msc->intpartid_max`；不支持 narrowing 的分支直接赋 `msc->partid_max`。`msc->partid_max` 是 request PARTID 上限，已不再携带前一个 RIS 的 internal 上限。最终只把最后的 `msc->intpartid_max` 纳入全局 min。
- 具体例子：request max=255，RIS0 internal max=15，RIS1 internal max=63；最终报告 64 个 control IDs，而 RIS0 只支持 16 个。创建第 17 个控制组后，对 RIS0 使用 internal=16 的配置超出其能力。父版本通过持续收紧 `msc->partid_max` 保持前一 RIS 的下界，拆成两个变量后丢失这一性质。
- 影响：暴露无法在所有 RIS 实现的控制组，并可能产生 MSC 配置错误/特性停用。是否特定硬件截断或发出哪种错误，不在本报告中当作已验证事实。
- 反证核验：系统层确有 `min(mpam_intpartid_max, msc->intpartid_max)`，但发生在整轮 RIS probe 后，无法修复 MSC 内部已经覆盖的较小值。`get_num_reqpartid()` 的能力判定只限制请求空间，并不会恢复丢失的 internal 下界。
- 修复方向：初始化 MSC internal 上限后，逐 RIS 取最小值，并在 request 上限继续下降时保持两者一致。
- 开发确认重点：确认目标平台/架构是否保证同一 MSC 所有 RIS internal 上限相同。如果确有规范保证，应引用相应约束；当前驱动实现按 RIS 读取该能力，因此不能仅按 HIP13 单一拓扑排除。

## 待开发确认的风险与未升为正式候选的观察

1. **离线 MSC 的动态映射恢复缺口。** `mpam_sync_config()` 会遍历各 component，`mpam_apply_config():2785` 忽略 `mpam_touch_msc()` 的返回值并返回成功；MSC 无在线可访问 CPU 时硬件映射未必写入。再次 online 的 `mpam_reprogram_msc():1701` 只遍历 intPARTID，未遍历扩展 reqPARTID。因此“MSC 不可访问期间分配/回收再分配扩展 reqPARTID，之后上线”存在软件 map 与硬件 map 不一致风险。**重要限制：** `mpam_resctrl_prepare_offline():1935` 在存在 visible volatile 资源（通常 L2）且 resctrl 已挂载时拒绝 CPU offline，阻止常见复现路径。需要确认没有此类资源/资源 invisible 的实际拓扑，或先 offline 后 mount/扩展的可达流程；未据此断言 HIP13 挂载期间常规 CPU hotplug 必然失效。
2. **缩放计数器保存路径没有采用相同单位。** 新普通读取 `mpam_devices.c:1170` 对短 MBWU 值左移 `mbwu_scale`，但 `mpam_save_mbwu_state():1583` 附近仍把原始 register 值直接加到 correction，并未一致地剥离 NRDY/应用 scale。SCALE>0 且走保存/恢复时可能混用计数单位。但恢复时 `config_mismatch` 又可能重置 correction；已有保存/恢复缺陷与新增 scale 回归需分开，且存在上述 volatile offline 限制，故不独立判定已经可复现。
3. **计数器溢出累加值。** `mpam_msmon_overflow_val():1018` 返回位掩码，短计数器缩放后为 `(2^31-1)<<scale`，并非通常模计数器的一整圈 `2^(31+scale)`。原先未缩放版本也返回最大值，因此不能把整个 off-by-one 归因于本 MR；需对照目标 MSC 的溢出/饱和定义，再判定 scale 将每次少计扩大 `2^scale` 的影响。仓库不足以证明目标硬件严格模计数语义，未引用未经核实的外部硬件假设。
4. **混合 narrowing/non-narrowing 资源。** `get_num_reqpartid():243` 只把 mbw_max/min、ccap/cmin 作为无 narrowing 时禁用扩展的条件，未覆盖所有 CPBM/priority 资源。当前 normal 配置路径会向扩展 request IDs 传播配置，能支持某些无 narrowing 资源，不能仅因条件遗漏就判为隔离失效；需与第 1 项离线恢复一起确认混合拓扑。
5. **SMMU 删除失败后的 ID 回收。** resctrl 删除组调用 `rdt_move_group_iommus()` 后不处理错误并继续释放 RMID。若 SMMU 更新失败仍保留旧标签，而扩展 reqPARTID 被分配给另一控制组，设备流量可能被错归属。但忽略返回值属于已有行为；当前所查 `arm_smmu_group_set_mpam()` 的正常已绑定设备更新路径未找到必然错误来源，因此仅保留失败处理风险，不列为本 MR 已确认新增缺陷。

## 覆盖、反证与排除

| 改动组 / 提交 | 覆盖内容与结论 |
|---|---|
| `26b0eaa72060`、`cb30664e92e8`、`44b22472388a`、`028605683527` | int/req 上限、索引编码解码、TASK/CPU 标签、INTERNAL 控制选择、对子监控组传播；候选 3/5，混合资源与 hotplug 风险。narrowing RIS 在映射完成后仅父 intPARTID 写配置，普通写配置路径也遍历扩展 reqIDs，因此不能声称所有正常配置更新漏同步。 |
| `5b2ba68802c4`、`d181cc9412cc`、`fd4560e6febe`、`b4e402930686` | 空闲链表、limbo、动态分配/回收、config sync、表生命周期；候选 1。`list_del_init` 使已分配/limbo entry 的 list 保持 empty，回收会检查全部 PMG；limbo entry 不会被当成空闲后提前重映射。 |
| `863f2241b71c`、`efc05cb8de88` | umount 先 flush limbo 后关闭 CDP，防止用新模式解码旧索引；无 narrowing 限制及部分混合资源风险。flush 中 monitor context 分配失败仍可能留下 limbo，属于待继续验证的低概率分支，不扩大成确定缺陷。 |
| `ee7098fbc7b0`、`5e436271c5d9`、`778c7889d10e` | scale、带宽位宽与 GENMASK、HIP13 workaround；核对字段提取与使用，scale 保存/溢出保留硬件待确认条件。未将厂商勘误无文档推定为错误。 |
| `19a6d76b3811`、`f193e049f4f2`、`15c38b9ceea3`、`6d130b806e35`、`5363e34df323`、`17e192641ecb` | 按资源初始化、排队、更新及取消 MBM work；L2 仅检查当前 monitor 保存的 partid/pmg，减少 modulo monitor 复用时切换目标；`rdt_domain.res` 在创建后、online handler 之前赋值。多 PMG/监控器复用与 CDP 的精确计数语义仍建议硬件核对，未断言全部 RMID 同时 free-run。 |
| `58310b452948`、`f3506bcf9c55`、`6b21ebc8cb3f` | HIP13 L2 零 CPBM、MBOPT 原始带宽值编解码/范围、total MBM 配置能力；审阅 schema 初始化与 read/update 对称路径，未发现足够证据单列问题。 |
| `1bc688bd7fb3`、`778fe7d34776`、`767a8420df3f` | SMMU 改用 reqPARTID/PMG、匹配函数、STE 字段写入及 group/kobject 引用；控制组匹配反查 internal，监控组匹配 request/PMG，与新编码一致。保留删除失败回收风险；没有把新增 pr_debug 当 correctness 缺陷。 |
| `d462edab8277` | `rdtgroup_rename():3893` 在 ARM64_MPAM 直接返回 EPERM，整个跨控制组监控组 rename/migration 路径不可达，因此撤销原来重新分配 RMID 的代码不会在此平台直接触发负 RMID 使用。代价是同父组纯 rename 也被禁止，应作为兼容性变更沟通，而非未经约定判为内存错误。任务在组间迁移、删除子组及删除父组另有遍历/同步，不等同于目录 rename。 |
| `0eff08a2e543`、`cb17dafb1c6f` | debugfs file get/put、子树生命周期、class/component/RIS 引用；候选 2/4。 |
| `96f69caf6a11`、`43a672a69909`、`63de741cd060` | comp cfg 清空、MSC unlink 后 SRCU grace period、PPI error allocation 清理；这些修复没有解决新增 class debugfs 生命周期。`__setup_ppi` freshly alloc_percpu 后检查非空的错误分支缺少现实可达证据，不把其可能重复 free 当正式问题。 |

另外两项已排除：

- `mpam_sync_config():1433` 的 error return 跳过 SRCU unlock，从局部看可疑，但 **当前 `mpam_apply_config()` 的所有返回值均为 0**，连 MSC touch 错误也没有返回。因此这个具体 SRCU 泄漏分支当前不可达；若未来修正错误传播，必须同时补 unlock 与展开回滚。
- 本 MR 在 `fs/resctrl` 增加架构 hooks，而 x86 中没有实现，看似链接问题；本树 `fs/resctrl/Kconfig` 受 `ARM_CPU_RESCTRL` 限制，x86 使用另一实现，不能直接据此声称 x86 构建回归。

## 实际执行的检查与限制

已读取固定 scope、merge diff、提交列表，使用 `git log`、`git blame`、`rg`、源码范围读取核对最终实现、引入提交与相关调用路径。保留了带行号的关键源码摘录。没有切换工作树、修改源码或提交代码。

收到用户“不需要测试”的最新指令之前，执行过一次简化 Python 源码断言/语义演示，文件为 [mpam-checks.py](evidence/mpam-checks.py) 与 [mpam-checks.txt](evidence/mpam-checks.txt)。它输出了根标签覆盖、class 文件未删除及 OOM 引用计数的简化见证，随后函数提取误匹配前置声明，在 `b.index('mpam_resctrl_exit')` 处抛出 `ValueError`，**没有全部通过**。此脚本既不是内核测试也不是硬件复现；收到指令后未修复或再次运行，不以其作为结论成立的前提。

未进行编译、KUnit、自测、故障注入、KASAN 运行或 MPAM/SMMU 硬件测试。硬件寄存器行为依赖仓库实现，没有获得目标平台勘误/寄存器语义的外部确认。以上候选交由开发人员依照所列条件和反证检查 review；不宣称已复现，也不宣称其余路径全面安全。
