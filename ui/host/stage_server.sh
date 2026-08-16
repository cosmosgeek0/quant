#!/bin/bash
set -u

ROOT=/home/ssm-user/quant-ui
STAGE_LOG=/tmp/quant-ui-stage.log
STAGE_PID_FILE=/tmp/quant-ui-stage.pid
STAGE_BODY=/tmp/quant-ui-stage-body

if ! cd "$ROOT"; then
  echo "== stage root Unknown"
  exit 2
fi

SERVER=${1:-ui/serve.py.new}
if [ ! -f "$SERVER" ]; then
  echo "== stage server file Unknown: $SERVER"
  exit 4
fi
echo "== staging $SERVER on 127.0.0.1:8081"
nohup python3 "$SERVER" --port 8081 > /tmp/quant-ui-stage.log 2>&1 &
stage_pid=$!
if ! echo "$stage_pid" > /tmp/quant-ui-stage.pid; then
  kill "$stage_pid" 2>/dev/null
  echo "== stage pid file Unknown"
  exit 3
fi

cleanup() {
  if kill -0 "$stage_pid" 2>/dev/null; then
    kill "$stage_pid" 2>/dev/null
    wait "$stage_pid" 2>/dev/null
  fi
  rm -f "$STAGE_PID_FILE" "$STAGE_BODY"
}
trap cleanup EXIT

sleep 3
failed=0
if ! kill -0 "$stage_pid" 2>/dev/null; then
  echo "== stage process Unknown"
  failed=1
fi

json_body_valid() {
  python3 -c 'import json,sys; json.load(sys.stdin)' < "$STAGE_BODY" 2>>"$STAGE_LOG"
}

get_path() {
  path=$1
  result=$(curl -sS --max-time 10 -o "$STAGE_BODY" -w '%{http_code} %{size_download}' "http://127.0.0.1:8081${path}" 2>>"$STAGE_LOG")
  curl_rc=$?
  if [ "$curl_rc" -ne 0 ]; then
    code=Unknown
    bytes=Unknown
  else
    code=${result%% *}
    bytes=${result#* }
  fi
  echo "== $path $code $bytes"
  if [ "$curl_rc" -ne 0 ] || [ "$code" != "200" ]; then
    failed=1
  fi
  if [ "$curl_rc" -eq 0 ]; then
    case "$path" in
      /api/*)
        if ! json_body_valid; then
          echo "== $path JSON Unknown"
          failed=1
        fi
        ;;
    esac
  fi
}

get_path "/"
get_path "/api/strip"
get_path "/api/health"
get_path "/api/decisions"
get_path "/api/failures"
get_path "/api/events"
get_path "/api/stream"
get_path "/api/candidates"
get_path "/api/position"
get_path "/api/momentum"
get_path "/api/fees"
get_path "/api/pnl"
get_path "/api/news"
get_path "/api/telemetry"
get_path "/api/poll_config"
get_path "/api/grace"
get_path "/api/network"
get_path "/api/network/history?edge_id=spot_rest&metric=status&range=1h"
get_path "/api/network/events?limit=5"

probe_message_code='import json,sys; error=json.load(sys.stdin).get("error",""); '
probe_message_code="${probe_message_code}"'ok=isinstance(error,str) and '
probe_message_code="${probe_message_code}"'("cooldown" in error or "already requested" in error); sys.exit(0 if ok else 1)'

post_json() {
  post_path=$1
  post_body=$2
  post_mode=$3
  result=$(curl -sS --max-time 10 -o "$STAGE_BODY" -w '%{http_code} %{size_download}' \
    -H "Origin: http://127.0.0.1:8081" -H "Content-Type: application/json" \
    -d "$post_body" "http://127.0.0.1:8081${post_path}" 2>>"$STAGE_LOG")
  curl_rc=$?
  if [ "$curl_rc" -ne 0 ]; then
    code=Unknown
    bytes=Unknown
  else
    code=${result%% *}
    bytes=${result#* }
  fi
  echo "== $post_path $code $bytes"
  if [ "$curl_rc" -ne 0 ]; then
    failed=1
    return
  fi
  if ! json_body_valid; then
    echo "== $post_path JSON Unknown"
    failed=1
    return
  fi
  if [ "$post_mode" = "success" ] && [ "$code" = "200" ]; then
    return
  fi
  if [ "$post_mode" = "probe" ] && [ "$code" = "200" ]; then
    return
  fi
  if [ "$post_mode" = "probe" ] && [ "$code" = "400" ] \
    && python3 -c "$probe_message_code" < "$STAGE_BODY" 2>>"$STAGE_LOG"; then
    return
  fi
  failed=1
}

post_json "/api/network/config" '{"key":"rest_probe_s","value":15}' success
post_json "/api/network/probe" '{"edge_id":"spot_rest"}' probe

if [ "$failed" -ne 0 ]; then
  echo "== stage verification incomplete"
  exit 10
fi
echo "== stage verification complete"
exit 0
