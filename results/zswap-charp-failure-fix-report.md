# zswap charp 参数更新失败问题修复报告

- 日期：2026-07-14
- 修复树：`/home/yjc/project/worktree/issue_zswap_fix`
- 基线 commit：`998334b47997`
- 目标分支：`issue_zswap_fix`
- 问题类型：zswap 延迟初始化路径 NULL pointer dereference
- 相关文件：`kernel/params.c`、`tools/testing/selftests/mm/Makefile`、`tools/testing/selftests/mm/zswap_charp_failure.sh`

## 问题背景

zswap 的 `zpool` 和 `compressor` 参数是 `charp` 类型参数：

```c
static char *zswap_compressor = CONFIG_ZSWAP_COMPRESSOR_DEFAULT;
static char *zswap_zpool_type = CONFIG_ZSWAP_ZPOOL_DEFAULT;
```

运行期通过 sysfs 写入这些参数时，zswap 的自定义 `.set` 回调最终仍依赖通用的 `param_set_charp()` 完成字符串保存。为了复现错误路径，本次新增 selftest 使用 `fail_function` 精确注入 `kmalloc_parameter()` 返回 NULL，模拟运行期参数更新过程中字符串副本分配失败。

测试场景固定为：

```text
zswap.enabled=0
zswap 处于 ZSWAP_UNINIT
写 /sys/module/zswap/parameters/zpool 或 compressor
注入 kmalloc_parameter() 失败
再写 /sys/module/zswap/parameters/enabled 触发 zswap_setup()
```

这一路径不依赖并发竞争。第一次参数写入失败后，坏状态会保留到后续 enabled 写入。

## 问题根因

旧的 `param_set_charp()` 更新顺序是先释放旧参数，再分配并提交新参数：

```c
maybe_kfree_parameter(*(char **)kp->arg);

if (slab_is_available()) {
	*(char **)kp->arg = kmalloc_parameter(strlen(val)+1);
	if (!*(char **)kp->arg)
		return -ENOMEM;
	strcpy(*(char **)kp->arg, val);
} else {
	*(const char **)kp->arg = val;
}
```

这里破坏了 charp 参数更新的失败原子性。`kmalloc_parameter()` 失败时，函数返回 `-ENOMEM`，但参数指针已经被 NULL 覆盖；如果旧值是此前动态分配的字符串，还可能已经被释放。

在 zswap 延迟初始化场景中，完整因果链是：

1. 系统以 `zswap.enabled=0` 启动，zswap 仍未初始化。
2. 对 `zswap.zpool` 写入新值时，`kmalloc_parameter()` 被注入失败。
3. `param_set_charp()` 返回 `-ENOMEM`，但 `zswap_zpool_type` 已经变成 NULL。
4. 后续写 `zswap.enabled=0` 仍会触发 `zswap_setup()`。
5. `zswap_setup()` 走 fallback pool 创建路径，调用 `zpool_has_pool(zswap_zpool_type)`。
6. `zpool_get_driver()` 中执行 `strcmp(driver->type, type)`，其中 `type == NULL`。
7. `strcmp()` 读取 0 地址，触发 NULL pointer dereference。

因此，崩溃点 `strcmp()` 和 zpool 查询路径都是受害位置；真正被破坏的不变量是：`param_set_charp()` 在更新失败时必须保持旧参数不变。

## 修复逻辑

修复放在通用 `param_set_charp()`，把 charp 更新改成先准备新值，成功后再释放旧值并提交：

```c
char *newval;
size_t len = strlen(val);

if (len > 1024) {
	pr_err("%s: string parameter too long\n", kp->name);
	return -ENOSPC;
}

if (slab_is_available()) {
	newval = kmalloc_parameter(len + 1);
	if (!newval)
		return -ENOMEM;
	memcpy(newval, val, len + 1);
} else {
	newval = (char *)val;
}

maybe_kfree_parameter(*(char **)kp->arg);
*(char **)kp->arg = newval;
```

这样 `kmalloc_parameter()` 失败时，函数在释放旧参数和修改参数指针之前返回，旧值仍然有效。启动早期 `slab_is_available() == false` 的特殊路径保持不变，仍直接引用被保留的命令行缓冲区。

同时新增：

- `ALLOW_ERROR_INJECTION(kmalloc_parameter, NULL)`：让 selftest 可以精确注入该内部 helper 的 NULL 返回。
- `tools/testing/selftests/mm/zswap_charp_failure.sh`：验证注入 `-ENOMEM` 后 `zpool` 和 `compressor` 都保持旧值。
- `tools/testing/selftests/mm/Makefile`：把该脚本加入 `TEST_PROGS`。

不选择只在 zswap 或 zpool 里加 NULL 判断，是因为那只能避开当前崩溃点，不能恢复已经损坏的参数状态，也不能覆盖其他 `charp` 参数用户。

## 验证步骤

本节记录可手工重复执行的验证流程。下面命令假设源码和构建目录与本次验证一致：

```text
baseline 源码：/home/yjc/project/worktree/issue_zswap_baseline
fix 源码：/home/yjc/project/worktree/issue_zswap_fix
baseline 构建目录：/home/yjc/project/worktree/build-zswap-baseline-oe
fix 构建目录：/home/yjc/project/worktree/build-zswap-fix-oe
initramfs 目录：/home/yjc/project/worktree/zswap-initramfs-root
initramfs 文件：/home/yjc/project/worktree/zswap-test-rootfs.cpio
```

如果需要重新准备 baseline，原则是：保留旧的 `param_set_charp()` 逻辑，只加入测试辅助能力，也就是 `ALLOW_ERROR_INJECTION(kmalloc_parameter, NULL)` 和同一份 `zswap_charp_failure.sh`。这样 baseline 与 fix 的区别只剩真正修复逻辑。

### 1. 生成测试内核配置

对 baseline 和 fix 分别执行。下面以 fix 为例，baseline 只需要替换 `src` 和 `build`：

```bash
src=/home/yjc/project/worktree/issue_zswap_fix
build=/home/yjc/project/worktree/build-zswap-fix-oe

rm -rf "$build"
mkdir -p "$build"

make -C "$src" O="$build" openeuler_defconfig
make -C "$src" O="$build" kvm_guest.config

"$src/scripts/config" --file "$build/.config" \
  -d WERROR \
  -e DEBUG_KERNEL \
  -e DEBUG_FS \
  -e KCOV \
  -e KCOV_INSTRUMENT_ALL \
  -e KCOV_ENABLE_COMPARISONS \
  -e DEBUG_INFO \
  -e DEBUG_INFO_DWARF4 \
  -e KASAN \
  -e KASAN_GENERIC \
  -e KASAN_INLINE \
  -e CONFIGFS_FS \
  -e SECURITYFS \
  -e KALLSYMS \
  -e KALLSYMS_ALL \
  -e NAMESPACES \
  -e UTS_NS \
  -e IPC_NS \
  -e PID_NS \
  -e NET_NS \
  -e CGROUPS \
  -e CGROUP_PIDS \
  -e MEMCG \
  -e USER_NS \
  -e KPROBES \
  -e FUNCTION_ERROR_INJECTION \
  -e FAULT_INJECTION \
  -e FAULT_INJECTION_DEBUG_FS \
  -e FAIL_FUNCTION \
  -e ZSWAP \
  -d ZSWAP_DEFAULT_ON \
  -e ZSMALLOC \
  -e ZSWAP_ZPOOL_DEFAULT_ZSMALLOC \
  -e CRYPTO_LZO \
  -e BLK_DEV_INITRD \
  -e DEVTMPFS \
  -e DEVTMPFS_MOUNT \
  -e TMPFS \
  -e IKCONFIG \
  -e IKCONFIG_PROC \
  -d RANDOMIZE_BASE \
  -e CMDLINE_BOOL \
  --set-str CMDLINE "net.ifnames=0" \
  --set-str SYSTEM_TRUSTED_KEYS "" \
  --set-str SYSTEM_REVOCATION_KEYS ""

make -C "$src" O="$build" olddefconfig
```

核对关键配置：

```bash
rg -n "^(CONFIG_|# CONFIG_)(WERROR|KCOV|DEBUG_INFO_DWARF4|KASAN|KASAN_INLINE|CONFIGFS_FS|SECURITYFS|FAIL_FUNCTION|ZSWAP|ZSWAP_DEFAULT_ON)" "$build/.config"
```

预期至少包含：

```text
# CONFIG_WERROR is not set
CONFIG_ZSWAP=y
# CONFIG_ZSWAP_DEFAULT_ON is not set
CONFIG_CONFIGFS_FS=y
CONFIG_SECURITYFS=y
CONFIG_DEBUG_INFO_DWARF4=y
CONFIG_KASAN=y
CONFIG_KASAN_INLINE=y
CONFIG_FAIL_FUNCTION=y
CONFIG_KCOV=y
```

### 2. 构建测试内核

分别构建 baseline 和 fix：

```bash
make -C /home/yjc/project/worktree/issue_zswap_baseline \
  O=/home/yjc/project/worktree/build-zswap-baseline-oe \
  -j8 bzImage \
  > /home/yjc/project/worktree/build-zswap-baseline-oe/build.log 2>&1

make -C /home/yjc/project/worktree/issue_zswap_fix \
  O=/home/yjc/project/worktree/build-zswap-fix-oe \
  -j8 bzImage \
  > /home/yjc/project/worktree/build-zswap-fix-oe/build.log 2>&1
```

构建成功后应看到：

```text
Kernel: arch/x86/boot/bzImage is ready
```

产物路径：

```text
/home/yjc/project/worktree/build-zswap-baseline-oe/arch/x86/boot/bzImage
/home/yjc/project/worktree/build-zswap-fix-oe/arch/x86/boot/bzImage
```

### 3. 准备 initramfs

initramfs 使用静态 busybox、bash 及其动态库，再放入 selftest。若目录不存在，可按下面方式重建：

```bash
root=/home/yjc/project/worktree/zswap-initramfs-root
cpio_img=/home/yjc/project/worktree/zswap-test-rootfs.cpio

rm -rf "$root" "$cpio_img"
mkdir -p "$root"/{bin,sbin,proc,sys,dev,tmp,lib/x86_64-linux-gnu,lib64}

cp /usr/bin/busybox "$root/bin/busybox"
cp /usr/bin/bash "$root/bin/bash"
cp /lib/x86_64-linux-gnu/libtinfo.so.6 "$root/lib/x86_64-linux-gnu/"
cp /lib/x86_64-linux-gnu/libc.so.6 "$root/lib/x86_64-linux-gnu/"
cp /lib64/ld-linux-x86-64.so.2 "$root/lib64/"
cp /home/yjc/project/worktree/issue_zswap_fix/tools/testing/selftests/mm/zswap_charp_failure.sh \
  "$root/zswap_charp_failure.sh"
chmod +x "$root/zswap_charp_failure.sh"

for app in sh mount umount grep cat dmesg poweroff reboot sleep printf mkdir tail; do
  ln -s busybox "$root/bin/$app"
done
ln -s ../bin/busybox "$root/sbin/poweroff"
```

写入 `init` 脚本：

```bash
cat > "$root/init" <<'EOF'
#!/bin/sh

export PATH=/bin:/sbin

mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
mount -t debugfs debugfs /sys/kernel/debug 2>/dev/null || true

echo "### zswap charp failure test start"
echo "### cmdline: $(cat /proc/cmdline)"

/bin/bash /zswap_charp_failure.sh ${ZSWAP_TEST_ARGS:-}
ret=$?

echo "### zswap charp failure test exit=${ret}"
echo "### dmesg tail"
dmesg | tail -n 80
echo "### zswap charp failure test done"

poweroff -f
reboot -f
while true; do
	sleep 1
done
EOF
```

写入并打包：

```bash
chmod +x "$root/init"
cd "$root"
find . -print0 | cpio --null -ov --format=newc > "$cpio_img"
```

### 4. 跑 baseline 普通验证

```bash
log=/home/yjc/project/worktree/build-zswap-baseline-oe/qemu-default.log

timeout --foreground 420s qemu-system-x86_64 \
  -m 2048 \
  -smp 2 \
  -machine accel=tcg \
  -kernel /home/yjc/project/worktree/build-zswap-baseline-oe/arch/x86/boot/bzImage \
  -initrd /home/yjc/project/worktree/zswap-test-rootfs.cpio \
  -append "console=ttyS0 earlyprintk=serial rdinit=/init zswap.enabled=0 nokaslr" \
  -display none \
  -serial stdio \
  -monitor none \
  -no-reboot \
  > "$log" 2>&1
```

预期结果：

```text
zpool: old='zsmalloc' new='(null)'
zswap charp failure: FAIL: failed update changed zpool
### zswap charp failure test exit=1
```

### 5. 跑 baseline 崩溃复现

```bash
log=/home/yjc/project/worktree/build-zswap-baseline-oe/qemu-reproduce.log

timeout --foreground 180s qemu-system-x86_64 \
  -m 2048 \
  -smp 2 \
  -machine accel=tcg \
  -kernel /home/yjc/project/worktree/build-zswap-baseline-oe/arch/x86/boot/bzImage \
  -initrd /home/yjc/project/worktree/zswap-test-rootfs.cpio \
  -append "console=ttyS0 earlyprintk=serial rdinit=/init zswap.enabled=0 nokaslr oops=panic panic=1 ZSWAP_TEST_ARGS=--reproduce" \
  -display none \
  -serial stdio \
  -monitor none \
  -no-reboot \
  > "$log" 2>&1
```

预期结果：

```text
zpool: old='zsmalloc' new='(null)'
triggering zswap setup; the test kernel may crash
BUG: kernel NULL pointer dereference, address: 0000000000000000
Oops: 0000 [#1] SMP KASAN NOPTI
RIP: 0010:strcmp+0x10/0x30
zpool_get_driver
zpool_has_pool
__zswap_pool_create_fallback
zswap_setup
zswap_enabled_param_set
Kernel panic - not syncing: Fatal exception
```

### 6. 跑 fix 普通验证

```bash
log=/home/yjc/project/worktree/build-zswap-fix-oe/qemu-default.log

timeout --foreground 420s qemu-system-x86_64 \
  -m 2048 \
  -smp 2 \
  -machine accel=tcg \
  -kernel /home/yjc/project/worktree/build-zswap-fix-oe/arch/x86/boot/bzImage \
  -initrd /home/yjc/project/worktree/zswap-test-rootfs.cpio \
  -append "console=ttyS0 earlyprintk=serial rdinit=/init zswap.enabled=0 nokaslr" \
  -display none \
  -serial stdio \
  -monitor none \
  -no-reboot \
  > "$log" 2>&1
```

预期结果：

```text
zpool: preserved 'zsmalloc' after injected -ENOMEM
compressor: preserved 'lzo' after injected -ENOMEM
zswap: loaded using pool lzo/zsmalloc
zswap charp failure: PASS
### zswap charp failure test exit=0
```

### 7. 跑 fix 崩溃复现对照

```bash
log=/home/yjc/project/worktree/build-zswap-fix-oe/qemu-reproduce.log

timeout --foreground 180s qemu-system-x86_64 \
  -m 2048 \
  -smp 2 \
  -machine accel=tcg \
  -kernel /home/yjc/project/worktree/build-zswap-fix-oe/arch/x86/boot/bzImage \
  -initrd /home/yjc/project/worktree/zswap-test-rootfs.cpio \
  -append "console=ttyS0 earlyprintk=serial rdinit=/init zswap.enabled=0 nokaslr oops=panic panic=1 ZSWAP_TEST_ARGS=--reproduce" \
  -display none \
  -serial stdio \
  -monitor none \
  -no-reboot \
  > "$log" 2>&1
```

预期结果仍然是 PASS，并且不应出现 `BUG:`、`Oops:` 或 `Kernel panic`：

```text
zpool: preserved 'zsmalloc' after injected -ENOMEM
compressor: preserved 'lzo' after injected -ENOMEM
zswap: loaded using pool lzo/zsmalloc
zswap charp failure: PASS
### zswap charp failure test exit=0
```

### 8. 快速提取关键日志

四次运行完成后，可以用下面命令提取核心证据：

```bash
for log in \
  /home/yjc/project/worktree/build-zswap-baseline-oe/qemu-default.log \
  /home/yjc/project/worktree/build-zswap-baseline-oe/qemu-reproduce.log \
  /home/yjc/project/worktree/build-zswap-fix-oe/qemu-default.log \
  /home/yjc/project/worktree/build-zswap-fix-oe/qemu-reproduce.log
do
  echo "### $log"
  rg -n "### zswap|zswap charp|zpool:|compressor:|triggering zswap|BUG:|Oops|Kernel panic|zpool_get_driver|zpool_has_pool|__zswap_pool_create_fallback|zswap_setup|zswap_enabled_param_set|zswap: loaded|exit=" "$log"
done
```

## 验证结果

### 测试配置

按以下顺序生成测试内核配置：

```bash
make O=<build-dir> openeuler_defconfig
make O=<build-dir> kvm_guest.config
scripts/config --file <build-dir>/.config \
  -d WERROR \
  -e KCOV -e KCOV_INSTRUMENT_ALL -e KCOV_ENABLE_COMPARISONS \
  -e DEBUG_INFO -e DEBUG_INFO_DWARF4 \
  -e KASAN -e KASAN_GENERIC -e KASAN_INLINE \
  -e CONFIGFS_FS -e SECURITYFS \
  -e KPROBES -e FUNCTION_ERROR_INJECTION \
  -e FAULT_INJECTION -e FAULT_INJECTION_DEBUG_FS -e FAIL_FUNCTION \
  -e ZSWAP -d ZSWAP_DEFAULT_ON \
  -e ZSMALLOC -e ZSWAP_ZPOOL_DEFAULT_ZSMALLOC \
  -e CRYPTO_LZO \
  -e BLK_DEV_INITRD -e DEVTMPFS -e DEVTMPFS_MOUNT -e TMPFS \
  -e IKCONFIG -e IKCONFIG_PROC \
  -d RANDOMIZE_BASE \
  --set-str CMDLINE "net.ifnames=0" \
  --set-str SYSTEM_TRUSTED_KEYS "" \
  --set-str SYSTEM_REVOCATION_KEYS ""
make O=<build-dir> olddefconfig
```

关键配置确认：

```text
# CONFIG_WERROR is not set
CONFIG_ZSWAP=y
# CONFIG_ZSWAP_DEFAULT_ON is not set
CONFIG_ZSWAP_COMPRESSOR_DEFAULT_LZO=y
CONFIG_ZSWAP_ZPOOL_DEFAULT_ZSMALLOC=y
CONFIG_CONFIGFS_FS=y
CONFIG_SECURITYFS=y
CONFIG_DEBUG_INFO_DWARF4=y
CONFIG_KASAN=y
CONFIG_KASAN_GENERIC=y
CONFIG_KASAN_INLINE=y
CONFIG_FAIL_FUNCTION=y
CONFIG_KCOV=y
CONFIG_KCOV_ENABLE_COMPARISONS=y
CONFIG_KCOV_INSTRUMENT_ALL=y
```

构建产物：

```text
/home/yjc/project/worktree/build-zswap-baseline-oe/arch/x86/boot/bzImage
/home/yjc/project/worktree/build-zswap-fix-oe/arch/x86/boot/bzImage
/home/yjc/project/worktree/zswap-test-rootfs.cpio
```

QEMU 启动命令核心参数：

```bash
qemu-system-x86_64 \
  -m 2048 -smp 2 -machine accel=tcg \
  -kernel <bzImage> \
  -initrd /home/yjc/project/worktree/zswap-test-rootfs.cpio \
  -append "console=ttyS0 earlyprintk=serial rdinit=/init zswap.enabled=0 nokaslr" \
  -display none -serial stdio -monitor none -no-reboot
```

`--reproduce` 复现崩溃路径时额外加入：

```text
oops=panic panic=1 ZSWAP_TEST_ARGS=--reproduce
```

### 基线：参数失败更新破坏旧值

日志：`/home/yjc/project/worktree/build-zswap-baseline-oe/qemu-default.log`

关键输出：

```text
### zswap charp failure test start
### cmdline: net.ifnames=0 console=ttyS0 earlyprintk=serial rdinit=/init zswap.enabled=0 nokaslr
zpool: old='zsmalloc' new='(null)'
zswap charp failure: FAIL: failed update changed zpool
### zswap charp failure test exit=1
```

这证明旧实现中，`kmalloc_parameter()` 被注入失败后，`zpool` 参数从原来的 `zsmalloc` 变成了 `(null)`。

### 基线：延迟 setup 消费坏参数并崩溃

日志：`/home/yjc/project/worktree/build-zswap-baseline-oe/qemu-reproduce.log`

关键输出：

```text
### zswap charp failure test start
### cmdline: net.ifnames=0 console=ttyS0 earlyprintk=serial rdinit=/init zswap.enabled=0 nokaslr oops=panic panic=1 ZSWAP_TEST_ARGS=--reproduce
zpool: old='zsmalloc' new='(null)'
triggering zswap setup; the test kernel may crash
[   18.306254][  T145] BUG: kernel NULL pointer dereference, address: 0000000000000000
[   18.306741][  T145] #PF: supervisor read access in kernel mode
[   18.306741][  T145] Oops: 0000 [#1] SMP KASAN NOPTI
[   18.306741][  T145] RIP: 0010:strcmp+0x10/0x30
```

调用栈：

```text
Call Trace:
 zpool_get_driver+0x93/0x170
 zpool_has_pool+0x17/0xb0
 __zswap_pool_create_fallback+0xcf/0x260
 zswap_setup+0x86/0x430
 zswap_enabled_param_set+0xe5/0x170
 param_attr_store+0x198/0x300
 module_attr_store+0x59/0x90
 sysfs_kf_write+0x1c3/0x260
 kernfs_fop_write_iter+0x3be/0x5e0
 vfs_write+0x68f/0x8b0
 ksys_write+0x12a/0x250
 do_syscall_64+0x55/0x100
 entry_SYSCALL_64_after_hwframe+0x78/0xe2
```

panic 结尾：

```text
[   18.324724][  T145] Kernel panic - not syncing: Fatal exception
```

这证明原始可观察故障并不是单纯的 selftest 失败，而是坏参数被 `zswap_setup()` 消费后真实触发 NULL pointer dereference。

### 修复版：失败更新保持旧值

日志：`/home/yjc/project/worktree/build-zswap-fix-oe/qemu-default.log`

关键输出：

```text
### zswap charp failure test start
### cmdline: net.ifnames=0 console=ttyS0 earlyprintk=serial rdinit=/init zswap.enabled=0 nokaslr
zpool: preserved 'zsmalloc' after injected -ENOMEM
compressor: preserved 'lzo' after injected -ENOMEM
[   17.962878][  T144] zswap: loaded using pool lzo/zsmalloc
zswap charp failure: PASS
### zswap charp failure test exit=0
```

这证明修复后同样的注入条件下，`zpool` 和 `compressor` 都保持旧值，随后 zswap 能用 `lzo/zsmalloc` 正常完成 setup。

### 修复版：同一 reproduce 路径不再崩溃

日志：`/home/yjc/project/worktree/build-zswap-fix-oe/qemu-reproduce.log`

关键输出：

```text
### zswap charp failure test start
### cmdline: net.ifnames=0 console=ttyS0 earlyprintk=serial rdinit=/init zswap.enabled=0 nokaslr oops=panic panic=1 ZSWAP_TEST_ARGS=--reproduce
zpool: preserved 'zsmalloc' after injected -ENOMEM
compressor: preserved 'lzo' after injected -ENOMEM
[   18.963913][  T144] zswap: loaded using pool lzo/zsmalloc
zswap charp failure: PASS
### zswap charp failure test exit=0
```

同一 `oops=panic panic=1` 环境下没有 Oops，没有 panic，并正常关机。

### 检查项

静态检查：

```text
bash -n tools/testing/selftests/mm/zswap_charp_failure.sh
git diff --check
```

结果均通过。

动态 A/B 结果汇总：

| 内核 | 触发方式 | 结果 |
|---|---|---|
| baseline | 注入 `kmalloc_parameter()` 失败 | `zpool` 从 `zsmalloc` 变成 `(null)`，selftest FAIL |
| baseline | 注入失败后写 `enabled=0` | `strcmp(NULL)`，NULL pointer dereference，panic |
| fix | 同样注入失败 | `zpool`、`compressor` 保持旧值，selftest PASS |
| fix | 同样注入失败后写 `enabled=0` | `zswap: loaded using pool lzo/zsmalloc`，无 Oops，PASS |

## 结论

修复命中了根因不变量：`param_set_charp()` 的失败路径不应改变旧参数值。A/B 验证证明旧内核可以稳定复现参数损坏和后续 zswap NULL pointer dereference；修复后同一 fault injection、同一 QEMU/initramfs、同一 zswap 延迟初始化路径下，参数保持有效，zswap setup 正常完成，不再崩溃。
