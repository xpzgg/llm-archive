# Linux 启动流程复习笔记

> 定位：复习用。主线 ARM64（服务器 = UEFI/ACPI 路线），x86 仅作对照。
> 结构：按启动顺序一层一层讲，每层回答三个问题：**解决什么问题 → 怎么解决 → 重点/注意**。

---

## 0. 全景链条（每一层只管一件事）

```
上电复位
 │
 ▼
【固件】解决：CPU 从固定入口醒来，如何找到并执行第一个"OS 相关"的程序
 │
 ▼
【Boot Manager】解决：存在多个可启动目标时，选谁
 │
 ▼
【Bootloader】解决：把内核镜像从"磁盘上的文件"变成"可运行的内核"，并移交控制权
 │
 ▼
【内核早期代码】解决：从裸机状态搭出 C 运行环境（页表/MMU/栈），初始化各子系统
 │
 ▼
【initramfs / 挂根】解决：真正的根文件系统怎么挂上来
 │
 ▼
【/sbin/init (PID 1)】解决：用户空间由谁展开
 │
 ▼
内核转入服务模式：syscall / 中断 / 异常时陷回内核，处理完返回用户态
```

关键心法：**理解启动流程 = 理解每一层"继承了什么状态、要补齐什么、交出什么"**。

---

## 1. 固件层：找到第一个"OS 相关"的程序

**解决什么问题**：上电后 CPU 从固定入口（reset vector）开始执行，此时内存没初始化、磁盘上的 OS 还够不着。固件负责完成最原始的硬件初始化，并找到第一个可执行目标。

**怎么解决**：两条技术路线，能力差异巨大——

- **BIOS（老路线）**：不认识任何文件系统，只会把磁盘第一个扇区（MBR 512B）读进内存执行。MBR 可用代码仅 446 字节，放不下文件系统驱动，所以必须**多级 bootstrap 接力**，每级唯一任务是"把下一级读进来"，直到某级认得文件系统：
  ```
  stage1 (boot.img, MBR 446B) → core.img（MBR 后间隙，有 FS 驱动）→ GRUB 主程序 → 内核
  ```
- **UEFI（现代路线）**：**自带文件系统驱动（强制 FAT32）**，自己挂载 ESP 分区、按路径读出 .efi 文件执行。"在磁盘上找到操作系统"这一步固件自己完成，bootstrap 接力链整段砍掉。

**重点/注意**：

- ARM 侧对照：ARM64 服务器（鲲鹏，SBSA 规范）= ARM Trusted Firmware（BL1/BL2/BL31）→ EDK2（即 UEFI）；嵌入式 ARM 的 SoC ROM code 和 BIOS 一样"笨"（只从固定位置读 SPL）→ SPL → U-Boot 逐级接力。
- 固件层在内核眼里只负责两件事：**把内核程序弄到内存里执行、留下信息表（内存图/ACPI 表/DTB）**。细节不深入。

---

## 2. Boot Manager：选谁启动

**解决什么问题**：一台机器上往往存在多个可启动目标（多个系统、多个内核版本、U盘/网络启动）。需要一个角色来决定"这次启动谁"。

**怎么解决**：UEFI 固件内置 Boot Manager——主板 NVRAM 里存着一张启动项表（Boot0000、Boot0001…），每项记录"去哪个 ESP 分区、执行哪个 .efi 文件"，按优先级顺序执行。Linux 下用 `efibootmgr -v` 查看和增删这张表；开机按 F12 弹的选单就是它。

**重点/注意**：

- 它只负责**交权**，不碰内核加载的任何细节。
- BIOS 时代几乎没有这层：固件按启动顺序挨个试设备，找到第一个能跑的就交权，选择逻辑极其简陋。

---

## 3. Bootloader：把内核变成"可运行状态"

**解决什么问题**：被选中的内核此时只是磁盘上的一个文件。要跑起来，得有人把它读进内存、备好它需要的所有外部信息、搭好 CPU 状态，最后把控制权交给它。

**怎么解决**：bootloader（GRUB2、systemd-boot 等）依次完成：

1. 从文件系统读出内核镜像（所以 bootloader 自己要认得 FAT/ext4）
2. 内核 + initramfs 装入内存约定地址
3. 填交接信息（x86: `boot_params`；arm64: DTB 指针 / ACPI 表 + EFI 内存图）
4. 搭好 CPU 状态（模式/异常级别切换、关中断）
5. 跳内核入口，自身随即作废

**重点/注意**：

- **判断归属的标准**：发生在内核入口（x86: `startup_64`；arm64: `__primary_entry`）之前的都是固件+bootloader 地盘；学它们只为知道内核"继承了什么状态"。
- 职能交叉的三个特例：GRUB2 身兼 manager（弹菜单）+ loader 两职；systemd-boot 几乎只做选择，装载靠内核自己的 EFI stub；**EFI stub 路径**下固件直接把内核 Image 当 EFI 应用加载，第三方 bootloader 整个被跳过。

---

## 4. 内核镜像：磁盘上那个文件是什么

**解决什么问题**：编译直接产物 `vmlinux`（ELF、带符号）不适合启动——bootloader 不认 ELF，体积也大。需要按"装载方的协议"重新打包成可启动镜像。

**怎么解决**：`vmlinux` → strip 符号 → 压缩 → 加上自解压代码和架构要求的头部：

- **ARM64（主线）**：`Image`（未压缩原始镜像）或 `Image.gz`（压缩版，解压责任在 bootloader/EFI stub 侧）
- **x86（对照）**：`bzImage`（自解压代码 + 压缩内核 + boot protocol 头 + EFI stub 段）；发行版放进 /boot 时惯例改名为 `vmlinuz`——**就是 bzImage 的马甲，不是独立格式**

**重点/注意**：

- 这些名字的本质：**同一内核按不同架构的装载约定打的包**，区别不在压缩格式。
- 实验配对：QEMU `-kernel` 喂 `Image`/`Image.gz`；GDB `symbol-file` 加载 `vmlinux`。
- vmlinux 符号是虚拟地址，MMU 开启前对不上 → 早期代码用物理地址硬断点（`hbreak *0x...`）。

---

## 5. 交接契约：bootloader 怎么把信息传给内核

**解决什么问题**：内核启动时必须知道一批外部信息——内存多大、命令行参数、initramfs 在内存哪里。内核此时自己没法探测（连内存管理都还没有），必须由 bootloader 按约定格式告知。

**怎么解决**：

- **x86**：`boot_params` 结构（setup_data 链表）。契约文档 = `Documentation/arch/x86/boot.rst`；字段定义在 `arch/x86/boot/header.S`。
- **ARM64 两条路线**：DTB（嵌入式/U-Boot，内存布局和外设拓扑全在设备树里）；**ACPI + EFI 内存图（服务器主线）**，EDK2 → GRUB/EFI stub → 内核。

**重点/注意**：

- `boot.rst` **当字典查，不顺序读**。
- EFI stub 入口 `efi_pe_entry()`（`drivers/firmware/efi/libstub/`）：临时模拟一个 bootloader（收集内存图、填交接参数），然后**与 GRUB 路径汇合到同一个内核入口**——读代码时找到这个汇合点，两条路径就都通了。

---

## 6. 内存图（Memory Map）

**解决什么问题**：物理地址空间不是一整块干净 RAM——里面有"洞"（ROM、MMIO 设备区、固件保留区）。内核若把 MMIO 当普通内存用会直接出错，必须先拿到一张"物理内存用途清单"。

**怎么解决**：清单由固件/bootloader 留下，内核早期解析：

- BIOS 路径：`int 0x15, eax=0xE820` → E820 表 → `boot_params.e820_table`
- UEFI 路径：`GetMemoryMap()` → EFI memory map（x86 上最终汇总成 E820 统一处理）
- 表项 = 起始地址 + 长度 + 类型：`RAM`（可用）/ `RESERVED` / `ACPI`（表所在，用完可回收）/ `NVS` / `UNUSABLE`（坏内存）

**内核消费流程**：
```
setup_arch() → 解析内存图 → 转成 memblock（buddy 就绪前的早期分配器，避开保留区）
     → 后期 buddy 接管；dmesg | grep e820、/proc/iomem 仍可查
```

**重点/注意**：

- 一句话记忆：内存图是固件给内核的"**交房清单**"，memblock 是内核拿清单做的第一件事。
- 源码定位：`arch/x86/kernel/e820.c`；arm64 从 DTB/EFI 图构建 memblock；`include/linux/memblock.h`。

---

## 7. 内核早期启动代码

**解决什么问题**：bootloader 移交时，CPU 处于"裸机继承状态"——没有可用页表（或仅恒等映射）、没有栈、没有 C 环境。内核要自己搭出能跑 C 代码、能初始化子系统的环境。

**怎么解决**（ARM64 主线，按执行顺序）：

```
arch/arm64/kernel/head.S   # __primary_entry / __primary_switched：早期页表、开 MMU、建栈
arch/arm64/kernel/setup.c  # setup_arch()：内存图→memblock、DTB/ACPI 解析起点
arch/arm64/mm/             # VMSAv8-64 页表（TTBR0/TTBR1 分离）
init/main.c                # start_kernel()：子系统初始化，两架构在此殊途同归
```

**重点/注意**：

- head.S 是全汇编，因为这是"没有 C 环境却要搭出 C 环境"的阶段——遇到不懂的汇编查 ARM ARM，不系统学。
- 导读：0xAX《linux-insides》Booting 章（x86 视角，概念可迁移，代码路径以 arm64 为准）+ 笨叔《奔跑吧 Linux 内核》。
- QEMU+GDB 单步这一段是验证理解的最好方式（MMU 开启前按物理地址下硬断点）。

---

## 8. initramfs：挂载真根之前的过渡

**解决什么问题**：**鸡生蛋问题**——根分区在 NVMe/SATA 盘上，访问盘需要驱动，而驱动模块又存在根分区里。内核需要一个不依赖任何磁盘的临时落脚点来加载驱动。

**怎么解决**：bootloader 把压缩 cpio 归档和内核一起加载进内存，内核 `populate_rootfs()` 解包到内存 rootfs（tmpfs）当临时根：

```
/init（脚本 + busybox + 必要模块）
   → 加载存储/FS 驱动 → 找到真根分区 → mount → switch_root 切换 → 丢弃 initramfs
```

**重点/注意**：

- **分水岭**：有 initramfs → 内核先执行里面的 `/init`（脚本，不是 systemd）；没有（驱动全 `=y` + `root=` 直接指定）→ 直接挂真根。嵌入式常走后者；发行版硬件千差万别才依赖 initramfs 按需加载模块。
- 内核**不强制**要求 initramfs，它是发行版的工程方案，不是机制必需品。
- 源码定位：`init/initramfs.c`（解包）、`init/do_mounts.c`（挂根）。

---

## 9. initramfs 的文件格式：澄清一个迷思

**解决什么问题**：`initramfs.img`、`xxx.cpio.gz` 多个名字造成的误解——以为是两种东西。

**答案**：**同一个东西**。`.img` 是发行版的产品名，`.cpio.gz` 是格式描述名（类似 .jar 与 .zip）。内核不管文件名：探测压缩格式（gz/xz/zstd/lz4/不压缩）→ 解出 cpio → 解包进 rootfs。

**重点/注意**：

1. **可能是拼接的多段 cpio**：x86 常见 = 未压缩 early cpio（CPU microcode）+ 压缩主 cpio，直接 `zcat` 可能报错。
2. **initramfs ≠ initrd**（真区别在机制）：initramfs = cpio 解包进 tmpfs，`switch_root` 即弃；initrd = 文件系统镜像当 `/dev/ram0` 块设备挂载，pivot_root 麻烦（遗留机制，`init/do_mounts_initrd.c`）。
3. **可内嵌进内核**：`CONFIG_INITRAMFS_SOURCE` 把 cpio 编进 Image，嵌入式常用。
4. 自制练手：`find . | cpio -o -H newc | gzip > my.img`，QEMU `-initrd` 挂载，`/init` 里打印点东西。

---

## 10. /sbin/init：用户空间的起点（PID 1）

**解决什么问题**：根文件系统就位后，用户空间由谁展开？内核需要一个确定的"第一个程序"。

**怎么解决**：`kernel_init()` → `run_init_process()` 按序尝试：

```c
/sbin/init → /etc/init → /bin/init → /bin/sh   /* 全失败 → panic */
```

- 现代发行版：`/sbin/init` 是 systemd 的符号链接；嵌入式：busybox init / sysvinit
- PID 1 = 所有用户态进程的祖宗 + 孤儿进程回收者（容器 PID 1 问题的根源）

**重点/注意**：

- **这里是内核空间 → 用户空间的分界线**：`start_kernel()` 之前是内核自己玩；exec `/sbin/init` 之后系统意图由用户空间驱动，内核转入"随叫随到"的服务模式。
- 源码定位：`init/main.c` 的 `kernel_init()`。

---

## 11. 自测清单（能默答即过关）

1. 每一层"解决什么问题"能否一句话说清？（固件/manager/bootloader/早期代码/initramfs/PID 1）
2. 为什么 BIOS 需要多级 bootstrap 而 UEFI 不需要？
3. GRUB2、systemd-boot、EFI stub 各兼了哪几层职能？
4. QEMU `-kernel` 和 GDB `symbol-file` 分别喂哪个文件？为什么？
5. 内核怎么知道哪段物理内存能用？memblock 因何出现、何时退场？
6. EFI stub 路径和 GRUB 路径在哪里汇合？
7. 有/无 initramfs 时内核行为的分水岭？`/init` 和 `/sbin/init` 谁是谁？
8. initramfs 和 initrd 的机制差别？
9. 用户空间第一个进程怎么被启动？全失败会怎样？
