# zswap charp 参数空指针问题复现说明

## 问题简介

zswap 的 `zpool` / `compressor` 是 `charp` 类型模块参数。通用参数处理函数
`param_set_charp()`（`kernel/params.c`）在旧实现中先释放旧字符串、再分配新字符串；
一旦为新值分配内存失败，sysfs 写入返回 `-ENOMEM`，但参数指针已被置为 `NULL`。

之后触发 zswap 初始化（写 `zswap.enabled`）时，调用链：

```text
zswap_enabled_param_set()
  zswap_setup()
    __zswap_pool_create_fallback()
      zpool_has_pool()
        zpool_get_driver()
          strcmp(driver->type, NULL)   # NULL pointer dereference -> kernel panic
```

修复方式：先分配并复制新字符串，成功后再释放旧值（修复 commit 只改
`kernel/params.c`，见同目录 `0001-params-fix-charp-corruption-on-allocation-failure.patch`）。

## 依赖的内核 config

```text
CONFIG_ZSWAP=y
# CONFIG_ZSWAP_DEFAULT_ON is not set   # 保证启动后 zswap.enabled=N；否则需内核命令行加 zswap.enabled=0
CONFIG_DEBUG_FS=y
CONFIG_FAULT_INJECTION=y
CONFIG_FAULT_INJECTION_DEBUG_FS=y
CONFIG_FAILSLAB=y
```

另外 QEMU 使用 ext4 virtio rootfs，需要 `CONFIG_VIRTIO_PCI/BLK/NET=y`、`CONFIG_EXT4_FS=y`
（openeuler_defconfig 默认已满足）。

## 测试前准备

1. 用上述 config 分别构建 baseline（未修复）和 fix（已修复）两版 bzImage。
2. QEMU 启动，串口 console、virtio-blk rootfs（Ubuntu rootfs，账号 `root/root`、`syz/syz`），
   host 端口 10022 转发 guest ssh。启动示例：

   ```bash
   sudo /home/yjc/project/agent_workdir/tools/qemu/run-qemu.sh <bzImage>
   ```

3. 把 PoC 拷进 guest：

   ```bash
   scp -P 10022 zswap-charp-null-poc.sh syz@127.0.0.1:/home/syz/
   ```

## PoC 使用

guest 内以 root 执行：

```bash
sudo bash /home/syz/zswap-charp-null-poc.sh
```

PoC 原理：通过 failslab + `/proc/self/task/<tid>/fail-nth` 逐次让当前进程的 slab
分配失败，同时把 `zpool` 参数写回当前值，命中分配失败后检查参数是否被污染为
`(null)`；若污染成功则写 `zswap.enabled` 触发 zswap setup。

## 预期结果

- **vulnerable（baseline）内核**：PoC 在某个 fail-nth（实测为 5）把 `zpool` 从
  `zsmalloc` 污染为 `(null)`，随后触发 zswap setup 时 NULL dereference，kernel panic：

  ```text
  fail-nth=5 write_rc=1 zpool=(null)
  corrupted zpool: old='zsmalloc' new='(null)'
  BUG: kernel NULL pointer dereference, address: 0000000000000000
  RIP: 0010:strcmp+0x10/0x30
  Call Trace: ... zpool_get_driver ... zswap_setup ... zswap_enabled_param_set ...
  Kernel panic - not syncing: Fatal exception
  ```

- **fixed 内核**：遍历 fail-nth=1..64 均无法污染参数，写入失败时参数保持旧值，
  PoC 输出 `failed to corrupt zpool within MAX_FAIL_NTH=64` 并以退出码 1 结束，
  内核无异常。

