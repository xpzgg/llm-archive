# Linux DRM 模块心智模型

<br/>

## 一、功能脑图

```
                                 ╔══════════════════════════════════════╗
                                 ║       Linux DRM Subsystem           ║
                                 ║   drivers/gpu/drm/  (94 核心文件)   ║
                                 ╚══════════════════════════════════════╝
                                                  │
          ┌──────────┬──────────┬──────────┬──────┼──────┬──────────┬──────────┬──────────┐
          │          │          │          │      │      │          │          │          │
          v          v          v          v      v      v          v          v          v
    ╔══════════╗╔══════════╗╔══════════╗╔══════════╗╔══════════╗╔══════════╗╔══════════╗╔══════════╗
    ║ 1.设备   ║║ 2.KMS /  ║║ 3.内存   ║║ 4.显示/  ║║ 5.GPU    ║║ 6.同步   ║║ 7.Frame- ║║ 8.辅助   ║
    ║   生命   ║║   Atomic ║║   管理    ║║   EDID   ║║   调度    ║║   机制   ║║   buffer ║║   工具   ║
    ║   周期   ║║          ║║           ║║          ║║           ║║          ║║          ║║          ║
    ╚══════════╝╚══════════╝╚══════════╝╚══════════╝╚══════════╝╚══════════╝╚══════════╝╚══════════╝
         │            │            │            │            │            │            │            │
         v            v            v            v            v            v            v            v
```

| # | 模块 | 核心文件 | 职责 |
|---|------|---------|------|
| 1 | **设备生命周期** | `drm_drv.c` `drm_file.c` `drm_ioctl.c` | `alloc` → `register` → `open` → `ioctl` → `release`; DRM-Master 鉴权、多客户端租约分离 |
| 2 | **KMS / Atomic** | `drm_atomic.c` `drm_atomic_helper.c` `drm_crtc.c` `drm_plane.c` `drm_connector.c` `drm_encoder.c` `drm_bridge.c` `drm_panel.c` `drm_property.c` `drm_blend.c` `drm_modeset_lock.c` | CRTC / Plane / Encoder / Connector / Bridge / Panel 对象模型; Atomic 状态机 (`check` → `swap` → `commit_tail`); Property (blob/enum/range); zpos/alpha 混合; 自刷新 (PSR) |
| 3 | **内存管理** | `drm_gem.c` `drm_gem_dma_helper.c` `drm_gem_shmem_helper.c` `drm_gem_vram_helper.c` `drm_prime.c` `drm_buddy.c` `drm_mm.c` `drm_gpuvm.c` `drm_gpusvm.c` | GEM 对象 (handle/mmap/pages/import/export); 分配器 (Buddy / MM range); DMA-CMA / SHMEM / VRAM+TTM 三种后端; PRIME dma-buf 跨设备共享; GPUVM 虚拟地址 / GPUSVM 共享虚拟内存 |
| 4 | **显示 / EDID** | `drm_edid.c` `drm_dp_helper.c` `drm_dp_mst_helper.c` `drm_dsc.c` `drm_dsi.c` `drm_hdmi_helper.c` `drm_hdr.c` `drm_eld.c` `drm_color_mgmt.c` `drm_damage_helper.c` | EDID/DisplayID 解析; DisplayPort (AUX/MST/DSC); HDMI; MIPI DSI/DBI; HDR/HDCP; 音频 ELD; Color Management (gamma/CTM/degamma); Damage Tracking |
| 5 | **GPU 调度** | `scheduler/sched_main.c` `scheduler/sched_entity.c` `scheduler/sched_fence.c` | Entity (提交队列) → Job (工作项) → Run Queue → 调度线程执行 → Fence 完成信号; 超时恢复; 优先级 |
| 6 | **同步机制** | `drm_syncobj.c` `drm_vblank.c` | dma-fence (implicit sync); syncobj timeline (explicit 用户态同步); VBlank 中断 / 帧计数 / fence 创建 |
| 7 | **Framebuffer** | `drm_framebuffer.c` `drm_dumb_buffers.c` `drm_gem_framebuffer_helper.c` | AddFB2 / RmFB; FB 与 GEM 绑定; dumb buffer 便捷分配; FB dirty 标记 |
| 8 | **辅助工具** | `drm_debugfs.c` `drm_fb_helper.c` `drm_format_helper.c` `drm_fourcc.c` `drm_panic.c` `drm_lease.c` `drm_ras.c` `drm_modes.c` `drm_trace_points.c` `drm_draw.c` `drm_of.c` `drm_cache.c` `drm_print.c` | debugfs (info/CRC); fbdev 模拟; fourcc 格式查询/转换; 紧急 panic 屏; RAS netlink; 模式解析; tracepoint; OF (设备树); CPU cache 管理 |

---

<br/>

## 二、核心调用栈

### 2.1 设备初始化 — `drm_dev_alloc` → 用户态 open 可用

```
   Userspace:  open("/dev/dri/card0")
   
   ═══════════════════════════════════════════════════════════════════════
   Kernel:
   
   drm_dev_alloc()                    [drm_drv.c]
     ├─ drm_mode_config_init()        [drm_mode_config.c]   KMS 基座初始化
     └─ drm_client_setup()            [drm_client.c]        内核客户端 (fbdev 等)
   
   drm_dev_register()                 [drm_drv.c]
     ├─ drm_minor_register()          [drm_file.c]          创建 /dev/dri/cardX, renderD128
     ├─ drm_sysfs_connector_add()     [drm_sysfs.c]         sysfs 暴露连接器
     └─ drm_debugfs_register()        [drm_debugfs.c]       debugfs 节点

   drm_open()                          [drm_file.c]         用户 open 触发
     ├─ drm_file_alloc()                                    分配 drm_file (per-fd context)
     ├─ driver->open()                                      驱动回调 (如 gem_context_create)
     └─ drm_client_dev_restore()                            恢复之前内核客户端配置
```

### 2.2 Atomic Commit — 一次模式设置/翻页的完整路径

```
   Userspace:  ioctl(DRM_IOCTL_MODE_ATOMIC)
   
   ══════════════════════════════════════════════════════════════════════════════
   Kernel:
   
   drm_ioctl()                        [drm_ioctl.c]         ★ ioctl 总入口
     └─ drm_mode_atomic_ioctl()       [drm_atomic_uapi.c]   解析用户态参数
          ├─ drm_atomic_state_alloc()                       分配 atomic state
          └─ drm_atomic_commit()      [drm_atomic.c]        ★ 核心入口
               │
               ├─[校验阶段]─────────────────────────────────────────────────────
               │ drm_atomic_check_only()
               │   └─ drm_atomic_helper_check()             [drm_atomic_helper.c]
               │        ├─ drm_atomic_helper_check_planes()  遍历 plane 检查
               │        ├─ drm_atomic_helper_check_crtcs()   遍历 CRTC 检查
               │        └─ driver->atomic_check()            驱动自定义检查
               │
               ├─[准备阶段]─────────────────────────────────────────────────────
               │ drm_atomic_helper_setup_commit()
               │   ├─ drm_crtc_commit_wait()                等待该 CRTC 之前的 commit 完成
               │   └─ 为每个 CRTC 分配 drm_crtc_commit
               │
               │ drm_atomic_helper_prepare_planes()
               │   └─ driver->prepare_fb()                  驱动准备 FB (pin 内存等)
               │
               ├─[原子交换]── ★ Point of No Return ─────────────────────────────
               │ drm_atomic_helper_swap_state()
               │   └─ 新旧 state 指针原子交换
               │
               └─[提交硬件]─────────────────────────────────────────────────────
                 drm_atomic_helper_commit()
                   └─ drm_atomic_helper_commit_tail()       ★ 驱动可覆写
                        ├─ drm_atomic_helper_wait_for_fences() 等待 implicit fence
                        ├─ drm_atomic_helper_commit_modeset_disables() 关闭旧 CRTC
                        ├─ drm_atomic_helper_commit_planes()          配置 plane
                        │    └─ driver->atomic_update()               驱动写寄存器
                        ├─ drm_atomic_helper_commit_modeset_enables()  启动新 CRTC
                        ├─ drm_atomic_helper_commit_hw_done()         硬件配置完毕
                        └─ drm_atomic_helper_wait_for_vblanks()       等待显示生效
                             └─ drm_crtc_vblank_wait()
```

### 2.3 GEM Buffer 生命周期 — 创建 → mmap → 渲染 → 显示 → 跨设备共享

```
   ═══ 创建 Framebuffer ═══                        ═══ 跨进程共享 ═══
   Userspace:                                     Userspace:
     ioctl(DRM_IOCTL_MODE_ADDFB2)                   ioctl(DRM_IOCTL_GEM_OPEN)
     ─────────────────────────                     ───────────────────────
   Kernel:                                        Kernel:
     drm_mode_addfb2()  [drm_framebuffer.c]         drm_gem_open_ioctl()  [drm_gem.c]
       ├─ drm_framebuffer_init()                      └─ 同名 handle → 不同 GEM object
       └─ 关联 GEM handle → GEM object


   ═══ mmap 到用户态 ═══                          ═══ PRIME 跨设备共享 ═══
   Userspace:  mmap()                             ┌─ Userspace: prime fd 传递 ─┐
   ─────────────────────                          │                             │
   Kernel:                                        v                             v
     drm_gem_mmap()  [drm_gem.c]             Export 侧:                    Import 侧:
       ├─ drm_gem_mmap_obj()                 drm_gem_prime_export()        drm_gem_prime_import()
       └─ driver->gem_mmap()                   [drm_prime.c]                 [drm_prime.c]
            ├─ drm_gem_shmem_mmap()              ├─ driver->export()           ├─ dma_buf_attach()
            ├─ drm_gem_prime_mmap()              └─ dma_buf_export()           └─ driver->import_sg_table()
            └─ drm_gem_dma_mmap()
```

### 2.4 GPU 命令提交 — DRM Scheduler 流水线

```
   Userspace:  ioctl(DRM_IOCTL_*)  (驱动特定: amdgpu_cs, i915_gem_execbuf ...)
   
   ═════════════════════════════════════════════════════════════════════════════
   Kernel (驱动代码):
   
   driver->submit_job()
     ├─ drm_sched_job_init()           [sched_main.c]    初始化 job, 关联 entity/fence
     ├─ drm_sched_job_arm()            [sched_main.c]    设置 scheduled fence, 就绪
     └─ drm_sched_entity_push_job()    [sched_entity.c]  推入 entity 队列
          ├─ drm_sched_entity_select_rq()                选择 run queue
          └─ drm_sched_wakeup()        [sched_main.c]    唤醒调度线程
               │
               ▼
   ┌──── [scheduler thread] ───────────────────────────────────────────┐
   │                                                                    │
   │  drm_sched_main()                   [sched_main.c]  调度主循环     │
   │    ├─ drm_sched_entity_pop_job()    [sched_entity.c]               │
   │    ├─ job->sched->ops->run_job()                    驱动回调       │
   │    │    └─ 写 GPU ring buffer → GPU 硬件开始执行                   │
   │    │                                                               │
   │    └─ [GPU 完成中断]                                               │
   │         ├─ drm_sched_job_done()      [sched_main.c]                │
   │         ├─ drm_sched_fence_finished() [sched_fence.c]              │
   │         └─ dma_fence_signal()                       通知所有等待者 │
   │              ├─ 遍历 fence chain                                    │
   │              ├─ 唤醒 syncobj timeline waiter (explicit sync)        │
   │              └─ 唤醒 GEM dma_resv waiter    (implicit sync)         │
   └────────────────────────────────────────────────────────────────────┘
```

### 2.5 热插拔 — 显示器连接/断开

```
   [HPD 中断 或 驱动 poll 线程]
   
   ═════════════════════════════════════════════════════════════════════
   
   driver->handle_hpd_irq()
     └─ drm_kms_helper_hotplug_event()        [drm_probe_helper.c]
          └─ drm_client_dev_hotplug()
               ├─ connector->detect()                          驱动读 HPD / 读取 EDID
               │
               ├─ drm_edid_read()               [drm_edid.c]  读取 EDID 数据块
               ├─ drm_edid_connector_update()   [drm_edid.c]  更新连接器属性
               ├─ drm_edid_to_eld()             [drm_edid.c]  解析音频 ELD
               ├─ drm_mode_prune_invalid()      [drm_modes.c] 过滤无效显示模式
               │
               └─ drm_client_modeset_probe()    [drm_client_modeset.c]
                    └─ drm_client_modeset_commit_atomic()
                         └─ drm_atomic_commit()               走 Atomic Commit 流程
```

### 2.6 VBlank 同步 — 帧同步基础

```
   Userspace:  ioctl(DRM_IOCTL_WAIT_VBLANK)
   
   ═════════════════════════════════════════════════════════════════════
   
   drm_wait_vblank_ioctl()                    [drm_vblank.c]
     ├─ drm_crtc_vblank_count()               获取当前 vblank 序列号
     ├─ drm_crtc_vblank_get()                 持引用 → 使能 vblank 中断
     │
     └─ [等待目标 sequence]
          ├─ wait_event_interruptible_timeout()            睡眠等待
          │    │
          │    └─ [vblank 硬件中断触发]
          │         ├─ drm_handle_vblank()                 更新 vblank 计数 + 时间戳
          │         ├─ drm_crtc_send_vblank_event()        发送 vblank 事件给等待客户端
          │         └─ wake_up()                           唤醒等待线程
          │
          └─ drm_crtc_vblank_put()                         释放引用
```

### 2.7 完整数据流 — 用户态 → 内核 → 硬件

```
     Userspace
   ┌──────────────────────────────────────────────────────────────┐
   │                                                              │
   │   Mesa / libdrm             Weston / Xorg          GBM      │
   │   ────────────             ──────────────        ───────    │
   │   OpenGL / Vulkan            显示服务器           GPU Buffer │
   │        │                         │                  │       │
   └────────┼─────────────────────────┼──────────────────┼───────┘
            │ open      ioctl          │ ioctl            │ mmap
            v                         v                  v
   ╔══════════════════════════════════════════════════════════════╗
   ║                  /dev/dri/card0  (字符设备)                   ║
   ╠══════════════════════════════════════════════════════════════╣
   ║                                                              ║
   ║  drm_open()         drm_ioctl()           drm_gem_mmap()     ║
   ║    │                    │                      │              ║
   ║    v                    v                      v              ║
   ║  drm_file            dispatch              GEM object          ║
   ║  (per-fd)         ┌────┴────┐              ┌──┴──┐            ║
   ║                   │         │              │     │            ║
   ║              Atomic     GPU 提交       DMA-CMA  SHMEM        ║
   ║              Commit    (Scheduler)      连续物理  共享内存     ║
   ║                 │         │              │     │            ║
   ║                 v         v              │     │            ║
   ║            ┌─────────────────────┐       │     │            ║
   ║            │   Hardware Driver   │◄──────┘     │            ║
   ║            │  i915 / amdgpu /    │◄────────────┘            ║
   ║            │  nouveau / msm / .. │                           ║
   ║            └─────────┬───────────┘                           ║
   ║                      │                                       ║
   ╚══════════════════════╪═══════════════════════════════════════╝
                          │
                          v
                  ┌───────────────┐
                  │   GPU / 显示   │
                  │   硬件         │
                  └───────────────┘
```
