reader和writer不需要同步，reclaimer和reader需要同步。实际上是一种多版本并发控制。write不是覆盖，而是创建新版本，老版本需要等之前的所有reader都退出后，才能被reclaim。

**收益**：读多写少场景，因为读写之间完全不用同步，所以性能接近无锁。

**开销**：维护多个版本；写者需要额外分配内存拷贝数据。

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

The updater uses this function to assign a new value to an RCU-protected pointer, in order to safely communicate the change in value from the updater to the reader. This macro does not evaluate to an rvalue, but it does execute any memory-barrier instructions required for a given CPU architecture.

**Barrier 的作用**：`rcu_assign_pointer` 内部插入了 write barrier（`smp_store_release` 语义），保证先完成新结构体所有字段的初始化，再让指针指向新结构体。如果没有 barrier，CPU 或编译器可能重排序，导致 reader 通过新指针看到未初始化的字段。

对应地，`rcu_dereference` 内部插入了 read barrier（`smp_load_acquire` 语义），确保 reader 拿到指针后，后续对该结构体字段的读取不会被重排到指针读取之前。

### rcu_dereference()

The reader uses [`rcu_dereference()`](https://www.kernel.org/doc/html/v5.10//core-api/kernel-api.html#c.rcu_dereference) to fetch an RCU-protected pointer, which returns a value that may then be safely dereferenced.

Note that [`rcu_dereference()`](https://www.kernel.org/doc/html/v5.10//core-api/kernel-api.html#c.rcu_dereference) does not actually dereference the pointer, instead, it protects the pointer for later dereferencing. // 实际上只是拿了指针当前版本的值，没有解引用

If you are going to be fetching multiple fields from the RCU-protected structure, using the local variable is of course preferred. Repeated [`rcu_dereference()`](https://www.kernel.org/doc/html/v5.10//core-api/kernel-api.html#c.rcu_dereference) calls look ugly, do not guarantee that the same pointer will be returned if an update happened while in the critical section, and incur unnecessary overhead on Alpha CPUs.

## reclaimer

> Grace period = 从调用 `synchronize_rcu()` 到所有已存在的 reader 退出临界区的时间窗口。只有 grace period 结束后，旧数据才能安全回收。

### synchronize_rcu()

Marks the end of updater code and the beginning of reclaimer code。It does this by blocking until all pre-existing RCU read-side critical sections on all CPUs have completed。保证在这之前的rcu 临界区都已退出 Note that [`synchronize_rcu()`](https://www.kernel.org/doc/html/v5.10//core-api/kernel-api.html#c.synchronize_rcu) will **not** necessarily wait for any subsequent RCU read-side critical sections to complete.

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
