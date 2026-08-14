#!/bin/bash
# Install, arm, stop, and inspect the one runtime chain plus its 1m job runner.
set -u
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -f "$SCRIPT_DIR/../src/runtime_paths.py" ]; then
  DEPLOY_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
elif [ -f "$SCRIPT_DIR/src/runtime_paths.py" ]; then
  DEPLOY_DIR=$SCRIPT_DIR
else
  echo "CONTROL | paths module unavailable" >&2
  exit 2
fi

if ! PATH_ASSIGNMENTS=$(PYTHONDONTWRITEBYTECODE=1 \
  python3 "$DEPLOY_DIR/src/runtime_paths.py" --shell); then
  echo "CONTROL | paths unresolved" >&2
  exit 2
fi
eval "$PATH_ASSIGNMENTS"

if [ -f "$DEPLOY_DIR/scripts/runtime_tick.sh" ]; then
  RUNTIME_TICK_TARGET="$DEPLOY_DIR/scripts/runtime_tick.sh"
else
  RUNTIME_TICK_TARGET="$DEPLOY_DIR/runtime_tick.sh"
fi
if [ -f "$DEPLOY_DIR/scripts/job_tick.sh" ]; then
  JOB_TICK_TARGET="$DEPLOY_DIR/scripts/job_tick.sh"
else
  JOB_TICK_TARGET="$DEPLOY_DIR/job_tick.sh"
fi
RUNTIME_CRON="*/2 * * * * $RUNTIME_TICK_TARGET"
JOB_CRON="* * * * * $JOB_TICK_TARGET"

usage() {
  echo "usage: $0 start ARM | stop | status" >&2
}

print_cron() {
  local cron_text
  local found=0
  cron_text=$(crontab -l 2>/dev/null) || cron_text=
  while IFS= read -r line; do
    case "$line" in
      *runtime_tick.sh*|*job_tick.sh*|*basis_auto.sh*|*basis_tick.sh*|*momentum_tick.sh*|*market_stream.py*|*basis_watch.py*|*momentum_live.py*|*basis_supervisor.py*)
        printf 'CRON | %s\n' "$line"
        found=1
        ;;
    esac
  done <<< "$cron_text"
  if [ "$found" -eq 0 ]; then
    echo "CRON | NONE"
  fi
}

print_flag() {
  local label=$1
  local path=$2
  if [ -e "$path" ]; then
    printf '%s=PRESENT | %s\n' "$label" "$path"
  else
    printf '%s=ABSENT | %s\n' "$label" "$path"
  fi
}

print_tail() {
  local label=$1
  local path=$2
  if [ -r "$path" ]; then
    tail -n 5 -- "$path" | while IFS= read -r line; do
      printf '%s | %s\n' "$label" "$line"
    done
  elif [ -e "$path" ]; then
    printf '%s | UNREADABLE | %s\n' "$label" "$path"
  else
    printf '%s | NONE | %s\n' "$label" "$path"
  fi
}

print_status() {
  printf 'RECEIPT_DIR=%s\n' "$RECEIPT_DIR"
  print_cron
  print_flag STOP "$STOP_FILE"
  print_flag STOP-JOBS "$STOP_JOBS_FILE"
  print_tail HEALTH "$HEALTH"
  print_tail RESULT "$JOB_RESULTS"
}

install_cron() {
  local scratch current next changes
  scratch=$(mktemp -d /tmp/runtime-control.XXXXXX) || return 1
  current="$scratch/current"
  next="$scratch/next"
  changes="$scratch/changes"
  : > "$current"
  : > "$next"
  : > "$changes"
  crontab -l > "$current" 2>/dev/null || true

  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      *basis_auto.sh*|*basis_tick.sh*|*momentum_tick.sh*|*market_stream.py*|*basis_watch.py*|*momentum_live.py*|*basis_supervisor.py*)
        printf 'REMOVED | %s\n' "$line" >> "$changes"
        ;;
      *runtime_tick.sh*|*job_tick.sh*)
        printf 'REPLACED | %s\n' "$line" >> "$changes"
        ;;
      *)
        printf '%s\n' "$line" >> "$next"
        ;;
    esac
  done < "$current"
  printf '%s\n%s\n' "$RUNTIME_CRON" "$JOB_CRON" >> "$next"

  if ! crontab "$next"; then
    rm -f "$current" "$next" "$changes"
    rmdir "$scratch"
    return 1
  fi
  if [ -s "$changes" ]; then
    while IFS= read -r line; do
      printf '%s\n' "$line"
    done < "$changes"
  else
    echo "REMOVED | NONE"
  fi
  printf 'INSTALLED | %s\n' "$RUNTIME_CRON"
  printf 'INSTALLED | %s\n' "$JOB_CRON"
  rm -f "$current" "$next" "$changes"
  rmdir "$scratch"
}

command_name=${1:-}
case "$command_name" in
  start)
    if [ "$#" -ne 2 ] || [ "${2:-}" != "ARM" ]; then
      echo "CONTROL | REFUSED | start requires explicit ARM"
      print_status
      exit 2
    fi
    if [ ! -r "$DEPLOY_DIR/RELEASE" ]; then
      echo "CONTROL | REFUSED | RELEASE unavailable"
      print_status
      exit 2
    fi
    if [ ! -f "$RUNTIME_TICK_TARGET" ] || [ ! -f "$JOB_TICK_TARGET" ]; then
      echo "CONTROL | REFUSED | cron target unavailable"
      print_status
      exit 2
    fi
    mkdir -p "$RECEIPT_DIR" "$JOBS_DIR" "$PROPOSALS_DIR" "$WATCH_DIR" || exit 2
    chmod 700 "$RUNTIME_TICK_TARGET" "$JOB_TICK_TARGET" || exit 2
    if ! install_cron; then
      echo "CONTROL | REFUSED | crontab install failed"
      print_status
      exit 2
    fi
    if ! rm -f "$STOP_FILE" "$STOP_JOBS_FILE" \
      || [ -e "$STOP_FILE" ] || [ -e "$STOP_JOBS_FILE" ]; then
      echo "CONTROL | REFUSED | stop files could not be cleared"
      print_status
      exit 2
    fi
    echo "CONTROL | ARMED"
    print_status
    ;;
  stop)
    if [ "$#" -ne 1 ]; then
      usage
      exit 2
    fi
    mkdir -p "$RECEIPT_DIR" || exit 2
    touch "$STOP_FILE" "$STOP_JOBS_FILE" || exit 2
    chmod 600 "$STOP_FILE" "$STOP_JOBS_FILE" || exit 2
    echo "CONTROL | STOPPED"
    print_status
    ;;
  status)
    if [ "$#" -ne 1 ]; then
      usage
      exit 2
    fi
    print_status
    ;;
  *)
    usage
    exit 2
    ;;
esac
