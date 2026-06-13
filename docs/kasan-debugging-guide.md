# KASAN 原理与使用指南

**一句话结论：KASAN 是面向 OOB 和 UAF 的动态内存错误检测器；它只能检查受 KASAN 覆盖的内存和访问路径，因此“没有报告”不能证明系统不存在内存问题。**

本文以 ARM64 Linux 为主，说明 KASAN 如何工作、能够发现哪些问题、存在哪些盲区，以及如何正确配置、运行和解读报告。具体行为可能随内核版本变化，应以目标内核的 `Documentation/dev-tools/kasan.rst`、`lib/Kconfig.kasan` 和 `mm/kasan/` 为准。

---

## 1. 背景：KASAN 如何发现非法访问

KASAN（Kernel Address Sanitizer）主要检测两类错误：

- out-of-bounds（OOB）：访问超出对象、数组或变量边界。
- use-after-free（UAF）：对象释放后仍被访问。

**最重要的判断是：KASAN 擅长回答“哪一次 CPU 内存访问越过了合法边界”，不擅长回答“为什么业务状态错了、谁和谁发生了竞争、内存为什么没有释放、设备或硬件写了什么”。**

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

## 2. 使用方法

遇到疑似内存破坏、随机崩溃、NULL 解引用、链表损坏或 UAF 时，可以先使用 KASAN 扫描一遍。KASAN 没有报告不代表没有问题；此时再根据现象考虑 KCSAN、`kmemleak`、KMSAN、IOMMU/SMMU、RAS/EDAC 等工具。

### 2.1 配置和编译

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

### 2.2 启动和运行参数

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

### 2.3 使用 KUnit 验证 KASAN

将 `CONFIG_KASAN_KUNIT_TEST` 编译为模块后，可通过加载测试模块触发官方测试：

```bash
modprobe kasan_test
dmesg -T | grep -E 'BUG: KASAN|kasan_test|KTAP'
```

测试会故意触发多种内存错误，因此看到 KASAN report 是预期行为。运行前应避免使用 `kasan.fault=panic` 或 `panic_on_warn=1`，并启用 `kasan_multi_shot`，否则测试可能在第一个用例后停止。判断测试是否通过应查看 KUnit 的 `ok`、`not ok` 和最终 suite 状态，而不是简单地以“日志中出现 BUG”为失败标准。

若测试编进内核，会在启动阶段运行。也可以使用内核的 `tools/testing/kunit/kunit.py` 在受支持的测试环境中执行和解析结果。

### 2.4 设计有效的复现环境

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

### 2.5 解读 KASAN 报告

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

如果没有 KASAN 报告，不要据此排除内存问题。可继续检查并发竞争、DMA、内存泄漏、未初始化读取和硬件错误，并分别考虑 KCSAN、IOMMU/SMMU、`kmemleak`、KMSAN、RAS/EDAC 等工具。
