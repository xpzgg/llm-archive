# AGENTS.md — dhugetlb 内存超分（buddy_limit）需求

本文件指导 AI agent 参与本需求的后续工作。读完后优先看配套文档，再动代码。

## 需求一句话

dhugetlb 场景内存超分：各 memcg 独占段（dpool）压到保底，order-0 buddy
兜底页计入多 memcg 共享的 buddy_limit 配额，峰值错开省内存；超限走
回收 → 重试 → OOM 完整保护。

## 文档索引（同目录）

- `dhugetlb-buddy-limit-requirements.md` — 需求清单 R0~R8、决策记录、待对齐项
- `dhugetlb-buddy-limit-design.md` — 设计文档 v4（核心语义决策在 §3）
- `dhugetlb-buddy-limit-testcases.md` — 验收用例表（⚠️ 尚未按 v4 的
  A 语义修订，TC-1.x/2.x 预期已过时，以设计文档为准）

## 代码仓库

内核树：`/home/yjc/project/openeuler`（openEuler，cgroup v1，含自研
dhugetlb/hugetlb_cma/memcg QoS 特性）。

关键代码锚点：

| 内容 | 位置 |
|---|---|
| dpool 优先于 buddy 的分配钩子 | `mm/page_alloc.c:5030` |
| dpool 分配判定（order-0 用户页口径） | `mm/dynamic_pool.c:704` |
| pool 页标记 `SetPagePool` | `mm/dynamic_pool.c:782` |
| 缺页路径先分配后 charge | `mm/memory.c:4767-4769` |
| memcg 结构体（新 counter 加这里） | `include/linux/memcontrol.h` |
| dhugetlb cgroup 文件组 | `mm/memcontrol.c:6485-6501` |
| 水线异步回收范本（high_async_ratio） | `mm/memcontrol.c:119-123` |
| 单设备 swapfile（多设备改造对象） | `mm/memcontrol.c:4745-4826` |
| move_charge 遍历框架（迁移复用） | `mm/memcontrol.c:205` |
| dpool 合并/规整迁移（需求 R8 挂点） | `mm/dynamic_pool.c:426` `do_migrate_range` |

## 已定稿的核心决策（不要擅自推翻）

1. **memory.limit 语义不变**：仍是 memcg 总配额，所有页记账。独占/共享
   区分靠物理来源 + buddy_limit 聚合闸，buddy 页双重记账（memory +
   buddy 两个 counter），folio 用 1 bit 标记是否计了 buddy 账。
2. 峰值顶到 memory.limit 直接 OOM，共享池不续命（待需求方最终确认）。
3. 物理占用上界 = Σdpool + buddy_limit，与 memory.limit 无关。

## 开发约束

- 零侵入默认路径：新逻辑只在 `dpool_enabled && mm_in_dynamic_pool()` 时
  触发；特性未启用时行为与性能零变化。
- charge/uncharge 对称性是正确性红线：任何路径不得造成 buddy counter
  泄漏；验收以"压力进程退出后 buddy_usage 精确归 0"为准。
- 按 patch 粒度增量开发，每个 patch 独立可编译可运行（bisectable）；
  commit message 遵循仓库 `git log` 现有风格。
- 验证方式：验收测试先行（cgroupfs + 内存压力脚本），断言行为级结果，
  数值断言给容差，不断言时序。
- 改动 memcg 回收/OOM 路径时，必须回归非 dhugetlb 普通 memcg 场景。

## 当前状态与下一步

- 阶段：设计/用例对齐中，**尚未开始编码**。
- 待办：① 按 v4 A 语义修订测试用例表；② 与架构师/需求方对齐（设计文档
  §3.3、需求文档"待对齐清单"）；③ 对齐后按 patch 序列开始实现
  （数据结构 → charge/uncharge → cgroup 接口 → 回收 → 水线/迁移/swapfile）。
