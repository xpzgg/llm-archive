# AMD KFD / AMDGPU 基础概念

> 背景：分析 amdgpu VM use-after-free 竞态修复时整理的概念笔记

---

### Q1: `kfd_process` 是什么概念？`amdgpu_vm` 是什么概念？

AMD 把 GPU 驱动拆成了两个独立的内核模块，各管一摊：

- **`amdgpu`**（DRM 驱动）：管**显存和页表**——分配显存、建立 GPU 虚拟地址映射、管理 buffer object。这是所有 AMD GPU 驱动都有的通用部分。
- **`amdkfd`**（KFD，Kernel Fusion Driver）：管**计算调度**——创建计算队列、把任务派发到 GPU、管理进程优先级。这是 ROCm 计算栈专用的。

类比到 CPU：`amdgpu` 类似于内存管理子系统（管页表），`amdkfd` 类似于进程调度器（管谁在跑）。

**`struct kfd_process`** 抽象的是 KFD 驱动眼里的"一个用户态计算进程"。一个进程打开 `/dev/kfd` 设备后，KFD 驱动需要知道"这个进程在用哪些 GPU、每个 GPU 上建了什么队列、它的优先级是什么"。`struct kfd_process` 就是这些信息的载体。

**`struct amdgpu_vm`** 抽象的是一块 GPU 虚拟地址空间。GPU 有自己的 MMU，用户态程序分配显存时，GPU 需要页表来做"GPU 虚拟地址 → 物理 VRAM 地址"的翻译。`struct amdgpu_vm` 就是这份 GPU 页表。每个打开 `/dev/dri/renderD128` 的文件描述符背后都有一个 `struct amdgpu_vm`。类比到 CPU，它就是 GPU 版的 `struct mm_struct`。

简单说：**`struct kfd_process` 管的是"谁在算"，`struct amdgpu_vm` 管的是"数据放哪"。**

### Q2: `kfd_process` 和 `amdgpu_vm` 有什么关系？

**一个 `kfd_process` 可以关联多个 `amdgpu_vm`，但一个 `amdgpu_vm` 应该只被一个 `kfd_process` 管理。**

原因很直观：一个计算进程可能用了多张 GPU 卡，每张卡上各有一个独立的 GPU 地址空间（`amdgpu_vm`），但它们都属于同一个进程（`kfd_process`）。反过来，一个 GPU 地址空间只能服务于一个进程——就像一个进程的 `mm_struct` 不会同时是两个进程的地址空间一样。

这两个独立的对象需要一个桥梁来连接。这个桥梁叫 `struct amdkfd_process_info`，它的职责是**协调一个计算进程的显存腾退/恢复**（eviction）——当系统显存紧张时，需要把某个进程的 GPU 数据暂时搬走，腾出空间给别人用，等轮到它再搬回来。这需要同时操作队列（KFD 侧）和页表（`amdgpu` 侧），所以需要一个跨两边的数据结构来协调。

`kfd_ioctl_acquire_vm` 这个 ioctl 就是建立连接的操作：用户态程序告诉内核"把我的这个显存地址空间（`amdgpu_vm`）绑定到我的计算进程（`kfd_process`）上"。内核在 `init_kfd_vm` 里创建桥梁 `struct amdkfd_process_info`，一头挂到 `kfd_process` 上，一头挂到 `amdgpu_vm` 上。

**这次修复的竞态就发生在"建桥"这一步**——两个并发的"建桥"操作试图把各自的桥挂到同一个 `amdgpu_vm` 上，互相覆盖，导致先建的那个桥泄漏，后续清理时访问已释放内存（UAF）。

### Q3: `create_process(current, false)` 中的 `current` 是什么？

`current` 是 Linux 内核的宏，指向当前正在执行内核代码的那个进程的 `struct task_struct`。`create_process(current, false)` 意思是：以当前调用这个 ioctl 的进程为 owner，创建一个 secondary `kfd_process`。

第二个参数 `false` 表示创建的是 secondary（非 primary）`kfd_process`。primary 的 `context_id` 字段会被设为 `KFD_CONTEXT_ID_PRIMARY`，而 secondary 会分配一个新的 ID。

### Q4: `filep` 是什么文件？`private_data` 为什么放 `kfd_process`？

`struct file` 代表内核对一个已打开文件的抽象。用户态每次 `open` 一个设备文件，内核创建一个 `struct file` 实例。这里 `filep` 就是用户态 `open("/dev/kfd")` 返回的文件描述符背后的内核结构。

`struct file` 的 `private_data` 字段是一个 `void *`，是 Linux 驱动开发的通用模式——驱动用这个指针绑定自己任意的数据结构到这个打开的文件描述符上。驱动在 `open` 回调时设置它，后续所有 ioctl 时取出来用。

这里是 `amdkfd` 驱动在 `open("/dev/kfd")` 时把新创建的 `kfd_process` 存入 `private_data`，目的就是让后续所有 ioctl 都能通过文件描述符找到对应的 `kfd_process`。用户态传文件描述符 → 内核查到 `struct file` → 从 `private_data` 拿到 `kfd_process` → 执行操作。这是 Linux 字符设备驱动的标准做法。

而 `kfd_ioctl_create_process` 的问题就在这里——它把 `private_data` 从旧的 `kfd_process`（P1）替换成了新的（P2）。替换后，新 ioctl 拿到 P2（用 P2 的 mutex），但旧 ioctl 可能还在用 P1（用 P1 的 mutex），两把不同的锁无法互相序列化，竞态就产生了。

### Q5: 引入 secondary `kfd_process` 要解决什么问题？

**一个用户态程序想在同一个 GPU 上跑多个互相隔离的计算任务。**

在这之前，一个 Linux 进程打开 `/dev/kfd` 后只对应一个 `kfd_process`。这个进程里所有 GPU 计算任务共享同一套资源——同一个队列管理器、同一个 eviction fence、同一套优先级。问题是：

- **任务之间没有隔离。** 一个任务触发显存腾退（eviction），所有任务都受影响。
- **不能独立管理。** 不能单独给某个任务调优先级、单独调试、单独回收资源。

引入 secondary `kfd_process` 之后，同一个 Linux 进程可以在同一个 `/dev/kfd` 文件描述符上通过 `kfd_ioctl_create_process` 创建多个独立的计算上下文。每个上下文有自己的 `kfd_process`，拥有独立的队列、eviction fence、资源管理。它们之间互不干扰，但都归属同一个用户态程序。

类比：类似于线程 vs 进程的关系。之前一个 Linux 进程在 KFD 里只有一个"计算进程"（所有任务像多线程一样共享一切）。现在允许一个 Linux 进程创建多个"计算进程"（像多进程一样互相隔离）。

**tradeoff：** 隔离带来了管理复杂度——多个 `kfd_process` 可能操作同一个 `amdgpu_vm`（GPU 地址空间），需要正确处理并发。这次 CVE 就是因为并发保护没做好。

### Q6: 为什么要通过打开文件来创建资源？

核心就是利用 VFS（虚拟文件系统）的生命周期管理来管理驱动的资源。Linux 的设计哲学是"一切皆文件"。设备驱动不发明自己的资源管理机制，而是借助 VFS 这套已有的、经过验证的机制：

- **创建：** 用户态 `open("/dev/kfd")` → 内核创建 `struct file` → 驱动在 open 回调里分配资源（`kfd_process`），挂到 `struct file` 的 `private_data` 字段上。
- **使用：** 用户态通过文件描述符发 ioctl → 驱动从 `struct file` 的 `private_data` 取回 `kfd_process` → 执行操作。
- **销毁：** 用户态 `close` 或进程退出（正常退出、被 kill、crash 都一样）→ 内核自动关闭所有文件描述符 → 驱动在 release 回调里清理资源。
- **继承：** `fork` 后子进程继承文件描述符 → 父子共享同一个 `struct file` 实例 → 共享同一份驱动资源。

不这么做的话，驱动就得自己实现一整套资源追踪、引用计数、异常清理机制——这恰恰是 VFS 已经做好的事情。把资源绑定到文件描述符上，驱动就免费获得了：进程异常退出时自动清理（不泄漏资源）、文件描述符的引用计数（多人共享时不提前释放）、文件系统的权限检查。

**一句话：打开设备文件不是目的，而是手段——借 VFS 的生命周期来管理驱动的资源生命周期。**
