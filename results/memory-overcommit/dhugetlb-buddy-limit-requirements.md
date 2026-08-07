# dhugetlb 内存超分 需求文档（v1）

> 记录下游提出的全部需求条目、我们的解读和当前状态。
> 配套文档：`dhugetlb-buddy-limit-design.md`（设计）、
> `dhugetlb-buddy-limit-testcases.md`（测试用例，待按 v4 语义修订）。

## 背景

下游业务在 dhugetlb（动态大页）场景下做内存超分：压缩每个子 memcg 的独占
内存（dpool + 备援），峰值波动走多个子 memcg 共享的配额池，利用峰值错开
提高内存利用率、节省总内存。

## 需求清单

| 编号 | 需求 | 状态 |
|---|---|---|
| R0 | 原始需求：buddy_limit 配额 + OOM 保护 | 已解读，语义定稿待确认 |
| R1 | memcg 内存不够时，允许使用共享内存 | 已纳入设计 |
| R2 | 使用内存时，优先使用 memcg 独占内存 | 已纳入设计 |
| R3 | cgroup 删除时，其使用的共享内存可正常释放 | 已纳入设计 |
| R4 | 共享内存支持容量约束 | 已纳入设计 |
| R5 | 共享内存支持迁移到 memcg | 已纳入设计 |
| R6 | 共享内存不足时回收使用者；支持水线提前 kswapd 回收 | 已纳入设计 |
| R7 | 支持多个 swapfile（ZRAM 及盘），支持优先级配置 | 已纳入设计，独立排期 |
| R8 | 共享内存与 1G 大页转换（hugetlb_cma 适配动态大页） | 已纳入设计 |

## 需求明细

### R0 原始需求

> 在 dhugetlb 子树内，为非 Pool 的 order-0 页面（buddy 页）引入独立的
> buddy_limit 配额，与 memory.limit 解耦。业务动机：压缩每个子 memcg 的
> 独占内存，峰值波动走多子共享的 buddy_limit，达成内存节省。buddy charge
> 融入 try_charge() 的 retry + OOM 机制，享有完整 OOM 保护。

解读：buddy 兜底页单独记账、多 memcg 共享限额；超限走标准回收/OOM 路径。
设计对应：§3、§4.1~4.3。

### R1 memcg 内存不够时，允许使用共享内存

解读：dpool 耗尽后走 buddy 分配，受共享 buddy_limit 约束（不是无限制）。
设计对应：§4.2。

### R2 使用内存时，优先使用 memcg 独占内存

解读：分配顺序 dpool 优先（现有机制）；quota 层面共享部分只是 dpool
不足时的补充。已确认不是"buddy 页只记共享账"。
设计对应：§2（分配顺序现状）、§3。

### R3 cgroup 删除时，共享内存正常释放

解读：memcg rmdir 不被残留 buddy 记账卡死，共享配额不泄漏。
设计对应：§4.4。验收：TC-6.1。

### R4 共享内存支持容量约束

解读：buddy_limit 可配置、可观测（buddy_usage）。
设计对应：§4.1、§5。验收：TC-2.x。

### R5 共享内存支持迁移到 memcg

解读：峰值回落后，把计入共享配额的页迁回 memcg 独占账，释放共享配额。
A 语义下实现简化为"从 buddy 账摘掉，memory 账不动"。
设计对应：§4.4。验收：TC-5.x。

### R6 共享内存不足时回收 + 水线提前回收

解读（两条）：
a) 共享配额不足时，回收对象是**共享内存的使用者**（按共享占用定向回收），
   不是只回收触发分配的 memcg；
b) 支持配置水线，越线提前触发 kswapd 异步回收，削峰、避免同步 OOM。
设计对应：§4.3。验收：TC-3.x、TC-4.1。

### R7 多 swapfile 与优先级

解读：memcg 可绑定多个 swap 设备（ZRAM、盘），按优先级选择换出目标
（ZRAM 优先，满后落盘）。现状 `memory.swapfile` 仅支持单设备、无优先级。
与 buddy_limit 主线解耦，可独立开发合入。
设计对应：§4.6。验收：TC-7.x。

### R8 共享内存与 1G 大页的转换（hugetlb_cma 适配动态大页）

> 原文：规整的时候迁移对象（dest page）优先从 source page 所属的
> memcg->dpool 里申请，没有再 fallback 到 buddy。

解读：dpool 合并大页或 CMA 收拢连续 1G 时，挡路的占用页需要迁移；
迁移目标页优先从该页所属 memcg 的 dpool 分配（页被吸进独占池、共享账
同步摘掉），池无空闲才回退 buddy。避免碎片在 buddy 里搬来搬去，同时
让共享占用回落。
设计对应：§4.5。注意 buddy 记账对称性（迁入 pool 后摘 buddy 账）。

## 关键决策记录

1. **memory.limit 语义不变**（v4 定稿方向）：继续作为 memcg 总配额，
   所有页都记账；独占/共享的区分落在物理来源（dpool/buddy）+ buddy_limit
   聚合闸上，采用双重记账。待架构师/需求方最终确认。
2. 已确认的推论：峰值顶到 memory.limit 直接 OOM，共享池不续命；
   "每 memcg 总量硬顶"与"共享池自由竞争"互斥，本需求选前者。
3. 已否决：memory.limit 只管 dpool（B）、memory.limit 改保底（C），
   理由见设计文档 §3.3。

## 待对齐清单

- A 语义最终确认（特别是"共享不续命"是否接受）；
- 兄弟 memcg 定向回收的公平性策略；
- buddy_limit 调低到 usage 之下的行为；
- 任务迁移时 buddy 记账是否跟随；
- 多 swapfile 优先级与全局 swap 优先级的关系。
