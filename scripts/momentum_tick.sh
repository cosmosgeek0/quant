#!/bin/bash
# Momentum decision tick — cron-fired on the host. PUBLIC reads + job-record
# writes ONLY (no credentials). Both STOP files hold it: no decisions while
# the strategy STOP or the queue STOP-JOBS is present. The owner-armed job
# runner remains the only money path.
set -u
D=/home/ssm-user/quant-run/wave2-validate-h2
R=/home/ssm-user/quant_receipts
[ -f "$R/STOP" ] && exit 0
[ -f "$R/STOP-JOBS" ] && exit 0
[ -f "$D/STOP" ] && exit 0
exec >> "$D/momentum.log" 2>&1
echo "=== $(date -u +%FT%TZ)"
cd "$D/src"
PYTHONDONTWRITEBYTECODE=1 python3 market_stream.py --once \
  --state-file "$R/watch/stream_state.json" \
  --log-file "$R/watch/stream_log.jsonl" \
  --windows-file "$R/watch/stream_windows.json" | tail -2
PYTHONDONTWRITEBYTECODE=1 python3 momentum_live.py \
  --tick "$R/watch/stream_state.json" \
  --state "$R/watch/momentum_live_state.json" \
  --decisions "$R/watch/decisions.jsonl" \
  --jobs-dir "$R/jobs" \
  --max-freshness-ms 90000 | tail -3
echo "momentum tick exit $?"
