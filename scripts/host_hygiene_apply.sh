#!/usr/bin/env bash
set -eu

umask 027

TEMP_FILE=
INSTALL_STATE=

cleanup()
{
    if [ -n "$TEMP_FILE" ] && [ -f "$TEMP_FILE" ]; then
        rm -f -- "$TEMP_FILE"
    fi
}

fail()
{
    printf 'result | status=failed | reason=%s\n' "$1" >&2
    exit 1
}

install_text()
{
    target=$1
    validator=$2
    shift 2

    TEMP_FILE=$(mktemp /tmp/host-hygiene.XXXXXX)
    printf '%s\n' "$@" > "$TEMP_FILE"

    if [ "$validator" = logrotate ]; then
        logrotate --debug "$TEMP_FILE" >/dev/null 2>&1 || \
            fail "logrotate_validation_failed"
    fi

    install -d -m 0755 -- "$(dirname "$target")"
    metadata=
    if [ -f "$target" ]; then
        metadata=$(stat -c '%U:%G:%a' -- "$target")
    fi

    if [ -f "$target" ] && cmp -s "$TEMP_FILE" "$target" && \
        [ "$metadata" = root:root:644 ]; then
        state=unchanged
    else
        install -o root -g root -m 0644 -- "$TEMP_FILE" "$target"
        state=updated
    fi

    rm -f -- "$TEMP_FILE"
    TEMP_FILE=
    INSTALL_STATE=$state
    printf 'result | file=%s | state=%s | mode=0644\n' \
        "$target" "$state"
}

check_property()
{
    unit=$1
    property=$2
    expected=$3
    actual=$(systemctl show "$unit" --property="$property" --value)
    if [ "$actual" != "$expected" ]; then
        fail "${unit}_${property}_not_applied"
    fi
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

[ "$(id -u)" -eq 0 ] || fail root_required
[ "$#" -eq 1 ] || fail usage_requires_absolute_quant_run_dir

input_dir=$1
case "$input_dir" in
    /*) ;;
    *) fail quant_run_dir_must_be_absolute ;;
esac

command -v realpath >/dev/null 2>&1 || fail missing_realpath
[ -d "$input_dir" ] || fail quant_run_dir_not_found
[ ! -L "$input_dir" ] || fail quant_run_dir_must_not_be_symlink
quant_dir=$(realpath -e -- "$input_dir")

case "$quant_dir" in
    /|/etc|/home|/home/ssm-user|/opt|/opt/cosmos|/usr|/var|/var/lib/cosmos)
        fail quant_run_dir_is_too_broad
        ;;
esac

for tool in chmod cmp dirname find install journalctl logrotate mktemp; do
    command -v "$tool" >/dev/null 2>&1 || fail "missing_${tool}"
done
for tool in realpath stat systemctl tr wc; do
    command -v "$tool" >/dev/null 2>&1 || fail "missing_${tool}"
done

owner=$(stat -c '%U' -- "$quant_dir")
group=$(stat -c '%G' -- "$quant_dir")
[ "$owner" = ssm-user ] || fail quant_run_dir_owner_not_ssm_user
[ "$group" = ssm-user ] || fail quant_run_dir_group_not_ssm_user

systemctl cat cosmos-app.service >/dev/null 2>&1 || \
    fail cosmos_app_unit_not_found
systemctl cat cloudflared.service >/dev/null 2>&1 || \
    fail cloudflared_unit_not_found

install_text \
    /etc/systemd/journald.conf.d/60-host-hygiene.conf none \
    '[Journal]' \
    'Storage=persistent' \
    'Compress=yes' \
    'Seal=yes' \
    'SystemMaxUse=1G' \
    'SystemKeepFree=2G' \
    'SystemMaxFileSize=64M' \
    'RuntimeMaxUse=128M' \
    'RuntimeKeepFree=256M' \
    'MaxFileSec=1day' \
    'MaxRetentionSec=30day'

journal_config_state=$INSTALL_STATE
if [ "$journal_config_state" = updated ]; then
    systemctl restart systemd-journald.service
    journalctl --rotate
    journal_action=restarted_and_rotated
else
    journal_action=unchanged
fi
[ "$(systemctl is-active systemd-journald.service)" = active ] || \
    fail journald_not_active
journal_usage=$(journalctl --disk-usage 2>/dev/null | tr '\n' ' ')
printf 'result | step=journald | active=yes | action=%s | %s\n' \
    "$journal_action" "$journal_usage"

install_text /etc/logrotate.d/cosmos-runtime logrotate \
    '/var/log/cosmos/*.log {' \
    '    daily' \
    '    rotate 14' \
    '    maxsize 50M' \
    '    missingok' \
    '    notifempty' \
    '    compress' \
    '    delaycompress' \
    '    dateext' \
    '    copytruncate' \
    '    su cosmos cosmos' \
    '}' \
    '' \
    '/var/log/cloudflared/*.log {' \
    '    daily' \
    '    rotate 14' \
    '    maxsize 50M' \
    '    missingok' \
    '    notifempty' \
    '    compress' \
    '    delaycompress' \
    '    dateext' \
    '    copytruncate' \
    '    su cloudflared cloudflared' \
    '}'
printf 'result | step=logrotate | syntax=verified\n'

install_text \
    /etc/systemd/system/cosmos-app.service.d/60-host-hygiene.conf none \
    '[Service]' \
    'Restart=on-failure' \
    'RestartSec=5s' \
    'NoNewPrivileges=yes' \
    'ProtectSystem=strict' \
    'ProtectHome=yes' \
    'ProtectKernelTunables=yes' \
    'ProtectKernelModules=yes' \
    'ProtectControlGroups=yes' \
    'RestrictSUIDSGID=yes' \
    'LockPersonality=yes' \
    'RestrictRealtime=yes' \
    'TasksMax=1024' \
    'LimitNOFILE=65536' \
    'UMask=0027'

install_text \
    /etc/systemd/system/cloudflared.service.d/60-host-hygiene.conf none \
    '[Service]' \
    'Restart=on-failure' \
    'RestartSec=5s' \
    'NoNewPrivileges=yes' \
    'ProtectSystem=full' \
    'ProtectHome=yes' \
    'ProtectKernelTunables=yes' \
    'ProtectKernelModules=yes' \
    'ProtectControlGroups=yes' \
    'RestrictSUIDSGID=yes' \
    'LockPersonality=yes' \
    'RestrictRealtime=yes' \
    'TasksMax=256' \
    'LimitNOFILE=65536' \
    'UMask=0027'

systemctl daemon-reload
check_property cosmos-app.service Restart on-failure
check_property cosmos-app.service NoNewPrivileges yes
check_property cosmos-app.service ProtectSystem strict
check_property cosmos-app.service TasksMax 1024
check_property cosmos-app.service LimitNOFILE 65536
check_property cloudflared.service Restart on-failure
check_property cloudflared.service NoNewPrivileges yes
check_property cloudflared.service ProtectSystem full
check_property cloudflared.service TasksMax 256
check_property cloudflared.service LimitNOFILE 65536
printf '%s\n' \
    'result | step=unit_dropins | config=verified | activation=next_restart'

file_count=$(find "$quant_dir" -xdev -mindepth 1 -maxdepth 1 \
    -type f -print | wc -l | tr -d ' ')
dir_count=$(find "$quant_dir" -xdev -mindepth 1 -maxdepth 1 \
    -type d -print | wc -l | tr -d ' ')

chmod 0750 -- "$quant_dir"
find "$quant_dir" -xdev -mindepth 1 -maxdepth 1 \
    -type d -exec chmod 0750 -- {} +
find "$quant_dir" -xdev -mindepth 1 -maxdepth 1 \
    -type f -exec chmod 0640 -- {} +

[ "$(stat -c '%a' -- "$quant_dir")" = 750 ] || \
    fail quant_run_dir_mode_not_applied
bad_dirs=$(find "$quant_dir" -xdev -mindepth 1 -maxdepth 1 \
    -type d ! -perm 0750 -print | wc -l | tr -d ' ')
bad_files=$(find "$quant_dir" -xdev -mindepth 1 -maxdepth 1 \
    -type f ! -perm 0640 -print | wc -l | tr -d ' ')
[ "$bad_dirs" -eq 0 ] || fail child_directory_mode_not_applied
[ "$bad_files" -eq 0 ] || fail top_file_mode_not_applied

printf '%s\n' \
    "result | step=permissions | path=$quant_dir | root_mode=0750" \
    "result | step=permissions | top_dirs=$dir_count | mode=0750" \
    "result | step=permissions | top_files=$file_count | mode=0640"

printf '%s\n' \
    'result | status=complete | service_restarts=not_performed' \
    'result | excluded=credentials,exchange_keys,money_code,live_money_actions'
