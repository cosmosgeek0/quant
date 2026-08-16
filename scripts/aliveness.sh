#!/bin/sh
# Host-side brain-pulse watchdog: SILENCE IS AN ALARM (owner order 2026-08-15).
R=${RECEIPT_DIR:-/home/ssm-user/quant_receipts}
WATCH_DIR=${WATCH_DIR:-$R/watch}
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
python3 - "$WATCH_DIR/feed_state.json" "$R/ALARM-feed-blind" "$now" "$ts" <<'PY' >> "$H"
import json, sys; from pathlib import Path
feed, alarm = map(Path, sys.argv[1:3]); now = int(sys.argv[3]); ts = sys.argv[4]
try:
    if not isinstance(payload := json.loads(feed.read_text(encoding="utf-8")), dict): raise ValueError
    written = payload.get("written_ms")
    if isinstance(written, bool): raise ValueError
    written = int(written)
    if written < 0 or written > (now + 5) * 1000: raise ValueError
    feed_age = max(0, (now * 1000 - written + 999) // 1000)
except (OSError, TypeError, ValueError):
    payload = {}; feed_age = 999999
symbols = payload.get("symbols", {}) if isinstance(payload.get("symbols"), dict) else {}
connections = payload.get("connections", {}) if isinstance(payload.get("connections"), dict) else {}
word = lambda value, fallback: value if isinstance(value, str) and value else fallback
facts = {"feed_age_s": feed_age}; reasons = ["file_stale"] if feed_age > 180 else []
for venue in ("spot", "perp"):
    state = connections.get(venue, {})
    state = state if isinstance(state, dict) else {}
    status = word(state.get("status"), "missing"); ticker = state.get("ticker")
    ticker_status = word(ticker.get("status"), "missing") if isinstance(ticker, dict) else "absent"
    count = sum(isinstance(row, dict) and row.get(venue + "_bid") is not None and row.get(venue + "_ask") is not None for row in symbols.values())
    marker = feed.with_name(".feed_eyes-" + venue + "-bad")
    bad = status in {"broken", "stale"} or ticker_status in {"broken", "stale"}
    if bad:
        try: marker.touch(mode=0o600, exist_ok=False)
        except FileExistsError: pass
        bad_age = max(0, now - int(marker.stat().st_mtime))
    else:
        marker.unlink(missing_ok=True); bad_age = 0
    facts.update({venue + "_status": status, venue + "_ticker_status": ticker_status, venue + "_with_ticker": count, venue + "_bad_age_s": bad_age})
    if bad_age > 900: reasons.append(venue + "_status_long")
    if feed_age <= 180 and count == 0: reasons.append(venue + "_ticker_empty")
row = {"stage": "feed_eyes", "ok": not reasons, **facts, "utc": ts}
if reasons: row.update({"alarm": "feed_blind", "reasons": reasons})
print(json.dumps(row, sort_keys=True, separators=(",", ":")))
if reasons: alarm.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
else: alarm.unlink(missing_ok=True)
PY
