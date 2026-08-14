# UI DESIGN DOCTRINE — quant owner dashboard

Binding coaching for every ui/ change, distilled 2026-08-14 from a study
pass over professional finance/trading design sources (list at bottom).
Pairs with the product contract (Desktop/2026-08-14-quant-owner-ui.md
§§2, 4, 9, 10, 15). Contract wins on conflict; owner's word wins on both.
Before ANY chart work, additionally load the `dataviz` skill.

## A. The standard we hold

- The page answers the contract's four questions in five seconds: money
  safe? making/losing? what opportunity? what authority? A screen that
  cannot answer "what is happening / why / what should I do" in five
  seconds for a familiar user is broken — redesign it, don't decorate it.
- Stripe's home-screen discipline is the benchmark for restraint: a few
  opinionated numbers, chosen for decision-making, nothing competing.
  Comprehensive belongs behind drill-down, not on the first screen.
- Bloomberg's real superpower is temporal, not visual: near-instant
  response. Speed is a design feature we protect (poll cheap, render
  keyed, never remount).
- Trust comes from specificity, not polish: exact timestamps, exact
  amounts, declared incompleteness. Honest "Unavailable" beats any
  placeholder. (Already law; the research confirms it is also taste.)

## B. The never-list (generic-AI tells; each one is a defect)

1. Pale text carrying meaning. Meaning-carrying text never lighter than
   --muted (#667085) on white (≥4.5:1). Data values render in --ink.
   Gray-on-gray "clean" is the #1 generic tell.
2. Indigo/purple gradients, glassmorphism blur, decorative gradients of
   any kind. Our surfaces are flat white with hairlines + one soft
   elevation step.
3. The four-feature-card hero grid and any layout that exists to fill a
   box rather than answer a question.
4. Emoji as icons; mixed icon sets; hand-drawn or cartoonish controls.
   Buttons are machined: crisp 1px borders or solid fills, small radii
   (--r-sm/--r-md), weight 600 labels, no outer glows.
5. Color on everything. When every number is colored, color stops
   meaning anything. Data is monochrome; color is reserved for status.
6. Center-aligned table cells. Never.
7. Hyphen-minus faking a minus sign. Negative amounts use U+2212 (−).
8. Scattered motion, entrance choreography, hover theatrics. Motion
   confirms actions; it never entertains.
9. Apologetic or vague empty states ("No data yet 😔"). Empty states
   state the fact and, when one exists, the action.
10. Fake data, demo content, cosmetic controls. (Program law; repeated
    because it is also the loudest generic tell.)
11. Skeleton shimmer for data that may never arrive. Skeletons are only
    for known-inbound first paints; absent sources render the honest
    absent state immediately.

## C. Typography (the personality of the page)

- One UI face: Inter-class (--sans), already set. One numeric zone face:
  --mono for ids, hashes, SHAs, receipt paths. Two faces total.
- Every numeral everywhere: `font-variant-numeric: lining-nums
  tabular-nums` (body already sets "tnum"; add "lnum" with it). Digits
  must stack by place value in any column.
- Fixed decimals per unit, chosen once, kept everywhere: USDT amounts
  show trailing zeros (8.00, not 8); fees/P&L to the precision the
  backend recorded. The browser NEVER reformats or recalculates money —
  it renders backend strings; formatting rules live server-side.
- Type scale is a contract, six steps max. Working scale: 11px caps
  labels (+0.04em tracking, weight 600, --muted) / 12.5px dense-table
  cells / 13.5px body (current) / 15px section heads / 20px card-lead
  numbers / 28–32px the few hero numbers. Hero numbers: heaviest weight,
  tightest leading; the number is big, its label is small and quiet.
- Symbols: currency/percent tight to the value (8.00 USDT, 0.30%,
  −0.02); ranges with en-dash (8–32); real ellipsis (…).
- Small-value crypto quantities (0.00015 BTC): render full precision;
  optional refinement later — muted leading zeros, meaningful digits in
  ink — only with owner approval.

## D. Color law

- Existing grammar KEEPS and is confirmed by the research: green =
  positive recorded fact only; amber = uncertainty; red = failure /
  attention; gray = unavailable; blue = interaction/accent, never a
  truth claim. One accent. No second accent, ever.
- Monochrome data, colored status. A table of numbers is ink on white;
  color appears only where state demands it (verdict chips, lamps,
  deltas that are recorded facts).
- Never color alone (7% of men see red/green as the same): every
  status carries at least three of {color, shape, icon, label} (Carbon
  rule). Filled marks for high attention, outlined for low; don't mix
  filled and outlined for the same tier on one page.
- Contrast floors: data text ~16:1 (--ink on white); all
  meaning-carrying text ≥4.5:1; disabled/decorative may go lighter but
  then must carry no information.
- If a dark theme ever comes: every semantic color gets a second hex
  tuned for dark (same hex reads differently on near-black); four
  elevation steps, not two. Not built until the owner asks.

## E. Density and layout

- Density = value ÷ (time × space). We optimize value density: more
  answered questions per screen-second, not more pixels filled.
- The owner is a professional operator: default density is HIGH — dense
  clean tables, numbers close to labels, whitespace as structure never
  filler. 8px spacing grid; deviations are defects.
- Hierarchy: the most consequential fact of each view sits top-left,
  largest. Everything scales down from it. One view = one primary
  question (the tab names already encode them).
- Dense table rows: 32–36px. Default visible columns ≤8; more behind
  expand/drawer.
- Conditional density: summary bands get weight and (recorded-fact)
  color; repeated table cells stay muted-regular. If everything is
  emphasized, nothing is.
- Inspection never loses place: inline expand / right-side drawer over
  page navigation; the list stays mounted behind (KeyedTable's expand +
  rank-freeze already implement this — protect that property in every
  new piece).
- Sticky: table headers under the strip; totals row sticky-bottom where
  a rollup exists.

## F. Tables (the product's first-class citizen)

- Numbers right-aligned; text left-aligned; header follows its column's
  alignment. No center alignment anywhere.
- Separators: 1px --line-soft horizontals; verticals only when a scan
  genuinely needs them. Zebra only if row states (hover/selected/
  expanded) stay unambiguous — states outrank stripes.
- Sort affordance: small chevron, never shifting column alignment;
  default sort = most decision-relevant first (actionable-first order
  is already the volume-tab law; generalize it).
- Hover: quiet background shift (--panel-2), no elevation jumps.
- Every money column declares its unit in the header once — cells never
  repeat the unit the header already states (data-ink rule), except
  hero/summary numbers which are self-contained.

## G. Status lamps and chips

- One lamp component, one chip component, reused everywhere; severity
  tiers map to the contract's precedence (Attention/Unknown >
  Reconciling > Exiting > Entering > Holding > Evaluating > Watching >
  Offline).
- Shape per tier stays consistent app-wide (e.g., dot = nominal states,
  triangle = attention, square = sealed/stopped) — pick once, never
  re-invent per tab.
- A status indicator exists only where the owner could act or must
  know; decorative lamps are banned ("no action necessary → plain
  text", Carbon).

## H. Live updates and motion (temporal craft)

- Contract §10 binds: no flicker, no whole-table remounts, no scroll
  loss, no collapsing expanded rows, no reordering a row under
  inspection. Keyed updates only.
- Interactions under 100ms get NO animation — instant is the feature.
  Motion tokens: 120ms (state confirm, current --fast), 200ms
  (drawer/expand), 350ms (page-level) — nothing longer, one easing.
- Changed-value flash: when a polled number changes, pulse the cell
  background one step (soft token of the value's semantic — blue-soft
  for neutral facts) fading ≤500ms. Never animate layout/position.
- Freshness is a first-class fact: every live surface shows its age;
  amber/red age thresholds per source (strip already does this —
  generalize, never hide staleness behind a spinner).
- Waits >10s (rare, e.g. first mirror fill): determinate language
  ("waiting for first pull, last attempt 12s ago"), never an infinite
  spinner.
- `prefers-reduced-motion`: all pulses/transitions off, states swap
  instantly (style.css already honors it — keep it true in new code).

## I. Words (microcopy is design material)

- Contract §4 owner-language table is the vocabulary; product terms
  (Policy / Authority / Execution) never blur into a vague "Active".
- Error/refusal pattern (Stripe): what happened + the exact fact,
  in interface voice: "Feed stopped 4m ago; runtime runs it only when
  STOP is absent." No apology, no exclamation marks, no hedging.
- Empty pattern: fact, then action if one exists: "No jobs today. The
  runner posts here when one is queued." Never "no data yet :)".
- Active voice, sentence case, one job per label; a control names its
  exact effect and keeps that name through the whole flow.
- Timestamps: UTC, exact, tabular; relative ages beside them where the
  owner scans ("06:10:32Z · 47s ago").

## J. Charts and sparklines (when the owner asks)

- Load the `dataviz` skill BEFORE the first line of chart code.
- Sparklines: monochrome, direction-only, no axes/grids, beside the
  number they explain. Full charts: hairline grid at most, honest axes
  (money axes start at zero unless trend-reading demands otherwise —
  then say so), every chart timestamped, values labeled at point of
  need, no 3D, no decorative gradients under lines.
- Color in charts follows the same law: monochrome series; color only
  for recorded-fact semantics.

## K. Self-critique loop (before any DONE)

1. Screenshot at 1x in the real browser (hard refresh — static cache
   is 60s). Look at it the way the owner will: five-second test on the
   four questions.
2. Run the never-list (§B) as a checklist against the diff.
3. Chanel rule: remove one accessory — find the least necessary visual
   decision in the new piece and delete it.
4. Verify: keyboard focus visible, reduced-motion path, both data
   states (present + honestly-absent), no layout shift while polling.
5. node --check every touched JS file; `python3 -m unittest
   tests.test_ui_serve -q` green.

## Sources (studied 2026-08-14)

- UI Density (four densities, value density) — mattstromawn.com/writing/ui-density/
- Enterprise data tables patterns — pencilandpaper.io/articles/ux-pattern-analysis-enterprise-data-tables
- Stripe dashboard breakdown (restraint, monochrome data, empty states,
  microcopy) — 925studios.co/blog/stripe-dashboard-design-breakdown
- Fintech dashboard patterns, Role-Metric-Density-Action, density by
  audience (Ramp/Mercury/Brex/Stripe/Wise) — themasterly.com/blog/fintech-dashboard-design-guide
- Fintech typography: readable money (tnum/lnum, U+2212, trailing
  zeros, locale) — medium.com/design-bootcamp/the-elements-of-fintech-typography-part-1-readable-money-b6c1226acbde
- Tabular numbers in CSS — dev.to/alanwest/tabular-numbers-in-css-font-variant-numeric-vs-monospace-hacks-25cn
- Carbon status indicator pattern (3-of-4 elements, tiers, shapes) —
  carbondesignsystem.com/patterns/status-indicator-pattern/
- Carbon data-viz palettes — v10.carbondesignsystem.com/data-visualization/color-palettes/
- Data-dense dashboard spec (8px grid, 36px rows, motion tokens) —
  designmd.app/library/data-dense-dashboard
- 8 lessons from a trading journal dashboard (two type roles, elevation
  steps, drawer pattern, conditional density, motion caps) —
  pixel-show.com/blog/designing-data-dense-dashboards
- Trading app design guides (density by user class, latency as trust)
  — lollypop.design/blog/2026/june/trading-app-design/,
  medium.com/@david.pham_1649/trading-app-design-the-complete-guide-to-ui-ux-system-architecture-5b10664c5002
- Colorblind-safe finance UI (never color alone) —
  smashingmagazine.com/2024/02/designing-for-colorblindness/
- Why AI sites look the same (averaging, defaults, indigo plague) —
  shuffle.dev/blog/2026/01/why-do-most-ai-generated-websites-look-the-same/,
  dev.to/alanwest/why-every-ai-built-website-looks-the-same-blame-tailwinds-indigo-500-3h2p
- Tick-color regional conventions (JP red=up) — medium.com/@danvim/deep-dive-into-the-opposing-color-schemes-in-asian-vs-western-stock-market-prices-part-1-origin-4e3ccdb27c99
