# 集成与主代理复核说明

固定 HEAD 与 MR 对应关系见 scope.json；全部结论为供开发人员确认的静态候选，未做运行复现。

## 特性交互

- **BBML2 与 OLK OBMM：存在应优先确认的映射粒度契约变化。** `force_pte_mapping()` 在 BBML3 下允许大块，而 `pfn_range_alloc` 的 `can_set_direct_map()` 仍被用来推断无需对齐/拆分。主代理独立阅读了 `check_update_lm_arg()`、invalid/nc 三层回调、pagewalk 和 OBMM 调用者，支持 BBML2 报告候选 1 的代码证据。是否落在未拆 PUD 上仍需开发确认目标部署。
- **BBML2 与 TLBID：未在所查 kernel pageattr 路径确认直接冲突。** 内核属性修改仍通过 `flush_tlb_kernel_range()` 广播；用户 mm 的 TLBID 定向刷新是另一入口。不能据此推断全部 TLBID/BBML2 组合安全。
- **VIP-SMT 与 CPU hotplug：owner 与配置生命周期需共同处理。** 主代理核对 `topology_core_has_smt()` 使用 thread_id；remove_cpu_topology 只清 sibling masks，剩余 CPU 仍可进入恢复，支持报告中的 owner 变更候选。idle exit 的 IRQ 开启状态也已核对 scheduler 路径。
- **MPAM 与调度/resctrl/debugfs：新增映射表和文件扩展了读者生命周期。** 主代理复核 `reqpartid_exit()` 在禁用 static key 前释放映射、resctrl_exit 不清任务标签，以及 class debugfs 子树不属于 MSC 子树，支持 MPAM-1/2 的静态触发链。MPAM-4 的成功 get/失败分配/遗漏 put 清理分支亦已逐行核对。
- **TLBID 与后续 vTLBID：分开归因。** !29666 是用户指定的基础特性；!29736 属后续关联集成且 KVM_ARM_VTLBID 默认 n。其候选不能描述为未开启该配置的默认宿主机必然触发。

## 误报核验示例

MPAM 的 mpam_sync_config 错误返回表面上漏 SRCU unlock，但当前 mpam_apply_config 所有出口只返回 0，因此未作为可达缺陷。若未来补错误传播，该清理问题需要同步处理。

## 复核边界

主代理复核的是关键静态证据和归因，不代表对报告每一条硬件假设进行了独立验证。Arm/HIP13、Realm、MPAM 和固件拓扑条件仍以各报告的“开发确认”说明为准。用户补充不运行测试后，所有后续工作仅为源码/历史/文档读取与报告整理。
