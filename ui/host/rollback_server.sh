#!/bin/bash
set -u

ROOT=/home/ssm-user/quant-ui

if [ "$#" -ne 1 ] || [ -z "${1-}" ]; then
  echo "== rollback usage: rollback_server.sh <backup file>"
  exit 2
fi
if ! cd "$ROOT"; then
  echo "== rollback root Unknown"
  exit 3
fi

backup=$1
case "$backup" in
  ui/serve.py.rollback-*|/home/ssm-user/quant-ui/ui/serve.py.rollback-*)
    ;;
  *)
    echo "== rollback backup path Unknown"
    exit 4
    ;;
esac
if [ ! -f "$backup" ]; then
  echo "== rollback backup Unknown"
  exit 5
fi
if ! cp -p "$backup" ui/serve.py; then
  echo "== rollback restore Unknown"
  exit 6
fi
echo "== rollback restored $backup"

failed=0
if ! sudo -n systemctl restart quant-ui; then
  echo "== quant-ui restart Unknown"
  failed=1
fi
sleep 3

state=$(systemctl is-active quant-ui 2>/dev/null)
state_rc=$?
if [ -z "$state" ]; then
  state=Unknown
fi
echo "== quant-ui $state"
if [ "$state_rc" -ne 0 ] || [ "$state" != "active" ]; then
  failed=1
fi

check_path() {
  path=$1
  result=$(curl -sS --max-time 10 -o /dev/null -w '%{http_code} %{size_download}' "http://127.0.0.1:8080${path}" 2>/dev/null)
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
}

check_path "/"
check_path "/api/strip"

if [ "$failed" -ne 0 ]; then
  echo "== rollback verification incomplete"
  exit 10
fi
echo "== rollback verification complete"
exit 0
