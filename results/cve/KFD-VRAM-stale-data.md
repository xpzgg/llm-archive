# KFD VRAM stale data exposure

## 一、根因分析

- **代码位置**：`drivers/gpu/drm/amd/amdgpu/amdgpu_amdkfd_gpuvm.c`，`amdgpu_amdkfd_gpuvm_alloc_memory_of_gpu()` 函数

- **根因**：内核中有两条 VRAM 分配路径——GEM ioctl 路径（`amdgpu_gem_create_ioctl`）和 KFD 路径（`amdgpu_amdkfd_gpuvm_alloc_memory_of_gpu`）。GEM 路径对**所有** userspace VRAM 分配都设置了 `AMDGPU_GEM_CREATE_VRAM_CLEARED` 标志（`amdgpu_gem.c:429` "always clear VRAM"），确保分配时将 VRAM 清零。而 KFD 路径只设置了 `AMDGPU_GEM_CREATE_VRAM_WIPE_ON_RELEASE`（释放时擦除），**遗漏了分配时的清零**。

  这导致通过 KFD（compute 场景，如 ROCm/RCCL）分配的 VRAM 中保留了上一次使用该内存区域的数据（stale data）。compute kernel 能直接观察到前一个进程残留的数据，形成信息泄露。commit message 中提到的 RCCL P2P 崩溃是实际触发场景——`ptrExchange/head/tail` 字段中的非零残留数据破坏了协议握手。

  开发者在添加 `VRAM_WIPE_ON_RELEASE`（释放时擦除，防止释放后泄露）时，没有同步加上 `VRAM_CLEARED`（分配时清零，防止分配后观察到旧数据）。这是一个**单向防护**的遗漏——保护了一个方向（释放），忽略了另一个方向（分配）。从 commit message 来看，GEM 路径的实现者显然意识到了"always clear VRAM"的必要性，但这个 invariant 没有传播到 KFD 路径，两条路径的初始化策略不一致。

---

## 二、修复逻辑分析

- **修复思路**：补上缺失的 `AMDGPU_GEM_CREATE_VRAM_CLEARED` 标志，使 KFD 路径的 VRAM 分配行为与 GEM ioctl 路径一致。

- **逐个改动解读**：

  唯一的改动：在 `alloc_flags` 赋值中，将 `AMDGPU_GEM_CREATE_VRAM_WIPE_ON_RELEASE` 改为 `AMDGPU_GEM_CREATE_VRAM_WIPE_ON_RELEASE | AMDGPU_GEM_CREATE_VRAM_CLEARED`。

  两个标志协同工作，形成完整的生命周期保护：
  - `VRAM_CLEARED`：**分配时**清零，防止 compute kernel 观察到前一个用户的残留数据
  - `VRAM_WIPE_ON_RELEASE`：**释放时**擦除，防止下一个用户在分配前通过其他手段读到当前用户的数据

  这只影响 VRAM 分支（`flags & KFD_IOC_ALLOC_MEM_FLAGS_VRAM` && `!adev->apu_prefer_gtt`）。GTT 分支和 APU 路径不受影响。

- **为什么有效**：`VRAM_CLEARED` 标志在 BO 创建流程中会被 amdgpu 的 ttm 层识别，触发 DMA 清零操作，保证分配给 compute kernel 的 VRAM 初始内容全零。消除了 stale data 的信息泄露路径。

---

## 三、摘要

**【问题根因】**
KFD 路径的 VRAM 分配（`amdgpu_amdkfd_gpuvm_alloc_memory_of_gpu`）仅设置了释放时擦除标志（`VRAM_WIPE_ON_RELEASE`），遗漏了分配时清零标志（`VRAM_CLEARED`），导致 compute kernel 可观察到前一个使用者残留的 VRAM 数据，造成信息泄露。

**【修复逻辑】**
在 KFD VRAM 分配路径中补充 `AMDGPU_GEM_CREATE_VRAM_CLEARED` 标志，使分配时行为与 GEM ioctl 路径一致，确保新分配的 VRAM 初始内容全零。

---

## 四、Kconfig 依赖

```
CONFIG依赖：CONFIG_DRM_AMDGPU=y || CONFIG_DRM_AMDGPU=m 则涉及。
KO依赖：如果CONFIG_DRM_AMDGPU以=m的形式打开的情况下，则可排查amdgpu.ko是否被加载，没有被加载则不涉及。
```

---

## 五、引入 Commit 分析

Patch 无 `Fixes:` 标签。

引入 Commit：6856e4b65f64 ("drm/amdgpu: Mark KFD VRAM allocations for wipe on release")

论证：该 commit 将 KFD VRAM 分配路径中的 alloc_flags 从 AMDGPU_GEM_CREATE_VRAM_CLEARED 改为 AMDGPU_GEM_CREATE_VRAM_WIPE_ON_RELEASE。commit message 明确说明"Stop clearing memory at allocation time. Instead mark the memory for wipe on release"，开发者有意移除了分配时清零，认为 wipe on release 更合适。但这个决策只覆盖了释放时防护，遗漏了分配时 stale data 的泄露风险。git log -S "VRAM_CLEARED" 在该文件中仅返回两个 commit：6856e4b65f64（移除）和 ad52d61（恢复），确认漏洞就在这个替换。

验证：6856e4b65f64 之前，KFD VRAM 分配使用 VRAM_CLEARED（由更早的 commit a46a2cd103a8 引入），分配时 VRAM 会被清零，stale data 不可被观察到。6856e4b65f64 移除 VRAM_CLEARED 后，分配时的清零保护消失，漏洞开始可触发。
