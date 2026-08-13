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
export QUANT_SHA=b08a18d956698072c8ead5102a6deb2c57c66c55
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

# Anti-bleed cooldown: a symbol that failed recently is skipped, not retried.
if find "$D" -maxdepth 1 -name "cooldown-$SYMBOL" -mmin -45 2>/dev/null | grep -q .; then
  echo "cooldown active for $SYMBOL — skipping this fire"
  exit 0
fi

# Margin headroom: the exchange reserves ~notional x band for a market short
# (measured -2019 on 2026-08-13). Top up USD-M from Spot inside the funded
# sub-account — ordinary receipted internal mechanics — before executing.
TOPUP=$(python3 - <<'PY'
import json, os, sys, urllib.request
from decimal import Decimal, ROUND_UP
sys.path.insert(0, ".")
from basis_executor import live_request_factory
symbol = os.environ["SYMBOL"]
request = live_request_factory(os.environ["BINANCE_API_KEY"],
                               os.environ["BINANCE_API_SECRET"])
kind, account = request("GET", "https://fapi.binance.com",
                        "/fapi/v3/account", None, True)
if kind != "ok" or not isinstance(account, dict):
    print("READ_FAILED")
    raise SystemExit
available = Decimal(str(account.get("availableBalance", "0")))
info = json.load(urllib.request.urlopen(
    "https://fapi.binance.com/fapi/v1/exchangeInfo?symbol=" + symbol,
    timeout=20))
band = Decimal("0")
for row in info["symbols"][0]["filters"]:
    if row["filterType"] == "PERCENT_PRICE":
        band = max(band, Decimal(str(row.get("multiplierUp", "0"))))
    if row["filterType"] == "PERCENT_PRICE_BY_SIDE":
        band = max(band, Decimal(str(row.get("askMultiplierUp", "0"))),
                   Decimal(str(row.get("bidMultiplierUp", "0"))))
if band <= 1:
    band = Decimal("1.5")
need = (Decimal("10") * band * Decimal("1.05")).quantize(Decimal("0.01"),
                                                         ROUND_UP)
gap = need - available
if gap <= 0:
    print("0")
else:
    print(min(gap.quantize(Decimal("0.01"), ROUND_UP), Decimal("10")))
PY
)
if [ "$TOPUP" = "READ_FAILED" ]; then echo "margin read failed — skipping"; exit 1; fi
if [ "$TOPUP" != "0" ]; then
  echo "margin top-up $TOPUP USDT spot-to-usdm"
  python3 wallet.py transfer spot-to-usdm --asset USDT --amount "$TOPUP" --execute-internal-transfer || { echo "top-up failed — skipping this fire"; exit 1; }
fi

python3 set_leverage_1x.py || { echo "leverage set failed for $SYMBOL — skipping this fire"; exit 1; }
python3 basis_executor.py --execute
E=$?
echo "execute $SYMBOL exit $E"
[ "$E" -ne 0 ] && touch "$D/cooldown-$SYMBOL"
exit $E
