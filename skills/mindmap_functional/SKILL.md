---
name: mindmap-functional
description: >
  Generate an interactive functional mind map as a self-contained HTML file. Use this skill
  whenever the user wants to visualize a technical concept, system, architecture, or knowledge
  domain as a clickable node diagram. Triggers: "功能脑图", "mind map", "思维导图",
  "mindmap of X", "visualize X as a mindmap", "make a diagram for X", or any request to
  decompose a topic into a hierarchical interactive visual. Always use this skill even for
  casual phrasing — it produces a polished downloadable HTML file, not a plain text outline.
---

# Functional Mind Map Skill

Produce a **single self-contained `.html` file** — interactive, dark terminal aesthetic,
no external dependencies.

## Workflow

1. **Decompose first, template second.** Before opening `template.html`, list the module's
   top-level functional responsibilities in plain prose (just for yourself — 1 line each).
   Settle the count and the names *before* seeing the template's example shape. This is
   the single most important step for avoiding template-anchored output.
2. Now read `assets/template.html` to understand the scaffold and CSS classes. The example
   inside it is a sampler of possible shapes, **not a target** — see the next section.
3. Build the diagram by adding/removing `<div class="mm-col">` blocks to match the
   responsibility list from step 1. Any number of columns works; the row scrolls
   horizontally.
4. Fill every placeholder with real content. Delete any example slot that has no real
   content under it (don't invent filler).
5. Write output to `/mnt/user-data/outputs/<topic>-mindmap.html` and call `present_files`.

## What goes in the mind map

The mind map shows **what the module does** — its functional responsibilities and the
sub-behaviours that implement them. Stay inside the module.

**Do NOT include as nodes:**
- External interfaces, public APIs, syscalls, ioctls, or sysfs/procfs entry points the
  module exposes. These are seams to the outside world, not functions of the module itself.
- Integration points with other subsystems framed as "talks to X" — only include if the
  *module's own work* genuinely is the integration (e.g. a bridge driver whose job is
  translation).
- File names, source layout, or build-system facts.

If the user explicitly asks for an "interface map" or "API surface", that's a different
deliverable — surface it back to the user rather than mixing it in.

## Node Structure

- **Root** — 1 node: topic name + one-line subtitle.
- **L1** — N nodes, where N is the number of distinct top-level functional responsibilities
  the module actually has. Typically 2–6. Don't pad to hit a number; don't compress two real
  responsibilities into one to save space. If the module genuinely has 7+, that's fine —
  the layout scrolls.
- **L2** — under each L1, the sub-behaviours that make up that responsibility. Count is
  per-L1 and varies; some L1s may have 2, others 5. Put what's actually there.
- **L3** — concrete details, leaf cases, or notable mechanisms under an L2. Optional —
  only add an L3 row when the L2 genuinely decomposes further. An L1 with no L3 row is fine.
- **Detail panel** — every node needs a `title`, `sub`, `body` entry in the `DETAIL` JS
  object, keyed by the same string used in `onclick="showDetail('...')"`.

## The template is a scaffold, not a shape to match

The example in `template.html` shows four columns with varied L2/L3 row counts. That
example exists **only to demonstrate that the layout is flexible** — it is not a target.
Specifically:

- **Don't copy the column count.** If the module has 2 real responsibilities, the diagram
  has 2 columns. If it has 7, it has 7. The example having 4 means nothing.
- **Don't copy the L2/L3 shape.** The example's "3 L2 + 2 L3 / 2 L2 + 1 wide / 4 L2, no L3 /
  1 L2 alone" is a sampler of *possible* shapes. Each column's shape comes from the
  substance of that L1, not from matching one of the examples.
- **Don't carry over the example's slots.** If the template shows a slot for an amber
  pitfall node and the module has no pitfall worth flagging, **delete the slot**. Don't
  invent content to fill it. Likewise for any L3 row that has no real content under it.
- **Don't be primed by the example's vocabulary.** The placeholder text ("入队", "完成处理",
  "ring 越界", etc., if present in any prior version) is filler — it must not influence
  what nodes appear in the actual output.

A good check: imagine the template had a *different* example (say, 2 columns with 2 L2
each). Would your output be the same? If yes, you're letting the module drive the shape.
If no — if you'd produce a different decomposition for the same module — you're being
anchored on the example, and you should redo the decomposition from scratch by listing
the module's actual responsibilities first, then drawing columns to match.

## Layout

L1 columns live in a horizontal flex row (`.mm-cols`). The container scrolls horizontally
when there are more columns than fit the viewport — this is intentional, do not wrap or
shrink columns to force fit. Each column has a fixed minimum width set in the template's
CSS so labels stay readable.

Within a column, L2 and L3 children stack vertically below their L1, each sibling group in
its own row. A row with multiple siblings uses `display: grid` with
`grid-template-columns: repeat(N, 1fr)` where N is the sibling count. The template provides
helper classes `cols1` through `cols4`; for N≥5, set the columns inline:
`style="grid-template-columns: repeat(5, 1fr)"`.

## Colour Rules

| Class    | Use                                                                  |
|----------|----------------------------------------------------------------------|
| `purple` | Root node only.                                                      |
| `teal`   | Default for all functional nodes (L1 / L2 / L3).                     |
| `amber`  | A functional node whose job *is* an error/degraded path — e.g. fault recovery, fallback, rate-limit drop, watchdog reset. Use sparingly, and only when it's a real responsibility of the module. |

**`amber` is not for known bugs, observed issues, or "things to watch out for".** Those
are not module functions and don't belong on this diagram. If you find yourself reaching
for amber to record a bug you remember, stop — the node should not exist at all.

The previous `coral` (interface) class is retired — interfaces don't belong in this
diagram. CSS for `coral` may still exist in the template for backward compatibility, but
new mind maps should not use it.

## Detail Body

`body` accepts HTML. Use `<br><br>` for paragraphs, `<code>x</code>` for inline code,
`<b>x</b>` for emphasis. Keep each body under ~250 words.

## Checklist Before Saving

- [ ] **Anchoring check**: did I list the module's responsibilities *before* opening the
      template, or did the template's example shape my decomposition? If the latter, redo.
- [ ] L1 count matches the module's real top-level responsibilities — not padded, not compressed,
      not "4 because the example had 4".
- [ ] No node represents an external API, ioctl, sysfs entry, or "interface to X".
- [ ] No node represents a known bug or observed issue.
- [ ] Each L1 column has only as many L2/L3 children as the substance warrants — empty
      example slots have been deleted, not filled with invented content.
- [ ] Every `onclick="showDetail('key')"` has a matching key in `DETAIL`, and there are
      no orphan DETAIL keys left over from deleted nodes.
- [ ] No `coral` class on any new node; `amber` (if used) is for genuine error-path
      *functions*, not for pitfalls.
- [ ] Root node has real topic name and subtitle; tagline at bottom reflects the topic.
- [ ] No unclosed HTML tags, no JS syntax errors.

