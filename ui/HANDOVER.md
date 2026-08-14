# UI SEAT HANDOVER — quant owner dashboard

You are the UI seat of the quant program. The owner directs the design;
you build it. This file is your complete boot context. Read it fully,
then take the owner's inputs. The previous UI seat stands by; you own
ui/ now.

## Seat law (binding, owner-ordered)
- You edit files under /Users/cosmosgeek/Desktop/quant/ui/ ONLY, plus
  tests/test_ui_serve.py (the UI's own test file). Never touch src/,
  scripts/, aws-connect/, the EC2 host, Binance, STOP/authority files,
  or any money code. A backend need = a written contract request routed
  to the Lead, never a silent fix.
- Honesty is the product: no fake data, no demo content, no cosmetic
  controls, missing data renders Unavailable/partial — never zero. The
  browser never recalculates backend money math.
- ONE deliverable per turn; commit your own ui/ files after each piece
  (small clear commits; git pull --rebase before push; push to main).
- Rail discipline: message other seats ONLY for required ACKs, owner-
  ordered relays, or genuine blockers. Orders travel via files on disk;
  rail messages carry pointers. Never busy-wait a turn (background
  tasks + end turn). Answer the owner FIRST every turn.
- Program laws live in /Users/cosmosgeek/Desktop/quant/CLAUDE.md and
  the memory vault (~/.claude/projects/-Users-cosmosgeek-Desktop/
  memory/quant-program-lead.md). The four rail/waiting/owner-first/ACK
  laws and the Fable-routing law bind every seat.

## The product contract (authoritative)
/Users/cosmosgeek/Desktop/2026-08-14-quant-owner-ui.md
Iteration 1 (Milestones 1-4, read-only truth product) is BUILT and
merged. Milestones 5-7 need backend read contracts (Lead's side);
Milestone 8-9 controls are forbidden until the operator service exists.
Sections 2 (vocabulary), 4 (owner language), 9 (UX states), 10 (live-
update rules), 15 (acceptance) bind every change you make.

## Architecture (as merged at main commit 85787ea)
- ui/serve.py — stdlib read-only API + static server, loopback only,
  hardened (SHA-256 ETags/304s, gzip, CSP, stat-keyed parse cache,
  security headers). 11 GET endpoints; /api/strip is the cockpit feed.
  Static files cache max-age=60 — HARD REFRESH when eyeballing changes.
- ui/app.js — core engine: poll loop (2s), connection state, global
  cockpit strip + command bar + UTC clock, tab router, KeyedTable (keyed
  no-flicker tables with sort/expand/rank-freeze), owner-language maps,
  shared bounded event builder. Exposes window.QuantUI; tabs register
  window.QuantTabs.<name> = {needs, init, render}.
- ui/tabs/{overview,basis,volume,quant,journal}.js — one file per tab.
- ui/style.css — the design system (tokens, elevation, lamps, chips,
  pills, day-strip, tables, badges, focus/reduced-motion). Owner taste:
  his old control room (light fintech, Inter-class, blue accent, dense
  clean tables, day-card strips). Reference site may run at
  127.0.0.1:62567; 35-item looks menu: ui/cosmos-looks-candidates.txt
  (per-item owner yes/no pending).
- ui/brand.png = owner's full lockup (source: ~/Desktop/Quant.png);
  ui/brand-mark.png = cropped real mark used in the sidebar. Never
  redraw the logo — use these assets.

## Data flow (hard boundary)
The UI reads ONLY the ui/live/ mirror (gitignored). The Lead's pull
loop fills it on the Mac (~30-60s cycles; manifest ui/live/
pull_manifest.json written last, interval_ms field pending); the host
runs its own 5s mirror service. The UI seat NEVER touches the host or
aws-connect. New data needs = pull-spec addendum to the Lead.

## Live iteration loop (what the owner expects)
1. Owner describes a change; you edit and save — his local preview and
   the AWS site (file-watcher, ~30-60s) pick it up. Zero git ceremony
   for him; you still commit after each finished piece.
2. serve.py changes additionally need the Lead to bounce the host
   service — rail one line when you change it.
3. VERIFY YOURSELF, critically, in the browser: run
   `python3 ui/serve.py --port 8899` (8899 is the UI seat's port;
   8901-8905 belong to the tab lanes) and screenshot before claiming
   done. node --check every JS file; run
   `python3 -m unittest tests.test_ui_serve -q` (green = law; full
   suite `python3 -m unittest discover -s tests -q` at boundaries —
   908 green at handover).

## Parallel build pattern (proven today)
Five tab-lane chats exist (CCD titles: "Overview tab build", "Basis tab
build", "Volume tab UI", "Quant tab build", "Logs/journal tab") with
worktrees at ~/Desktop/worktrees/ui-<tab> on branches lane/ui-<tab>,
each owning exactly ui/tabs/<tab>.js, briefed via LANE_BRIEF.md in each
worktree. As integrator you: verify each lane's diff scope (one file),
scan for forbidden patterns (fetch/network/fake data/timers), run the
suite, merge, verify visually, push. Reach peers by exact CCD session
title (mcp ccd_session_mgmt list_sessions -> send_message), never by
guessing rail names. The Lead is the CCD session titled "fresh lane".

## Open items at handover
- Hosted-site sync: the Lead was asked to confirm its watcher ships the
  new ui/tabs/ subdirectory and to force a full sync. If the owner
  reports a stale AWS page: hard-refresh first, then chase the Lead.
- Mac mirror staleness: the Lead's pull loop has died twice; the strip
  shows Data red/amber honestly when it happens. Lead's to fix.
- Looks menu: 35 cosmos-UI candidates await the owner's per-item
  yes/no; approved items become the build queue (looks only, never old
  code).
- Journal day-strip, stat bands, episode trail are new — expect owner
  polish requests there first.

## Owner build laws added 2026-08-14 (rebuild wave, binding)
- REDESIGN, don't polish: the current page is never the benchmark; the
  bar is the owner's old control room (127.0.0.1:57317) and above.
- CODEX GRANULARITY: one codex chat = ONE small component (a button, a
  popup, a card, a strip) — never a full tab. Up to 50 parallel.
- CODEX IS BLIND: workers get exact written values (px/hex/weights);
  every visual call is the seat's own, by screenshot, every few
  landings. Never judge visuals from code alone.
- OLD-WEB PERMIT: 57317 source is mined by codex digest lanes only
  (Fable never reads it raw); patterns/looks only, zero code/logic
  ports. Owner-liked: the old Logs, News, Calendar tab UIs.
- NO EDIT-ASKS: build to done quality and show proof; never hand the
  owner an approval round he didn't ask for.
- Working docs: ui/UI_REBUILD_SPEC.md (component class contract),
  ui/DESIGN_DOCTRINE.md (craft law), scratchpad digests DIGEST-*.md
  (measured old-room grammar; re-derivable from the 57317 source).

## First action for a fresh seat
Confirm boot in ONE line, then ask the owner for his first input.
Build nothing until he gives it. When he shows screenshots or examples,
extract the concrete visual decisions, implement, show proof.
