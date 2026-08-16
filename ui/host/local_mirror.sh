#!/bin/bash
R=/home/ssm-user/quant_receipts
L=/home/ssm-user/quant-ui/ui/live
D=/home/ssm-user/quant-run/wave2-validate-h2
mkdir -p "$L/jobs" "$L/proposals"
while true; do
  start_ms=$(($(date +%s%N)/1000000))
  n=0
  for f in health.jsonl decisions.jsonl stream_state.json basis-position.json momentum-position.json momentum-paper-receipts.jsonl commission-rates.json; do
    [ -f "$R/$f" ] && cp -p "$R/$f" "$L/.tmp.$f" && mv "$L/.tmp.$f" "$L/$f" && n=$((n+1)) || rm -f "$L/$f"
  done
  [ -f "$R/jobs/results.jsonl" ] && cp -p "$R/jobs/results.jsonl" "$L/jobs/.tmp.r" && mv "$L/jobs/.tmp.r" "$L/jobs/results.jsonl" && n=$((n+1))
  [ -f "$D/jobs.log" ] && tail -n 2000 "$D/jobs.log" > "$L/.tmp.jl" && mv "$L/.tmp.jl" "$L/jobs.log" && n=$((n+1))
  find "$R" -maxdepth 1 -name '*.json' -newer "$L" 2>/dev/null | head -200 | while read -r p; do cp -p "$p" "$L/$(basename "$p")"; done
  for flag in STOP STOP-JOBS account-governor.lock; do
    if [ -e "$R/$flag" ]; then touch "$L/$flag"; else rm -f "$L/$flag"; fi
  done
  for g in "$R"/account-governor-*.jsonl; do [ -f "$g" ] && cp -p "$g" "$L/$(basename "$g")"; done
  end_ms=$(($(date +%s%N)/1000000))
  printf '{"utc":"%s","ok":true,"interval_ms":5000,"cycle_ms":%s,"files":%s}\n' "$(date -u +%FT%TZ)" "$((end_ms-start_ms))" "$n" > "$L/.tmp.manifest"
  mv "$L/.tmp.manifest" "$L/pull_manifest.json"
  sleep 5
done
