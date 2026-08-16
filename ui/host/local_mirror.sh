#!/bin/bash
set -u

R="${QUANT_MIRROR_R:-/home/ssm-user/quant_receipts}"
L="${QUANT_MIRROR_L:-/home/ssm-user/quant-ui/ui/live}"
D="${QUANT_MIRROR_D:-/home/ssm-user/quant-run/wave2-validate-h2}"
mkdir -p "$L/jobs" "$L/proposals"

file_size() {
  stat -c %s "$1" 2>/dev/null || stat -f %z "$1"
}

copy_if_changed() {
  local source_path=$1
  local target_path=$2
  local temporary_path=$3
  local source_size
  local target_size

  if [ -f "$target_path" ] && [ ! "$source_path" -nt "$target_path" ]; then
    source_size=$(file_size "$source_path") || return 1
    target_size=$(file_size "$target_path") || return 1
    [ "$source_size" != "$target_size" ] || return 0
  fi
  if cp -p "$source_path" "$temporary_path" && mv "$temporary_path" "$target_path"; then
    n=$((n+1))
    return 0
  fi
  rm -f "$temporary_path"
  return 1
}

while true; do
  start_ms=$(($(date +%s%N)/1000000))
  n=0
  for f in health.jsonl decisions.jsonl stream_state.json basis-position.json momentum-position.json momentum-paper-receipts.jsonl commission-rates.json; do
    if [ -f "$R/$f" ]; then
      copy_if_changed "$R/$f" "$L/$f" "$L/.tmp.$f" || rm -f "$L/$f"
    else
      rm -f "$L/$f"
    fi
  done
  if [ -f "$R/jobs/results.jsonl" ]; then
    copy_if_changed "$R/jobs/results.jsonl" "$L/jobs/results.jsonl" "$L/jobs/.tmp.r"
  fi
  if [ -f "$D/jobs.log" ] && [ "$D/jobs.log" -nt "$L/jobs.log" ]; then
    if tail -n 2000 "$D/jobs.log" > "$L/.tmp.jl" && mv "$L/.tmp.jl" "$L/jobs.log"; then
      n=$((n+1))
    else
      rm -f "$L/.tmp.jl"
    fi
  fi
  while IFS= read -r p; do
    name=$(basename "$p")
    copy_if_changed "$p" "$L/$name" "$L/.tmp.$name"
  done < <(find "$R" -maxdepth 1 -type f -name '*.json' 2>/dev/null | head -200)
  for flag in STOP STOP-JOBS account-governor.lock; do
    if [ -e "$R/$flag" ]; then
      [ -e "$L/$flag" ] || touch "$L/$flag"
    else
      [ ! -e "$L/$flag" ] || rm -f "$L/$flag"
    fi
  done
  for g in "$R"/account-governor-*.jsonl; do
    if [ -f "$g" ]; then
      name=$(basename "$g")
      copy_if_changed "$g" "$L/$name" "$L/.tmp.$name"
    fi
  done
  end_ms=$(($(date +%s%N)/1000000))
  printf '{"utc":"%s","ok":true,"interval_ms":5000,"cycle_ms":%s,"files":%s,"errors":[]}\n' "$(date -u +%FT%TZ)" "$((end_ms-start_ms))" "$n" > "$L/.tmp.manifest"
  mv "$L/.tmp.manifest" "$L/pull_manifest.json"
  [ "${QUANT_MIRROR_ONCE:-0}" = 1 ] && exit 0
  sleep 5
done
