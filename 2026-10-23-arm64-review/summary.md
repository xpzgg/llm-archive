# 开发复核入口

固定 HEAD：`00ba9aa3d7f58091cb87f8da96599d6d72023ea0`（OLK-6.6）。四个指定 MR 提出 **15 项静态候选**；后续 vTLBID 关联 MR 另有 **2 项**，不归因到基础 TLBID MR。以下均是待开发人员确认的问题线索，不是已复现缺陷。优先级为审查建议，硬件/部署前提不满足时应调整适用性与优先级。

## 候选清单

| 特性 / MR | 编号 | 建议级别 | 分析结论 | 主要触发条件 / 开发确认点 |
| --- | --- | --- | --- | --- |
| [VIP-SMT !28466](vip-smt-review.md) | VIP-1 | P2 | idle 恢复可把刚成功写入的本地寄存器覆盖成旧值 | 影子读取与 MSR 之间插入 sysfs 写 IPI；确认 IRQ 状态 |
| VIP-SMT | VIP-2 | P2 | shared base offline 后，剩余 sibling 用陈旧影子恢复配置 | SMT owner 变化；核对共享寄存器及 per-CPU 影子生命周期 |
| VIP-SMT | VIP-3 | P2 | 目标 CPU offline 导致 IPI 失败，sysfs 仍返回成功 | 写 sibling 的共享属性、目标选择后 base offline |
| [BBML2/BBML3 !28481](bbml2-review.md) | 候选 1 | 高 | OBMM 局部线性映射操作可能修改整个未拆 PUD | BBML3 + rodata=full + OBMM；2MB folio 位于未拆 1GB PUD |
| BBML2/BBML3 | 候选 2 | 高 | present-invalid PUD 未被 pud_present/leaf 识别，恢复被跳过 | BBML3 Realm，大块共享覆盖完整 PUD；部署触发置信度中 |
| [MPAM !27488](mpam-review.md) | MPAM-1 | P1 | 停用时先释放 reqPARTID 表，调度仍可查表 | 扩展标签任务与 MSC 错误停用并发 |
| MPAM | MPAM-2 | P1 | class debugfs 文件留存，指向已释放 class 内存 | 启用 debugfs，remove 最后一个相关 MSC/component |
| MPAM | MPAM-3 | P2 | 根监控组的任务标签被 CPU 默认标签覆盖 | 显式根组监控标签映射到 intPARTID 0 |
| MPAM | MPAM-4 | P2 | debugfs cpumask 读取 OOM 后遗漏 put，删除可无限等待 | get 成功后 kmalloc 失败，随后 remove |
| MPAM | MPAM-5 | P2 | 多 RIS 的 internal PARTID 上限未保持全体最小值 | 同 MSC 各 RIS 上限不同；需核对硬件约束 |
| [TLBID !29666](tlbid-review.md) | TLB-1 | P1 | 按 domain 数量分配却按 raw ID 索引，可越界写 | 非连续正 ID，或伴随合法正域的负 ID；固件输入 |
| TLBID | TLB-2 | P1 | 16 位域容量截断为零，bitmap 分配与访问不匹配 | NIS/NOS=16，ACPI 有非零 domain |
| TLBID | TLB-3 | P1 | ACPI 条目/payload 边界不足，可越界读 | 截断结构或域列表 offset/count 超出表范围 |
| TLBID | TLB-4 | P1（条件） | PAGE_SHIFT 地址编码与 VA[55:12] 字段契约不一致 | 16K/64K、DOMAIN 路径；需以 Arm 一手规格确认 |
| TLBID | TLB-5 | P1（条件） | MMFR4 漏聚合且 matches 忽略 local scope | 支持 SYSINSTR128 但 TLBID 不一致的启动/late CPU |
| [vTLBID !29736，关联](tlbid-review.md) | VTLB-1 | P1 | 局部 cpumask_var_t 未分配就写入 | KVM_ARM_VTLBID=y + CPUMASK_OFFSTACK=y；vCPU load |
| vTLBID，关联 | VTLB-2 | P1 | int 移位后赋 u64，无法可靠写高半部映射 | KVM_ARM_VTLBID=y，entry offset 到达32或符号边界 |

`P1/P2` 仅用于建议优先处理顺序。表中高影响候选不等于目标产品默认配置可触发；尤其 KVM_ARM_VTLBID 默认 n，4K 页不触发 TLB-4，固件表类问题不代表普通用户输入可达。

## 如何供开发人员 review

逐条从报告的准确代码位置、引入提交和触发链核对，先判断产品配置和硬件约束，再确认调用者是否提供了报告未发现的前置保证。能以实际代码/规范反驳的候选应关闭并记录依据；尚缺硬件规格的保留为待确认，避免直接当作缺陷提交修复。

报告还记录了未升为正式候选的风险和已排除的误报，例如 MPAM 不可达的 SRCU 错误返回分支、BBML2 已有的 late CPU 拒绝机制、TLBID 超集域选择并非必然错误等。

## 交付与限制

- Astra 提示词、设计依据、固定 scope、MR 提交列表/净差异、四份详细报告及集成复核说明均已归档。
- 只提交分析，不修改内核源码、分支或提交历史；本轮没有真实内核/硬件复现结果。
- 用户提出不需要测试之前，VIP-SMT/MPAM 曾运行简化分析脚本，历史记录在各报告末尾；补充要求之后停止所有新增测试和模型执行。那些脚本不作为硬件行为的证明或报告验收依据。
- 本次覆盖记录描述实际阅读和追查范围，不是穷尽所有架构、固件和并发状态的正确性保证。
