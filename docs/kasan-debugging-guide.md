# KASAN 原理、能力边界与使用指南

**一句话结论：KASAN 是面向 OOB 和 UAF 的动态内存错误检测器；它只能检查受 KASAN 覆盖的内存和访问路径，因此“没有报告”不能证明系统不存在内存问题。**

本文以 ARM64 Linux 为主，说明 KASAN 如何工作、能够发现哪些问题、存在哪些盲区，以及如何正确配置、运行和解读报告。具体行为可能随内核版本变化，应以目标内核的 `Documentation/dev-tools/kasan.rst`、`lib/Kconfig.kasan` 和 `mm/kasan/` 为准。

---

## 1. 背景：KASAN 如何发现非法访问

KASAN（Kernel Address Sanitizer）主要检测两类错误：

- out-of-bounds（OOB）：访问超出对象、数组或变量边界。
- use-after-free（UAF）：对象释放后仍被访问。

KASAN 的基本思路是为内存维护额外的可访问性状态，并在每次内存访问时检查该状态。不同模式对“状态如何保存、检查由谁执行”的实现不同。

### 1.1 Generic KASAN：shadow 内存与编译器插桩

Generic KASAN 使用 shadow 内存记录每个地址是否可访问。每 8 字节被监控内存对应 1 个 shadow byte：

```c
shadow_addr = (kernel_addr >> 3) + KASAN_SHADOW_OFFSET;
```

shadow byte 的核心语义：

| 值 | 含义 |
|---|---|
| `0x00` | 对应的 8 字节全部可访问 |
| `0x01`～`0x07` | 前 N 字节可访问，其余字节不可访问 |
| 负值（最高位为 1） | 整个 8 字节粒度不可访问，具体值区分 redzone、已释放内存等状态 |

当前内核中常见的 Generic KASAN 标记：

| 值 | 常见含义 |
|---|---|
| `0xff` | 已释放 page |
| `0xfe` | 大块 page allocation 的 redzone |
| `0xfc` | slab object redzone |
| `0xfb` | 已释放 slab object |
| `0xfa` | 带 free metadata 的已释放 slab object |
| `0xf9` | global variable redzone |
| `0xf8` | vmalloc/vmap 不可访问区域 |
| `0xf1`～`0xf4` | stack redzone/partial shadow |

> poison 值不是稳定的跨版本接口。分析报告时，以目标内核的 `mm/kasan/kasan.h` 为准。

编译器通过 `-fsanitize=kernel-address` 在受支持的 load/store 前插入检查。outline 模式调用 `__asan_load*()` / `__asan_store*()`，inline 模式直接生成 shadow 检查。逻辑可简化为：

```c
shadow = *kasan_mem_to_shadow(addr);
if (!access_is_valid(shadow, addr, size))
    kasan_report(addr, size, is_write, ip);
```

实际实现还要处理跨 granule 访问、不同访问宽度和架构细节。

### 1.2 分配器集成与 quarantine

KASAN 还需要分配器维护对象状态：

1. 分配对象时，unpoison 对象的合法范围，poison 尾部 padding/redzone。
2. 释放对象时，poison 对象并保存 free 信息。
3. Generic KASAN 可将对象放入 quarantine，暂时不归还 slab freelist，以延迟对象复用。

quarantine 延长的是“对象尚未复用”的时间。对象离开 quarantine 后通常仍保持 poisoned，直到重新分配时才会 unpoison。真正容易漏报的是：原地址已经重新分配给其他对象，此时旧指针访问的地址可能重新变为合法。

### 1.3 Tag-Based KASAN

Software Tag-Based KASAN 使用 ARM64 Top Byte Ignore（TBI），将 tag 放在指针高位，并在 shadow 中保存每个 16 字节内存粒度的 tag。编译器插入的检查负责比较 pointer tag 和 memory tag。

Hardware Tag-Based KASAN 使用 ARM Memory Tagging Extension（MTE）保存和检查 allocation tag。tag 不匹配时由硬件产生 fault，因此运行开销明显低于软件插桩模式。

### 1.4 三种模式如何选择

| 模式 | 配置 | 机制 | 适用场景 | 限制 |
|---|---|---|---|---|
| Generic | `CONFIG_KASAN_GENERIC` | 1:8 shadow、编译器插桩、quarantine | 开发和精确定位 | 开销最高 |
| Software Tag-Based | `CONFIG_KASAN_SW_TAGS` | 1:16 tag shadow、编译器插桩 | ARM64 长时间测试 | 精度受 tag 碰撞影响 |
| Hardware Tag-Based | `CONFIG_KASAN_HW_TAGS` | ARM64 MTE 硬件检查 | 低开销测试或生产监测 | 需要支持 MTE 的 CPU |

Generic KASAN 的 Kconfig 说明给出的典型代价约为 1/8 shadow 内存、额外的分配器元数据和约 3 倍性能下降。实际开销与工作负载、编译器及 inline/outline 模式有关。

Kunpeng 920 不支持 ARMv8.5-A MTE，因此不能使用 HW_TAGS。此平台需要精确调试时通常选择 Generic KASAN。

---

## 2. 排查地图

```text
遇到疑似内存问题
│
├─ 先根据日志或业务现象查第 3 章
│  ├─ 表中写“适合”       → 启用 KASAN，按第 4 章操作
│  ├─ 表中写“部分适合”   → KASAN 与推荐工具同时使用
│  └─ 表中写“不适合”     → 不要等待 KASAN 报告，直接使用推荐工具
│
└─ KASAN 没有报告
   ├─ 不代表没有问题
   └─ 回到第 3 章，根据现象检查其他根因和工具
```

---

## 3. KASAN 的能力边界

本章按下游能够观察到的现象组织。先找最接近的现象，再决定是否使用 KASAN。

表中的结论含义：

- **适合**：建议优先使用 KASAN。
- **部分适合**：KASAN 可能看到最终错误，但不能保证捕获或不能直接定位根因。
- **不适合**：不要以“没有 KASAN 报告”作为排除依据，应直接使用表中的其他手段。
- **不适用**：该现象本身不是 KASAN 要检测的错误。

### 3.1 系统崩溃、Oops 或 panic

| 看到的现象 | 常见根因 | KASAN 是否适合 | 应该怎么做 |
|---|---|---|---|
| 日志包含 `slab-out-of-bounds`、`stack-out-of-bounds`、`global-out-of-bounds` | 数组下标越界、长度计算错误、`memcpy()` 长度过大 | **适合** | 保存完整 KASAN 报告，重点看非法访问栈、对象大小和 allocation 栈 |
| 日志包含 `use-after-free` | 对象释放后，timer、workqueue、IRQ、其他线程或回调仍在使用 | **适合** | 对比 access、allocation、free 三组调用栈，检查释放前是否取消异步任务并完成同步 |
| 日志包含 `double-free` 或 `invalid-free` | 重复释放、释放了对象中间地址、错误路径多执行了一次清理 | **适合** | 检查所有释放路径、错误回滚路径和引用计数 |
| `NULL pointer dereference`，故障地址接近 `0x0` | 未检查返回值、初始化遗漏、对象被并发清空、`ERR_PTR()` 使用错误 | **不适合** | 从 Oops 的 PC、调用栈和寄存器定位故障行；检查 NULL、`IS_ERR()` 和错误路径 |
| `Unable to handle kernel paging request`，地址明显异常 | 指针未初始化、指针字段被写坏、错误地址计算、对象已经失效 | **部分适合** | 先看 Oops 和反汇编；若怀疑更早的越界写或 UAF，再使用 KASAN 寻找写坏点 |
| 崩溃发生在汇编、异常入口或早期启动阶段 | 汇编访问错误、页表/地址转换错误、KASAN 尚未完整初始化 | **不适合** | 查看 ESR/FAR、寄存器和反汇编，检查页表及架构初始化流程 |
| 崩溃点每次不同，栈和链表内容明显损坏 | 更早发生的越界写、UAF、DMA 覆盖或硬件损坏 | **部分适合** | KASAN 用于排查 CPU 代码越界/UAF；同时检查 DMA、RAS/EDAC 和硬件日志 |

### 3.2 数据、指针、链表或结构体内容被写坏

| 看到的现象 | 常见根因 | KASAN 是否适合 | 应该怎么做 |
|---|---|---|---|
| 某个 buffer 尾部或相邻对象被覆盖 | 数组越界、长度或 offset 算错、拷贝长度过大 | **适合** | 使用 KASAN 复现，报告通常直接指向越界写的位置 |
| 同一结构体中一个字段覆盖了另一个字段 | 字段长度使用错误、错误类型转换、结构体内部越界 | **不适合** | 因为访问仍在同一对象内，优先检查字段边界、类型、`FORTIFY_SOURCE` 和 UBSAN |
| 指针偶尔变成 `NULL` 或随机值 | 越界写、UAF、并发修改、DMA 覆盖、硬件 bit flip | **部分适合** | KASAN 只能排查 CPU 越界和 UAF；并行使用 KCSAN、DMA/IOMMU 和 RAS 检查 |
| 链表报 `list corruption`，或遍历时死循环 | 重复加入/删除链表、对象提前释放、并发操作缺少锁、内存覆盖 | **部分适合** | 使用 KASAN 查 UAF/OOB，结合 `CONFIG_DEBUG_LIST`、KCSAN/lockdep 和链表操作审计 |
| 数据在设备完成 DMA 后被破坏 | DMA 长度、方向、descriptor 或 buffer 生命周期错误 | **不适合** | 检查 DMA 映射和 descriptor；查看 SMMU/IOMMU fault；必要时缩小映射或增加 guard page |
| 只有特定机器出现单 bit 或随机数据变化 | 内存、总线、CPU cache 或设备硬件错误 | **不适合** | 查看 RAS、EDAC、ECC、MCE/SEA 日志并做硬件诊断 |

### 3.3 问题只在并发或压力场景出现

| 看到的现象 | 常见根因 | KASAN 是否适合 | 应该怎么做 |
|---|---|---|---|
| 高并发时偶现 UAF | 一个 CPU 释放对象，另一个 CPU 或异步回调仍在使用 | **部分适合** | KASAN 可确认 UAF 结果；使用 KCSAN、锁/RCU/refcount 审计寻找竞争根因 |
| 多线程读写同一字段，结果偶尔错误但地址合法 | data race、缺锁、内存顺序错误 | **不适合** | 使用 KCSAN、lockdep 和 tracing，检查同步关系 |
| 增加延时、打印日志后问题消失 | 竞争窗口或时序问题 | **不适合** | 减少侵入式日志，使用 tracepoint/ftrace/KCSAN 记录竞争双方 |
| 压力运行一段时间后随机崩溃 | UAF、对象复用、内存泄漏、DMA 或硬件问题 | **部分适合** | 先用 KASAN 排查 OOB/UAF，再根据现象检查 `kmemleak`、DMA 和 RAS |

### 3.4 内存持续增长、分配失败或性能下降

| 看到的现象 | 常见根因 | KASAN 是否适合 | 应该怎么做 |
|---|---|---|---|
| 可用内存持续下降，卸载业务后不恢复 | 内存泄漏、引用未释放、缓存没有回收 | **不适合** | 使用 `kmemleak`、slab 统计、`page_owner` 和对象分配/释放计数 |
| `kmalloc()`、page allocation 失败 | 内存不足、碎片、高阶页不足、泄漏或 reclaim 异常 | **不适合** | 查看 OOM 日志、`/proc/buddyinfo`、`/proc/slabinfo`、`page_owner` 和 reclaim 路径 |
| slab cache 数量持续增加 | 对象泄漏、引用计数未归零、销毁路径遗漏 | **不适合** | 比较 alloc/free 数量，使用 `kmemleak` 和对象生命周期 tracing |
| 开启 KASAN 后内存和 CPU 开销明显增加 | shadow、对象元数据、quarantine 和访问插桩开销 | **不适用** | 这是 KASAN 的正常开销；仅在测试环境使用 Generic KASAN，必要时选择 outline 或 Tag-Based KASAN |

### 3.5 数据值异常，但没有越界或崩溃

| 看到的现象 | 常见根因 | KASAN 是否适合 | 应该怎么做 |
|---|---|---|---|
| 第一次读取对象时值随机 | 内存未初始化、初始化分支遗漏 | **不适合** | 使用 KMSAN（平台支持时）、编译器告警，并检查所有初始化路径 |
| 指针指向了错误对象，但地址可正常访问 | 对象查找错误、类型混淆、旧地址已被新对象复用 | **不适合** | 给对象增加 magic/generation，记录对象生命周期，检查类型转换和查找逻辑 |
| 长度、大小或地址计算结果异常 | 整数溢出、符号转换、单位换算错误 | **部分适合** | 使用 UBSAN、`check_*_overflow()` 和边界检查；KASAN 只能捕获其最终造成的部分 OOB |
| 引用计数不正确，但尚未发生 UAF | get/put 不配对、错误路径漏 put 或多 put | **不适合** | 使用 `refcount_t`、增加 get/put tracing，审计对象 ownership |

### 3.6 为什么 KASAN 可能没有报告

即使问题属于越界或 UAF，以下情况也可能没有 KASAN 报告：

| 原因 | 说明 | 下一步 |
|---|---|---|
| 问题路径没有在本次测试中执行 | KASAN 是动态工具，只检查真正运行到的代码 | 增强复现负载和覆盖率 |
| 访问发生在汇编或关闭插桩的函数中 | 该次 load/store 没有 KASAN check | 检查 Makefile、`noinstr`、`__no_sanitize_address` 和反汇编 |
| DMA/设备直接写内存 | 设备不执行 CPU 插桩 | 使用 IOMMU/SMMU 和设备调试手段 |
| UAF 地址已经分配给新对象 | 地址重新变为合法，旧指针访问可能通过检查 | 使用对象 generation、SLUB debug、KFENCE 和生命周期 tracing |
| vmalloc 未启用 KASAN 覆盖 | 相关 shadow 可能允许访问 | 启用 `CONFIG_KASAN_VMALLOC` |
| 错误发生在同一合法对象内部 | KASAN 通常只知道整个对象是否合法 | 检查字段边界、类型和业务状态 |
| 根因是 race、泄漏、未初始化或硬件错误 | 这些不属于 KASAN 的主要检测模型 | 按上表选择 KCSAN、`kmemleak`、KMSAN 或 RAS/EDAC |

最重要的判断是：**KASAN 擅长回答“哪一次 CPU 内存访问越过了合法边界”，不擅长回答“为什么业务状态错了、谁和谁发生了竞争、内存为什么没有释放、设备或硬件写了什么”。**

---

## 4. 使用方法

### 4.1 配置和编译

用于开发调试的 Generic KASAN 基础配置：

```text
CONFIG_KASAN=y
CONFIG_KASAN_GENERIC=y
CONFIG_KASAN_INLINE=y          # 更快、镜像更大；也可选择 KASAN_OUTLINE
CONFIG_KASAN_STACK=y
CONFIG_KASAN_VMALLOC=y         # 架构支持时启用
CONFIG_STACKTRACE=y
CONFIG_KASAN_EXTRA_INFO=y      # 可选：记录 alloc/free CPU 和时间戳
```

若要运行官方自检，还需要：

```text
CONFIG_KUNIT=y
CONFIG_KASAN_KUNIT_TEST=m      # 也可编进内核
```

检查目标配置：

```bash
grep -E 'CONFIG_(KASAN|KUNIT|STACKTRACE)' .config
make olddefconfig
make -j"$(nproc)"
```

### 4.2 启动和运行参数

常用参数：

| 参数 | 作用 |
|---|---|
| `kasan_multi_shot` | 允许输出多次 KASAN 报告 |
| `kasan.fault=report` | 报告后继续运行，默认行为 |
| `kasan.fault=panic` | 发现错误后 panic |
| `kasan.fault=panic_on_write` | 非法写时 panic |

Tag-Based KASAN 还支持 `kasan.stacktrace=` 等参数；HW_TAGS 支持 `kasan.mode=sync/async/asymm`、采样和运行时开关。调试时优先使用同步检查和完整采样，否则报告可能延迟或漏掉被采样跳过的 allocation。

启动后确认配置和日志：

```bash
if [ -r /proc/config.gz ]; then
    zcat /proc/config.gz
else
    cat "/boot/config-$(uname -r)"
fi | grep -E 'CONFIG_(KASAN|STACKTRACE|KUNIT)'

cat /proc/cmdline
dmesg -T | grep -i kasan
```

### 4.3 使用 KUnit 验证 KASAN

将 `CONFIG_KASAN_KUNIT_TEST` 编译为模块后，可通过加载测试模块触发官方测试：

```bash
modprobe kasan_test
dmesg -T | grep -E 'BUG: KASAN|kasan_test|KTAP'
```

测试会故意触发多种内存错误，因此看到 KASAN report 是预期行为。运行前应避免使用 `kasan.fault=panic` 或 `panic_on_warn=1`，并启用 `kasan_multi_shot`，否则测试可能在第一个用例后停止。判断测试是否通过应查看 KUnit 的 `ok`、`not ok` 和最终 suite 状态，而不是简单地以“日志中出现 BUG”为失败标准。

若测试编进内核，会在启动阶段运行。也可以使用内核的 `tools/testing/kunit/kunit.py` 在受支持的测试环境中执行和解析结果。

### 4.4 设计有效的复现环境

1. 使用与故障环境一致的驱动、固件、CPU 数量和负载。
2. 确保目标代码没有通过 Makefile、`noinstr` 或函数属性关闭 KASAN。
3. UAF 难复现时增加并发、循环次数和内存压力，以改变对象复用时序。
4. 保留完整串口或持久化日志，避免只截取 `BUG: KASAN` 附近几行。
5. 测试系统可使用 `kasan_multi_shot` 收集多个现场；生产系统应评估继续运行造成二次破坏的风险。

收集日志：

```bash
dmesg -T > /tmp/kasan.log
grep -n -A140 -B10 'BUG: KASAN' /tmp/kasan.log
```

### 4.5 解读 KASAN 报告

按以下顺序阅读：

1. **报告标题**：如 `slab-out-of-bounds`、`use-after-free`。标题是最佳推断，不保证就是最终根因。
2. **访问类型**：`Read/Write of size N`，确认读写方向和宽度。
3. **非法访问栈**：定位实际执行非法 load/store 的代码。
4. **对象说明**：确认地址位于对象内部、左侧还是右侧，以及所属 cache 和对象大小。
5. **allocation 栈**：确定对象来源和初始化路径。
6. **free 栈**：UAF 场景下确定生命周期结束位置。
7. **shadow/tag dump**：判断命中的是 partial granule、redzone 还是 freed 状态。

使用带调试信息的 `vmlinux` 定位源码：

```bash
scripts/faddr2line vmlinux 'faulting_function+0xoffset'
addr2line -e vmlinux -fip 0xffff8000xxxxxxxx
```

优先使用日志中的 `function+offset`。若直接使用运行时绝对地址，必须先处理 KASLR relocation。

OOB 报告应重点对比访问宽度、对象边界和索引计算。UAF 报告应将 access、allocation、free 三条时间线放在一起，检查 timer、workqueue、IRQ、RCU、引用计数和错误路径是否正确结束对象生命周期。

如果没有 KASAN 报告，回到第 3 章按实际现象选择其他工具。不要仅凭“没有报告”排除内存问题。
