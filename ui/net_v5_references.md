# Network V5 — measured reference census (2026-08-16)

Method: 10 parallel codex research lanes, one per category, each curl-
verifying every URL it reports (HTTP status recorded per row). Raw rows:
214; deduped 203; reachable 179; judged operator-grade 150; relevance-5
to a loops-vs-cadence page: 72. Lead spot-checked 8 random URLs (all
200). Raw catalog: scratchpad research/merged.json + lane*.jsonl (session
artifacts); this file is the durable digest.

## Technique census across 150 operator-grade surfaces

| technique | count | V5 |
|---|---|---|
| table-first | 86 | yes — worst-first table |
| event-log | 86 | ADDED this commit (QNet.events card) |
| printed-stats | 75 | yes — stat lines under panels |
| state-ladder | 71 | yes — Operational/Degraded/Outage = Up/Late/Down |
| semantic-color-only | 68 | yes — DESIGN.md law |
| sparklines | 44 | yes — trend column |
| dense-mono-numbers | 43 | yes — mono datum caste |
| status-raster | 41 | yes — schedule history |
| distributions | 28 | yes — lag envelope panels |
| sla-percent | 25 | inverse — late% column |
| topology-map | 21 | diluted — path strip (weakest element) |
| uptime-bars | 18 | yes — raster cells |
| latency-percentiles | 8 | yes — p50/p95 |

## Lane verdicts (the ONE must-have per category, 10 lanes)

status-raster / one-cell-per-expected-run ×6 · state-ladder ×2 ·
table-first ×1 · distributions ×1. Open owner choice: V5's raster is
TIME-aligned (Grafana status-history style, rows comparable); Cronitor/
Healthchecks strips are RUN-aligned (one cell per expected beat, rows not
column-aligned). Both are canon; V5 ships time-aligned.

## Strongest references by category (all curl-verified 200)

Cron/heartbeat (the page's true product category):
- https://healthchecks.io/ — Period/Grace, Up→Late→Down ladder, table
- https://cronitor.io/cron-job-monitoring — run strips, duration asserts
- https://sentry.io/product/cron-monitoring/ — missed/failed check-ins
- https://www.checklyhq.com/product/heartbeat-monitoring/ — next-expected-ping
- https://elmah.io/features/heartbeats/ — interval+grace sliders

Status pages (state ladder + uptime bars + event log genre):
- https://status.stripe.com/ — 90-day per-service uptime bars
- https://status.cloud.google.com/ — product×region status matrix
- https://slack-status.com/ — five-level ladder, incident log
- https://status.kraken.com/ / https://status.gemini.com/ /
  https://status.databento.com/ — trading-venue ops status genre
- https://www.nasdaqtrader.com/Trader.aspx?id=MarketSystemStatusToday —
  timestamped system-status tables, market ops

Network instruments (live public data):
- https://smokeping.oetiker.ch/ — RTT median + distribution smoke
- https://stats.ams-ix.net/ — printed peak/avg/current under graphs
- https://my.es.net/ — topology with live per-link traffic
- https://stat.ripe.net/ · https://atlas.ripe.net/ · https://lg.he.net/
  · https://lg.ring.nlnog.net/ · https://www.ihr.live/ihr/en-us/
- https://mainsfrequency.com/ — single-quantity live instrument, gauge+graph

Schedulers/CI (run-history genre):
- https://airflow.apache.org/docs/apache-airflow/stable/ui.html — DAG
  table: cadence, next-run, run-state grid
- https://docs.temporal.io/web-ui · https://kestra.io/docs/ui/executions
- https://circleci.com/docs/guides/insights/insights/ — p50/p95 durations

SRE write-ups showing real internal dashboards:
- https://blog.cloudflare.com/alerts-observability/ — real alert Grafana
- https://engineering.fb.com/2012/09/19/core-infra/monitoring-cache-with-claspin/
  — 10k-host status heatmap (the raster at Facebook scale)
- https://dropbox.tech/infrastructure/lessons-learned-in-incident-management

Dashboard canon:
- https://grafana.com/grafana/dashboards/7587-prometheus-blackbox-exporter/
  — 18.1M downloads; the de-facto probe dashboard
- https://play.grafana.org/d/grafana_slo_app-bsf5l7ph2hdga0i90aj9p/ — SLO
  budget instrument (28-day SLI, remaining budget)
- https://status.kuma.pet/ · https://status.openstatus.dev/ — modern OSS
  status instruments, per-monitor bars + heartbeat feeds

## V5 audit verdict after the census

Confirmed by census: table-first, state-ladder, raster, printed stats,
distributions, semantic color, sparklines, percentiles. Gap found and
closed same-turn: event-log (86/150). Owner-decision items left open:
per-loop grace as explicit config (Healthchecks pattern; V5 hard-codes
2×/4×), run-duration tracking (needs recorder change — telemetry has no
duration field today), run-aligned vs time-aligned raster cells.
