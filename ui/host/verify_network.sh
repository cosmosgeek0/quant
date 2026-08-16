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
  rmdir "$tmp_dir" 2>/dev/null
}
trap cleanup EXIT

failed=0
read_file_fact() {
  fact_path=$1
  FACT_SHA=Unknown
  FACT_BYTES=Unknown
  FACT_MTIME=Unknown
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
}

read_file_fact "$SOURCE_FEED"
source_sha=$FACT_SHA
echo "== source feed sha=$FACT_SHA bytes=$FACT_BYTES mtime=$FACT_MTIME"
read_file_fact "$MIRROR_FEED"
mirror_sha=$FACT_SHA
echo "== mirror feed sha=$FACT_SHA bytes=$FACT_BYTES mtime=$FACT_MTIME"
if [ "$source_sha" = "Unknown" ] || [ "$mirror_sha" = "Unknown" ]; then
  echo "== feed sha_match Unknown"
elif [ "$source_sha" = "$mirror_sha" ]; then
  echo "== feed sha_match yes"
else
  echo "== feed sha_match no"
fi

echo "== feed meta"
if [ -f "$FEED_META" ]; then
  if cat "$FEED_META"; then
    echo
  else
    echo "Unknown"
    failed=1
  fi
else
  echo "Unknown"
  failed=1
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
benchmark /api/network "$network_url" "$tmp_dir/network.json"
benchmark /api/network/history-24h "$history_url" "$tmp_dir/history.json"

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

if [ -f "$tmp_dir/network.json" ]; then
  for word in secret key signature connectorId; do
    count=$(grep -F -c "$word" "$tmp_dir/network.json")
    echo "== network grep $word $count"
  done
else
  echo "== network grep secret Unknown"
  echo "== network grep key Unknown"
  echo "== network grep signature Unknown"
  echo "== network grep connectorId Unknown"
  failed=1
fi

if [ "$failed" -ne 0 ]; then
  echo "== network verification incomplete"
  exit 10
fi
echo "== network verification complete"
exit 0
