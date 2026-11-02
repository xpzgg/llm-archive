# PR 30245（MOC bypass cache）AI Review 真伪核查

- 对象：<https://atomgit.com/openeuler/kernel/pull/30245/discuss>
- 目标分支：`OLK-6.6`（base `a233c75ea23f`），PR head `bb5d6b5616548686d633c88bd51dcf5263f5ba31`
- 系列：arm64 PBHA（FEAT_HPDS2）backport + openEuler 自有 MOC 接口，17 个 commit
- 待核查对象：`hulk-ci-bot` 的 6 条 AI Review（一个 commit 一条），另有 2 条无风险报告
- 核查日期：2026-10-31

## 结论摘要

6 条 AI Review 中，**真正需要动手的只有 3 处**：

1. `877c7daf` 的 `examples:` 写成 `-|`，会让 `make dt_binding_check` 硬失败，并让整个 binding 被丢出 processed-schema.json。
2. `51464613` 的 `early_pbha_init()` 让 `ARM64_HAS_PBHA_STAGE2` 永不置位（AI 标"提示"，实际是全 PR 唯一有功能后果的）。
3. 文档层：6 处 `arm/cpus.yaml` 旧路径引用、`pbha.rst` 放错目录、缺 `maintainers`、bit 编号自相矛盾（含 example）。

其余（d3c044 的 VTCR 两条、514646 的混合 MIDR 与 pKVM 两条、594dcb 的 bit59 不展示、877c 的 `/chosen` 未描述）都属设计确认或补充说明，不该算回归。

## 核查方法

1. 拉 PR 元信息与评论：`/api/v5/repos/openeuler/kernel/pulls/30245/comments`（20 条，其中 hulk-ci-bot 6 条）。
2. 用 `/api/v5/repos/openeuler/kernel/contents/<path>?ref=<sha>` 取 PR head / base 的真实文件，不看 diff 转述。
3. 本地装 dtschema 2026.9，对真实 binding 实跑 `dt-doc-validate`、`dt-mk-schema`、`dt-extract-example`，不靠推理。

```bash
python3 -m venv /tmp/dtsv && /tmp/dtsv/bin/pip install dtschema   # 2026.9
/tmp/dtsv/bin/dt-doc-validate -u <bindings-dir> arm/cpus.yaml      # exit 1
/tmp/dtsv/bin/dt-mk-schema -o out.yaml <dir>                       # exit 0，坏文件被忽略
/tmp/dtsv/bin/dt-extract-example arm/cpus.yaml                     # 生成非法 dts
```

## 逐条判定

| # | commit | AI 报的问题 | 判定 |
|---|---|---|---|
| 1 | `d3c0443a88c8` KVM: Detect and enable PBHA for stage2 | ①对所有 FEAT_HPDS2 CPU 无条件写 VTCR 0xf，未按组合语义门控 ②pKVM host 的 stage2 也被置 0xf，host stage1 PBHA 静默失效 | **都不算回归**。代码与 2021 RFC v1 逐字一致；①是 Marc Zyngier 当年提出、作者承认的 imp-def 下注，也是系列停在 RFC 的原因；②属"是否有意"的设计确认，只影响性能 |
| 2 | `e0bfcc0ae939` rename cpu.yaml | 6 处文档引用仍指向 `arm/cpus.yaml` | **真**，纯文档、低危 |
| 3 | `877c7daf192e` 新增 cpus.yaml binding | ①`examples` 用 `-\|` ②`/chosen` 位置未写进 binding ③6 处引用 ④拼写 `a a` + bit 编号矛盾 + 缺 maintainers | **①③④真，②属补充**。①会硬失败；③与 2 重复；④其中 maintainers 的严重性被 AI 自己判低了 |
| 4 | `5146461312c6` KVM: Configure PBHA bits for stage2 | ①混合 MIDR 触发 `cpu_die_early()` ②pKVM 从 EL1 可控内存读 safe_bits ③`for (i = 0 ;` 空格 ④early_pbha_init 跳过 stage2 分解 | **③④真**（④被降级为"提示"，应升为真问题）；**①②不算**：①是 cpufeature 对 system feature 的标准处理且平台同构，②是 2021 年的加固建议、6.6 pKVM 里同类做法普遍 |
| 5 | `e82b27e306ce` pbha 文档 | rst 放 `Documentation/arm64/`，toctree 在 `Documentation/arch/arm64/index.rst` | **真**，文档不进构建，低危 |
| 6 | `594dcb85b03d` ptdump | ①`set="PBHA0"` / `clear="  "` 列不对齐 ②只展示 bit59 | **①真**（纯显示）；②属预期，调试输出只覆盖本系列实际用到的 PBHA0 |

另外两条（`f93552f61ff5`、`61ec4b08dfbd`）AI 报"无潜在风险"，与核查结果一致。

## 真问题细节

### 1. `arm/cpus.yaml` 的 examples 写法（唯一的硬故障）

`Documentation/devicetree/bindings/arm/cpus.yaml` 把原来的 `- |` 改成了 `-|`（空格被吃掉），`examples` 于是被解析成一个折叠字符串而非字符串数组。

实测后果链：

- `dt-doc-validate`：`examples: '...' is not of type 'array'`，exit 1。
- `dt-mk-schema`：打印 `ignoring, error in schema: examples`，**exit 0**，但该 binding 不进 `processed-schema.json` → 以后 `make dtbs_check` 不会拿它校验任何 dts，等于这个新 binding 从未生效。
- `dt-extract-example`：生成的 `cpus.example.dts` 里把 `-` 当成节点名，dtc 报 syntax error；OLK-6.6 的 `always-$(CHECK_DT_BINDING) += *.example.dtb` 会构建它，所以全量 `make dt_binding_check` 在此中止。

即使补上空格，example 还有两个问题：内容里的 `...` 不是合法 DTS；`cpu@0` 缺 `reg`，而改名后的 `cpu.yaml` 的 `required` 含 `reg`，示例校验过不了。

### 2. `early_pbha_init()` 短路 stage2 分解（唯一有功能后果的）

`arch/arm64/kernel/cpufeature.c`（PR head）：

- `early_pbha_init():2257` 执行完 `update_pbha_perf_only_bit()` + `enable_pbha_inner()` 后置 `pbha_enabled = true`，但没有调用 `stage2_test_pbha_value()`。
- `plat_can_use_pbha_stage1():2155` 有 `if (pbha_enabled) goto out_unlock`，于是 2179 行那段 stage2 分解循环永远不跑。
- 结果 `arm64_pbha_stage2_safe_bits` 恒为 0 → `plat_can_use_pbha_stage2():2300` 的 `return !!arm64_pbha_stage2_safe_bits` 为 false → `ARM64_HAS_PBHA_STAGE2` 永不置位。
- 后果：`kvm_get_vtcr()` 一直用默认的 `0xf`，guest 的 PBHA 位被 stage2 全屏蔽，KVM guest PBHA 静默不生效。

触发条件是 DT 启动且 `/cpus` 或 `/chosen` 带 `arm,pbha-performance-only` —— 也就是这套特性最常见的路径；ACPI 启动反而不受影响。

修法二选一：在 `early_pbha_init()` 里补一遍 `stage2_test_pbha_value()` 循环；或把 `pbha_enabled` 拆成"DT 已解析"和"stage1 已使能"两个状态，让 stage1 的 system-scope 回调仍能补算 stage2。

### 3. 文档层四项

- **6 处旧路径引用**（实测每个文件 1 处）：`cpu/cpu-capacity.txt`、`cpu/cpu-topology.txt`、`cpu/idle-states.yaml`、`interrupt-controller/apple,aic.yaml`、`interrupt-controller/apple,aic2.yaml`、`interrupt-controller/arm,gic-v3.yaml`。rename 后这些引用指向的是新的 cpus 容器 binding，语义错了。
- **`pbha.rst` 放错目录**：PR head 上 `Documentation/arm64/` 只剩 `ilp32.txt` + `pbha.rst`，而 toctree 条目加在 `Documentation/arch/arm64/index.rst`。sphinx 会报 `toctree contains reference to nonexisting document`，这份文档不进入构建。
- **缺 `maintainers`**：meta-schema 确实要求（2026.9 实测 `'maintainers' is a required property`）。
- **bit 编号矛盾**：正文自己写了 `value 5` = bit-0 + bit-2（⇒ bit-2 权重 4），却写 "the values 2, 4, 6"，2 是 bit-1；而且 example 里 `arm,pbha-no-aliases = <0x02 0x04 0x06 0x08>` 沿用了同一个错，只改句子会留下自相矛盾的示例。

## AI Review 自身判错之处

1. **`877c7daf` 报告称"dtschema 2026.9 meta-schema 不强制 maintainers"** —— 假。实测仍 required，`dt-doc-validate` exit 1。
2. **把 `51464613` 的 early_pbha_init 问题降级为"提示"**，并在同一条报告里写"未发现 regression"。它才是这份 PR 里唯一有功能后果的问题。
3. **`FINAL REGRESSIONS FOUND: 2 / 4` 数字虚高** —— 把 VTCR 设计确认、混合 MIDR 等算作回归，与自己的"非 bug"措辞矛盾。

## 附：另一份评审意见的结论修正

此前另有两条评审意见（不在该 PR 的 API 评论列表中，来自别的工具）：

- **"缺 maintainers 会让 `cmd_mk_schema` 失败、target 中止"** —— 结论错。`cmd_chk_bindings` 在 OLK-6.6 和 7.3 里都是 `... || true`；`dt-mk-schema` 对 meta-schema 违规只是"ignoring"并 exit 0。真实危害是这条规则没有构建门禁，只能靠 review 拦，而不是构建失败。
- **"bit 编号与 value 5 的例子不一致"** —— 完全成立，补一句：example 也错了。

## 待修清单

1. `cpus.yaml`：`examples` 改回 `- |`，示例写成合法 DTS（去掉 `...`，`cpu@0` 补 `reg`）。
2. `cpufeature.c`：`early_pbha_init()` 补 stage2 分解，或拆 `pbha_enabled` 状态。
3. `cpus.yaml`：补 `maintainers`；修 `a a` 拼写与 "2, 4, 6" → 与 bit-2 语义一致的值，并同步 example。
4. 六处 `arm/cpus.yaml` 引用改 `arm/cpu.yaml`。
5. `pbha.rst` 移到 `Documentation/arch/arm64/`。
6. 顺手：`ptdump.c` 的 `clear` 改成 5 个空格；`for (i = 0 ;` 去掉分号前空格。

## 备注

把上游仍在使用的 `arm/cpus.yaml` 改名，而这个系列从未合入上游，后续 backport 会持续冲突。若保留 rename，六处引用应一并改掉；否则给新 binding 换个文件名更省事。
