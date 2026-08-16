#!/bin/bash
# Land a verified host release while the one-minute job tick is gated.
set -uo pipefail
umask 077
WORK_DIR=
RELEASE_TEMP=
cleanup() {
  [ -z "$RELEASE_TEMP" ] || rm -f -- "$RELEASE_TEMP"
  if [ -n "$WORK_DIR" ]; then
    rm -f -- "$WORK_DIR/entries" "$WORK_DIR/expected" \
      "$WORK_DIR/actual"
    rmdir "$WORK_DIR" 2>/dev/null || true
  fi
}
refuse() {
  printf 'HOST-DEPLOY | REFUSED | %s\n' "$1" >&2
  exit 2
}
device_id() {
  stat -c '%d' -- "$1" 2>/dev/null || stat -f '%d' -- "$1"
}
trap cleanup EXIT
trap 'refuse INTERRUPTED' HUP INT TERM
[ "$#" -eq 4 ] || \
  refuse USAGE_STAGING_DEPLOY_MANIFEST_RELEASE_SHA
STAGING_DIR=$1 DEPLOY_DIR=$2 MANIFEST=$3 RELEASE_SHA=$4
for tool in cmp cut dirname find mkdir mktemp mv realpath sha256sum sort stat; do
  command -v "$tool" >/dev/null 2>&1 || refuse "TOOL_UNAVAILABLE_$tool"
done
[ -d "$STAGING_DIR" ] && [ ! -L "$STAGING_DIR" ] || \
  refuse STAGING_DIR_UNAVAILABLE
[ -d "$DEPLOY_DIR" ] && [ ! -L "$DEPLOY_DIR" ] || \
  refuse DEPLOY_DIR_UNAVAILABLE
[ -f "$MANIFEST" ] || refuse MANIFEST_UNAVAILABLE
case "$RELEASE_SHA" in
  *[!0-9a-f]*) refuse RELEASE_SHA_INVALID ;;
esac
[ "${#RELEASE_SHA}" -eq 40 ] || refuse RELEASE_SHA_INVALID
STAGING_DIR=$(realpath -- "$STAGING_DIR") || refuse STAGING_DIR_UNRESOLVED
DEPLOY_DIR=$(realpath -- "$DEPLOY_DIR") || refuse DEPLOY_DIR_UNRESOLVED
MANIFEST=$(realpath -- "$MANIFEST") || refuse MANIFEST_UNRESOLVED
case "$STAGING_DIR/" in "$DEPLOY_DIR/"*) refuse DEPLOY_DIRS_OVERLAP ;; esac
case "$DEPLOY_DIR/" in "$STAGING_DIR/"*) refuse DEPLOY_DIRS_OVERLAP ;; esac
MARKER="$DEPLOY_DIR/DEPLOYING"
if ! (set -C; printf '%s\n' "$RELEASE_SHA" > "$MARKER") 2>/dev/null; then
  refuse DEPLOYING_MARKER_PRESENT
fi
[ "$(device_id "$STAGING_DIR")" = "$(device_id "$DEPLOY_DIR")" ] || \
  refuse CROSS_FILESYSTEM
[ ! -L "$DEPLOY_DIR/RELEASE" ] && [ ! -d "$DEPLOY_DIR/RELEASE" ] || \
  refuse RELEASE_TARGET_UNSAFE
WORK_DIR=$(mktemp -d /tmp/host-deploy-atomic.XXXXXX) || \
  refuse WORK_DIR_UNAVAILABLE
: > "$WORK_DIR/entries" || refuse WORK_DIR_UNAVAILABLE
FILE_COUNT=0
while IFS=' ' read -r digest rel extra || \
    [ -n "${digest:-}${rel:-}${extra:-}" ]; do
  [ -n "${digest:-}" ] && [ -n "${rel:-}" ] && [ -z "${extra:-}" ] || \
    refuse MANIFEST_ROW_INVALID
  case "$digest" in
    *[!0-9a-f]*) refuse MANIFEST_SHA256_INVALID ;;
  esac
  [ "${#digest}" -eq 64 ] || refuse MANIFEST_SHA256_INVALID
  case "$rel" in
    ''|/*|.|..|../*|*/..|*/../*|*[!A-Za-z0-9._/-]*) \
      refuse MANIFEST_PATH_UNSAFE ;;
    RELEASE|DEPLOYING) refuse MANIFEST_PATH_RESERVED ;;
  esac
  printf '%s\t%s\n' "$rel" "$digest" >> "$WORK_DIR/entries" || \
    refuse WORK_DIR_UNAVAILABLE
  FILE_COUNT=$((FILE_COUNT + 1))
done < "$MANIFEST"
[ "$FILE_COUNT" -gt 0 ] || refuse MANIFEST_EMPTY
cut -f1 "$WORK_DIR/entries" | LC_ALL=C sort > "$WORK_DIR/expected" || \
  refuse MANIFEST_SORT_FAILED
: > "$WORK_DIR/actual" || refuse WORK_DIR_UNAVAILABLE
while IFS= read -r -d '' source; do
  [ "$source" = "$MANIFEST" ] && continue
  rel=${source#"$STAGING_DIR"/}
  case "$rel" in
    ''|/*|.|..|../*|*/..|*/../*|*[!A-Za-z0-9._/-]*) \
      refuse STAGING_PATH_UNSAFE ;;
  esac
  printf '%s\n' "$rel" >> "$WORK_DIR/actual" || refuse WORK_DIR_UNAVAILABLE
done < <(find "$STAGING_DIR" -type f -print0)
[ -z "$(find "$STAGING_DIR" -mindepth 1 ! -type d ! -type f -print -quit)" ] || \
  refuse STAGING_ENTRY_UNSAFE
LC_ALL=C sort -o "$WORK_DIR/actual" "$WORK_DIR/actual" || \
  refuse STAGING_SORT_FAILED
cmp -s "$WORK_DIR/expected" "$WORK_DIR/actual" || \
  refuse MANIFEST_STAGE_MISMATCH
while IFS=$'\t' read -r rel digest; do
  source="$STAGING_DIR/$rel"
  target="$DEPLOY_DIR/$rel"
  parent=$(dirname -- "$target")
  mkdir -p -- "$parent" || refuse TARGET_DIR_CREATE_FAILED
  parent=$(realpath -- "$parent") || refuse TARGET_DIR_UNRESOLVED
  case "$parent" in
    "$DEPLOY_DIR"|"$DEPLOY_DIR"/*) ;;
    *) refuse TARGET_DIR_UNSAFE ;;
  esac
  [ "$(device_id "$source")" = "$(device_id "$parent")" ] || \
    refuse CROSS_FILESYSTEM
  [ ! -L "$target" ] && [ ! -d "$target" ] || refuse TARGET_PATH_UNSAFE
  mv -f -- "$source" "$target" || refuse ATOMIC_RENAME_FAILED
done < "$WORK_DIR/entries"
while IFS=$'\t' read -r rel digest; do
  landed=$(sha256sum -- "$DEPLOY_DIR/$rel") || refuse LANDED_SHA256_UNAVAILABLE
  [ "${landed%% *}" = "$digest" ] || refuse LANDED_SHA256_MISMATCH
done < "$WORK_DIR/entries"
RELEASE_TEMP=$(mktemp "$DEPLOY_DIR/.RELEASE.XXXXXX") || \
  refuse RELEASE_TEMP_UNAVAILABLE
printf '%s\n' "$RELEASE_SHA" > "$RELEASE_TEMP" || refuse RELEASE_WRITE_FAILED
mv -f -- "$RELEASE_TEMP" "$DEPLOY_DIR/RELEASE" || refuse RELEASE_RENAME_FAILED
RELEASE_TEMP=
rm -f -- "$MARKER" || refuse MARKER_REMOVE_FAILED
printf 'HOST-DEPLOY | OK | files=%d | release=%s\n' "$FILE_COUNT" "$RELEASE_SHA"
