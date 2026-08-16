#!/bin/bash
set -u

ROOT=/home/ssm-user/quant-ui

if [ "$#" -ne 1 ] || [ -z "${1-}" ]; then
  echo "== deploy usage: deploy_network.sh <release>"
  exit 2
fi

release=$1
if ! cd "$ROOT"; then
  echo "== deploy root Unknown"
  exit 3
fi

if ! mkdir -p data; then
  echo "== data directory Unknown"
  exit 4
fi
if ! printf '%s\n' "$release" > ui/RELEASE; then
  echo "== ui/RELEASE Unknown"
  exit 5
fi
echo "== ui/RELEASE written"

network_files=0
for file in ui/network_*.py; do
  if [ ! -f "$file" ]; then
    continue
  fi
  sha_row=$(sha256sum "$file" 2>/dev/null)
  sha_rc=$?
  digest=${sha_row%% *}
  if [ "$sha_rc" -ne 0 ] || [ "${#digest}" -lt 16 ]; then
    echo "== $file sha256 Unknown"
    exit 6
  fi
  echo "== $file ${digest:0:16}"
  network_files=$((network_files + 1))
done
if [ "$network_files" -eq 0 ]; then
  echo "== ui/network_*.py Unknown"
  exit 7
fi

mirror_source=ui/host/local_mirror.sh
mirror_target=/home/ssm-user/quant-ui/local_mirror.sh
if [ ! -f "$mirror_source" ]; then
  echo "== $mirror_source Unknown"
  exit 8
fi
sha_row=$(sha256sum "$mirror_source" 2>/dev/null)
sha_rc=$?
digest=${sha_row%% *}
if [ "$sha_rc" -ne 0 ] || [ "${#digest}" -lt 16 ]; then
  echo "== $mirror_source sha256 Unknown"
  exit 9
fi
echo "== $mirror_source ${digest:0:16}"

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
if [ -e "$mirror_target" ]; then
  mirror_backup="${mirror_target}.bak-${timestamp}"
  if ! cp -p "$mirror_target" "$mirror_backup"; then
    echo "== mirror backup Unknown"
    exit 10
  fi
  echo "== mirror backup $mirror_backup"
fi
if ! cp "$mirror_source" "$mirror_target"; then
  echo "== mirror install Unknown"
  exit 11
fi
echo "== mirror installed $mirror_target"
if ! sudo -n systemctl restart quant-ui-mirror; then
  echo "== quant-ui-mirror restart Unknown"
  exit 12
fi

unit_source=ui/host/quant-ui-network.service
unit_target=/etc/systemd/system/quant-ui-network.service
if ! sudo -n install -m 644 "$unit_source" "$unit_target"; then
  echo "== quant-ui-network unit install Unknown"
  exit 13
fi
if ! sudo -n systemctl daemon-reload; then
  echo "== systemd daemon-reload Unknown"
  exit 14
fi
if ! sudo -n systemctl enable --now quant-ui-network; then
  echo "== quant-ui-network enable Unknown"
  exit 15
fi

sleep 8
failed=0
for service in quant-ui quant-ui-mirror quant-ui-network quant-ui-telemetry quant-ui-news cloudflared; do
  state=$(systemctl is-active "$service" 2>/dev/null)
  state_rc=$?
  if [ -z "$state" ]; then
    state=Unknown
  fi
  echo "== $service $state"
  if [ "$state_rc" -ne 0 ] || [ "$state" != "active" ]; then
    failed=1
  fi
done

for file in ui/live/feed_state.json ui/live/feed_state.meta.json ui/live/network_current.json data/network_history.sqlite3; do
  echo "== ls -la $file"
  if ! ls -la "$file"; then
    failed=1
  fi
done

print_head() {
  head_file=$1
  echo "== head -c 400 $head_file"
  if [ -f "$head_file" ]; then
    head -c 400 "$head_file"
    echo
  else
    echo "Unknown"
    failed=1
  fi
}

print_head ui/live/network_current.json
print_head ui/live/pull_manifest.json

samples=$(python3 -c \
  'import sqlite3; d=sqlite3.connect("file:data/network_history.sqlite3?mode=ro", uri=True); print(d.execute("SELECT COUNT(*) FROM samples").fetchone()[0]); d.close()' 2>/dev/null)
samples_rc=$?
if [ "$samples_rc" -eq 0 ]; then
  echo "== sqlite samples $samples"
else
  echo "== sqlite samples Unknown"
  failed=1
fi
events=$(python3 -c \
  'import sqlite3; d=sqlite3.connect("file:data/network_history.sqlite3?mode=ro", uri=True); print(d.execute("SELECT COUNT(*) FROM events").fetchone()[0]); d.close()' 2>/dev/null)
events_rc=$?
if [ "$events_rc" -eq 0 ]; then
  echo "== sqlite events $events"
else
  echo "== sqlite events Unknown"
  failed=1
fi

if [ "$failed" -ne 0 ]; then
  echo "== deploy verification incomplete"
  exit 20
fi
echo "== deploy verification complete"
exit 0
