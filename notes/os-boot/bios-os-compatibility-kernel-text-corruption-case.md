# BIOS/UEFI 为什么需要与 OS 配套：一次 kernel text 变零案例

## 结论先行

BIOS 与 OS 通常通过 UEFI、ACPI、SMCCC/PSCI 等标准接口协作，理论上是松耦合的，不需要每升级一次内核就升级 BIOS。但“松耦合”不等于“任意版本组合都兼容”。

对新服务器平台、内部开发版本和厂商扩展功能而言，固件表格式、内存保留范围、错误处理协议及对应的内核驱动可能仍在共同演进。此时所谓“BIOS 与 OS 配套”，本质上是双方对同一套软硬件契约有一致理解，并且该组合经过验证。

不配套造成的后果取决于被破坏的是哪一类契约：轻则某项性能或监控功能不可用，重则中断、IOMMU、内存所有权出现错误，最终表现为随机内存破坏、内核 text 被覆盖或启动崩溃。

本文记录的案例中，静态 `vmlinux` 和 `Image` 里的目标函数代码都正常，但运行时函数入口变成连续的零并触发 ARM64 Undefined instruction。BIOS/内核不配套是一个有现实依据的根因方向，但在完成配套 BIOS 的 A/B 测试和内存范围审计前，不能仅凭版本不配套就认定根因已经闭环。

---

## 1. 案例概况

### 1.1 最终故障

系统执行 `/init` 时发生 panic：

```text
Run /init as init process
Internal error: Oops - Undefined instruction
pc : dup_peer_shared_vma+0x0/0x70
lr : vm_area_dup+0xf0/0x138
Code: 00000000 00000000 00000000 00000000 (00000000)
Kernel panic - not syncing: Oops - Undefined instruction: Fatal exception
```

ARM64 的 `0x00000000` 不是有效的普通执行指令。这里不是函数内部处理了错误数据，而是 CPU 刚进入函数入口就执行了全零内容。

调用路径本身属于正常的 ELF 装载过程：

```text
执行 /init
  -> 装载 ELF interpreter
  -> elf_map()
  -> vm_munmap()
  -> 拆分 VMA
  -> vm_area_dup()
  -> dup_peer_shared_vma()
  -> 函数入口为零，触发 Undefined instruction
```

### 1.2 同一轮启动中的其他异常

日志还包含多类固件相关警告：

```text
ACPI _CPC: Return Package type mismatch
sbsa-uart: IRQ index 0 not found
ACPI thermal: Thermal Zone [TZ00] (-273 C)
IORT: node ... don't map to its
_OSC: platform retains control ... AE_NOT_FOUND
Hardware Error: unknown section type
```

此外，外部 initramfs 也存在异常：

```text
Trying to unpack rootfs image as initramfs...
Initramfs unpacking failed: invalid magic at start of compressed archive
Freeing initrd memory: 409600K
```

这些信息不能单独证明 BIOS 导致了最终 panic，但共同说明当前固件向内核提供的若干接口并不完全符合内核预期，值得优先检查版本配套和启动内存布局。

### 1.3 已经确认的事实

对配套构建产物检查后得到：

```text
ffff800080401240 <dup_peer_shared_vma>:
ffff800080401240: d503201f  nop
ffff800080401244: d503201f  nop
ffff800080401248: 14000010  b ...
```

该函数：

- 位于常驻 `.text`，不在 `.init.text`；
- `vmlinux` 中存在正常 ARM64 指令；
- `Image` 对应偏移 `0x401240` 中也存在相同指令；
- 函数大小与 panic 中的 `/0x70` 一致。

因此可以排除两个初始猜测：

1. 源函数被编译成了全零；
2. 函数因为错误的 `__init` 标记而正常地随 init text 被释放。

问题边界由此收敛为：

```text
磁盘上的 vmlinux/Image 正常
  -> 实际启动的 Image 可能不是该文件
  -> 或加载后的内存内容被覆盖/清零
  -> CPU 最终从全零的 kernel text 取指
```

---

## 2. 服务器上所说的“BIOS”到底包含什么

在 ARM64 服务器语境中，“BIOS”往往是对整套平台固件的统称，不只是一段传统 PC BIOS 代码。它可能包括：

- UEFI 固件：初始化平台并提供启动服务、运行时服务和系统表；
- TF-A/EL3 固件：提供 PSCI、SMCCC、安全世界切换等服务；
- SCP/MCP 等管理固件：管理电源、时钟、温度和部分 RAS 功能；
- RAS/SDEI/GHES 固件：采集硬件错误并向 OS 通知；
- 厂商平台固件：初始化 DDR、互连、PCIe、SMMU、GIC 和加速器；
- bootloader 或 UEFI boot manager：把 kernel、initramfs 和启动参数放到内存并移交控制权。

这些组件未必由一个二进制文件构成，但通常作为一个经过验证的 firmware bundle 发布。只更新其中一部分，也可能形成内部不兼容。

---

## 3. BIOS 在 OS 启动前后分别做什么

### 3.1 启动前：把裸硬件变成可接管的平台

上电后，OS 还不存在。固件必须先完成最低限度的平台初始化：

- 训练并初始化 DDR；
- 初始化 CPU、片上互连和 NUMA 拓扑；
- 初始化 GIC、timer、watchdog 等基础设施；
- 配置 PCIe host bridge、SMMU/IOMMU 等设备；
- 发现启动介质并加载后续启动程序；
- 建立安全启动和度量启动的信任链。

如果这些工作没有完成，内核甚至没有稳定的内存和执行环境。

### 3.2 移交时：描述硬件并划分所有权

固件不能只把 CPU 跳到内核入口，还必须告诉内核“机器是什么样”和“哪些资源归谁”：

- UEFI memory map：哪些物理内存可用，哪些属于 runtime、reserved、ACPI 等；
- ACPI 表或 Device Tree：CPU、NUMA、中断、timer、PCIe、IOMMU 和设备拓扑；
- kernel/initramfs 的加载地址、长度和命令行；
- SMBIOS：平台和硬件身份信息；
- HEST/BERT/ERST/EINJ 等 RAS 描述；
- secure boot、TPM、RMM 等安全能力描述。

这个阶段的核心不是“发现设备”，而是建立一份双方一致认可的资源契约。

### 3.3 OS 接管后：固件并没有完全消失

UEFI boot services 会在 OS 接管后退出，但部分固件能力仍然存在：

- UEFI runtime services 仍可能被内核调用；
- ACPI AML 由内核解释执行；
- PSCI/SMCCC 负责 CPU 上下电、挂起和系统复位；
- PCC/CPPC 参与性能与功耗控制；
- firmware-first RAS 先在固件中处理错误，再通过 GHES/SDEI 通知内核；
- 安全世界或管理处理器仍可异步访问共享内存和硬件。

因此，“进入内核后 BIOS 就完全退出、OS 与 BIOS 无关”只适用于非常粗略的启动模型，不适用于现代服务器。

---

## 4. 为什么 BIOS 要与 OS 配套

配套的对象不是两个版本号，而是接口的**语法、语义和所有权**。

```text
固件初始化硬件
  -> 固件用标准表和调用接口描述平台
  -> 内核按自己的版本规则解释这些信息
  -> 内核建立中断、页表、IOMMU、设备和电源管理状态
  -> 固件与内核继续通过约定好的共享资源协作
```

只要双方对契约理解一致，版本可以不同；一旦理解不一致，即使每一边单独看都“没有明显 bug”，组合起来也可能失败。

### 4.1 标准接口为何仍需要兼容性验证

UEFI、ACPI 和 PSCI 等标准解决的是长期兼容问题，但标准并不能消除所有差异：

- 标准存在不同 revision，旧内核可能不认识新字段；
- 固件可能输出格式合法但语义错误的数据；
- 厂商会在正式标准之外引入私有表、GUID 或能力位；
- 新硬件功能往往需要固件与内核补丁同步开发；
- 内核可能包含只针对特定固件版本验证过的 quirk；
- 一个 firmware bundle 内部也可能出现 UEFI、EL3、SCP 版本不一致。

所以成熟平台通常维护 firmware/OS compatibility matrix。所谓“配套版本”，是已知可以正确协作的组合，而不是宣称 BIOS 和内核天然强绑定。

### 4.2 内部版本为何更容易出现问题

上游稳定内核主要依赖公开、稳定的标准 ABI；内部 kernel 往往还包含尚未标准化的平台特性，例如新的 RAS、机密计算、加速器、IOMMU 或内存管理能力。

这类功能常形成事实上的同步开发关系：

```text
固件新增表字段/能力位/共享内存
         <->
内核新增解析逻辑/驱动/错误处理路径
```

如果只升级一边，内核可能误解字段布局、地址范围或能力状态。此时“版本不配套”就不只是缺少新功能，而可能破坏资源所有权。

---

## 5. 不配套为什么会产生不同类型的故障

| 契约 | 固件提供什么 | 不匹配后的典型后果 |
|---|---|---|
| 内存所有权 | UEFI memory map、reserved memory、ACPI NVS、共享缓冲区 | 内核重复分配固件仍在使用的页，或错误释放 kernel/initrd 所在页，导致静默数据破坏和随机 panic |
| CPU/中断 | MADT、GIC redistributor、ITS、GTDT、PSCI | CPU 启动失败、中断丢失、watchdog 异常、系统卡死 |
| IOMMU/DMA | IORT、SMMU、stream ID、保留映射 | 设备不可用；更严重时 DMA 越界写入内核内存 |
| PCIe | MCFG、`_CRS`、`_OSC`、BAR window | ECAM/BAR 冲突、设备枚举失败、热插拔或 AER 异常 |
| 性能与功耗 | CPPC `_CPC`、PCC、idle/performance state | cpufreq 不工作、性能异常、功耗或调频失控 |
| 温度与时钟 | thermal ACPI objects、GTDT、watchdog | 虚假温度、错误降频、计时异常、意外复位 |
| RAS | HEST/GHES/BERT、SDEI、vendor section | 虚假硬件错误、错误无法上报；共享缓冲区描述错误时还可能破坏内存 |
| 安全能力 | Secure Boot、TPM、RMM、CCA | 验签失败、度量异常、机密虚机能力不可用 |
| 启动交接 | Image/initramfs 地址与长度、命令行 | `invalid magic`、镜像被覆盖、找不到 `/init` 或早期启动崩溃 |

这里最重要的分界是：

- **能力不匹配**通常只会关闭某项功能；
- **格式不匹配**通常表现为解析警告或设备初始化失败；
- **地址和所有权不匹配**可能直接导致任意内存破坏，是最危险的一类。

### 5.1 内存所有权错误为何会变成 kernel text 全零

内核代码所在物理页必须始终被标记为内核占用。假如固件或启动链给出的地址/长度错误，可能形成如下链路：

```text
某段 kernel text 对应的物理页没有被正确保留
  -> 内核页分配器把它当作普通空闲页
  -> initrd 释放、清零分配或设备 DMA 重用了该页
  -> 虚拟地址仍然指向原来的物理页
  -> kallsyms 仍把该地址解析成原函数名
  -> CPU 取到 0x00000000
  -> Undefined instruction
```

这可以解释一个容易困惑的现象：panic 仍然显示正确的函数名，但函数内容已经不存在。符号表描述的是“这个地址原本是什么”，并不保证该地址在运行时没有被破坏。

可能造成这种错误的具体来源包括：

- UEFI memory map 中错误的可用/保留属性；
- initramfs 起始地址或长度错误，与 kernel Image 重叠；
- 固件共享缓冲区未声明为 reserved；
- IORT/SMMU 信息错误导致设备 DMA 到错误地址；
- firmware-first RAS 仍向 OS 已复用的共享页写入；
- bootloader 实际加载了另一份 Image，或加载过程覆盖了部分镜像。

---

## 6. 如何理解本案例与 BIOS 不配套的关系

### 6.1 可以确认的结论

- panic 的直接原因是运行时 kernel text 为零；
- 手头的 `vmlinux` 和 `Image` 在目标偏移处都正常；
- 目标函数属于常驻 `.text`，正常的 `free_initmem()` 不应该释放它；
- 同一启动日志存在多项 ACPI/固件接口异常；
- 外部 initramfs 的格式、地址或内容至少有一项不正确。

### 6.2 合理但尚未证明的推断

BIOS/内核不配套可能使启动链传递错误的内存描述，或者使固件和内核对某段共享内存的所有权理解不同，进而导致目标 text 页被清零或覆盖。

时间上，panic 发生在：

```text
Freeing initrd memory
  -> Freeing unused kernel memory
  -> Run /init
  -> 首次走到 dup_peer_shared_vma()
  -> 入口为零
```

这使“释放范围或内存所有权错误”成为优先方向，但时间相邻不是因果证明。目标函数也可能更早就已经被破坏，只是直到执行 `/init` 才第一次被调用。

### 6.3 不能直接下的结论

不能仅根据“测试同学说版本不配套”就写成：

> BIOS 版本不配套直接清零了 `dup_peer_shared_vma()`。

这个表述跳过了中间机制，也缺少验证。正确的阶段性结论应是：

> 静态内核产物正常，运行时 text 被破坏；日志存在明确的固件接口异常，且当前 BIOS/kernel 不在配套矩阵中。固件提供的启动内存布局或共享资源描述错误是高优先级假设，需要通过配套 BIOS A/B 测试和地址范围审计闭环。

---

## 7. 推荐的排查与闭环方法

### 7.1 先固定所有版本和实际启动物

记录并保存：

- BIOS/UEFI、BMC、TF-A/EL3、SCP/MCP、RMM 等固件版本；
- kernel commit、config、编译器和链接器版本；
- `vmlinux`、`Image`、initramfs 的 SHA-256；
- bootloader 配置、启动命令和完整 kernel command line；
- Server 型号、主板 revision 和 CPU stepping。

只比较 `uname -r` 或 Linux version string 不足以证明镜像来自同一次构建。

### 7.2 确认 bootloader 真正加载的内容

本案例中：

```text
_text                    = 0xffff800080000000
dup_peer_shared_vma      = 0xffff800080401240
Image 内偏移             = 0x401240
```

磁盘 Image 对应字节正常：

```text
1f2003d5 1f2003d5 10000014 3f2303d5
```

还应在 bootloader 跳转前检查 `kernel_load_address + 0x401240`，确认内存里仍是相同内容。若此时已为零，问题在镜像选择、传输、解包或加载阶段。

### 7.3 做配套版本 A/B 测试

最有说服力的实验矩阵是：

| BIOS | Kernel | 结果 |
|---|---|---|
| 配套版本 | 当前 Image/initramfs | 是否稳定启动 |
| 当前版本 | 当前 Image/initramfs | 是否稳定复现 |
| 当前版本 | 已知可用 kernel | 是否稳定启动 |

测试时除目标变量外，initramfs、启动参数和硬件配置必须保持一致。只有可重复的单变量对照，才能把“相关性”提高为有说服力的根因证据。

### 7.4 审计物理地址范围

重点对照以下区间是否重叠，属性是否正确：

- kernel Image 的物理加载范围；
- initramfs 起始地址和长度；
- UEFI reserved/runtime 区域；
- ACPI reclaim/NVS 区域；
- GHES/SDEI/RAS 共享缓冲区；
- RMM/CCA 等安全世界保留区；
- crashkernel、CMA 和设备固件预留区；
- SMMU 保留映射及设备 DMA aperture。

可在诊断版本中开启更完整的 EFI/memblock 日志，并保存 ACPI tables 供离线检查。关键不是只看“地址合法”，而是确认固件和内核对该地址的生命周期及所有权理解一致。

### 7.5 确认 text 在哪个阶段变化

在诊断内核中对目标函数做分阶段 dump 或校验：

```text
bootloader 跳转前
  -> 内核早期入口后
  -> alternatives/ftrace 初始化后
  -> free_initrd_mem() 前后
  -> free_initmem() 前后
  -> 执行 /init 前
```

第一次从正常变为零的位置，就是最有价值的断点。

辅助实验包括：

- 使用 `nokaslr` 固定地址，判断问题是否跟随物理位置；
- 使用已确认有效的最小 initramfs，隔离外部 400 MiB initrd；
- 暂停非必要设备，降低 DMA 干扰；
- 对比关闭或开启特定固件功能后的现象，但不要把规避参数当成最终修复。

---

## 8. 这个案例可以复用的诊断方法

### 8.1 先区分“符号正确”和“代码内容正确”

调用栈能解析出函数名，只说明 PC 落在该符号的地址范围内。`Code:` 才反映 CPU 当时真正看到的指令内容。

```text
正确函数名 + 全零 Code
```

应优先想到 text 被释放、覆盖、未正确加载或映射错误，而不是先分析函数业务逻辑。

### 8.2 同时检查静态产物和运行时内存

排查链条应分层：

```text
源码
  -> vmlinux
  -> Image
  -> 启动介质中的文件
  -> bootloader 加载后的物理内存
  -> 内核运行时虚拟地址
```

任意相邻两层都可能不一致。只检查编译目录里的 Image，无法证明机器真正执行的是同一份内容。

### 8.3 把“版本不配套”翻译成具体机制

“版本不配套”不是最终根因描述。高质量的分析必须继续追问：

- 哪个接口不配套？
- 是字段格式、能力位还是地址范围？
- 谁认为这段资源属于自己？
- 哪个动作最终改变了目标内存？
- 为什么更换配套版本可以避免该动作？

只有回答这些问题，才能形成可修复、可验证、可迁移的结论。

### 8.4 不要把同时出现的异常强行合并

本案例至少存在两个直接可见的问题：

1. 外部 initramfs `invalid magic`；
2. kernel text 在运行时变为零。

二者可能由同一个错误的加载地址/长度引起，也可能完全独立。调查时应分别建立证据链，再检查它们是否在某个共同机制上汇合。

---

## 9. 最终认知

BIOS 与 OS 的关系可以概括为：

> 正常情况下通过标准接口松耦合；新平台和内部功能通过经过验证的契约配套；真正危险的不是版本号不同，而是双方对硬件状态、地址和资源所有权的理解不同。

从影响等级看：

```text
能力位不一致
  -> 功能降级

表格式或语义不一致
  -> 解析警告、设备或性能功能异常

地址与所有权不一致
  -> 内存覆盖、DMA 破坏、随机 panic、无法启动
```

本案例的价值不只是说明“BIOS 要配套”，而是展示了如何从一次 Undefined instruction 出发，通过核对符号、section、`vmlinux` 和 `Image`，把问题从函数逻辑逐步收敛到启动链与运行时内存所有权，并为固件兼容性假设建立可验证的实验路径。
