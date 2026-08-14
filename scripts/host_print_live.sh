#!/bin/sh
# Print a bounded, checksummed snapshot of host receipts for the read-only UI.
set -u
umask 077

RECEIPT_ROOT=/home/ssm-user/quant_receipts
START_UTC=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
printf 'LIVEPULL BEGIN %s\n' "$START_UTC"

WORK_DIR=$(mktemp -d /tmp/host-print-live.XXXXXX) || {
    printf 'ERROR snapshot-temp-unavailable\n'
    printf 'LIVEPULL END 0\n'
    exit 1
}
SEEN_FILE=$WORK_DIR/seen
PAYLOAD_FILE=$WORK_DIR/payload
ENCODED_FILE=$WORK_DIR/encoded
PROPOSAL_LIST=$WORK_DIR/proposals
ROOT_JSON_LIST=$WORK_DIR/root-json
: > "$SEEN_FILE"

cleanup() {
    rm -f "$SEEN_FILE" "$PAYLOAD_FILE" "$ENCODED_FILE" \
        "$PROPOSAL_LIST" "$ROOT_JSON_LIST"
    rmdir "$WORK_DIR" 2>/dev/null || true
}
trap cleanup 0
trap 'exit 1' 1 2 15

FILE_COUNT=0

emit_path() {
    REL_PATH=$1
    COPY_MODE=$2

    if grep -F -x -- "$REL_PATH" "$SEEN_FILE" >/dev/null 2>&1; then
        return
    fi
    printf '%s\n' "$REL_PATH" >> "$SEEN_FILE"

    case "$REL_PATH" in
        ''|/*|..|../*|*/..|*/../*|*[!A-Za-z0-9._/-]*)
            printf 'ERROR unsafe-relpath\n'
            return
            ;;
    esac

    SOURCE_FILE=$RECEIPT_ROOT/$REL_PATH
    if [ ! -e "$SOURCE_FILE" ]; then
        printf 'ABSENT %s\n' "$REL_PATH"
        return
    fi
    if [ ! -f "$SOURCE_FILE" ] || [ ! -r "$SOURCE_FILE" ]; then
        printf 'ERROR %s unreadable\n' "$REL_PATH"
        return
    fi

    if [ "$COPY_MODE" = tail ]; then
        if ! tail -n 2000 "$SOURCE_FILE" > "$PAYLOAD_FILE"; then
            printf 'ERROR %s snapshot-failed\n' "$REL_PATH"
            return
        fi
    elif ! cp "$SOURCE_FILE" "$PAYLOAD_FILE"; then
        printf 'ERROR %s snapshot-failed\n' "$REL_PATH"
        return
    fi

    FILE_SHA=$(sha256sum "$PAYLOAD_FILE" 2>/dev/null | awk '{print $1}')
    FILE_BYTES=$(wc -c < "$PAYLOAD_FILE" | tr -d '[:space:]')
    case "$FILE_SHA" in
        ''|*[!0-9a-f]*)
            printf 'ERROR %s digest-failed\n' "$REL_PATH"
            return
            ;;
    esac
    case "$FILE_BYTES" in
        ''|*[!0-9]*)
            printf 'ERROR %s digest-failed\n' "$REL_PATH"
            return
            ;;
    esac
    if [ "${#FILE_SHA}" -ne 64 ]; then
        printf 'ERROR %s digest-failed\n' "$REL_PATH"
        return
    fi

    printf 'FILE %s %s %s\n' "$REL_PATH" "$FILE_SHA" "$FILE_BYTES"
    base64 < "$PAYLOAD_FILE" | tr -d '\n' | fold -w 1800 > "$ENCODED_FILE"
    if [ -s "$ENCODED_FILE" ]; then
        cat "$ENCODED_FILE"
        if [ -n "$(tail -c 1 "$ENCODED_FILE")" ]; then
            printf '\n'
        fi
    fi
    printf 'ENDFILE %s\n' "$REL_PATH"
    FILE_COUNT=$((FILE_COUNT + 1))
}

emit_path health.jsonl full
emit_path decisions.jsonl full
emit_path jobs/results.jsonl full
emit_path stream_state.json full
emit_path stream_log.jsonl tail

if [ -d "$RECEIPT_ROOT/proposals" ]; then
    find "$RECEIPT_ROOT/proposals" -maxdepth 1 -type f -name '*.json' \
        -print 2>/dev/null | LC_ALL=C sort > "$PROPOSAL_LIST"
    while IFS= read -r PROPOSAL_PATH; do
        case "$PROPOSAL_PATH" in
            "$RECEIPT_ROOT"/*)
                emit_path "${PROPOSAL_PATH#"$RECEIPT_ROOT"/}" full
                ;;
        esac
    done < "$PROPOSAL_LIST"
fi

emit_path basis-position.json full
emit_path momentum-position.json full
emit_path momentum-paper-receipts.jsonl full
emit_path commission-rates.json full

if [ -d "$RECEIPT_ROOT" ]; then
    find "$RECEIPT_ROOT" -maxdepth 1 -type f -name '*.json' \
        -printf '%T@\t%f\n' 2>/dev/null \
        | LC_ALL=C sort -rn -k1,1 \
        | sed -n '1,200p' \
        | cut -f2- > "$ROOT_JSON_LIST"
    while IFS= read -r ROOT_JSON; do
        [ -n "$ROOT_JSON" ] && emit_path "$ROOT_JSON" full
    done < "$ROOT_JSON_LIST"
fi

TODAY_UTC=$(date -u '+%F')
YESTERDAY_UTC=$(date -u -d "$TODAY_UTC - 1 day" '+%F' 2>/dev/null || true)
emit_path "account-governor-$TODAY_UTC.jsonl" full
if [ -n "$YESTERDAY_UTC" ]; then
    emit_path "account-governor-$YESTERDAY_UTC.jsonl" full
else
    printf 'ERROR yesterday-date-unavailable\n'
fi

for FLAG_NAME in STOP STOP-JOBS account-governor.lock; do
    if [ -e "$RECEIPT_ROOT/$FLAG_NAME" ]; then
        printf 'FLAG %s PRESENT\n' "$FLAG_NAME"
    else
        printf 'FLAG %s ABSENT\n' "$FLAG_NAME"
    fi
done

printf 'LIVEPULL END %s\n' "$FILE_COUNT"
