#!/usr/bin/env python3
"""Read Spot/USD-M balances and make one durable internal transfer.

Every transfer owns a local request ID and baseline before one adapter POST.
Unresolved receipts use read-only reconciliation and are never resubmitted.
Credentials and QUANT_SHA come from the environment and are never printed.
"""

import argparse, json, os, re, secrets, sys, time
from decimal import Decimal, InvalidOperation

from execution import binance_http, binance_transfer
from limits import PER_LEG_CAP_USDT as TRANSFER_HARD_CEILING


SPOT_BASE = os.environ.get("BINANCE_BASE_URL", binance_http.SPOT_BASE_URL)
FUTURES_BASE = os.environ.get("BINANCE_FUTURES_BASE_URL", binance_http.FUTURES_BASE_URL)
RECEIPT_DIR = os.path.expanduser(os.environ.get("WALLET_RECEIPT_DIR", "~/quant_receipts/wallet"))
HISTORY_LOOKBACK_MS = 60_000
TRANSFER_ASSET = "USDT"

DIRECTIONS = ("spot-to-usdm", "usdm-to-spot")
TERMINAL_TRANSFER = frozenset({"CONFIRMED", "FAILED"})
ASSET_RE = re.compile(r"^[A-Z0-9]{1,20}$")
REQUEST_ID_RE = re.compile(r"^l3w[0-9a-f]{20,40}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def utc(): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def now_ms(): return int(time.time() * 1000)


def emit(row): print(json.dumps(row, separators=(",", ":"), sort_keys=True))


def decimal(raw, label):
    if not isinstance(raw, (str, Decimal)): raise ValueError("%s must be a decimal" % label)
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("%s must be a decimal" % label)
    if not value.is_finite(): raise ValueError("%s must be finite" % label)
    return value


def decimal_text(value): return format(value, "f")


def validated_transfer_amount(asset, raw_amount):
    if asset != TRANSFER_ASSET: raise ValueError("internal transfers are restricted to USDT")
    amount = decimal(raw_amount, "amount")
    if amount <= 0: raise ValueError("amount must be positive")
    if amount > TRANSFER_HARD_CEILING:
        raise ValueError("amount exceeds the compiled %s USDT transfer ceiling" % decimal_text(TRANSFER_HARD_CEILING))
    return amount


def last4(value): return str(value or "")[-4:]


def safe_api_error(kind, error=None):
    row = {"kind": kind}
    if isinstance(error, binance_http.HttpResult):
        if type(error.code) is int: row["code"] = error.code
        if isinstance(error.msg, str): row["error"] = error.msg[:80]
    return row


def first_failure(receipt, where, kind, error=None):
    if "first_failure" not in receipt:
        receipt["first_failure"] = {"utc": utc(), "where": where,
                                    **safe_api_error(kind, error)}


class ReceiptStore:
    def __init__(self, root):
        self.root = os.path.realpath(os.path.expanduser(root))
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        self.lock_path = os.path.join(self.root, "transfer.lock")

    def path(self, kind, request_id): return os.path.join(self.root, "%s-%s.json" % (kind, request_id))

    def _sync_root(self):
        fd = os.open(self.root, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)

    def create(self, kind, request_id, payload):
        path, encoded = self.path(kind, request_id), self._encoded(payload)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded); handle.flush(); os.fsync(handle.fileno())
            self._sync_root()
        except Exception:
            try:
                os.unlink(path); self._sync_root()
            except OSError: pass
            raise
        return path

    def save(self, path, payload):
        if os.path.dirname(os.path.realpath(path)) != self.root: raise ValueError("receipt path is outside WALLET_RECEIPT_DIR")
        tmp = "%s.tmp-%s-%s" % (path, os.getpid(), secrets.token_hex(2))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(self._encoded(payload)); handle.flush(); os.fsync(handle.fileno())
            os.replace(tmp, path); self._sync_root()
        finally:
            try: os.unlink(tmp)
            except OSError: pass

    def load_transfer(self, request_id):
        if not REQUEST_ID_RE.fullmatch(request_id): raise ValueError("invalid L3 wallet request id")
        path = self.path("transfer", request_id)
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        if payload.get("request_id") != request_id: raise ValueError("receipt request id mismatch")
        return path, payload

    def acquire_lock(self, request_id):
        try:
            fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError: return False
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(request_id + "\n"); handle.flush(); os.fsync(handle.fileno())
        self._sync_root()
        return True

    def lock_owner_last4(self):
        try:
            with open(self.lock_path, encoding="ascii") as handle:
                return last4(handle.read().strip())
        except OSError: return ""

    def release_lock(self, request_id):
        try:
            with open(self.lock_path, encoding="ascii") as handle:
                owner = handle.read().strip()
        except FileNotFoundError: return
        if owner != request_id: raise RuntimeError("wallet transfer lock belongs to another request")
        os.unlink(self.lock_path); self._sync_root()

    def intent_writer(self, path, receipt):
        def write(intent):
            try:
                if not isinstance(intent, binance_http.MutationIntent): raise ValueError("intent type is invalid")
                request_id, sha = receipt.get("request_id"), receipt.get("sha")
                if intent.venue != "BINANCE_TRANSFER" or intent.operation != "TRANSFER" or intent.identity != request_id or intent.git_sha != sha: raise ValueError("intent ownership does not agree")
                if path != self.path("transfer", request_id): raise ValueError("intent receipt path does not agree")
                if receipt.get("mutation_attempts") != 0 or "intent" in receipt: raise ValueError("intent receipt is unavailable")
                if type(intent.params) is not tuple or any(
                        type(pair) is not tuple or len(pair) != 2
                        or type(pair[0]) is not str or type(pair[1]) is not str
                        for pair in intent.params): raise ValueError("intent params are not canonical")
                with open(self.lock_path, encoding="ascii") as handle:
                    if handle.read().strip() != request_id: raise ValueError("intent lock ownership does not agree")
                updated = dict(receipt)
                updated["intent"] = _intent_record(intent)
                updated["submission_started_utc"], updated["submission_started_ms"] = utc(), now_ms()
                updated["mutation_attempts"] = 1
                self.save(path, updated)
                receipt.clear(); receipt.update(updated)
            except Exception:
                raise binance_http.IntentNotDurable("intent was not durably written") from None

        return write

    @staticmethod
    def _encoded(payload): return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()


def parse_spot(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("balances"), list): raise ValueError("invalid Spot balance response")
    rows = []
    for item in payload["balances"]:
        if not isinstance(item, dict) or not isinstance(item.get("asset"), str): raise ValueError("invalid Spot balance row")
        value = decimal(item.get("free"), "Spot free")
        rows.append({"account": "spot", "asset": item["asset"], "free": decimal_text(value)})
    return rows


def parse_futures(payload):
    if not isinstance(payload, list): raise ValueError("invalid USD-M balance response")
    rows = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("asset"), str): raise ValueError("invalid USD-M balance row")
        value = decimal(item.get("availableBalance"), "USD-M available balance")
        rows.append({"account": "usd_m", "asset": item["asset"], "free": decimal_text(value)})
    return rows


def selected_rows(rows, assets, include_zero):
    wanted = set(assets)
    selected = []
    for row in rows:
        if wanted and row["asset"] not in wanted: continue
        if not wanted and not include_zero and decimal(row["free"], "free") == 0: continue
        selected.append(row)
    return sorted(selected, key=lambda row: (row["account"], row["asset"]))


def _result_status(kind, error):
    blocked = kind == "api_error" and isinstance(error, binance_http.HttpResult) and error.code in (-2014, -2015)
    return "BLOCKED" if blocked else "UNKNOWN"


def account_result(result, parser):
    if not isinstance(result, binance_http.HttpResult):
        return {"status": "UNKNOWN", "kind": "unclear", "error": "invalid_http_result"}
    if result.kind != "ok":
        return {"status": _result_status(result.kind, result), **safe_api_error(result.kind, result)}
    try: return {"status": "OK", "balances": parser(result.payload)}
    except ValueError:
        return {"status": "UNKNOWN", "kind": "schema_error", "error": "invalid_balance_schema"}


def _gated_transport(transport, sync_results):
    failures = {}
    for account, base_url in (("spot", SPOT_BASE), ("usd_m", FUTURES_BASE)):
        result = sync_results.get(account)
        if not isinstance(result, binance_http.HttpResult): result = binance_http.HttpResult("unclear", None, None, "time_sync_result_missing")
        if result.kind != "ok": failures[base_url] = result

    def call(method, base_url, path, params=None, signed=False):
        if signed and base_url in failures: return failures[base_url]
        return transport(method, base_url, path, params, signed)

    return call


def _transfer_balances(transport, asset): return binance_transfer.read_balances(transport, SPOT_BASE, FUTURES_BASE, asset)


def _projected_balances(result, direction, asset):
    source = "spot" if direction == "spot-to-usdm" else "usd_m"
    destination = "usd_m" if source == "spot" else "spot"
    if not isinstance(result, binance_http.ReadResult) or result.kind != "ok" \
            or not isinstance(result.value, binance_transfer.TransferBalances):
        kind = result.kind if isinstance(result, binance_http.ReadResult) else "unclear"
        error = result.error if isinstance(result, binance_http.ReadResult) else None
        status = _result_status(kind, error)
        failure = {"status": status, **safe_api_error(kind, error)}
        return {"source": {"account": source, **failure},
                "destination": {"account": destination, **failure}}, None
    values = {"spot": result.value.spot, "usd_m": result.value.usdm}
    rows = {}
    for label, account in (("source", source), ("destination", destination)):
        rows[label] = {"status": "OK", "account": account, "asset": asset, "free": decimal_text(values[account])}
    return rows, result.value


def new_request_id(): return "l3w" + format(time.time_ns(), "x") + secrets.token_hex(4)


def _intent_record(intent):
    return {"venue": intent.venue, "operation": intent.operation,
            "identity": intent.identity, "git_sha": intent.git_sha,
            "params": [list(pair) for pair in intent.params]}


def _receipt_intent(receipt):
    direction = receipt["direction"]
    if not isinstance(direction, str): raise binance_http.AdapterRefusal("receipt direction is invalid")
    intent = binance_transfer.build_transfer(
        direction.replace("-", "_"), receipt["asset"], decimal(receipt["amount"], "receipt amount"), receipt["request_id"], receipt["sha"],
    )
    if receipt.get("intent") != _intent_record(intent) or receipt.get("transfer_type") != dict(intent.params).get("type"): raise binance_http.AdapterRefusal("receipt intent is not exact")
    return intent


def _receipt_query_identity(receipt):
    baseline = receipt.get("baseline_tran_ids")
    if type(baseline) is not list or any(type(value) is not int or value <= 0 for value in baseline) or len(set(baseline)) != len(baseline): raise binance_http.AdapterRefusal("receipt baseline is not exact")
    raw = receipt.get("tran_id")
    if raw in (None, ""): tran_id = None
    elif type(raw) is str and re.fullmatch(r"[1-9][0-9]*", raw): tran_id = int(raw)
    else: raise binance_http.AdapterRefusal("receipt transfer id is not exact")
    start_ms = receipt.get("history_start_ms")
    if type(start_ms) is not int or start_ms < 0: raise binance_http.AdapterRefusal("receipt history window is not exact")
    return frozenset(baseline), tran_id, start_ms


def finish_transfer(store, path, receipt, state, note, release=False):
    receipt.update({"state": state, "note": note, "finished_utc": utc()}); store.save(path, receipt)
    if release: store.release_lock(receipt["request_id"])
    return path, receipt


def reconcile_transfer(store, path, receipt, transport, *, sleep=time.sleep):
    # A durable mutation_attempts=0 receipt was saved before any POST.  It is
    # therefore known-unsent and must never adopt an unrelated history row.
    if receipt.get("mutation_attempts") != 1:
        return path, receipt
    try:
        intent = _receipt_intent(receipt)
        baseline, tran_id, start_ms = _receipt_query_identity(receipt)
        query = binance_transfer.query_transfer(
            transport, SPOT_BASE, intent, baseline, tran_id,
            start_ms, now_ms(), sleep=sleep,
        )
    except (KeyError, TypeError, ValueError, binance_http.AdapterRefusal):
        first_failure(receipt, "transfer_receipt", "unclear")
        return finish_transfer(
            store, path, receipt, "UNKNOWN",
            "receipt identity is incomplete; no transfer request was resent",
        )

    if query.kind == "found" and isinstance(
            query.row, binance_transfer.TransferRow):
        row, row_status = query.row, query.row.status.upper()
        receipt["tran_id"] = str(row.tran_id)
        receipt["exchange"] = {
            "asset": row.asset,
            "amount": decimal_text(row.amount),
            "status": row_status,
            "timestamp": row.time_ms,
            "tran_id": receipt["tran_id"],
            "type": row.transfer_type,
        }
        if row_status in TERMINAL_TRANSFER:
            balances = _transfer_balances(transport, receipt["asset"])
            receipt["after"], _ = _projected_balances(
                balances, receipt["direction"], receipt["asset"]
            )
            if any(v.get("status") != "OK" for v in receipt["after"].values()):
                first_failure(
                    receipt, "post_transfer_balances", balances.kind,
                    balances.error,
                )
            note = "exchange history is terminal; no transfer request was resent"
            return finish_transfer(
                store, path, receipt, row_status, note, release=True
            )
        first_failure(receipt, "transfer_history", "nonterminal_status")
        return finish_transfer(
            store, path, receipt, "UNKNOWN",
            "exchange transfer is nonterminal; query this receipt again; do not resend"
        )

    if query.kind == "not_found" \
            and receipt.get("submission_kind") == "api_error":
        return finish_transfer(
            store, path, receipt, "REJECTED",
            "Binance rejected the single POST and history contains no new match",
            release=True,
        )

    first_failure(receipt, "transfer_history", query.kind, query.error)
    notes = {
        "ambiguous": "multiple new matching history rows",
        "not_found": "no new matching history row became visible",
        "error": "history query was rejected",
        "pending": "history query remains pending",
    }
    return finish_transfer(
        store, path, receipt, "UNKNOWN",
        notes.get(query.kind, "transfer history is unresolved")
        + "; no transfer request was resent",
    )


def execute_transfer(store, transport, direction, asset, amount, sha, *,
                     sleep=time.sleep):
    amount_value = validated_transfer_amount(asset, amount)
    request_id = new_request_id()
    intent = binance_transfer.build_transfer(
        direction.replace("-", "_") if isinstance(direction, str) else direction, asset, amount_value, request_id, sha
    )
    amount = decimal_text(amount_value)
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
        "transfer_type": dict(intent.params)["type"],
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

    balance_result = _transfer_balances(transport, asset)
    receipt["before"], balances = _projected_balances(
        balance_result, direction, asset
    )
    if balances is None:
        first_failure(
            receipt, "pre_transfer_balances", balance_result.kind,
            balance_result.error,
        )
        return finish_transfer(
            store, path, receipt, "BLOCKED",
            "both source and destination balances must be factual before transfer",
            release=True,
        )
    source_free = balances.spot if direction == "spot-to-usdm" else balances.usdm
    if source_free < amount_value:
        first_failure(receipt, "pre_transfer_balance", "insufficient_free")
        return finish_transfer(
            store, path, receipt, "REJECTED_BEFORE_TRANSFER",
            "source free balance is below the requested amount", release=True
        )

    history = binance_transfer.read_transfer_history(
        transport, SPOT_BASE, receipt["transfer_type"],
        receipt["history_start_ms"], now_ms(), sleep=sleep,
    )
    if history.kind != "ok" or not isinstance(history.value, tuple):
        first_failure(receipt, "baseline_history", history.kind, history.error)
        return finish_transfer(
            store, path, receipt, "BLOCKED",
            "baseline transfer history is unavailable; no transfer was submitted",
            release=True,
        )
    receipt["baseline_tran_ids"] = [row.tran_id for row in history.value]
    try:
        submission = binance_transfer.submit_transfer_once(
            transport, SPOT_BASE, intent, store.intent_writer(path, receipt)
        )
    except binance_http.IntentNotDurable:
        first_failure(receipt, "transfer_intent", "unclear")
        return finish_transfer(
            store, path, receipt, "BLOCKED",
            "transfer intent was not durably stored; no request was submitted",
            release=True,
        )
    receipt["submission_kind"] = submission.kind
    receipt["submission_result"] = safe_api_error(
        submission.kind, submission.error
    )
    if submission.kind == "accepted" and type(submission.tran_id) is int:
        receipt["tran_id"] = str(submission.tran_id)
    else:
        first_failure(
            receipt, "transfer_submit", submission.kind, submission.error
        )
    store.save(path, receipt)
    return reconcile_transfer(
        store, path, receipt, transport, sleep=sleep,
    )


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


def run_balances(args, store, transport, sha):
    request_id = new_request_id()
    receipt = {
        "kind": "balance_snapshot",
        "request_id": request_id,
        "sha": sha,
        "started_utc": utc(),
    }
    path = store.create("balance", request_id, receipt)
    raw = {
        "spot": binance_http.read(
            transport, SPOT_BASE, "/api/v3/account", signed=True),
        "usd_m": binance_http.read(
            transport, FUTURES_BASE, "/fapi/v3/balance", signed=True),
    }
    spot = account_result(raw["spot"], parse_spot)
    futures = account_result(raw["usd_m"], parse_futures)
    receipt["accounts"] = {"spot": spot, "usd_m": futures}
    for account, result in receipt["accounts"].items():
        if result["status"] != "OK":
            first_failure(
                receipt, "%s_balance" % account,
                result.get("kind", raw[account].kind), raw[account],
            )
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
    return binance_http.BinanceHttp(api_key, api_secret)


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
        transport = validate_common(sha)
        sync_results = {
            "spot": transport.sync_time(SPOT_BASE, "/api/v3/time"),
            "usd_m": transport.sync_time(FUTURES_BASE, "/fapi/v1/time"),
        }
        transport = _gated_transport(transport, sync_results)
        if args.command == "balances":
            assets = []
            for raw in args.asset:
                asset = raw.upper()
                if not ASSET_RE.fullmatch(asset):
                    raise ValueError("asset must be 1-20 uppercase letters or digits")
                assets.append(asset)
            args.asset = assets
            return run_balances(args, store, transport, sha)

        if args.command == "reconcile":
            path, receipt = store.load_transfer(args.request_id)
            if receipt.get("state") not in (
                "CONFIRMED", "FAILED", "REJECTED", "REJECTED_BEFORE_TRANSFER",
                "BLOCKED",
            ):
                path, receipt = reconcile_transfer(
                    store, path, receipt, transport,
                )
            print_transfer(receipt)
            if receipt["state"] == "CONFIRMED":
                return 0
            if receipt["state"] == "BLOCKED":
                return 3
            return 2 if receipt["state"] in ("FAILED", "REJECTED") else 4

        asset = args.asset.upper()
        if not ASSET_RE.fullmatch(asset):
            raise ValueError("asset must be 1-20 uppercase letters or digits")
        if not args.execute_internal_transfer:
            raise ValueError("transfer requires --execute-internal-transfer")
        path, receipt = execute_transfer(
            store, transport, args.direction, asset, args.amount, sha,
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
