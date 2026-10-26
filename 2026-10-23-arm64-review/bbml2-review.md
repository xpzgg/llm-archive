# BBML2 noabort / BBML3 静态审查

## 结论与范围

固定 HEAD：`00ba9aa3d7f58091cb87f8da96599d6d72023ea0`；MR !28481 merge：`c1f3198b5061cf9bb1b661ff77d06afd3e1af3c0`，相对第一父提交审查 24 个提交、22 个文件。最终实现使用 BBML3 命名，包括 capability 检测、contpte TLBI 消除、内核大页分裂、rodata=full、异构 CPU 回退、KFENCE、Realm、KPTI、present-invalid、trans_pgd 及通用 pagewalk API 调整，并追查固定 HEAD 上的 TLBID 和 OBMM 集成。

识别出两个需要开发人员优先复核的高置信问题候选。下述代码路径与条件矛盾已由静态源码确认；未进行运行复现，因此不将其表述为已在硬件验证的故障。按用户最新要求，不新增测试、构造模型或执行内核验证。严重程度表示条件成立时的影响，与证据置信度分开。

## 候选 1：rodata=full 的大页优化破坏 OBMM 对线性映射粒度的假设

**严重程度：高；静态证据置信度：高；类型：本 MR 引入的 OLK 特性交互回归。**

关键位置（均为固定 HEAD）：

- `arch/arm64/mm/mmu.c:658` 的 `force_pte_mapping()` 在 BBML3 上返回 false；`:1062` 开始的 `map_mem()` 允许 rodata=full 建立 PUD/contiguous 映射。
- `arch/arm64/mm/pfn_range_alloc.c:419`：`check_update_lm_arg()` 遇到 `can_set_direct_map()` 为真直接返回成功；`:439` 的 `invalid_pud_entry()`、`:459` 的 `invalid_pmd_entry()` 直接改整个 leaf，没有检查本次区间是否覆盖整个 leaf。
- 同文件 `:500` 的 `set_linear_mapping_invalid()` 和 `:584` 的 `set_linear_mapping_nc()` 没有调用 `split_kernel_leaf_mapping()`。
- `drivers/ub/obmm/ubmempool_allocator.c:364` 的 `buddy_alloc_memseg()`，`:222` 的 `set_memseg_linear_mapping_invalid()`。

引入语义变化的提交是 `56f1c1ef7258159392898a9f8658a5f40ef546b3`（允许 rodata=full 大页映射）；原有 PFN range API 不分裂的代码此前依赖 `can_set_direct_map()` 意味着页粒度映射。MR 虽把其 walker 调用改名，却未更新这一假设。

可达触发链：

1. 4K 页、BBML3 CPU（包括本 MR 的 HIP13 白名单）、`rodata=full`、`CONFIG_PFN_RANGE_ALLOC`/OBMM；`contig_mem_pool_percent != 100`，使全局 PMD-only 限制不生效；普通本地 RAM 中存在完整 1GB PUD block。
2. OBMM 默认分配器是 `buddy_highmem`（`:440`），默认粒度为 `OBMM_BASIC_GRANU == 2MB`（`obmm_core.h:41`、allocator `:512`）。`pfn_range_alloc(512, nid)` 在 `can_set_direct_map()` 为真时使用普通 folio 分配（`:268`），可以获得某个仍由 PUD block 映射的 2MB 区间。
3. `buddy_alloc_memseg()` 随后使该区间线性映射失效。参数检查直接通过；walker 向 PUD 回调传入的虽然只是 2MB 子区间，`invalid_pud_entry()` 仍清整个 PUD 的 VALID。
4. 结果是 2MB 所属的整个 1GB 线性映射失效。相邻页可能仍被内核使用，之后访问会发生 translation fault。恢复同一子区间时又恢复整个 PUD，可能使同 PUD 中另一个已借出的区间意外重新可访问。`set_linear_mapping_nc()` 还可改变整个大块的内存属性。

被破坏的不变量是：区间权限/属性操作不得影响区间外的物理页。BBML3 只允许特定映射转换省略 BBM，并不让一个 PUD 描述符只对局部 2MB 生效。

反证核验：

- `can_set_direct_map()` 仍因 rodata_full 返回 true，并未改成检测当前页表粒度。
- `pmd_mapping_reserve_and_remap()` 在 can_set_direct_map 为真时直接返回，且零比例默认直接返回，没有把这些页预先拆成 PMD/PTE；其“mapped at PTE level”日志正体现旧假设。
- OBMM 选择器对非 100% 比例的限制只适用于 `hugetlb_pmd`，不阻止默认 `buddy_highmem`。
- `split_kernel_leaf_mapping()` 只接入 `pageattr.c` 的属性操作；这里使用另一套 callback，不会间接经过它。
- 非 BBML3、debug_pagealloc 强制 PTE、或目标页已被此前操作拆细时不触发该 PUD 示例，不能据此否定未拆页的路径。
- 固定 HEAD 之前未见该路径的后续分裂修复。

修复方向：在这两个 PFN range API 修改前确保边界按真实 leaf/contiguous 粒度拆分，检查分裂错误并与并发页表修改协调；同时更新 `can_set_direct_map()` 的消费者，不能继续用它推断 PTE-only。不可只增加 PUD 对齐检查而静默破坏已有 2MB OBMM 接口。对 contiguous group 的处理也须覆盖。

交开发人员确认：目标产品的 OBMM 是否允许上述启动参数组合、实际分配 folio 所处页表层级。若产品固定使用 PMD-only，4K/2MB 示例可被部署约束规避，但通用内核接口仍存在语义回归。建议开发人员审阅页表更新前后的邻页权限；本次未运行此验证。

## 候选 2：present-invalid 未接入 pud_present，Realm 大块共享后无法恢复 PUD

**严重程度：高；静态证据置信度：高；部署触发置信度：中；类型：本 MR 大页支持及回移适配不完整。**

关键位置：

- `arch/arm64/include/asm/pgtable.h:97`：`pte_present()` 只识别 `PTE_VALID | PTE_PROT_NONE`。
- `:752`：`pud_present()` 直接使用该定义；`:753`：`pud_leaf()` 要求 pud_present 为真。
- `arch/arm64/include/asm/pgtable-prot.h:28`：新 `PTE_PRESENT_INVALID` 为 bit 59，与 bit 58 的 PROT_NONE 不同。
- `arch/arm64/mm/pageattr.c:44` 的 PUD 属性回调只处理 pud_leaf；`:300`、`:316` 是 Realm 的失效/恢复两次属性更新。
- `mm/pagewalk.c` 的 `walk_pud_range()` 在回调后遇到 `!pud_present()` 直接跳过子树，未返回错误。

相关提交：`56f1c1ef7258` 允许大块；`dfe0724243c7` 泛化 present-invalid；`45d2c73251fa` 声称修复 invalid large leaf，并将回调切换为 pud_leaf，但未使固定分支的 pud_present 识别新编码。该 commit message 描述的 PMD 修复不能自动覆盖此分支的 PUD 实现。

触发链：

1. 支持 BBML3 的 Realm guest，4K 页，足够连续 RAM，有完整 PUD block。某次 `set_memory_decrypted()` 覆盖至少一个完整 PUD；例如配置大型早期 SWIOTLB。
2. `swiotlb_update_mem_attributes()`（`kernel/dma/swiotlb.c:264`）把整个池传给该 API。池大小由 `swiotlb=` 参数控制（`:185`），早期池通过 memblock 分配（`:329`），不受 buddy MAX_ORDER 的小分配上限约束。选择大于等于 2GB 的连续池，区间内部可包含完整 1GB PUD，而不要求池起点恰好 1GB 对齐。实际机器需有满足分配与 RSI 共享条件的 RAM。
3. 分裂函数只拆区间两端，内部完整 PUD 保留。第一次回调把 PUD VALID 清零并设置 PRESENT_INVALID，RSI 共享操作成功。
4. 第二次准备恢复时，PUD 的 VALID=0、PROT_NONE=0、PRESENT_INVALID=1，故 pud_present=false、pud_leaf=false。属性回调不修改，walker 也跳过，最终返回 0。
5. 该完整 PUD 永久保留 invalid 状态，而调用者认为共享完成。后续 SWIOTLB bounce 的 CPU 访问可能 fault。

反证核验：

- PMD 的 `pmd_present()`（`:492`）额外 OR 了 `pmd_present_invalid()`，因此 PMD 可以恢复；常规较小池覆盖 PMD 的成功不能反驳 PUD 问题。
- 非 BBML3 Realm 在 `force_pte_mapping()` 中退回 PTE，避开此问题；这是 BBML3 新启用的大块路径。
- 范围两端的拆分不能保证内部 PUD 也拆分。大池只要求覆盖完整 PUD，不要求所有边界都对齐。
- `kernel_page_present()` 也用 pud_leaf 判定，遇到这种 invalid PUD 会误按下级页表处理；这是同一编码不一致的次生风险，未另算问题。
- `trans_pgd` 改为依据 table/leaf 路径并显式 mkvalid，不能修复日常 pageattr 的第二遍跳过。
- 固定 HEAD 未见补齐 pte_present/pud_present 的后续修复。

修复方向：统一 present-invalid 在各级 present/leaf helper 中的语义（尤其 PUD），或显式增加符合本分支编码的识别；审阅 swap/用户映射相关 helper，避免简单扩展 bit mask 后影响原有语义。随后检查 pageattr、ptdump、split、kernel_page_present 的一致性。

交开发人员确认：Realm 环境是否允许 BBML3 和大 SWIOTLB 组合、池所在 PUD 的实际层级、RSI 对共享区间的限制。代码级漏恢复不依赖 TLB 冲突等硬件细节，但本次未验证该配置能否在目标机器完整启动。

## 待验证风险及已排除候选

- **HIP13 白名单的硬件保证**：`cpu_supports_bbml3()`（cpufeature.c:2174）接受所有 HIP13 版本，未按 revision 限制。未取得足以证明每个 revision 无 TLB conflict abort 且不会组合旧新条目属性的芯片一手资料，故不把白名单本身认定为缺陷。Arm 官方旧版资料仅列 BBM 0/1/2，不能用旧文档否定后续 BBML3 定义；需要厂商/新版架构文档确认。检索到的[Arm 已知问题文档](https://documentation-service.arm.com/static/686bf0c446dc1e49e34b8121)属旧版定义，未将其当作 BBML3 否定证据。
- **异构/CPU 热插拔**：新 MATCH_ALL_EARLY_CPUS 在启动 CPU 置位，后续早期 CPU 缺失即清除；不能被另一个支持 CPU重新开启。最终启用时不支持的 late CPU 被 verify_local_cpu_caps 拒绝；最终未启用时支持的 late CPU 不会改变已确定系统能力。因此未确认“热插拔绕过 BBML3 全局约束”。
- **早期异构回退**：CPU0 执行整段拆分，其余 CPU 切换至 idmap 并移除 TTBR1；完成后全局 TLBI，再清共享 flag。未确认缺失等待或无同步发布。分配失败会 panic，是显式失败策略，未把缺少优雅降级单列为正确性缺陷。
- **KPTI**：改造为能力确定后独立 stop_machine 重画 nG，保留每 CPU vector 更新，且在 linear-map 回退之后执行。共享 flag 在各轮前重置；未确认跨两轮混用错误。
- **TLBID**：contpte_convert 的 BBML3 优化省略中间 TLBI，但保留调用者最终权限/映射变更所需的刷新契约。kernel pageattr 使用 `flush_tlb_kernel_range()`，固定 HEAD 的此路径仍使用广播 vaale1is 或 flush_tlb_all，没有通过用户 mm 的 TLBID domain 缩小刷新范围。未确认这两条路径直接产生 TLBID 漏刷；没有扩展宣称整个 TLBID 实现安全。
- **KFENCE/debug_pagealloc/原子上下文**：debug_pagealloc 始终 PTE；KFENCE 早期池预映射为 PTE、晚期池初始化时拆分，并在常规保护路径绕开 mutex。未确认这两类原子调用引入睡眠。一般 Realm/secretmem/权限修改有新增分配失败的可能；pageattr 已传播错误，未仅因可失败就列缺陷。
- **并发 split**：split mutex 序列化大页拆分；页表发布前有 dsb(ishst)。权限更新在 mutex 外，但正常互不重叠对象的边界拆分已消除共享 cont group；没有构造出足够证据的独立丢更新问题。内存热移除不由 mmap_lock 自动保护，walker 新注释明确要求调用者保证范围生命周期，不把该已有限制直接算新增 bug。

## 实质改动覆盖记录

| 改动组 | 覆盖内容及结论 |
| --- | --- |
| capability 与 sysreg | 新类型、检测/日志、ID 字段、MIDR 白名单、early/late CPU 规则；硬件白名单列待确认 |
| contpte | folding/unfolding、AF/dirty 聚合、临时 clear 与最终 TLBI 契约；未确认新增缺陷 |
| 大页分裂 | PUD→PMD→PTE、cont 位、边界优化、有效性继承、分配失败、发布屏障；发现 OLK 旁路调用者遗漏 |
| rodata/full 与 early init | map_mem、memblock_free_all 后 SWIOTLB 属性更新、page_alloc_available、异构回退、热添加映射策略 |
| pageattr | PMD/PUD/PTE 回调、有效/无效转换、readonly 线性别名、Realm、secretmem 接口；发现 PUD 编码缺口 |
| KFENCE/KPTI/proc.S | 早/晚池、原子保护、stop_machine、idmap 同步旗标及 TTBR1 恢复 |
| trans_pgd/pgtable | PTE_PRESENT_INVALID、pmd_mkinvalid、mkvalid、hibernate/kexec 临时映射恢复；PUD helper 不一致已报告 |
| 通用 pagewalk | kernel lockless/locked 和 debug 拆分；openrisc/riscv/sw_64 的旧 init_mm 调用迁移保持 init_mm 语义；ptdump 保留内存在线锁与 mmap 写锁 |
| OLK 集成 | pfn_range_alloc、OBMM、TLBID kernel flush、virtCCA 强制 PTE；未将只出现于外部假设的行为列为确认故障 |

## 实际检查与限制

实际执行：阅读 merge diff、24 个提交列表、重点引入提交、固定 HEAD 调用者；`rg` 搜索 pagewalk/valid/noflush/PFN range 调用；查阅 merge 后相关文件历史；确认共享工作树 HEAD；检索 Arm 一手文档的可得范围。没有修改源码、切分支、提交代码。

归档：`evidence/bbml2-merge.diff`、`bbml2-commits.txt`、`bbml2-stat.txt` 是原始范围材料；`evidence/bbml2-static-evidence.txt` 保存固定 HEAD 带行号摘录，内容仅为静态证据。

未执行：编译、单元测试、模型运行、QEMU/真机启动、Realm 共享、大池 SWIOTLB、OBMM 运行、CPU 热插拔、TLBID 压力测试。遵循用户“只给分析，由开发人员 review 是否真实”的最新要求，报告供人工复核；不存在运行复现结果或全面安全保证。
