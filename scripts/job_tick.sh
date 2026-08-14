#!/bin/bash
# One cron pass for the strictly validated live-test job runner.
set -u
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -f "$SCRIPT_DIR/../src/runtime_paths.py" ]; then
  DEPLOY_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
elif [ -f "$SCRIPT_DIR/src/runtime_paths.py" ]; then
  DEPLOY_DIR=$SCRIPT_DIR
else
  echo "JOB-TICK | paths module unavailable" >&2
  exit 2
fi

RUNNER_LOG="$DEPLOY_DIR/jobs.log"
exec >> "$RUNNER_LOG" 2>&1
echo "=== $(date -u +%FT%TZ)"

if ! PATH_ASSIGNMENTS=$(PYTHONDONTWRITEBYTECODE=1 python3 -c \
  'import sys; sys.path.insert(0, sys.argv[1]); import runtime_paths; print(runtime_paths.render_shell(runtime_paths.PATHS))' \
  "$DEPLOY_DIR/src"); then
  echo "JOB-TICK | paths unresolved" >&2
  exit 2
fi
eval "$PATH_ASSIGNMENTS"
export RECEIPT_DIR
export PYTHONDONTWRITEBYTECODE=1

mkdir -p "$RECEIPT_DIR" "$JOBS_DIR" "$PROPOSALS_DIR" "$WATCH_DIR" \
  || exit 2

append_failure() {
  local stage=$1
  local code=$2
  local utc
  utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  printf '{"stage":"%s","exit":%d,"utc":"%s"}\n' \
    "$stage" "$code" "$utc" >> "$HEALTH"
}

RELEASE_FILE="$DEPLOY_DIR/RELEASE"
if [ ! -r "$RELEASE_FILE" ]; then
  append_failure release 2
  echo "JOB-TICK | REFUSED | RELEASE unavailable" >&2
  exit 2
fi
release_lines=()
while IFS= read -r line || [ -n "$line" ]; do
  release_lines+=("$line")
done < "$RELEASE_FILE"
if [ "${#release_lines[@]}" -ne 1 ] \
  || [[ ! "${release_lines[0]:-}" =~ ^[0-9a-f]{40}$ ]]; then
  append_failure release 2
  echo "JOB-TICK | REFUSED | RELEASE must contain one full lowercase SHA" \
    >&2
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

job_runner_stage() {
  if ! load_runtime_credentials; then
    echo "JOB-TICK | credential bootstrap failed" >&2
    return 4
  fi
  python3 "$DEPLOY_DIR/src/job_runner.py" --tick
}

run_stage job_runner "0" \
  job_runner_stage

echo "JOB-TICK | exit $overall"
exit "$overall"
