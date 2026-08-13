#!/bin/bash
# Stop only live-test jobs; the strategy STOP file remains independent.
set -eu
D=/home/ssm-user/quant-run/wave2-validate-h2
R=/home/ssm-user/quant_receipts

mkdir -p "$R"
touch "$R/STOP-JOBS"
chmod 600 "$R/STOP-JOBS"
(
  crontab -l 2>/dev/null | grep -Fv "$D/job_tick.sh" || true
) | crontab -

if crontab -l 2>/dev/null | grep -Fq "$D/job_tick.sh"; then
  CRON_STATE=PRESENT
else
  CRON_STATE=REMOVED
fi
echo "STOP-JOBS=PRESENT"
echo "CRON=$CRON_STATE"
