#!/bin/sh
# Host-side brain-pulse watchdog: SILENCE IS AN ALARM (owner order 2026-08-15).
R=/home/ssm-user/quant_receipts
H=$R/health.jsonl
now=$(date -u +%s)
ts=$(date -u +%FT%TZ)
age() { f=$1; [ -f "$f" ] && echo $(( now - $(stat -c %Y "$f") )) || echo 999999; }
DEC_AGE=$(age "$R/decisions.jsonl")
FEED_AGE=$(age "$R/stream_state.json")
sealed=0; [ -e "$R/STOP" ] && sealed=1
ALARM=$R/ALARM-brain-stale
if [ "$sealed" -eq 0 ] && [ "$DEC_AGE" -gt 900 ]; then
  printf '{"stage":"aliveness","alarm":"brain_stale","decisions_age_s":%s,"utc":"%s"}\n' "$DEC_AGE" "$ts" >> "$H"
  printf '{"utc":"%s","decisions_age_s":%s}\n' "$ts" "$DEC_AGE" > "$ALARM"
elif [ "$sealed" -eq 0 ] && [ "$FEED_AGE" -gt 900 ]; then
  printf '{"stage":"aliveness","alarm":"feed_stale","feed_age_s":%s,"utc":"%s"}\n' "$FEED_AGE" "$ts" >> "$H"
  printf '{"utc":"%s","feed_age_s":%s}\n' "$ts" "$FEED_AGE" > "$ALARM"
else
  printf '{"stage":"aliveness","ok":true,"decisions_age_s":%s,"feed_age_s":%s,"utc":"%s"}\n' "$DEC_AGE" "$FEED_AGE" "$ts" >> "$H"
  rm -f "$ALARM"
fi
