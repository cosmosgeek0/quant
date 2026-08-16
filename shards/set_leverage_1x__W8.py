"""Owner-triggered 1x USD-M CLI; the shared adapter owns exchange actions."""

import json, os, re, sys, uuid

from execution import binance_futures as futures
from execution import binance_http as http

SYMBOL_RE = re.compile(r"[A-Z0-9]{5,20}")
SHA_RE = re.compile(r"[0-9a-f]{40}")


def durable_intent_writer(directory: str, identity: str):
    """Return an exclusive writer that syncs the intent and its directory."""
    path = os.path.join(directory, "leverage-intent-" + identity + ".json")

    def write(intent: http.MutationIntent) -> None:
        if intent.identity != identity:
            raise ValueError("intent identity does not match its receipt")
        os.makedirs(directory, mode=0o700, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        with os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8") as handle:
            json.dump(intent.__dict__, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    return write


def run(symbol: str, transport: http.Transport, mutation_id: str, git_sha: str,
        write_intent: http.IntentWriter) -> int:
    try:
        result = futures.set_leverage_1x_once(
            transport, http.FUTURES_BASE_URL, symbol, mutation_id, git_sha, write_intent)
    except (http.AdapterRefusal, http.IntentNotDurable):
        print("LEVERAGE | %s | request refused before confirmation" % symbol)
        return 1
    if result.kind == "proven":
        detail = "changed %sx -> 1x" % result.before if result.mutated else "already 1x"
        print("LEVERAGE | %s | %s | proven" % (symbol, detail))
        return 0
    before = result.before if result.before is not None else "unproved"
    after = result.after if result.after is not None else "unproved"
    print("LEVERAGE | %s | %s | before=%s | after=%s | no retry" %
          (symbol, result.kind, before, after))
    return 1


def main() -> int:
    symbol = os.environ.get("SYMBOL", "").upper()
    sha = os.environ.get("QUANT_SHA", "").lower()
    key, secret = os.environ.get("BINANCE_API_KEY", ""), os.environ.get("BINANCE_API_SECRET", "")
    if (not SYMBOL_RE.fullmatch(symbol) or not SHA_RE.fullmatch(sha)
            or not key.strip() or not secret.strip()):
        print("LEVERAGE | SYMBOL, credentials, and full QUANT_SHA are required")
        return 2
    transport = http.BinanceHttp(key, secret)
    if transport.sync_time(http.FUTURES_BASE_URL, "/fapi/v1/time").kind != "ok":
        print("LEVERAGE | clock synchronization unproved | no mutation")
        return 1
    identity = "leverage-" + uuid.uuid4().hex
    root = os.path.expanduser(os.environ.get("RECEIPT_DIR", "~/quant_receipts"))
    writer = durable_intent_writer(os.path.join(root, "leverage-intents"), identity)
    return run(symbol, transport, identity, sha, writer)


if __name__ == "__main__":
    sys.exit(main())
