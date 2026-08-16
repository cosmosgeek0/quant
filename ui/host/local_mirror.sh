#!/bin/bash
set -u

R="${QUANT_MIRROR_R:-/home/ssm-user/quant_receipts}"
L="${QUANT_MIRROR_L:-/home/ssm-user/quant-ui/ui/live}"
D="${QUANT_MIRROR_D:-/home/ssm-user/quant-run/wave2-validate-h2}"
mkdir -p "$L/jobs" "$L/proposals"

feed_tail_complete() {
  feed_tail=$(tail -c 64 "$1" 2>/dev/null) || return 1
  case "$feed_tail" in
    *'"written_ms"'*) ;;
    *) return 1 ;;
  esac
  feed_last=$(printf '%s' "$feed_tail" | tr -d '[:space:]' | tail -c 1)
  [ "$feed_last" = "}" ]
}

while true; do
  start_ms=$(($(date +%s%N)/1000000))
  n=0
  for f in health.jsonl decisions.jsonl stream_state.json basis-position.json momentum-position.json momentum-paper-receipts.jsonl commission-rates.json; do
    [ -f "$R/$f" ] && cp -p "$R/$f" "$L/.tmp.$f" && mv "$L/.tmp.$f" "$L/$f" && n=$((n+1)) || rm -f "$L/$f"
  done
  feed="$R/watch/feed_state.json"; feed_error=
  feed_mtime_json=null; feed_bytes_json=null; feed_sha_json=null; feed_copied_json=null
  if [ ! -f "$feed" ]; then feed_error="feed_state missing"
  elif [ ! -s "$feed" ]; then feed_error="feed_state empty"
  # V1 uses head/tail sentinels for truncated writes; full parsing every 5 s costs about 2% of one core, and malformed copies are Unknown.
  elif [ "$(head -c 1 "$feed" 2>/dev/null)" != "{" ] || ! feed_tail_complete "$feed"; then
    feed_error="feed_state incomplete"
  elif ! cp -p "$feed" "$L/.tmp.feed_state.json"; then
    feed_error="feed_state copy failed"; rm -f "$L/.tmp.feed_state.json"
  elif ! feed_tail_complete "$L/.tmp.feed_state.json"; then
    feed_error="feed_state incomplete"; rm -f "$L/.tmp.feed_state.json"
  elif mv "$L/.tmp.feed_state.json" "$L/feed_state.json"; then
    feed_epoch=$(stat -c %Y "$L/feed_state.json" 2>/dev/null || stat -f %m "$L/feed_state.json")
    feed_mtime_json="\"$(date -u -d "@$feed_epoch" +%FT%TZ 2>/dev/null || date -u -r "$feed_epoch" +%FT%TZ)\""
    feed_bytes_json=$(stat -c %s "$L/feed_state.json" 2>/dev/null || stat -f %z "$L/feed_state.json")
    feed_sha_json="\"$(sha256sum "$L/feed_state.json" | cut -d' ' -f1)\""
    feed_copied_json="\"$(date -u +%FT%TZ)\""
    n=$((n+1))
  else
    feed_error="feed_state copy failed"; rm -f "$L/.tmp.feed_state.json"
  fi
  feed_error_json=null; errors_json='[]'; ok=true; if [ -n "$feed_error" ]; then
    feed_error_json="\"$feed_error\""; errors_json="[\"$feed_error\"]"; ok=false
  fi
  feed_meta_format='{"source":"watch/feed_state.json","source_mtime_utc":%s'
  feed_meta_format="${feed_meta_format}"',"source_bytes":%s,"sha256":%s'
  feed_meta_format="${feed_meta_format}"',"copied_utc":%s,"error":%s}\n'
  printf "$feed_meta_format" \
    "$feed_mtime_json" "$feed_bytes_json" "$feed_sha_json" \
    "$feed_copied_json" "$feed_error_json" > "$L/.tmp.feed_state.meta.json" \
    && mv "$L/.tmp.feed_state.meta.json" "$L/feed_state.meta.json"
  [ -f "$R/jobs/results.jsonl" ] && cp -p "$R/jobs/results.jsonl" "$L/jobs/.tmp.r" && mv "$L/jobs/.tmp.r" "$L/jobs/results.jsonl" && n=$((n+1))
  [ -f "$D/jobs.log" ] && tail -n 2000 "$D/jobs.log" > "$L/.tmp.jl" && mv "$L/.tmp.jl" "$L/jobs.log" && n=$((n+1))
  find "$R" -maxdepth 1 -name '*.json' -newer "$L" 2>/dev/null | head -200 | while read -r p; do cp -p "$p" "$L/$(basename "$p")"; done
  for flag in STOP STOP-JOBS account-governor.lock; do
    if [ -e "$R/$flag" ]; then touch "$L/$flag"; else rm -f "$L/$flag"; fi
  done
  for g in "$R"/account-governor-*.jsonl; do [ -f "$g" ] && cp -p "$g" "$L/$(basename "$g")"; done
  end_ms=$(($(date +%s%N)/1000000))
  printf '{"utc":"%s","ok":%s,"interval_ms":5000,"cycle_ms":%s,"files":%s,"errors":%s}\n' "$(date -u +%FT%TZ)" "$ok" "$((end_ms-start_ms))" "$n" "$errors_json" > "$L/.tmp.manifest"
  mv "$L/.tmp.manifest" "$L/pull_manifest.json"
  [ "${QUANT_MIRROR_ONCE:-0}" = 1 ] && exit 0
  sleep 5
done
