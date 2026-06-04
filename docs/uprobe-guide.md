# uprobe 使用指南

## 1. 原理

uprobe 是 Linux 内核提供的**用户态动态追踪机制**，允许在用户程序的任意指令地址处插入探测点，无需修改源码或重新编译。

### 工作流程

```
1. 用户注册 uprobe（指定二进制文件 + 偏移地址）
2. 内核将该地址的指令替换为断点指令（x86: int3, ARM64: brk）
3. 程序执行到该地址时触发 trap，陷入内核
4. 内核执行 uprobe handler，收集用户指定的寄存器/内存数据
5. 内核将数据写入 tracefs（ftrace 缓冲区），用户通过 tracefs 读取
6. 恢复原始指令，程序继续执行
```

### 核心能力

- **抓取函数入参**：通过参数寄存器读取传入的值或指针
- **抓取函数返回值**：通过 uretprobe 读取返回寄存器
- **读取任意内存**：通过寄存器 + 偏移解引用结构体字段
- **零侵入**：不需要修改目标程序，不需要重新编译

---

## 2. 调用约定（参数传递机制）

uprobe 抓取函数参数的关键是**知道参数在哪个寄存器里**。这由 CPU 架构的调用约定（ABI）决定。

### 通用规则

参数通过**寄存器**传递，不用栈指针（`%sp`）。区别在于：

| 情况 | 传递方式 | uprobe 语法 |
|------|----------|-------------|
| 小参数（int、指针、≤16B 结构体） | 值直接在寄存器里 | `字段名=%寄存器:类型` |
| 大结构体（>16B） | 指针在寄存器里，数据在内存 | `字段名=+偏移(%寄存器):类型` |

### ARM64 (AAPCS64)

参数寄存器为 **x0-x7**，最多 8 个参数通过寄存器传递，超过的走栈。

C++ 成员函数隐含 `this` 作为第一个参数：

```
返回值 func(this,    arg1,     arg2,     arg3, ...)
              x0      x1        x2        x3
```

uprobe 中使用 `%x0`, `%x1`, ..., `%x7` 引用。

| 寄存器 | 用途 |
|--------|------|
| x0-x7 | 参数 / 返回值 |
| x8 | 大结构体返回值的目标地址 |
| x9-x15 | 临时寄存器（caller-saved） |
| x19-x28 | callee-saved 寄存器 |
| x29 | 帧指针 (fp) |
| x30 | 链接寄存器 (lr，返回地址) |
| sp | 栈指针（**不要用 sp 读参数**） |

### x86_64 (System V AMD64 ABI)

参数寄存器为 **rdi, rsi, rdx, rcx, r8, r9**，最多 6 个整数/指针参数通过寄存器传递。

C++ 成员函数：

```
返回值 func(this,    arg1,     arg2,     arg3, ...)
              rdi     rsi       rdx       rcx
```

uprobe 中使用 `%di`, `%si`, `%dx`, `%cx`, `%r8`, `%r9` 引用。

| 寄存器 | 用途 |
|--------|------|
| rdi | 第 1 个参数 |
| rsi | 第 2 个参数 |
| rdx | 第 3 个参数 |
| rcx | 第 4 个参数 |
| r8 | 第 5 个参数 |
| r9 | 第 6 个参数 |
| rax | 返回值 |
| rsp | 栈指针（**不要用 rsp 读参数**） |

### 如何确认参数寄存器

调用约定告诉你"应该怎样"，但编译器可能优化。**反汇编是最终裁判。**

```bash
# 按函数名查找并反汇编
objdump -d /path/to/binary | grep -A 30 '<FunctionName'
```

看入口前几条指令，找到参数从哪些寄存器被存走：

**ARM64 示例：**
```asm
ProcessPacket:
    stp  x29, x30, [sp, #-48]!   // 保存帧
    mov  x19, x0                  // x0 → x19（第 1 个参数：this）
    mov  x20, x1                  // x1 → x20（第 2 个参数：pkt 指针）
    ldr  w21, [x1, #0x08]         // 从 pkt + 0x08 读取字段
```

**x86_64 示例：**
```asm
ProcessPacket:
    push   rbp
    mov    rbp, rsp
    mov    r12, rdi               # rdi → r12（第 1 个参数：this）
    mov    r13, rsi               # rsi → r13（第 2 个参数：pkt 指针）
    mov    r14, rdx               # rdx → r14（第 3 个参数：port_id）
```

**判断方法：函数入口处被 `mov` 存走的寄存器就是参数。**

---

## 3. uprobe 语法

### 注册探测点

```bash
echo 'p:事件名 /path/to/binary:偏移 字段=表达式 ...' > /sys/kernel/debug/tracing/uprobe_events
```

### 数据读取语法

```
# 直接读寄存器的值（小参数）
字段名=%寄存器:类型

# 读寄存器指向的内存 + 偏移（大结构体字段）
字段名=+偏移(%寄存器):类型
```

### 类型

| 类型 | 说明 |
|------|------|
| x8 / u8 | 8 位无符号 |
| x16 / u16 | 16 位无符号 |
| x32 / u32 | 32 位无符号 |
| x64 / u64 | 64 位无符号 |
| s32 / s64 | 有符号 32/64 位 |

### 启用和读取

```bash
# 启用探测
echo 1 > /sys/kernel/debug/tracing/events/事件分类/事件名/enable

# 读取结果
cat /sys/kernel/debug/tracing/trace

# 清空
echo > /sys/kernel/debug/tracing/trace

# 删除探测点
echo '-:事件分类/事件名' > /sys/kernel/debug/tracing/uprobe_events
```

---

## 4. 完整示例

### 场景

C++ 成员函数，一个小参数（int）一个大参数（结构体指针）：

```c
// 网络包结构体，>16B，传指针
struct Packet {
    u32 src_port;       // +0x00
    u32 dst_port;       // +0x04
    u16 flags;          // +0x08
    u16 reserved;       // +0x0A
    u32 seq_num;        // +0x0C
    u32 ack_num;        // +0x10
    u8  payload[1024];  // +0x14
} __attribute__((packed));

// C++ 成员函数
// ConnectionHandler::ProcessPacket(Packet pkt, int direction)
//   - pkt: 大结构体，传指针（x1）
//   - direction: 小参数，值直接在寄存器（x2）
//
// ARM64 寄存器布局：
//   x0 = this
//   x1 = &pkt（指向 Packet 结构体的指针）
//   x2 = direction（int 值直接在寄存器里）
```

### 示例 1：读取小参数（值直接在寄存器）

`direction` 是 int，值直接在 x2 里，不需要解引用：

```bash
echo 'p:ProcessPacket /usr/local/bin/myapp:0x5a3c0 direction=%x2:x32' \
  > /sys/kernel/debug/tracing/uprobe_events
```

### 示例 2：读取大结构体字段（指针 + 偏移）

`pkt` 是大结构体，x1 存的是指针。要读取字段需要加偏移：

```
x1 指向 Packet 起始地址
│
├─ +0x00  src_port    (u32)
├─ +0x04  dst_port    (u32)
├─ +0x08  flags       (u16)
├─ +0x0A  reserved    (u16)
├─ +0x0C  seq_num     (u32)   ← 目标字段
├─ +0x10  ack_num     (u32)
└─ +0x14  payload[]   (u8[])
```

```bash
# 读取 seq_num（偏移 0x0C）和 flags（偏移 0x08）
echo 'p:ProcessPacket /usr/local/bin/myapp:0x5a3c0 \
  flags=+0x8(%x1):u16 seq=+0xc(%x1):x32 src=+0x0(%x1):x32 dst=+0x4(%x1):x32 direction=%x2:x32' \
  > /sys/kernel/debug/tracing/uprobe_events
```

### 示例 3：小参数 + 大参数一起抓

```bash
echo 'p:ProcessPacket /usr/local/bin/myapp:0x5a3c0 \
  direction=%x2:x32 flags=+0x8(%x1):u16 seq=+0xc(%x1):x32' \
  > /sys/kernel/debug/tracing/uprobe_events
```

### 示例 4：全量 dump 验证

不确定数据布局时，先打印前几个 8 字节确认：

```bash
# 打印 x1 指向的内存前 48 字节
echo 'p:ProcessPacket /usr/local/bin/myapp:0x5a3c0 \
  d0=+0x0(%x1):x64 d1=+0x8(%x1):x64 d2=+0x10(%x1):x64 d3=+0x18(%x1):x64 d4=+0x20(%x1):x64 d5=+0x28(%x1):x64' \
  > /sys/kernel/debug/tracing/uprobe_events

# 或者先打印寄存器本身的值，确认哪个是指针
echo 'p:ProcessPacket /usr/local/bin/myapp:0x5a3c0 x0=%x0:x64 x1=%x1:x64 x2=%x2:x64' \
  > /sys/kernel/debug/tracing/uprobe_events
```

### 示例 5：通过反汇编确认寄存器

```bash
objdump -d --start-address=0x5a3c0 --stop-address=0x5a420 /usr/local/bin/myapp
```

看到类似：

```asm
ProcessPacket:
    stp  x29, x30, [sp, #-48]!
    mov  x29, sp
    mov  x19, x0                  // x0 → x19（this）
    mov  x20, x1                  // x1 → x20（&pkt 指针）
    ldr  w21, [x1, #0x0c]         // 从 pkt + 0x0C 读取 seq_num
    mov  w22, w2                  // w2 → w22（direction，直接是值）
```

入口处被 `mov` 存走的寄存器就是参数。x0=this, x1=&pkt, x2=direction，跟 AAPCS64 一致。

---

## 5. 常见问题

### Q: 用 `%sp` 读参数为什么读不到？

参数通过参数寄存器传递，不在被调函数的栈帧上。`%sp` 指向的是当前函数的栈帧（返回地址、局部变量等），跟参数无关。

### Q: 怎么知道函数地址？

```bash
# 方法 1：nm 查找符号
nm /path/to/binary | grep FunctionName

# 方法 2：objdump 查找
objdump -t /path/to/binary | grep FunctionName

# 方法 3：readelf
readelf -s /path/to/binary | grep FunctionName
```

注意：如果二进制被 strip 过，符号可能不存在，需要通过反汇编手动定位。

### Q: 结构体字段偏移怎么算？

对于 `__attribute__((packed))` 的结构体，偏移就是各字段大小的累加。对于非 packed 结构体，需要考虑对齐填充。最稳妥的方法：

```bash
# 如果有源码，用 pahole 或 gdb 查看布局
gdb /path/to/binary -batch -ex "ptype /o struct_name"
```

### Q: uprobe 提示 "invalid argument"？

常见原因：
1. **寄存器名错误**：ARM64 用 `%x0`-`%x7`，x86_64 用 `%di`/`%si`/`%dx` 等，不能混用
2. **偏移地址越界**：地址不在二进制代码段内
3. **二进制路径错误**：确保路径是目标进程实际加载的文件
4. **语法错误**：检查引号、冒号、括号是否正确

### Q: 如何确认编译器实际用了哪个寄存器？

看反汇编，函数入口前几条 `mov` 指令会告诉你。详见第 2 节"如何确认参数寄存器"。

---

## 6. 快速参考

```
# ARM64 参数寄存器 (AAPCS64)
C++ 成员函数：this=x0, arg1=x1, arg2=x2, arg3=x3, ...
C   普通函数：arg0=x0, arg1=x1, arg2=x2, arg3=x3, ...

# uprobe 语法
直接读寄存器值：   字段名=%寄存器:类型        （小参数：int、指针）
读指针+偏移取字段： 字段名=+偏移(%寄存器):类型 （大结构体字段）

# 永远不要用 %sp 读函数参数
```
