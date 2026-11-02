# glibc malloc：架构图与阅读资料

先看 [glibc 2.31 malloc 内存管理架构图](notes/linux-kernel/mm/glibc/glibc-malloc-2.31.excalidraw)：上半部分区分 tcache、arena 中的空闲块索引与承载 chunk 的 heap；下半部分概括 `malloc` 请求如何从复用已有 chunk 走到向内核申请内存。

建议按以下顺序阅读：

1. [Azeria Labs Part 1：Understanding the Glibc Heap Implementation](https://azeria-labs.com/heap-exploitation-part-1-understanding-the-glibc-heap-implementation/)：heap、chunk、top chunk，以及何时向内核取得更多内存。
2. [Azeria Labs Part 2：Free Bins](https://azeria-labs.com/heap-exploitation-part-2-glibc-heap-free-bins/)：释放后的 chunk 如何进入各类 bin，之后如何被复用；也介绍 tcache。
3. [sploitF-U-N：Understanding glibc malloc](https://sploitfun.wordpress.com/2015/02/10/understanding-glibc-malloc/)：经典的 arena、heap 与线程关系图。文章写于 tcache 引入之前，分配路径不要直接套用到 glibc 2.31。
4. [杰哥：glibc 内存分配器](https://jia.je/kb/software/glibc_allocator.html)：中文源码分析；其中 glibc 2.31 的流程图可用于对照这里的架构图。

图和文章中的具体阈值、bin 行为依赖 glibc 版本与配置；以 [glibc 2.31 的 `malloc.c`](https://github.com/bminor/glibc/blob/glibc-2.31/malloc/malloc.c) 为准。
