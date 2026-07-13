---
name: write-kernel-bug-report
description: "Write or rewrite clear Chinese Linux kernel bug reports, especially for Syzkaller, KASAN, UAF, race, refcount, RCU, teardown, and upstream-fix analysis."
---

# Write Kernel Bug Report

Write the result as a technical article, not as a chronological dump of investigation notes.

## Core principle

Find the direct root cause first and put it at the beginning of the report as a one-sentence conclusion. State the complete causal chain:

    who performed which operation
    → which lifetime, ordering, locking, or reference guarantee was missing
    → which concurrent path invalidated the object or state
    → where the stale access or failure finally occurred

For example, do not merely say that call_rcu and kfree were both used. Say that a path queued an embedded rcu_head without retaining its owner, another path freed the owner, and the RCU consumer later dereferenced the dangling node.

Everything else in the report should explain or prove this sentence.

## Narrative structure

Normally organize the report in this order:

1. 摘要：start with the one-sentence conclusion, then give impact, trigger, fix direction, and any important uncertainty.
2. Syzkaller/问题报告信息：present only raw evidence such as crash, allocation, free, work/callback creation, address offset, and object mapping.
3. 背景：explain only the subsystem knowledge needed by the reader, such as object model, connection or creation flow, data path, normal teardown, ownership, and synchronization.
4. 根因分析：connect the evidence to the subsystem model; reconstruct the competing paths and race, and explain why existing protection failed.
5. 修复建议：explain the upstream solution, semantic dependencies, backport strategy, invalid partial fixes, and validation.

Treat this as a narrative order rather than a rigid template. Rename, merge, or omit subsections when the issue requires it, but do not mix evidence, normal background, root cause, and repair into peer sections with overlapping responsibilities.

## Judgment rules

- Distinguish facts directly proven by the report from source-based inference and unresolved questions.
- Treat the crash site as a victim when the real defect is an earlier ownership or lifetime violation.
- Explain normal behavior in the background section; explain the defect only in the root-cause section.
- Use heading levels to express logical containment, not merely to decorate or subdivide text.
- Keep function names, fields, call stacks, tables, and timelines only when they clarify causality.
- When discussing upstream fixes, explain the ownership model they establish and their dependencies; textual applicability alone is not enough.
- Write primarily in Chinese and follow repository-local writing and destination rules.

Before delivering, reread the摘要 and the first paragraph of each main section. A reader should understand the bug from the first screen and be able to follow one continuous argument from evidence to fix.
