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

# Repository Guide

This repository is a personal technical archive. It contains long-lived notes, downstream-facing troubleshooting docs, generated analysis results, scripts, and local agent/tooling configuration.

## Directory Layout

| Path | Purpose |
|---|---|
| `notes/` | Long-lived learning notes, organized by technical area. Prefer durable concepts, design tradeoffs, and mental models over version-specific implementation trivia. See `notes/AGENTS.md` before editing. |
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
| `notes/linux-kernel/` | Linux kernel notes, split by subsystem: `rcu/`, `mm/`, `interrupt/`, `os-boot/`, `gpu/`, `general/`. |
| `notes/linux-kernel/rcu/` | RCU internals: overview, QS reporting, trace events, API use, `rcu_sync`, and subsystem breakdowns. |
| `notes/linux-kernel/mm/` | Memory-management topics such as boot memory init, OOM, IOMMU, Maple Tree, and page-fault diagrams. |
| `notes/linux-kernel/interrupt/` | Interrupt architecture and ARM-specific interrupt/SDEI material. |
| `notes/linux-kernel/os-boot/` | Boot and reboot flows. |
| `notes/linux-kernel/gpu/` | GPU-related notes, currently AMD-focused. |
| `notes/linux-kernel/general/` | Temporary or general kernel notes. Clean up or promote durable material when it becomes stable. |
| `notes/ai_infra/` | AI infrastructure notes: `ascendc/`, `pytorch/`, `transformer/`, `vllm/`. |
| `notes/computer-architecture/` | Computer architecture study notes. |
| `notes/known-concepts.md` | Index of concepts already understood by the user. Check this before writing explanations. |

When editing `notes/`, follow the local rule: explain What, Why, How, and So What as needed, but do not force every article into a rigid template.

## Where New Work Goes

- Put durable technical understanding in `notes/<topic>/` (kernel topics under `notes/linux-kernel/<subsystem>/`).
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

## Explanation Style (Teaching / Learning Sessions)

When explaining complex technical concepts (kernel subsystems, allocator design, etc.) — in conversation or in notes. These are heuristics to apply with judgment, not a mandatory script; skip whatever does not fit the moment.

- **Open with the promise and the crux.** One sentence on what the user will be able to answer after this session, and the core problem the mechanism exists to solve — OSTEP-style: "The crux of the problem is ...". State the problem before explaining any mechanism.
- **Give the overview, then stop.** What the concept is → the key structures/components and each one's responsibility → why it exists and why it is shaped this way (the problem it solves, the design pressure behind it) → how they relate and form a system — and hold there. Do not open with struct fields or code-level detail, and do not expand into deeper layers on your own.
- **Go deeper only where the user asks.** Depth is user-driven: explain the layer they asked about, not the layer that seems next.
- **Don't tell, let them experience — with brakes.** When expanding, prefer leading with the question that opens onto the mechanism and let the user think first. But tell directly when: (a) the user asks you to just explain; (b) they have been stuck on the same point long enough that more struggle adds nothing; (c) what is missing is an underivable fact (a definition, an API, a hardware behavior) — hand over the fact, no suspense.
- **Code walkthroughs:** before reading through a function or flow, state What it does (its role in the bigger picture, inputs/outputs, responsibility boundary) and Why it exists / why it is shaped that way (which design decision it embodies); only then walk the How. Never open a walkthrough with line-by-line code.
- **No real-world metaphors.** Use direct technical language. Precise correspondences to concepts the user already knows (e.g. "SLUB's cpu_slab plays the same role as buddy's PCP") are encouraged. Do not invent casual translations for technical terms — use the original term (e.g. `seal`, `F_SEAL_WRITE`) and state plainly what the mechanism does.
- The user is transitioning to AI infrastructure work. When a topic has GPU / AI-Infra relevance (training, inference, NCCL, CUDA, PyTorch, /dev/shm, pinned memory, etc.), explicitly call out those connections. `notes/mm/overview.md` tags such modules with `gpu`.

## SVG Diagram Conventions

When hand-writing SVG diagrams (examples: `notes/mm/slub/slub-overview.svg`, `notes/mm/slub/slub-alloc-path.svg`):

- **Jumps use connector circles, never long arrows.** For goto-like flow: at the jump-away point, draw the arrow INTO a named circle; at the arrival point, place a same-named circle beside the flow with only an outgoing arrow merging into it. State the convention in a legend ("arrow into circle = jump away; arrow out of circle = arrive here").
- **Keep elements off container borders.** Boxes must not touch or coincide with lane/group rectangle edges — leave ~20px margin.
- **Lane labels go top-right, right-aligned**, so they never collide with the flow spine or decision diamonds.
- **Verify connectivity.** Gaps between flow segments happen easily (e.g. across lane boundaries) — check every transition actually has its arrow.
- **Always render and inspect before handing to the user**: `qlmanage -t -s 1600 -o /tmp <file>.svg`, view the PNG, zoom into dense regions to catch text overflow and overlaps, fix, then `open` the SVG. For coordinate-cascading changes a full rewrite is acceptable; use Edit for localized fixes.

## Operational Notes

- Use `rg` / `rg --files` for repository search.
- Before editing a subdirectory, check for local guidance files such as `AGENTS.md` or `CLAUDE.md`.
- Do not rewrite unrelated notes while making a focused change.
- The repository may contain local generated files or partial investigations; preserve unrelated work.
