# dhugetlb buddy_limit 设计文档（草案 v3）

## 1. 背景与目标

dhugetlb（dynamic hugetlb）为每个 memcg 维护一个独占的动态大页池（dpool）。
池内 4K 页不够时，order-0 用户页会兜底走 buddy 系统分配。目前这部分 buddy 页
只能计入 memcg 自己的 `memory.limit`，导致每个子 memcg 都必须按自身峰值配置
limit，独占备援无法压缩。

**目标**：为 dhugetlb 子树内"非 Pool 的 order-0 页面（buddy 页）"引入独立的
共享配额 `buddy_limit`，与 `memory.limit` 解耦，由同一父 memcg 下的多个子
memcg 共享。子 memcg 独占内存只覆盖保底用量，峰值波动从共享配额支取，利用
峰值错开节省总内存。

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
  `high_async_ratio`（mm/memcontrol.c:119-123），usage 越过 high 的
  ratio 水线即触发 memcg 异步回收。
- 每 memcg swap 已有单设备绑定：CONFIG_MEMCG_SWAP_QOS 的
  `memory.swapfile`，`memcg->swap_dev->type` 只能记录一个 swap 设备
  （或 none/all），无优先级概念（mm/memcontrol.c:4745-4826，
  include/linux/memcontrol.h:407-409）。
- dhugetlb 现有接口为 cgroup v1 文件：`dhugetlb.nr_pages` 等
  （mm/memcontrol.c:6485-6501）。

## 3. 总体设计

### 3.1 核心概念：共享内存是一类独立的记账对象

本设计把 dhugetlb 子树内的 buddy 兜底页定义为一类独立的资源——**共享内存**，
它有自己的配额（buddy_limit）、自己的统计（buddy_usage）、自己的回收策略
（同步 OOM + 异步水线）和自己的生命周期管理（迁移、reparent）。它与
`memory.limit` 完全解耦：两套配额独立配置、独立统计、独立演进。

页的归属因此分为三类：

| 页类型 | 记账对象 | 说明 |
|---|---|---|
| pool 页（1G/2M/4K） | dpool 现有机制 | 独占预留，本设计不改 |
| buddy 页，记在自己额度内 | 自己的 memory counter | 需求 2：优先用自己的 |
| buddy 页，记在共享配额内 | 共享 buddy counter | 需求 1：不够时用共享的 |

### 3.2 配额模型：与 memory 平行的第二条 page_counter 链

```
                  父 memcg (子树根)
                 ┌────────────────────────────┐
                 │ memory counter (现有)       │
                 │ buddy counter  (新增)       │ ← buddy_limit 配在这里，
                 └────────┬───────────────────┘   多子天然共享
            ┌─────────────┼─────────────┐
       子 memcg A     子 memcg B     子 memcg C
       memory+buddy   memory+buddy   memory+buddy   ← 每个子 memcg 两条链都挂父级
```

`struct mem_cgroup` 新增 `struct page_counter buddy`，父指针挂父 memcg 的
`buddy` counter，形成与 `memory` counter 平行的层级链。复用 page_counter
自带语义：charge 沿祖先传播、任一祖先超限即失败——父 memcg 配置 buddy_limit
后所有后代共享，**无需任何额外的子树管理代码**。默认 `PAGE_COUNTER_MAX`
（不限），未配置时行为与现状完全一致。

### 3.3 charge 模型：自己额度优先，共享兜底

对 dhugetlb 子树内的 buddy 兜底页（判定：`dpool_enabled &&
mm_in_dynamic_pool(mm) && !PagePool(page)`），charge 顺序：

```
1. try 自己的 memory counter → 成功：与现状完全相同      ← 优先用自己的
2. 超限 → try 共享 buddy counter → 成功：该页计为共享内存  ← 不够时用共享的
3. 都超限 → try_charge() 现有 retry 循环：回收 → 重试 → OOM ← 完整 OOM 保护
```

folio 需记录计入了哪个 counter（1 bit），供 uncharge/迁移对称处理。
pool 页、非 dhugetlb 页、order > 0 页一律走现有路径。

### 3.4 回收模型：同步 OOM 兜底 + 异步水线削峰

- **同步**：共享配额超限时不新增失败路径，直接落入 try_charge() 现有的
  回收 → 重试 → OOM 循环，超分行为受完整 OOM 保护。
- **异步**：参照现有 `memory.high` + `high_async_ratio`，在 buddy counter
  上配置水线，共享用量越线即提前唤醒 kswapd 对使用共享配额的子 memcg
  回收，把压力挡在同步 OOM 之前。

### 3.5 生命周期：迁入、迁出、销毁

共享内存支持完整的生命周期操作，均复用 memcg 现有框架：

- **迁回 memcg**：峰值回落后，把共享占用迁回自己的 memory counter
  （复用 move_charge 的 folio 遍历框架，方向为 counter 间迁移）；
- **cgroup 删除**：offline 时 buddy charge 跟随现有 reparent 路径迁到
  父级 buddy counter，配额记账不泄漏。

### 3.6 swap 配套：多设备分级

共享内存超分后回收压力增大，swap 能力同步扩展：memcg 的 swap 绑定从单设备
扩展为按优先级排序的设备列表（ZRAM 优先、盘兜底），与共享内存机制配套但
实现上相互独立。

### 3.7 设计原则

1. **零侵入默认路径**：所有新逻辑只在 `dpool_enabled && mm_in_dynamic_pool()`
   为真时触发；不配置 dhugetlb 的系统无任何行为变化和性能损耗。
2. **最大化复用**：page_counter 层级、try_charge 的 retry/OOM、
   high_async_ratio 水线、move_charge 遍历框架，均为现成机制的小幅扩展。
3. **功能项解耦**：4.1 是核心，其余功能项（水线、迁移、多 swapfile）可独立
   开发、独立合入。

## 4. 功能项设计

### 4.1 共享配额与容量约束（需求 1、2、4）

**方案**：即 3.2/3.3 的数据结构与 charge 模型，这是本特性的核心。

**接口**：

| 文件 | 属性 | 说明 |
|---|---|---|
| `dhugetlb.buddy_limit` | 写 | 共享配额（字节），`max` 不限（默认）；配在父 memcg 上对所有后代生效 |
| `dhugetlb.buddy_usage` | 读 | 当前共享内存使用量（含层级累计） |

**涉及代码点**：`mem_cgroup` 结构体（include/linux/memcontrol.h）、
`mem_cgroup_charge()` 判定与分流（mm/memcontrol.c）、folio 释放时的
对称 uncharge、cftype 注册（mm/memcontrol.c:6485 附近的 dhugetlb 文件组）。

### 4.2 回收与水线（需求 6）

**方案**：

- 同步回收：复用 try_charge() retry 循环，先回收当前 memcg。增强项：
  回收不足时按共享用量从大到小回收兄弟 memcg（第一版可不做，见 7.5）。
- 异步水线：参照 CONFIG_MEMCG_QOS 的 `memory.high` + `high_async_ratio`
  实现（mm/memcontrol.c:119-123），在 buddy counter 上等价实现：
  共享用量越过 `buddy_limit * ratio` 即触发对共享使用者的 kswapd
  异步回收。

**接口**：

| 文件 | 属性 | 说明 |
|---|---|---|
| `dhugetlb.buddy_wmark_ratio` | 写 | 异步回收水线百分比，默认关闭 |

### 4.3 共享内存迁移回 memcg（需求 5）

**方案**：新增写接口，内核从该 memcg 的 LRU 上找计入共享配额的 folio，
逐个 try charge 自己的 memory counter，成功后从 buddy counter 对称扣减，
达到指定量或无可迁页为止；迁移失败（自己 limit 也不够）跳过该页，不影响
业务。复用 move_charge 的 folio 遍历/锁定框架（mm/memcontrol.c:205
`move_charge_struct`），但方向是同一 memcg 内 counter 间迁移，不涉及
mm 遍历和 task immigration。

**接口**：

| 文件 | 属性 | 说明 |
|---|---|---|
| `dhugetlb.buddy_migrate` | 写 | 把 N 字节共享内存迁回自己的 memory counter |

### 4.4 cgroup 删除（需求 3）

**方案**：memcg offline/destroy 时，buddy counter 跟随 memory counter 现有
reparent 路径：子 memcg 的 buddy charge 迁移到父 memcg 的 buddy counter
（page_counter 层级天然支撑），保证 rmdir 不被残留 charge 卡住，父级看到的
使用量始终等于存活后代之和。

**涉及代码点**：`mem_cgroup_css_offline()` / `mem_cgroup_css_free()`
的 counter reparent 逻辑。

### 4.5 多 swapfile 与优先级（需求 7）

**方案**：`memcg->swap_dev->type`（单设备）扩展为按优先级排序的设备列表：

```c
struct memcg_swap_entry {
	int type;		/* swap 设备 (zram/盘) */
	int prio;		/* 优先级，数值小者优先 */
	struct list_head list;
};
```

- swap 分配路径（`memcg_get_swap_type()`，mm/memcontrol.c:4803）改为按
  优先级遍历列表：高优先级设备（如 ZRAM）有空间则用，满了再落盘设备；
- 单项老配置行为与现状一致，保证兼容。

**接口**：`memory.swapfile` 写语法扩展为 ``<path> [prio]``，可多次写入
绑定多个设备；读取按优先级列出全部绑定；`none`/`all` 语义不变。

## 5. 典型使用流程

1. 父 memcg 配 `dhugetlb.buddy_limit`（共享池大小）和
   `dhugetlb.buddy_wmark_ratio`（如 80，越过 80% 提前异步回收）；
2. 各子 memcg 按保底需求配自己的 dpool 和 `memory.limit`（较现状压缩）；
3. 峰值期子 memcg 池内 4K 页不足 → buddy 分配 → 自己 limit 内优先，
   超出部分计共享配额；
4. 峰值回落后按需写 `dhugetlb.buddy_migrate` 把共享占用迁回自己名下；
5. 回收路径按 4.5 配置的优先级换出（ZRAM → 盘）。

Sizing 关系：`Σ(子 memcg 保底) + buddy_limit < Σ(子 memcg 各自峰值)`，
差值即节省量。

## 6. 边界、限制与风险

### 6.1 限制

- 仅作用于 dhugetlb 子树内的 order-0 用户页；order > 0 分配（含 THP）
  不走 dpool，不受影响（dhugetlb 与 THP 的既有冲突不变）。
- 内核线程、`__GFP_IO|__GFP_FS` 被剥离的分配不进 dpool，也不进共享配额
  （口径同 `dynamic_pool_should_alloc()`）。

### 6.2 统计口径

计入共享配额的页 LRU 归属不变，但不再占其 memory counter。监控需使用
`dhugetlb.buddy_usage` 与 memory usage 的组合口径。

### 6.3 风险

- charge/uncharge 对称性：folio 必须记录计入的是哪个 counter，否则
  迁移/释放时计账漂移（3.3 的 1 bit 标记）。
- 共享池被少数 memcg 长期占满时，其他兄弟的 buddy 分配会频繁走回收/OOM
  路径，表现为"自己 limit 远未用完却被 OOM"。超分比例需业务侧把控。
- 水线参数（ratio、水线余量）需按业务峰值形态调优，缺省值保守处理。

## 7. 未决问题（后续逐步分析）

1. 需求 2 的解读确认：本设计采用"配额扣减顺序上自己优先、共享兜底"。
   若需求方本意是"buddy 页一律只记共享配额"，则 3.3 退化为单步 charge，
   4.3 迁移接口可取消——需要与需求方确认。
2. 多级嵌套：后代 memcg 能否再设更小的 buddy_limit（page_counter 层级
   语义天然支持取最小约束，确认是否符合预期）。
3. buddy_limit 调低到当前 usage 之下的行为：拒绝写入，还是接受并靠
   回收/拒绝新分配收敛。
4. 任务迁移（cgroup.procs）时 buddy charge 是否随
   `memory.move_charge_at_immigrate` 迁移。
5. 对兄弟 memcg 的同步回收（4.2 增强项）的触发条件与公平性策略。
6. 多 swapfile 的优先级与全局 swap 优先级（`/proc/swaps` prio）的关系：
   memcg 内独立排序还是作为全局 prio 的偏移。
7. 性能实测：dhugetlb 子树内 order-0 用户页多一次 page_counter 操作，
   预期可忽略，需压测确认。
