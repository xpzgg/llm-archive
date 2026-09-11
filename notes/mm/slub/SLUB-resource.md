首选：SLUB 作者本人讲的 deck

   Christoph Lameter（SLUB 作者）— "Slab allocators in the Linux Kernel: SLAB, SLOB, SLUB"，LinuxCon Europe 2014
   • slides PDF：https://events.static.linuxfound.org/sites/events/files/slides/slaballocators.pdf
   • 为什么 slab 要建在页分配器之上、SLAB/SLOB/SLUB 三代设计差异、SLUB 的元数据藏进 struct page 的技巧、per-cpu
     slab、partial 链表、slab merging。讲的就是你正在学的 v6.6 经典架构（比 sheaves 早十年），一份 deck 建立整体认
     识，正好满足你的需求。

   配套（文字，讲得细）

   • LWN "The SLUB allocator"（2007）：https://lwn.net/Articles/229984/ — SLUB 原始 patchset 的设计动机，短而清晰。
     注意它讲的是 2007 版，还没有 cmpxchg 无锁快路径，但分层模型和 v6.6 一致。
   • Oracle 博客 "Linux SLUB Allocator Internals and Debugging" 四连篇（2022，基于
     v5.19）：https://blogs.oracle.com/linux/linux-slub-allocator-internals-and-debugging-1 — 目前找到的最贴近源码
     的图文走读，包括两条 freelist 的区分、frozen 语义、cmpxchg 快路径，和我们讲的内容几乎一一对应（v5.19 ≈ v6.6）
     。Part 1 看设计就够，2–4 是 slub_debug/KASAN，以后调试用。

   视频（maintainer 视角）

   • Vlastimil Babka（现任 slab maintainer）两个 2023 年的 talk（都验证可播放）："Reducing the Kernel's Slab
     Allocators"（https://www.youtube.com/watch?v=KAx1Wa-Q6k8）和 "The rise and fall of kernel slab
     allocators"（https://www.youtube.com/watch?v=OM-bEHQweHY）——讲三代分配器的历史和"为什么 SLUB 胜出"（SLOB 6.4
     删、SLAB 6.8 删）。

   一个提醒

   搜到的 2025 之后的材料（LWN 的 sheaves 系列、Babka 的 FOSDEM 2026 演讲）讲的都是 7.x sheaves 新架构——就是你
   notes 里 slub-sheaves.md 那套。先别碰这些，等 v6.6 经典架构学完再看，否则两套模型会在脑子里打架。

   建议顺序：Lameter deck 建立大图 → 继续我们的 ___slab_alloc 走读 → Oracle Part 1 当对照读物 → 主线学完后 Babka 视
   频 + sheaves 材料收尾。
