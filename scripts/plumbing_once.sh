#!/bin/bash
# One capped basis plumbing sequence — HOST-EXECUTED on the owner's trigger.
# Steps: leverage 1x (one-shot, proven) -> receipted 9 USDT Spot->USD-M
# transfer -> full signed validate -> ONE capped basis cycle. Stops at the
# first step that does not prove out. Nothing here retries.
set -u
D=/home/ssm-user/quant-run/wave2-validate-h2
SECRET_ID=$(grep -oE 'SECRET_ID = "[^"]+"' /home/ssm-user/quant-run/wave2-check-20260813-h1/w2check.py | cut -d'"' -f2)
SECRET_JSON=$(aws secretsmanager get-secret-value --secret-id "$SECRET_ID" --query SecretString --output text)
export BINANCE_API_KEY=$(printf '%s' "$SECRET_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["BINANCE_API_KEY"])')
export BINANCE_API_SECRET=$(printf '%s' "$SECRET_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["BINANCE_API_SECRET"])')
unset SECRET_JSON
export SYMBOL=BNBUSDT
export QUANT_SHA=4971faadcb0fb07f43fb8e5f0e207e5800ca040f
cd $D/src
echo "STEP 1 | leverage exactly 1x"
python3 set_leverage_1x.py || { echo "SEQUENCE STOPPED at leverage"; exit 1; }
echo "STEP 2 | receipted transfer 9 USDT spot-to-usdm"
python3 wallet.py transfer spot-to-usdm --asset USDT --amount 9 --execute-internal-transfer || { echo "SEQUENCE STOPPED at transfer"; exit 1; }
echo "STEP 3 | complete signed validate"
python3 basis_executor.py --validate-only
V=$?
if [ "$V" -eq 5 ]; then echo "SEQUENCE STOPPED | preflight refused"; exit 5; fi
if [ "$V" -eq 2 ]; then echo "ACCOUNT ARMED | account proofs held, no market edge this minute | press the trigger again later"; exit 2; fi
echo "STEP 4 | one capped basis cycle"
python3 basis_executor.py --execute
E=$?
echo "CYCLE EXIT $E"
exit $E
