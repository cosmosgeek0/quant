# ASK LEDGER — every owner ask from ask.txt, tracked
2026-08-14, UI seat. Nothing here is skimmed; each row is an ask in the
owner's own words (condensed), its state, and where it lives. States:
BUILT (verified on screen) · IN-BUILD · QUEUED (this rebuild) ·
CONTRACT (needs a Lead read/operator contract first — commander file
section noted) · DEFERRED (owner said later).

## Basis tab
| Ask | State | Where |
|---|---|---|
| Both-venue token list, limited universe | QUEUED (assembly next) | spec §basis; stream basis_gap rows |
| Sortable: gap/price/name/number | QUEUED | KeyedTable sort, spec §basis |
| Click row → expands in-tab | QUEUED | c9 tape + expand grammar |
| On-demand gap graph 1H/…/day, spot vs futures lines overlaid | CONTRACT — history endpoint (§7.3); until then "History collecting", never fabricated | commander §5.2 |
| Funding cost shown ("how much Binance takes") | QUEUED (rate+carry fields exist on candidates) | spec §basis |
| Don't render charts eagerly — on-demand pulls | LAW (spec: charts load after expand) | spec §J, contract §10 |

## Volume tab
| Ask | State | Where |
|---|---|---|
| Spot-only / futures-only / both distinction + tradability | BUILT in current tab, restyle QUEUED | volume.js Route column |
| Interesting movers, not 600-row dump | BUILT (top_surge only) | volume.js |

## Quant (trader) tab — one-word name ✓
| Ask | State | Where |
|---|---|---|
| Live "what is it doing" trail, Claude-style thinking→drop→next | BUILT (live trail, breathing lamp, arrival motion) | tabs/quant.js + m1/m2 |
| On-demand detail pulls, not a giant log pile | BUILT (bounded events, expand-for-facts) | quant.js |
| Left activity / right controls layout | BUILT (65/35) | quant.js |
| Controls: variables, dropdowns + custom values, approve-then-locked | CONTRACT — operator service (§8, milestones 8–9); until then real policy shown read-only, zero dead buttons | quant.js policy card |
| Halt/pause button | CONTRACT — same (§8: pause never blocks exits) | commander §8 |
| Scan cadence shown ("how fast it looks") | QUEUED — measured host ticks only, never config-as-truth | contract §5.4 |
| No abilities invented beyond current machine | LAW everywhere | spec, doctrine |

## Overview (dashboard)
| Ask | State | Where |
|---|---|---|
| THE money number, green profit/red loss, on top | BUILT as honest "—" until ledger exists (contract: never a fake zero); wired to go live with §7.1 | overview.js |
| Day-by-day P&L graph | CONTRACT — normalized ledger (§7.1/M6) | commander §5.1 |
| Values owner cares about; red flags at a glance | BUILT (stat row + tones) — being reframed into route-frame | overview.js |
| Fees spent visible | CONTRACT — /api/fees is rates, not spend (§5.1 law) | commander |

## Journal (logs)
| Ask | State | Where |
|---|---|---|
| Curated interesting events, not everything | BUILT (bounded curated list) | journal.js |
| Expandable per-log detail | BUILT (facts + technical drawer) | journal.js |
| Classify good/bad/neutral | BUILT (tones + severity counts) | journal.js |
| On-demand deeper pulls | CONTRACT — journal read model (§7.7) | commander |

## Shell & cosmetics (owner adds, 2026-08-14 latest)
| Ask | State | Where |
|---|---|---|
| Global search top-right (⌘K) | IN-BUILD (S1 palette + S2 field lanes) | measured anatomy in DIGEST-SHELL |
| Settings, left-bottom, all cosmetic features 57317 had | IN-BUILD (S3: light/dark, 7 accents, zoom, timezone display, motion) — browser-side prefs only, never machine controls | DIGEST-SHELL runtime accents |
| Clock done properly | IN-BUILD (S4: 130×42 chip, mono tabular, zone dot) | DIGEST-SHELL |
| One framed surface per page — no floating gray boxes | IN-BUILD (c8 frame adoption, route-frame grammar) | DIGEST-SHELL §5 |
| Animation everywhere it means something | LAW + BUILT (LIVING UI: trail, lamps, flash) — extending each wave | spec LIVING UI |

## General laws from ask.txt (all standing)
No fake features/cards/data · real capabilities only · plumbing hidden
(no ms/SHA/paths in default view) · abstraction for owner, drill-down
for evidence · account/borrow tab DEFERRED by owner's word · old-site
looks permitted, old code banned.
