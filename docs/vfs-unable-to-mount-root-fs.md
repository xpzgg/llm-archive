# VFS: Unable to mount root fs on unknown-block(X,Y) 排障指南

## 一句话结论

内核启动时找不到根文件系统。90% 的情况是**启动参数 `root=` 写错了**或**磁盘/文件系统驱动没编进内核也没打包 initramfs**。

---

## 背景：内核是怎么挂载根文件系统的

理解这个报错之前，需要先知道正常情况下内核启动时发生了什么。

### 启动流程简述

```
BIOS/UEFI 固件
    → bootloader（GRUB / U-Boot）
        → 加载内核（vmlinuz）
            → 内核初始化硬件
                → 挂载根文件系统（/）    ← 报错就发生在这里
                    → 执行 /sbin/init（PID 1）
                        → 用户空间启动
```

内核启动的最后一步是把根文件系统挂载到 `/`。根文件系统上有 `/bin`、`/lib`、`/sbin/init` 等一切用户态程序，挂载不上根，后续全部无从谈起，所以内核直接 panic。

### 内核挂载根需要什么条件

内核要成功挂载根，三件事缺一不可：

1. **知道根在哪个设备上** — 通过启动参数 `root=/dev/sda2` 或 `root=UUID=xxx` 告诉内核
2. **能访问这个设备** — 需要对应的磁盘驱动（如 AHCI、NVMe）已经加载
3. **能识别设备上的文件系统** — 需要对应的文件系统驱动（如 ext4、xfs）已经加载

三件事任何一件没满足，都会报 `Unable to mount root fs`。

### 驱动加载的两种方式

内核驱动有两种编译方式，这直接影响启动时能不能用：

- **Built-in（`=y`）**：直接编译进内核镜像，内核启动时就可用
- **Module（`=m`）**：编译成独立的 `.ko` 文件，启动后动态加载

关键问题来了：如果磁盘驱动是 module，`.ko` 文件存放在磁盘上的 `/lib/modules/` 目录里。但磁盘驱动没加载，就没法读磁盘——这是个死循环：

```
加载磁盘驱动 → 驱动文件在磁盘上 → 需要磁盘驱动才能读磁盘 → ...
```

### initramfs 怎么解这个死循环

Linux 的解法是 initramfs（initial RAM filesystem）：bootloader 把内核和一个压缩包一起加载到内存，内核把这个压缩包解压后挂载为一个临时的内存文件系统。这个临时文件系统里包含了必要的驱动和脚本，它负责加载磁盘驱动，找到真正的根设备，然后把根切过去。

```
bootloader 加载内核 + initramfs 到内存
    → 内核解压 initramfs，挂为临时根
        → 执行 initramfs 中的 /init 脚本
            → 脚本加载磁盘驱动（insmod）
                → 找到真正的根设备
                    → switch_root 切到真正的根
                        → 启动 /sbin/init
```

所以正常启动分两个阶段：
- **第一阶段**：initramfs 在内存中，用来加载驱动、找到根设备
- **第二阶段**：切换到真正的根文件系统，进入正常启动

如果磁盘和文件系统驱动都编进了内核（`=y`），可以不用 initramfs，内核自己就能直接挂根。但如果有些驱动是 module（`=m`），就必须有 initramfs，否则就会掉进前面的死循环。

**QEMU 直启动的注意：** 如果你用 `qemu -kernel vmlinuz -hda rootfs.img` 这种方式启动，没有 initramfs 参与，所有磁盘驱动和文件系统驱动必须 `=y` 编进内核，没有别人帮你加载 `.ko`。

---

## 排查地图

看到 `VFS: Unable to mount root fs on unknown-block(X,Y)` 时，**先看设备号 (X,Y)**，它直接告诉你排查方向：

```
unknown-block(X,Y)
        │
        ├── (0,0) ── 内核不知道根设备是谁 ──────── 见第 1 节
        │       │
        │       ├── 启动参数没传 root=           → 1.1
        │       ├── root= 写错了                 → 1.2
        │       └── 磁盘驱动没加载，设备不存在     → 1.3
        │
        └── (非0,0) ── 内核知道设备但挂不上 ────── 见第 2 节
                │
                ├── 文件系统驱动没加载            → 2.1
                ├── 分区不存在或变了              → 2.2
                └── 文件系统损坏                  → 2.3
```

下面按这个地图展开。

---

## 第 1 节：设备号是 (0,0) — 内核不知道根设备是谁

`(0,0)` 意味着内核没拿到有效的根设备信息。对应背景里的条件 1（知道根在哪个设备上）或条件 2（能访问这个设备）没满足。

### 1.1 启动参数没传 root=

内核需要通过启动参数知道根文件系统在哪个设备上。

**怎么确认：**

在正常启动的同环境机器上：

```bash
cat /proc/cmdline
```

看输出中有没有 `root=xxx`。如果是 QEMU 启动，检查 `-append` 参数里有没有 `root=`。

**怎么修：**

- GRUB：编辑 `/etc/default/grub` 中的 `GRUB_CMDLINE_LINUX`，加入 `root=xxx`，然后 `grub-mkconfig -o /boot/grub/grub.cfg`
- U-Boot：修改 bootargs 环境变量
- QEMU：`-append "root=/dev/sda"` 或 `root=/dev/vda`（virtio 磁盘）

### 1.2 root= 写错了

`root=` 参数值不对，指向了一个不存在的设备。

**常见错误：**

- `/dev/sda` 写成了 `/dev/sda1`（或反过来，分区号对不上）
- 用 UUID 方式但 UUID 写错了或分区被重建过
- QEMU 中用了 virtio 磁盘但 root 写了 `/dev/sda`（virtio 磁盘是 `/dev/vda`）

**怎么确认：**

```bash
# 在正常机器上，查看当前根设备和 UUID
lsblk -o NAME,FSTYPE,MOUNTPOINT
blkid
```

核对 `root=` 的值和实际设备是否一致。

### 1.3 磁盘驱动没加载，设备根本不存在

这是自编译内核最常见的原因。`root=` 参数没问题，但内核里没有对应的磁盘驱动，所以设备节点根本没出现。

**典型场景：** 内核 `.config` 中磁盘驱动设为了 `=m`（module），但系统没有 initramfs（比如 QEMU 直启动）。

**怎么确认：**

检查 `.config`：

```bash
grep -E 'CONFIG_BLK_DEV_SD|CONFIG_AHCI|CONFIG_NVME|CONFIG_VIRTIO_BLK' .config
```

如果结果是 `=m`，而且没有 initramfs，就是这个问题。

**怎么修（二选一）：**

**方案 A：把驱动编进内核（推荐，QEMU 直启动必须这样做）**

```kconfig
CONFIG_BLK_DEV_SD=y        # SCSI 磁盘驱动（SATA/SAS 都需要）
CONFIG_VIRTIO_BLK=y        # QEMU virtio 磁盘（如果用 -drive if=virtio）
CONFIG_AHCI=y              # AHCI SATA（如果用 -drive if=ide）
CONFIG_ATA=y               # libata 核心
CONFIG_NVME=y              # NVMe SSD

# 文件系统驱动也要 =y
CONFIG_EXT4_FS=y
CONFIG_XFS_FS=y
```

改完后重新 `make` 内核。

**方案 B：制作 initramfs**

```bash
# Debian/Ubuntu
update-initramfs -c -k <内核版本号>

# RHEL/CentOS
dracut --force /boot/initramfs-<内核版本号>.img <内核版本号>
```

然后在 bootloader 配置中确保 `initrd` 行指向了这个文件。

---

## 第 2 节：设备号非 (0,0) — 内核知道设备但挂不上

有具体设备号（如 `(8,2)` 表示主设备号 8 次设备号 2，即 SCSI 磁盘第二个分区）说明内核已经找到了设备，但挂载文件系统失败了。对应背景里的条件 3（能识别文件系统）没满足，或者磁盘本身有问题。

### 2.1 文件系统驱动没加载

内核找到了磁盘分区，但读不懂上面的文件系统。

**典型场景：** 和 1.3 类似，文件系统驱动（ext4、xfs 等）编成了 `=m` 但没有 initramfs。

**怎么确认：**

```bash
grep -E 'CONFIG_EXT4_FS|CONFIG_XFS_FS|CONFIG_BTRFS_FS' .config
```

看是不是 `=m`。

**怎么修：**

同 1.3，要么改成 `=y`，要么用 initramfs。

### 2.2 分区不存在或分区号变了

设备号有值但分区对不上，比如原来根在 `/dev/sda2`，后来分区表被改过变成了 `/dev/sda3`。

**怎么确认：**

用 rescue 系统或 live USB 启动后：

```bash
fdisk -l /dev/sda
```

看分区表和你的 `root=` 参数是否匹配。

### 2.3 文件系统损坏

分区在，文件系统类型也对，但文件系统本身坏了。

**怎么确认和修复：**

用 rescue 系统启动后：

```bash
fsck.ext4 /dev/sda2    # 替换为你的根分区设备和对应文件系统工具
```

---

## 自编译内核检查清单

自己编译内核遇到这个问题，按这个清单逐项排查：

- [ ] `.config` 中磁盘控制器驱动设为 `=y`（AHCI / NVMe / VIRTIO_BLK）
- [ ] `.config` 中 `CONFIG_BLK_DEV_SD=y`（SCSI 磁盘，SATA 也依赖它）
- [ ] `.config` 中根分区文件系统驱动设为 `=y`（EXT4 / XFS 等）
- [ ] `CONFIG_BLOCK=y`（块设备支持总开关）
- [ ] 启动参数中 `root=` 指向正确的设备（核对 `lsblk` 或 `blkid`）
- [ ] 如果磁盘/文件系统驱动用的 `=m`，确认已制作 initramfs 且 bootloader 配置了 `initrd`
