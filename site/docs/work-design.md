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
2. **This week's one thing to try**: a single featured card, shown expanded: the pipeline's
   featured pick (`featuredId` in `work-briefing.json`: a real maker, an official link, a
   publisher's coverage); without one, the first card with a named maker (never a forum handle),
   from the week's try list in the pipeline's order, then today's picks (`featuredPick` in
   `src/lib/work.ts`).
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
2. **Outcome headline** (`card.headline`, what the reader gets), the strongest text on the page, in
   the display face, with what the tool does in a quieter line under it. When the pipeline had no
   outcome and wrote its stand-in (the tool plus what it does), the heading is what it does
   (`cardTitle` in `src/lib/work.ts`).
   **What you get** (`card.youGet`) sits between the headline and what the tool does: the practical
   payoff in one or two plain sentences written to "you", in a mint wash with a small field-green
   marker and a "What you get" label, 17px (18.5px on the featured card). Never folded. The pipeline
   writes the model's line only when its figures and names are in the article, and otherwise a line
   built from the card's own fields (`pipeline/digest/work.py`, `fallback_you_get`), so every card
   has one. When most of its words are the heading's, it is left out (`youGetLine`, 70% overlap),
   so nothing is read twice. Rows show it as a second, one-line, muted line under the headline (two
   lines on a phone); the teaser, the
   home band's pick and the story page's card show it under the headline too.
3. **Chips**: cost (free/included in the accent wash, paid outlined, not stated dashed), time (a
   three-step effort scale: minutes, an afternoon, needs a developer), "Already included in: <plan>"
   when `card.includedIn` names one, and who it is for.
4. **Watch out**: one calm line in a pale caution wash with a hatched highlighter edge. Never
   hidden, never truncated.
5. "Not for everyone" limits, when there are any.
6. **How to use it** (`<details>`), only when there is something behind it: "use it for" as a
   checklist, and the expanded blocks below.
7. Foot: **Try it ↗** (accent button), "What happened · sources" (blue, to the news desk), and the
   "leave for now" reason on skip cards (dashed border, muted).

Expanded (the featured card; the same blocks inside the details of a collapsed card):

- **Use it for**: a checklist with drawn boxes.
- **How to set it up** (`card.steps: string[]`): a numbered list with large numerals, labelled with
  where the steps came from (`card.stepsSource`): "From the article", or "From <maker>'s own page"
  when `pipeline/digest/howto.py` took them from the maker's own help page. The story page's card
  shows the same list, numbered in small accent circles.
- **Prompt** (`card.prompt: string`): a monospace block with a labelled **Copy prompt** button and
  a live "Copied" status (the layout's script copies; `public/app.js` only records `copy_prompt`).
- **Before / after** (`card.example: { before, after }`): two panels side by side, stacked on a
  phone.

Each block renders only when the card has the data: the pipeline writes them only when the article
gives them (`pipeline/digest/work.py`, `card_out`).

Measurement (`public/app.js`) keys on attributes, so markup changes must keep them: `data-work-try`
on every Try / Site / Open link, `data-work-copy` on the copy button, `data-work-howto` on the
"How to use it" `<details>`, `data-work-next` on `WorkNext` (its `track` prop) and the playbook and
next-week links, and `data-work-tool` on the card.

## Small visual language

- Label tape for tool names; highlighter underline for the wordmark; hatched caution edge for
  "watch out"; drawn checkboxes for "use it for"; the three-step effort scale; big numerals for
  steps and weeks; graph paper only in the band.
- No emoji, no gradients, no drop shadows, 2–3px radii, 1.5px borders.

## Scope and safety

- Everything is under `body[data-section="work"]` / `.work-theme`; only `layouts/Work.astro` sets
  the body attribute. The home page's two doors (below) carry the same tokens on their own
  `.wk-home.work-theme` element, redefined for dark mode in the same three blocks. The story page's
  card keeps the site's look, in the section's green (`.work-section`), with "What you get" and
  "How to set it up" added.

## On the home page

The news desk stays first; AI at Work gets two doors in its own materials, both server-rendered in
the flow (no layout shift), both hidden when `homeWork()` (`src/lib/work.ts`) finds neither a
featured pick nor three good cards from the last seven days. Their items are not repeated in the
day lists.

- **Teaser** (`WorkTeaser.astro`): "AI at Work · One thing to try this week", the tool on its tape,
  the outcome headline, cost and time chips, "See how →" to /work. First in the grid's HTML: on a
  desktop it heads the side rail beside the briefing, on a phone (900px and under) it sits above
  the briefing, without the tape. Seen before any scrolling on both.
- **Band** (`WorkHome.astro`), right after the briefing: a small graph-paper head with the
  wordmark and "All of AI at Work →"; the week's pick on a leaf-white card with an ink border
  (tape, maker, headline, what it does, chips, the catch, "See how →" with what is waiting on
  /work, and the prompt or "use it for" beside it); two more as dense rows (three without a pick);
  shortcuts to the job pages that have items.
- The masthead's "AI at Work" item carries the section's square and a pale wash of its green.
- Clicks are `next_click` events (`data-work-next`): `home-teaser`, `home-band`,
  `home-band-prompt`, `home-band-item`, `home-band-job`, `home-band-all`.
- Focus: a 3px blue ring on every link, button and summary. Motion: hover transitions only, off
  under `prefers-reduced-motion: reduce`.
- Phones 320–414px: tabs scroll sideways, rows and tiles stack, no horizontal page scroll.

## The simple card (plain words, September 2026)

For a busy owner who decides in seconds. The wording rules live in `pipeline/digest/plain.py`
(limits in `LIMITS`, one shared `JARGON` list with plain `SWAPS`, a Flesch-Kincaid check), applied to
every card by `work.plain_card` on the way in and on every export; `simplify.py` rewrites recent older
cards with a free model, grounded in the card and its summary. Where this differs from the sections
above, this wins.

- **Visible without a click:** the tool tape with maker and date; the headline (verb first, 45-70
  characters, no "AI" outside a product name); "What you get" (one sentence to "you", at most 120
  characters); at most three labels (cost: Free / Free to try / Included in [plan] / Paid: from $X/mo /
  Price not stated; time: 5 minutes / An afternoon / Needs a developer; "No tech skills" or "No card
  needed" only when the article said so); the catch on one line (at most 80 characters); ONE action
  (`card.action`: "Try it in Gmail", "Open Canva"); optionally the example line ("For example, a café
  could use it to ...", always "could") and "Start here:" with the first step.
- **Behind "How to set it up":** the other steps, the prompt, before and after, what to use it for,
  who cannot use it, and the sources. "Leave for now" is a muted note, never a second button.
- **List rows:** headline, cost and time, a Try link. Nothing else.
- A card without a rule-keeping headline or any use has `hub: false`: its story page shows it, the
  hub's lists and the featured slot do not. A "Needs a developer" card is never featured.
- `card_view` (public/app.js, `[data-work-card]`): once per card per page view when half of it is on
  screen, at most 10 a page. The admin line shows tries plus prompt copies per 100 /work views and
  the featured card's try rate (`work-briefing.json` `featuredTool`).
