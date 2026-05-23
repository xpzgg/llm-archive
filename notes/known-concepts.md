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
