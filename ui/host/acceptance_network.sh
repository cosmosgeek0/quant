#!/bin/bash
set -u

ROOT=/home/ssm-user/quant-ui
UI_DIR=$ROOT/ui
DB_PATH=$ROOT/data/network_history.sqlite3
BASE_URL=http://127.0.0.1:8080
SERVICE=quant-ui-network
WINDOW_S=${1:-600}
FAILURES=
NEEDS_START=0
FETCH_BODY=
NETWORK_BODY=
BENCH_BODY=

utc_now() {
  date -u '+%Y-%m-%dT%H:%M:%SZ'
}

add_failure() {
  if [ -z "$FAILURES" ]; then
    FAILURES=$1
  fi
}

restore_service() {
  if [ "$NEEDS_START" -ne 1 ]; then
    return
  fi
  if sudo -n systemctl start "$SERVICE" >/dev/null 2>&1; then
    NEEDS_START=0
    echo "== safeguard start=PASS service=$SERVICE"
  else
    add_failure safeguard_start
    echo "== safeguard start=FAIL service=$SERVICE"
  fi
}

trap restore_service EXIT

case "$WINDOW_S" in
  ''|*[!0-9]*) add_failure window_argument ;;
  *)
    if [ "$WINDOW_S" -le 0 ]; then
      add_failure window_argument
    fi
    ;;
esac
if [ "$#" -gt 1 ]; then
  add_failure window_argument
fi
if [ -n "$FAILURES" ]; then
  trap - EXIT
  echo "== ACCEPTANCE FAIL $FAILURES"
  echo "== final utc=$(utc_now)"
  exit 20
fi

fetch_url() {
  FETCH_BODY=
  if FETCH_BODY=$(curl --silent --show-error --fail --noproxy '*' \
    --connect-timeout 2 --max-time 10 "$1" 2>/dev/null); then
    return 0
  fi
  FETCH_BODY=
  return 1
}

json_value() {
  python3 -c '
from __future__ import annotations
import json
import sys

try:
    value = json.load(sys.stdin)
    for key in sys.argv[1].split("."):
        value = value.get(key) if isinstance(value, dict) else None
except (OSError, TypeError, ValueError):
    value = None
if value is None or isinstance(value, (dict, list)):
    print("Unknown")
elif isinstance(value, bool):
    print("true" if value else "false")
else:
    text = " ".join(str(value).split())
    print(text[:120] if text else "Unknown")
' "$1"
}

json_is_network() {
  python3 -c '
from __future__ import annotations
import json
import sys

required = {
    "cf_connector", "connector_origin", "spot_rest", "spot_wss",
    "usdm_rest", "usdm_wss", "feed_machine", "news_fetch",
}
try:
    data = json.load(sys.stdin)
    edges = data.get("edges") if isinstance(data, dict) else None
    found = {
        row.get("id") for row in edges
        if isinstance(row, dict) and row.get("required") is True
    }
    valid = isinstance(data.get("collector"), dict) and found == required
except (AttributeError, OSError, TypeError, ValueError):
    valid = False
raise SystemExit(0 if valid else 1)
'
}

print_required_edges() {
  python3 -c '
from __future__ import annotations
import json
import sys

edge_ids = (
    "browser_site", "cf_connector", "connector_origin", "spot_rest",
    "spot_wss", "usdm_rest", "usdm_wss", "feed_machine", "news_fetch",
)
required = edge_ids[1:]
allowed = {"Operational", "Degraded", "Outage", "Stale", "Unknown", "Off"}
try:
    data = json.load(sys.stdin)
    rows = data.get("edges", []) if isinstance(data, dict) else []
except (OSError, TypeError, ValueError):
    rows = []
rows = rows if isinstance(rows, list) else []
by_id = {
    row.get("id"): row for row in rows
    if isinstance(row, dict) and row.get("id") in required
}
for edge_id in required:
    row = by_id.get(edge_id, {})
    health = row.get("health")
    health = health if health in allowed else "Unknown"
    observed = row.get("observed_at")
    observed = "Unknown" if observed is None else " ".join(str(observed).split())[:80]
    print("== baseline edge id=%s health=%s observed_at=%s" % (
        edge_id, health, observed or "Unknown"))
'
}

stale_values() {
  python3 -c '
from __future__ import annotations
import json
import sys

required = (
    "cf_connector", "connector_origin", "spot_rest", "spot_wss",
    "usdm_rest", "usdm_wss", "feed_machine", "news_fetch",
)
shown = ("spot_rest", "spot_wss", "cf_connector", "feed_machine", "news_fetch")
allowed = {"Operational", "Degraded", "Outage", "Stale", "Unknown", "Off"}
try:
    data = json.load(sys.stdin)
    collector = data.get("collector", {}).get("status", "unknown")
    rows = data.get("edges", [])
except (AttributeError, OSError, TypeError, ValueError):
    collector, rows = "unknown", []
rows = rows if isinstance(rows, list) else []
by_id = {
    row.get("id"): row for row in rows
    if isinstance(row, dict) and row.get("id") in required
}
health = {}
for edge_id in required:
    value = by_id.get(edge_id, {}).get("health")
    health[edge_id] = value if value in allowed else "Unknown"
all_stale = len(by_id) == len(required) and all(health[key] == "Stale" for key in required)
values = [collector] + [health[key] for key in shown]
values.append("yes" if all_stale else "no")
print("|".join(values))
'
}

recovery_values() {
  python3 -c '
from __future__ import annotations
import json
import sys

try:
    data = json.load(sys.stdin)
    rows = data.get("edges", []) if isinstance(data, dict) else []
except (OSError, TypeError, ValueError):
    rows = []
rows = rows if isinstance(rows, list) else []
by_id = {
    row.get("id"): row for row in rows
    if isinstance(row, dict) and row.get("id") in ("spot_rest", "usdm_rest")
}
health = [by_id.get(key, {}).get("health", "Unknown") for key in ("spot_rest", "usdm_rest")]
counter_rows = [by_id.get(key, {}).get("counters") for key in ("spot_rest", "usdm_rest")]
probes = [value.get("probes") if isinstance(value, dict) else None for value in counter_rows]
valid = all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in probes)
total = str(sum(probes)) if valid else "Unknown"
print("|".join((str(health[0]), str(health[1]), total)))
'
}

db_counts() {
  python3 -c '
from __future__ import annotations
import sqlite3
import sys
import urllib.parse

database = None
try:
    path = urllib.parse.quote(sys.argv[1])
    database = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    samples = database.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    events = database.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    print("%d|%d" % (samples, events))
except (OSError, sqlite3.Error, TypeError, ValueError):
    print("Unknown|Unknown")
finally:
    if database is not None:
        database.close()
' "$DB_PATH"
}

latest_event_after() {
  python3 -c '
from __future__ import annotations
import sqlite3
import sys
import urllib.parse

database = None
try:
    path = urllib.parse.quote(sys.argv[1])
    database = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    value = database.execute("SELECT MAX(event_id) FROM events").fetchone()[0]
    print(max(0, int(value or 0) - 6))
except (OSError, sqlite3.Error, TypeError, ValueError):
    print("Unknown")
finally:
    if database is not None:
        database.close()
' "$DB_PATH"
}

print_events() {
  python3 -c '
from __future__ import annotations
import json
import sys

def clean(value, limit):
    if value is None:
        return "Unknown"
    text = " ".join(str(value).split())
    return text[:limit] if text else "Unknown"

try:
    data = json.load(sys.stdin)
    rows = data.get("events", []) if isinstance(data, dict) else []
except (OSError, TypeError, ValueError):
    rows = []
rows = rows if isinstance(rows, list) else []
if not rows:
    print("== recovery events=Unknown")
    raise SystemExit(1)
for row in rows[-6:]:
    row = row if isinstance(row, dict) else {}
    print("== recovery event id=%s edge=%s transition=%s reason=%s" % (
        clean(row.get("event_id"), 20), clean(row.get("edge_id"), 24),
        clean(row.get("transition"), 35), clean(row.get("reason"), 50)))
'
}

events_show_recovery() {
  python3 -c '
from __future__ import annotations
import json
import sys

try:
    data = json.load(sys.stdin)
    rows = data.get("events", []) if isinstance(data, dict) else []
except (OSError, TypeError, ValueError):
    rows = []
rows = rows if isinstance(rows, list) else []
visible = any(
    isinstance(row, dict)
    and row.get("edge_id") in ("spot_rest", "usdm_rest")
    and "Operational" in str(row.get("transition", ""))
    for row in rows
)
raise SystemExit(0 if visible else 1)
'
}

db_size() {
  local value
  value=$(stat -c %s "$DB_PATH" 2>/dev/null) || value=Unknown
  case "$value" in
    ''|*[!0-9]*) echo Unknown ;;
    *) echo "$value" ;;
  esac
}

service_value() {
  local value
  value=$(systemctl show "$SERVICE" -p "$1" --value 2>/dev/null) || value=Unknown
  case "$value" in
    ''|*[!0-9]*) echo Unknown ;;
    *) echo "$value" ;;
  esac
}

rss_for_pid() {
  local value=$1
  local rss
  case "$value" in
    ''|0|*[!0-9]*) echo Unknown; return ;;
  esac
  rss=$(ps -o rss= -p "$value" 2>/dev/null | awk 'NR == 1 {print $1}') || rss=Unknown
  case "$rss" in
    ''|*[!0-9]*) echo Unknown ;;
    *) echo "$rss" ;;
  esac
}

known_unsigned() {
  case "$1" in
    ''|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

signed_delta() {
  if known_unsigned "$1" && known_unsigned "$2"; then
    echo $((1 - 1 + $1 - $2))
  else
    echo Unknown
  fi
}

float_less() {
  python3 -c '
from __future__ import annotations
import math
import sys

try:
    value = float(sys.argv[1])
    limit = float(sys.argv[2])
    valid = math.isfinite(value) and math.isfinite(limit) and value < limit
except (TypeError, ValueError):
    valid = False
raise SystemExit(0 if valid else 1)
' "$1" "$2"
}

cpu_percent() {
  python3 -c '
from __future__ import annotations
import sys

try:
    delta = int(sys.argv[1]) - int(sys.argv[2])
    elapsed = int(sys.argv[3])
    if delta < 0 or elapsed <= 0:
        raise ValueError
    print("%.6f" % (delta * 100.0 / (elapsed * 1000000000.0)))
except (TypeError, ValueError):
    print("Unknown")
' "$1" "$2" "$3"
}

growth_per_hour() {
  python3 -c '
from __future__ import annotations
import sys

try:
    delta = int(sys.argv[1]) - int(sys.argv[2])
    elapsed = int(sys.argv[3])
    if elapsed <= 0:
        raise ValueError
    print("%.3f" % (delta * 3600.0 / elapsed))
except (TypeError, ValueError):
    print("Unknown")
' "$1" "$2" "$3"
}

benchmark_endpoint() {
  local label=$1
  local url=$2
  local size_limit=$3
  local time_limit=$4
  local marker=__QNET_ACCEPTANCE_CURL__
  local run=1
  local ok_runs=0
  local max_bytes=0
  local max_time=0
  local response body meta code bytes time_total curl_rc
  local result=PASS
  BENCH_BODY=
  while [ "$run" -le 10 ]; do
    response=$(curl --silent --show-error --noproxy '*' --connect-timeout 2 \
      --max-time 10 -w "$marker%{http_code} %{size_download} %{time_total}" \
      "$url" 2>/dev/null)
    curl_rc=$?
    if [ "$curl_rc" -ne 0 ] || [[ "$response" != *"$marker"* ]]; then
      echo "== endpoint $label sample=$run result=Unknown"
      result=FAIL
      run=$((run + 1))
      continue
    fi
    body=${response%%"$marker"*}
    meta=${response##*"$marker"}
    code=${meta%% *}
    meta=${meta#* }
    bytes=${meta%% *}
    time_total=${meta#* }
    case "$bytes" in
      ''|*[!0-9]*) bytes=Unknown ;;
    esac
    if [ "$code" != 200 ] || [ "$bytes" = Unknown ] || ! float_less "$time_total" 1000000; then
      echo "== endpoint $label sample=$run code=$code bytes=$bytes time_total_s=Unknown"
      result=FAIL
      run=$((run + 1))
      continue
    fi
    if ! printf '%s' "$body" | python3 -c 'import json,sys; value=json.load(sys.stdin); assert isinstance(value,dict)' 2>/dev/null; then
      echo "== endpoint $label sample=$run code=200 bytes=$bytes body=Unknown"
      result=FAIL
      run=$((run + 1))
      continue
    fi
    BENCH_BODY=$body
    ok_runs=$((ok_runs + 1))
    if [ "$bytes" -gt "$max_bytes" ]; then
      max_bytes=$bytes
    fi
    if float_less "$max_time" "$time_total"; then
      max_time=$time_total
    fi
    echo "== endpoint $label sample=$run bytes=$bytes time_total_s=$time_total"
    run=$((run + 1))
  done
  if [ "$ok_runs" -ne 10 ]; then
    result=FAIL
  fi
  if [ "$size_limit" != none ] && [ "$max_bytes" -ge "$size_limit" ]; then
    result=FAIL
  fi
  if [ "$time_limit" != none ] && ! float_less "$max_time" "$time_limit"; then
    result=FAIL
  fi
  echo "== endpoint $label result=$result runs=$ok_runs/10 max_bytes=$max_bytes" \
    "max_time_total_s=$max_time p95_proxy_s=$max_time proxy=max_of_10"
  if [ "$result" != PASS ]; then
    add_failure "endpoint_$label"
  fi
}

scan_count() {
  printf '%s\n%s\n%s\n' "$SCAN_NETWORK" "$SCAN_HISTORY" "$SCAN_EVENTS" \
    | grep -i -F -c -- "$1" || true
}

release=Unknown
if [ -r "$UI_DIR/RELEASE" ]; then
  release=$(sed -n '1p' "$UI_DIR/RELEASE" 2>/dev/null | tr -d '\r\n')
  case "$release" in
    ''|*[!A-Za-z0-9._-]*) release=Unknown ;;
  esac
fi
release=${release:0:120}
if ! fetch_url "$BASE_URL/api/network"; then
  add_failure record_api
fi
NETWORK_BODY=$FETCH_BODY
recorder_release=$(printf '%s' "$NETWORK_BODY" | json_value collector.recorder_release)
deployed_release=$(printf '%s' "$NETWORK_BODY" | json_value deployed_ui_release)
recorder_release=${recorder_release:0:64}
deployed_release=${deployed_release:0:64}
echo "== record ui_release=$release"
echo "== record recorder_release=$recorder_release deployed_ui_release=$deployed_release"
if [ "$release" = Unknown ] || [ "$recorder_release" = Unknown ] \
  || [ "$deployed_release" = Unknown ]; then
  add_failure record_release
fi

shopt -s nullglob
network_hash_files=("$UI_DIR"/network_*.py)
shopt -u nullglob
hash_files=("${network_hash_files[@]}" "$UI_DIR/serve.py")
if [ "${#network_hash_files[@]}" -eq 0 ]; then
  add_failure record_hash
fi
for hash_path in "${hash_files[@]}"; do
  relative=${hash_path#"$ROOT"/}
  if [ ! -f "$hash_path" ]; then
    echo "== record sha256 file=$relative first16=Unknown"
    add_failure record_hash
    continue
  fi
  hash_row=$(sha256sum "$hash_path" 2>/dev/null) || hash_row=
  hash_value=${hash_row%% *}
  hash_value=${hash_value:0:16}
  case "$hash_value" in
    ????????????????) ;;
    *) hash_value=Unknown; add_failure record_hash ;;
  esac
  echo "== record sha256 file=$relative first16=$hash_value"
done

if ! fetch_url "$BASE_URL/api/network"; then
  add_failure baseline_api
fi
NETWORK_BODY=$FETCH_BODY
if ! printf '%s' "$NETWORK_BODY" | json_is_network; then
  add_failure baseline_shape
fi
printf '%s' "$NETWORK_BODY" | print_required_edges
collector_status=$(printf '%s' "$NETWORK_BODY" | json_value collector.status)
db_bytes=$(db_size)
IFS='|' read -r sample_count event_count < <(db_counts)
echo "== baseline database bytes=$db_bytes samples=$sample_count events=$event_count"
if [ "$db_bytes" = Unknown ] || [ "$sample_count" = Unknown ] || [ "$event_count" = Unknown ]; then
  add_failure baseline_database
fi
baseline_pid=$(service_value MainPID)
baseline_rss=$(rss_for_pid "$baseline_pid")
baseline_cpu=$(service_value CPUUsageNSec)
echo "== baseline recorder utc=$(utc_now) collector=$collector_status main_pid=$baseline_pid" \
  "rss_kib=$baseline_rss cpu_usage_nsec=$baseline_cpu"
if [ "$collector_status" != running ] || [ "$baseline_pid" = Unknown ] \
  || [ "$baseline_rss" = Unknown ] || [ "$baseline_cpu" = Unknown ]; then
  add_failure baseline_recorder
fi

stop_utc=$(utc_now)
stop_tick=$SECONDS
if sudo -n systemctl stop "$SERVICE" >/dev/null 2>&1; then
  NEEDS_START=1
  echo "== stop result=PASS service=$SERVICE utc=$stop_utc"
  stale_pass=no
  stale_attempt=1
  while [ "$stale_attempt" -le 9 ]; do
    sleep 10
    elapsed=$((SECONDS - stop_tick))
    if fetch_url "$BASE_URL/api/network"; then
      NETWORK_BODY=$FETCH_BODY
    else
      NETWORK_BODY=
    fi
    IFS='|' read -r poll_collector poll_spot poll_spot_wss poll_cf poll_feed \
      poll_news poll_all < <(printf '%s' "$NETWORK_BODY" | stale_values)
    echo "== stale poll elapsed_s=$elapsed collector=$poll_collector spot_rest=$poll_spot" \
      "spot_wss=$poll_spot_wss cf_connector=$poll_cf feed_machine=$poll_feed" \
      "news_fetch=$poll_news required_all_stale=$poll_all"
    if [ "$poll_collector" = stopped ] && [ "$poll_all" = yes ]; then
      stale_pass=yes
      stale_seconds=$elapsed
      break
    fi
    stale_attempt=$((stale_attempt + 1))
  done
  if [ "$stale_pass" = yes ]; then
    echo "== stale result=PASS seconds=$stale_seconds"
  else
    echo "== stale result=FAIL timeout_s=90"
    add_failure stale_timeout
  fi
else
  echo "== stop result=FAIL service=$SERVICE utc=$stop_utc"
  add_failure stop_service
fi

if fetch_url "$BASE_URL/api/network"; then
  NETWORK_BODY=$FETCH_BODY
fi
IFS='|' read -r pre_spot pre_usdm probes_at_start \
  < <(printf '%s' "$NETWORK_BODY" | recovery_values)
start_utc=$(utc_now)
start_tick=$SECONDS
if sudo -n systemctl start "$SERVICE" >/dev/null 2>&1; then
  NEEDS_START=0
  echo "== start result=PASS service=$SERVICE utc=$start_utc" \
    "rest_probes_total_start=$probes_at_start"
  recovery_pass=no
  recovery_attempt=1
  while [ "$recovery_attempt" -le 18 ]; do
    sleep 5
    elapsed=$((SECONDS - start_tick))
    if fetch_url "$BASE_URL/api/network"; then
      NETWORK_BODY=$FETCH_BODY
    else
      NETWORK_BODY=
    fi
    IFS='|' read -r recovery_spot recovery_usdm probes_now \
      < <(printf '%s' "$NETWORK_BODY" | recovery_values)
    probes_delta=$(signed_delta "$probes_now" "$probes_at_start")
    echo "== recovery poll elapsed_s=$elapsed spot_rest=$recovery_spot" \
      "usdm_rest=$recovery_usdm probes_delta=$probes_delta scope=spot_plus_usdm"
    if [ "$recovery_spot" = Operational ] && [ "$recovery_usdm" = Operational ] \
      && known_unsigned "$probes_delta" && [ "$probes_delta" -ge 2 ]; then
      recovery_pass=yes
      recovery_seconds=$elapsed
      break
    fi
    recovery_attempt=$((recovery_attempt + 1))
  done
  if [ "$recovery_pass" = yes ]; then
    echo "== recovery result=PASS seconds=$recovery_seconds probes_delta=$probes_delta" \
      "scope=spot_plus_usdm"
  else
    echo "== recovery result=FAIL timeout_s=90"
    add_failure recovery_timeout
  fi
else
  echo "== start result=FAIL service=$SERVICE utc=$start_utc probes_start=$probes_at_start"
  add_failure start_service
fi

if [ "$NEEDS_START" -eq 1 ]; then
  restore_service
fi
if [ "$NEEDS_START" -eq 1 ]; then
  trap - EXIT
  echo "== ACCEPTANCE FAIL $FAILURES"
  echo "== final utc=$(utc_now)"
  exit 20
fi

event_after=$(latest_event_after)
if known_unsigned "$event_after" \
  && fetch_url "$BASE_URL/api/network/events?limit=6&after=$event_after"; then
  events_six=$FETCH_BODY
  if ! printf '%s' "$events_six" | print_events; then
    add_failure recovery_events
  elif ! printf '%s' "$events_six" | events_show_recovery; then
    echo "== recovery transition=Unknown"
    add_failure recovery_events
  else
    echo "== recovery transition=PASS"
  fi
else
  echo "== recovery events=Unknown"
  add_failure recovery_events
fi

window_cpu_start=$(service_value CPUUsageNSec)
window_db_start=$(db_size)
IFS='|' read -r window_samples_start window_events_start < <(db_counts)
window_utc=$(utc_now)
window_tick=$SECONDS
echo "== measure start utc=$window_utc window_s=$WINDOW_S cpu_usage_nsec=$window_cpu_start" \
  "db_bytes=$window_db_start samples=$window_samples_start events=$window_events_start"
if ! sleep "$WINDOW_S"; then
  add_failure measure_window
fi
window_elapsed=$((SECONDS - window_tick))
window_cpu_end=$(service_value CPUUsageNSec)
window_pid_end=$(service_value MainPID)
window_rss_end=$(rss_for_pid "$window_pid_end")
window_db_end=$(db_size)
IFS='|' read -r window_samples_end window_events_end < <(db_counts)
cpu_delta=$(signed_delta "$window_cpu_end" "$window_cpu_start")
cpu_average=$(cpu_percent "$window_cpu_end" "$window_cpu_start" "$window_elapsed")
db_delta=$(signed_delta "$window_db_end" "$window_db_start")
db_growth=$(growth_per_hour "$window_db_end" "$window_db_start" "$window_elapsed")
samples_delta=$(signed_delta "$window_samples_end" "$window_samples_start")
events_delta=$(signed_delta "$window_events_end" "$window_events_start")
cpu_result=FAIL
if float_less "$cpu_average" 1; then
  cpu_result=PASS
else
  add_failure measure_cpu
fi
rss_result=FAIL
if known_unsigned "$window_rss_end" && [ "$window_rss_end" -lt 61440 ]; then
  rss_result=PASS
else
  add_failure measure_rss
fi
db_result=FAIL
if known_unsigned "$window_db_end" && [ "$window_db_end" -lt 104857600 ]; then
  db_result=PASS
else
  add_failure measure_database
fi
echo "== measure cpu result=$cpu_result elapsed_s=$window_elapsed delta_nsec=$cpu_delta" \
  "average_one_core_pct=$cpu_average limit_pct=1"
echo "== measure rss result=$rss_result end_kib=$window_rss_end limit_kib=61440 main_pid=$window_pid_end"
echo "== measure database result=$db_result end_bytes=$window_db_end delta_bytes=$db_delta" \
  "growth_bytes_per_hour=$db_growth limit_bytes=104857600"
echo "== measure rows samples_start=$window_samples_start samples_end=$window_samples_end" \
  "samples_delta=$samples_delta events_start=$window_events_start" \
  "events_end=$window_events_end events_delta=$events_delta"
echo "== measure requests_api_network=SKIP reason=not_measurable_here"

benchmark_endpoint network "$BASE_URL/api/network" 100000 0.050
SCAN_NETWORK=$BENCH_BODY
benchmark_endpoint history \
  "$BASE_URL/api/network/history?edge_id=spot_rest&metric=rtt_ms&range=24h" \
  1000000 0.250
SCAN_HISTORY=$BENCH_BODY
benchmark_endpoint events "$BASE_URL/api/network/events?limit=200" none none
SCAN_EVENTS=$BENCH_BODY

scan_sources=0
[ -n "$SCAN_NETWORK" ] && scan_sources=$((scan_sources + 1))
[ -n "$SCAN_HISTORY" ] && scan_sources=$((scan_sources + 1))
[ -n "$SCAN_EVENTS" ] && scan_sources=$((scan_sources + 1))
if [ "$scan_sources" -ne 3 ]; then
  add_failure identifier_bodies
fi
for term in connectorId receipt_path job_id signature; do
  count=$(scan_count "$term")
  scan_result=PASS
  if [ "$scan_sources" -ne 3 ]; then
    scan_result=Unknown
  elif [ "$count" -ne 0 ]; then
    scan_result=FAIL
    add_failure "identifier_$term"
  fi
  echo "== identifiers term=$term count=$count bodies=$scan_sources/3 result=$scan_result"
done
secret_count=$(scan_count secret)
echo "== identifiers term=secret count=$secret_count bodies=$scan_sources/3" \
  "result=MARKED note=aws_secrets_labels_may_match"

if [ "$NEEDS_START" -eq 1 ]; then
  restore_service
fi
trap - EXIT
final_utc=$(utc_now)
if [ -n "$FAILURES" ]; then
  echo "== ACCEPTANCE FAIL $FAILURES"
  echo "== final utc=$final_utc"
  exit 20
fi
echo "== ACCEPTANCE PASS"
echo "== final utc=$final_utc"
exit 0
