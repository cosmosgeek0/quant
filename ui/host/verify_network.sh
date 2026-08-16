#!/bin/bash
set -u

ROOT=/home/ssm-user/quant-ui
SOURCE_FEED=/home/ssm-user/quant_receipts/watch/feed_state.json
MIRROR_FEED=ui/live/feed_state.json
FEED_META=ui/live/feed_state.meta.json

if ! cd "$ROOT"; then
  echo "== verify root Unknown"
  exit 2
fi

tmp_dir=$(mktemp -d /tmp/quant-ui-network-verify.XXXXXX)
tmp_rc=$?
if [ "$tmp_rc" -ne 0 ] || [ -z "$tmp_dir" ]; then
  echo "== verify temporary directory Unknown"
  exit 3
fi

cleanup() {
  rm -f "$tmp_dir/curl.err" "$tmp_dir/network.json" "$tmp_dir/network.json.next"
  rm -f "$tmp_dir/history.json" "$tmp_dir/history.json.next"
  rm -f "$tmp_dir/events.json" "$tmp_dir/events.json.next"
  rmdir "$tmp_dir" 2>/dev/null
}
trap cleanup EXIT

failed=0
read_file_fact() {
  fact_path=$1
  FACT_SHA=Unknown
  FACT_BYTES=Unknown
  FACT_MTIME=Unknown
  FACT_MTIME_EPOCH=Unknown
  if [ ! -f "$fact_path" ]; then
    failed=1
    return
  fi
  sha_row=$(sha256sum "$fact_path" 2>/dev/null)
  sha_rc=$?
  if [ "$sha_rc" -eq 0 ]; then
    FACT_SHA=${sha_row%% *}
  else
    failed=1
  fi
  FACT_BYTES=$(stat -c %s "$fact_path" 2>/dev/null)
  bytes_rc=$?
  if [ "$bytes_rc" -ne 0 ] || [ -z "$FACT_BYTES" ]; then
    FACT_BYTES=Unknown
    failed=1
  fi
  FACT_MTIME=$(stat -c %y "$fact_path" 2>/dev/null)
  mtime_rc=$?
  if [ "$mtime_rc" -ne 0 ] || [ -z "$FACT_MTIME" ]; then
    FACT_MTIME=Unknown
    failed=1
  fi
  FACT_MTIME_EPOCH=$(stat -c %Y "$fact_path" 2>/dev/null)
  mtime_epoch_rc=$?
  case "$FACT_MTIME_EPOCH" in
    ''|*[!0-9]*)
      FACT_MTIME_EPOCH=Unknown
      failed=1
      ;;
  esac
  if [ "$mtime_epoch_rc" -ne 0 ]; then
    FACT_MTIME_EPOCH=Unknown
    failed=1
  fi
}

read_file_fact "$SOURCE_FEED"
source_sha=$FACT_SHA
source_mtime_epoch=$FACT_MTIME_EPOCH
echo "== source feed sha=$FACT_SHA bytes=$FACT_BYTES mtime=$FACT_MTIME"
read_file_fact "$MIRROR_FEED"
mirror_sha=$FACT_SHA
mirror_mtime_epoch=$FACT_MTIME_EPOCH
echo "== mirror feed sha=$FACT_SHA bytes=$FACT_BYTES mtime=$FACT_MTIME"
if [ "$source_sha" = "Unknown" ] || [ "$mirror_sha" = "Unknown" ]; then
  echo "== feed sha_match Unknown"
elif [ "$source_sha" = "$mirror_sha" ]; then
  echo "== feed sha_match yes"
else
  echo "== feed sha_match no"
fi

echo "== feed meta"
meta_sha=Unknown
if [ -f "$FEED_META" ]; then
  if cat "$FEED_META"; then
    echo
  else
    echo "Unknown"
    failed=1
  fi
  meta_sha=$(python3 -c 'import json,sys; value=json.load(sys.stdin).get("sha256"); print(value if isinstance(value,str) else "")' < "$FEED_META" 2>/dev/null)
  meta_rc=$?
  if [ "$meta_rc" -ne 0 ] || [ -z "$meta_sha" ]; then
    meta_sha=Unknown
    failed=1
  fi
else
  echo "Unknown"
  failed=1
fi

if [ "$meta_sha" = "Unknown" ] || [ "$mirror_sha" = "Unknown" ]; then
  echo "== feed meta_sha_match Unknown"
  failed=1
elif [ "$meta_sha" = "$mirror_sha" ]; then
  echo "== feed meta_sha_match yes"
else
  echo "== feed meta_sha_match no"
  failed=1
fi

if [ "$source_mtime_epoch" = "Unknown" ] || [ "$mirror_mtime_epoch" = "Unknown" ]; then
  echo "== feed mirror_lag_s Unknown"
  failed=1
else
  feed_lag_s=$((source_mtime_epoch - mirror_mtime_epoch))
  echo "== feed mirror_lag_s $feed_lag_s"
  if [ "$feed_lag_s" -gt 30 ]; then
    failed=1
  fi
fi

summary_code='import json,sys; d=json.load(open(sys.argv[1], encoding="utf-8")); '
summary_code="${summary_code}"'c=d.get("collector") or {}; '
summary_code="${summary_code}"'print("== network", d.get("schema_version"), d.get("generated_at"), c.get("status")); '
summary_code="${summary_code}"'[print("== edge", e.get("id"), e.get("health"), e.get("observed_at")) for e in d.get("edges", [])]'
if ! python3 -c "$summary_code" ui/live/network_current.json 2>/dev/null; then
  echo "== network summary Unknown"
  failed=1
fi

benchmark() {
  bench_label=$1
  bench_url=$2
  body_path=$3
  max_body_bytes=$4
  max_bytes=
  max_time=
  ok_runs=0
  run=1
  while [ "$run" -le 10 ]; do
    attempt_path="${body_path}.next"
    result=$(curl -sS --max-time 10 -o "$attempt_path" -w '%{http_code} %{size_download} %{time_total}' "$bench_url" 2>>"$tmp_dir/curl.err")
    curl_rc=$?
    if [ "$curl_rc" -ne 0 ]; then
      echo "== $bench_label run=$run Unknown"
      rm -f "$attempt_path"
      failed=1
      run=$((run + 1))
      continue
    fi
    set -- $result
    code=${1-Unknown}
    bytes=${2-Unknown}
    time_total=${3-Unknown}
    if [ "$code" != "200" ]; then
      echo "== $bench_label run=$run code=$code"
      rm -f "$attempt_path"
      failed=1
      run=$((run + 1))
      continue
    fi
    if ! mv "$attempt_path" "$body_path"; then
      echo "== $bench_label body Unknown"
      failed=1
      run=$((run + 1))
      continue
    fi
    if [ "$max_body_bytes" -gt 0 ] && [ "$bytes" -gt "$max_body_bytes" ]; then
      echo "== $bench_label run=$run bytes=$bytes limit=$max_body_bytes"
      failed=1
    fi
    ok_runs=$((ok_runs + 1))
    if [ -z "$max_bytes" ] || [ "$bytes" -gt "$max_bytes" ]; then
      max_bytes=$bytes
    fi
    if [ -z "$max_time" ]; then
      max_time=$time_total
    else
      max_time=$(awk -v old="$max_time" -v new="$time_total" 'BEGIN {if (new > old) print new; else print old}')
    fi
    run=$((run + 1))
  done
  if [ "$ok_runs" -eq 0 ]; then
    echo "== $bench_label runs=0/10 max_bytes=Unknown max_time_total=Unknown"
  else
    echo "== $bench_label runs=$ok_runs/10 max_bytes=$max_bytes max_time_total=$max_time"
  fi
}

network_url=http://127.0.0.1:8080/api/network
history_url='http://127.0.0.1:8080/api/network/history?edge_id=spot_rest&metric=status&range=24h'
events_url='http://127.0.0.1:8080/api/network/events?limit=200'
benchmark /api/network "$network_url" "$tmp_dir/network.json" 100000
benchmark /api/network/history-24h "$history_url" "$tmp_dir/history.json" 1000000
benchmark /api/network/events "$events_url" "$tmp_dir/events.json" 300000

pid=$(systemctl show quant-ui-network -p MainPID --value 2>/dev/null)
pid_rc=$?
case "$pid" in
  ''|0|*[!0-9]*)
    echo "== recorder cpu_rss Unknown"
    failed=1
    ;;
  *)
    echo "== recorder ps %cpu rss"
    if [ "$pid_rc" -ne 0 ] || ! ps -o %cpu,rss -p "$pid"; then
      echo "== recorder cpu_rss Unknown"
      failed=1
    fi
    ;;
esac

du_row=$(du -sh data/ 2>/dev/null)
du_rc=$?
if [ "$du_rc" -eq 0 ] && [ -n "$du_row" ]; then
  echo "== data $du_row"
else
  echo "== data Unknown"
  failed=1
fi

scan_identifiers() {
  scan_label=$1
  scan_body=$2
  if [ ! -f "$scan_body" ]; then
    echo "== $scan_label identifier scan Unknown"
    failed=1
    return
  fi
  for word in connectorId receipt_path job_id '"signature"'; do
    if grep -F -q "$word" "$scan_body"; then
      echo "== $scan_label identifier $word present"
      failed=1
    else
      echo "== $scan_label identifier $word absent"
    fi
  done
}

scan_identifiers network "$tmp_dir/network.json"
scan_identifiers history "$tmp_dir/history.json"
scan_identifiers events "$tmp_dir/events.json"

if [ "$failed" -ne 0 ]; then
  echo "== VERIFY FAILED"
  exit 20
fi
echo "== network verification complete"
exit 0
