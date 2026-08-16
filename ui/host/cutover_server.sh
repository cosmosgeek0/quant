#!/bin/bash
set -u

ROOT=/home/ssm-user/quant-ui
CURRENT=ui/serve.py
NEW=ui/serve.py.new

if ! cd "$ROOT"; then
  echo "== CUTOVER FAILED"
  echo "== cutover root Unknown"
  exit 2
fi
if [ ! -f "$CURRENT" ] || [ ! -f "$NEW" ]; then
  echo "== CUTOVER FAILED"
  echo "== cutover input Unknown"
  exit 3
fi

curl_path() {
  path=$1
  result=$(curl -sS --max-time 10 -o /dev/null -w '%{http_code} %{size_download}' "http://127.0.0.1:8080${path}" 2>/dev/null)
  curl_rc=$?
  if [ "$curl_rc" -ne 0 ]; then
    CURL_CODE=Unknown
    CURL_BYTES=Unknown
  else
    CURL_CODE=${result%% *}
    CURL_BYTES=${result#* }
  fi
  echo "== $path $CURL_CODE $CURL_BYTES"
  if [ "$curl_rc" -ne 0 ] || [ "$CURL_CODE" != "200" ]; then
    return 1
  fi
  return 0
}

verify_cutover() {
  verify_failed=0
  service_state=$(systemctl is-active quant-ui 2>/dev/null)
  service_rc=$?
  if [ -z "$service_state" ]; then
    service_state=Unknown
  fi
  echo "== quant-ui $service_state"
  if [ "$service_rc" -ne 0 ] || [ "$service_state" != "active" ]; then
    verify_failed=1
  fi

  if curl_path "/"; then
    if [ -f ui/index.html ]; then
      expected_bytes=$(stat -c %s ui/index.html 2>/dev/null)
      expected_rc=$?
      if [ "$expected_rc" -eq 0 ] && [ -n "$expected_bytes" ]; then
        echo "== index expected $expected_bytes"
      else
        echo "== index expected Unknown"
        verify_failed=1
      fi
      if [ "$expected_rc" -eq 0 ] && [ "$CURL_BYTES" != "$expected_bytes" ]; then
        verify_failed=1
      fi
    else
      echo "== index expected Unknown"
      verify_failed=1
    fi
  else
    verify_failed=1
  fi
  if ! curl_path "/api/strip"; then
    verify_failed=1
  fi
  if ! curl_path "/api/network"; then
    verify_failed=1
  fi
  if ! curl_path "/api/telemetry"; then
    verify_failed=1
  fi
  return "$verify_failed"
}

cmp -s "$CURRENT" "$NEW"
cmp_rc=$?
changed=0
backup=
if [ "$cmp_rc" -eq 1 ]; then
  changed=1
  timestamp=$(date -u +%Y%m%dT%H%M%SZ)
  backup="${CURRENT}.rollback-${timestamp}"
  if ! cp -p "$CURRENT" "$backup"; then
    echo "== CUTOVER FAILED"
    echo "== cutover backup Unknown"
    exit 4
  fi
  echo "== backup $backup"
elif [ "$cmp_rc" -ne 0 ]; then
  echo "== CUTOVER FAILED"
  echo "== cutover comparison Unknown"
  exit 5
else
  echo "== serve.py unchanged"
fi

if ! mv "$NEW" "$CURRENT"; then
  echo "== CUTOVER FAILED"
  if [ "$changed" -eq 1 ]; then
    /bin/bash ui/host/rollback_server.sh "$backup"
    rollback_rc=$?
    if [ "$rollback_rc" -ne 0 ]; then
      exit 21
    fi
  fi
  exit 6
fi

cutover_failed=0
if ! sudo -n systemctl restart quant-ui; then
  echo "== quant-ui restart Unknown"
  cutover_failed=1
fi
sleep 3
if ! verify_cutover; then
  cutover_failed=1
fi

if [ "$cutover_failed" -ne 0 ]; then
  echo "== CUTOVER FAILED"
  if [ "$changed" -eq 1 ]; then
    /bin/bash ui/host/rollback_server.sh "$backup"
    rollback_rc=$?
    if [ "$rollback_rc" -ne 0 ]; then
      exit 21
    fi
  else
    echo "== rollback content unchanged"
    rollback_failed=0
    if ! sudo -n systemctl restart quant-ui; then
      echo "== quant-ui restart Unknown"
      rollback_failed=1
    fi
    sleep 3
    service_state=$(systemctl is-active quant-ui 2>/dev/null)
    service_rc=$?
    if [ -z "$service_state" ]; then
      service_state=Unknown
    fi
    echo "== quant-ui $service_state"
    if [ "$service_rc" -ne 0 ] || [ "$service_state" != "active" ]; then
      rollback_failed=1
    fi
    if ! curl_path "/"; then
      rollback_failed=1
    fi
    if ! curl_path "/api/strip"; then
      rollback_failed=1
    fi
    if [ "$rollback_failed" -ne 0 ]; then
      echo "== rollback verification incomplete"
    else
      echo "== rollback verification complete"
    fi
  fi
  exit 20
fi

echo "== CUTOVER COMPLETE"
exit 0
