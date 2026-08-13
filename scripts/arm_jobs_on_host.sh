#!/bin/bash
# Owner action: install the one-minute job trigger and clear only its stop file.
set -eu
D=/home/ssm-user/quant-run/wave2-validate-h2
R=/home/ssm-user/quant_receipts
CRON_LINE="* * * * * $D/job_tick.sh"

mkdir -p "$R/jobs"
chmod 700 "$R/jobs"
chmod 700 "$D/job_tick.sh" "$D/arm_jobs_on_host.sh" "$D/stop_jobs_on_host.sh"
rm -f "$R/STOP-JOBS"
(
  crontab -l 2>/dev/null | grep -Fv "$D/job_tick.sh" || true
  printf '%s\n' "$CRON_LINE"
) | crontab -

echo "ARMED"
crontab -l | grep -Fx "$CRON_LINE"
