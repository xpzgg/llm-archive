# shmem soft-offline evict PoC 使用说明

PoC 源码：配套的 `shmem_soft_offline_evict_poc.c`。

## 依赖

- 内核 `CONFIG_MEMORY_FAILURE=y`，否则 `MADV_SOFT_OFFLINE` 直接返回 EINVAL。
- 建议 `panic_on_warn=1`（boot cmdline 或 `sysctl kernel.panic_on_warn=1`）：
  未修复内核上 WARN 升级为 panic，现象最明确；不开也能看到 WARN + call
  trace，只是不 panic。
- guest 内需要 root（`MADV_SOFT_OFFLINE` 需要 CAP_SYS_ADMIN）。
- 多 vCPU 的 KVM guest 最容易复现（vCPU 抢占放大竞态窗口）；裸机也能中，
  时间更长。
- 复现率对内核配置敏感：实测用 issue 附带的 config 几分钟内必中；自裁剪的
  defconfig 可能慢一个量级，复现不出来时先怀疑 config。

## 构建

```sh
gcc -O2 -pthread -o shmem_soft_offline_evict_poc shmem_soft_offline_evict_poc.c
```

极简 rootfs 可加 `-static`。

## 运行

```sh
timeout 600 ./shmem_soft_offline_evict_poc -p 4 -r 4 -n 0
```

参数：

- `-p N`：`madvise(MADV_SOFT_OFFLINE)` 线程数，默认 4
- `-r N`：`MAP_FIXED` 替换线程数，默认 4
- `-n N`：每个 adviser 的 madvise 次数，`0` = 无限循环（配合 `timeout` 控窗口）
- `-l LEN` / `-a ADDR` / `-d USEC`：映射长度 / 固定地址 / madvise 间隔延时，
  一般用默认值

另开一路观察 dmesg：

```sh
dmesg -w | grep -E 'invalidated|shmem_evict|WARNING|Kernel panic'
```

**注意：判定必须看 dmesg，不能只看串口控制台**。串口默认
`console_loglevel=4`，只放 ERR 及以上级别：`invalidated`（pr_info）、
`WARNING:` 横幅、修复后的 pr_warn 行都不会出现在串口上，串口只能看到最后的
panic。如需在串口看全过程：`echo 8 > /proc/sys/kernel/printk`，或 boot cmdline
加 `loglevel=8`。未修复内核若串口只有 `Kernel panic` 没有 WARNING 横幅，是
loglevel 过滤，不是没触发。

**前置确认**：dmesg 里要能看到 `Soft offline: 0x...: invalidated`，说明打到了
evict 快速路径；几分钟都没有说明没打到关键路径（查 config、是否 root）。

## 判定

未修复内核（`WARN_ON(inode->i_blocks)`）：

```text
Soft offline: 0x56b64: invalidated
WARNING: ... at mm/shmem.c:... shmem_evict_inode+0x...
Kernel panic - not syncing: kernel: panic_on_warn set ...
```

KVM guest 内通常几十秒到几分钟 panic（实测 14s~300s），概率性触发；跑 10 分钟
不 panic 基本可认为没打到。

修复后内核（WARN_ON 降级为 pr_warn）：

```text
shmem_evict_inode: ino=573864 i_blocks=8 alloced=1 swapped=0 nrpages=0
```

这行是降级后的预期打印，说明撞中过竞态且被记录下来，**不是故障**。通过标准：
跑满窗口（建议 ≥10 分钟，严格 30 分钟）无 `WARNING:`、无 `Kernel panic`，
PoC 进程正常存活。
