# NETWORK V1 — FROZEN CONTRACT (integrator, 2026-08-16 ~16:50Z)

Every worker builds against THIS file. Facts marked MEASURED were read on the
box on 2026-08-16 16:30–16:35Z. Where this file and ui/NETWORK-V1-BUILD-ORDER.md
differ, the order wins; tell the integrator instead of guessing.

## 0. Ground rules for every module
- Python: stdlib only. Runs on the box under Python 3.12 AND locally under
  Python 3.9 — no `match`, no `X | Y` unions or builtin generics at runtime
  (put `from __future__ import annotations` at the top), no `zoneinfo`-only
  paths. `python3 -m py_compile` your file. Tests: `unittest`, deterministic,
  fixtures under tests/fixtures/network/ (never production data). Time is
  injected (`now_ms` parameter) so tests never sleep.
- One writer per file. Edit only the files your card names. Never run git.
- Missing evidence is `None` + Unknown. Never coerce missing to 0 or green.
- No secrets, ids, paths, or raw payloads in API output. The cloudflared
  `/ready` `connectorId` and any `receipt_path` are NEVER emitted.
- Language: clean, soft, factual — code comments, test names, messages.

## 1. Where things live (MEASURED)
- Box: user ssm-user, Python 3.12.3, sqlite 3.45.1 (JSON1 present), 2 vCPU,
  7.8 GB RAM, sudo -n works for service install.
- Dashboard dir: /home/ssm-user/quant-ui/ (repo ui/ copied to
  /home/ssm-user/quant-ui/ui/). Web server: quant-ui.service =
  `python3 ui/serve.py --port 8080` (loopback). Data the UI reads:
  /home/ssm-user/quant-ui/ui/live/ (mirror + recorders write here only).
- Mirror: quant-ui-mirror.service = `/bin/bash /home/ssm-user/quant-ui/local_mirror.sh`
  (23 lines, sha256 610c1426…, imported verbatim as ui/host/local_mirror.sh).
  Sources: R=/home/ssm-user/quant_receipts, D=/home/ssm-user/quant-run/wave2-validate-h2.
- Feed evidence: R/watch/feed_state.json — 5.4 MB, rewritten about once per
  second by the feed daemon (written_ms). NOT mirrored today. Schema:
  top keys connections{perp,spot}, symbols{...}, version, window,
  window_newest_ms, window_oldest_ms, written_ms (ms epoch as STRING).
  connections.<venue>: dropped_frames (cumulative int), expected_connections,
  handshake_failures, reconnects, successful_handshakes, status
  (healthy|degraded|broken), shards[] (kline sockets: attempts, connected,
  connection, handshake_failures, reconnects, status, stream_count,
  successful_handshakes), ticker{attempts, connected, connection:"ticker",
  handshake_failures, last_message_ms (STRING ms), reconnects, shards[...
  each with last_message_ms], status, stream_count, successful_handshakes}.
  Fixture: tests/fixtures/network/feed_state_sample.json (+ _degraded).
- Machine stream: ui/live/stream_state.json — dict symbol → row, 656 rows,
  345 KB, rewritten every 2–3 min. Row keys: basis_gap_pct, book_age_ms (STRING
  ms), freshness_ms (STRING ms), funding_rate, mark, next_funding_ms, oi,
  oi_change_pct, perp_ask, perp_bid, perp_min_notional, perp_step, spot_ask,
  spot_bid, spot_min_notional, spot_step, surge_ratio, utc (ISO Z), venues,
  vol_1m_quote, vol_med_1m. NO status and NO source field exists → source =
  Unknown "Runtime does not record book source". Fixture: stream_state_sample.json.
- News: ui/live/news_sources.json {as_of, available, live, pending,
  sources[{key,label,domain,channel,status(live|pending|error|...),count,
  last_ts}]}; ui/live/news.jsonl (mtime = last fetch). Fixture: news_sources_sample.json.
- Cloudflared 2026.7.3: metrics on http://127.0.0.1:20241/metrics (default
  range 20241–20245, discovered by listing listeners; the recorder discovers
  the same way — try the configured URL first, then 20241..20245, never other
  ports); /ready → {"status":200,"readyConnections":4,...}. Metric names
  PRESENT: cloudflared_tunnel_ha_connections, cloudflared_tunnel_total_requests,
  cloudflared_tunnel_request_errors, cloudflared_tunnel_concurrent_requests_per_tunnel,
  cloudflared_tunnel_response_by_code{status_code}, cloudflared_tunnel_server_locations,
  cloudflared_tunnel_tunnel_register_success, cloudflared_proxy_connect_streams_errors,
  quic_client_latest_rtt{conn_index}, quic_client_smoothed_rtt, quic_client_min_rtt,
  quic_client_lost_packets{conn_index,reason}. ABSENT on this version:
  cloudflared_tunnel_active_streams, cloudflared_tunnel_timer_retries (use the
  present ones; absent → null). Fixture: cloudflared_metrics_sample.txt.
- Origin: GET http://127.0.0.1:8080/ answers 200 in ~3 ms (MEASURED).
- Manifest: box mirror writes {"utc","ok","interval_ms","cycle_ms","files"} —
  no error field; the Mac pull writes "errors" (list); serve.py reads "error"
  (string). New rule: manifest carries "errors": [] always; readers accept
  both "error" (string) and "errors" (list). Fixtures: pull_manifest_*.json.
- Job results: ui/live/jobs/results.jsonl rows {job_id, op, symbol, verdict,
  exit_code, receipt_path, utc}. Only op/verdict/utc may be surfaced.

## 2. Nodes and edges (ids are FROZEN — history keys on them)
nodes[]: {id, label, kind}
  browser (Browser) · cloudflare (Cloudflare edge) · cloudflared (Tunnel
  connector) · quant_ui (Quant UI origin) · box (Execution box) · binance_spot
  (Binance Spot) · binance_usdm (Binance USD-M) · feed (Market feed) ·
  machine (Machine) · news (News sources) · binance_account (Binance signed
  reads) · binance_exec (Binance execution) · aws_secrets · aws_ssm.
edges[] (id: source→target · mode · required · evidence):
  browser_site      browser→quant_ui   Session    no   page-side fetch timing only; recorder writes nothing
  cf_connector      cloudflare→cloudflared Continuous yes  cloudflared metrics + /ready, every cf_monitor_s
  connector_origin  cloudflared→quant_ui   Periodic  yes  loopback GET / timing + tunnel request_errors delta, every cf_monitor_s
  spot_rest         box→binance_spot   Periodic   yes  GET https://api.binance.com/api/v3/time every rest_probe_s
  spot_wss          box↔binance_spot   Continuous yes  feed_state.connections.spot (passive), every wss_read_s
  usdm_rest         box→binance_usdm   Periodic   yes  GET https://fapi.binance.com/fapi/v1/time every rest_probe_s
  usdm_wss          box↔binance_usdm   Continuous yes  feed_state.connections.perp (passive), every wss_read_s
  feed_machine      feed→machine       Continuous yes  feed_state written_ms + stream_state rows, every wss_read_s
  news_fetch        news→box           Periodic   yes  news_sources.json + news.jsonl mtime, every news_monitor_s
  binance_account   box→binance_account On-demand no   nothing mirrored → Unknown "No signed-read evidence is mirrored"
  binance_exec      box→binance_exec   On-demand  no   last jobs/results.jsonl row → last_used_at, last_op, last_outcome
  aws_secrets       box→aws_secrets    On-demand  no   Unknown "No secrets-read evidence is mirrored"
  aws_ssm           box→aws_ssm        On-demand  no   Unknown "No SSM evidence is mirrored"
Groups (for pause/interval controls): rest={spot_rest,usdm_rest}
  wss={spot_wss,usdm_wss,feed_machine} cf={cf_connector,connector_origin}
  news={news_fetch}. Required denominator = the eight `required: yes` edges.

## 3. Metrics per edge (names FROZEN; all numbers, null when unknown)
- spot_rest / usdm_rest: rtt_ms, http_status, clock_offset_ms (server_time −
  midpoint(send,recv)), used_weight_1m (X-MBX-USED-WEIGHT-1M header),
  weight_limit_1m (documented: spot 6000, usdm 2400), retry_after_s,
  consecutive_failures; counters: count_429, count_418, probes, failures.
  One request per venue per cycle. Never retry a 429/418; honour Retry-After
  by skipping probes until it passes.
- spot_wss / usdm_wss: expected_connections, connected_count (ticker + all
  shards with connected true), successful_handshakes, handshake_failures,
  reconnects, stream_count (sum), last_message_age_ms (now − max
  last_message_ms), snapshot_age_ms (now − written_ms), dropped_frames_total,
  dropped_frames_delta (since previous sample; null on first), feed_status
  (string healthy|degraded|broken|other).
- feed_machine: feed_snapshot_age_ms, stream_row_age_ms (now − newest row
  utc), symbols_total, book_age_p50_ms, book_age_p95_ms, book_age_max_ms,
  symbols_book_le_5s (rows with book_age_ms ≤ 5000), freshness_p50_ms,
  source (always null in V1 — not recorded by the runtime).
- cf_connector: ha_connections, ready_connections, ready_status,
  total_requests_delta, concurrent_requests, latest_rtt_ms_max,
  smoothed_rtt_ms_max, min_rtt_ms_min, lost_packets_delta,
  connect_streams_errors_delta, register_success_total, metrics_url_port.
- connector_origin: rtt_ms, http_status, size_bytes, request_errors_delta
  (cloudflared_tunnel_request_errors delta), response_5xx_delta (sum of
  response_by_code 5xx deltas), consecutive_failures.
- news_fetch: sources_available, sources_live, sources_pending,
  sources_error, as_of_age_s, last_fetch_age_s.
- binance_exec (on-demand): last_used_at (ISO), last_op, last_outcome.
- browser_site: page-side fetch_ms only (client), never stored.

## 4. Health derivation (fixed rules, coded ONCE in ui/network_health.py)
Vocabulary: health ∈ Operational|Degraded|Outage|Stale|Unknown|Off;
authority ∈ Public|Read-only|Signed-read|Trade-capable|Owner-gated (per
edge, static: REST/WSS/cf/origin/news/feed = Public or Read-only;
binance_account = Signed-read; binance_exec = Trade-capable; aws_* =
Owner-gated); monitoring ∈ Running|Paused; truth.configured/transport/
application ∈ Pass|Fail|Unknown|Not-applicable. Owner-gated is never health.
Severity rank for "worst" and for history cells: Outage > Stale > Degraded >
Unknown > Off > Operational.
- Unknown: evidence file missing/malformed or the metric not exposed →
  health_reason names why (order lists the phrases).
- Off: the group's monitor is Paused (config) → reason "Monitor paused
  (receipt <id>)".
- Stale (recorder-side): evidence older than its freshness contract —
  REST/cf/origin: last sample older than 3× interval; WSS: mirrored
  written_ms older than 60 s (feed or mirror stopped); feed_machine: newest
  stream row older than 600 s; news: as_of older than 3× news_monitor_s.
  Stale (API-side, read time): now > fresh_until → Stale "No fresh sample
  since <observed_at>" — this is how a stopped recorder shows.
- REST: Outage when consecutive_failures ≥ 2 or a 418 is in force; Degraded
  when one failure, or a 429 within the last 5 min, or used_weight_1m ≥ 80 %
  of weight_limit_1m; Operational when the last probe is HTTP 200 with a
  valid serverTime AND two consecutive successes since the last failure
  (recovery quorum; the interim reads "Degraded — recovering 1/2").
- WSS: feed_status healthy → Operational; degraded → Degraded; broken or
  connected_count == 0 → Outage; overrides: last_message_age_ms > 60000 →
  Degraded (Binance closes a silent socket after ~1 min); connected_count <
  expected_connections → Degraded; reconnects increased since previous
  sample → event "reconnect" (health unchanged unless another rule fires);
  dropped_frames_delta transitioning 0→>0 → event "frames dropping" and
  >0→0 → event "frames stopped dropping" (metric, not a health rule).
- feed_machine: Operational when book_age_p50_ms ≤ 5000; Degraded when
  book_age_p50_ms > 5000 while feed_snapshot_age_ms ≤ 5000 (socket fresh,
  machine books older — the condition measured on 2026-08-16); Outage when
  book_age_p50_ms > 60000 while the feed is fresh; Stale per above; Unknown
  when either file is missing.
- cf_connector: Operational when ha_connections ≥ 1 and ready_status == 200
  and connect_streams_errors_delta == 0; Degraded when ha_connections < 4
  or connect_streams_errors_delta > 0 or ready_status != 200; Outage when
  ha_connections == 0 or metrics unreachable on 2 consecutive samples;
  Unknown when no metrics endpoint is found (reason "cloudflared metrics
  endpoint not found on 127.0.0.1:20241–20245").
- connector_origin: Operational when http_status == 200; Degraded when one
  failure or request_errors_delta > 0; Outage when consecutive_failures ≥ 2.
- news_fetch: Operational when as_of fresh and sources_error == 0; Degraded
  when sources_error > 0 or sources_live < sources_available / 2; Stale per
  above; Unknown when the file is missing.
- On-demand edges: health Unknown always in V1; reason carries last used /
  last outcome when known ("On-demand path; last used <utc>, <op> <verdict>").
- Truth per edge: REST configured=Pass, transport=Pass if any HTTP response
  (Fail on connect error), application=Pass on 200+serverTime. WSS
  configured=Pass if file present, transport=Pass if connected_count > 0,
  application=Pass if last_message_age_ms ≤ 60000. feed_machine
  configured=Pass if stream file present, transport=Not-applicable,
  application=Pass if book_age_p50_ms ≤ 5000. cf configured=Pass if metrics
  reachable, transport=Pass if ha_connections ≥ 1, application=Pass if ready
  200. origin configured=Pass, transport=Pass if TCP connect, application=
  Pass on 200. news configured=Pass if file, transport=Not-applicable,
  application=Pass if as_of fresh. On-demand: all Unknown.
- Transitions: an event row is written ONLY when health changes, or on the
  named markers (reconnect, frames dropping start/stop, 429/418 seen,
  origin proxy errors delta > 0, collector start/stop, config change,
  probe-now result, deployed release change). Healthy samples never make events.
- Summary: operational_required, observed_required (required edges with any
  sample), unknown_count (all edges), worst {edge_id, health}, oldest_required_
  evidence_at, last_transition {ts, edge_id, transition}.

## 5. Storage (FROZEN)
- Current: ui/live/network_current.json (atomic: write .tmp then os.replace).
- History: $QUANT_UI_DATA_DIR/network_history.sqlite3 — box default
  /home/ssm-user/quant-ui/data (OUTSIDE the web root); local default
  <repo>/data (gitignored). PRAGMA journal_mode=WAL, synchronous=NORMAL,
  auto_vacuum=INCREMENTAL. Tables:
    samples(ts_ms INTEGER NOT NULL, edge_id TEXT NOT NULL, status TEXT NOT NULL,
            metrics TEXT NOT NULL)  -- metrics = JSON object
      INDEX samples_edge_ts ON samples(edge_id, ts_ms)
    events(event_id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL,
           edge_id TEXT, transition TEXT NOT NULL, severity TEXT NOT NULL,
           reason TEXT NOT NULL, evidence TEXT NOT NULL)  -- evidence = JSON
      INDEX events_ts ON events(ts_ms)
    meta(key TEXT PRIMARY KEY, value TEXT)  -- schema_version=1, last_prune_ms
  Retention: samples 48 h, events 30 d, prune at most hourly, size cap 100 MB
  (page_count×page_size; when over: prune samples to 24 h, then
  incremental_vacuum). Corrupt DB → move aside as .corrupt-<ts>, recreate,
  write an event "storage reset".
- Monitoring config: ui/live/network_config.json
  {"rest_probe_s":15,"wss_read_s":15,"cf_monitor_s":15,"news_monitor_s":120,
   "paused":{"rest":false,"wss":false,"cf":false,"news":false},
   "updated_at":"<ISO>","receipt_id":<int or null>}
  Allowed: rest_probe_s ∈ {15,30,60}; wss_read_s ∈ {5,10,15}; cf_monitor_s ∈
  {15,30,60}; news_monitor_s ∈ {120,300,600}; paused.<group> ∈ {true,false}.
  The recorder rereads it every tick (mtime check). Missing file = defaults.
- Probe-now request: ui/live/network_probe_request.json
  {"edge_id":"spot_rest","request_id":"<hex>","requested_at":"<ISO>"} — the
  API writes it (atomic); the recorder consumes it on its next tick (reads,
  deletes), runs ONE probe for that edge if ≥ 30 s since that edge's last
  probe-now, else records event "probe refused (cooldown)". Allowed edges:
  spot_rest, usdm_rest, connector_origin, cf_connector.
- Receipts = event rows: transition "config" (reason "rest_probe_s 15 → 30",
  evidence {key, old, new, receipt_id, reread}) and "probe" (reason "probe
  now spot_rest: 200 in 187 ms").
- Release: ui/RELEASE (one line, git SHA written by the deploy script; local
  "dev") → deployed_ui_release; the recorder also stamps recorder_release.

## 6. network_current.json / GET /api/network (FROZEN shape, < 100 KB)
{
 "schema_version": 1,
 "generated_at": "<ISO Z>", "generated_ms": <int>,
 "collector": {"status":"running|stopped|unknown", "started_at":..., "last_cycle_at":...,
               "cycle_ms":<int>, "tick_s":5, "recorder_release":"<sha|dev>",
               "config": {...network_config.json...}, "probe_now": {"<edge>": "<last ISO>"}},
 "deployed_ui_release": "<sha|dev|null>",
 "summary": {"operational_required":n, "observed_required":n, "required_total":8,
             "unknown_count":n, "worst":{"edge_id":..,"health":..},
             "oldest_required_evidence_at":"<ISO|null>",
             "last_transition":{"ts":..,"edge_id":..,"transition":..}|null},
 "nodes": [ {"id","label","kind"} ],
 "edges": [ {"id","source","target","label","direction":"->|<->","mode","required":bool,
             "group":"rest|wss|cf|news|session|ondemand",
             "health","health_reason","authority","monitoring":"Running|Paused",
             "truth":{"configured","transport","application"},
             "observed_at":"<ISO|null>","observed_ms":<int|null>,"fresh_until_ms":<int|null>,
             "cadence_ms":<int|null>,"sample_count":<int>,
             "metrics":{...section 3...},"counters":{...},
             "evidence":{"source":"<what file/URL>","source_age_s":<num|null>},
             "controls":["pause_monitor","monitor_every","probe_now"]  // only those real for this edge
           } ],
 "events": [ {"event_id","ts","edge_id","transition","severity","reason","evidence"} ]  // newest 20
}
API read-time rules (ui/network_api.py): apply Stale when now > fresh_until_ms;
collector.status = "stopped" when now − generated_ms > 3 × tick_s × 1000;
if network_current.json is missing → 200 with schema_version, generated_at
null, collector.status "unknown", edges = the 13 static edges all Unknown
"Recorder has not written yet" (never 500, never empty).

## 7. History and events endpoints
GET /api/network/history?edge_id=<id>&metric=status|<metric name>&range=1h|6h|24h
 → {"edge_id","metric","range","bucket_s":60|300|900,"from_ms","to_ms",
    "buckets":[{"t":<bucket start ms>,"n":<samples>,"status":"<worst by rank>",
                "avg":<num|null>,"max":<num|null>}]}  (status-only when metric=status)
 Bucket count 60/72/96; NEVER more than 720; unknown edge/metric/range → 400.
 Empty buckets are omitted (the page draws them grey "no data").
GET /api/network/events?after=<event_id>&limit=<1..200 default 50>
 → {"events":[...oldest→newest after cursor...],"next_after":<last event_id>}
POST /api/network/config  body {"key":"rest_probe_s","value":30} or
 {"key":"paused","group":"rest","value":true} → validate allowlist (400 on
 anything else) → write config atomically → append event "config" → reread
 file → 200 {"ok":true,"saved":{"key","old","new","receipt_id","reread":true}}.
 Cooldown 2 s per key. Origin/Sec-Fetch-Site check: an Origin header that is
 not this Host, or Sec-Fetch-Site cross-site → 403.
POST /api/network/probe body {"edge_id":"spot_rest"} → allowlist → if that
 edge's last probe-now < 30 s ago → 400 {"ok":false,"error":"cooldown 27 s"}
 else write the request file → 200 {"ok":true,"saved":{"edge_id","request_id"}}.
serve.py: routes register in API_ROUTES / the do_POST writers dict exactly
like /api/poll_config and /api/grace; body ≤ 4096 bytes; the V5 routes and
static serving are untouched.

## 8. Python interfaces (build to these; siblings may not exist yet — import
##    lazily inside functions or accept injected callables in tests)
ui/network_store.py
  class NetworkStore:
    def __init__(self, path: str, size_cap_bytes: int = 100*1024*1024): ...
    def open(self) -> None            # creates schema, pragmas, meta
    def close(self) -> None
    def insert_sample(self, ts_ms: int, edge_id: str, status: str, metrics: dict) -> None
    def insert_event(self, ts_ms: int, edge_id, transition: str, severity: str,
                     reason: str, evidence: dict) -> int   # returns event_id
    def history(self, edge_id: str, metric: str, range_key: str, now_ms: int) -> dict  # section 7 shape
    def events_after(self, after_id: int, limit: int) -> dict  # section 7 shape
    def latest_events(self, limit: int = 20) -> list
    def prune(self, now_ms: int) -> dict   # {"samples_deleted","events_deleted","bytes"}
    def size_bytes(self) -> int
  RANGES = {"1h": (3600_000, 60_000), "6h": (21600_000, 300_000), "24h": (86400_000, 900_000)}
  STATUS_RANK = {"Outage":6,"Stale":5,"Degraded":4,"Unknown":3,"Off":2,"Operational":1}
ui/network_probe_binance.py
  VENUES = {"spot_rest": ("https://api.binance.com/api/v3/time", 6000),
            "usdm_rest": ("https://fapi.binance.com/fapi/v1/time", 2400)}
  def probe_time(edge_id: str, opener=None, timeout_s: float = 5.0, now_ms=None) -> dict
      # returns {"ok":bool,"http_status":int|None,"rtt_ms":int|None,"clock_offset_ms":int|None,
      #          "used_weight_1m":int|None,"retry_after_s":int|None,"error":str|None,
      #          "server_time_ms":int|None}; opener = callable(url, timeout) → (status, headers dict, body bytes)
      # NO retry inside; a 429/418 returns ok False with http_status set.
ui/network_probe_cf.py
  def discover_metrics_url(configured: str|None, opener=None) -> str|None   # tries configured, then 127.0.0.1:20241..20245/metrics
  def scrape_metrics(url: str, opener=None, timeout_s: float = 3.0) -> dict   # {"ok","raw_values":{name→float or {labels→float}},"error"}
  def parse_prometheus_text(text: str) -> dict   # {"cloudflared_tunnel_ha_connections":4.0, "quic_client_latest_rtt":{"conn_index=0":1.0,...}, ...}
  def read_ready(base_url: str, opener=None) -> dict   # {"ok","status","ready_connections"} — connectorId dropped
  def probe_origin(url: str = "http://127.0.0.1:8080/", opener=None, timeout_s: float = 3.0) -> dict  # {"ok","http_status","rtt_ms","size_bytes","error"}
  def cf_metrics_from_raw(raw: dict, prev_raw: dict|None) -> dict   # section 3 cf_connector + connector_origin deltas
ui/network_evidence.py
  def read_feed_connections(path: str, now_ms: int) -> dict|None
      # {"spot": {...spot_wss metrics minus delta...}, "perp": {...}, "written_ms": int, "snapshot_age_ms": int}
      # None when missing/malformed (caller reports Unknown); tolerant of string ms
  def read_stream_state(path: str, now_ms: int) -> dict|None   # feed_machine metrics (section 3) minus feed_snapshot_age_ms
  def read_news(sources_path: str, jsonl_path: str, now_ms: int) -> dict|None   # news_fetch metrics
  def read_last_job(path: str) -> dict|None   # {"last_used_at","last_op","last_outcome"} — never receipt_path/job_id
  def read_manifest(path: str) -> dict|None   # normalises "error"/"errors" → {"utc","ok","errors":[...],...}
ui/network_health.py
  EDGES: list of static edge dicts (id, source, target, label, direction, mode, required, group, authority, controls)
  NODES: list of static node dicts
  def derive_edge(edge_id: str, evidence: dict|None, prev: dict|None, config: dict, now_ms: int) -> dict
      # returns the full edge dict of section 6 (health, reason, truth, metrics, counters, fresh_until_ms, ...)
      # `prev` = the previous derived edge (for quorum, deltas, event detection); evidence = the raw dict from probes/evidence readers
  def transitions(prev_edge: dict|None, new_edge: dict) -> list   # [{"transition","severity","reason","evidence"}] per section 4
  def summarize(edges: list, now_ms: int) -> dict   # summary of section 6
  def apply_read_time_staleness(current: dict, now_ms: int) -> dict   # used by the API
ui/network_recorder.py
  main(argv): --live-dir, --data-dir, --tick 5, --once, --metrics-url, --origin-url, -q
  Loop: every tick reread config; for each group due (interval and not paused): collect → derive → write samples
  (every collected edge every cycle) → write events (transitions only) → write current atomically; consume
  probe request; prune hourly; SIGTERM → write collector "stopped" event and current, exit 0.
ui/network_api.py
  def api_network(sources, now=None) -> dict
  def api_network_history(sources, query: dict) -> tuple(status_code, dict)
  def api_network_events(sources, query: dict) -> tuple(status_code, dict)
  def write_network_config(sources, body, headers=None) -> (saved, error)   # V5 writer signature
  def write_network_probe(sources, body, headers=None) -> (saved, error)
  sources gains keys: "network_current", "network_config", "network_probe_request",
  "network_db" (path), "ui_release".

## 9. Deploy shape (integrator runs; workers only write the scripts)
- ui/host/quant-ui-network.service: copy of quant-ui-telemetry.service shape
  (User ssm-user, WorkingDirectory /home/ssm-user/quant-ui, Environment
  QUANT_UI_DATA_DIR=/home/ssm-user/quant-ui/data, ExecStart
  /usr/bin/python3 ui/network_recorder.py --tick 5 -q, Restart on-failure,
  NoNewPrivileges, ProtectSystem=full, ReadWritePaths for ui/live and data).
- ui/host/local_mirror.sh: the imported script + the feed_state block; the
  deploy replaces /home/ssm-user/quant-ui/local_mirror.sh and restarts
  quant-ui-mirror (covered by the build order).
- Staging: ui/host/stage_server.sh runs the combined serve.py on 127.0.0.1:8081
  (foreground with timeout) so every old and new endpoint can be curled;
  ui/host/cutover_server.sh backs up ui/serve.py to ui/serve.py.rollback-<ts>,
  restarts quant-ui, verifies index size + /api/strip + /api/network;
  ui/host/rollback_server.sh restores the backup and restarts.
