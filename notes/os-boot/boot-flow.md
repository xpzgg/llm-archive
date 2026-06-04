# BIOS Done 到 OS 启动成功：关键流程

## 1. 总体流程

```
BIOS/UEFI done
  → 加载 bootloader（GRUB/U-Boot/efibootmgr）
  → bootloader 加载内核镜像 + initramfs 到内存
  → 跳转到内核入口
  → 内核启动（head.S → start_kernel）
  → 挂载根文件系统
  → 执行 /sbin/init
  → 用户态初始化（systemd/sysvinit）
  → OS 启动完成
```

## 2. 各阶段说明

### 2.1 BIOS/UEFI → Bootloader（或直接 → 内核）

BIOS/UEFI 完成硬件初始化后，需要把内核和 initramfs 加载到内存并跳转。这有两种模式：

**有独立 bootloader 的场景：**
- **UEFI + GRUB**：BIOS 从 EFI System Partition 加载 GRUB，GRUB 再加载内核和 initramfs
- **U-Boot**：常用于 ARM/嵌入式，U-Boot 加载内核和 initramfs 后 booti 跳转

**无独立 bootloader 的场景（如 FPGA 内存启动）：**
- BIOS 完成硬件初始化后，**直接**把内核和 initramfs 搬到内存指定地址，然后跳转到内核入口
- BIOS 自身充当了 bootloader 的角色，不需要 GRUB/U-Boot 这个中间层

**关键点**：无论有没有独立 bootloader，"把内核和 initramfs 放到内存正确位置、传启动参数、跳转"这件事必须有人做。bootloader 的职责就是这个，它不关心内核内部逻辑。

### 2.2 Bootloader → 内核入口

bootloader 跳转到内核后，执行的是 `arch/arm64/kernel/head.S`（ARM64 场景）。

这一阶段是汇编，做最底层的准备工作：
- 建立 MMU 页表（identity mapping）
- 开启 MMU
- 跳转到 C 代码 `start_kernel()`

### 2.3 start_kernel()：内核主初始化

这是内核启动的核心，函数在 `init/main.c`。按顺序做了大量初始化：

```
setup_arch()          → 架构相关初始化（内存布局、设备树解析）
trap_init()            → 异常向量表
mm_init()              → 内存管理子系统
sched_init()           → 调度器
workqueue_init()       → 工作队列
drivers 初始化         → 各种子系统和驱动
rest_init()            → 创建内核线程 kernel_init（PID 1）
```

`rest_init()` 最后创建 `kernel_init` 线程，这就是未来的 PID 1。

### 2.4 kernel_init：挂载根文件系统

`kernel_init` 线程的关键路径（`init/main.c`）：

```
kernel_init()
  → kernel_init_freeable()
      → 先解压 initramfs 到 rootfs
      → 检查 rootfs 中是否有 /init
          ├── 有 /init → 执行它，由 /init 负责后续
          └── 没有 /init → prepare_namespace()
              → mount_root() → 挂载 root= 指定的设备
  → execve("/sbin/init") → 切换到用户态 PID 1
```

### 2.5 /sbin/init：用户态初始化

内核挂载完根文件系统后，`execve("/sbin/init")` 进入用户态。`/sbin/init`（通常是 systemd）负责：
- 读取配置，启动各种服务
- 设置网络、挂载额外文件系统
- 最终到达 login prompt 或目标运行级别

---

## 3. initramfs / rootfs / 真实根文件系统的关系

这是最容易混淆的部分。

### 3.1 三个概念

| 概念 | 是什么 | 生命周期 |
|------|--------|----------|
| **rootfs** | 内核内嵌的一个 tmpfs/ramfs，始终存在，是最初的根 | 内核启动时自动创建，始终挂载在 `/` |
| **initramfs** | bootloader 传给内核的一个 cpio 归档（如 fs.cpio.gz） | 内核解压到 rootfs 中，内容成为初始的 `/` |
| **真实根文件系统** | 磁盘上的 ext4/xfs 等文件系统 | 被 mount 到 `/` 上，替代 rootfs 成为新的根 |

### 3.2 它们之间的流转过程

```
阶段1：内核启动，自动创建 rootfs（tmpfs，挂载在 /）
        此时 / 是空的内存文件系统

阶段2：内核把 initramfs（cpio archive）解压到 rootfs 中
        此时 / 有了内容：/init、/bin、/lib、/dev 等

阶段3（有磁盘的场景）：
        /init（initramfs 里的程序）执行
          → 加载磁盘驱动
          → mount 磁盘分区到某个挂载点（如 /mnt/root）
          → switch_root /mnt/root  ← 把根从 rootfs 切换到磁盘
          → exec /sbin/init        ← 在磁盘文件系统上启动 init

        切换后：
          / 指向磁盘上的 ext4/xfs
          rootfs（tmpfs）被卸载或遗忘
          initramfs 的内容从内存中释放

阶段3（无磁盘的场景，如 FPGA 内存启动）：
        /init 执行
          → 直接在 rootfs 上完成所有初始化
          → exec /sbin/init 或直接 exec shell
          → rootfs + initramfs 的内容就是最终的根文件系统
          → 没有切换，没有磁盘参与
```

### 3.3 一句话总结

**rootfs 是内核自带的空壳，initramfs 是往这个空壳里填充的内容，真实磁盘文件系统是最终要切换到的目标。** 在无磁盘场景下，initramfs 就是最终态。

### 3.4 switch_root 做了什么

`switch_root`（或 `pivot_root`）的核心操作：
1. 把当前根（rootfs）挂载到新根（磁盘）的某个子目录下
2. `chroot` 到新根
3. 卸载旧的 rootfs
4. `exec /sbin/init` 替换当前进程

之后 `/` 就是磁盘文件系统了，initramfs 的内存被释放。

---

## 4. FPGA 内存启动场景的典型流程

```
U-Boot（bootloader）
  → 加载 Image（内核）到内存地址 A
  → 加载 fs.cpio.gz（initramfs）到内存地址 B
  → 传 bootargs: "root=/dev/ram0 console=ttyAMA0 ..."
  → bootm / booti 跳转到内核

内核
  → 解压 fs.cpio.gz 到 rootfs
  → rootfs 中有 /init → 执行 /init
  → /init 做 mount proc、sys、devtmpfs 等基础挂载
  → /init exec /sbin/init（或直接启动 shell）
  → 系统就绪
```

这种场景下 **initramfs 就是最终根文件系统**，没有 switch_root，没有磁盘。

---

## 5. 各层之间的配套关系（耦合度）

```
BIOS ←弱→ Bootloader ←中→ Kernel ←强→ Initramfs ←弱→ OS 用户态
                                              ↑
                                     同时 Kernel ←弱→ OS 用户态
```

### 5.1 最强：Kernel ↔ Initramfs

内核配置决定了 initramfs 里必须有什么。**耦合的根源是 `=m` 模块：**

- 内核某个驱动 `=m`（模块）→ initramfs 必须包含对应的 `.ko`，否则启动时找不到设备
- 内核某个驱动 `=y`（内建）→ initramfs 不需要这个 `.ko`
- `.ko` 的版本号、CRC 校验必须和内核严格匹配，insmod 不兼容的模块直接报错

所以内核和 initramfs **基本上是一对一绑定，必须一起构建、一起发布。**

**但如果所有驱动都 `=y`（内建），这个强耦合就不存在了。** 此时 initramfs 只提供 `/init`、`/bin`、`/lib` 等用户态程序，它们和内核之间只有 syscall ABI 的弱耦合，换内核基本没事。

### 5.2 中等：Bootloader ↔ Kernel + Initramfs

Bootloader 需要知道内核和 initramfs 在内存中的地址、大小，以及传什么 bootargs。但 bootloader 不关心内核版本和 initramfs 内容，它只是搬运工。

### 5.3 弱：BIOS ↔ Kernel

单向依赖。BIOS 提供硬件描述信息（ACPI 表 / ARM 的设备树），内核启动时读取。BIOS 不关心上面跑什么内核。换内核不需要换 BIOS，反之亦然。

### 5.4 最弱：BIOS ↔ OS

基本没有关系。BIOS 在内核接管后就退出了，OS（用户态）感知不到 BIOS。

### 5.5 弱：Kernel ↔ OS 用户态

通过 syscall ABI 交互。只要内核不破坏 syscall 兼容性，同一个用户态可以跑在不同内核版本上。

### 5.6 一句话总结

**kernel 和 initramfs 是强绑定的一对（前提是有 `=m` 模块），其他层之间都是松耦合。** 回片联调时如果启动出问题，优先查这对组合是否匹配。
