#!/bin/bash
# Machine trigger — cron-fired on the execution host; no human, no chat.
# Every fire is safe by construction: the executor's lock, receipt gate,
# signed preflight, leverage gate, and plan gate all still bind. A STOP file
# halts everything; any reconcile that needs a human halts the loop itself.
set -u
D=/home/ssm-user/quant-run/wave2-validate-h2
R=/home/ssm-user/quant_receipts
[ -f "$D/STOP" ] && exit 0
exec >> "$D/auto.log" 2>&1
echo "=== $(date -u +%FT%TZ)"
SECRET_ID=$(grep -oE 'SECRET_ID = "[^"]+"' /home/ssm-user/quant-run/wave2-check-20260813-h1/w2check.py | cut -d'"' -f2)
SECRET_JSON=$(aws secretsmanager get-secret-value --secret-id "$SECRET_ID" --query SecretString --output text)
export BINANCE_API_KEY=$(printf '%s' "$SECRET_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["BINANCE_API_KEY"])')
export BINANCE_API_SECRET=$(printf '%s' "$SECRET_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["BINANCE_API_SECRET"])')
unset SECRET_JSON
export QUANT_SHA=81e999ad8630cd8098376ab57ff05f2ce5be854d
cd "$D/src"

# A held lock means an unreconciled run owns the lane: reconcile it first.
if [ -f "$R/basis-executor.lock" ]; then
  SYMBOL=$(python3 - <<'PY'
import glob, json, os
for path in sorted(glob.glob("/home/ssm-user/quant_receipts/basis-executor-*.json"), reverse=True):
    try:
        receipt = json.load(open(path))
    except Exception:
        continue
    if isinstance(receipt, dict) and receipt.get("kind") != "reconcile" \
            and receipt.get("verdict") == "OPEN_EXPOSURE" \
            and not receipt.get("reconciled_by"):
        rid = os.path.basename(path)[len("basis-executor-"):-len(".json")]
        for line in open("/home/ssm-user/quant_receipts/events.jsonl"):
            try:
                event = json.loads(line)
            except Exception:
                continue
            if event.get("run_id") == rid and event.get("symbol"):
                print(event["symbol"])
                raise SystemExit
        break
PY
)
  if [ -z "$SYMBOL" ]; then
    echo "lock held with no reconcilable source — halting auto for a human"
    touch "$D/STOP"
    exit 3
  fi
  export SYMBOL
  python3 basis_executor.py --reconcile
  RC=$?
  echo "reconcile $SYMBOL exit $RC"
  if [ "$RC" -ge 3 ]; then
    echo "reconcile needs a human — halting auto"
    touch "$D/STOP"
  fi
  exit $RC
fi

# Plumbing bar 5 bps until the first CLOSED cycle exists; economic 35 after.
if python3 - <<'PY'
import glob, json, sys
for path in glob.glob("/home/ssm-user/quant_receipts/basis-executor-*.json"):
    try:
        receipt = json.load(open(path))
    except Exception:
        continue
    if isinstance(receipt, dict) and receipt.get("verdict") == "CLOSED":
        sys.exit(0)
sys.exit(1)
PY
then MIN_BPS=35; else MIN_BPS=5; fi

BEST=$(python3 basis_scan.py --best-min-bps "$MIN_BPS" 2>/dev/null | grep '^BEST|')
echo "bar ${MIN_BPS}bps -> ${BEST:-scan failed}"
case "$BEST" in
  BEST\|NONE|"") exit 0 ;;
esac
export SYMBOL=$(printf '%s' "$BEST" | cut -d'|' -f2)
export MIN_ENTRY_BPS=$MIN_BPS
python3 set_leverage_1x.py || { echo "leverage set failed for $SYMBOL — skipping this fire"; exit 1; }
python3 basis_executor.py --execute
echo "execute $SYMBOL exit $?"
