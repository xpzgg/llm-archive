# SLUB Sheaves（7.x 新架构，补充笔记）

> 基于 Linux 7.1-rc1。2025 年底合入（Vlastimil Babka），**彻底删除了经典的 `kmem_cache_cpu`**（per-CPU 冻结 slab + cmpxchg 无锁 freelist 那套）。网上绝大多数 SLUB 资料讲的都是旧架构，看新代码时注意区分。
> 学习主线用的是 v6.6 旧架构，见 [slub.md](slub.md)。

---

## 设计本质

旧架构的每 CPU 快路径直接持有一张 slab、用页内 freelist 链表分配。问题：① freelist 是链表，pop 一个对象就要摸一次对象所在的 cacheline，批量操作 cache 不友好；② cmpxchg128 无锁技巧复杂难维护；③ 每 CPU 缓存和某张具体 slab 绑死，批量转移不灵活。

Sheaves 把每 CPU 缓存改成**指针数组（"麦捆"）**：push/pop 只动数组尾部，不碰对象内存；整捆交换就是换一个指针，天然支持批量。农场隐喻：对象=谷粒，sheaf=麦捆，barn=谷仓。

---

## 代码骨架

```
kmem_cache
├── cpu_sheaves（每 CPU，local_trylock 保护）
│   ├── main:  slab_sheaf { void *objects[capacity]; size }
│   ├── spare: 备用 sheaf（全空或全满）
│   └── rcu_free: kfree_rcu 批处理专用
├── per_node[nid]
│   ├── barn: 存放整捆的满/空 sheaf（中间层）
│   └── kmem_cache_node.partial: 部分使用的 slab 链表
└── struct slab: 页内侵入式 freelist + inuse/objects 计数（这层没变）

分配逐层降级：
main pop → 空则与 spare/barn 换一捆满的 → 从 partial slab 收割一批装捆 → buddy 要新页
释放逐层反向：
main push → 满则换捆 → barn 满则把整捆对象还回各自 slab 的 freelist
```

与 buddy 体系平行：sheaves ↔ PCP，barn/list_lock ↔ zone->lock，partial 链表 ↔ free_area。同一哲学：锁粒度从"每次操作"变成"每批次"。

---

## 反直觉点

- 数组比 freelist 链表快的关键：pop 链表必须读空闲对象内存里的 next 指针（cache miss），pop 数组只碰 sheaf 自己的 cacheline。
- `struct slab` 依然复用 `struct page` 内存（`SLAB_MATCH` 静态断言保证字段对齐），slab 层的侵入式 freelist 在新旧架构中都存在——sheaves 只是替换了"每 CPU 缓存"这一层。
- `rcu_free` sheaf 让 `kfree_rcu` 攒满一捆再一次性过 RCU 宽限期，摊薄回调开销。
