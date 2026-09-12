RCU 是一种针对**读多写少**场景的同步机制。核心思路：

- **reader 不加锁、不等待**，直接读当前版本的数据
- **writer（也叫 updater）不覆盖旧数据**，而是拷贝一份，修改后发布新版本
- **reclaimer 等 所有 reader 退出后**，再回收旧版本

整个过程 reader 和 writer 完全不互相阻塞，所以读性能接近无锁。代价是 writer 每次要额外分配内存做拷贝，以及维护多个版本直到可以安全回收。

本质上是一种**多版本并发控制**。

## API 总览

| 角色 | API | 作用 |
|------|-----|------|
| reader | [`rcu_read_lock()`](https://www.kernel.org/doc/html/v5.10//core-api/kernel-api.html#c.rcu_read_lock) / [`rcu_read_unlock()`](https://www.kernel.org/doc/html/v5.10//core-api/kernel-api.html#c.rcu_read_unlock) | 标记临界区，告诉 reclaimer 当前还在用 |
| reader | [`rcu_dereference()`](https://www.kernel.org/doc/html/v5.10//core-api/kernel-api.html#c.rcu_dereference) | 安全获取 RCU 保护的指针 |
| updater | [`rcu_assign_pointer()`](https://www.kernel.org/doc/html/v5.10//core-api/kernel-api.html#c.rcu_assign_pointer) | 发布新版本指针，让 reader 可见 |
| reclaimer | [`synchronize_rcu()`](https://www.kernel.org/doc/html/v5.10//core-api/kernel-api.html#c.synchronize_rcu) | 同步等待所有 reader 退出临界区 |
| reclaimer | [`call_rcu()`](https://www.kernel.org/doc/html/v5.10//core-api/kernel-api.html#c.call_rcu) | 异步注册回调，grace period 后执行 |

![image-20260509184038814]()

```c
// The following diagram shows how each API communicates among the reader, updater, and reclaimer.

rcu_assign_pointer() // reader能看到新版本
                        +--------+
+---------------------->| reader |---------+
|                       +--------+         |
|                           |              |
|                           |              | Protect:
|                           |              | rcu_read_lock()
|                           |              | rcu_read_unlock()
|        rcu_dereference()  |              |
+---------+                 |              | // 需要等所有临界区退出，才能回收旧版本
| updater |<----------------+              |
+---------+                                V
|                                    +-----------+
+----------------------------------->| reclaimer |
                                     +-----------+
  Defer:
  synchronize_rcu() & call_rcu() // 同步等待 & 异步回调
```

## reader

### rcu_read_lock()

rcu 读者使用，告诉reclaimer，当前读者还在用，不要释放。

- 用于异步释放内存，所有的reader退出临界区后，才会执行call rcu的回调。
- 临界区内不允许block（上下文切换会被rcu认为临界区已退出。临界区内block，会产生异常，还在临界区，但是rcu认为该cpu已退出临界区。）

### rcu_read_unlock()

通知reclaimer，reader已退出临界区。

## writer

```
/* 直接用 rcu_assign_pointer */
rcu_assign_pointer(g_obj, new_obj); // write

p = rcu_dereference(head.next); // read
return p->data;

/* 更常见：通过链表原语间接使用，内部已经包含了 rcu_assign_pointer 的语义 */
list_add_rcu(&new_obj->list, &g_list);
list_del_rcu(&old_obj->list);
```

### rcu_assign_pointer()

updater 用来给 RCU 保护的指针赋新值，把更新安全地发布给 reader。这个宏不能当右值用，但会在必要的架构上插入内存屏障。

**Barrier 的作用**：`rcu_assign_pointer` 内部插入了 write barrier（`smp_store_release` 语义），保证先完成新结构体所有字段的初始化，再让指针指向新结构体。如果没有 barrier，CPU 或编译器可能重排序，导致 reader 通过新指针看到未初始化的字段。

对应地，`rcu_dereference` 内部插入了 read barrier（`smp_load_acquire` 语义），确保 reader 拿到指针后，后续对该结构体字段的读取不会被重排到指针读取之前。

### rcu_dereference()

reader 用来拿 RCU 保护的指针，拿到后可以安全解引用。

注意 [`rcu_dereference()`](https://www.kernel.org/doc/html/v5.10//core-api/kernel-api.html#c.rcu_dereference) 本身不解引用，只是把指针当前版本的值取出来。

如果要读结构体的多个字段，建议用局部变量存一下指针。反复调 [`rcu_dereference()`](https://www.kernel.org/doc/html/v5.10//core-api/kernel-api.html#c.rcu_dereference) 不好看，而且临界区内如果发生了更新，每次不一定返回同一个指针，Alpha CPU 上还会有额外开销。

## reclaimer

> Grace period = 从调用 `synchronize_rcu()` 到所有已存在的 reader 退出临界区的时间窗口。只有 grace period 结束后，旧数据才能安全回收。

### synchronize_rcu()

updater 代码和 reclaimer 代码的分界线。会阻塞等待所有 CPU 上已存在的 RCU 临界区都退出。注意只等调用前已存在的临界区，之后新进入的不等。

```
        CPU 0                  CPU 1                 CPU 2
    ----------------- ------------------------- ---------------
1.  rcu_read_lock()
2.                    enters synchronize_rcu()
3.                                               rcu_read_lock()
4.  rcu_read_unlock()
5.                     exits synchronize_rcu()
6.                                              rcu_read_unlock()
```

```
/* updater 代码：负责更新数据 */
rcu_assign_pointer(g_obj, new_obj);
synchronize_rcu();          /* ← 这行是分界线 */
/* reclaimer 代码：负责回收旧数据 */
kfree(old_obj);
```

### call_rcu()

异步版本的reclaimer，注册回调，等所有reader退出临界区，执行回调。

## example use

```c
struct foo {
        int a;
        char b;
        long c;
};
DEFINE_SPINLOCK(foo_mutex);

struct foo __rcu *gbl_foo;

void foo_update_a(int new_a)
{
        struct foo *new_fp;
        struct foo *old_fp;

        new_fp = kmalloc(sizeof(*new_fp), GFP_KERNEL);
        spin_lock(&foo_mutex);
        old_fp = rcu_dereference_protected(gbl_foo, lockdep_is_held(&foo_mutex));
        *new_fp = *old_fp;          /* 拷贝旧数据（Copy） */
        new_fp->a = new_a;
        rcu_assign_pointer(gbl_foo, new_fp);  /* 发布新版本 */
        spin_unlock(&foo_mutex);
        /* spinlock 只负责写者之间互斥，RCU 不提供写者间同步 */
        synchronize_rcu();          /* 在锁外等待 grace period，不需要持锁等待 */
        kfree(old_fp);
}

int foo_get_a(void)
{
        int retval;

        rcu_read_lock();
        retval = rcu_dereference(gbl_foo)->a;
        rcu_read_unlock();
        return retval; // 注意这里是值拷贝。如果返回结构体指针，不能在临界区外用，可能会被释放。
}
```

## 适用场景

- **读多写少** — 读写零同步开销，性能接近无锁
- **读者不能 block** — 临界区内不允许 sleep/上下文切换
- **可容忍短暂旧数据** — reader 可能读到更新前的版本（stale data）
- **典型场景**：路由表、ACL 规则、设备配置、内核中的 `task_struct` 指针等

## 常见误区

- **临界区内 sleep/block** — RCU 通过 preempt count 判断临界区是否退出，临界区内 sleep 会让 RCU 误以为该 CPU 已退出，导致 grace period 判断出错
- **临界区外使用 RCU 保护的指针** — 拿到的指针只在临界区内有效，出了临界区数据可能随时被释放（参见 `foo_get_a` 返回值拷贝的注释）
- **多次 `rcu_dereference()` 不用局部变量** — 临界区内可能发生更新，每次调不一定返回同一个指针，读到的字段可能来自不同版本
- **直接修改 RCU 保护的指针指向的对象** — 必须 alloc 新副本、拷贝、修改、发布。不能原地改，因为 reader 可能正在读
- **认为 RCU 能处理写写互斥** — RCU 只解决读写并发，多个 writer 之间需要额外的锁（如 spinlock）来互斥
- **以为 `synchronize_rcu()` 会等所有 reader** — 只等调用前已存在的临界区，之后新进入的不管

## 构建在 RCU 之上的 utility

前面讲的是 RCU 的**基础 API**。内核里还有一些**构建在 RCU 之上的封装**，针对特定场景做了优化或简化。它们本身不改变 RCU 怎么工作，只是把"用 RCU 解决某类问题"的常见模式封装成可复用的工具。

| Utility | 解决什么问题 |
|---|---|
| **`rcu_sync`** | N 个紧密 burst 的 writer 共享一次 GP。第一个 enter 发起 GP，后续 enter 直接睡 waitqueue 蹭那次 GP。详见 [rcu_sync.md](rcu_sync.md) |
| **`rcuwait`** | 基于 RCU 的轻量 wait queue。等对象被 RCU 释放后再唤醒等待者，比 wait_event 简单 |
| **`get_state_synchronize_rcu()` / `poll_state_synchronize_rcu()`** | 异步 GP：拿一个 GP 序号快照，之后轮询是否完成，避免阻塞当前任务 |
| **`call_rcu_hurry()`** | 强制不延迟的 `call_rcu`——绕过 NOCB/lazy 的延迟批处理，立即触发 GP。用于不能容忍回调延迟的场景 |
| **`kfree_rcu()` / `kvfree_rcu()` / `call_rcu(..., kfree_rcu_cb)`** | 一行 API 完成"GP 后释放"，省得手写 kfree callback。`kfree_rcu(p, rh)` 的 `rh` 是为缓存回调结构体预留的字段 |
| **`percpu_rw_semaphore`** | 用 rcu_sync + percpu 计数器实现的读多写少锁。reader fastpath 几乎零开销（`preempt_disable` + percpu 自增），writer 通过 rcu_sync 推 reader 进慢路后查计数器。是 rcu_sync 的主要用户 |

**这组和基础 API 的关系**：基础 API（`rcu_read_lock` / `rcu_assign_pointer` / `synchronize_rcu` / `call_rcu`）是 RCU 提供的能力；这些 utility 是别人用这些能力构造的"打包方案"。如果遇到的问题恰好匹配某个 utility 的设计目的，直接用比自己用基础 API 重新组合更省事、更不容易出错。
