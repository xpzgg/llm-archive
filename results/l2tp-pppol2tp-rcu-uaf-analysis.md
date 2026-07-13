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

针对当前 OLK-6.6 稳定分支，推荐先让 pending 的 ps->rcu callback 显式持有 session 引用，以小范围修改直接补上缺失的生命周期保证。若后续希望与主线统一，再按语义回移 session RCU free 和 PPP socket/session 生命周期重构；不能只把 kfree() 替换为 kfree_rcu()，因为两个独立 callback 之间没有确定的执行顺序。

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

两条路径的静态调用关系为：

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

将实际交错过程按上面的两条路径逐行展开：

| 阶段 | Tunnel teardown | PPPoX socket release | RCU callback 线程 |
|---|---|---|---|
| 1 | l2tp_tunnel_del_work() 进入 l2tp_tunnel_closeall() | | |
| 2 | 对目标 session 调用 l2tp_session_delete()，设置 session->dead | | |
| 3 | 将 session 从索引摘除并进入 synchronize_rcu() | | |
| 4 | 仍在 synchronize_rcu() 中等待 | pppol2tp_release() 从 sk_user_data 取得同一个 session | |
| 5 | 仍在 synchronize_rcu() 中等待 | 调用 l2tp_session_delete()，发现 dead 已设置，直接返回 | |
| 6 | 仍在 synchronize_rcu() 中等待 | 清空 ps->sk，提交 call_rcu(&ps->rcu, pppol2tp_put_sk) | |
| 7 | synchronize_rcu() 返回，执行 l2tp_session_dec_refcount() | pppol2tp_release() 返回 | |
| 8 | session 引用变为 0，kfree(session)，ps->rcu 随宿主对象一起释放 | | |
| 9 | | | 从 callback 链表出队 ps->rcu，读取已释放的 rcu_head->next，触发 UAF |

这个顺序同时解释了两个看似矛盾的现象：

- PPPoX release 能从 sk_user_data 得到 session，是因为它不通过已经摘除的 session 索引查找；
- tunnel worker 的 synchronize_rcu() 只等待旧 reader，不会等待第 6 阶段刚提交的 callback，也不会为 ps->rcu 持有 session 引用。

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

### 方案选择

针对当前 OLK-6.6 稳定分支，建议优先做一个直接修复本次 UAF 的小补丁：让 pending 的 ps->rcu callback 显式持有 session 引用。该方案修改范围小，不需要改变整个 PPP socket/session 所有权模型，适合作为产品分支修复。

如果后续希望与主线统一并消除这一类脆弱的间接生命周期关系，再单独进行主线方案的语义回移。主线改动建立在一系列 L2TP 重构之上，且 c5cbaef992d6 后来出现过需要修正的 socket 引用问题，因此不建议为了本次 UAF 直接机械 cherry-pick。

### 最小自修方案

核心规则是：

    pppol2tp_release() 在提交 ps->rcu 前增加一个 session 引用；
    pppol2tp_put_sk() 完成对 ps 的最后一次访问后再释放该引用。

可以在 pppol2tp_session 中保存 callback 对应的 session：

    struct pppol2tp_session {
        ...
        struct l2tp_session *release_session;
        struct rcu_head rcu;
    };

pppol2tp_release() 应在 l2tp_session_delete() 之前取得 callback 引用，再设置 callback 上下文并提交：

    session = pppol2tp_sock_to_session(sk);
    if (session) {
        l2tp_session_inc_refcount(session);

        l2tp_session_delete(session);

        ps = l2tp_session_priv(session);
        mutex_lock(&ps->sk_lock);
        ps->__sk = rcu_dereference_protected(ps->sk,
                                             lockdep_is_held(&ps->sk_lock));
        RCU_INIT_POINTER(ps->sk, NULL);
        ps->release_session = session;
        mutex_unlock(&ps->sk_lock);

        call_rcu(&ps->rcu, pppol2tp_put_sk);
    }

callback 中先完成 socket 处理，最后释放 session：

    static void pppol2tp_put_sk(struct rcu_head *head)
    {
        struct pppol2tp_session *ps;
        struct l2tp_session *session;

        ps = container_of(head, typeof(*ps), rcu);
        session = ps->release_session;

        sock_put(ps->__sk);
        l2tp_session_dec_refcount(session);
    }

callback 引用必须在 l2tp_session_delete() 前取得，否则 delete 路径仍可能先释放 session。l2tp_session_dec_refcount() 必须放在 callback 对 ps 的最后一次访问之后；如果 callback 提交前增加新的失败分支，也必须回收该引用。实现时还应优先使用 refcount_inc_not_zero() 或等价 helper，避免从零引用复活对象。

该方案建立的顺序是：

    call_rcu 提交
        → callback session ref 保证 session 和 ps 存活
        → tunnel worker 可以释放 core 引用
        → RCU callback 安全访问 ps 和 ps->__sk
        → callback 最后释放 session 引用
        → 此后才允许 kfree(session)

它直接修复报告中的生命周期缺口，但仍应继续定位原有 socket 关联引用为什么没有阻止 tunnel worker 成为最后一次 put。

### 主线语义回移方案

如果选择与主线生命周期模型对齐，需要处理以下改动，而不是只回移两个表面补丁：

| 改动 | 在当前 OLK-6.6 上的处理 |
|---|---|
| d17e89999574：l2tp: free sessions using rcu | 增加 l2tp_session::rcu，并将最终释放改为 kfree_rcu() |
| c5cbaef992d6：l2tp: refactor ppp socket/session relationship | 回移完整的 socket/session 所有权重构，删除 ps->rcu callback |
| 9b8c88f875c0：l2tp: do not use sock_hold() in pppol2tp_session_get_sock() | 当前分支已通过 582d44d36f95/d5ecbb961de5 包含等价修复，回移 c5 时必须保留当前实现 |
| 当前 e15457708214 ioctl 修复 | c5 改变 helper 返回引用的类型后，将对应 sock_put(sock->sk) 改为 l2tp_session_dec_refcount(session) |

d17e89999574 使 session 通过 kfree_rcu() 释放，保护已经从 sk_user_data 读到 session、但尚未成功增加引用的 RCU reader。当前 OLK 使用 hlist，并且部分 lookup 仍调用无条件的 l2tp_session_inc_refcount()，所以不建议照搬 d17 同时删除现有 synchronize_rcu()；保留它更符合当前查找实现。

c5cbaef992d6 重新定义双方的所有权：

- 删除 pppol2tp_session::rcu、pppol2tp_put_sk() 和旧 call_rcu()；
- session 显式持有关联 PPPoX socket 的引用；
- socket 路径通过 refcount_inc_not_zero() 显式取得 session 引用；
- session_close 统一清除 ps->sk 和 sk_user_data，并释放双方引用；
- PPPoX socket 设置 SOCK_RCU_FREE；
- session 最终通过 kfree_rcu() 释放。

这个模型从根本上删除了本次成为悬空节点的 ps->rcu，不再依赖 socket destructor 间接维持 callback 宿主对象。

主线后续的 9b8c88f875c0 修复了 c5 引入的一个问题：SOCK_RCU_FREE 只能保证零引用 socket 的内存暂时存在，RCU reader 不能再对这种 socket 调用 sock_hold()。当前 OLK 已有该提交的等价稳定版回移，应用 c5 时不能把 pppol2tp_session_get_sock() 恢复成旧实现。

当前分支还包含 e15457708214。该补丁中的 pppol2tp_ioctl() 使用 pppol2tp_sock_to_session() 后通过 sock_put(sock->sk) 释放保护；应用 c5 后，该 helper 改为返回 session 引用，所以错误路径和统一退出路径都必须改为 l2tp_session_dec_refcount(session)。这是一个不会产生文本冲突、但会产生语义错误的本地适配点。

主线 c5 的父提交链中还包含将 session 删除移入 workqueue 的 fc7ec7f554d7。当前 OLK 的 session 删除仍是同步实现，但现有 in-tree 调用者位于可睡眠上下文，tunnel closeall 也会在调用 delete 前释放 hlist 锁，因此不必为了本次问题回移整套 IDR/list/workqueue 重构；需要审计内部新增调用者，确认 pppol2tp_session_close() 不会在原子上下文或持有冲突锁时执行。

### 不建议的局部修补

不建议只把现有的 kfree(session) 改成 kfree_rcu(session, rcu)。旧代码仍然保留 ps->rcu callback，而 session free callback 和 ps->rcu callback 可能由不同 CPU、不同时间提交。两个 callback 都经过 grace period，不代表 session free callback 一定晚于 ps callback。

同样，不建议只增加 synchronize_rcu()、调用全局 rcu_barrier()、人为延长等待时间或修改 RCU nocb 配置。这些改动没有让 callback 显式拥有宿主 session，或者成本与影响范围明显超过问题本身。

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

采用最小自修时，应确认从 call_rcu() 提交到 callback 最后一次访问 ps 期间始终存在 callback session 引用，并且 session 的最终释放只能发生在 callback put 之后。

采用主线语义回移时，应确认旧的 call_rcu(&ps->rcu, ...) 已消除；从 socket 取得 session 后持有明确的 session 引用；session 和 socket 分别使用正确的 RCU 释放方式；当前 ioctl 路径释放的是 session 引用而不是错误的 socket 引用。

两种方案都必须满足：原 reproducer 长时间运行不再触发 UAF；并发 close、tunnel delete 和 netns cleanup 不产生 refcount warning；session、socket、tunnel、work 和 callback 均无泄漏。

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

- 上游后续修复 l2tp: do not use sock_hold() in pppol2tp_session_get_sock()：

  https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=9b8c88f875c04d4cb9111bd5dd9291c7e9691bf5
