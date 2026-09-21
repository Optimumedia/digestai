# AI at Work: section design

AI at Work is a publication inside Digest AI. The news desk is a dense, cool, blue-and-white
newsroom. AI at Work is a workbook: calm, practical and hands-on. It keeps enough of the brand that
readers always know they are on Digest AI, and changes the rest.

## What stays from the brand (about 10–15%)

- The whole site masthead (logo, tools, theme toggle, category menu), the ticker and the footer.
  They sit outside the section's scope and do not change.
- **Digest blue as a thread.** The brand accent (`#2442d6`, dark `#8397ff`) is used only for three
  things: the "Digest AI" kicker in the section band, links that lead back to the news desk ("← News
  desk", "What happened · sources" on a card), and the keyboard focus ring. In this section, blue
  means "this goes to the newsroom".
- **Source Sans 3** stays the body face, and **JetBrains Mono** is still used for dates and counts.
- The section's existing green (`--work-accent`) stays its accent, so the masthead's "AI at Work"
  link, the home block and the story-page card still match.

## Palette

The tokens are declared on `body[data-section="work"]`, which only the Work layout sets. They are
redefined for dark mode the same way `:root` is: in `prefers-color-scheme: dark` guarded by
`:root:not([data-theme="light"])`, and again under `:root[data-theme="dark"]`, so the theme toggle
keeps working. `.work-theme` then maps them onto the site's own tokens (`--bg`, `--surface`,
`--ink`, `--accent`…), so shared components inside the section (listen player, tables, lists) switch
with no second copy of their styles.

| Token | Role | Light | Dark |
|---|---|---|---|
| `--w-ground` | Page ground, "sage paper" | `#eef1eb` | `#101915` |
| `--w-surface` | Cards, tiles, "leaf white" | `#fbfcfa` | `#17221d` |
| `--w-inset` | Wells, table heads, "pale moss" | `#e2e8e0` | `#1f2d26` |
| `--w-ink` | Text, label tape, "pine ink" | `#16211b` | `#e4ece6` |
| `--w-ink-2` | Secondary text, "bark" | `#3d4b43` | `#b2c0b7` |
| `--w-ink-3` | Labels and meta, "lichen" | `#56655d` | `#8e9d95` |
| `--w-line` | Dividers, "fern line" | `#c9d3c8` | `#2d3c34` |
| `--w-rule` | Card and tile borders, "stem" | `#9fb0a4` | `#44574c` |
| `--w-accent` | Section accent, "field green" (= `--work-accent`) | `#0f6f5c` | `#5ed3b4` |
| `--w-accent-soft` | Free-cost chip, hover wash, "mint wash" | `#daf0ea` | `#10302a` |
| `--w-accent-ink` | Text on the accent | `#ffffff` | `#05231d` |
| `--w-marker` | Highlighter: wordmark underline, caution hatching | `#f3d23c` | `#e3c84a` |
| `--w-caution` | "Watch out" wash | `#fbf1c4` | `#2a2713` |
| `--w-caution-ink` | "Watch out" label | `#5c4a00` | `#ecd772` |
| `--w-thread` | Digest blue, the brand thread | `#2442d6` | `#8397ff` |

Checked for WCAG AA: every text colour is at least 4.7:1 on every surface it sits on (ink-3 on the
inset is the lowest at 4.9:1 light, 5.1:1 dark); text on the accent is 6.9:1 / 9.3:1.

## Type

| Role | Face | Use |
|---|---|---|
| Display | **Archivo SemiCondensed 700** (self-hosted, `/fonts/archivo-semicondensed-700-latin*.woff2`, 15 KB + 13 KB latin-ext, `font-display: swap`) | Wordmark, H1, section headings, card headlines, job tiles, label tape, week numerals |
| Body | Source Sans 3 (the site's) | Everything you read |
| Meta | JetBrains Mono (the site's) | Dates, counts |

One weight of one width keeps the added download to a single 15 KB file for Latin text. It is a
sturdy, slightly narrow grotesque: the feel of a field manual or a label maker, and nothing like the
newsroom's Newsreader serif.

## Layout concept: a workbook, not a news desk

- **Section band** under the site menu: graph-paper background, "Digest AI" kicker in blue, the
  "AI at Work" wordmark with a highlighter underline, the section tagline, the way back to the news
  desk, and index tabs (This week · Tool directory · Playbooks · Podcast · RSS) standing on a 2px ink
  rule, the current tab joined to the page like a notebook divider.
- **Headings a scanner can navigate by**: every section is an H2 in the display face with a small
  accent square, and a hairline under it instead of the newsroom's heavy rule.
- **Calmer rhythm**: 8px spacing base, 40–48px between sections, generous card padding, a 68ch
  reading measure for lists.
- **Every page ends with one "Next" link** (a big, full-width link with a short reason), not a grid
  of related links.

### The hub (/work)

1. Page head.
2. **This week's one thing to try**: a single featured card, shown expanded (the most useful item of
   the week, from the week's own try list).
3. **Find it by job**: five tiles, one per job page, each with its line and its count.
4. **What changed**: a compact, dense list, one line per item (tool, what you get, cost and time
   chips), with the job filter above it. The filter works on the rows, and says when a job has none.
5. The weekly podcast.
6. **Past weeks**, numbered.
7. Next: this week's playbook.

### Job pages, playbooks, tool directory

- Job page: head, this week's changes as dense rows, the job's cards (collapsed), its tools, then
  Next: the next job.
- Playbook (week): what changed (dense rows), what to try (numbered cards), what to leave (rows with
  the reason), prev/next week. A side-rail table of contents appears only on long playbooks (10+
  items) at desktop width.
- Tool directory: the table and one spec sheet per tool, ready to become per-tool pages (each sheet
  has its anchor and could link to `/work/tools/<slug>` later). Next: this week's playbook.

## Card anatomy (`WorkEntry.astro`)

Collapsed (job pages, playbooks):

1. Tool on **label tape** (ink strip, display face, caps) · maker · date.
2. **Outcome headline** (what it does), the strongest text on the page, in the display face.
3. **Chips**: cost (free/included in the accent wash, paid outlined, not stated dashed), time (a
   three-step effort scale: minutes, an afternoon, needs a developer), and who it is for.
4. **Watch out**: one calm line in a pale caution wash with a hatched highlighter edge. Never
   hidden, never truncated.
5. "Not for everyone" limits, when there are any.
6. **How to use it** (`<details>`), only when there is something behind it: "use it for" as a
   checklist, and the expanded blocks below.
7. Foot: **Try it ↗** (accent button), "What happened · sources" (blue, to the news desk), and the
   "leave for now" reason on skip cards (dashed border, muted).

Expanded (the featured card; the same blocks inside the details of a collapsed card):

- **Use it for**: a checklist with drawn boxes.
- **Steps** (`card.steps: string[]`): a numbered list with large numerals. *Slot: no data yet.*
- **Prompt** (`card.prompt: string`): a monospace block with a labelled **Copy prompt** button and
  a live "Copied" status. *Slot: no data yet.*
- **Before / after** (`card.example: { before, after }`): two panels side by side, stacked on a
  phone. *Slot: no data yet.*

Each block renders only when the card has the data. The field names are declared as optional on
`WorkCard` in `src/lib/work.ts`; the pipeline can fill them under those names, or the names can be
changed there and in `WorkEntry.astro`.

## Small visual language

- Label tape for tool names; highlighter underline for the wordmark; hatched caution edge for
  "watch out"; drawn checkboxes for "use it for"; the three-step effort scale; big numerals for
  steps and weeks; graph paper only in the band.
- No emoji, no gradients, no drop shadows, 2–3px radii, 1.5px borders.

## Scope and safety

- Everything is under `body[data-section="work"]` / `.work-theme`; only `layouts/Work.astro` sets
  them. The home page's AI at Work block only gets the display face on its heading and a thin accent
  rule. The story page's card is unchanged.
- Focus: a 3px blue ring on every link, button and summary. Motion: hover transitions only, off
  under `prefers-reduced-motion: reduce`.
- Phones 320–414px: tabs scroll sideways, rows and tiles stack, no horizontal page scroll.
