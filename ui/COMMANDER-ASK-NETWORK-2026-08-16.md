# COMMANDER ASK — Network tab (owner request, 2026-08-16)

Read first (same branch, `lane/ui-network-v5`):
- https://github.com/cosmosgeek0/quant/blob/lane/ui-network-v5/ui/NETWORK-TAB-BRIEF.md (rev 4 — the design brief; two Codex passes already folded in)
- https://github.com/cosmosgeek0/quant/blob/lane/ui-network-v5/ui/DASHBOARD.md (what runs where, deploy steps)
- https://github.com/cosmosgeek0/quant/blob/lane/ui-network-v5/ui/DESIGN.md (look and voice law)
- https://github.com/cosmosgeek0/quant/blob/lane/ui-network-v5/ui/net_v5_references.md (V5's earlier reference census)

## Context since your last audit (MEASURED 2026-08-16)
- arbitrage.cosmosgeek.org now serves Network V5 from the execution box.
  Services on the box: quant-ui (serve.py, 127.0.0.1:8080), quant-ui-mirror
  (5-second local receipt copy into ui/live), quant-ui-telemetry (recorder,
  15 s), quant-ui-news (fetcher, 120 s). Tunnel, Cloudflare Access, the
  machine's directories: untouched. Evidence: evidence/dashboard-deploy-2026-08-16.txt.
- The Desktop checkout is fast-forwarded to the same commits as the V5 lane —
  one UI source now (the "readable checkout still has the older page"
  discrepancy is closed).
- Nothing on the box measures the box ↔ Binance link yet (no REST timing, no
  rate-limit weight, no error counts, no websocket state reaching the
  dashboard). The feed's connection evidence exists in
  RECEIPT_DIR/watch/feed_state.json but no mirror copies it.

## Owner's words that bind the design
- One big story graph up front that tells the whole story and is clickable
  (drill to what went wrong and where).
- Coloured single charts per wire, not grey.
- No switch without a real endpoint. Nothing invented, nothing decorative.
- Heartbeats (job runner, supervisor, basis watch, basis supervisor) belong to
  the Machine tab, not Network.
- Tabs: Overview / Basis / Reverse / Volume / Machine / Journal / News / Network.
- Machine will carry the real controls (max fee, per-trade cap, transfer cap,
  STOP). Network shows whether the paths between systems work.

## Questions — decisions, not an essay; max 15 rows; MEASURED / INFERRED on each
1. The ten-wire list in the brief (§3): anything missing or wrong for THIS
   project (not for an enterprise console)?
2. Collectors on the money box: which are safe to run there (public GET
   probes to Binance ping/time, the feed_state mirror, cloudflared's local
   metrics) and which must not (signed probes, anything writing outside
   ui/live)?
3. The /api/network contract (nodes[], edges[], events[]; three truths;
   Unknown never green/zero): accept, or change what?
4. Host-side controls (news interval/pause, public-feed resync): the
   authenticated-controller design (impact text, confirmation, receipt, state
   re-read) versus the read-only law in AGENTS.md — allowed at all, and how?
5. Build order and card cuts under the size law (about 500 lines per card,
   one worker per file): name the first three cards exactly (files, scope).
6. What to skip for phase 1 (animation? playback? topology strip?) so the
   first landing shows real Binance wire truth within days, not weeks.

Reply as plain rows. The owner pastes your rows to the builder unchanged.
