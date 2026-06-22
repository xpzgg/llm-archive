# rcu_sync — RCU 之上的轻量读写门控

> 一句话:`rcu_sync` 把 **N 个本来各自要 `synchronize_rcu()` 的写者**合并成 **只等一次 `synchronize_rcu()`**。

源码:`kernel/rcu/sync.c` + `include/linux/rcu_sync.h`,作者 Oleg Nesterov,2015 年引入(commit `cc44ca848f5e`)。

## 1. 解决什么问题(动机)

`synchronize_rcu()` 本身**很贵**——要等所有 CPU 跑过 quiescent state,几十毫秒级。但写者之间常常出现紧密 burst(cgroup 切换、连续 down_write),如果每次都自己 `synchronize_rcu()`,N 个写者付 N 次 GP。

`rcu_sync` 这个机制把这种 burst **合并**:第一个 enter 发起 GP,后续 enter 直接睡 waitqueue 蹭那一次 GP。

引入 commit 的 message 给的等价朴素实现:

```c
struct rcu_sync_struct { atomic_t counter; };

rcu_sync_is_idle:  return atomic_read(&rss->counter) == 0;
rcu_sync_enter:    atomic_inc(&rss->counter); synchronize_sched();
rcu_sync_exit:     synchronize_sched(); atomic_dec(&rss->counter);
```

朴素版本的语义就是 rcu_sync 的语义,只是每个 enter/exit 都各付一次 GP。rcu_sync 通过状态机**优化掉紧密 burst 中多余的 GP**。

## 2. 一个常见误解(澄清)

> "读者 fastpath 完全不用 `rcu_read_lock`,所以 rcu_sync 是为了避开 `rcu_read_lock` 的开销。"

**这是错的。**`rcu_read_lock` 在 `PREEMPT_NONE`/`PREEMPT_VOLUNTARY` 上几乎是空操作,没什么可避开的。`rcu_sync` 的真正价值是 **GP 批量化**,不是避开 `rcu_read_lock`。

percpu-rwsem fastpath 用 `preempt_disable` 而不是 `rcu_read_lock`,只是为了省掉 lockdep/nesting 计数等附加开销,这是次要好处。注释自己都写:"We are in an RCU-sched read-side critical section"——fastpath **仍在 RCU-sched 保护下**,只是换了更便宜的形式。

## 3. 状态机

`struct rcu_sync` 三个字段:`gp_state`(状态机)、`gp_count`(引用计数)、`gp_wait`(等待队列)。

```
GP_IDLE → GP_ENTER → GP_PASSED → GP_EXIT → GP_REPLAY → GP_IDLE
 读者快路   正在等GP    读者已慢路   正在恢复    需再等GP    读者快路
```

- `GP_IDLE`:无写者,读者走 fastpath
- `GP_ENTER`:第一个写者刚 enter,正在等 GP
- `GP_PASSED`:GP 已过,所有现存读者被推到慢路
- `GP_EXIT`:最后一个写者 exit,正在恢复 fastpath
- `GP_REPLAY`:恢复过程中又来了 enter,需要重做 GP

## 4. `rcu_sync_enter()` 详解

```c
void rcu_sync_enter(struct rcu_sync *rsp)
{
    int gp_state;

    spin_lock_irq(&rsp->rss_lock);
    gp_state = rsp->gp_state;
    if (gp_state == GP_IDLE) {
        WRITE_ONCE(rsp->gp_state, GP_ENTER);  // 我来当 GP 发起者
        WARN_ON_ONCE(rsp->gp_count);
    }
    rsp->gp_count++;                           // 不管谁 enter,计数都要 ++
    spin_unlock_irq(&rsp->rss_lock);

    if (gp_state == GP_IDLE) {
        /* 第一个 enter,亲自驱动 GP_ENTER → GP_PASSED */
        synchronize_rcu();
        rcu_sync_func(&rsp->cb_head);
        return;
    }

    /* 已有 enter 在等 GP,蹭它即可 */
    wait_event(rsp->gp_wait, READ_ONCE(rsp->gp_state) >= GP_PASSED);
}
```

### 注释 1:为什么选 `synchronize_rcu` 而不是 `call_rcu`

作者在代码注释里辩护了两种写法的取舍:

| 写法 | 实现 | 等价于 |
|------|------|--------|
| 异步(放弃) | `rcu_sync_call(rsp)` + `wait_event` 等回调 | `call_rcu_hurry(cb)` + 睡 |
| 同步(采用) | `synchronize_rcu()` + 手动调 `rcu_sync_func` | `call_rcu(cb)` 的同步等价物 |

选同步的两个理由:

1. **`synchronize_rcu()` 在特殊场景更快**:
   - `rcu_expedited` 模式:用 IPI 强行让所有 CPU 报告 quiescent state,几毫秒结束。
   - `rcu_blocking_is_gp()` 为 true(典型:单 CPU 在线,任何阻塞本身就是 GP):直接返回,几乎零开销。
   - `call_rcu` 走普通 GP 路径,享受不到这些加速。
2. **early boot 不能用 `call_rcu`**:回调机制依赖 RCU 子系统初始化。但作者也说"this shouldn't happen"——理论上不应该有 early boot 调 enter,这是防御性说明。

### 注释 2:"Not really needed"

```c
synchronize_rcu();
rcu_sync_func(&rsp->cb_head);   // 推进 GP_ENTER → GP_PASSED
/* Not really needed, wait_event() would see GP_PASSED. */
return;
```

意思是:`return` 那行可以省掉,直接落到函数末尾的 `wait_event(... >= GP_PASSED)`——状态已经被推进到 `GP_PASSED`,`wait_event` 会立刻返回。手动调 `rcu_sync_func` 只是为了让状态推进的语义显式化。

## 5. `rcu_sync_exit()` 与 `rcu_sync_func()`

### exit

```c
void rcu_sync_exit(struct rcu_sync *rsp)
{
    spin_lock_irq(&rsp->rss_lock);
    if (!--rsp->gp_count) {                          // 引用计数归零
        if (rsp->gp_state == GP_PASSED) {
            WRITE_ONCE(rsp->gp_state, GP_EXIT);
            rcu_sync_call(rsp);                       // 异步发起恢复 GP
        } else if (rsp->gp_state == GP_EXIT) {
            WRITE_ONCE(rsp->gp_state, GP_REPLAY);     // 恢复中又来过 enter
        }
    }
    spin_unlock_irq(&rsp->rss_lock);
}
```

### `rcu_sync_func`(回调,三个分支处理 GP 期间的交错)

```c
spin_lock_irqsave(&rsp->rss_lock, flags);
if (rsp->gp_count) {
    /* enter 端发起的 GP 完成,推进到 GP_PASSED */
    WRITE_ONCE(rsp->gp_state, GP_PASSED);
    wake_up_locked(&rsp->gp_wait);
} else if (rsp->gp_state == GP_REPLAY) {
    /* exit 期间又来过 enter,重新发起 GP */
    WRITE_ONCE(rsp->gp_state, GP_EXIT);
    rcu_sync_call(rsp);
} else {
    /* 恢复 GP 完成,回到 GP_IDLE */
    WRITE_ONCE(rsp->gp_state, GP_IDLE);
}
spin_unlock_irqrestore(&rsp->rss_lock, flags);
```

三个分支就是处理 enter/exit 在 GP 进行中交错的 corner case——这是状态机最核心的部分。

## 6. 真实用户:percpu-rwsem

`percpu_down_write` 用 rcu_sync 保证切换可见性:

```c
void percpu_down_write(struct percpu_rw_semaphore *sem)
{
    rcu_sync_enter(&sem->rss);                        // 发起 GP,推读者到慢路
    __percpu_down_write_trylock(sem);                 // 置 block=1
    rcuwait_wait_event(&sem->writer,
        readers_active_check(sem),                    // 直接读 percpu 计数器
        TASK_UNINTERRUPTIBLE);
}
```

读者 fastpath(`percpu-rwsem.h:55-72`):

```c
preempt_disable();   // 这就是 RCU-sched 读侧临界区
if (likely(rcu_sync_is_idle(&sem->rss)))
    this_cpu_inc(*sem->read_count);                   // percpu 计数器
else
    __percpu_down_read(sem, ...);                     // slowpath
preempt_enable();
```

percpu-rwsem 自己额外用 percpu 计数器 + `block` 标志,让**写者检测读者退出不需要再等 GP**——直接看计数器归零。`rcu_sync_enter` 在这里的作用是提供 GP 屏障,保证内存可见性的有序性(让 fastpath 读者最终会读到 `block=1`)。

另一个用户:`cgroup_threadgroup_rwsem`(`cgroup.c:1289`)。

## 7. So What(影响)

1. **紧密写者只付一次 GP**——cgroup 等热路径省下大量毫秒级 GP 等待。
2. **读者 fastpath 接近零开销**——`rcu_sync_is_idle()` 就是个 `READ_ONCE`,在 RCU 读侧临界区内调用即可。
3. **代价**:5 状态机 + 引用计数 + waitqueue,enter/exit 交错 corner case 多。
4. **契约**:读者必须自己在 RCU 读侧临界区里调 `rcu_sync_is_idle()`,否则不安全。

## 8. 学习材料

一手材料为主,这个机制小众,没有专门二手教程。

- **`git show cc44ca848f5e`** —— 引入 commit,5 分钟讲清动机。
- **`include/linux/rcu_sync.h`** —— 结构和 fastpath 接口。
- **`kernel/rcu/sync.c`** —— 5 状态转换、引用计数、waitqueue 唤醒。
- **`kernel/locking/percpu-rwsem.c` + `include/linux/percpu-rwsem.h`** —— 真实用户,内存屏障 A/B/C/D 配对的精巧设计。
- **Paul McKenney《Is Parallel Programming Hard, And, If So, What Can You Do About It?》** —— RCU 圣经,虽没专门讲 rcu_sync,但读完前几章自己就能看懂。
