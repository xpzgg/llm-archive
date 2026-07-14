# zswap 参数更新失败导致 NULL 指针解引用问题分析

- 日期：2026-07-13
- 分析基线：openEuler OLK-5.10、OLK-6.6
- 问题类型：KASAN null-ptr-deref
- 涉及模块：`kernel/params.c`、`mm/zswap.c`、`mm/zpool.c`

## 摘要

**一句话结论：运行期写入 zswap 的 charp 参数时，通用的 `param_set_charp()` 在新字符串分配成功前就执行旧值释放流程，并直接用分配结果覆盖参数；分配失败因此将 `zswap_zpool_type` 留为 NULL，后续 zswap 延迟初始化把该 NULL 传给 `zpool_get_driver()`，最终在 `strcmp()` 中解引用零地址。**

这是参数更新失败语义被破坏后产生的顺序性错误，不需要并发竞争。崩溃点 `strcmp()` 和直接调用者 `zpool_get_driver()` 都是受害位置；真正的缺陷发生在更早的一次 charp 参数更新中。`param_set_charp()` 返回 `-ENOMEM`，但参数已经不再保持调用前的有效值。

问题可造成内核崩溃。zswap 参数文件为 root-owned sysfs 属性，实际触发通常需要特权写入，并通过 failslab、fail-nth 或极端内存压力使一个很小的 `GFP_KERNEL` 分配失败，因此现实攻击面有限，但错误恢复路径违反了通用参数接口应有的失败原子性，且影响范围不限于 zswap。

根因修复应放在 `param_set_charp()`：先分配并复制替换值，成功后再释放旧值并提交更新，使失败操作保持旧参数不变。只在 zswap 或 zpool 增加 NULL 校验只能阻止某一个崩溃点，不能恢复已经损坏的参数状态，也不能覆盖其他 charp 调用者。

当前缺少原始 syz 程序和故障注入记录，因此“之前一次 zpool 参数写入在 `kmalloc_parameter()` 处失败”属于基于源码赋值点排查得到的高置信度推断；栈本身直接证明的是 zswap 初始化时传给 `zpool_has_pool()` 的类型指针为 NULL。

## Syzkaller 报告信息

### 故障现场

报告的核心信息为：

    BUG: KASAN: null-ptr-deref in strcmp+0x38/0x80 lib/string.c:349
    Read of size 1 at addr 0000000000000000 by task syz.0.741/3753

关键调用路径为：

    strcmp()
    └── zpool_get_driver()
        └── zpool_has_pool()
            └── __zswap_pool_create_fallback()
                └── zswap_setup()
                    └── zswap_enabled_param_set()
                        └── param_attr_store()
                            └── sysfs write

报告中的源码位置为：

    zpool_get_driver+0x84/0x164 mm/zpool.c:84
    zpool_has_pool+0x28/0xa0 mm/zpool.c:124
    __zswap_pool_create_fallback+0x134/0x244 mm/zswap.c:698
    zswap_setup.part.0+0xd4/0x260 mm/zswap.c:1518
    zswap_enabled_param_set+0x138/0x1c0 mm/zswap.c:899

OLK-5.10 的 `mm/zswap.c:698` 精确对应：

    has_zpool = zpool_has_pool(zswap_zpool_type);

`mm/zpool.c:84` 对应：

    if (!strcmp(driver->type, type)) {

KASAN 在 `strcmp()` 中读取零地址，说明参与比较的两个字符串之一为 NULL。在标准 OLK-5.10/6.6 中，注册的 zpool driver 分别把 `type` 初始化为 `"zsmalloc"`、`"zbud"` 或 `"z3fold"`；结合调用链和所有赋值点，NULL 参数应为传入的 `zswap_zpool_type`。

### 报告能够直接证明和不能直接证明的内容

报告直接证明：

| 证据 | 可以得出的结论 |
|---|---|
| 当前系统调用为 sysfs write | 崩溃发生在运行期参数写入路径 |
| `zswap_enabled_param_set()` 位于栈中 | 本次写入正在触发 zswap 初始化或使能 |
| `mm/zswap.c:698` | zswap 正在查询配置的 zpool 类型 |
| `strcmp()` 读取地址 0 | zpool 类型比较收到了 NULL 字符串 |

报告没有给出导致参数变为 NULL 的前一次系统调用。根据源码，`zswap_zpool_type` 初始值为 `CONFIG_ZSWAP_ZPOOL_DEFAULT`，zswap 自身的其他赋值只会把它设置为默认字符串或空字符串常量。唯一能在返回错误的同时将其写成 NULL 的路径是运行期 `param_set_charp()` 分配失败。因此建议在原始 syz 程序中检查以下模式：

    fault injection / fail-nth
    → write("/sys/module/zswap/parameters/zpool", ...)
    → write 返回 -ENOMEM
    → write("/sys/module/zswap/parameters/enabled", ...)
    → NULL pointer dereference

## 参数和 zswap 初始化背景

### charp 参数的两种内存所有权

`param_set_charp()` 同时服务内核启动参数、模块加载参数和运行期 sysfs 参数。它根据 slab 是否可用采用两种所有权模型。

启动早期 slab 尚未初始化，不能调用 `kmalloc()`。此时 `val` 指向经过 `parse_args()` 原地切分的内核命令行缓冲区。该缓冲区虽然已被插入 NUL 字符而成为代码注释所说的 “mangled commandline”，但其内存会被长期保留，因此参数可以直接借用该指针：

    slab_is_available() == false
    → 不分配字符串副本
    → 参数直接指向保留的命令行内存

运行期 sysfs 写入使用的输入缓冲区只在当前写操作期间有效，参数不能在回调返回后继续引用它。因此 slab 可用后，`param_set_charp()` 必须分配并保存一份副本：

    slab_is_available() == true
    → kmalloc_parameter()
    → 复制 val
    → 参数拥有动态字符串副本

`kmalloc_parameter()` 还会把分配记录加入全局链表，使 `param_free_charp()` 和后续参数更新能够判断并释放由参数框架拥有的字符串。该所有权管理属于通用 charp 机制，不应由 zswap 重新实现。

### zswap 参数回调

zswap 的 compressor 和 zpool 都使用 charp 保存字符串，同时注册自定义 `.set` 回调：

    zswap_compressor_param_set()
    zswap_zpool_param_set()
    └── __zswap_param_set()

自定义回调负责 zswap 的业务语义，例如检查压缩算法或 zpool driver 是否存在，以及在运行中创建、切换 pool。字符串的分配、释放和参数提交仍交给通用的 `param_set_charp()`。

当 zswap 尚未初始化时，`__zswap_param_set()` 不创建 pool，只保存用户配置，随后由 `zswap_setup()` 统一创建初始 pool：

    case ZSWAP_UNINIT:
        ret = param_set_charp(s, kp);
        break;

OLK-5.10 通过提交 `699bb9e83020 ("mm/zswap: delay the initializaton of zswap until the first enablement")` 引入这条延迟初始化路径；OLK-6.6 包含对应的上游实现 `141fdeececb3 ("mm/zswap: delay the initialization of zswap")`。

### 参数锁为什么不能阻止问题

sysfs 参数写入由通用的 kernel parameter mutex 串行化，zswap 还使用 `zswap_init_lock` 保护初始化状态。因此该问题不需要两个参数写操作并发执行。

锁只能保证没有其他线程同时观察更新过程，不能保证失败时自动回滚。第一次写 zpool 参数在持锁状态下把全局指针留成 NULL，释放锁后这个无效状态会一直存在；第二次写 enabled 参数随后合法地取得锁并使用该 NULL。

## 根因分析

### `param_set_charp()` 在失败前已经提交了部分状态

存在问题的实现顺序为：

    maybe_kfree_parameter(*(char **)kp->arg);

    *(char **)kp->arg = kmalloc_parameter(strlen(val) + 1);
    if (!*(char **)kp->arg)
        return -ENOMEM;

这段代码包含两个不能安全回滚的动作：

1. 在确认替换值可用前尝试释放旧值；如果旧值是此前动态分配的字符串，它会在这里被真正释放；
2. 直接把分配结果写入参数指针。

当 `kmalloc_parameter()` 返回 NULL 时，函数虽然报告 `-ENOMEM`，但调用者持有的参数已从有效字符串变成 NULL。如果旧值来自此前的运行期更新，它已经被释放；如果旧值是编译期默认字符串或 early boot 命令行指针，释放函数虽然是 no-op，该有效指针仍已被 NULL 覆盖。函数没有保存旧指针，因此 zswap 无法在外层回调中恢复原值。

正常的失败语义应为：

    old = 有效参数
    尝试 set(new) 失败
    返回错误，参数仍为 old

实际语义却是：

    old = 有效参数
    释放流程处理 old，随后覆盖参数指针
    尝试分配 new 失败
    返回错误，参数变为 NULL

### 从参数损坏到 `strcmp()` 崩溃的完整因果链

完整触发链可以重建为：

| 顺序 | 操作 | 状态变化 |
|---|---|---|
| 1 | 系统启动时 zswap 保持关闭 | `zswap_init_state == ZSWAP_UNINIT` |
| 2 | 特权任务写 zpool 参数 | 进入 `__zswap_param_set()` |
| 3 | `param_set_charp()` 执行旧值释放流程并尝试分配新值 | 旧参数不再受保护 |
| 4 | `kmalloc_parameter()` 失败 | `zswap_zpool_type == NULL`，写操作返回 `-ENOMEM` |
| 5 | 任务写 enabled 参数 | `zswap_enabled_param_set()` 调用 `zswap_setup()` |
| 6 | fallback 查询 zpool | `zpool_has_pool(NULL)` |
| 7 | 遍历已注册 driver | `strcmp(driver->type, NULL)` |
| 8 | `strcmp()` 读取第二个字符串 | KASAN 报告地址 0 读取 |

当前实现会在处理 enabled 的布尔值之前执行初始化，因此只要状态仍为 `ZSWAP_UNINIT`，写 `enabled=0` 也可能进入 `zswap_setup()`；触发并不依赖最终是否真的要打开 zswap。

### 为什么 NULL 校验不是根因修复

在 `zpool_has_pool()`、`zpool_get_driver()` 或 zswap fallback 中增加 NULL 校验，可以避免当前调用链进入 `strcmp(NULL)`，但无法恢复参数框架的失败语义：

- 参数更新已经失败并丢失旧值；
- 后续读取该 charp 参数仍会面对无效状态；
- zswap compressor 使用相同机制，可能在 crypto 路径以另一种调用栈崩溃；
- 其他使用 `param_set_charp()` 的模块仍然暴露于同类错误；
- 在一个消费点返回错误，只会隐藏或移动下一次失败位置。

如果已有下游补丁增加了 zswap NULL 校验，可以将其保留为防御性加固，但不应把它视为完整修复。

### OLK-6.6 适用性

OLK-6.6 仍保留构成问题的全部条件：

1. `param_set_charp()` 先执行旧值释放流程，再把分配结果直接写入参数；
2. zswap compressor 和 zpool 使用 charp；
3. `ZSWAP_UNINIT` 参数更新直接调用 `param_set_charp()`；
4. enabled 写入可以触发延迟初始化；
5. fallback 未在调用 `zpool_has_pool()` 前验证类型指针；
6. `zpool_get_driver()` 直接使用 `strcmp(driver->type, type)`。

OLK-5.10 与 OLK-6.6 在 zswap pool 数量、压缩上下文和其他 MM 实现上存在差异，但这些差异不改变上述因果链。因此内部版本只要没有修改通用 charp 更新语义，就应判定为受影响。

## 修复建议

### 在通用 charp 层实现事务式更新

推荐把更新顺序改为：

    分配新值
    → 复制成功
    → 释放旧值
    → 提交新指针

核心实现为：

    int param_set_charp(const char *val, const struct kernel_param *kp)
    {
        char *newval;

        ...
        if (slab_is_available()) {
            newval = kmalloc_parameter(strlen(val) + 1);
            if (!newval)
                return -ENOMEM;
            strscpy(newval, val, strlen(val) + 1);
        } else {
            newval = (char *)val;
        }

        maybe_kfree_parameter(*(char **)kp->arg);
        *(char **)kp->arg = newval;
        return 0;
    }

这样 `kmalloc_parameter()` 失败时，函数在释放旧值和修改参数指针之前返回，调用者继续持有原来的有效字符串。成功路径仍由 `kmalloced_params` 链表正确跟踪新旧分配。

启动早期的特殊语义保持不变：slab 不可用时仍然直接引用被保留的命令行缓冲区，不引入早期内存分配。修改只是在运行期分配成功后才提交替换值。

更新期间会短暂同时持有新旧两个字符串，理论上增加最多约 1025 字节的瞬时内存需求。相比失败后破坏全局参数状态，这一代价可以接受；内存不足时返回 `-ENOMEM` 并保留旧值正是安全的降级行为。

### 为什么不建议为 zswap 重写字符串管理

zswap 的自定义 `.set` 回调适合处理以下业务规则：

- zpool driver 是否存在；
- compressor 是否可用；
- 是否需要创建或切换 pool；
- zswap 初始化状态是否允许修改。

动态字符串的分配登记、释放和失败回滚属于通用 charp 所有权模型。`kmalloc_parameter()` 及其分配链表是 `kernel/params.c` 的内部实现；如果 zswap 自行使用 `kstrdup()`，还需要重新实现匹配的 `.free` 回调和所有权规则，增加泄漏、重复释放以及启动早期语义不一致的风险。

因此建议：

- 根因补丁放在 `param_set_charp()`；
- zswap 保持现有业务 `.set` 回调；
- 已存在的 NULL 校验可以保留为防御，但不要新增一套 zswap 专用 charp 分配逻辑。

### 社区提交和回移建议

该问题最早可追溯到运行期 charp 参数开始复制并释放旧值的提交：

    Fixes: e180a6b7759a ("param: fix charp parameters set via sysfs")

建议补丁主题为：

    params: preserve charp values on allocation failure

补丁只修改通用 helper，不依赖 zswap 版本特有的数据结构，适合标记：

    Cc: stable@vger.kernel.org

回移到 OLK-5.10、OLK-6.6 或内部版本时，不应只判断文本是否一致，而应确认目标版本同时满足：

1. 新字符串在旧字符串释放前完成分配和复制；
2. 任何失败返回都没有修改 `*kp->arg`；
3. early boot 仍不调用 `kmalloc()`；
4. 成功路径仍由原有参数分配跟踪机制负责释放。

### 验证方案

静态和构建检查：

    git diff --check
    scripts/checkpatch.pl --strict --no-tree - < patch
    make -jN kernel/params.o

故障注入回归应覆盖 zpool 和 compressor 两个参数：

1. 以 zswap 未初始化状态启动；
2. 记录当前 zpool 参数值；
3. 使用 failslab 或 `/proc/self/task/<tid>/fail-nth` 使参数字符串分配失败；
4. 写入新的 zpool 值并确认系统调用返回 `-ENOMEM`；
5. 重新读取 zpool 参数，确认仍为步骤 2 的旧值；
6. 分别写 `enabled=0` 和 `enabled=1`，确认没有 KASAN 报告；
7. 对 compressor 参数重复相同过程；
8. 重新运行原始 syz reproducer。

修复的关键判定标准不是“`strcmp()` 不再崩溃”，而是“任何 charp 分配失败之后，参数值与调用前完全一致”。这才能证明错误恢复语义已经从根因上修复。

## 当前处理状态

目标 worktree：

    /home/yjc/project/worktree/issue_zswap

已实施的修复：

1. `kernel/params.c` 中的 `param_set_charp()` 已改为先准备 `newval`，只有在 `kmalloc_parameter()` 成功并完成复制后，才释放旧参数并提交新指针。
2. early boot 路径保持不分配内存，仍直接引用被保留的命令行参数缓冲区。
3. 为了让回归测试能稳定命中目标分配点，`kmalloc_parameter()` 增加 `ALLOW_ERROR_INJECTION(kmalloc_parameter, NULL)`。
4. 新增 `tools/testing/selftests/mm/zswap_charp_failure.sh`，并加入 `tools/testing/selftests/mm/Makefile` 的 `TEST_PROGS`。

当前 selftest 的运行前提：

    CONFIG_ZSWAP=y
    CONFIG_DEBUG_FS=y
    CONFIG_FUNCTION_ERROR_INJECTION=y
    CONFIG_FAULT_INJECTION=y
    CONFIG_FAULT_INJECTION_DEBUG_FS=y
    CONFIG_FAIL_FUNCTION=y

运行方式：

    # 建议启动参数包含 zswap.enabled=0，并在写 enabled 参数前运行
    ./tools/testing/selftests/mm/zswap_charp_failure.sh

未修复内核上，普通模式会验证失败后参数是否被改写；`--reproduce` 会在发现参数损坏后继续写 `enabled=0`，用于触发原始 call trace 的消费路径，KASAN 测试内核可能崩溃。

已完成的本地验证：

    bash -n tools/testing/selftests/mm/zswap_charp_failure.sh
    git diff --check
    scripts/checkpatch.pl --no-tree --strict --file tools/testing/selftests/mm/zswap_charp_failure.sh
    git diff -- kernel/params.c tools/testing/selftests/mm/Makefile | scripts/checkpatch.pl --no-tree --strict -
    make O=/tmp/issue_zswap_build -j8 kernel/params.o kernel/fail_function.o
    make O=/tmp/issue_zswap_build -j8 mm/zswap.o

其中 `/tmp/issue_zswap_build` 临时配置打开了 `FUNCTION_ERROR_INJECTION`、`FAULT_INJECTION`、`FAULT_INJECTION_DEBUG_FS`、`FAIL_FUNCTION` 和 `ZSWAP`。`kernel/params.o` 中已确认存在 `_eil_addr_kmalloc_parameter` 符号和 `_error_injection_whitelist` section，说明 `kmalloc_parameter()` 的 fail_function 白名单条目真实生成。

尚未在当前容器内完成的验证：

1. 启动包含该补丁的目标内核；
2. 在目标内核运行 `zswap_charp_failure.sh`；
3. 在未修复和已修复内核间做动态 A/B 对照。

原因是当前执行环境不是刚构建出的测试内核运行环境，不能直接操作该内核的 `/sys/module/zswap/parameters` 和 `/sys/kernel/debug/fail_function` 状态。动态验证应在 qemu、测试机或 syzkaller 环境中完成。
