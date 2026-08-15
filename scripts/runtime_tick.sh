#!/bin/bash
# One ordered cron pass for the observation, decision, and supervision chain.
set -u
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -f "$SCRIPT_DIR/../src/runtime_paths.py" ]; then
  DEPLOY_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
elif [ -f "$SCRIPT_DIR/src/runtime_paths.py" ]; then
  DEPLOY_DIR=$SCRIPT_DIR
else
  echo "RUNTIME | paths module unavailable" >&2
  exit 2
fi

if ! PATH_ASSIGNMENTS=$(PYTHONDONTWRITEBYTECODE=1 \
  python3 "$DEPLOY_DIR/src/runtime_paths.py" --shell); then
  echo "RUNTIME | paths unresolved" >&2
  exit 2
fi
eval "$PATH_ASSIGNMENTS"
export RECEIPT_DIR
export PYTHONDONTWRITEBYTECODE=1

KNOBS_FILE="$DEPLOY_DIR/knobs.env"
if [ -f "$KNOBS_FILE" ]; then
  # Auditor note (18b193f delta): never source free shell — accept only
  # strict KEY=decimal lines; anything else is ignored, not executed.
  while IFS= read -r knob_line; do
    case "$knob_line" in
      QUANT_[A-Z_]*=[0-9]*|QUANT_[A-Z_]*=[0-9]*.[0-9]*)
        export "${knob_line%%=*}=${knob_line#*=}"
        ;;
    esac
  done < "$KNOBS_FILE"
fi

FEED_STATE="$WATCH_DIR/feed_state.json"
FEED_LOG="$WATCH_DIR/feed.log"
FEED_PID_FILE="$WATCH_DIR/feed.pid"
FEED_STOP_FILE="$WATCH_DIR/feed.stop"

mkdir -p "$RECEIPT_DIR" "$JOBS_DIR" "$PROPOSALS_DIR" "$WATCH_DIR" || exit 2

append_failure() {
  local stage=$1
  local code=$2
  local utc
  utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  printf '{"stage":"%s","exit":%d,"utc":"%s"}\n' \
    "$stage" "$code" "$utc" >> "$HEALTH"
}

append_feed_restart() {
  local utc
  utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  printf '{"service":"market-feed","status":"RESTARTED","event":"restart","restart_count":1,"utc":"%s"}\n' \
    "$utc" >> "$HEALTH"
}

RELEASE_FILE="$DEPLOY_DIR/RELEASE"
release_lines=()
if [ -r "$RELEASE_FILE" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    release_lines+=("$line")
  done < "$RELEASE_FILE"
fi
if [ "${#release_lines[@]}" -ne 1 ] \
  || [[ ! "${release_lines[0]:-}" =~ ^[0-9a-f]{40}$ ]]; then
  append_failure release 2
  echo "RUNTIME | RELEASE must contain one full lowercase SHA" >&2
  exit 2
fi
export QUANT_SHA=${release_lines[0]}

overall=0
run_stage() {
  local stage=$1
  local accepted=$2
  local code
  shift 2
  "$@"
  code=$?
  case " $accepted " in
    *" $code "*) return 0 ;;
  esac
  append_failure "$stage" "$code"
  overall=1
  return 0
}

momentum_position_standing() {
  [ -r "$MOMENTUM_POSITION" ] \
    && python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1], encoding="utf-8")).get("state") in {"OPEN", "OPENING", "CLOSING", "EXIT_PENDING", "UNKNOWN"} else 1)' \
      "$MOMENTUM_POSITION" 2>/dev/null
}

stream_stage() {
  python3 "$DEPLOY_DIR/src/market_stream.py" --once \
    --state-file "$STREAM_STATE" \
    --log-file "$STREAM_LOG" \
    --windows-file "$STREAM_WINDOWS" \
    --feed-state "$FEED_STATE"
}

momentum_stage() {
  python3 "$DEPLOY_DIR/src/momentum_live.py" \
    --tick "$STREAM_STATE" \
    --state "$MOMENTUM_STATE" \
    --receipts "$MOMENTUM_RECEIPTS" \
    --decisions "$DECISIONS" \
    --jobs-dir "$JOBS_DIR" \
    --position "$MOMENTUM_POSITION" \
    --results "$JOB_RESULTS" \
    --max-freshness-ms 90000 \
    "$@"
}

read_feed_pid() {
  local pid
  [ -r "$FEED_PID_FILE" ] || return 1
  pid=$(<"$FEED_PID_FILE")
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  printf '%s\n' "$pid"
}

feed_process_live() {
  local pid=$1
  local command
  kill -0 "$pid" 2>/dev/null || return 1
  command=$(ps -ww -p "$pid" -o command= 2>/dev/null) || return 1
  case "$command" in
    *"$DEPLOY_DIR/src/market_feed.py"*"--serve"*) return 0 ;;
  esac
  return 1
}

start_feed() {
  local pid checks=0
  nohup python3 "$DEPLOY_DIR/src/market_feed.py" --serve \
    --state-file "$FEED_STATE" \
    --stop-file "$FEED_STOP_FILE" \
    >> "$FEED_LOG" 2>&1 < /dev/null &
  pid=$!
  if ! printf '%s\n' "$pid" > "$FEED_PID_FILE"; then
    kill -TERM "$pid" 2>/dev/null || true
    return 1
  fi
  while [ "$checks" -lt 10 ]; do
    if feed_process_live "$pid"; then
      return 0
    fi
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.1
    checks=$((checks + 1))
  done
  kill -TERM "$pid" 2>/dev/null || true
  rm -f "$FEED_PID_FILE"
  return 1
}

feed_stage() {
  local pid
  local restart=0
  if [ -e "$FEED_PID_FILE" ]; then
    if pid=$(read_feed_pid) && feed_process_live "$pid"; then
      return 0
    fi
    restart=1
    rm -f "$FEED_PID_FILE" || return 1
  fi
  start_feed || return 1
  if [ "$restart" -eq 1 ]; then
    append_feed_restart || return 1
  fi
  return 0
}

load_runtime_credentials() {
  local secret_source secret_id secret_json api_key api_secret
  if [ -n "${BINANCE_API_KEY:-}" ] && [ -n "${BINANCE_API_SECRET:-}" ]; then
    return 0
  fi
  secret_source=${QUANT_SECRET_SOURCE:-/home/ssm-user/quant-run/wave2-check-20260813-h1/w2check.py}
  [ -r "$secret_source" ] || return 4
  secret_id=$(grep -oE 'SECRET_ID = "[^"]+"' "$secret_source" \
    | cut -d'"' -f2)
  [ -n "$secret_id" ] \
    && [ "$(printf '%s\n' "$secret_id" | wc -l | tr -d ' ')" -eq 1 ] \
    || return 4
  secret_json=$(aws secretsmanager get-secret-value \
    --secret-id "$secret_id" --query SecretString --output text 2>/dev/null) \
    || return 4
  api_key=$(printf '%s' "$secret_json" | python3 -c \
    'import json,sys; value=json.load(sys.stdin).get("BINANCE_API_KEY"); print(value if isinstance(value,str) else "")' \
    2>/dev/null) || return 4
  api_secret=$(printf '%s' "$secret_json" | python3 -c \
    'import json,sys; value=json.load(sys.stdin).get("BINANCE_API_SECRET"); print(value if isinstance(value,str) else "")' \
    2>/dev/null) || return 4
  unset secret_json
  [ -n "$api_key" ] && [ -n "$api_secret" ] || return 4
  export BINANCE_API_KEY="$api_key"
  export BINANCE_API_SECRET="$api_secret"
}

supervisor_stage() {
  if ! load_runtime_credentials; then
    echo "RUNTIME | supervisor credential bootstrap failed" >&2
    return 4
  fi
  python3 "$DEPLOY_DIR/src/basis_supervisor.py" --tick
}

# STOP and STOP-JOBS both seal basis entry publication while the public
# observation chain continues. Standing exposure additionally receives
# manage-only HOLD/EXIT decisions under either seal.
if [ -e "$STOP_FILE" ]; then
  echo "RUNTIME | observation under STOP | entries sealed"
fi

run_stage feed "0" \
  feed_stage

run_stage stream "0" \
  stream_stage

run_stage basis_recorder "0" \
  python3 "$DEPLOY_DIR/src/basis_recorder.py" \
    --stream-state "$STREAM_STATE" \
    --receipt-dir "$RECEIPT_DIR"

if [ -e "$STOP_FILE" ] || [ -e "$STOP_JOBS_FILE" ]; then
  run_stage basis_watch "0" \
    python3 "$DEPLOY_DIR/src/basis_watch.py" --once \
      --receipt-dir "$RECEIPT_DIR" \
      --state-file "$BASIS_WATCH_STATE" \
      --stream-state "$STREAM_STATE" \
      --observe-only
else
  run_stage basis_watch "0" \
    python3 "$DEPLOY_DIR/src/basis_watch.py" --once \
      --receipt-dir "$RECEIPT_DIR" \
      --state-file "$BASIS_WATCH_STATE" \
      --stream-state "$STREAM_STATE"
fi

if [ -e "$STOP_FILE" ] || [ -e "$STOP_JOBS_FILE" ]; then
  if momentum_position_standing; then
    run_stage momentum_live "0" \
      momentum_stage --decision-only --manage-only
  else
    run_stage momentum_live "0" \
      momentum_stage --decision-only
  fi
else
  run_stage momentum_live "0" \
    momentum_stage
fi

# basis_supervisor documents 0=acted, 1=held, 2=frozen, 3=idle. HELD and IDLE
# are ordinary tick outcomes; FROZEN and unexpected codes are failures.
run_stage supervisor "0 1 3" \
  supervisor_stage

exit "$overall"
