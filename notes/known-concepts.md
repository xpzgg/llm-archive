# 已掌握概念索引

> 讲解新内容时，下列概念默认用户已理解，跳过详细解释或一句话带过。

## GPU / AMD

- **`struct kfd_process` / amdkfd 驱动** — KFD（Kernel Fusion Driver）侧的计算进程抽象，管理队列、优先级、eviction fence。对应 `/dev/kfd` 设备。
- **`struct amdgpu_vm` / amdgpu 驱动** — GPU 虚拟地址空间（GPU 页表），管理 GPU 虚拟地址到物理 VRAM 的映射。类比 CPU 的 `struct mm_struct`。对应 `/dev/dri/renderD*` 设备。
- **`struct amdkfd_process_info`** — KFD 和 amdgpu 之间的桥梁对象，协调跨驱动的显存 eviction（腾退/恢复）。
- **`kfd_ioctl_acquire_vm`** — 将一个 `amdgpu_vm` 绑定到 `kfd_process` 的 ioctl，建立两者之间的桥梁。
- **`kfd_ioctl_create_process`** — 在同一 FD 上创建 secondary `kfd_process` 的 ioctl，用于同一进程内的多计算上下文隔离。
- **`file->private_data` 模式** — Linux 驱动通过 `struct file` 的 `private_data` 字段将驱动资源绑定到文件描述符的标准做法。
- **VFS 资源管理** — 借 open/close/fork 的文件生命周期管理驱动资源，免费获得自动清理、引用计数、权限检查。

### 关系与不变量

- **1:N 关系**：一个 `kfd_process` 可关联多个 `amdgpu_vm`（多卡场景），但一个 `amdgpu_vm` 应只被一个 `kfd_process` 管理。
- **secondary kfd_process 的动机**：同一用户态进程内创建多个隔离的计算上下文（独立队列、eviction fence、优先级），代价是引入并发管理复杂度。

## Locking / rtmutex / futex

### 核心问题

- **优先级反转 (Priority Inversion)** — 高优先级任务被低优先级任务间接阻塞。经典场景：低优先级持锁 → 高优先级等锁被阻塞 → 中优先级抢占低优先级 → 高优先级被中优先级间接卡住。
- **优先级继承 (Priority Inheritance, PI)** — 解决反转的机制：临时提升持锁低优先级任务的优先级到等待者同等水平，让其尽快释放锁。rtmutex 就是带 PI 的互斥锁。

### rtmutex 数据结构

- **`struct rt_mutex_base`** — rtmutex 核心结构，维护 `wait_list`（按优先级排序的等待队列）和 `owner`（`struct task_struct *`，当前持锁者）。
- **`struct rt_mutex_waiter`** — 抽象一次等锁事件。`waiter->task`（`struct rt_mutex_waiter` 的字段）指向等待者。挂到 `struct rt_mutex_base` 的 `wait_list` 上。
- **`pi_blocked_on`**（`struct task_struct` 的字段，类型 `struct rt_mutex_waiter *`）— 指向当前阻塞这个任务的 waiter，回答"这个任务在等啥"。为 NULL 表示未被任何 rtmutex 阻塞。
- **`pi_lock`**（`struct task_struct` 的字段，类型 `raw_spinlock_t`）— 保护该任务的 PI 相关状态（`pi_blocked_on` 等）。

### 锁设计

- **per-object 锁 + 固定加锁顺序** — rtmutex 不用全局锁，而是每个对象各持一把锁（`struct rt_mutex_base` 的 `wait_lock` 和 `struct task_struct` 的 `pi_lock`）。操作跨对象状态时按固定顺序同时持有：先 `wait_lock` 再 `pi_lock`。不同 CPU 上操作不同锁链的代码可并行，避免全局锁瓶颈。

### futex

- **futex (Fast Userspace Mutex)** — 用户态锁的内核支持机制。无竞争时纯用户态原子操作（不进内核），只有竞争时才通过系统调用进内核排队睡眠 / 唤醒。
- **`futex_requeue`** — 一个系统调用，把等在 futex A 上的任务直接搬到 futex B 的等待队列上，不唤醒。省掉 N 次无意义的唤醒-重阻塞（内核态↔用户态切换）。
- **proxy lock（代理加锁）** — `futex_requeue` 搬运任务时，需要让睡眠中的任务在新 rtmutex 上排队（保证 PI 链正确）。但任务在睡眠，不能自己操作，所以由调用 requeue 的任务（`current`）代替它完成入队。此时 `waiter->task`（被搬运的任务）≠ `current`（执行搬运的任务）。
