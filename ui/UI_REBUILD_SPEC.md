# UI REBUILD SPEC v2 — the owner room, product grade
2026-08-14, UI seat. Binding for every rebuild lane this wave.
Pairs with: DESIGN_DOCTRINE.md (craft law) + the product contract
(Desktop/quant-owner-ui.md — §2 vocabulary, §4 strip, §5 tabs, §9 UX
states, §10 live rules, §15 acceptance). Contract wins on conflict.

## Mission
REDESIGN, not polish (owner order 2026-08-14): the current 8899 page
is the wireframe to replace, never the benchmark. Target: past the
craft level of the owner's old control room (127.0.0.1:57317, source
at Desktop/arbitrage-cosmos-old-preview.KxiJsw/ui-peak-jul10-75c55ec2/
web, commit 75c55ec2) on REAL current data only. No feature invention,
no fake fields, no old-project code — patterns and looks only.
OWNER-LIKED (explicit, 2026-08-14): the old Logs, News and Calendar
tab UIs. Their patterns (day strip, curated feed rows, calendar-grade
date navigation, filter pills with counts) are permitted style sources
— folded into Journal/Quant where real data supports them. News and
Calendar do NOT become tabs; five tabs stay law.

## The four signature patterns (adopted from the owner's references)
1. PROVENANCE CELLS — every table value is two lines: primary value in
   ink, then a tiny muted sub-line saying where it comes from or what
   it means ("Spot ask · Perp bid", "35m · carry +0.04%", "entered
   09:45:03Z"). No bare numbers in tables.
2. STATUS-AS-VALUE — a state can be a stat card's value ("Paused" in
   green when it means safety), with the mechanism in the sub-line.
3. HONEST-CALM ABSENCE — missing data renders as a quiet "—" value
   with a muted sub-line explaining WHY ("No account contract yet"),
   inside the same designed card. Absence is never mono-red, never
   shouted, never zero. One page-level note may summarize gaps.
4. EXACT-REASON DECISIONS — every refusal/decision shows its bold
   reason + muted exact numbers ("gap 0.123% below watch 0.15%").

## Class contract (style.css owns these; lanes MUST use them)
- Shell: .shell / .side / .side-brand / .side-sec (eyebrow) /
  .side-item (+.active) / .main / .topstrip / .page / .page-head
- Cards: .card / .card-head (title + right meta) / .card-body
- Stat cards: .stat > .stat-l (label) + .stat-v (value) + .stat-s
  (sub). Variants: .stat.absent (value "—"), .stat.good/.warn/.bad
  (value tone). Grid: .stat-row (auto-fit minmax 180px).
- Tables: .tbl (keyed tables keep existing KeyedTable API) /
  th.num + td.num (right-aligned tabular) / .cell2 > .c2p + .c2s
  (provenance cell) / .sym (symbol identity cell: mark + name + sub)
- Chips: .chip + tone classes .ok .warn .bad .info .off — dot built
  in via ::before; never color-only (text always present).
- Pills: .pillbar > .pill (+.active, + .pill-n count badge)
- Feed: .feed > .feed-row > .feed-t (UTC time) + .feed-k (bold kind)
  + .feed-d (muted detail) + optional .chip
- Empty states: .empty > .empty-t (title) + .empty-d (explanation).
- Icons: <span class="ic" style="--ic:url(/vendor/icons/NAME.svg)">
  </span> — CSS mask, tints via currentColor. Never emoji.
- Numbers: money/qty always tabular; negatives use U+2212; units in
  .unit (10px muted caps, tight to value).

## Shell (I own: index.html, style.css, app.js core)
- Sidebar 216px: brand lockup (brand-mark.png + QUANT / OWNER ROOM),
  sections OVERVIEW(Overview) ENGINE(Basis, Volume) CONTROL(Quant)
  RECORDS(Journal), lucide icons, active = soft blue-tint fill.
- Topstrip (single render, the ONLY global strip): Execution ·
  Authority · Equity · Net today · Exposure · Risk · Data · UTC clock.
  Authority NEVER shows machine ARMED: no operator contract endpoint
  exists → "Authority: Unavailable" with sub "no operator contract".
  The word ARMED must not appear anywhere (contract acceptance #26).
  Execution derives from position/momentum/failures/feed per §2
  precedence; STOP flags map to the §4 owner-language table.
- Page header per tab: icon + one-word title + freshness meta.

## Per-tab spec (lanes own exactly ui/tabs/<tab>.js)
Data: window.QuantUI.data only (existing poll endpoints). NO new
fetches, NO timers, NO invented fields. Every § reference = contract.

### overview.js — the money page (§5.1)
Row 1 .stat-row: Net today (absent: "—" + "ledger incomplete") ·
Exposure (§5.1) · Capital (absent + why) · Risk usage (real governor
numbers) · Data freshness. Row 2: two .card halves: Realized records
Basis / Volume (PARTIAL labels, exact §5.1 rules, provenance cells).
Row 3: Recent activity feed (last 5 meaningful, owner language, no
"time unrecorded" plumbing words — absent time renders "—" with sub
"unrecorded"). Empty overall state: §5.1 exact sentence.

### basis.js (§5.2 iteration-1 truth only)
Stat band: Eligible pairs (from stream basis_gap rows) · Valid
proposals (candidates) · Open position (position) — absent-calm.
Pillbar: All · Proposals · Open · Refused(only if provable).
Table columns: Asset(.sym) | Spot(.cell2 ask · provenance) |
Perp(.cell2 bid) | Gap(.cell2 signed% · "entry executable") |
Edge(only where backend gives it; else "Not evaluated" .c2s) |
Funding(.cell2 rate · period/carry) | Planned(per-leg note) |
Phase(chip). Sort: Edge desc where present, else Gap. Expanded row:
cost waterfall from candidate fields (§5.2 list), "History
collecting" for charts. NO client math beyond formatting.

### volume.js (§5.3 iteration-1: Interesting only)
Keep 9-col contract already merged; restyle to .cell2/.sym/chips;
Route column: "Futures long"/"Observe only" with .c2s explaining
venue truth; decision chain in expanded row from /api/decisions
window with "partial source" label where absent.

### quant.js (§5.4 left trail only + §5.4 right = read-only policy)
Left: state header chip + curated feed from provable events
(decisions/failures/position/momentum) labeled "Recent — partial";
one heartbeat line per healthy pass, never per-symbol spam.
Right: read-only policy facts card (1× locked, caps, ceilings §5.4
Risk display list) — NO controls, NOT EVEN disabled buttons; a
single note: "Controls arrive with the operator service."

### journal.js (§5.5 iteration-1)
Day-strip (7 day cards, active day ring, per-severity mini-counts
where derivable) + severity pillbar + table Time|Severity|Strategy|
Symbol|Event(.cell2 bold kind + muted exact reason)|Outcome(chip).
Expanded: §5.5 fields that exist today; evidence collapsed.

## LIVING UI (owner order 2026-08-14: "not basic static UI slap")
The product must feel ALIVE — a machine you can watch working — while
staying honest. From ask.txt: the trader trail behaves like a coding
agent's activity view — a pinned NOW line showing what the machine is
doing this second, and finished items dropping down into history.
- Signature piece: the LIVE TRAIL. Pinned now-card with a breathing
  state lamp; when a new event arrives on poll, it slides in at the
  top (200ms ease-out) and the previous now-line settles into the
  trail. No fabricated activity: the now-line shows the real current
  phase and the real newest record; a quiet machine shows its calm
  heartbeat ("Watching — last pass 12s ago"), never invented motion.
- Breathing lamps: Watching = slow 3s ease-in-out opacity breath on
  the lamp glow; Attention = 1.2s urgent pulse; Paused/sealed =
  steady, no motion. Lamp only — never whole cards.
- Flash-on-change: every polled numeric cell pulses its background
  one soft token step, fading <=500ms, when its VALUE changes.
- HONEST-MOTION LAW: motion signals arrival, change, or state —
  never drama. Money digits never tween/count-up (a number is a fact:
  flash the cell, never animate the digits). Nothing loops except
  lamps. prefers-reduced-motion disables everything, states swap
  instantly.
- Expands, drawers, pills, day cards: 160-200ms ease-out, the one
  easing family, as already specified.

## Hard rules for every lane
- node --check passes; zero console errors; no innerHTML from
  unsanitized strings (use the existing esc() helper).
- Keyed updates only — never rebuild a table on poll (§10).
- Every §9 state implemented: loading / empty / unavailable / stale
  (age chip amber>45s red>120s from mirror pull) / disconnected.
- No color-alone meaning; chips always carry text; direction gets
  arrow glyphs (arrow-up-right/arrow-down-right icons or ▲▼).
- Absence: "—" + why. Zero is a measured number, never a placeholder.
- Language: contract §4 owner-language mappings verbatim where they
  apply. No paths, PIDs, SHAs, cron, raw JSON in default view.
- Report back: files touched, what changed visually (3 lines max),
  any contract line you could not satisfy and why.
