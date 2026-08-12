#!/bin/bash
# xfer.sh <local-file> <remote-path> [lines-per-batch] — transfer a file to the cosmos host.
#
# The IAM operator holds ssm:StartSession on the instance only — no SendCommand, no port
# forwarding, no S3 — so the only channel is the interactive shell. The PTY drops input past
# ~4KB on a single line and drops lines under sustained volume, so the file is base64'd, folded
# to 1800-char lines, and appended in verified batches: after each batch the remote byte count
# must equal the expected prefix length, or the staging file is truncated back to the last good
# offset and the batch is retried. Final integrity is proved by sha256 on both ends.
set -uo pipefail
SRC="${1:?usage: xfer.sh <local-file> <remote-path> [lines-per-batch]}"
DST="${2:?usage: xfer.sh <local-file> <remote-path> [lines-per-batch]}"
BATCH="${3:-600}"
RETRIES=4
SP="$(mktemp -d)"
RUN="$(cd "$(dirname "$0")" && pwd)/ssmrun.sh"

# fold(1) emits no trailing newline on the final short line, so wc -l undercounts by one and
# the last chunk is silently never sent. Force the terminator, then count records not newlines.
base64 < "$SRC" | tr -d '\n' | fold -w 1800 > "$SP/lines"
[ -n "$(tail -c 1 "$SP/lines")" ] && printf '\n' >> "$SP/lines"
TOTAL=$(awk 'END{print NR}' "$SP/lines")
LOCAL_SHA=$(shasum -a 256 "$SRC" | cut -d' ' -f1)
echo "src=$(basename "$SRC") bytes=$(wc -c < "$SRC" | tr -d ' ') lines=$TOTAL batch=$BATCH"

printf 'mkdir -p %s\n: > %s.b64\necho STAGED\n' "$(dirname "$DST")" "$DST" > "$SP/s0"
bash "$RUN" "$SP/s0" 6 >/dev/null 2>&1

done_lines=0; i=0
while [ "$done_lines" -lt "$TOTAL" ]; do
  i=$((i+1))
  end=$((done_lines+BATCH)); [ "$end" -gt "$TOTAL" ] && end=$TOTAL
  EXP=$(head -n "$end" "$SP/lines" | tr -d '\n' | wc -c | tr -d ' ')
  PREV=$(head -n "$done_lines" "$SP/lines" | tr -d '\n' | wc -c | tr -d ' ')
  ok=0
  for try in $(seq 1 $RETRIES); do
    {
      # truncate back to the last verified offset, then append this batch
      printf 'truncate -s %s %s.b64\n' "$PREV" "$DST"
      sed -n "$((done_lines+1)),${end}p" "$SP/lines" \
        | sed "s|^|printf '%s' '|; s|\$|' >> $DST.b64|"
      printf 'echo MARK_%s_%s=$(wc -c < %s.b64)\n' "$i" "$try" "$DST"
    } > "$SP/s"
    bash "$RUN" "$SP/s" $(( BATCH / 60 + 12 )) > "$SP/o" 2>&1
    GOT=$(grep -oE "MARK_${i}_${try}=[0-9]+" "$SP/o" | tail -1 | cut -d= -f2)
    if [ "${GOT:-0}" = "$EXP" ]; then ok=1; break; fi
    echo "  batch $i try $try: got ${GOT:-NONE} want $EXP — retrying"
  done
  [ "$ok" = 1 ] || { echo "XFER_FAILED at batch $i"; exit 1; }
  done_lines=$end
  echo "batch $i ok: $done_lines/$TOTAL lines ($EXP bytes staged)"
done

cat > "$SP/fin" <<EOS
base64 -d < $DST.b64 > $DST && rm -f $DST.b64
echo FIN_SHA=\$(sha256sum $DST | cut -d' ' -f1)
echo FIN_BYTES=\$(wc -c < $DST)
EOS
bash "$RUN" "$SP/fin" 25 > "$SP/ofin" 2>&1
RS=$(grep -oE 'FIN_SHA=[0-9a-f]{64}' "$SP/ofin" | tail -1 | cut -d= -f2)
echo "local_sha =$LOCAL_SHA"
echo "remote_sha=${RS:-NONE}"
[ "$RS" = "$LOCAL_SHA" ] && echo "XFER_VERIFIED" || { echo "XFER_FAILED (sha mismatch)"; exit 1; }
