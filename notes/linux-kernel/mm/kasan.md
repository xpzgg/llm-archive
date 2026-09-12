# KASAN：原理、能力与边界

> 一句话：KASAN 用「shadow memory 记录每块内存能不能访问」+「编译器在每次访存前插入检查」，把 OOB/UAF 这类原本随机、滞后、现场缺失的内存错误，变成在非法访问发生瞬间产生的、带完整调用栈的确定性报告。
>
> 最重要的边界：**KASAN 没报问题 ≠ 没问题**。它只覆盖被插桩的 CPU 访问和受管理的内存。

## 1. Why：为什么需要 KASAN

内核是 C 代码，没有边界检查。越界写（OOB）和释放后访问（UAF）往往**不会立刻崩溃**：越界写踩到的可能是 padding 或无辜对象，等受害者读到坏数据时才崩——崩溃栈指向「受害者」而不是「肇事者」，出错点和崩溃点可能隔很远，复现靠运气。

KASAN 的思路：不等错误造成破坏，在非法访存指令执行那一刻直接拦下，把「随机破坏」变成带第一现场和调用栈的「确定性报错」。

## 2. What：能发现什么，不能发现什么

能发现（本质是「哪一次 CPU 访存越过了合法边界」）：

- slab 对象越界（kmalloc 对象读写出界）
- 栈变量、全局变量越界（编译器在栈帧/全局变量周围加 redzone）
- UAF（释放后访问）、部分 double-free

不能发现（排障时要主动想到这些盲区）：

| 盲区 | 原因 | 该用什么 |
|---|---|---|
| DMA / 设备写坏内存 | 不经过 CPU load/store | IOMMU/SMMU |
| 数据竞争 | 每次访问单独看都合法 | KCSAN |
| 内存泄漏 | 没有非法访问，只是没人释放 | kmemleak |
| 读未初始化内存 | 地址合法，内容未初始化 | KMSAN |
| 汇编 / 关闭插桩的代码 | 编译器覆盖不到 | 代码审查 |

另外注意：UAF 检出窗口有限——对象一旦被复用，旧指针的访问地址重新变「合法」就漏报了；tag-based 模式还存在 tag 碰撞漏检。

## 3. How：机制

两部分：shadow memory 记录状态，编译器插桩在每次访存前查询状态。

### 3.1 shadow memory：用 1/8 内存记录可访问性

每 8 字节被监控内存（一个 granule）对应 1 个 shadow byte：

```
shadow_byte = (target_addr >> 3) + KASAN_SHADOW_OFFSET
```

shadow byte 的语义（Generic KASAN）：

| 值 | 含义 |
|---|---|
| `0x00` | 8 字节全部可访问 |
| `0x01`～`0x07` | 前 N 字节可访问（对象尾部不足 8 字节时） |
| 负值 | 整个 granule 不可访问；具体值区分 redzone（`0xFC`）、已释放 slab 对象（`0xFB`）等 |

> poison 具体取值随版本变化，以目标内核 `mm/kasan/kasan.h` 为准。

**shadow 谁来写**：堆对象由分配器写——分配时 unpoison 合法范围、poison redzone；释放时 poison 整个对象并记录 free 栈；quarantine 把释放对象隔离一段时间再复用，拉大 UAF 检出窗口。栈和全局变量边界编译期已知，redzone 由编译器直接建立。

### 3.2 编译器插桩：每次访存前查 shadow

编译时开 `-fsanitize=kernel-address`，编译器在每次 load/store 前插入检查：

```c
shadow = *kasan_mem_to_shadow(addr);
if (!access_is_valid(shadow, addr, size))
    kasan_report(addr, size, is_write, ip);
```

为什么必须编译期做：CPU 没有「拦截每次访存」的硬件钩子；编译器恰好知道每次访存的地址和宽度。代价：编译器看不见的访问（汇编、关掉插桩的代码）是盲区。

闭环：alloc（写 shadow 划边界）→ access（查 shadow 拦越界）→ free（写 shadow 收回边界）。

### 3.3 三种模式

| 模式 | 机制 | 开销 | 适用 |
|---|---|---|---|
| Generic | 1:8 shadow + 插桩 + quarantine | 高（~3x 性能、1/8 内存） | 开发调试，字节级精确 |
| SW_TAGS | 指针/内存各存 tag，软件比较（16B 粒度） | 中 | ARM64 长时间测试 |
| HW_TAGS | ARM MTE 硬件比较 tag | 低 | 需 CPU 支持 MTE（Kunpeng 920 不支持） |

## 4. 使用

### 4.1 配置与运行

「随机崩溃、链表损坏、莫名 NULL 解引用、怀疑内存被踩」时，先上 KASAN 扫一遍。开发调试配置：

```text
CONFIG_KASAN=y
CONFIG_KASAN_GENERIC=y
CONFIG_KASAN_STACK=y
CONFIG_KASAN_VMALLOC=y      # 架构支持时
CONFIG_STACKTRACE=y
```

常用启动参数：`kasan_multi_shot`（允许报多次，测试环境建议加上）、`kasan.fault=panic`（出错即停）。

首次搭建可用官方测试自检：`CONFIG_KASAN_KUNIT_TEST=m` 后 `modprobe kasan_test`，看到 KASAN 报告是预期行为（测试故意触发错误），通过与否看 KUnit 的 `ok/not ok`。

### 4.2 报告怎么读（以真实报告为例）

`kasan_test` 故意触发的一次越界写（已删减）：

```text
BUG: KASAN: slab-out-of-bounds in kmalloc_oob_right+0x705/0x7d0 [kasan_test]
Write of size 1 at addr ffff888005fa5f73 by task kunit_try_catch/249
...
Call Trace:
 kasan_report+0x119/0x140
 kmalloc_oob_right+0x705/0x7d0 [kasan_test]
Allocated by task 249:
 __kasan_kmalloc+0x7f/0x90
 kmalloc_oob_right+0xae/0x7d0 [kasan_test]

The buggy address belongs to the object at ffff888005fa5f00
which belongs to the cache kmalloc-128 of size 128
The buggy address is located 0 bytes to the right of
allocated 115-byte region [ffff888005fa5f00, ffff888005fa5f73)

Memory state around the buggy address:
>ffff888005fa5f00: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 03 fc
                                                            ^
```

从上往下读：

1. **标题行**：错误类型 + 肇事函数。
2. **访问行**：`Write of size 1` —— 一次 1 字节写越界。
3. **Call Trace**：`kasan_report` 之上最近的业务函数就是肇事指令，用 `scripts/faddr2line vmlinux 'kmalloc_oob_right+0x705'` 定位源码。UAF 报告还会多一段 `Freed by`，把 access/alloc/free 三条栈摆在一起查生命周期问题。
4. **对象归属**：对象在 `kmalloc-128` 里，实际申请 115 字节，肇事地址恰好是右边界外第 0 字节——典型的「数组多写了一个」。
5. **shadow dump**：`^` 指向肇事 granule。14 个 `00` + `03` + `fc`（redzone）：115 = 14×8 + 3，与文字部分完全吻合——shadow 语义（3.1 节）在报告里就是这样体现的。

### 4.3 没报告时

不要据此排除内存问题，回到第 2 节盲区表换工具。

## 参考

- 官方文档：https://www.kernel.org/doc/html/latest/dev-tools/kasan.html
- 实现：`mm/kasan/`、`lib/Kconfig.kasan`
