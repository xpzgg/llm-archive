- [ ] radix-tree, rb-tree, maple-tree关系
- [ ] anonymous and file-backed vma
- [ ] PGD 的地址是怎么得到的？



## 数据结构

### Radix Tree

**#1 LWN — Trees I: Radix trees（2006，入门经典）**
https://lwn.net/Articles/175432/
介绍 Linux radix tree 的 API 和基本概念，包括 key-value 存储机制、tag 特性，以及在 page cache 里的核心应用。入门必读，虽然年代久远但原理没变。 [LWN.net](https://lwn.net/Articles/175432/)

**#2 LWN — A multi-order radix tree（2016）**
https://lwn.net/Articles/688130/
介绍 radix tree 在 page cache 里的具体用途，以及 huge page 场景下 one-to-one 关系不够用的痛点，是理解 radix tree 局限性的关键文章。读完#1 之后看这篇，建立"为什么需要演进"的问题意识。 [LWN.net](https://lwn.net/Articles/688130/)

**#3 Matthew Wilcox LinuxCon 2016 演讲 slides**
[http://events17.linuxfoundation.org/sites/events/files/slides/LinuxConNA2016%20-%20Radix%20Tree.pdf](http://events17.linuxfoundation.org/sites/events/files/slides/LinuxConNA2016 - Radix Tree.pdf)
Wilcox 本人讲 radix tree 内部结构和 page cache 用法，图示丰富，比纯文字好理解。

**#4 linux-insides — Radix tree（代码向）**
https://0xax.gitbooks.io/linux-insides/content/DataStructures/linux-datastructures-2.html
从 `radix_tree_root` 和 `radix_tree_node` 结构体出发，逐字段分析实现细节。适合对照源码看。 [Linux Inside](https://0xax.gitbooks.io/linux-insides/content/DataStructures/linux-datastructures-2.html)

**#5 内核官方文档（genradix）**
https://docs.kernel.org/core-api/generic-radix-tree.html
API 参考，查接口用。

------

### XArray

**#1 LWN — The XArray data structure（2018，设计动机最清楚）**
https://lwn.net/Articles/745073/
Wilcox 在 linux.conf.au 上的演讲总结，解释了 radix tree API 的根本问题，以及 XArray 如何重新设计接口——把"树"的比喻换成"数组"、把锁内置到 API 里、去掉 preload 机制。理解 XArray 设计哲学必读。 [LWN.net](https://lwn.net/Articles/745073/)

**#2 内核官方文档 — XArray（Wilcox 本人写的）**
https://docs.kernel.org/core-api/xarray.html
Wilcox 写的权威文档，解释了 XArray 作为抽象数据类型的行为——类似超大指针数组，支持 RCU lockless 读，cache 友好，适合密集 index 场景。API 说明最全面，设计原则也写得清楚。 [Linux Kernel](https://docs.kernel.org/core-api/xarray.html)

**#3 LWN — Introducing the eXtensible Array（2017，RFC 阶段原始讨论）**
https://lwn.net/Articles/715948/
Wilcox 发出第一版 XArray 时的说明，解释了他观察 radix tree 使用方式之后得出的设计方向。看社区对最初设计的反馈，能理解哪些 trade off 是被反复讨论过的。 [LWN.net](https://lwn.net/Articles/715948/)

**#4 LWN — XArray and the mainline（2018，LSFMM 讨论）**
https://lwn.net/Articles/757342/
LSFMM 2018 上 Wilcox 和其他维护者讨论 XArray 合入进展，涉及 page cache 转换计划和性能数据。了解社区推动过程，以及 Andrew Morton 等人的关切点。 [LWN.net](https://lwn.net/Articles/757342/)

**#5 Wilcox LCA2018 演讲录像（Internet Archive）**
https://archive.org/details/lca2018-The_design_and_implementation_of_the_XArray
Wilcox 在 linux.conf.au 2018 上的完整演讲，讲 XArray 的 API 设计思路和实现细节。视频版，配合#1 的文字总结一起看。 [Internet Archive](https://archive.org/details/lca2018-The_design_and_implementation_of_the_XArray)

------

### Maple Tree

**#1 Oracle Blog — 入门篇（2021，作者本人写）**
https://blogs.oracle.com/linux/the-maple-tree-a-modern-data-structure-for-a-complex-problem
Liam Howlett 介绍 maple tree 的设计背景，从 VMA 管理问题出发解释为什么需要这个新数据结构。从这篇开始。 [Oracle](https://blogs.oracle.com/linux/the-maple-tree-a-modern-data-structure-for-a-complex-problem)

**#2 LWN — Introducing maple trees（2021，RFC 阶段分析）**
https://lwn.net/Articles/845507/
分析了 mmap_lock 竞争问题的背景，以及 maple tree 作为解决方案被提出的过程，包括它替换 rbtree 的具体目标。理解问题背景最好的一篇。 [LWN.net](https://lwn.net/Articles/845507/)

**#3 lore — v10 patch cover letter（设计最终定稿版）**
https://lore.kernel.org/lkml/20220621204632.3370049-1-Liam.Howlett@oracle.com/T/
patch series 的完整介绍，说明 maple tree 作为 RCU-safe、基于 B-tree 的范围查询数据结构的设计，non-leaf 节点分支因子 10，leaf 节点分支因子 16。一手资料，设计说明最权威。 [LWN.net](https://lwn.net/Articles/901714/)

**#4 Oracle Blog — 内部结构深入篇（2024）**
https://blogs.oracle.com/linux/maple-tree-storing-ranges
聚焦内部存储布局和 debug dump 解读，包括 pivot 和 slot 的具体结构。看完前三篇之后的进阶材料。 [Oracle](https://blogs.oracle.com/linux/maple-tree-storing-ranges)

**#5 LWN — The next steps for the maple tree（2024，后续方向）**
https://lwn.net/Articles/974860/
社区对 maple tree 下一步演进的讨论，包括 guard VMA 优化方向，了解这个方向的最新动态。

**#6 LWN — StackRot 漏洞（2023，从安全角度理解设计边界）**
https://lwn.net/Articles/937377/
StackRot 漏洞涉及 maple tree 节点替换与 mmap_lock 的交互问题，maple 节点通过 RCU 回调延迟释放。你有 RCU 背景，这篇读起来会特别有收获。 [LWN.net](https://lwn.net/Articles/937377/)

------

### 学习路径建议

三个数据结构有传承关系，按这个顺序来效率最高：

Radix tree #1#2 → XArray #1#2 

rb tree -> Maple tree #1#2#3 → 各自的代码深入篇

