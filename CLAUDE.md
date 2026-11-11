# Repository Guide

This repository is a personal technical archive. It contains long-lived notes, downstream-facing troubleshooting docs, generated analysis results, scripts, and local agent/tooling configuration.

## Directory Layout

| Path | Purpose |
|---|---|
| `notes/` | Long-lived learning notes, organized by technical area. Prefer durable concepts, design tradeoffs, and mental models over version-specific implementation trivia. |
| `docs/` | Troubleshooting guides for downstream users. These should be operational: explain the mechanism, provide a decision map, and include concrete commands. See `docs/CLAUDE.md` for the expected structure. |
| `results/` | Output from focused investigations, reports, mind maps, and generated summaries. Use this for one-off or deliverable-style artifacts that are not yet canonical notes. |
| `scripts/` | Small helper scripts and sample logs used to parse, analyze, or demonstrate technical traces. Keep scripts narrow and document their expected input if non-obvious. |
| `patch/` | Patch files or kernel change snippets preserved for reference. |
| `prompts/` | Reusable prompts and prompt fragments. |
| `neovim/` | Neovim configuration and related notes. Treat this as a separate config area, not part of the kernel notes. |
| `skills/` | Reusable agent skills kept with the archive (e.g. `write-kernel-bug-report`). |
| `.claude/`, `.codex/`, `.agents/` | Local agent configuration and skills. Do not treat these as user-facing documentation. |

## Notes Subtree

`notes/` is the main knowledge base. Current topic folders include:

| Path | Topic |
|---|---|
| `notes/linux-kernel/` | General track: Linux kernel notes split by subsystem: `rcu/`, `mm/`, `iommu/`, `interrupt/`, `pci/`, `gpu/`, `general/`. Chip features tied to one subsystem (MPAM, TLBID…) stay in that subsystem's folder. |
| `notes/linux-kernel/rcu/` | RCU internals: overview, QS reporting, trace events, API use, `rcu_sync`, and subsystem breakdowns. |
| `notes/linux-kernel/mm/` | Memory-management topics such as boot memory init, OOM, Maple Tree, MMU features, MPAM, and page-fault diagrams. |
| `notes/linux-kernel/iommu/` | IOMMU / SMMU. |
| `notes/linux-kernel/interrupt/` | Interrupt architecture and ARM-specific interrupt/SDEI material. |
| `notes/linux-kernel/gpu/` | GPU-related notes, currently AMD-focused. |
| `notes/linux-kernel/general/` | Temporary or general kernel notes. Clean up or promote durable material when it becomes stable. |
| `notes/chip-kernel/` | Specialized track: chip software–CPU / software–hardware co-design. Holds `chip-kernel-roadmap.md` (the capability roadmap) and cross-cutting topics: `platform-boot/` (boot, bring-up, reboot) and `computer-architecture/` (computer architecture study notes). |
| `notes/ai-infra/` | AI infrastructure notes: `ascendc/`, `pytorch/`, `transformer/`, `vllm/`. |

学习和讲解类的笔记（讲清一个机制或模块）按 `tech-mastery` skill 的要求写。

## Where New Work Goes

- Put durable technical understanding in `notes/<topic>/` (kernel subsystems under `notes/linux-kernel/<subsystem>/`; cross-cutting chip co-design topics under `notes/chip-kernel/`).
- Put downstream runbooks or issue triage guides in `docs/`.
- Put investigation output, CVE writeups, diagrams, and generated reports in `results/`.
- Put reusable parsing or analysis helpers in `scripts/`.
- Put patches in `patch/`.

If a result becomes part of the stable knowledge base, move or rewrite it into `notes/` instead of linking to a transient result forever.

## Writing Preferences

- Write primarily in Chinese unless the surrounding file is clearly English-only.
- Prefer concise explanations with concrete mechanisms and causality.
- Use source-level details only when they clarify the design. Do not bury the main idea under function names and field names.
- For Linux kernel topics, distinguish stable concepts from version-sensitive implementation details.
- Keep generated HTML, Draw.io files, logs, and large outputs out of prose note directories unless they are the primary artifact.

## Operational Notes

- Use `rg` / `rg --files` for repository search.
- Before editing a subdirectory, check for local guidance files such as `AGENTS.md` or `CLAUDE.md`.
- Do not rewrite unrelated notes while making a focused change.
- The repository may contain local generated files or partial investigations; preserve unrelated work.
