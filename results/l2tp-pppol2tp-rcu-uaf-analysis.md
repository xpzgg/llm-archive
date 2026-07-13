# L2TP/PPPoL2TP RCU callback 节点提前释放问题分析

- 日期：2026-07-13
- 分析基线：openEuler OLK-6.6
- 问题类型：KASAN slab-use-after-free
- 涉及模块：net/l2tp/l2tp_core.c、net/l2tp/l2tp_ppp.c

## 摘要

**一句话结论：pppol2tp_release() 提交内嵌于 session 的 ps->rcu 后，未额外持有 session 引用；并发的 l2tp_tunnel_del_work() 先释放了整个 session，导致 RCU 队列中的 rcu_head 成为悬空节点并最终触发 UAF。**

Syzkaller 报告了一个发生在 rcu_cblist_dequeue() 中的 slab-use-after-free。被访问的地址属于一个已经释放的 l2tp_session 对象，且对象内偏移 0x180 对应 PPPoL2TP 私有数据中的 pppol2tp_session::rcu。这个 rcu_head 由 pppol2tp_release() 提交给 call_rcu()，宿主 session 则由 l2tp_tunnel_del_work() 路径释放。

问题的直接原因是：PPPoX socket release 提交异步 RCU callback 后，没有明确的 session 引用保证内嵌 rcu_head 的宿主对象存活；并发的 tunnel teardown 可以在 callback 出队前将 session 释放。随后 RCU nocb 线程读取 rcu_head->next，触发 KASAN。rcu_cblist_dequeue() 是受害位置，不是缺陷来源。

该竞争涉及两个独立的关闭入口：PPPoX session socket 的 release 路径，以及 tunnel transport socket 或网络命名空间清理触发的 tunnel teardown 路径。旧实现通过 socket destructor、session 引用计数和两套 RCU 机制间接维持生命周期，但没有为 pending 的 ps->rcu callback 建立清晰、独立的宿主对象所有权。

推荐按依赖关系回移上游修复：先使 l2tp_session 支持 RCU 延迟释放，再回移 PPP socket/session 生命周期重构，删除旧的 pppol2tp_session::rcu callback，由 session_close 统一解除 socket/session 关联。仅在现有代码中把 kfree() 替换为 kfree_rcu()，不能证明两个独立 callback 的执行顺序，因此不是完整修复。

报告仍有一个需要结合内部代码或 reproducer 确认的问题：标准 OLK-6.6 的正常 connect 成功路径理论上保留 core 引用和 socket 关联引用，需要继续定位为什么本次 tunnel worker 的 session put 成为了最后一次 put。这会影响是否还存在额外的引用计数缺陷，但不改变上述 UAF 机制。

## Syzkaller 报告信息

### 故障现场

KASAN 报错位置为：

    BUG: KASAN: slab-use-after-free in rcu_cblist_dequeue
    Read of size 8 at addr ffff888106c9d180 by task rcuos/1/28

对象信息为：

    object base:  ffff888106c9d000
    object range: [ffff888106c9d000, ffff888106c9d200)
    access addr:  ffff888106c9d180
    offset:       0x180
    slab cache:   kmalloc-512

报告中的分配、释放、callback 提交和最终访问可以对应到同一个 PPPoL2TP session：

| 事件 | 关键调用路径 | 含义 |
|---|---|---|
| 对象分配 | pppol2tp_connect() → l2tp_session_create() → kzalloc() | 创建 PPPoL2TP session |
| callback 提交 | pppol2tp_release() → call_rcu() | 提交 session 私有区内的 rcu_head |
| 对象释放 | l2tp_tunnel_del_work() → l2tp_tunnel_closeall() → l2tp_session_delete() → l2tp_session_dec_refcount() → kfree() | tunnel worker 释放宿主 session |
| UAF 访问 | rcu_nocb_cb_kthread() → rcu_do_batch() → rcu_cblist_dequeue() | RCU 线程读取已释放 callback 节点 |

### 关键调用栈

对象由 PPPoL2TP connect 路径分配：

    pppol2tp_connect()
    └── l2tp_session_create()
        └── kzalloc(sizeof(struct l2tp_session) + priv_size)

对象由 tunnel 删除工作项释放：

    l2tp_tunnel_del_work()
    └── l2tp_tunnel_closeall()
        └── l2tp_session_delete()
            └── l2tp_session_dec_refcount()
                └── l2tp_session_free()
                    └── kfree(session)

KASAN 记录到的最近一次相关 RCU work 创建来自 PPPoX socket release：

    pppol2tp_release()
    └── call_rcu(&ps->rcu, pppol2tp_put_sk)

最终访问发生在 RCU callback 出队阶段：

    rcu_nocb_cb_kthread()
    └── nocb_cb_wait()
        └── rcu_do_batch()
            └── rcu_cblist_dequeue()

报告还列出了 fib_rules cleanup 触发的 kvfree_call_rcu()。它只是 KASAN 保存的另一条近期 RCU work 创建记录，与本次 l2tp_session 的分配和释放对象不匹配，不是主要嫌疑路径。

### 故障地址与对象布局

PPPoL2TP 创建 session 时，将通用的 struct l2tp_session 和协议私有的 struct pppol2tp_session 放在同一次分配中：

    kzalloc(sizeof(struct l2tp_session) +
            sizeof(struct pppol2tp_session), GFP_KERNEL)

逻辑布局如下：

    struct l2tp_session
    └── priv[]
        └── struct pppol2tp_session
            ├── owner
            ├── sk_lock
            ├── sk
            ├── __sk
            └── rcu

故障地址相对对象起始地址的偏移为：

    ffff888106c9d180 - ffff888106c9d000 = 0x180

结合 OLK-6.6 的结构布局，该偏移对应 pppol2tp_session::rcu。也就是说，RCU 队列中的节点位于已经被 kfree() 的 l2tp_session 分配块内部。

### 为什么在 rcu_cblist_dequeue() 中报错

rcu_cblist_dequeue() 从 callback 链表取出首节点时需要读取 next：

    rhp = rclp->head;
    rclp->head = rhp->next;

本次 rhp 指向 &ps->rcu，但 ps 的宿主 session 已经释放。因此第一次读取 rhp->next 就触发 KASAN。此处的含义不是 RCU core 错误地释放了 L2TP 对象，而是 L2TP 向 RCU 队列提交了一个生命周期不足的节点。

## L2TP 背景

### L2TP 与 PPPoL2TP 对象模型

L2TP 用于在 IP 网络上传输二层会话。一个 tunnel 提供底层传输通道，tunnel 内可以存在多个 session；每个 session 承载一个 PPP、Ethernet 等 pseudowire。L2TPv2 常用于承载 PPP，L2TPv3 还支持其他二层类型。本问题位于 net/l2tp/l2tp_ppp.c，属于 PPPoL2TP 路径。

内核中的主要对象关系为：

    network namespace
    └── struct l2tp_tunnel
        ├── UDP/IP transport socket
        └── 多个 struct l2tp_session
            └── PPP pseudowire 私有数据
                └── struct pppol2tp_session

| 对象 | 作用 | 与本问题的关系 |
|---|---|---|
| l2tp_tunnel | 保存 tunnel ID、transport socket 和 session 集合 | 删除 tunnel 时批量关闭 session |
| l2tp_session | tunnel 内的一条逻辑数据通道 | 本次被提前释放的宿主对象 |
| pppol2tp_session | PPP pseudowire 的私有数据 | 包含 ps->sk、ps->__sk 和 ps->rcu |
| PPPoX socket | 用户态操作 PPPoL2TP session 的 socket | close 时进入 pppol2tp_release() |

连接成功后，PPPoX socket 和 session 形成双向关系：

    PPPoX socket
      sk_user_data ──────────────► l2tp_session

    l2tp_session->priv
      pppol2tp_session->sk ──────► PPPoX socket

sk->sk_user_data 使 socket 路径可以找到 session；ps->sk 使 session 的收发路径可以找到 PPPoX socket。ps->__sk 用于 teardown 期间暂存已经从 ps->sk 解除的 socket，ps->rcu 则用于延迟执行 sock_put(ps->__sk)。

### 连接创建流程

用户态通常创建 AF_PPPOX/PX_PROTO_OL2TP socket，然后用 sockaddr_pppol2tp 调用 connect。内核中的核心流程为：

    socket(AF_PPPOX, ..., PX_PROTO_OL2TP)
    └── connect(fd, sockaddr_pppol2tp, ...)
        └── pppol2tp_connect()
            ├── 查找或创建 tunnel
            ├── 查找或创建 session
            ├── l2tp_session_register()
            ├── ppp_register_net_channel()
            ├── sk->sk_user_data = session
            └── rcu_assign_pointer(ps->sk, sk)

新建 session 时，l2tp_session_create() 分配通用 session 及其 PPP 私有区；注册成功后，session 加入 tunnel 的 session 索引。连接完成后，socket 与 session 的双向关联也参与双方的引用和关闭流程。

### 数据路径

接收方向中，数据先从 tunnel 的 transport socket 进入 L2TP core，再根据 session ID 查找具体 session，最后交给 PPP 层：

    UDP packet
    → l2tp_udp_encap_recv()
    → 根据 session ID 查找 l2tp_session
    → l2tp_recv_common()
    → pppol2tp_recv()
    → PPP generic layer

发送方向相反：

    PPP generic layer
    → pppol2tp_xmit()
    → l2tp_xmit_skb()
    → 添加 L2TP header
    → tunnel transport socket
    → UDP/IP

这些路径可能与 socket close、tunnel delete 和网络命名空间退出并发，因此 session 索引、session 对象和 ps->sk 都需要并发保护。

### 正常关闭流程

PPPoL2TP 存在两个可以独立发生的关闭入口。

第一个入口是关闭 PPPoX session socket：

    pppol2tp_release()
    ├── pppox_unbind_sock()
    ├── 将 socket 标记为 PPPOX_DEAD
    ├── 从 sk_user_data 取得 session
    ├── l2tp_session_delete(session)
    ├── ps->__sk = ps->sk
    ├── ps->sk = NULL
    └── call_rcu(&ps->rcu, pppol2tp_put_sk)

ps->sk 被清空后，旧的 RCU reader 仍可能持有 socket 指针，所以代码不能立即 sock_put()，而是在 grace period 后由 pppol2tp_put_sk() 释放该 socket 引用。

第二个入口是 tunnel teardown。transport socket 关闭、显式删除 tunnel 或网络命名空间清理，都可能调度 tunnel 删除工作项：

    l2tp_tunnel_delete()
    → queue_work(tunnel->del_work)
    → l2tp_tunnel_del_work()
    → l2tp_tunnel_closeall()
    → l2tp_session_delete(session)

l2tp_session_delete() 使用 session->dead 保证删除动作只启动一次。首次删除者将 session 从索引摘除，等待已有的 RCU 查找者退出，清理队列并减少 session 引用；后续删除者发现 dead 已设置后直接返回。

这里需要注意：session->dead 是一次性删除标志，不是完成量或生命周期引用。它不保证另一个删除者已经完成，也不保证 l2tp_session_delete() 返回后 session 仍然存活。

### 两套 RCU 保护

旧实现中有两套目的不同的 RCU 保护：

| 被保护的关系 | 典型 reader | teardown 操作 | 作用 |
|---|---|---|---|
| session 索引中的 l2tp_session | 数据接收和管理查询 | hlist_del_init_rcu() + synchronize_rcu() | 等待旧的 session 查找者退出 |
| pppol2tp_session::sk 指向的 socket | PPP 收发和状态查询 | 清空 ps->sk + call_rcu(&ps->rcu, ...) | 延迟释放旧 socket 引用 |

前一套 RCU 保护 session 查找，后一套 RCU 保护 socket 指针。synchronize_rcu() 只等待相关的旧 reader，不会替 ps->rcu callback 持有宿主 session，也不等价于等待该 callback 执行完成。

## 根因分析

### 直接根因

pppol2tp_release() 将 &ps->rcu 提交到 RCU callback 队列后，ps 所在的 l2tp_session 仍可能由 tunnel teardown 释放：

    call_rcu(&ps->rcu, pppol2tp_put_sk)

call_rcu() 只登记 callback，不会自动增加 rcu_head 宿主对象的引用。上述调用并不等价于 l2tp_session_get(session)。如果 session 在 callback 执行前变为零引用，kfree(session) 会同时释放 ps 和 ps->rcu，RCU 队列中随即留下悬空节点。

因此，问题不是简单的“使用 call_rcu() 的同时又使用 kfree()”。只要引用计数或其他所有权规则能够保证宿主对象活到 callback 结束，这种组合本身可以成立。本问题真正缺少的是这个生命周期保证。

### 两条并发 teardown 路径

根据报告，最符合现场的竞争关系是：tunnel worker 已经开始删除 session，并在 synchronize_rcu() 中等待；与此同时，PPPoX socket release 取得同一个 session，发现 session->dead 已设置，于是 l2tp_session_delete() 直接返回，但 release 随后仍然访问 session 私有区并提交 ps->rcu callback。

两条路径分别为：

    Tunnel teardown                         PPPoX socket release
    ----------------                        --------------------
    l2tp_tunnel_del_work()                  pppol2tp_release()
    l2tp_tunnel_closeall()                  从 sk_user_data 取得 session
    l2tp_session_delete()                   l2tp_session_delete()
    设置 session->dead                      发现 dead，直接返回
    unhash + synchronize_rcu()              清空 ps->sk
    l2tp_session_dec_refcount()             call_rcu(&ps->rcu, ...)
    kfree(session)

### 可能的竞争时序

完整时序可以还原为：

| 顺序 | Tunnel worker | PPPoX release | RCU callback 线程 |
|---|---|---|---|
| 1 | 设置 session->dead | | |
| 2 | 从索引摘除 session，进入 synchronize_rcu() | | |
| 3 | | 从 sk_user_data 取得 session | |
| 4 | | 调用 l2tp_session_delete()，因 dead 已设置而返回 | |
| 5 | | 清空 ps->sk，提交 call_rcu(&ps->rcu, ...) | |
| 6 | synchronize_rcu() 返回 | | |
| 7 | 最后一次 session put，引用变为 0 | | |
| 8 | kfree(session) | | |
| 9 | | | 出队 ps->rcu，读取已释放的 next |

报告由 rcuos/1 报出，说明 callback 被 offload 到 RCU nocb kthread。offload 增加了 callback 提交到执行之间的延迟，使竞争更容易复现，但不是根因；只要宿主对象可以先于 callback 释放，普通 softirq callback 环境中也存在同样风险。

### session->dead 和引用计数为什么没有阻止 UAF

session->dead 只阻止重复执行删除主体。第二个调用者看到 dead 后不会等待第一个删除者完成，也不会因此得到一个新的 session 引用。于是 pppol2tp_release() 可以在另一个 CPU 即将完成最后一次 put 时，继续使用 session->priv 中的 ps。

旧代码还依赖 socket/session 之间的间接引用关系：connect 完成后应有 socket 关联引用，socket destructor 最终释放它；pppol2tp_release() 中的临时保护主要是 sock_hold(sk)，而不是专门为 pending ps->rcu callback 获取 session 引用。这种分散的所有权模型在两个 teardown 入口交错时非常脆弱。

标准 OLK-6.6 的正常 connect 成功路径理论上保留 session 初始引用和 socket 关联引用，因此仍应通过内部代码或 reproducer 确认以下问题：

    为什么 l2tp_tunnel_del_work() 中的 session put
    在本次执行中成为了最后一次 put？

建议重点检查 socket destructor 的执行时机、connect/error path 是否多释放引用、内部补丁是否改变 sk_user_data 或 refcount 规则，以及是否存在额外的 l2tp_session_dec_refcount()。这属于“最后一个引用为何消失”的进一步定位；KASAN 地址、提交栈和释放栈已经足以证明 pending rcu_head 的宿主对象被提前释放。

### 排除项

- rcu_cblist_dequeue() 只是第一个读取悬空节点的位置，RCU core 不是根因。
- CONFIG_RCU_NOCB_CPU 只扩大触发窗口，不会制造该生命周期错误。
- synchronize_rcu() 等待旧 reader，不负责等待 ps->rcu callback 完成。
- fib_rules 的 kvfree_call_rcu() 与故障对象不匹配，是无关的近期 RCU work 记录。

## 修复建议

### 上游修复方案

上游后续通过两项相关改动重构了这部分生命周期：

1. l2tp: free sessions using rcu

   提交：d17e89999574aca143dd4ede43e4382d32d98724

   该改动使 l2tp_session 的最终释放经过 RCU，保证仍可能被旧 RCU reader 观察到的 session 内存不会立即回收。

2. l2tp: refactor ppp socket/session relationship

   提交：c5cbaef992d6420d8bcebea1b1fcc23302a67c57

   该改动重新定义 PPP socket 与 session 的所有权关系，主要包括：

   - 删除 pppol2tp_session::rcu 和 pppol2tp_put_sk()；
   - 删除旧的 call_rcu(&ps->rcu, ...) 路径；
   - session 显式持有关联 socket 的引用；
   - socket 路径取得 session 时显式获取 session 引用；
   - 由 session_close 统一解除双向关联；
   - 通过 RCU helper 访问 sk_user_data；
   - PPPoX socket 设置 SOCK_RCU_FREE；
   - l2tp_session 最终通过 kfree_rcu() 释放。

重构后的原则是：从 socket 取得 session 的路径显式持有 session，session_close 负责解除关联，最终释放由 RCU 延迟完成，不再把用于 sock_put() 的 callback 节点嵌在一个可能先行释放的 session 中。

### OLK-6.6 回移建议

建议按生命周期语义整体回移，不要只摘取表面修改：

1. 回移 session RCU free 的基础改动，使 l2tp_session 具备正确的延迟释放能力；
2. 回移 PPP socket/session relationship 重构，删除旧 ps->rcu callback；
3. 统一由 session_close 拆除 ps->sk、sk_user_data 和相关引用；
4. 审计所有从 socket 获取 session 的路径，确认在使用期间显式持有 session 引用；
5. 检查两项提交在 OLK-6.6 上对 session 索引、workqueue、socket helper 和相关前置补丁的依赖；
6. 对内部差异代码额外审计所有 session get/put，定位报告中最后一个 socket 关联引用消失的原因。

当前 OLK-6.6 的 l2tp_ppp.c 与第二项上游补丁主体接近，但不能只孤立应用 c5cbaef992d6。该补丁采用 RCU 方式读取 sk_user_data，其安全性依赖 session 已经通过 kfree_rcu() 延迟释放。

### 不建议的局部修补

不建议只把现有的 kfree(session) 改成 kfree_rcu(session, rcu)。旧代码仍然保留 ps->rcu callback，而 session free callback 和 ps->rcu callback 可能由不同 CPU、不同时间提交。两个 callback 都经过 grace period，不代表 session free callback 一定晚于 ps callback。

同样，不建议只增加一次 synchronize_rcu()、延长 socket 引用，或修改 RCU nocb 配置。这些改动可能缩小特定窗口，但没有消除生命周期规则中的歧义。

### 验证方案

建议启用以下调试配置：

    CONFIG_KASAN=y
    CONFIG_DEBUG_OBJECTS_RCU_HEAD=y
    CONFIG_PROVE_RCU=y
    CONFIG_RCU_NOCB_CPU=y

在 pppol2tp_connect() 完成关联、pppol2tp_sock_to_session() 返回、l2tp_session_delete() 入口及最后 put、pppol2tp_release() 提交 callback、socket destructor 释放 session 引用、l2tp_session_free() 最终释放等位置记录 session 地址、ps->rcu 地址、socket 地址、session->dead、session refcount、CPU 和调用位置。

验证场景至少覆盖：

| 场景 | 验证目标 |
|---|---|
| PPPoX fd 与 tunnel transport fd 并发 close | 原始 teardown 竞争 |
| 显式 tunnel delete 与 PPPoX close 并发 | 两个删除入口交错 |
| netns cleanup 与 PPPoX close 并发 | 网络命名空间退出路径 |
| 新建 session 后 connect/close | session 创建路径 |
| connect 已有 netlink session 后 close | 已有 session 的引用路径 |
| RCU nocb 开启和关闭 | 不同 callback 执行环境 |
| 长时间重复运行 reproducer | UAF、refcount warning、泄漏及残留 work |

修复完成应同时满足：原 reproducer 不再触发 UAF；旧的 call_rcu(&ps->rcu, ...) 已消除；socket 路径取得 session 后存在明确引用；session 最终使用正确的 RCU 释放方式；并且 socket、session、tunnel、work 和 callback 均无泄漏。

## 附录：源码与参考资料

### 关键源码位置

| 功能 | 文件和函数 |
|---|---|
| session 创建与最终释放 | net/l2tp/l2tp_core.c：l2tp_session_create()、l2tp_session_free() |
| tunnel 批量关闭 session | net/l2tp/l2tp_core.c：l2tp_tunnel_closeall() |
| session 删除 | net/l2tp/l2tp_core.c：l2tp_session_delete() |
| PPPoX socket 建立关联 | net/l2tp/l2tp_ppp.c：pppol2tp_connect() |
| PPPoX socket release | net/l2tp/l2tp_ppp.c：pppol2tp_release() |
| 异步 socket put | net/l2tp/l2tp_ppp.c：pppol2tp_put_sk() |
| RCU callback 出队 | kernel/rcu/rcu_segcblist.c：rcu_cblist_dequeue() |

### 参考资料

- 公开 syzbot 同类报告：

  https://syzkaller.appspot.com/bug?extid=7b261708d878bd949ebc

- 上游提交 l2tp: free sessions using rcu：

  https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=d17e89999574aca143dd4ede43e4382d32d98724

- 上游提交 l2tp: refactor ppp socket/session relationship：

  https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=c5cbaef992d6420d8bcebea1b1fcc23302a67c57
