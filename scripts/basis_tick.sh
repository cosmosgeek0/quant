#!/bin/bash
# Supervisor tick — cron-fired. STOP blocks entry inside the supervisor;
# reconcile/check/close always run. No STOP removal happens here, ever.
set -u
D=/home/ssm-user/quant-run/wave2-validate-h2
exec >> "$D/supervisor.log" 2>&1
echo "=== $(date -u +%FT%TZ)"
SECRET_ID=$(grep -oE 'SECRET_ID = "[^"]+"' /home/ssm-user/quant-run/wave2-check-20260813-h1/w2check.py | cut -d'"' -f2)
SECRET_JSON=$(aws secretsmanager get-secret-value --secret-id "$SECRET_ID" --query SecretString --output text)
export BINANCE_API_KEY=$(printf '%s' "$SECRET_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["BINANCE_API_KEY"])')
export BINANCE_API_SECRET=$(printf '%s' "$SECRET_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["BINANCE_API_SECRET"])')
unset SECRET_JSON
export QUANT_SHA=__RELEASE_SHA__
cd "$D/src"
python3 basis_supervisor.py --tick
echo "tick exit $?"
