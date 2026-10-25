# NUMA 与 Cache 学习计划

## 一、Cache & NUMA 基础

**Cache 部分**

1. [CPU Caches and Why You Care](https://www.aristeia.com/videos.html) — Scott Meyers(code::dive 2014,1 小时 16 分的 talk，有 [slides PDF](https://neil3d.github.io/reading/assets/slides/codedive-CPUCachesHandouts.pdf))
   最推荐的入门材料。讲清 cache 层级、cacheline、命中率、为什么代码要照顾局部性，不陷入实现细节。只看 slides 翻一遍也就 20 分钟。

2. [Gallery of Processor Cache Effects](http://igoro.com/archive/gallery-of-processor-cache-effects/) — Igor Ostrovsky
   单篇博客，用一组可复现的小实验展示 cacheline 大小、关联度、伪共享对性能的影响。和 Meyers 的 talk 互补：一个给概念，一个给直觉。

3. [Modern Microprocessors: A 90-Minute Guide](https://www.lighterra.com/papers/modernmicroprocessors/) — Jason Robert Carey Patterson
   如果想再往下扎一点（流水线、乱序执行、store buffer、内存屏障的来源），这篇是标准的"一小时读本"，比 Drepper 短得多。

**NUMA 部分**

4. [NUMA (Non-Uniform Memory Access): An Overview](https://queue.acm.org/detail.cfm?id=2513149) — Christoph Lameter,ACM Queue 2013
   NUMA 基础的标准读物，十几页。讲清 UMA→NUMA 的演化、node 划分、local/remote 访问代价、Linux 的内存策略（localalloc/interleave/bind)。作者是 Linux slab/SLUB 的作者。

5. [NUMA Deep Dive Series](https://frankdenneman.nl/2016/07/06/introduction-2016-numa-deep-dive-series/) — Frank Denneman（只读 Part 1 和 Part 2 即可）
   偏硬件视角：node 内部结构、QPI/UPI 互连、不同跳数的访问延迟。后几篇是 VMware 虚拟化的，可以跳过。

## 二、Cache & NUMA 常见坑

**Cache 相关**

6. [False Sharing](https://mechanical-sympathy.blogspot.com/2011/07/false-sharing.html) — Martin Thompson
   经典中的经典：多个 CPU 写不同变量但共享同一条 cacheline，导致无意义的所有权乒乓。几乎所有 cache 性能问题的原型。

7. [MCS locks and qspinlocks](https://lwn.net/Articles/590243/) — LWN
   锁竞争场景下的 cacheline 乒乓问题，以及 Linux 怎么一步步从 ticket lock 演进到 qspinlock。

**NUMA 相关**

8. [The MySQL "swap insanity" problem and the effects of the NUMA architecture](https://blog.jcole.us/2010/09/28/mysql-swap-insanity-and-the-numa-architecture/) — Jeremy Cole
   NUMA 最著名的生产事故：内存还有富余却疯狂 swap——本质是 zone_reclaim 和"优先本地分配"策略。注意这篇结论有误（真实原因是内核 bug，2014 年才修复），但它是理解 NUMA 内存分配问题的最佳案例入口。

9. [AutoNUMA: the other approach to NUMA scheduling](https://lwn.net/Articles/488709/) — LWN
   内核如何自动处理 NUMA 调度问题：page fault 采样、跨节点迁移页和线程。读完能理解 `kernel/sched/fair.c` 里那套 numa balancing 的由来。

10. [NUMA-aware qspinlocks](https://lwn.net/Articles/852138/) — LWN
    锁乒乓问题在 NUMA 下的解法和争议（CNA 进内核那轮），直接对应内核源码 `kernel/locking/qspinlock_cna.h`。

## 建议路径

基础 1→2→4(NUMA 只需 4,Denneman 选读）→ 问题 6→8→7→9→10。每一步单篇都不超过半小时，10 读完回头看内核里的 CNA 实现会有"全串起来了"的感觉。
