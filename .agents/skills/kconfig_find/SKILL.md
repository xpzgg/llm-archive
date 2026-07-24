---
name: kconfig_find
description: >
  Linux 内核 .o 文件到 Kconfig 的依赖查找助手。当用户询问某个内核模块或目标文件
  （如 nouveau_gem.o、amdgpu.o）由哪个 Kconfig 选项控制编译时触发。也适用于用户
  拿到一个陌生宏（如 CONFIG_DRM_NOUVEAU）想确认它在 Makefile 中如何驱动编译时触发。
---

# Kernel Config Dependency Finder

查找内核某 `.o` 文件依赖哪个 Kconfig 选项（`CONFIG_*`）来控制编译。**所有回复使用中文，技术术语保留英文原文。**

---

## 查找方法

Kbuild 系统中 `.o` → Kconfig 的依赖关系分两层，**倒着查**：

### 标准三步流程

```
Step 1: 在 .o 所在目录的 Makefile 中查找文件名
Step 2: 确认它是独立受控还是随其他 obj 捆绑
Step 3: 往顶层目录追查目录级的 CONFIG 门控
```

### 具体命令

```bash
# Step 1 — 在子目录 Makefile 找 .o 的引用
grep '<target>.o' drivers/<subsystem>/<driver>/Makefile

# 结果解读：
#   obj-$(CONFIG_FOO) += <target>.o     → 独立受 CONFIG_FOO 控制
#   <driver>-y += <target>.o            → 随 <driver>.o 一起编译
#   <driver>-$(CONFIG_FOO) += <target>.o → 受 CONFIG_FOO 条件加入

# Step 2 — 如果是 xxx-y 引用，往上层追
grep 'CONFIG_<DRIVER>' drivers/<subsystem>/Makefile

# Step 3 — 必要时继续往上追到 drivers/Makefile
grep 'CONFIG_<SUBSYSTEM>' drivers/Makefile
```

### 特殊情况

- **`xxx-y += foo.o`** 中的 `foo.o` 没有自己的 CONFIG，它随 `xxx.o` 的整体 CONFIG 走。`xxx.o` 的 config 在同一个 Makefile 或上层 Makefile 中定义
- **`obj-y += foo.o`** 表示无条件编译（少见，通常用于核心基础设施）
- **`lib-y` / `obj-m`** 分别对应编译进 lib 或编译为模块

---

## 输出格式

### 依赖链路

```
<target>.o
  └─ <driver>/Makefile: <driver>-y += <target>.o
  └─ <driver>/Makefile: obj-$(CONFIG_XXX) += <driver>.o
  └─ <subsystem>/Makefile: obj-$(CONFIG_XXX) += <driver>/
```

如需要，附上 grep 验证命令供用户复现。

---

## 分析约束

- **优先在用户指定的内核树（如 /home/yjc/project/linux）中查找**。若用户未指定路径，询问内核源码根目录位置
- 严格基于 Makefile 实际内容分析，不得臆测
- 如 `.o` 可能由 `ifdef` / `ifneq` / `ifeq` 等条件控制，需一并说明
