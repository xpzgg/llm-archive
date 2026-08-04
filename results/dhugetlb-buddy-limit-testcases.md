# dhugetlb buddy_limit 测试用例表（对齐稿 v1）

> 用途：与架构师、需求提出方对齐"什么叫实现了"。每条用例 = 场景 + 前置 + 操作 + 预期。
> 阶段标注：P0 = 初版验收必须；P1 = 初版可选/v2 补齐。
> 断言原则：行为级断言（"最终 OOM"），数值断言给 ±10% 容差，不断言时序。

## 0. 测试环境（所有用例公共前置）

- 内核启动参数 `dynamic_hugetlb=on`，THP 关闭（dhugetlb 既有约束）；
- cgroup v1 memory 挂载于 `/sys/fs/cgroup/memory`；
- 预分配 ≥ 4 个 1G hugetlb 大页；
- 测试程序 `memhog <size_mb>`：匿名内存 touch 后常驻（保证页被计入且不可轻易回收）；
- 标准层级：

```
/sys/fs/cgroup/memory/A          ← 父：dhugetlb.buddy_limit、wmark_ratio 配在这里
├── B   ← 子：dhugetlb.nr_pages 配 dpool，memory.limit_in_bytes = 512M
└── C   ← 子：同 B
```

观测点：`A/dhugetlb.buddy_usage`、`B(C)/memory.usage_in_bytes`、dmesg（OOM/回收日志）。

---

## 1. charge 模型：自己优先、共享兜底（需求 1、2）— P0

### TC-1.1 独占额度内，不动共享配额
- 前置：B limit=512M，buddy_limit 充足。
- 操作：B 内跑 `memhog 300`。
- 预期：`B/memory.usage_in_bytes` ≈ 300M；`A/dhugetlb.buddy_usage` = 0。

### TC-1.2 独占额度不足，超出部分计入共享配额
- 前置：同上。
- 操作：B 内跑 `memhog 700`。
- 预期：B usage ≈ 512M（顶满自己 limit）；buddy_usage ≈ 188M；进程正常存活。

### TC-1.3 多个子 memcg 共享同一池
- 前置：B、C limit 均 512M，buddy_limit 充足。
- 操作：B、C 各跑 `memhog 700`。
- 预期：buddy_usage ≈ 188M × 2 = 376M（两者超出部分之和）。

### TC-1.4 pool 优先的既有行为不变
- 前置：B 的 dpool 空闲 4K 页充足。
- 操作：B 内跑 `memhog 300`。
- 预期：内存来自 dpool（`dhugetlb.nr_pages` 读数可见 pool 用量上升）；buddy_usage = 0。

---

## 2. 容量约束（需求 4）— P0

### TC-2.1 buddy_limit 硬上限生效
- 前置：A buddy_limit=256M，B limit=512M。
- 操作：B 内跑 `memhog 900`（超出部分 388M > 256M）。
- 预期：buddy_usage 顶到 256M 后分配受阻，进入回收/OOM 路径（见 TC-3.x）；不配置 buddy_limit 的对照组可正常跑完。

### TC-2.2 默认不限，行为与现状一致
- 前置：不配 buddy_limit（默认 max）。
- 操作：B 内跑 `memhog 700`。
- 预期：行为同未引入本特性的内核（超限部分计入自己 limit 的既有语义，或由最终设计确认）。

### TC-2.3 buddy_usage 读数准确
- 前置：TC-1.2 状态。
- 操作：`cat A/dhugetlb.buddy_usage`。
- 预期：读数 = 实际共享占用（±10%）。

---

## 3. 回收与 OOM（需求 6 前半）— P0

### TC-3.1 共享配额满，先回收后放行
- 前置：A buddy_limit=256M；B 内有 256M 共享占用且页为冷文件页（可回收）。
- 操作：C 触发共享分配使总量超限。
- 预期：buddy_usage 因回收回落，C 的分配最终成功；无 OOM。

### TC-3.2 回收对象是共享内存的使用者，不只是触发者
- 前置：B 是共享大户（占 200M，页可回收），C 仅少量共享占用。
- 操作：C 触发超限分配。
- 预期：**B 的共享占用被回收**（B 侧观测到使用量下降），C 分配成功；而不是 C 被直接 OOM。

### TC-3.3 回收不动时，memcg OOM 兜底
- 前置：B 的 256M 共享占用全为不可回收匿名页（memhog 常驻），C 触发超限。
- 操作：继续加压。
- 预期：dmesg 出现 memcg OOM kill 日志；被杀进程释放后 buddy_usage 相应回落；系统无全局 OOM、无 hang。

---

## 4. 水线异步回收（需求 6 后半）— P1

### TC-4.1 越过水线提前回收，避免同步 OOM
- 前置：A buddy_limit=256M，`dhugetlb.buddy_wmark_ratio`=80；负载页大部分可回收。
- 操作：B、C 缓慢持续加压（速率低于回收能力）。
- 预期：buddy_usage 越过 ~205M（80%）后 kswapd 活跃（`/proc/vmstat` 或 trace 可见），buddy_usage 被压在水线附近波动，全程无 memcg OOM；对照组（ratio 关闭）出现同步回收/stall。

---

## 5. 共享内存迁回 memcg（需求 5）— P1

### TC-5.1 峰值回落后迁回，释放共享配额
- 前置：B 有 188M 共享占用（TC-1.2 状态）；之后 B 释放了部分独占占用，自己 limit 出现 ≥188M 余量。
- 操作：`echo 188M > B/dhugetlb.buddy_migrate`（语法以最终接口为准）。
- 预期：buddy_usage 下降 ≈188M；B 的 memory.usage_in_bytes 上升 ≈188M；总占用不变。

### TC-5.2 自己额度不足时安全跳过
- 前置：B 有共享占用，但自己 limit 余量不足。
- 操作：写入大于余量的迁移量。
- 预期：按可迁量部分迁移或跳过；进程无异常，配额账目两边一致（buddy_usage + 各 usage 总账平衡）。

---

## 6. cgroup 删除（需求 3）— P0

### TC-6.1 带共享占用的子 memcg 正常删除
- 前置：B 有 100M 共享占用；B 内进程已退出或被杀。
- 操作：`rmdir B`。
- 预期：rmdir 成功不卡死；A 的 buddy_usage 记账正确（占用随页释放归零，或按 reparent 语义正确转移）；再建同名 memcg 可正常使用。

---

## 7. 多 swapfile 与优先级（需求 7）— P1

### TC-7.1 按优先级选择 swap 设备
- 前置：B 绑定两个 swapfile：zram（prio 1）、盘（prio 2）；开启回收场景。
- 操作：触发 B 的匿名页回收。
- 预期：换出页进入 zram（`/proc/swaps` 或 zram 统计可见），盘设备无流量。

### TC-7.2 高优先级设备满后落次级设备
- 前置：zram 容量配小。
- 操作：继续加压使换出量超过 zram 容量。
- 预期：超出部分落入盘设备；业务无 swap 失败。

### TC-7.3 老语法兼容
- 前置：无。
- 操作：`echo <path> > memory.swapfile`（不带 prio，单设备）。
- 预期：行为与现状完全一致。

---

## 8. 基础正确性与回归 — P0

### TC-8.1 配额无泄漏（最重要的一条）
- 前置：执行过 TC-1.2/1.3 任意组合。
- 操作：杀掉所有 memhog，等待回收完成。
- 预期：**buddy_usage 精确归 0**；反复执行 10 轮加压-释放，每轮结束都归 0。

### TC-8.2 非 dhugetlb memcg 零影响
- 前置：建普通 memcg D（不配 dpool、不在 A 子树内）。
- 操作：D 内跑内存压力，越过其 limit。
- 预期：走原生 memory.limit 语义（回收/OOM），全程 buddy_usage 无变化。

### TC-8.3 特性未启用时零影响
- 前置：内核不加 `dynamic_hugetlb=on` 启动。
- 操作：检查接口文件与内存行为。
- 预期：`dhugetlb.*` 接口不可用或写入安全失败；系统行为与不包含本特性的内核一致。

---

## 附：用例与验收优先级汇总

| 功能点 | 用例 | 阶段 |
|---|---|---|
| charge 模型（需求 1、2） | TC-1.1 ~ 1.4 | P0 |
| 容量约束（需求 4） | TC-2.1 ~ 2.3 | P0 |
| 回收与 OOM（需求 6 前半） | TC-3.1 ~ 3.3 | P0 |
| 水线异步回收（需求 6 后半） | TC-4.1 | P1 |
| 迁回 memcg（需求 5） | TC-5.1 ~ 5.2 | P1 |
| cgroup 删除（需求 3） | TC-6.1 | P0 |
| 多 swapfile（需求 7） | TC-7.1 ~ 7.3 | P1 |
| 泄漏与回归 | TC-8.1 ~ 8.3 | P0 |

P0 全部通过 = 初版可交付；P1 为 v2 验收依据。
