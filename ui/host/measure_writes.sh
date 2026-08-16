#!/bin/bash
set -u

if [ "$#" -ne 1 ]; then
  printf 'usage: %s <seconds>\n' "$(basename "$0")" >&2
  exit 2
fi

case "$1" in
  ''|*[!0-9]*)
    printf 'seconds must be a positive integer\n' >&2
    exit 2
    ;;
esac

window_s=$1
if [ "$window_s" -le 0 ]; then
  printf 'seconds must be a positive integer\n' >&2
  exit 2
fi

mirror_io=/sys/fs/cgroup/system.slice/quant-ui-mirror.service/io.stat
network_io=/sys/fs/cgroup/system.slice/quant-ui-network.service/io.stat
disk_source=$(df --output=source /home/ssm-user 2>/dev/null | tail -1)
if [ -n "$disk_source" ]; then
  disk_device=$(basename "$disk_source")
else
  disk_source=Unknown
  disk_device=Unknown
fi

read_disk_sectors() {
  [ "$disk_device" != Unknown ] || return 1
  awk -v device="$disk_device" '
    $3 == device { print $10; found = 1; exit }
    END { if (!found) exit 1 }
  ' /proc/diskstats 2>/dev/null
}

read_cgroup_wbytes() {
  local path=$1
  [ -r "$path" ] || return 1
  awk '
    {
      for (field = 1; field <= NF; field++) {
        if ($field ~ /^wbytes=/) {
          split($field, value, "=")
          total += value[2]
          found = 1
        }
      }
    }
    END {
      if (!found) exit 1
      printf "%.0f\n", total
    }
  ' "$path"
}

print_disk_rate() {
  local start_value=$1
  local end_value=$2
  if [ "$start_value" = Unknown ] || [ "$end_value" = Unknown ]; then
    printf 'disk device=%s sectors_delta=Unknown bytes_per_s=Unknown gb_per_day=Unknown\n' "$disk_device"
  elif [ "$end_value" -lt "$start_value" ]; then
    printf 'disk device=%s sectors_delta=Unknown bytes_per_s=Unknown gb_per_day=Unknown reason=counter_decreased\n' "$disk_device"
  else
    local sectors_delta=$((end_value-start_value))
    awk -v device="$disk_device" -v sectors_delta="$sectors_delta" -v seconds="$window_s" '
      BEGIN {
        bytes = sectors_delta * 512
        bytes_per_s = bytes / seconds
        gb_per_day = bytes_per_s * 86400 / 1000000000
        printf "disk device=%s sectors_delta=%s bytes_per_s=%.2f gb_per_day=%.3f\n", device, sectors_delta, bytes_per_s, gb_per_day
      }
    '
  fi
}

print_service_rate() {
  local service=$1
  local start_value=$2
  local end_value=$3
  if [ "$start_value" = Unknown ] || [ "$end_value" = Unknown ]; then
    printf 'service=%s wbytes_delta=Unknown bytes_per_s=Unknown\n' "$service"
  elif [ "$end_value" -lt "$start_value" ]; then
    printf 'service=%s wbytes_delta=Unknown bytes_per_s=Unknown reason=counter_decreased\n' "$service"
  else
    local wbytes_delta=$((end_value-start_value))
    awk -v service="$service" -v wbytes_delta="$wbytes_delta" -v seconds="$window_s" '
      BEGIN {
        printf "service=%s wbytes_delta=%s bytes_per_s=%.2f\n", service, wbytes_delta, wbytes_delta / seconds
      }
    '
  fi
}

disk_start=$(read_disk_sectors) || disk_start=Unknown
mirror_start=$(read_cgroup_wbytes "$mirror_io") || mirror_start=Unknown
network_start=$(read_cgroup_wbytes "$network_io") || network_start=Unknown

printf 'window_s=%s\n' "$window_s"
printf 'disk source=%s device=%s sectors_written_start=%s\n' "$disk_source" "$disk_device" "$disk_start"
printf 'service=quant-ui-mirror.service wbytes_start=%s\n' "$mirror_start"
printf 'service=quant-ui-network.service wbytes_start=%s\n' "$network_start"

sleep "$window_s"

disk_end=$(read_disk_sectors) || disk_end=Unknown
mirror_end=$(read_cgroup_wbytes "$mirror_io") || mirror_end=Unknown
network_end=$(read_cgroup_wbytes "$network_io") || network_end=Unknown

printf 'disk source=%s device=%s sectors_written_end=%s\n' "$disk_source" "$disk_device" "$disk_end"
printf 'service=quant-ui-mirror.service wbytes_end=%s\n' "$mirror_end"
printf 'service=quant-ui-network.service wbytes_end=%s\n' "$network_end"
print_disk_rate "$disk_start" "$disk_end"
print_service_rate quant-ui-mirror.service "$mirror_start" "$mirror_end"
print_service_rate quant-ui-network.service "$network_start" "$network_end"
