# Wealth Manager primitive — design brief

A skill (SKILL.md) that turns any long-horizon agent with a CLI (Claude Code, an
open Claude bot, a Hermes-style agent) into a plain-spoken wealth manager for a
regular person with money and a view of the world. It is NOT an alpha engine.
It is diagnostic: it tells the user what they are actually buying (beta,
concentration, correlation, factor tilts), proposes a sensible holistic
portfolio, and shows it with charts.

## Non-negotiable principles

1. **Engines do arithmetic, agents do judgment.** Every number the agent quotes
   comes from `tools/wm.py`. The agent never hand-computes returns, betas,
   correlations, or weights.
2. **Simplest thing that works.** One skill file, one Python engine, one
   playbook. If a feature needs a second service, a database, or a framework,
   it is out of scope. Always ask: could this be simpler?
3. **Talk like a sharp friend, not a child-minder and not a sell-side note.**
   Short sentences. Name the risk, quantify it, offer the alternative. Example
   tone: "You want AI memory exposure. Fine. But MU and NVDA have moved
   together ~0.8 over the last 2 years, so buying both is one bet, not two."
4. **Holistic first.** Any thematic sleeve is judged against the whole picture:
   existing holdings, cash needs, horizon, home currency, risk tolerance.
5. **No look-ahead, no fake precision.** Stats are reported with their window.
   No target prices. No buy/sell labels. "This is beta-chasing" is a valid and
   useful verdict.
6. **Zero infrastructure.** `uv run tools/wm.py ...` must work on a fresh
   machine with only `uv` installed (PEP 723 inline deps). Free data only
   (yfinance for prices, Ken French library for factors). Local file cache.

## Layout

```
wealth-mgmgt/
  SKILL.md                 # the conversation protocol (what the agent does, in what order)
  DESIGN.md                # this file
  references/
    playbook.md            # the wealth-manager sensibility: interview questions, allocation
                           # heuristics, how to interpret every stat wm.py emits, tone rules
    themes.md              # small curated theme -> instruments map (ETFs + a few single names)
                           # so "I want AI" resolves to candidates without a search
  tools/
    wm.py                  # the single engine CLI
    test_wm.py             # offline tests on synthetic prices (no network)
  profiles/                # <name>.json — who the user is (written during the interview)
  portfolios/              # <slug>.json + <slug>.html — proposals and their reports
  .cache/                  # price and factor parquet cache (gitignored)
```

## The flow the skill encodes

0. **Load or create the profile** (`profiles/<name>.json`). Questions, asked
   conversationally and only the ones that matter: goals, horizon, home
   currency, cash you need in the next 12–24 months, existing holdings (tickers
   + rough weights or amounts), risk tolerance in plain words, views on the
   world / themes you want.
1. **Baseline**: if there are existing holdings, run `wm.py analyze` on them
   first and tell the user where they are actually exposed.
2. **Thematic sleeve**: user says "I want AI". Agent proposes candidates from
   `references/themes.md` plus its own knowledge, runs `wm.py analyze`, calls
   out correlation clusters and beta, proposes a diversified sleeve.
3. **Holistic proposal**: `wm.py build` with the sleeve plus the ballast
   (broad equity, bonds/T-bills in home currency, dividend/cash-flow assets)
   sized to the profile. Presents weights and the report (`wm.py report`).
4. **Iterate**: the user pushes back; agent re-runs. Every accepted proposal is
   saved to `portfolios/`.
5. **Optional deep dives**: per-name questions can be routed to other
   primitives (e.g. the ROIC framework) — the skill only points at them.

## Engine: `tools/wm.py`

Single file, argparse subcommands, JSON to stdout (so an agent can read it),
human-readable table with `--pretty`. Benchmark default `SPY`, overridable.

- `prices T1 T2 ... [--years 5]` — adjusted closes, cached, robust download
  (sequential, retry on yfinance cache lock, skip and report tickers that fail).
- `analyze T1 T2 ... [--weights ...] [--bench SPY] [--years 5]` —
  per asset: ann. return, ann. vol, max drawdown, beta and alpha vs bench
  (with R²), Sharpe (rf from FF RF); pairwise correlation matrix; if weights
  given, portfolio-level versions of the same plus the "concentration" view:
  effective number of bets (1 / sum w²) and PCA share of variance explained by
  the first component. Also a simple correlation clustering: groups with
  pairwise corr > 0.7 flagged as "one bet".
- `factors T1 T2 ... [--model 3|5]` — Fama-French 3/5 factor regression per
  asset (daily), loadings + t-stats + alpha annualized. Plain-English tilt
  summary is the agent's job, but include `tilts` field with |t|>2 loadings.
- `build T1 T2 ... --method equal|invvol|minvar|riskparity [--max-weight 0.25]
  [--sleeves '{"theme":["NVDA","MU"],"ballast":["VTI","BND"]}' --sleeve-weights '{"theme":0.3,"ballast":0.7}']`
  — weights (long-only, sum to 1, cap), then the `analyze` output for the
  resulting portfolio. Sleeve mode: weights within each sleeve by method,
  sleeves combined by given weights.
- `report portfolio.json [--out portfolios/x.html]` — a single self-contained
  Plotly HTML: allocation donut, cumulative growth of 100 vs bench, drawdown,
  rolling 1y beta, correlation heatmap, per-asset stats table. Clean, no
  dashboard bloat. Also `--png` optional is NOT required.
- `save portfolio.json` is not needed: `build --save portfolios/<slug>.json`
  writes the JSON that `report` reads.

Portfolio JSON shape (stable contract for the skill):
```json
{"name": "...", "created": "YYYY-MM-DD", "bench": "SPY", "years": 5,
 "weights": {"NVDA": 0.1, ...}, "sleeves": {...}, "stats": {...analyze output...}}
```

Missing data: report which tickers were dropped and why in a `warnings`
array; never silently fill. Short history: analysis window is the common
window; report its start date.

## Out of scope for v1

Brokerage connections, live rebalancing, tax, options, anything needing a
paid API, a web UI. The HTML report is the UI.

## Product stance (from the owner)

- We never say "this will make you money." Not once, not implied. We may look
  like an alpha tool; we are a diagnostic and an empowerment tool.
- Underperforming the S&P is an acceptable outcome when it is the user's
  informed choice: people have theses they want to see play out, cash needs,
  currencies, and lives. The job is to make the trade-off explicit, not to
  forbid it.
- Fees matter. The product is free; the instruments should be cheap. Prefer
  low expense-ratio ETFs; mention the fee whenever an instrument is proposed.
- No repo bloat. One skill, one engine, one playbook, one theme map. Resist
  adding files.

## Addendum: regimes (owner request)

Complex means finance 101 done well, not HFT. Add one subcommand:

- `regimes T1 T2 ... [--weights ...] [--bench SPY] [--years 10] [--window 126]`
  1. **Named regimes**: a fixed table in the engine of dated windows with a
     one-line label (at minimum: 2018 Q4 selloff; 2020 COVID crash Feb–Mar
     2020; 2020–21 zero-rate rally; 2022 hiking cycle Jan–Oct 2022; 2023–24
     AI/mega-cap rally; 2025–present). For each regime where data exists:
     portfolio (if weights) and per-asset total return, ann. vol, max
     drawdown, beta vs bench. Labels are plain English so the agent can say
     "in the 2022 hiking cycle this basket lost X% while SPY lost Y%."
  2. **Rolling correlation breaks**: rolling `--window`-day pairwise
     correlation; for each pair report current, min, max, and the date of the
     largest 63-day change ("when it broke"). Also rolling avg pairwise
     correlation of the whole basket as a series (for the report).
  3. **Holding-period table**: for the portfolio, trailing 1y/2y/3y/5y total
     return and max drawdown vs bench.
  Output JSON like everything else; `report` gains one chart: rolling average
  pairwise correlation with regime shading.

## Addendum: cards + Teenage Engineering aesthetic (owner request)

The chat itself must show interactive UI, not just link a report. So:

- **Plotly is gone.** Charts are hand-drawn SVG from Python (line, area,
  donut, heatmap, small bar) plus a few KB of vanilla JS for hover readouts.
  A full report is tens of KB, a card is a few KB. One renderer for both.
- **Cards.** Every analysis subcommand (`analyze`, `factors`, `regimes`,
  `build`) accepts `--card path.html` and writes a card of its result next to
  the JSON on stdout. A card is one screen: a verdict slot (empty string the
  engine leaves for the agent? No — cards carry numbers only; the verdict is
  chat text), 2–4 tiles, 1–2 charts, no table.
  - `analyze` card: tiles (worst fall vs bench, independent bets, moves with
    market, cost) + donut (if weights) + correlation heatmap.
  - `factors` card: tilt bars (|t|>2 loadings, plain labels) per asset.
  - `regimes` card: regime bars (portfolio vs bench per regime) + rolling
    avg-correlation line with regime shading.
  - `build` card: same as analyze plus growth-of-100 vs bench.
- **`--fragment`** on `--card` and `report`: emit only a `<div class="wm">…</div>`
  with scoped `<style>` and an inline `<script>` in an IIFE, no
  doctype/html/head/body, no ids that could collide (use data-attributes or
  scoped classes), colors defined as CSS custom properties on `.wm` with
  fallbacks so the block adopts a host's light/dark. This is what gets pasted
  into a chat widget (Claude Code desktop's `show_widget`, an Artifact, etc.).
  Without `--fragment`, wrap the same fragment in a minimal standalone page.
- **Aesthetic: Teenage Engineering, but clean.** Instrument-panel calm:
  off-white or graphite panels, hairline 1px rules, monospace/grotesk type
  (system: `"SF Mono", "JetBrains Mono", ui-monospace` for numbers and labels,
  a clean sans for sentences), small uppercase tracked labels, one orange
  accent (`#ff4f00`-ish) used sparingly for the "you" series and active
  states, muted greys for the benchmark, tiny round-cornered tiles that read
  like knobs/keys, numbers large and tabular. Flat. No gradients, no shadows,
  no emoji. Dark mode is the graphite variant, not inverted white.
- Regime shading is a subtle hatch or a 6% tint, labels vertical.
- Hover: a readout line at the top-right of each chart (date · value ·
  bench), like a device display, not a floating tooltip box.
- Keep the report as the composed set of the same cards plus a Details table.

## Addendum: the report as a spec sheet (design system v2)

The card-grid version is generic. Replace it with an editorial, single-column
spec sheet. Same data, same engine functions, same fragment contract.

**Point of view.** A portfolio report is a spec sheet for a machine the user
owns. Spec sheets have: a name, one line of what it is, a ruled table of key
figures, numbered sections, and diagrams with captions. No cards inside cards.
No donut. No decoration that doesn't carry a number.

**Type.** IBM Plex Sans (400/500/600) for text, IBM Plex Mono (400/500) for
every number, label, axis and legend. Load from Google Fonts with `display=swap`
and system fallbacks (`-apple-system, "Helvetica Neue"` / `ui-monospace,
Menlo`) so offline still reads correctly. Scale: masthead 34/40 semibold with
−0.02em tracking; lede 17/26 regular; section heads 15/22 medium; body
14/22; captions 12.5/18; mono labels 11/16 with 0.06em tracking, uppercase
ONLY in the key-figure keys and section numbers, nowhere else. Tabular
figures everywhere. True minus sign. Numbers never wrap.

**Grid.** One column, max 680px, 24px side gutter on phones. Vertical rhythm on
an 8px grid. Sections separated by a single 1px hairline, never a box. Charts
sit flush with the column, no border, no background panel. Key-figure strip is
a ruled table: rows separated by hairlines, icon | key | value | comparison.

**Color.** Paper `#f7f6f2`, ink `#141414`, muted `#6b6b6b`, hairline
`#d9d7d0`, accent (you) `#ff4f00`, benchmark `#8c8c8c` dashed, band tint 5%.
Sleeve families: theme = accent + `#ff8a4c`; core = `#1f1f1f`, `#5a5a5a`,
`#9a9a9a` (ink greys, not blues); cash = `#c9c6bd`. Heat ramp: one hue, ink
at 1.0 fading to paper at 0. Dark: paper `#111`, ink `#f2f0ea`, hairline
`#2a2a2a`, muted `#9a9a9a`, accent `#ff6a26`. Nothing else.

**Icons.** Lucide (MIT), inline SVG, 16px, 1.5px stroke, currentColor. Exactly
these, with exact meaning:
`trending-down` worst fall · `git-fork` independent bets · `activity` moves
with the market · `receipt` annual cost · `layers` what you own · `line-chart`
how it grew · `arrow-down-to-line` drawdown · `waves` moves with market chart
· `grid-2x2` correlation · `split` diversification over time · `list` details.
No emoji. No icon without a label next to it.

**Structure of the report** (fragment identical, minus masthead margins):

```
Masthead      name (or "Your portfolio")  ·  mono meta: Sep 2021 – Sep 2026 · vs S&P 500 · MXN
Lede          one factual sentence the engine can always write:
              "Six holdings that behave like two and a half. It moves at 0.71× the
              market and its worst fall was −23% against the S&P 500's −29%."
Key figures   ruled 4-row table with icons (see above), comparison in muted
01 What you own      allocation strip: one horizontal stacked bar, segments in
                     sleeve families, sleeve header row above with totals,
                     holding labels + % beneath; tap/hover highlights a segment
02 How it grew       growth-of-100, you (accent) vs bench (dashed grey), end
                     labels on the line, year ticks
03 Worst falls       drawdown area, annotate the single worst trough with a
                     small mono label "−23% · Oct 2022"
04 Moves with market rolling beta, dotted 1.0 reference labelled
05 Move together     heatmap, blank diagonal, cluster sentence as caption
06 When it broke     avg pairwise corr with regime strip
Details              disclosure → spec table
Footer              one mono line: "Historical. Not a forecast. Numbers from wm.py."
```

Section heads: `01` in mono muted, then the title in sans medium, then a
12.5px caption in muted. Readout (device display) stays, right-aligned on the
section head line, mono.

**Hover.** Crosshair hairline in ink at 40% opacity, readout updates. Touch
works. Nothing else animates.

**Cards** (`--card`) use the same system: masthead, lede, key figures, and
one or two sections. The `factors` card's bars use ink for positive, accent
for negative? No — ink for all, and the label carries the sign. The `regimes`
card: bars per regime you vs bench, bench in grey.

Kill list: rounded panels, drop shadows, gradients, donut, "hover a slice"
hints (the readout says "tap or hover" once, in the masthead meta), uppercase
outside keys/section numbers, colour used for anything but you/bench/sleeves.

## Addendum v2.1: typography and alignment (owner: "colors yes, type no")

Colours stay exactly as v2. Everything below replaces the v2 type rules.

**Faces.** Geist (sans) and Geist Mono, from Google Fonts, variable weights,
`display=swap`. Fallbacks: `Inter, -apple-system, "Helvetica Neue", sans-serif`
and `"JetBrains Mono", ui-monospace, Menlo, monospace`. Sans for words. Mono
only for numbers, tickers, axis ticks, dates, and section numbers. Nothing
else.

**Scale** (px/line-height, weight, tracking):
- Masthead 40/44, 600, −0.03em.
- Lede 18/28, 400, −0.01em, ink.
- Key-figure value 28/32 mono 500, −0.02em.
- Section title 16/24, 600, −0.01em. Section number 12 mono 400 muted.
- Body / captions 14/22, 400, muted.
- Labels (key-figure keys, sleeve names, legend) 12/16 sans 500, muted,
  **sentence case, no tracking, no uppercase anywhere on the page.**
- Axis ticks, readouts 11/16 mono 400 muted.
- All numerals tabular (`font-variant-numeric: tabular-nums`), true minus.

**The left edge.** One vertical line at the column's left edge that every
element touches: masthead, lede, key-figure icons, section numbers, captions,
the allocation strip, the heatmap's first column, the details table, the
footer, and — critically — every chart's plot area. Y-axis tick labels
therefore do not sit to the left of the plot: they sit inside the plot,
right-aligned against the right edge of the column, on top of the gridline
(the Stripe/Linear convention). The rolling-beta 1.0 reference label sits the
same way. X-axis ticks hang below the plot, left-aligned to their tick.

**Key figures.** A 3-column grid on that edge: `[icon+key] [value] [comparison]`
with fixed tracks `1fr 120px 1fr`; value right-aligned in its track, comparison
left-aligned in the last track. Rows 56px tall, hairline between. On phones,
two rows per figure: key+value on the first line, comparison beneath.

**Section head.** `[number] [icon] [title] ……… [readout]` on one 24px line;
number and icon in a 56px lead track so all titles align vertically; readout
right-aligned in mono. Caption beneath, indented to the title's left edge on
desktop, to the column edge on phones.

**Allocation strip.** Sleeve header row: sleeve name sans 12/500 muted +
total mono 12 ink, positioned at the start of each sleeve's span. Segments
24px tall with 2px paper gaps, square corners. Legend beneath as a wrapped
row: swatch 10px square, ticker mono 12 ink, share mono 12 muted.

**Whitespace.** Section spacing 56px desktop, 40px phone. Masthead to lede 20,
lede to key figures 32. Never less than 16px between a caption and a chart.

**Details table.** Header row sans 12/500 muted sentence case; cells mono 13;
right-aligned numerics; portfolio row first in ink 500, bench row muted.

Kill list additions: uppercase, letter-spacing > 0 anywhere, IBM Plex, y-axis
labels outside the plot, any element whose left edge is not the column edge or
the title indent.

## Addendum v2.2: no commentary

Every string on the page is a name, a number, a date, or a plain description
of what a chart contains. Removed: the "tap or hover" hint, the footer, every
explanatory caption ("Every dip is…", "When this line climbs…"), and "Count
them as one bet." Section titles are now nouns: What you own · Growth of 100 ·
Falls from peak · Moves with the market · Move together · Correlation over
time · Return by period. The chat carries the interpretation; the page carries
the facts.

## Addendum v3: bento cards (supersedes the v2 layout; keeps v2 colours, v2.1 type, v2.2 no-commentary)

The bare ruled sheet reads as a text dump. The page is a set of distinct
components, each a card with one job, on a grid that lines up to the pixel.

**Ground and cards.** Page ground = paper. Card = panel (`#fff` light,
`#1a1a1a` dark), 1px hairline border, 12px radius, 20px padding, no shadow, no
gradient. Cards never nest. A card has: header row → content → optional
footline. Nothing else.

**Grid.** 12 columns, max width 960px, 16px gutters, 24px page margin (16px
on phones). Desktop (≥ 900px) layout, top to bottom:

```
masthead      not a card: title 40/44 600, meta mono 11, lede 18/28   (cols 1–12)
key figures   4 cards × 3 cols: Worst fall · Separate bets · Moves with the market · Annual cost
what you own  1 card, 12 cols: allocation strip + sleeve headers + legend
growth        card, 7 cols: Growth of 100         │ falls    card, 5 cols: Falls from peak
market        card, 6 cols: Moves with the market │ over time card, 6 cols: Correlation over time
move together card, 7 cols: heatmap               │ details  card, 5 cols: spec table (open, scrollable inside)
```
Tablet (600–899px): key figures 2×2, every other card full width. Phone
(< 600px): everything full width, key figures 2×2.

Cards in the same row have equal height (grid `align-items: stretch`); chart
heights flex to fill so headers, plots and footlines line up across the row.

**Key-figure card.** Vertical stack, 20px padding: icon chip (28×28, radius
8, background = ink at 6%, icon 16px ink, centred to the pixel) → 12px gap →
label 12/16 500 muted → 4px → value 32/36 mono 500 ink, tabular, true minus →
8px → comparison 12/16 muted, max two lines, bottom-aligned across the row.

**Section card header.** One 24px row: icon (16px inside a 24×24 box, no
chip) → 10px → title 15/24 600 ink → flex → readout mono 11/24 muted, right
edge flush with the content edge. 16px between header and content. No section
numbers.

**Charts inside cards.** Plot area spans the content width exactly; y labels
inside on their gridline (v2.1); x ticks hang 8px below the plot; no plot
padding on the left. Hover readout writes into the header readout slot.

**Allocation card.** Sleeve header row (sleeve name 12/500 muted + total mono
12 ink) above the strip, strip 28px tall with 2px paper gaps, legend row of
tags below (swatch 10px, ticker mono 12 ink, share mono 12 muted). Readout:
largest holding.

**Heatmap card.** Grid fits the content width; row labels flush left inside the
card content; blank diagonal; footline = cluster sentence (descriptive only).

**Details card.** Header "Details" with the list icon, table always open,
`overflow-x: auto` inside the card only, sticky first column on phones.

**Icons.** The Lucide set from v2, verified against source; chips only on key
figures. Every icon box is 24×24 or a 28×28 chip, glyph ink centred to
(12,12) ± 0.2 units.

**Alignment contract (measured, ±0.5px, all three viewports):** card left and
right edges on grid columns; equal card heights per row; header icon centre =
title centre; readout baseline = title baseline; key-figure chips share y and
x across the row, values share a baseline, comparisons bottom-align; plot
left edge = card content left edge; legend/tag rows share a baseline.

Kill list stays from v2.2: no hints, no footer, no captions that explain,
no section numbers, no uppercase, no letter-spacing, no shadows.

## Addendum: ingestion (`ingest`)

Getting real holdings in is where the interview usually dies, so the file path
is now first-class. The boundary follows the house rule: structured files are
arithmetic, layout is judgment. CSV/XLSX and sectioned (IBKR-style) reports
parse deterministically; PDFs and screenshots are read by the agent — or
text-extracted by `ingest` for hosts that cannot read files — and re-submitted
as `--positions` JSON, which the same code path prices, converts and weighs.
Cash, options and unmapped names are reported separately, never folded into
`analysis_ready`. Nothing new outside `wm.py`; two lazy deps (openpyxl, pypdf)
that `uv` resolves.
