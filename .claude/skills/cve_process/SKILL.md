---
name: cve_process
description: >
  Linux Kernel CVE 回合（backport）分析助手。当用户提到 CVE 编号、内核 patch 链接
  （git.kernel.org、GitHub commit、patchwork 等）、或直接粘贴 patch 原文并要求分析时触发。
  适用场景包括：分析 CVE 根因、理解 patch 修复逻辑、评估 backport 可行性与依赖、
  生成缺陷跟踪系统（如 Bugzilla/Jira）用的根因/方案摘要。
  即使用户只说"帮我看下这个 commit"或"这个 patch 能回合吗"也应触发此 skill。
---

# CVE Backport Analyzer

协助内核安全工程师将上游 CVE patch 回合（backport）到发行版内核。
**所有回复使用中文，技术术语保留英文原文。**

---

## 信息获取策略

**核心原则：WebSearch 优先，避免无效的 WebFetch 调用。** NVD、GitHub、patchwork、lore、lists.freedesktop.org 等域名的 WebFetch 在企业网络环境中通常被拦截，浪费 token。

### 步骤 1：WebSearch（必须最先执行）

根据用户输入类型，选择对应的搜索策略：

**仅有 CVE 编号（如 CVE-2026-31490）：**
```
搜索 1: "<CVE-ID> Linux kernel patch commit"
搜索 2: "<CVE-ID>" （补充搜索，获取更多细节）
```
stack.watch、cybersecurity-help.cz、opencve.io 等站点的搜索结果摘要通常直接包含：
- commit hash 和 title
- 影响版本范围
- 漏洞描述和修复方案原文

**有 commit hash / patch 链接：**
```
搜索 1: "<commit-hash> <patch-title关键词> diff"
搜索 2: "<函数名> <漏洞类型关键词> fix"
```

**用户直接粘贴 patch/diff 原文：**
→ 跳过搜索步骤，直接进入分析。

### 步骤 2：仅在必要时 WebFetch

仅当搜索结果摘要信息不足（缺少 patch diff 细节、缺少函数名等）时，才尝试 WebFetch：
- 优先尝试 `stack.watch` 的 CVE 页面（该站对 WebFetch 友好度较高）
- 不要尝试 NVD、GitHub、patchwork、lore、lists.freedesktop.org（已知被拦截）
- 若 WebFetch 均失败，基于搜索结果摘要直接分析即可——摘要通常已包含足够信息

### 步骤 3：信息仍不足

明确告知用户缺少哪些信息，请用户提供 patch 原文或 diff。

---

## 输出格式

### 一、根因分析

回答一个核心问题：**这个 bug 为什么存在？** 不是复述代码触发流程，而是找到那条最本质的逻辑矛盾链。说清楚原有代码的设计假设是什么、哪里想漏了、哪个 invariant 被违反了——说清楚"当时的开发者为什么没考虑到这个情况"。

---

### 二、修复逻辑分析

结合 commit message 和 patch diff，讲清楚 **patch 做了什么、为什么能修复问题**。修复逻辑不是说明"改了哪些行"，而是说明"怎么打破那条矛盾链"。

- **修复思路**：patch 的整体策略是什么? 不是说明改了哪些行，而是说明怎么打破那条矛盾链。
- **逐个改动解读**：对每个有意义的 hunk，解释改动的目的——为什么要这样改、这个改动和根因的因果关系。每个改动从矛盾链的哪个环节入手、如何协同使矛盾不再成立
- **为什么有效**：把这些改动串起来，解释它们如何消除根因中提到的设计缺陷

---

### 三、系统记录摘要

> ⚠️ 以下两条用于直接更新到缺陷跟踪系统，务必精炼准确。

**【问题根因】**
（一句话：点出最本质的逻辑矛盾链）

**【解决方案】**
（一句话：通过什么方式打破了这条矛盾链）

---

### 四、Kconfig 依赖

通过 patch 中的文件路径，在内核源码中追溯 Kconfig/Makefile，确认依赖的编译选项。将模板中的占位符替换为实际值，**不得修改模板原文措辞**，仅替换 `CONFIG_XX`/`CONFIG_xxx`/`xxx.ko` 部分。如果 config 是 bool 类型（只有 y/n），不需要 `|| =m` 分支和 KO 依赖行。

若存在依赖链（如 `CONFIG_A` depends on `CONFIG_B`，`CONFIG_B` depends on `CONFIG_C`），只需写链上最末端的 config——它能 transitive 地覆盖所有上游依赖，无需逐层列出。

```
CONFIG依赖：CONFIG_XX=y || CONFIG_XX=m 则涉及。
KO依赖：如果CONFIG_xxx以=m的形式打开的情况下，则可排查xxx.ko是否被加载，没有被加载则不涉及。
```

---

## 分析约束

- 严格基于 patch 实际内容分析，不得臆测无法从 diff 中得出的结论
- 技术术语保留英文原文，说明用中文
- 若 patch 涉及多个 commit（fix + fix-of-fix），逐一说明各 commit 的角色
