#!/bin/sh
# One-way Mac -> host sync for the live-edit ui/ zone. ui/live/ is excluded
# because it is populated by ui_pull.sh in the opposite direction.
#
# Deletions are uploaded as ui-sync-deletions.txt with one repo-relative
# ui/... path per line. This script does not apply host deletions; wiring and
# consuming that tombstone file is intentionally the Lead's host-side task.
#
# Environment:
#   REPO_ROOT       repository root (normally inferred from this script)
#   HOST_UI_ROOT    host destination root (default /home/ssm-user/quant-ui)
#   UI_SYNC_STATE   local JSON state file (default REPO_ROOT/.ui-sync-state.json)
#   SYNC_INTERVAL   seconds between cycles (default 4)
#   XFER_CMD        transfer command (default aws-connect/xfer.sh)
set -u
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEFAULT_REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
REPO_ROOT=${REPO_ROOT:-$DEFAULT_REPO_ROOT}
HOST_UI_ROOT=${HOST_UI_ROOT:-/home/ssm-user/quant-ui}
UI_SYNC_STATE=${UI_SYNC_STATE:-$REPO_ROOT/.ui-sync-state.json}
SYNC_INTERVAL=${SYNC_INTERVAL:-4}
XFER_CMD=${XFER_CMD:-$REPO_ROOT/aws-connect/xfer.sh}

usage() {
    echo "usage: $0 [--once] [--dry-run]" >&2
}

ONCE=0
DRY_RUN=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --once)
            ONCE=1
            ;;
        --dry-run)
            DRY_RUN=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage
            exit 2
            ;;
    esac
    shift
done

python3 - "$REPO_ROOT" "$HOST_UI_ROOT" "$UI_SYNC_STATE" \
    "$SYNC_INTERVAL" "$XFER_CMD" "$ONCE" "$DRY_RUN" <<'PY'
import hashlib
import json
import math
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath


repo_root = Path(sys.argv[1]).resolve()
host_ui_root = sys.argv[2]
state_path = Path(sys.argv[3])
xfer_text = sys.argv[5]
once = sys.argv[6] == "1"
dry_run = sys.argv[7] == "1"

MAX_BYTES = 5 * 1024 * 1024
SHA256_RE = re.compile(r"[0-9a-f]{64}")
SAFE_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
IGNORE_FILE_RE = re.compile(r"\.[A-Za-z0-9_-]+ignore")


class SyncError(Exception):
    pass


def validate_interval(value):
    try:
        interval = float(value)
    except ValueError as exc:
        raise SyncError("SYNC_INTERVAL must be a positive number") from exc
    if not math.isfinite(interval) or interval <= 0:
        raise SyncError("SYNC_INTERVAL must be a positive number")
    return interval


def validate_host_root(value):
    path = PurePosixPath(value)
    if (not value.startswith("/") or value == "/" or ".." in value
            or path.as_posix() != value):
        raise SyncError("HOST_UI_ROOT must be a normalized absolute path")
    if any(SAFE_COMPONENT_RE.fullmatch(part) is None for part in path.parts[1:]):
        raise SyncError("HOST_UI_ROOT contains a disallowed path component")
    return value


def validate_relpath(value, file_path=True):
    if not isinstance(value, str) or not value:
        return "path is empty or non-text"
    if any(char in value for char in ("\x00", "\n", "\r", "\t")):
        return "path contains a control character"
    if not value.startswith("ui/"):
        return "path is outside ui/"
    if ".." in value:
        return "path contains '..'"
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        return "path is not normalized"
    parts = path.parts
    if len(parts) < 2 or parts[0] != "ui":
        return "path is outside ui/"
    if parts[1] == "live":
        return "path is in excluded ui/live/"
    for index, part in enumerate(parts[1:], start=1):
        if SAFE_COMPONENT_RE.fullmatch(part) is not None:
            continue
        is_last = index == len(parts) - 1
        if file_path and is_last and IGNORE_FILE_RE.fullmatch(part) is not None:
            continue
        if part.startswith("."):
            return "dot path is not an ignore-style file"
        return "path component is not allowlisted"
    return None


def display_path(value):
    return value if isinstance(value, str) and value.isprintable() else repr(value)


def remote_path(relpath):
    return host_ui_root + "/" + relpath[len("ui/"):]


def load_state():
    if not state_path.exists():
        return {}, False
    try:
        if state_path.is_symlink():
            raise SyncError("state file must not be a symlink")
        with state_path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SyncError("state file is unreadable: %s" % exc) from exc
    if (not isinstance(payload, dict) or payload.get("version") != 1
            or not isinstance(payload.get("files"), dict)):
        raise SyncError("state file has an unsupported format")

    accepted = {}
    refused = False
    for relpath, digest in sorted(payload["files"].items()):
        reason = validate_relpath(relpath)
        if reason is not None:
            print("REFUSE state %s: %s" % (display_path(relpath), reason),
                  file=sys.stderr)
            refused = True
            continue
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            print("REFUSE state %s: invalid sha256" % display_path(relpath),
                  file=sys.stderr)
            refused = True
            continue
        accepted[relpath] = digest
    return accepted, refused


def write_state(files):
    parent = state_path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=".ui-sync-state.",
                                              dir=str(parent))
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump({"version": 1, "files": files}, stream,
                      sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, state_path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def hash_regular_file(path):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SyncError("not a regular file")
        if before.st_size > MAX_BYTES:
            raise SyncError("file exceeds 5MB")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BYTES:
                raise SyncError("file exceeds 5MB")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size,
                    after.st_mtime_ns)):
            raise SyncError("file changed while hashing")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def scan_ui():
    ui_root = repo_root / "ui"
    try:
        root_stat = os.lstat(ui_root)
    except OSError as exc:
        raise SyncError("ui root is unavailable: %s" % exc) from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise SyncError("ui root must be a real directory")

    files = {}
    present = set()
    refused = False
    walk_errors = []

    def walk_error(exc):
        walk_errors.append(exc)

    for directory, dirnames, filenames in os.walk(
            ui_root, topdown=True, followlinks=False, onerror=walk_error):
        dirnames.sort()
        filenames.sort()
        kept_dirs = []
        for name in dirnames:
            path = Path(directory) / name
            relpath = path.relative_to(repo_root).as_posix()
            if relpath == "ui/live":
                continue
            reason = validate_relpath(relpath, file_path=False)
            try:
                mode = os.lstat(path).st_mode
            except OSError as exc:
                print("REFUSE %s: unreadable: %s" % (relpath, exc),
                      file=sys.stderr)
                refused = True
                continue
            if reason is not None:
                print("REFUSE %s: %s" % (relpath, reason), file=sys.stderr)
                refused = True
                continue
            if stat.S_ISLNK(mode):
                present.add(relpath)
                print("REFUSE %s: symlink" % relpath, file=sys.stderr)
                refused = True
                continue
            if not stat.S_ISDIR(mode):
                present.add(relpath)
                print("REFUSE %s: not a directory" % relpath,
                      file=sys.stderr)
                refused = True
                continue
            kept_dirs.append(name)
        dirnames[:] = kept_dirs

        for name in filenames:
            path = Path(directory) / name
            relpath = path.relative_to(repo_root).as_posix()
            reason = validate_relpath(relpath)
            if reason is not None:
                print("REFUSE %s: %s" % (relpath, reason), file=sys.stderr)
                refused = True
                continue
            present.add(relpath)
            try:
                mode = os.lstat(path).st_mode
                if stat.S_ISLNK(mode):
                    raise SyncError("symlink")
                if not stat.S_ISREG(mode):
                    raise SyncError("not a regular file")
                files[relpath] = hash_regular_file(path)
            except (OSError, SyncError) as exc:
                print("REFUSE %s: %s" % (relpath, exc), file=sys.stderr)
                refused = True

    for exc in walk_errors:
        print("REFUSE ui/: scan error: %s" % exc, file=sys.stderr)
        refused = True
    return files, present, refused


def snapshot_file(source, expected_digest, directory):
    target = Path(directory) / "payload"
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    source_fd = os.open(source, flags)
    try:
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            raise SyncError("source stopped being a regular file")
        if source_stat.st_size > MAX_BYTES:
            raise SyncError("source exceeds 5MB")
        digest = hashlib.sha256()
        total = 0
        with target.open("wb") as output:
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_BYTES:
                    raise SyncError("source exceeds 5MB")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        after = os.fstat(source_fd)
        if ((source_stat.st_dev, source_stat.st_ino, source_stat.st_size,
             source_stat.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size,
                    after.st_mtime_ns)):
            raise SyncError("source changed during snapshot")
        if digest.hexdigest() != expected_digest:
            raise SyncError("source changed after scan")
    finally:
        os.close(source_fd)
    return target


def run_xfer(source, destination, label):
    try:
        completed = subprocess.run(
            xfer_command + [str(source), destination],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            check=False,
        )
    except OSError as exc:
        print("SYNC failed %s: transfer could not start: %s" % (label, exc),
              file=sys.stderr)
        return False
    marker = any(line.strip() == "XFER_VERIFIED"
                 for line in completed.stdout.splitlines())
    if completed.returncode != 0 or not marker:
        print("SYNC failed %s: xfer_status=%d sha_verified=%s" %
              (label, completed.returncode, "yes" if marker else "no"),
              file=sys.stderr)
        return False
    print("SYNC verified %s" % label)
    return True


def persist(files):
    try:
        write_state(files)
    except OSError as exc:
        print("SYNC state write failed: %s" % exc, file=sys.stderr)
        return False
    return True


def run_cycle():
    try:
        state, refused_state = load_state()
        current, present, refused_scan = scan_ui()
    except SyncError as exc:
        print("SYNC error: %s" % exc, file=sys.stderr)
        return 1

    failed = refused_state or refused_scan
    changed = [relpath for relpath in sorted(current)
               if state.get(relpath) != current[relpath]]
    deleted = [relpath for relpath in sorted(state)
               if relpath not in current and relpath not in present]

    if dry_run:
        for relpath in changed:
            print("DRY-RUN transfer %s -> %s" %
                  (relpath, remote_path(relpath)))
        for relpath in deleted:
            print("DRY-RUN delete %s" % relpath)
        if deleted:
            print("DRY-RUN transfer ui-sync-deletions.txt -> %s" %
                  (host_ui_root + "/ui-sync-deletions.txt"))
        return 1 if failed else 0

    for relpath in changed:
        destination = remote_path(relpath)
        print("SYNC transfer %s -> %s" % (relpath, destination))
        try:
            with tempfile.TemporaryDirectory(prefix="ui-sync.") as temp_dir:
                source = snapshot_file(repo_root / relpath,
                                       current[relpath], temp_dir)
                verified = run_xfer(source, destination, relpath)
        except (OSError, SyncError) as exc:
            print("SYNC failed %s: %s" % (relpath, exc), file=sys.stderr)
            verified = False
        if not verified:
            failed = True
            continue
        state[relpath] = current[relpath]
        if not persist(state):
            failed = True

    if deleted:
        for relpath in deleted:
            print("SYNC delete %s" % relpath)
        with tempfile.TemporaryDirectory(prefix="ui-sync.") as temp_dir:
            tombstone = Path(temp_dir) / "ui-sync-deletions.txt"
            tombstone.write_text("".join(path + "\n" for path in deleted),
                                 encoding="utf-8")
            destination = host_ui_root + "/ui-sync-deletions.txt"
            print("SYNC transfer ui-sync-deletions.txt -> %s" % destination)
            if run_xfer(tombstone, destination, "ui-sync-deletions.txt"):
                for relpath in deleted:
                    state.pop(relpath, None)
                if not persist(state):
                    failed = True
            else:
                failed = True

    return 1 if failed else 0


try:
    interval = validate_interval(sys.argv[4])
    host_ui_root = validate_host_root(host_ui_root)
    xfer_command = shlex.split(xfer_text)
    if not xfer_command:
        raise SyncError("XFER_CMD must not be empty")
except SyncError as exc:
    print("SYNC configuration error: %s" % exc, file=sys.stderr)
    raise SystemExit(2)

if once:
    raise SystemExit(run_cycle())

try:
    while True:
        run_cycle()
        time.sleep(interval)
except KeyboardInterrupt:
    raise SystemExit(130)
PY
