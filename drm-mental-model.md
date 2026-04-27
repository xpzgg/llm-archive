# Linux DRM 模块心智模型

## 1. 功能脑图

```
                                ┌─────────────────────────────────────────────┐
                                │              Linux DRM Subsystem              │
                                │         drivers/gpu/drm/  94 个核心文件       │
                                └─────────────────────────────────────────────┘
                                                      │
      ┌───────────┬───────────┬───────────┬───────────┼───────────┬───────────┬───────────┐
      v           v           v           v           v           v           v           v
 ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐
 │ 设备管理 │ │ KMS /   │ │  内存    │ │ 显示/   │ │  GPU    │ │  同步   │ │Frame-   │ │  辅助   │
 │ 生命周期 │ │ Atomic  │ │  管理    │ │ EDID    │ │ 调度    │ │  机制   │ │buffer   │ │  工具   │
 └─────────┘ └─────────┘ └─────────┘ └─────────┘ └─────────┘ └─────────┘ └─────────┘ └─────────┘
      │           │           │           │           │           │           │           │
 ┌────┴────┐ ┌────┴────┐ ┌────┴────┐ ┌────┴────┐ ┌────┴────┐ ┌────┴────┐ ┌────┴────┐ ┌────┴────┐
 │drm_drv  │ │对象模型 │ │GEM 核心 │ │EDID    │ │Entity   │ │dma-fence│ │创建/销毁│ │debugfs  │
 │ ─────── │ │ ─────── │ │ ─────── │ │ ─────── │ │ ─────── │ │ ─────── │ │ ─────── │ │ ─────── │
 │alloc    │ │CRTC     │ │init    │ │parse   │ │init     │ │signaled │ │AddFB2   │ │info     │
 │register │ │ 扫描时序│ │handle  │ │ 基本块 │ │push_job │ │wait     │ │RmFB     │ │CRC      │
 │open     │ │         │ │mmap    │ │ 扩展块 │ │rq select│ │context  │ │dumb buf │ │         │
 │release  │ │Plane    │ │pages   │ │        │ │         │ │         │ │         │ │fbdev    │
 │ioctl    │ │ primary │ │import  │ │Display-│ │Job      │ │syncobj  │ │GEM 绑定 │ │ 模拟    │
 │         │ │ cursor  │ │export  │ │  ID    │ │ init    │ │ ─────── │ │         │ │         │
 │auth     │ │ overlay │ │        │ │ 扩展   │ │ arm     │ │timeline │ │mmap     │ │panic    │
 │ Master  │ │         │ │分配器  │ │        │ │ fence   │ │point    │ │ GEM     │ │ 屏      │
 │ 鉴权    │ │Encoder  │ │ ─────── │ │Display-│ │         │ │wait     │ │ 对象    │ │         │
 │         │ │ 编码器  │ │Buddy   │ │  Port  │ │Sched    │ │         │ │         │ │lease    │
 │lease    │ │         │ │ MM     │ │ DP AUX │ │ init    │ │vblank   │ │         │ │ 多客户端│
 │ 资源租  │ │Bridge   │ │        │ │ DP MST │ │ start   │ │ 中断    │ │         │ │ 分离    │
 │ 约分离  │ │ 桥接链  │ │GEM 类型│ │ DSC     │ │ stop    │ │ 帧计数  │ │         │ │         │
 │         │ │         │ │ ─────── │ │ 压缩流 │ │ fault   │ │ fence   │ │         │ │RAS      │
 │         │ │Panel    │ │DMA/CMA │ │ HDR    │ │         │ │ 创建    │ │         │ │ netlink │
 │         │ │ MIPI面板│ │ 连续物理│ │ HDCP   │ │         │ │         │ │         │ │         │
 │         │ │         │ │SHMEM   │ │ 内容   │ │         │ │         │ │         │ │fourcc   │
 │         │ │Property │ │ 共享内存│ │ 保护   │ │         │ │         │ │         │ │ 格式    │
 │         │ │ blob    │ │VRAM    │ │        │ │         │ │         │ │         │ │         │
 │         │ │ enum    │ │ 独立显存│ │Audio   │ │         │ │         │ │         │ │draw     │
 │         │ │ range   │ │ + TTM  │ │ ELD    │ │         │ │         │ │         │ │ 内核    │
 │         │ │         │ │ 迁移驱逐│ │ 音频   │ │         │ │         │ │         │ │ 绘制    │
 │         │ │Atomic   │ │        │ │        │ │         │ │         │ │         │ │         │
 │         │ │ 状态机  │ │PRIME   │ │MIPI    │ │         │ │         │ │         │ │trace    │
 │         │ │ ─────── │ │ dma-buf│ │ DSI    │ │         │ │         │ │         │ │         │
 │         │ │commit   │ │ 跨设备 │ │ DBI    │ │         │ │         │ │         │ │pagemap  │
 │         │ │check    │ │ 共享   │ │        │ │         │ │         │ │         │ │ 页表    │
 │         │ │swap     │ │        │ │        │ │         │ │         │ │         │ │         │
 │         │ │tail     │ │GPUVM   │ │Color   │ │         │ │         │ │         │ │modes    │
 │         │ │         │ │ 虚拟   │ │ Mgmt   │ │         │ │         │ │         │ │ 模式    │
 │         │ │Blend    │ │ 地址   │ │ gamma  │ │         │ │         │ │         │ │ 解析    │
 │         │ │ zpos    │ │ 空间   │ │ CTM    │ │         │ │         │ │         │ │         │
 │         │ │ alpha   │ │        │ │ degamma│ │         │ │         │ │         │ │format   │
 │         │ │         │ │SVM     │ │        │ │         │ │         │ │         │ │ helper  │
 │         │ │Damage   │ │ 共享   │ │Damage  │ │         │ │         │ │         │ │ 格式    │
 │         │ │ 脏区域  │ │ 虚拟   │ │ 脏区域 │ │         │ │         │ │         │ │ 转换    │
 │         │ │ 跟踪    │ │ 内存   │ │ 追踪   │ │         │ │         │ │         │ │         │
 └─────────┘ └─────────┘ └─────────┘ └─────────┘ └─────────┘ └─────────┘ └─────────┘ └─────────┘

                        ┌─────────────────────────────────────────────────┐
                        │                   硬件驱动层                     │
                        │ i915  amdgpu  nouveau  msm  panfrost  etnaviv  │
                        │ vmwgfx  vkms  simpledrm  ...  共 60+ 个驱动     │
                        └─────────────────────────────────────────────────┘
```

## 2. 核心调用栈

### 2.1 设备初始化 ── 从 drm_dev_alloc 到用户态可用

```
userspace: open("/dev/dri/card0")
────────────────────────────────────────────
kernel:
  drm_dev_alloc()                          ← 分配 drm_device，绑定 drm_driver
    ├─ drm_mode_config_init()              ← 初始化 mode_config (KMS 基座)
    ├─ drm_client_setup()                  ← 内核客户端 (fbdev 等)
    └─ ...                                 ← 驱动特定初始化

  drm_dev_register()                       ← 向 DRM 核心注册
    ├─ drm_minor_register()                ← 创建设备号 /dev/dri/cardX, renderD
    ├─ drm_sysfs_connector_add()           ← sysfs 暴露连接器
    └─ drm_debugfs_register()              ← debugfs 节点

  drm_open()            [drm_file.c]       ← 用户 open → 创建 drm_file
    ├─ drm_file_alloc()
    ├─ driver->open()                      ← 驱动回调 (如 gem_context_create)
    └─ drm_client_dev_restore()            ← 恢复之前的内核客户端配置
```

### 2.2 Atomic Commit ── 一次模式设置/翻页的完整路径

```
userspace: ioctl(DRM_IOCTL_MODE_ATOMIC)
────────────────────────────────────────────────────────────────
kernel:
  drm_ioctl()                  [drm_ioctl.c]         ← ioctl 总入口
    └─ drm_mode_atomic_ioctl() [drm_atomic_uapi.c]   ← 原子 IOCTL handler
         ├─ drm_atomic_state_alloc()                  ← 分配 atomic state
         ├─ 遍历用户提交的 property，填充 state
         └─ drm_atomic_commit()    [drm_atomic.c]     ← ★ 核心入口
              │
              ├─ drm_atomic_check_only()              ← 阶段1: 校验
              │    ├─ drm_atomic_helper_check()       ← 遍历对象检查
              │    │    ├─ drm_atomic_helper_check_planes()
              │    │    ├─ drm_atomic_helper_check_crtcs()
              │    │    └─ driver->atomic_check()     ← 驱动自定义检查
              │    └─ ...
              │
              ├─ drm_atomic_helper_setup_commit()     ← 阶段2: 准备 commit
              │    ├─ drm_crtc_commit_wait()          ← 等待之前的 commit 完成
              │    └─ 为每个 CRTC 分配 commit 结构
              │
              ├─ drm_atomic_helper_prepare_planes()   ← 阶段3: 准备 FB
              │    └─ driver->prepare_fb()            ← 驱动准备 FB (pin, 等)
              │
              ├─ drm_atomic_helper_swap_state()       ← ★ 阶段4: 原子交换
              │    └─ 新旧 state 指针交换 —— Point of no return
              │
              ├─ drm_atomic_state_get()               ← 拿住旧 state
              │
              └─ drm_atomic_helper_commit()           ← 阶段5: 提交硬件
                   │
                   └─ drm_atomic_helper_commit_tail()  ← ★ 驱动可覆写
                        │
                        ├─ drm_atomic_helper_wait_for_fences()  ← 等待 implicit fence
                        │
                        ├─ drm_atomic_helper_commit_modeset_disables()
                        │    └─ 关闭不再需要的 CRTC/Encoder
                        │
                        ├─ drm_atomic_helper_commit_planes()    ← 配置 plane (FB地址等)
                        │    ├─ drm_atomic_helper_commit_planes_on_crtc()
                        │    └─ driver->atomic_update()         ← 驱动写寄存器
                        │
                        ├─ drm_atomic_helper_commit_modeset_enables()
                        │    └─ 启用新的 CRTC/Encoder
                        │
                        ├─ drm_atomic_helper_commit_hw_done()   ← 硬件已配置完成
                        │
                        └─ drm_atomic_helper_wait_for_vblanks() ← 等待 vblank
                             └─ drm_crtc_vblank_wait()          ← 确保显示到屏幕上
```

### 2.3 GEM Buffer 生命周期 ── 创建 → mmap → 渲染 → 显示

```
userspace: ioctl(DRM_IOCTL_MODE_ADDFB2)
                          │                    userspace: ioctl(DRM_IOCTL_GEM_OPEN)
                          v                    userspace: mmap()
────────────────────────────────────────────────────────────────────────────
kernel:
  drm_mode_addfb2()               [drm_framebuffer.c]
    ├─ drm_mode_addfb2_ioctl()
    ├─ drm_framebuffer_init()                    ← 初始化 framebuffer 对象
    └─ 关联 GEM handle → GEM object

  drm_gem_handle_create()         [drm_gem.c]    ← 创建用户态 handle
    └─ 分配 handle id，插入 file->object_idr

  drm_gem_open_ioctl()            [drm_gem.c]
    └─ 跨进程共享: 同名 handle → 不同 GEM object

  drm_gem_mmap()                  [drm_gem.c]    ← mmap GEM 到用户态
    ├─ drm_gem_mmap_obj()
    ├─ driver->gem_mmap()                        ← 驱动实际 mmap
    │    ├─ drm_gem_shmem_mmap()   (SHMEM)
    │    ├─ drm_gem_prime_mmap()   (PRIME/dma-buf)
    │    └─ drm_gem_dma_mmap()     (DMA/CMA)
    └─ 返回虚拟地址给用户态

  GEM 跨设备共享:
  drm_gem_prime_export()          [drm_prime.c]
    ├─ driver->gem_prime_export()                ← 导出为 dma-buf fd
    └─ dma_buf_export()

  drm_gem_prime_import()          [drm_prime.c]
    ├─ dma_buf_attach()
    └─ driver->gem_prime_import_sg_table()       ← 导入为 GEM object
```

### 2.4 GPU 命令提交 ── DRM Scheduler

```
userspace: ioctl(DRM_IOCTL_*)  (驱动特定: 如 amdgpu_cs_ioctl)
────────────────────────────────────────────────────────────────
kernel (驱动代码):
  driver->submit_job()
    ├─ drm_sched_job_init()           [sched_main.c]  ← 初始化 job
    │    └─ 关联 entity + fence + callback
    │
    ├─ drm_sched_job_arm()            [sched_main.c]  ← 就绪
    │    └─ 设置 job->s_fence->scheduled
    │
    └─ drm_sched_entity_push_job()    [sched_entity.c] ← 推入调度队列
         │
         ├─ drm_sched_entity_select_rq()              ← 选择合适的 run queue
         ├─ 将 job 加入 entity 的队列
         └─ drm_sched_wakeup()         [sched_main.c] ← 唤醒调度线程
              │
              ▼
    [scheduler thread]   drm_sched_main()             ← 调度主循环
      ├─ drm_sched_entity_pop_job()   [sched_entity.c]
      ├─ job->sched->ops->run_job()                   ← 驱动回调: 提交到 GPU ring
      │    └─ 将命令写入 ring buffer → GPU 开始执行
      │
      └─ [GPU 完成后]
           ├─ drm_sched_job_done()     [sched_main.c]
           ├─ drm_sched_fence_finished() [sched_fence.c]
           └─ dma_fence_signal()                      ← 通知所有等待者
                ├─ dma_fence_chain_walk()             ← 遍历 fence chain
                ├─ 唤醒 syncobj timeline waiter
                └─ 唤醒 implicit fence waiter (GEM dma_resv)
```

### 2.5 热插拔 ── 显示器连接/断开

```
[HPD interrupt / 驱动 poll 线程]
──────────────────────────────────────
  driver->handle_hpd_irq()
    │
    ├─ drm_helper_hpd_irq_event()       ← 调度热插拔处理
    │
    └─ drm_kms_helper_hotplug_event()   [drm_probe_helper.c]
         │
         └─ drm_client_dev_hotplug()
              │
              ├─ drm_helper_probe_detect()            ← 探测连接器状态
              │    └─ connector->detect()             ← 驱动读 HPD / EDID
              │
              ├─ drm_helper_probe_single_connector_modes()
              │    ├─ drm_edid_read()                 ← 读取 EDID
              │    ├─ drm_edid_connector_update()     ← 更新连接器属性
              │    ├─ drm_edid_to_eld()               ← 解析音频 ELD
              │    └─ drm_mode_prune_invalid()        ← 过滤无效模式
              │
              └─ drm_client_modeset_probe()           ← 内核客户端重新配置
                   └─ drm_client_modeset_commit_atomic()
                        └─ drm_atomic_commit()        ← 走 atomic commit 流程
```

### 2.6 VBlank 同步 ── 帧同步基础

```
userspace: ioctl(DRM_IOCTL_WAIT_VBLANK)
────────────────────────────────────────
  drm_wait_vblank_ioctl()              [drm_vblank.c]
    ├─ drm_crtc_vblank_count()         ← 获取当前 vblank 计数
    ├─ drm_crtc_vblank_get()           ← 持有 vblank 引用 (使能中断)
    │
    └─ [等待目标 sequence]
         ├─ wait_event_interruptible_timeout()        ← 睡眠等待
         │    └─ [vblank 中断触发]
         │         ├─ drm_handle_vblank()             ← 更新 vblank 计数
         │         ├─ drm_crtc_send_vblank_event()    ← 发送 vblank event
         │         └─ wake_up()                       ← 唤醒等待者
         │
         └─ drm_crtc_vblank_put()                     ← 释放引用

  drm_crtc_arm_vblank_event()           ← 为下一次 vblank 预约事件
    └─ 发送 page-flip 完成事件 (CRTC_EVENT_FLIP_COMPLETE) 给等待的客户端
```

## 3. 数据流总览

```
                        用户态
        ┌─────────────────┼─────────────────┐
        │ Mesa / libdrm   │                  │
        │                 │                  │
   open │        ioctl    │     mmap        │ poll / read
        v                 v                  v
  ╔══════════════════════════════════════════════╗
  ║              /dev/dri/card0                    ║
  ╠════════════════════════════════════════════════╣
  ║ drm_open()  drm_ioctl()  drm_gem_mmap()  drm_read() ║
  ║    │            │              │              ║
  ║    v            v              v              v
  ║ drm_file    dispatch     GEM obj      event queue
  ║                             ^
  ║                             │
  ║                    ┌────────┴────────┐
  ║                    │   dma-buf fd    │← PRIME 跨设备共享
  ║                    │   scather/gather│
  ║                    └────────┬────────┘
  ║                             │
  ║                    drm_gem_prime_import()
  ╚════════════════════════════╤═════════════════╝
                                │
                    ┌───────────┴───────────┐
                    │     Hardware driver   │
                    │  (i915/amdgpu/nouveau)│
                    └───────────────────────┘
```
