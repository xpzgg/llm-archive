---
name: cve-backport-analyzer
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

## 输入处理

### 链接转换规则

`git.kernel.org` 限制 robot 访问，需自动转换为 GitHub 镜像后访问：

```
https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=<HASH>
→
https://github.com/torvalds/linux/commit/<HASH>
```

其他链接类型（GitHub、NVD、Patchwork、lore.kernel.org 等）直接访问，无需转换。

### 输入失败处理

- 链接转换后仍无法访问 → 明确告知，请用户提供 patch 原文或 diff
- 用户直接粘贴 patch/diff 原文 → 跳过链接步骤，直接进入分析
- 仅提供 CVE 编号无 patch → 先查询 NVD（`https://nvd.nist.gov/vuln/detail/<CVE-ID>`）获取关联 commit，再分析

---

## 输出格式

---

### CVE 基本信息

| 字段 | 内容 |
|------|------|
| **CVE ID** | |
| **影响子系统** | |
| **Upstream Commit** | |
| **影响版本范围** | （如已知） |

---

### 一、根因分析

深入分析漏洞成因，帮助工程师理解问题本质


---

### 二、修复逻辑分析

深入分析 patch 的修复逻辑，帮助工程师理解改动意图：

---

### 三、摘要

> ⚠️ 以下两条用于直接更新到缺陷跟踪系统，务必精炼准确。

**【问题根因】**
（一句话：在什么场景下，哪里的什么逻辑缺陷，导致了什么安全问题）

**【修复逻辑】**
（一句话：通过什么修复手段，在哪里解决了该问题）

---


---

## 分析约束

- 严格基于 patch 实际内容分析，不得臆测无法从 diff 中得出的结论
- 技术术语保留英文原文，说明用中文
- 若 patch 涉及多个 commit（fix + fix-of-fix），逐一说明各 commit 的角色

### 引入问题补丁分析（默认不执行）

仅当用户**明确要求**分析引入问题的 commit 时才执行（如"帮我确认下引入问题的补丁"）。不主动分析。

执行时必须：
1. 通过 `git log` 回溯缺陷代码的修改历史，定位引入 commit
2. **验证**：确认引入 commit 之前该缺陷不可触发（如版本检查、条件判断等保护是否阻止了触发路径）
3. 给出结论和验证依据，不能想当然
