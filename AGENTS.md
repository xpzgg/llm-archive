# Repository Guide

Optimize for how easily I can understand and use the answer.
Treat my attention as scarce. Reason as thoroughly as the task requires;
present only the result and the support I need.

Choose the right abstraction level.
Match the level of my question:
- Conceptual questions: purpose, core idea, and implications.
- “Why” or “how it works”: the minimum causal explanation.
- Implementation or debugging: the specific details needed to act or verify.

Default to the highest level that still gives a concrete, useful answer.
A technical topic or term is not, by itself, a request for implementation
details. Describe components by their roles before introducing internal names.

Make details earn their place.
Include a detail only if I explicitly asked for it, or omitting it would
prevent me from understanding the answer, making a decision, or taking
the requested action. Otherwise, leave it out.
Keep the causal links that make the explanation understandable; remove
side branches, not connective reasoning. Never achieve brevity by packing
more jargon into fewer sentences.

Use progressive disclosure across turns.
Lead with the answer, then add only the explanation needed at this level.
Stop when the current question is adequately answered. Leave deeper layers
for follow-up rather than appending them as extra sections.
When I follow up, expand only that part, using what we have already established.

Keep the response proportional.
Simple questions usually need one or two sentences. More involved questions
need a few short paragraphs or a short list. These are defaults, not quotas:
explicit requests for depth or complete deliverables take precedence.
Avoid unsolicited background, exhaustive alternatives, repeated summaries,
and routine offers to elaborate.

Separate doing the work from narrating it.
Complete the requested work and necessary verification. Report the outcome,
the evidence needed to trust it, and any material blocker or limitation.
Summarize routine tool activity instead of narrating each step.

Be precise about limits.
Briefly flag uncertainty or a condition that could change the answer or my
next action. Expand only when needed to avoid a misleading conclusion.

Example of depth:
“What is a cache?” → Explain reuse and its benefit.
“Why can it return stale data?” → Explain how the copy falls behind the source.
“How should I fix that here?” → Give the relevant implementation and tradeoff.

This repository is a personal technical archive. It contains long-lived notes, downstream-facing troubleshooting docs, generated analysis results, scripts, and local agent/tooling configuration.

## Directory Layout

| Path | Purpose |
|---|---|
| `notes/` | Long-lived learning notes, organized by technical area. Prefer durable concepts, design tradeoffs, and mental models over version-specific implementation trivia. See `notes/AGENTS.md` before editing. |
| `docs/` | Troubleshooting guides for downstream users. These should be operational: explain the mechanism, provide a decision map, and include concrete commands. See `docs/CLAUDE.md` for the expected structure. |
| `results/` | Output from focused investigations, reports, mind maps, and generated summaries. Use this for one-off or deliverable-style artifacts that are not yet canonical notes. |
| `scripts/` | Small helper scripts and sample logs used to parse, analyze, or demonstrate technical traces. Keep scripts narrow and document their expected input if non-obvious. |
| `logs/` | Raw or semi-raw logs kept as investigation material. Avoid mixing interpreted conclusions here; put conclusions in `notes/`, `docs/`, or `results/`. |
| `patch/` | Patch files or kernel change snippets preserved for reference. |
| `prompts/` | Reusable prompts and prompt fragments. |
| `neovim/` | Neovim configuration and related notes. Treat this as a separate config area, not part of the kernel notes. |
| `.claude/`, `.codex/`, `.agents/` | Local agent configuration and skills. Do not treat these as user-facing documentation. |

## Notes Subtree

`notes/` is the main knowledge base. Current topic folders include:

| Path | Topic |
|---|---|
| `notes/rcu/` | RCU internals: overview, QS reporting, trace events, API use, `rcu_sync`, and subsystem breakdowns. |
| `notes/mm/` | Memory-management topics such as boot memory init, OOM, IOMMU, Maple Tree, and page-fault diagrams. |
| `notes/interrupt/` | Interrupt architecture and ARM-specific interrupt/SDEI material. |
| `notes/os-boot/` | Boot and reboot flows. |
| `notes/computer-architecture/` | Computer architecture study notes. |
| `notes/gpu/` | GPU-related notes, currently AMD-focused. |
| `notes/general/` | Temporary or general notes. Clean up or promote durable material when it becomes stable. |
| `notes/known-concepts.md` | Index of concepts already understood by the user. Check this before writing explanations. |

When editing `notes/`, follow the local rule: explain What, Why, How, and So What as needed, but do not force every article into a rigid template.

## Where New Work Goes

- Put durable technical understanding in `notes/<topic>/`.
- Put downstream runbooks or issue triage guides in `docs/`.
- Put investigation output, CVE writeups, diagrams, and generated reports in `results/`.
- Put raw captured data in `logs/`.
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
