#!/bin/bash
# Cron target for one strictly validated live-test job.
set -eu
D=/home/ssm-user/quant-run/wave2-validate-h2
R=/home/ssm-user/quant_receipts
exec >> "$D/jobs.log" 2>&1
echo "=== $(date -u +%FT%TZ)"

SECRET_ID=$(grep -oE 'SECRET_ID = "[^"]+"' /home/ssm-user/quant-run/wave2-check-20260813-h1/w2check.py | cut -d'"' -f2)
[ -n "$SECRET_ID" ] || { echo "JOB-TICK | secret id unavailable"; exit 1; }
SECRET_JSON=$(aws secretsmanager get-secret-value --secret-id "$SECRET_ID" --query SecretString --output text)
export BINANCE_API_KEY="$(printf '%s' "$SECRET_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["BINANCE_API_KEY"])')"
export BINANCE_API_SECRET="$(printf '%s' "$SECRET_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["BINANCE_API_SECRET"])')"
unset SECRET_JSON
export QUANT_SHA=7ac26aa771937f802930dac5eb017d6583d968d8
export RECEIPT_DIR="$R"

cd "$D/src"
if python3 job_runner.py --tick; then
  RC=0
else
  RC=$?
fi
echo "JOB-TICK | exit $RC"
exit "$RC"
