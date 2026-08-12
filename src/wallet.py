#!/usr/bin/env python3
"""Read Binance Spot/USD-M balances and perform receipted internal transfers.

Balance reads are signed but read-only.  Transfers use Binance's universal
transfer endpoint for Spot <-> USD-M Futures.  That endpoint does not accept a
client-supplied idempotency key, so every attempt gets a durable local request
ID and baseline history snapshot before the single POST.  The returned
exchange ``tranId`` (or a unique new history row after an unclear response) is
then recorded.  A transfer POST is never retried; unresolved receipts may only
be reconciled with read-only history queries.

Environment (values are read but never printed):
  BINANCE_API_KEY, BINANCE_API_SECRET, QUANT_SHA
Optional:
  BINANCE_BASE_URL, BINANCE_FUTURES_BASE_URL, WALLET_RECEIPT_DIR
"""

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation


SPOT_BASE = os.environ.get("BINANCE_BASE_URL", "https://api.binance.com")
FUTURES_BASE = os.environ.get(
    "BINANCE_FUTURES_BASE_URL", "https://fapi.binance.com"
)
RECEIPT_DIR = os.path.expanduser(
    os.environ.get("WALLET_RECEIPT_DIR", "~/quant_receipts/wallet")
)
RECV_WINDOW = "5000"
HTTP_TIMEOUT = 15
READ_TRIES = 3
QUERY_ATTEMPTS = 8
QUERY_SLEEP = 2.0
HISTORY_LOOKBACK_MS = 60_000
HISTORY_PAGE_SIZE = 100
HISTORY_PAGE_LIMIT = 20

DIRECTIONS = {
    "spot-to-usdm": "MAIN_UMFUTURE",
    "usdm-to-spot": "UMFUTURE_MAIN",
}
TERMINAL_TRANSFER = {"CONFIRMED", "FAILED"}
ASSET_RE = re.compile(r"^[A-Z0-9]{1,20}$")
REQUEST_ID_RE = re.compile(r"^l3w[0-9a-f]{20,40}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


def utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def now_ms():
    return int(time.time() * 1000)


def emit(row):
    print(json.dumps(row, separators=(",", ":"), sort_keys=True))


def decimal(raw, label):
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("%s must be a decimal" % label)
    if not value.is_finite():
        raise ValueError("%s must be finite" % label)
    return value


def decimal_text(value):
    return format(value, "f")


def last4(value):
    text = str(value or "")
    return text[-4:] if text else ""


def safe_api_error(kind, payload):
    row = {"kind": kind}
    if isinstance(payload, dict):
        if isinstance(payload.get("code"), int):
            row["code"] = payload["code"]
        if isinstance(payload.get("http"), int):
            row["http"] = payload["http"]
        if isinstance(payload.get("error"), str):
            row["error"] = payload["error"][:80]
    return row


def first_failure(receipt, where, kind, payload=None):
    if "first_failure" not in receipt:
        receipt["first_failure"] = {
            "utc": utc(),
            "where": where,
            **safe_api_error(kind, payload or {}),
        }


class ReceiptStore:
    def __init__(self, root):
        self.root = os.path.realpath(os.path.expanduser(root))
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        self.lock_path = os.path.join(self.root, "transfer.lock")

    def path(self, kind, request_id):
        return os.path.join(self.root, "%s-%s.json" % (kind, request_id))

    def create(self, kind, request_id, payload):
        path = self.path(kind, request_id)
        encoded = self._encoded(payload)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        return path

    def save(self, path, payload):
        if os.path.dirname(os.path.realpath(path)) != self.root:
            raise ValueError("receipt path is outside WALLET_RECEIPT_DIR")
        tmp = "%s.tmp-%s-%s" % (path, os.getpid(), secrets.token_hex(2))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(self._encoded(payload))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def load_transfer(self, request_id):
        if not REQUEST_ID_RE.fullmatch(request_id):
            raise ValueError("invalid L3 wallet request id")
        path = self.path("transfer", request_id)
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        if payload.get("request_id") != request_id:
            raise ValueError("receipt request id mismatch")
        return path, payload

    def acquire_lock(self, request_id):
        try:
            fd = os.open(
                self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(request_id + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True

    def lock_owner_last4(self):
        try:
            with open(self.lock_path, encoding="ascii") as handle:
                return last4(handle.read().strip())
        except OSError:
            return ""

    def release_lock(self, request_id):
        try:
            with open(self.lock_path, encoding="ascii") as handle:
                owner = handle.read().strip()
        except FileNotFoundError:
            return
        if owner != request_id:
            raise RuntimeError("wallet transfer lock belongs to another request")
        os.unlink(self.lock_path)

    @staticmethod
    def _encoded(payload):
        return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()


class BinanceClient:
    def __init__(self, api_key, api_secret):
        self.api_key = api_key
        self.api_secret = api_secret
        self.offsets = {}

    def _once(self, method, base, path, params=None, signed=False):
        values = dict(params or {})
        if signed:
            if base not in self.offsets:
                return "unclear", {"error": "TimeNotSynchronized"}
            values["timestamp"] = str(now_ms() + self.offsets[base])
            values["recvWindow"] = RECV_WINDOW
        query = urllib.parse.urlencode(values)
        if signed:
            signature = hmac.new(
                self.api_secret.encode(), query.encode(), hashlib.sha256
            ).hexdigest()
            query += "&signature=" + signature
        url = base + path + ("?" + query if query else "")
        request = urllib.request.Request(url, method=method)
        if self.api_key:
            request.add_header("X-MBX-APIKEY", self.api_key)
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                return "ok", json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read(4096).decode(errors="replace")
            if exc.code >= 500 or exc.code in (408, 409):
                return "unclear", {"http": exc.code}
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict) and isinstance(parsed.get("code"), int):
                return "api_error", {"code": parsed["code"]}
            return "unclear", {"http": exc.code}
        except Exception as exc:
            return "unclear", {"error": type(exc).__name__}

    def read(self, base, path, params=None, signed=False, tries=READ_TRIES):
        kind, payload = "unclear", {}
        for attempt in range(tries):
            kind, payload = self._once("GET", base, path, params, signed)
            if kind != "unclear":
                return kind, payload
            if attempt + 1 < tries:
                time.sleep(1 + attempt)
        return kind, payload

    def sync_time(self, base, path):
        kind, payload = self.read(base, path, tries=READ_TRIES)
        if kind != "ok" or not isinstance(payload, dict):
            return kind, payload
        server_time = payload.get("serverTime")
        if not isinstance(server_time, int):
            return "schema_error", {"error": "MissingServerTime"}
        self.offsets[base] = server_time - now_ms()
        return "ok", {"serverTime": server_time}

    def spot_balances(self):
        if SPOT_BASE not in self.offsets:
            kind, payload = self.sync_time(SPOT_BASE, "/api/v3/time")
            if kind != "ok":
                return kind, payload
        return self.read(SPOT_BASE, "/api/v3/account", signed=True)

    def futures_balances(self):
        if FUTURES_BASE not in self.offsets:
            kind, payload = self.sync_time(FUTURES_BASE, "/fapi/v1/time")
            if kind != "ok":
                return kind, payload
        return self.read(FUTURES_BASE, "/fapi/v3/balance", signed=True)

    def transfer_history(self, transfer_type, start_ms, end_ms):
        if SPOT_BASE not in self.offsets:
            kind, payload = self.sync_time(SPOT_BASE, "/api/v3/time")
            if kind != "ok":
                return kind, payload
        rows = []
        page = 1
        while page <= HISTORY_PAGE_LIMIT:
            kind, payload = self.read(
                SPOT_BASE,
                "/sapi/v1/asset/transfer",
                {
                    "type": transfer_type,
                    "startTime": str(start_ms),
                    "endTime": str(end_ms),
                    "current": str(page),
                    "size": str(HISTORY_PAGE_SIZE),
                },
                signed=True,
            )
            if kind != "ok":
                return kind, payload
            if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
                return "schema_error", {"error": "InvalidTransferHistory"}
            rows.extend(payload["rows"])
            total = payload.get("total", len(rows))
            if not isinstance(total, int) or total <= len(rows):
                return "ok", rows
            page += 1
        return "schema_error", {"error": "TransferHistoryPageLimit"}

    def submit_transfer(self, transfer_type, asset, amount):
        if SPOT_BASE not in self.offsets:
            kind, payload = self.sync_time(SPOT_BASE, "/api/v3/time")
            if kind != "ok":
                return kind, payload
        # Deliberately one call.  Callers must reconcile; they must never retry.
        return self._once(
            "POST",
            SPOT_BASE,
            "/sapi/v1/asset/transfer",
            {"type": transfer_type, "asset": asset, "amount": amount},
            signed=True,
        )


def parse_spot(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("balances"), list):
        raise ValueError("invalid Spot balance response")
    rows = []
    for item in payload["balances"]:
        if not isinstance(item, dict) or not isinstance(item.get("asset"), str):
            raise ValueError("invalid Spot balance row")
        value = decimal(item.get("free"), "Spot free")
        rows.append({"account": "spot", "asset": item["asset"],
                     "free": decimal_text(value)})
    return rows


def parse_futures(payload):
    if not isinstance(payload, list):
        raise ValueError("invalid USD-M balance response")
    rows = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("asset"), str):
            raise ValueError("invalid USD-M balance row")
        value = decimal(item.get("availableBalance"), "USD-M available balance")
        rows.append({"account": "usd_m", "asset": item["asset"],
                     "free": decimal_text(value)})
    return rows


def selected_rows(rows, assets, include_zero):
    wanted = set(assets)
    selected = []
    for row in rows:
        if wanted and row["asset"] not in wanted:
            continue
        if not wanted and not include_zero and decimal(row["free"], "free") == 0:
            continue
        selected.append(row)
    return sorted(selected, key=lambda row: (row["account"], row["asset"]))


def account_result(kind, payload, parser):
    if kind != "ok":
        status = "BLOCKED" if (
            kind == "api_error" and payload.get("code") in (-2014, -2015)
        ) else "UNKNOWN"
        return {"status": status, **safe_api_error(kind, payload)}
    try:
        return {"status": "OK", "balances": parser(payload)}
    except ValueError as exc:
        return {"status": "UNKNOWN", "kind": "schema_error",
                "error": type(exc).__name__}


def balance_for_asset(client, account, asset):
    if account == "spot":
        kind, payload = client.spot_balances()
        result = account_result(kind, payload, parse_spot)
    else:
        kind, payload = client.futures_balances()
        result = account_result(kind, payload, parse_futures)
    if result["status"] != "OK":
        return result
    matches = [row for row in result["balances"] if row["asset"] == asset]
    if len(matches) != 1:
        return {"status": "UNKNOWN", "kind": "schema_error",
                "error": "AssetBalanceMissing"}
    return {"status": "OK", **matches[0]}


def new_request_id():
    return "l3w" + format(time.time_ns(), "x") + secrets.token_hex(4)


def history_fingerprint(row):
    return str(row.get("tranId", ""))


def matching_history(rows, receipt):
    baseline = set(receipt.get("baseline_tran_ids", []))
    matches = []
    for row in rows:
        try:
            same_amount = decimal(row.get("amount"), "history amount") == decimal(
                receipt["amount"], "receipt amount"
            )
        except ValueError:
            continue
        if row.get("type") != receipt["transfer_type"] or \
                row.get("asset") != receipt["asset"] or not same_amount:
            continue
        tran_id = history_fingerprint(row)
        if receipt.get("tran_id"):
            if tran_id == str(receipt["tran_id"]):
                matches.append(row)
        elif tran_id and tran_id not in baseline:
            matches.append(row)
    return matches


def query_receipt(client, receipt, attempts=QUERY_ATTEMPTS):
    last_kind, last_payload = "unclear", {}
    for attempt in range(attempts):
        end_ms = now_ms() + client.offsets.get(SPOT_BASE, 0)
        last_kind, last_payload = client.transfer_history(
            receipt["transfer_type"], receipt["history_start_ms"], end_ms
        )
        if last_kind == "ok":
            matches = matching_history(last_payload, receipt)
            if len(matches) == 1:
                return "found", matches[0]
            if len(matches) > 1:
                return "ambiguous", {"matches": len(matches)}
        elif last_kind == "api_error":
            return last_kind, last_payload
        if attempt + 1 < attempts:
            time.sleep(QUERY_SLEEP)
    if last_kind == "ok":
        return "not_found", {}
    return last_kind, last_payload


def finish_transfer(store, path, receipt, state, note, release=False):
    receipt["state"] = state
    receipt["note"] = note
    receipt["finished_utc"] = utc()
    store.save(path, receipt)
    if release:
        store.release_lock(receipt["request_id"])
    return path, receipt


def reconcile_transfer(store, path, receipt, client, attempts=QUERY_ATTEMPTS):
    state, payload = query_receipt(client, receipt, attempts)
    if state == "found":
        row_status = str(payload.get("status", "UNKNOWN")).upper()
        receipt["tran_id"] = str(payload.get("tranId", receipt.get("tran_id", "")))
        receipt["exchange"] = {
            "asset": payload.get("asset"),
            "amount": str(payload.get("amount", "")),
            "status": row_status,
            "timestamp": payload.get("timestamp"),
            "tran_id": receipt["tran_id"],
            "type": payload.get("type"),
        }
        if row_status in TERMINAL_TRANSFER:
            source = "spot" if receipt["direction"] == "spot-to-usdm" else "usd_m"
            destination = "usd_m" if source == "spot" else "spot"
            receipt["after"] = {
                "source": balance_for_asset(client, source, receipt["asset"]),
                "destination": balance_for_asset(client, destination, receipt["asset"]),
            }
            if any(v.get("status") != "OK" for v in receipt["after"].values()):
                first_failure(receipt, "post_transfer_balances", "read_incomplete")
            note = "exchange history is terminal; no transfer request was resent"
            return finish_transfer(
                store, path, receipt, row_status, note, release=True
            )
        first_failure(receipt, "transfer_history", "nonterminal_status")
        return finish_transfer(
            store, path, receipt, "UNKNOWN",
            "exchange transfer is nonterminal; query this receipt again; do not resend"
        )

    if state == "not_found" and receipt.get("submission_kind") == "api_error":
        return finish_transfer(
            store, path, receipt, "REJECTED",
            "Binance rejected the single POST and history contains no new match",
            release=True,
        )

    first_failure(receipt, "transfer_history", state, payload)
    notes = {
        "ambiguous": "multiple new matching history rows",
        "not_found": "no new matching history row became visible",
        "api_error": "history query was rejected",
        "unclear": "history query transport stayed unclear",
        "schema_error": "history response schema was unusable",
    }
    return finish_transfer(
        store, path, receipt, "UNKNOWN",
        notes.get(state, "transfer history is unresolved")
        + "; no transfer request was resent",
    )


def execute_transfer(store, client, direction, asset, amount, sha,
                     query_attempts=QUERY_ATTEMPTS):
    request_id = new_request_id()
    if not store.acquire_lock(request_id):
        return None, {
            "request_id": request_id,
            "state": "BLOCKED",
            "note": "another wallet action is unresolved",
            "lock_owner_last4": store.lock_owner_last4(),
        }

    receipt = {
        "kind": "internal_transfer",
        "request_id": request_id,
        "sha": sha,
        "direction": direction,
        "transfer_type": DIRECTIONS[direction],
        "asset": asset,
        "amount": amount,
        "started_utc": utc(),
        "started_ms": now_ms(),
        "state": "PREPARED",
        "mutation_attempts": 0,
        "idempotency": {
            "local_request_id": request_id,
            "exchange_client_id_supported": False,
            "rule": "single POST; reconcile history before any new action",
        },
    }
    receipt["history_start_ms"] = receipt["started_ms"] - HISTORY_LOOKBACK_MS
    try:
        path = store.create("transfer", request_id, receipt)
    except Exception:
        store.release_lock(request_id)
        raise

    source = "spot" if direction == "spot-to-usdm" else "usd_m"
    destination = "usd_m" if source == "spot" else "spot"
    before_source = balance_for_asset(client, source, asset)
    before_destination = balance_for_asset(client, destination, asset)
    receipt["before"] = {
        "source": before_source,
        "destination": before_destination,
    }
    if before_source.get("status") != "OK" or \
            before_destination.get("status") != "OK":
        first_failure(receipt, "pre_transfer_balances", "read_incomplete")
        return finish_transfer(
            store, path, receipt, "BLOCKED",
            "both source and destination balances must be factual before transfer",
            release=True,
        )
    if decimal(before_source["free"], "source free") < decimal(amount, "amount"):
        first_failure(receipt, "pre_transfer_balance", "insufficient_free")
        return finish_transfer(
            store, path, receipt, "REJECTED_BEFORE_TRANSFER",
            "source free balance is below the requested amount", release=True
        )

    end_ms = now_ms() + client.offsets.get(SPOT_BASE, 0)
    history_kind, history = client.transfer_history(
        receipt["transfer_type"], receipt["history_start_ms"], end_ms
    )
    if history_kind != "ok":
        first_failure(receipt, "baseline_history", history_kind, history)
        return finish_transfer(
            store, path, receipt, "BLOCKED",
            "baseline transfer history is unavailable; no transfer was submitted",
            release=True,
        )
    receipt["baseline_tran_ids"] = [
        history_fingerprint(row) for row in history
        if history_fingerprint(row)
    ]
    receipt["submission_started_utc"] = utc()
    receipt["mutation_attempts"] = 1
    store.save(path, receipt)

    kind, response = client.submit_transfer(
        receipt["transfer_type"], asset, amount
    )
    receipt["submission_kind"] = kind
    receipt["submission_result"] = safe_api_error(kind, response)
    if kind == "ok" and isinstance(response, dict) and response.get("tranId") is not None:
        receipt["tran_id"] = str(response["tranId"])
    elif kind == "ok":
        receipt["submission_kind"] = "schema_error"
        first_failure(receipt, "transfer_submit", "schema_error")
    else:
        first_failure(receipt, "transfer_submit", kind, response)
    store.save(path, receipt)
    return reconcile_transfer(store, path, receipt, client, query_attempts)


def print_transfer(receipt):
    row = {
        "kind": "internal_transfer",
        "request_id_last4": last4(receipt.get("request_id")),
        "state": receipt.get("state", "UNKNOWN"),
    }
    for key in ("direction", "asset", "amount"):
        if key in receipt:
            row[key] = receipt[key]
    if receipt.get("tran_id"):
        row["tran_id_last4"] = last4(receipt["tran_id"])
    if receipt.get("lock_owner_last4"):
        row["lock_owner_last4"] = receipt["lock_owner_last4"]
    emit(row)
    if "first_failure" in receipt:
        emit({"first_failure": receipt["first_failure"]})


def run_balances(args, store, client, sha):
    request_id = new_request_id()
    receipt = {
        "kind": "balance_snapshot",
        "request_id": request_id,
        "sha": sha,
        "started_utc": utc(),
    }
    path = store.create("balance", request_id, receipt)
    spot_kind, spot_payload = client.spot_balances()
    futures_kind, futures_payload = client.futures_balances()
    spot = account_result(spot_kind, spot_payload, parse_spot)
    futures = account_result(futures_kind, futures_payload, parse_futures)
    receipt["accounts"] = {"spot": spot, "usd_m": futures}
    for account, result in receipt["accounts"].items():
        if result["status"] != "OK":
            first_failure(receipt, "%s_balance" % account, result.get("kind", "unknown"), result)
            break
    receipt["finished_utc"] = utc()
    if all(result["status"] == "OK" for result in receipt["accounts"].values()):
        receipt["verdict"] = "READ_OK"
        code = 0
    elif any(result["status"] == "BLOCKED" for result in receipt["accounts"].values()):
        receipt["verdict"] = "BLOCKED"
        code = 3
    else:
        receipt["verdict"] = "UNKNOWN"
        code = 4
    store.save(path, receipt)

    for account, result in receipt["accounts"].items():
        if result["status"] == "OK":
            for row in selected_rows(result["balances"], args.asset, args.include_zero):
                emit(row)
        else:
            status = {"account": account,
                      "status": result["status"], "kind": result.get("kind")}
            if "code" in result:
                status["code"] = result["code"]
            emit(status)
    emit({"kind": "balance_snapshot", "request_id_last4": last4(request_id),
          "verdict": receipt["verdict"]})
    if "first_failure" in receipt:
        emit({"first_failure": receipt["first_failure"]})
    return code


def validate_common(sha):
    if not SHA_RE.fullmatch(sha or ""):
        raise ValueError("QUANT_SHA must identify the committed code")
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")
    if not api_key or not api_secret:
        raise ValueError("missing BINANCE_API_KEY / BINANCE_API_SECRET")
    return BinanceClient(api_key, api_secret)


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)

    balances = sub.add_parser("balances", help="read Spot and USD-M balances")
    balances.add_argument("--asset", action="append", default=[],
                          help="emit this asset even when its free balance is zero")
    balances.add_argument("--include-zero", action="store_true",
                          help="emit every zero balance returned by the exchange")

    transfer = sub.add_parser("transfer", help="perform one internal transfer")
    transfer.add_argument("direction", choices=sorted(DIRECTIONS))
    transfer.add_argument("--asset", required=True)
    transfer.add_argument("--amount", required=True)
    transfer.add_argument("--execute-internal-transfer", action="store_true",
                          help="required explicit mutation flag")

    reconcile = sub.add_parser(
        "reconcile", help="query one existing receipt; never submit a transfer"
    )
    reconcile.add_argument("request_id")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    sha = os.environ.get("QUANT_SHA", "")
    try:
        store = ReceiptStore(RECEIPT_DIR)
        client = validate_common(sha)
        if args.command == "balances":
            assets = []
            for raw in args.asset:
                asset = raw.upper()
                if not ASSET_RE.fullmatch(asset):
                    raise ValueError("asset must be 1-20 uppercase letters or digits")
                assets.append(asset)
            args.asset = assets
            return run_balances(args, store, client, sha)

        if args.command == "reconcile":
            path, receipt = store.load_transfer(args.request_id)
            if receipt.get("state") not in (
                "CONFIRMED", "FAILED", "REJECTED", "REJECTED_BEFORE_TRANSFER"
            ):
                path, receipt = reconcile_transfer(store, path, receipt, client)
            print_transfer(receipt)
            return 0 if receipt["state"] == "CONFIRMED" else \
                2 if receipt["state"] in ("FAILED", "REJECTED") else 4

        asset = args.asset.upper()
        if not ASSET_RE.fullmatch(asset):
            raise ValueError("asset must be 1-20 uppercase letters or digits")
        amount_value = decimal(args.amount, "amount")
        if amount_value <= 0:
            raise ValueError("amount must be positive")
        if not args.execute_internal_transfer:
            raise ValueError("transfer requires --execute-internal-transfer")
        path, receipt = execute_transfer(
            store, client, args.direction, asset, decimal_text(amount_value), sha
        )
        print_transfer(receipt)
        if receipt["state"] == "CONFIRMED":
            return 0
        if receipt["state"] == "BLOCKED":
            return 3
        if receipt["state"] in ("FAILED", "REJECTED", "REJECTED_BEFORE_TRANSFER"):
            return 2
        return 4
    except (ValueError, FileNotFoundError) as exc:
        emit({"verdict": "REJECTED", "error": str(exc)})
        return 2
    except Exception as exc:
        emit({"verdict": "UNKNOWN", "error": type(exc).__name__})
        return 4


if __name__ == "__main__":
    sys.exit(main())
