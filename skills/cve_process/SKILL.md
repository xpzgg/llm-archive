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

深入分析漏洞成因，帮助工程师理解问题本质：

- **代码路径**：定位出问题的 subsystem / driver / 函数调用链
- **触发条件**：说明漏洞如何被触发（竞态条件、越界访问、use-after-free、整数溢出等）
- **缺陷本质**：解释原有逻辑为何存在安全缺陷

简明扼要，聚焦安全相关事实，避免重复 patch diff 的内容。

---

### 二、解决方案分析

深入分析 patch 的修复逻辑，帮助工程师理解改动意图：

- **修复策略**：加锁、边界检查、指针校验、引用计数修正等
- **关键改动**：涉及的核心函数 / 结构体 / 宏
- **上下文依赖评估**：API 差异、结构体变化、前置依赖 patch 等 backport 风险点

---

### 三、系统记录摘要

> ⚠️ 以下两条用于直接更新到缺陷跟踪系统，务必精炼准确。

**【问题根因】**
（一句话：在什么场景下，哪里的什么逻辑缺陷，导致了什么安全问题）

**【解决方案】**
（一句话：通过什么修复手段，在哪里解决了该问题）

---

### 四、Backport 注意事项

按以下维度逐项评估，无问题则注明"无"：

| 维度 | 说明 |
|------|------|
| **前置依赖 patch** | 是否有必须先合入的上游 commit |
| **API / 函数签名变化** | 目标内核版本是否存在接口差异 |
| **数据结构变化** | 相关 struct 字段是否有版本差异 |
| **Kconfig 依赖** | 是否依赖特定编译选项 |
| **测试建议** | 验证 backport 正确性的推荐方法 |

---

## 分析约束

- 严格基于 patch 实际内容分析，不得臆测无法从 diff 中得出的结论
- 技术术语保留英文原文，说明用中文
- 若 patch 涉及多个 commit（fix + fix-of-fix），逐一说明各 commit 的角色
