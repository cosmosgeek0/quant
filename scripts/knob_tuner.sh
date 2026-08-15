#!/bin/bash
# One hourly, evidence-bound adjustment pass for non-money runtime knobs.
set -u
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -f "$SCRIPT_DIR/../src/runtime_paths.py" ]; then
  DEPLOY_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
elif [ -f "$SCRIPT_DIR/src/runtime_paths.py" ]; then
  DEPLOY_DIR=$SCRIPT_DIR
else
  echo "KNOB-TUNER | paths module unavailable" >&2
  exit 2
fi

if ! PATH_ASSIGNMENTS=$(PYTHONDONTWRITEBYTECODE=1 \
  python3 "$DEPLOY_DIR/src/runtime_paths.py" --shell); then
  echo "KNOB-TUNER | paths unresolved" >&2
  exit 2
fi
eval "$PATH_ASSIGNMENTS"
export RECEIPT_DIR
export PYTHONDONTWRITEBYTECODE=1

mkdir -p "$RECEIPT_DIR" || exit 2

python3 - "$RECEIPT_DIR/funnel.jsonl" "$DEPLOY_DIR/knobs.env" \
  "$HEALTH" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


FUNNEL = Path(sys.argv[1])
KNOBS = Path(sys.argv[2])
HEALTH = Path(sys.argv[3])
KNOB_ORDER = (
    "QUANT_IGNITION_SURGE",
    "QUANT_MAX_ENTRIES_DAY",
    "QUANT_DAILY_LOSS_CAP",
)
DEFAULTS = {
    "QUANT_IGNITION_SURGE": "3",
    "QUANT_MAX_ENTRIES_DAY": "10",
    "QUANT_DAILY_LOSS_CAP": "2.00",
}
IGNITION_STAGES = frozenset(("ignited", "ignition"))
OWNER_TICKET_STAGES = frozenset(("ticket_eligible", "entered"))
WINDOW = timedelta(hours=1)
SURGE_STEP = Decimal("0.5")
SURGE_FLOOR = Decimal("1.5")


def utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def parse_utc(raw: object) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise ValueError("utc is absent")
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError("utc is invalid") from None
    if value.tzinfo is None:
        raise ValueError("utc is naive")
    return value.astimezone(timezone.utc)


def append_health(row: dict[str, object]) -> None:
    HEALTH.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(row, sort_keys=True, separators=(",", ":"))
               + "\n").encode("ascii")
    descriptor = os.open(
        str(HEALTH), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600
    )
    try:
        written = os.write(descriptor, payload)
        if written != len(payload):
            raise OSError("short health append")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def health_row(now: datetime, **updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        "stage": "knob_tuner",
        "action": "noop",
        "change": None,
        "reason": "unknown",
        "rows": 0,
        "utc": utc_text(now),
    }
    row.update(updates)
    return row


def append_noop(now: datetime, reason: str, **updates: object) -> int:
    append_health(health_row(now, reason=reason, **updates))
    return 0


def finite_positive_decimal(raw: str, name: str) -> Decimal:
    try:
        value = Decimal(raw)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{name} is invalid") from None
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{name} is invalid")
    return value


def validate_knobs(values: dict[str, str]) -> None:
    surge = finite_positive_decimal(
        values["QUANT_IGNITION_SURGE"], "QUANT_IGNITION_SURGE"
    )
    if surge > Decimal("100"):
        raise ValueError("QUANT_IGNITION_SURGE is invalid")

    entries = values["QUANT_MAX_ENTRIES_DAY"]
    if re.fullmatch(r"[0-9]+", entries) is None:
        raise ValueError("QUANT_MAX_ENTRIES_DAY is invalid")
    if not 1 <= int(entries) <= 24:
        raise ValueError("QUANT_MAX_ENTRIES_DAY is invalid")

    loss = finite_positive_decimal(
        values["QUANT_DAILY_LOSS_CAP"], "QUANT_DAILY_LOSS_CAP"
    )
    if loss > Decimal("10.00"):
        raise ValueError("QUANT_DAILY_LOSS_CAP is invalid")


def load_knobs() -> dict[str, str]:
    values = {
        name: os.environ.get(name, DEFAULTS[name]) for name in KNOB_ORDER
    }
    if KNOBS.exists():
        seen: set[str] = set()
        try:
            lines = KNOBS.read_text(encoding="ascii").splitlines()
        except (OSError, UnicodeError):
            raise ValueError("knobs file is unreadable") from None
        for line in lines:
            if not line or "=" not in line:
                raise ValueError("knobs file has an invalid line")
            name, value = line.split("=", 1)
            if name not in DEFAULTS or name in seen or not value:
                raise ValueError("knobs file has an invalid assignment")
            seen.add(name)
            values[name] = value
    if any(not isinstance(value, str) for value in values.values()):
        raise ValueError("knob value is invalid")
    validate_knobs(values)
    return values


def decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def write_knobs(values: dict[str, str]) -> None:
    payload = "".join(f"{name}={values[name]}\n" for name in KNOB_ORDER)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".knobs.env.", dir=str(KNOBS.parent)
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, KNOBS)
        directory = os.open(str(KNOBS.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def evidence_was_processed(evidence_id: str) -> bool:
    if not HEALTH.is_file():
        return False
    try:
        with HEALTH.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(row, dict) \
                        and row.get("stage") == "knob_tuner" \
                        and row.get("evidence_id") == evidence_id:
                    return True
    except (OSError, UnicodeError):
        return True
    return False


def recent_funnel_rows(
    now: datetime,
) -> tuple[list[tuple[datetime, int, dict[str, object]]], int]:
    rows: list[tuple[datetime, int, dict[str, object]]] = []
    invalid = 0
    try:
        with FUNNEL.open(encoding="utf-8") as handle:
            for sequence, line in enumerate(handle):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                    if not isinstance(payload, dict):
                        raise ValueError("row is not an object")
                    row_utc = parse_utc(payload.get("utc"))
                except (TypeError, ValueError):
                    invalid += 1
                    continue
                if row_utc < now - WINDOW or row_utc > now:
                    continue
                entered = payload.get("entered")
                starving_stage = payload.get("starving_stage")
                if type(entered) is not int or entered < 0 \
                        or not isinstance(starving_stage, str) \
                        or not starving_stage:
                    invalid += 1
                    continue
                rows.append((row_utc, sequence, payload))
    except (OSError, UnicodeError):
        return [], -1
    rows.sort(key=lambda item: (item[0], item[1]))
    return rows, invalid


def evidence_id(rows: list[tuple[datetime, int, dict[str, object]]]) -> str:
    digest = hashlib.sha256()
    for _row_utc, _sequence, payload in rows:
        digest.update(json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def main() -> int:
    now = datetime.now(timezone.utc)
    if not FUNNEL.exists():
        return append_noop(now, "funnel_absent")

    rows, invalid = recent_funnel_rows(now)
    if invalid < 0:
        return append_noop(now, "funnel_unreadable")
    if invalid:
        return append_noop(now, "funnel_invalid", invalid_rows=invalid)
    if not rows:
        return append_noop(now, "no_recent_rows")

    identity = evidence_id(rows)
    newest = rows[-1][2]
    starving_stage = newest["starving_stage"]
    common = {
        "evidence_id": identity,
        "rows": len(rows),
        "starving_stage": starving_stage,
    }
    if evidence_was_processed(identity):
        return append_noop(now, "already_processed", **common)

    entered = sum(row[2]["entered"] for row in rows)
    if entered != 0:
        return append_noop(now, "entries_present", entered=entered, **common)

    if starving_stage == "fresh_ok":
        append_health(health_row(
            now,
            action="advisory",
            reason="freshness_advisory",
            **common,
        ))
        return 0
    if starving_stage in OWNER_TICKET_STAGES:
        append_health(health_row(
            now,
            action="advisory",
            reason="owner_ticket_advisory",
            **common,
        ))
        return 0
    if starving_stage not in IGNITION_STAGES:
        append_health(health_row(
            now,
            action="advisory",
            reason="unmapped_stage",
            **common,
        ))
        return 0

    try:
        values = load_knobs()
    except ValueError:
        append_health(health_row(now, reason="knobs_invalid", **common))
        return 2

    before = finite_positive_decimal(
        values["QUANT_IGNITION_SURGE"], "QUANT_IGNITION_SURGE"
    )
    if before <= SURGE_FLOOR:
        return append_noop(now, "surge_floor_reached", **common)
    after = max(SURGE_FLOOR, before - SURGE_STEP)
    if (KNOBS.parent / "TUNER_ADVISORY").exists():
        # Owner governance word: advisory-only — recommend, never mutate.
        append_health(health_row(
            now, action="advisory", reason="tuner_advisory_mode",
            recommended_from=decimal_text(before),
            recommended_to=decimal_text(after), **common))
        return 0
    values["QUANT_IGNITION_SURGE"] = decimal_text(after)
    try:
        write_knobs(values)
    except OSError:
        append_health(health_row(now, reason="knobs_write_failed", **common))
        return 2

    append_health(health_row(
        now,
        action="changed",
        reason="ignition_starved",
        change={
            "from": decimal_text(before),
            "knob": "QUANT_IGNITION_SURGE",
            "step": "-0.5",
            "to": decimal_text(after),
        },
        **common,
    ))
    return 0


try:
    raise SystemExit(main())
except OSError:
    print("KNOB-TUNER | local persistence failed", file=sys.stderr)
    raise SystemExit(2)
PY
