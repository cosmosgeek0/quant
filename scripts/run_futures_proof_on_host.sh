D=/home/ssm-user/quant-run/wave2-validate-h2
SECRET_ID=$(grep -oE 'SECRET_ID = "[^"]+"' /home/ssm-user/quant-run/wave2-check-20260813-h1/w2check.py | cut -d'"' -f2)
SECRET_JSON=$(aws secretsmanager get-secret-value --secret-id "$SECRET_ID" --query SecretString --output text)
export BINANCE_API_KEY=$(printf '%s' "$SECRET_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["BINANCE_API_KEY"])')
export BINANCE_API_SECRET=$(printf '%s' "$SECRET_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["BINANCE_API_SECRET"])')
unset SECRET_JSON
export SYMBOL=TSTUSDT MAX_NOTIONAL=8 QUANT_SHA=911cbea09d87cb12c961375db857649d131144e9
cd $D/src
echo "EXECUTION_MECHANICS_ONLY | one capped futures short + reduce-only close | expected cost: cents"
python3 futures_hand.py --short --execute
echo "PROOF EXIT $?"
