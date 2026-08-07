# dhugetlb buddy_limit 设计文档（v4）

## 1. 背景与目标

dhugetlb（dynamic hugetlb）为每个 memcg 维护一个独占的动态大页池（dpool）。
池内 4K 页不够时，order-0 用户页会兜底走 buddy 系统分配。本需求为这类
"非 Pool 的 order-0 页面（buddy 页）"引入独立的共享配额 `buddy_limit`，
由同一父 memcg 下的多个子 memcg 共享，实现内存超分：各 memcg 独占段
（dpool）压缩到保底量，峰值波动从共享池支取，利用峰值错开提高内存利用率。

非目标：不改变 pool 页（1G/2M/4K）的现有分配与记账语义；不涉及 order > 0
分配；不引入新的调度/优先级机制。

## 2. 现状分析（代码事实）

- 分配路径：`__alloc_pages()` 在走 buddy 之前先尝试
  `dynamic_pool_alloc_page()`（mm/page_alloc.c:5030）。后者只对 order-0、
  GFP_HIGHUSER_MOVABLE 类用户页生效（mm/dynamic_pool.c:704
  `dynamic_pool_should_alloc()`），池空返回 NULL，继续走常规 buddy 分配。
  即**分配顺序上已是 pool 优先**。
- pool 页带 `PagePool` 标记（mm/dynamic_pool.c:782），buddy 兜底页没有。
- charge 时机：缺页路径先分配、后 charge（mm/memory.c:4767-4769，
  `vma_alloc_folio()` 成功后 `mem_cgroup_charge()`）。charge 时即可用
  `PagePool` 标记区分页来源。
- memcg 配额基于 `struct page_counter` 层级：charge 沿祖先链传播，任一祖先
  超限即失败；`try_charge()` 内含 回收 → 重试 → OOM 的完整循环。
- 异步回收水线已有现成机制：CONFIG_MEMCG_QOS 提供 `memory.high` +
  `high_async_ratio`（mm/memcontrol.c:119-123）。
- 每 memcg swap 已有单设备绑定：CONFIG_MEMCG_SWAP_QOS 的
  `memory.swapfile`，`memcg->swap_dev->type` 单设备、无优先级
  （mm/memcontrol.c:4745-4826，include/linux/memcontrol.h:407-409）。
- dhugetlb 现有接口为 cgroup v1 文件：`dhugetlb.nr_pages` 等
  （mm/memcontrol.c:6485-6501）。

## 3. 核心语义决策（v4 定稿方向，待架构师/需求方最终确认）

### 3.1 决策：memory.limit 语义不变

本需求**不要求改变 memcg 的总配额语义**。memory.limit 继续作为 memcg 的
总配额：所有页（无论物理上来自 dpool 还是 buddy）都记入 memory counter，
顶到 limit 走回收/OOM，行为与现状完全一致。

独占与共享的区分**不落在 memory.limit 上**，而落在：

- **物理来源**：dpool 页 = 独占段（物理预留，压缩到保底量）；buddy 页 = 共享段；
- **共享总闸**：buddy 页在记 memory 账的同时，**加记**一笔共享 buddy 账，
  受 buddy_limit 聚合约束。

### 3.2 双重记账模型

```
dhugetlb 子树内的 buddy 兜底页（!PagePool && mm_in_dynamic_pool）：
  1. charge memory counter（与现状完全一致，含 retry/OOM）——总配额约束
  2. 加 charge buddy counter（沿层级传播）——共享池聚合约束
  两个约束任一不满足，分配都失败。
folio 上用 1 bit 标记"本页也计了 buddy 账"，uncharge 时对称扣减，防泄漏。
```

由此得到两个重要性质：

- **物理占用上界 = Σdpool + buddy_limit**，与 memory.limit 无关——每个页
  要么来自 dpool（有物理预留顶着），要么来自 buddy（被共享闸卡着）。
  内存节省即：Σdpool(保底) + buddy_limit(同时峰值) < Σ(各自峰值备援)。
- **可观测口径**：`memory.usage_in_bytes` = 总用量（语义不变，存量监控
  零改动）；`dhugetlb.buddy_usage` = 其中共享部分；两者之差 ≈ 独占段用量。

### 3.3 已否决的候选语义（对齐记录）

| 候选 | 内容 | 否决原因 |
|---|---|---|
| B：memory.limit 只管 dpool | buddy 页完全不记 memory 账 | 戳破 memcg 回收体系的压力感知（memcg 用 1G 只计 300M，回收/OOM 时机失控）；存量监控全部失效 |
| C：memory.limit 改为保底，超限改记共享账 | buddy 页先记自己、溢出记共享 | 改变 memory.limit 总配额语义（需求方明确不要求）；页记哪本账依赖 charge 时的 limit 余量，对称性复杂；单 memcg 失去总量硬顶 |

另有一条本质约束已在讨论中确认：**"每 memcg 总量硬顶"与"共享池自由竞争"
互斥**（总量 = 独占 + 共享抢占量，卡死其一才能卡死总量）。A 语义选择了
"总量硬顶"（memory.limit 不变），代价是：峰值顶到 memory.limit 直接
OOM，共享池不续命——此点需与需求方明确确认。

## 4. 总体设计

### 4.1 数据结构

`struct mem_cgroup` 新增：

```c
struct page_counter buddy;	/* buddy 来源页的共享配额 */
```

挂到父 memcg 的 `buddy` counter 上，形成与 `memory` counter 平行的第二条
层级链。复用 page_counter 语义：charge 沿祖先传播、任一祖先超限即失败。
父 memcg 配置 buddy_limit 后所有后代共享，无需额外子树管理逻辑。默认
`PAGE_COUNTER_MAX`（不限），未配置时行为与现状完全一致。

### 4.2 charge / uncharge

在 `mem_cgroup_charge()` 增加判定（`dpool_enabled &&
mm_in_dynamic_pool(mm) && !PagePool(page)`），命中则在 memory charge
之外加做 buddy charge；buddy charge 失败时按 4.3 回收 → 重试 → OOM，
最终失败则回滚 memory charge、释放 folio。

uncharge 时依据 folio 上的 buddy 标记对称扣减。**charge/uncharge 对称性
是正确性红线**：配额泄漏（usage 只涨不跌）是本特性最典型的缺陷模式。

pool 页、非 dhugetlb 页、order > 0 页一律走现有路径。

### 4.3 回收与水线（需求 6）

- **同步**：buddy counter 超限时，回收**共享内存的使用者**（按各 memcg
  共享占用从大到小定向回收，不只回收触发者）→ 重试 → memcg OOM 兜底。
- **异步水线**：参照 `memory.high` + `high_async_ratio`（CONFIG_MEMCG_QOS）
  在 buddy counter 上等价实现：共享用量越过 `buddy_limit × ratio` 即
  触发对共享使用者的 kswapd 异步回收，把压力挡在同步 OOM 之前。

### 4.4 生命周期

- **迁回 memcg（需求 5）**：`dhugetlb.buddy_migrate` 写入 N 字节，内核
  将该 memcg 的 buddy 账页从 buddy counter 上摘掉（uncharge buddy，
  memory 账不动，物理页不动），释放共享配额。复用 move_charge 的 folio
  遍历框架（mm/memcontrol.c:205）。
- **cgroup 删除（需求 3）**：offline 时 buddy charge 跟随 memory counter
  现有 reparent 路径迁到父级 buddy counter，记账不泄漏、rmdir 不卡死。

### 4.5 规整迁移进 dpool（需求 8）

dpool 合并大页（`dpool_promote_pool()`，mm/dynamic_pool.c:426）或
hugetlb_cma 收拢连续 1G 时，`do_migrate_range()` 把挡路的占用页迁走。
现状 dest page 一律从 buddy 分配，页只是在 buddy 里搬家，还可能落进下一块
待规整区域。改为：

```
迁移 dest page 分配顺序：
1. 优先从 source page 所属 memcg 的 dpool 申请空闲 4K 页
2. dpool 无空闲 → fallback buddy（维持现状）
```

效果：共享/buddy 页被吸进独占池，buddy 账同步摘掉；物理区域腾空合并回
大页；碎片不再被赶来赶去。挂点在迁移的 new_page 回调
（`do_migrate_range`/`migrate_pages` 支持自定义分配目标）。

### 4.6 多 swapfile 与优先级（需求 7）

`memcg->swap_dev->type`（单设备）扩展为按优先级排序的设备列表
`{type, prio}`；swap 分配路径（`memcg_get_swap_type()`，
mm/memcontrol.c:4803）按优先级遍历：ZRAM 优先，满了落盘。`memory.swapfile`
写语法扩展为 `<path> [prio]`，可多次绑定；`none`/`all` 与老语法行为不变。
本项与 buddy_limit 主线解耦，可独立开发合入。

### 4.7 设计原则

1. 零侵入默认路径：所有新逻辑只在 `dpool_enabled && mm_in_dynamic_pool()`
   为真时触发；
2. 最大化复用：page_counter 层级、try_charge retry/OOM、high_async_ratio
   水线、move_charge 遍历框架，均为现成机制的小幅扩展；
3. 功能项解耦：4.1~4.4 是核心链路，4.5/4.6 可独立排期。

## 5. 用户接口汇总（cgroup v1）

| 文件 | 属性 | 说明 | 需求 |
|---|---|---|---|
| `dhugetlb.buddy_limit` | 写 | 共享配额（字节），`max` 不限（默认）；配在父 memcg 对后代生效 | 1、4 |
| `dhugetlb.buddy_usage` | 读 | 共享内存使用量（含层级累计） | 4 |
| `dhugetlb.buddy_wmark_ratio` | 写 | 异步回收水线百分比，默认关闭 | 6 |
| `dhugetlb.buddy_migrate` | 写 | 把 N 字节共享内存迁回独占账 | 5 |
| `memory.swapfile` | 读写 | 扩展：`<path> [prio]`，多设备 | 7 |

## 6. 典型配置与 sizing

场景：3 个 memcg，每家峰值 1G、保底 300M，共享池 1.5G：

```
每个子 memcg:  dpool = 300M，memory.limit = 1G（总配额，不变）
父 memcg:      dhugetlb.buddy_limit = 1.5G
```

- 单 memcg 硬顶 = memory.limit = 1G（语义不变）；
- 物理内存规划 = 3×300M + 1.5G = 2.4G（对比不超分的 3G，省 600M）；
- 已知代价：共享池打满时，memcg 没到 memory.limit 也会被回收/OOM。

## 7. 边界、限制与风险

- 仅作用于 dhugetlb 子树内 order-0 用户页；order > 0（含 THP）不走 dpool，
  不受影响（dhugetlb 与 THP 既有冲突不变）；内核线程、
  `__GFP_IO|__GFP_FS` 被剥离的分配不进共享配额。
- 风险一：charge/uncharge 对称性（folio buddy 标记），配额泄漏是最典型
  缺陷，验收以"释放后 buddy_usage 精确归 0"为准。
- 风险二：兄弟 memcg 定向回收的遍历与并发（mm/vmscan.c 侧），是本特性
  最复杂的实现点。
- 风险三：超分固有代价——共享池满时"自己 limit 没用满也被 OOM"，需
  业务侧知晓。

## 8. 未决问题

1. ~~需求 2 的语义解读~~ → 已定稿为 A 语义（见 3.1），待架构师/需求方
   最终确认"顶到 memory.limit 直接 OOM、共享不续命"。
2. 多级嵌套：后代能否再设更小 buddy_limit（page_counter 层级天然支持，
   确认预期）。
3. buddy_limit 调低到当前 usage 之下的行为：拒绝写入，或靠回收收敛。
4. 任务迁移（cgroup.procs）时 buddy charge 是否随 move_charge 迁移。
5. 兄弟 memcg 定向回收的公平性策略细节。
6. 多 swapfile 优先级与全局 swap 优先级（/proc/swaps prio）的关系。
7. 性能实测：dhugetlb 子树内 order-0 用户页多一次 page_counter 操作，
   预期可忽略，需压测确认。
