# Linux OOM 触发机制

## 1. OOM 是什么

OOM（Out Of Memory）是内核在内存分配彻底失败时，**主动杀掉一个进程以释放内存**的最后兜底机制。

关键点：**内存紧张 ≠ OOM 触发**。系统会先做一系列内存回收尝试，只有所有尝试都失败，OOM 才动作。

---

## 2. 内存分配的决策链

进程申请内存时，内核按顺序尝试，**任何一步成功就返回**：

```
1. 从空闲内存拿                 （正常路径，瞬间完成）
       ↓ 不够
2. 唤醒后台回收线程 kswapd       （异步，调用者不阻塞）
       ↓ 跟不上
3. 调用者自己同步回收            （慢，调用者阻塞几毫秒到几秒）
       ↓ 还是拿不到
4. 触发 OOM                     （最后兜底）
```

第 3 步叫「直接回收」（direct reclaim）：调用者亲自扫描内存，把脏页写回磁盘、把匿名页 swap 出去。回收的快慢取决于磁盘 IO 速度。

---

## 3. OOM 触发的精确条件

不是「内存紧张」，而是「**分配失败 + 所有回收尝试都失败**」。

具体条件：
1. 分配请求拿不到空闲内存
2. 已经走过 direct reclaim（同步回收）
3. **reclaim 完全挤不出任何一页**（不是慢，是真的挤不出来）

### 关键陷阱：能挤出一页就不算失败

这是 OOM 看起来「太保守」的根本原因。

考虑下面场景：
- 系统内存被吃满
- swap 很慢（每秒只能写 100KB ≈ 25 页）
- 100 个进程同时申请内存

每个 alloc 都进 direct reclaim，每个都在等 swap out。但**只要每秒还能挤出去几页，每个等待的 alloc 最终都能拿到一页**——alloc 不算失败，OOM 不触发。

系统进入「稳态慢速死亡」：所有进程都在等回收，看起来挂死，但内核认为「还在产出，正常」。

这种状态可以持续数小时，直到某个 alloc 一次性需求大且撞上 reclaim 完全停滞的瞬间，才真正失败、触发 OOM。

---

## 4. OOM 触发后：杀谁

内核扫描所有进程，给每个打分（`oom_score`，0-1000），杀分数最高的。

打分算法：

```
基础分 = 进程占用内存 / 系统总内存 × 1000
```

**内存占用越大的进程，越优先被杀**。

可以通过 `/proc/<pid>/oom_score_adj`（-1000 ~ 1000）手动调整：

- `-1000`：完全保护，永不杀（用于 sshd 等关键服务）
- `0`：默认
- `1000`：必杀（用于不重要的测试程序）

```bash
echo -1000 > /proc/$(pidof sshd)/oom_score_adj   # 保护 sshd
echo 1000 > /proc/$(pidof test_app)/oom_score_adj # 标记为必杀
```

注意：这个值不持久，进程重启就丢。要持久化用 systemd：

```ini
[Service]
OOMScoreAdjust=-500
```

---

## 5. 怎么让 OOM 触发更快

OOM 触发慢的根本原因是「**回收还能挤出一点就不算失败**」。要让 OOM 来得快，思路是**减少回收缓冲**——让回收路径产出变少、变早失败。

### 5.1 主要旋钮

| 旋钮 | 默认 | 作用 | 让 OOM 更快的方向 |
|------|------|------|------------------|
| **swap 开关** | 开 | 匿名页唯一的回收出口 | 关掉（`swapoff -a`） |
| **`vm.swappiness`** | 60 | swap 倾向（0-100） | 调低（推荐 10） |
| **`vm.watermark_scale_factor`** | 10 | 后台回收启动时机（千分比） | 调低（推荐 5） |

### 5.2 查看与设置命令

```bash
# === swap ===
swapon --show                  # 查看启用的 swap 设备（空 = 已关）
swapoff -a                     # 临时关掉所有 swap
# 永久关：注释 /etc/fstab 里的 swap 行

# === swappiness ===
sysctl vm.swappiness                              # 查看
sysctl -w vm.swappiness=10                        # 立即生效
echo "vm.swappiness = 10" >> /etc/sysctl.d/99-oom.conf && sysctl --system   # 持久化

# === watermark_scale_factor ===
sysctl vm.watermark_scale_factor                              # 查看
sysctl -w vm.watermark_scale_factor=5                         # 立即生效
echo "vm.watermark_scale_factor = 5" >> /etc/sysctl.d/99-oom.conf && sysctl --system   # 持久化
```

### 5.3 `vm.swappiness`

控制回收时倾向 swap 匿名页还是丢 file 页。范围 0-100：

- `0`：几乎不 swap，优先回收 file 页
- `60`（默认）：平衡
- `100`：积极 swap

**调低 = 让内核不愿 swap**，匿名页回收产出变少，alloc 更容易真正失败 → OOM 更快触发。

### 5.4 `vm.watermark_scale_factor`

控制 kswapd（后台回收线程）启动时机。它决定空闲内存三个水位的间距：

```
空闲内存
  ─────  high  ← kswapd 达到这停止
       间距（本参数控制）
  ─────  low   ← 空闲跌到这下，kswapd 启动
       间距
  ─────  min   ← 空闲跌到这下，alloc 失败 → OOM
```

参数是**千分比**（不是百分比），范围 1-1000：

| 值 | 含义 | 64GB 服务器间距 |
|----|------|---------------|
| 10（默认）| 1‰ | 64MB |
| 100 | 10‰ = 1% | 640MB |
| 1 | 0.1‰ | 6.4MB |

**调小** = 间距小 = kswapd 启动晚 = 后台回收少做 = 更多 alloc 进 direct reclaim → direct reclaim 跟不上就触发 OOM。

### 5.5 三种激进程度的配置

```bash
# 方案 A：保守（推荐，保留少量缓冲）
sysctl -w vm.swappiness=10
sysctl -w vm.watermark_scale_factor=10    # 保持默认

# 方案 B：激进（让 OOM 明显更快）
sysctl -w vm.swappiness=10
sysctl -w vm.watermark_scale_factor=5

# 方案 C：最激进（彻底关掉 swap）
swapoff -a
```

**代价**：调小回收缓冲意味着日常负载下 alloc 也更容易进 direct reclaim，**系统整体会变慢**。换 OOM 来得快是要付代价的。

---

## 6. 关键认知

| 误区 | 真相 |
|------|------|
| 内存满了就会 OOM | 内存满了会先回收，OOM 是最后兜底 |
| OOM 杀进程是 bug | OOM 是设计好的兜底机制，杀进程是预期行为 |
| 调 swappiness=0 就不 swap | 仍然会 swap，只是优先级最低 |

**生产环境的正确做法**：用 cgroup 给可能吃内存的进程设上限，让它在自己的 cgroup 内触发 OOM，不影响系统其他部分。

```bash
mkdir /sys/fs/cgroup/test_app
echo 8589934592 > /sys/fs/cgroup/test_app/memory.max   # 8GB 上限
echo 1 > /sys/fs/cgroup/test_app/memory.oom.group      # OOM 杀整个 cgroup
echo $PID > /sys/fs/cgroup/test_app/cgroup.procs
```
