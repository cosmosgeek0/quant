#!/bin/sh
# Pull and verify the host receipt snapshot used by the read-only UI.
set -u
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
SSMRUN=$REPO_ROOT/aws-connect/ssmrun.sh
# The printer must NOT stream through the PTY (it drops lines under volume):
# the Lead xfers scripts/host_print_live.sh to the host once; each cycle runs
# a one-line driver that executes that host-side copy.
HOST_PRINTER_REMOTE=${HOST_PRINTER_REMOTE:-/home/ssm-user/quant-run/host_print_live.sh}
HOST_PRINTER=$(mktemp "${TMPDIR:-/tmp}/ui-pull-driver.XXXXXX")
printf 'bash %s\n' "$HOST_PRINTER_REMOTE" > "$HOST_PRINTER"
LIVE_DIR=${UI_LIVE_DIR:-$REPO_ROOT/ui/live}
CYCLE=${CYCLE:-30}
SSM_WAIT=${SSM_WAIT:-45}

usage() {
    echo "usage: $0 [--once] [--transcript FILE]" >&2
}

case "$CYCLE" in
    ''|*[!0-9]*|0) echo "CYCLE must be a positive integer" >&2; exit 2 ;;
esac
case "$SSM_WAIT" in
    ''|*[!0-9]*|0) echo "SSM_WAIT must be a positive integer" >&2; exit 2 ;;
esac

ONCE=0
TRANSCRIPT=
while [ "$#" -gt 0 ]; do
    case "$1" in
        --once)
            ONCE=1
            shift
            ;;
        --transcript)
            [ "$#" -ge 2 ] || { usage; exit 2; }
            TRANSCRIPT=$2
            shift 2
            ;;
        *)
            usage
            exit 2
            ;;
    esac
done

CURRENT_TRANSCRIPT=
cleanup() {
    if [ -n "$CURRENT_TRANSCRIPT" ]; then
        rm -f "$CURRENT_TRANSCRIPT"
    fi
}
trap cleanup 0
trap 'exit 130' 1 2 15

parse_transcript() {
    PARSE_FILE=$1
    PARSE_STARTED_NS=$2
    PARSE_TRANSPORT_STATUS=$3
    python3 - "$PARSE_FILE" "$LIVE_DIR" "$PARSE_STARTED_NS" \
        "$PARSE_TRANSPORT_STATUS" <<'PY'
import base64
import binascii
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


transcript_path = Path(sys.argv[1])
live_root = Path(sys.argv[2])
try:
    started_ns = int(sys.argv[3])
except ValueError:
    started_ns = time.monotonic_ns()
try:
    transport_status = int(sys.argv[4])
except ValueError:
    transport_status = 1

errors = []
writes = {}
absences = set()
flags = {}
invalid_paths = set()
file_headers = 0
begin_utc = None
ended = False
reserved = {"pull_manifest.json", "STOP", "STOP-JOBS",
            "account-governor.lock"}


def add_error(message):
    if message not in errors:
        errors.append(message)


def safe_relpath(value):
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or str(path) != value
            or any(part in ("", ".", "..") for part in path.parts)
            or value in reserved):
        add_error("unsafe relpath: %s" % value)
        return None
    return value


def invalidate(value):
    invalid_paths.add(value)
    writes.pop(value, None)
    absences.discard(value)


def is_control(line):
    return line.startswith(("FILE ", "ENDFILE ", "ABSENT ", "FLAG ",
                            "ERROR ", "LIVEPULL BEGIN ", "LIVEPULL END "))


if transport_status != 0:
    add_error("ssmrun exit status %d" % transport_status)

try:
    raw_lines = transcript_path.read_bytes().decode(
        "utf-8", errors="replace").splitlines()
except OSError as exc:
    raw_lines = []
    add_error("transcript read failed: %s" % exc)

lines = [line.rstrip("\r") for line in raw_lines]
begin_index = None
for index, line in enumerate(lines):
    match = re.fullmatch(r"LIVEPULL BEGIN (\S+)", line)
    if match:
        if begin_index is not None:
            add_error("duplicate LIVEPULL BEGIN")
            break
        begin_index = index
        begin_utc = match.group(1)

if begin_index is None:
    add_error("missing LIVEPULL BEGIN")
else:
    index = begin_index + 1
    try:
        while index < len(lines):
            line = lines[index]
            file_match = re.fullmatch(
                r"FILE (\S+) ([0-9a-f]{64}) ([0-9]+)", line)
            if file_match:
                relpath, expected_sha, expected_bytes_text = file_match.groups()
                file_headers += 1
                checked_relpath = safe_relpath(relpath)
                payload_lines = []
                block_bad = checked_relpath is None
                closed = False
                cursor = index + 1
                while cursor < len(lines):
                    payload_line = lines[cursor]
                    end_match = re.fullmatch(r"ENDFILE (\S+)", payload_line)
                    if end_match:
                        closed = True
                        if end_match.group(1) != relpath:
                            add_error("ENDFILE mismatch: %s" % relpath)
                            block_bad = True
                        break
                    if is_control(payload_line):
                        add_error("truncated FILE block: %s" % relpath)
                        block_bad = True
                        break
                    if payload_line:
                        if (len(payload_line) > 1800
                                or re.fullmatch(r"[A-Za-z0-9+/=]+",
                                                payload_line) is None):
                            add_error("invalid base64 line: %s" % relpath)
                            block_bad = True
                        else:
                            payload_lines.append(payload_line)
                    cursor += 1
                if not closed:
                    add_error("truncated FILE block: %s" % relpath)
                    block_bad = True
                if checked_relpath in writes or checked_relpath in absences:
                    add_error("duplicate path: %s" % relpath)
                    block_bad = True
                if checked_relpath in invalid_paths:
                    block_bad = True
                if not block_bad:
                    try:
                        payload = base64.b64decode(
                            "".join(payload_lines).encode("ascii"),
                            validate=True)
                    except (binascii.Error, ValueError):
                        add_error("base64 decode failed: %s" % relpath)
                        block_bad = True
                    else:
                        expected_bytes = int(expected_bytes_text)
                        if len(payload) != expected_bytes:
                            add_error("byte count mismatch: %s" % relpath)
                            block_bad = True
                        if hashlib.sha256(payload).hexdigest() != expected_sha:
                            add_error("sha256 mismatch: %s" % relpath)
                            block_bad = True
                if block_bad and checked_relpath is not None:
                    invalidate(checked_relpath)
                elif checked_relpath is not None:
                    writes[checked_relpath] = payload
                index = cursor + 1 if closed else cursor
                continue

            if line.startswith("FILE "):
                add_error("malformed FILE header")
                index += 1
                continue

            absent_match = re.fullmatch(r"ABSENT (\S+)", line)
            if absent_match:
                relpath = safe_relpath(absent_match.group(1))
                if relpath is not None:
                    if (relpath in writes or relpath in absences
                            or relpath in invalid_paths):
                        add_error("duplicate path: %s" % relpath)
                        invalidate(relpath)
                    else:
                        absences.add(relpath)
                index += 1
                continue

            flag_match = re.fullmatch(
                r"FLAG (STOP|STOP-JOBS|account-governor\.lock) "
                r"(PRESENT|ABSENT)", line)
            if flag_match:
                name, state = flag_match.groups()
                if name in flags:
                    add_error("duplicate FLAG: %s" % name)
                else:
                    flags[name] = state
                index += 1
                continue

            end_match = re.fullmatch(r"LIVEPULL END ([0-9]+)", line)
            if end_match:
                ended = True
                declared = int(end_match.group(1))
                if declared != file_headers:
                    add_error("file count mismatch: declared %d saw %d" %
                              (declared, file_headers))
                break

            if line.startswith("ERROR "):
                add_error("host: %s" % line[6:])
            elif line.startswith("LIVEPULL BEGIN "):
                add_error("duplicate LIVEPULL BEGIN")
            index += 1
    except Exception as exc:  # keep the manifest path alive on parser defects
        add_error("parser failure: %s" % exc)

if not ended:
    add_error("truncated transcript: missing LIVEPULL END")

try:
    live_root.mkdir(parents=True, exist_ok=True)
    resolved_root = live_root.resolve()
except OSError as exc:
    print("PULL manifest unavailable: %s" % exc, file=sys.stderr)
    raise SystemExit(1)


def destination(relpath, create_parent):
    pure = PurePosixPath(relpath)
    target = live_root.joinpath(*pure.parts)
    parent = target.parent
    if create_parent:
        parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = parent.resolve(strict=False)
    try:
        resolved_parent.relative_to(resolved_root)
    except ValueError as exc:
        raise OSError("destination escapes live root") from exc
    return target


def atomic_write(target, content):
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=".ui-pull-", dir=str(target.parent))
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


written_files = 0
for relpath, payload in writes.items():
    if relpath in invalid_paths:
        continue
    try:
        atomic_write(destination(relpath, True), payload)
    except OSError as exc:
        add_error("write failed: %s: %s" % (relpath, exc))
    else:
        written_files += 1

# Adding a present marker is safe even if another block failed. Destructive
# mirror actions wait until the whole cycle has remained error-free.
for flag_name, state in flags.items():
    if state != "PRESENT":
        continue
    try:
        atomic_write(destination(flag_name, True), b"")
    except OSError as exc:
        add_error("flag write failed: %s: %s" % (flag_name, exc))

if not errors:
    for relpath in sorted(absences):
        try:
            destination(relpath, False).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            add_error("remove failed: %s: %s" % (relpath, exc))
    if not errors:
        for flag_name, state in flags.items():
            if state != "ABSENT":
                continue
            try:
                destination(flag_name, False).unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                add_error("flag remove failed: %s: %s" % (flag_name, exc))

if begin_utc is None:
    manifest_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
else:
    manifest_utc = begin_utc
cycle_ms = max(0, (time.monotonic_ns() - started_ns) // 1_000_000)
manifest = {
    "utc": manifest_utc,
    "ok": not errors,
    "interval_ms": int(os.environ.get("SYNC_INTERVAL_MS", "45000")),
    "cycle_ms": cycle_ms,
    "files": written_files,
    "errors": errors,
}
manifest_target = live_root / "pull_manifest.json"
try:
    atomic_write(
        manifest_target,
        (json.dumps(manifest, separators=(",", ":")) + "\n").encode("utf-8"))
except OSError as exc:
    print("PULL manifest write failed: %s" % exc, file=sys.stderr)
    raise SystemExit(1)
raise SystemExit(0 if manifest["ok"] else 1)
PY
}

monotonic_ns() {
    python3 -c 'import time; print(time.monotonic_ns())'
}

if [ -n "$TRANSCRIPT" ]; then
    STARTED_NS=$(monotonic_ns)
    parse_transcript "$TRANSCRIPT" "$STARTED_NS" 0
    exit $?
fi

run_cycle() {
    STARTED_NS=$(monotonic_ns)
    CURRENT_TRANSCRIPT=$(mktemp "${TMPDIR:-/tmp}/ui-pull.XXXXXX") || return 1
    if "$SSMRUN" "$HOST_PRINTER" "$SSM_WAIT" \
        > "$CURRENT_TRANSCRIPT" 2>&1; then
        TRANSPORT_STATUS=0
    else
        TRANSPORT_STATUS=$?
    fi
    parse_transcript "$CURRENT_TRANSCRIPT" "$STARTED_NS" "$TRANSPORT_STATUS"
    PARSE_STATUS=$?
    rm -f "$CURRENT_TRANSCRIPT"
    CURRENT_TRANSCRIPT=
    return "$PARSE_STATUS"
}

if [ "$ONCE" -eq 1 ]; then
    run_cycle
    exit $?
fi

while :; do
    run_cycle || true
    sleep "$CYCLE"
done
