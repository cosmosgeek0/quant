#!/usr/bin/env python3
"""News fetcher for the Quant dashboard.

Pulls the Tree of Alpha public news API (the same firehose the owner's
previous control room used) and writes a normalized, sanitized JSONL
snapshot to ui/live/news.jsonl for serve.py's /api/news endpoint.

Impact scoring and category classification are ported in essence from
the previous project's newsContext service (owner-permitted item),
rewritten stdlib-only. No keys, one public GET, atomic write.

Run:  python3 ui/news_fetcher.py [--limit 200] [-q]
Scheduling is a separate, later wiring step — running this by hand is
the honest state until then.
"""

import argparse
import json
import os
import re
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TREE_URL = "https://news.treeofalpha.com/api/news?limit={limit}"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Safari/537.36")
OUT_PATH = Path(__file__).resolve().parent / "live" / "news.jsonl"
TIMEOUT_SECONDS = 15
MAX_TITLE_CHARS = 300

CRITICAL = re.compile(
    r"\b(hack(?:ed)?|exploit(?:ed)?|breach(?:ed)?|stolen|drain(?:ed)?|depeg|"
    r"bankruptcy|insolven(?:t|cy)|trading suspension|withdrawal halt|"
    r"bridge pause|chain halt|service outage|emergency pause)\b", re.I)
DECISIVE_MACRO = re.compile(
    r"\b(fomc|interest rate decision|rate decision|cpi|consumer price index|"
    r"pce|ppi|payroll|employment situation|unemployment report|gdp|"
    r"monetary policy (?:decision|statement|report)|central bank decision)\b",
    re.I)
REGULATORY_VERB = re.compile(
    r"\b(approve[sd]?|reject[sd]?|pass(?:es|ed)?|sign(?:s|ed)?|ban(?:s|ned)?|"
    r"charges?|sues?|lawsuit|court ruling|final rule|enforcement action|"
    r"license[sd]?|settlement|sanction[sd]?)\b", re.I)
REGULATORY_NOUN = re.compile(
    r"\b(sec|cftc|fca|esma|ofac|regulator|government|court|state|parliament|"
    r"senate|congress|occ|crypto|bitcoin|etf|stablecoin|token)\b", re.I)
VENUE_STRUCTURE = re.compile(
    r"\b(listing|delisting|trading support|perpetual contract|margin pair|"
    r"leverage (?:tier|limit)|network suspension|deposit suspension|"
    r"withdrawal suspension|maintenance|tick size|"
    r"removal of .*trading pairs?)\b", re.I)
ISSUER_STRUCTURE = re.compile(
    r"\b(stablecoin|reserve report|redemption|attestation|etf approval|"
    r"fund launch|custody approval)\b", re.I)
MARKET_SHOCK = re.compile(
    r"\b(surge[sd]?|jump[sd]?|plunge[sd]?|tumble[sd]?|crash(?:es|ed)?|"
    r"liquidations?|record inflow|record outflow)\b", re.I)
HAS_NUMBERS = re.compile(r"\b\d+(?:\.\d+)?%\b|\$\s?\d", re.I)
ONCHAIN = re.compile(
    r"\b(whale|wallet|transfer(?:red)?|inflow|outflow|burn(?:ed)?|"
    r"liquidat(?:ed|ion))\b", re.I)
BIG_MONEY = re.compile(
    r"\$\s?\d|\b\d+(?:\.\d+)?\s?(?:million|billion|m|bn)\b", re.I)
CORPORATE = re.compile(
    r"\b(chief executive|ceo|cfo|chief legal officer|steps? down|resigns?|"
    r"acquires?|acquisition|merger|funding round|raises? \$|"
    r"files? for (?:ipo|bankruptcy))\b", re.I)
GEOPOLITICAL = re.compile(
    r"\b(war|missile|airstrike|strikes? against|invasion|ceasefire|"
    r"sanctions?)\b", re.I)
MAJOR_ENTITY = re.compile(
    r"\b(federal reserve|fed|sec|cftc|ecb|boj|pboc|tether|circle|blackrock|"
    r"grayscale)\b", re.I)
LARGE_SCALE = re.compile(
    r"\$\s?\d+(?:\.\d+)?\s?(?:billion|bn)\b|"
    r"\$\s?(?:[1-9]\d{2,})(?:\.\d+)?\s?(?:million|m)\b", re.I)
LOW_SIGNAL = re.compile(
    r"\b(opinion|explainer|what happened|price prediction|could|may|might|"
    r"why |here'?s what|roundup|sponsored|hiring|job opening)\b", re.I)

CAT_RULES = (
    ("Security", CRITICAL),
    ("Macro", DECISIVE_MACRO),
    ("Regulation", re.compile(
        r"\b(sec|cftc|regulator|lawsuit|court|enforcement|sanction|"
        r"congress|senate|law|bill|ruling)\b", re.I)),
    ("Exchange", re.compile(
        r"\b(binance|coinbase|kraken|bybit|okx|upbit|bithumb|listing|"
        r"delisting|exchange)\b", re.I)),
    ("On-Chain", ONCHAIN),
    ("Macro", re.compile(
        r"\b(fed|treasury|inflation|tariff|oil|dollar|yield|bond)\b", re.I)),
    ("Markets", re.compile(
        r"\b(stocks?|equit|nasdaq|s&p|dow|shares?|ipo|etf)\b", re.I)),
)


def score_impact(title):
    if CRITICAL.search(title):
        return 3
    score = 0
    if DECISIVE_MACRO.search(title):
        score += 3
    if REGULATORY_VERB.search(title) and REGULATORY_NOUN.search(title):
        score += 3
    if VENUE_STRUCTURE.search(title):
        score += 3
    if ISSUER_STRUCTURE.search(title):
        score += 2
    if MARKET_SHOCK.search(title) and HAS_NUMBERS.search(title):
        score += 2
    if ONCHAIN.search(title) and BIG_MONEY.search(title):
        score += 1
    if CORPORATE.search(title):
        score += 1
    if GEOPOLITICAL.search(title):
        score += 2
    if MAJOR_ENTITY.search(title):
        score += 1
    if LARGE_SCALE.search(title):
        score += 1
    if LOW_SIGNAL.search(title):
        score -= 1
    if score >= 4:
        return 3
    if score >= 2:
        return 2
    return 1


def classify(title):
    for name, rule in CAT_RULES:
        if rule.search(title):
            return name
    return "Crypto"


def clean_title(raw):
    text = re.sub(r"\s+", " ", str(raw)).strip()
    text = "".join(ch for ch in text if ch.isprintable())
    return text[:MAX_TITLE_CHARS]


def source_name(item):
    info = item.get("info") or {}
    src = str(item.get("source") or "").strip()
    title = str(item.get("title") or "")
    handle = re.match(r"^([A-Za-z0-9 ._&'-]{2,40})\s*\(@", title)
    if handle:
        return handle.group(1).strip()
    if src.lower() == "twitter" and isinstance(info, dict):
        quoted = info.get("quotedUser") or {}
        if isinstance(quoted, dict) and quoted.get("name"):
            return str(quoted["name"])[:40]
    return src[:40] or "Tree"


def normalize(item):
    title = clean_title(item.get("title") or "")
    if not title:
        return None
    url = str(item.get("url") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = ""
    time_ms = item.get("time")
    if not isinstance(time_ms, (int, float)) or time_ms <= 0:
        return None
    ts = datetime.fromtimestamp(time_ms / 1000.0, tz=timezone.utc)
    strip_prefix = re.sub(r"^[^:]{2,60}\(@[A-Za-z0-9_]+\):\s*", "", title)
    headline = strip_prefix if len(strip_prefix) >= 8 else title
    if re.fullmatch(r"https?://\S+", headline):
        return None
    return {
        "id": str(item.get("_id") or "")[:64],
        "ts": ts.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source": source_name(item),
        "title": headline,
        "url": url,
        "impact": score_impact(headline),
        "category": classify(headline),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("-q", "--quiet", action="store_true")
    args = parser.parse_args()

    request = urllib.request.Request(
        TREE_URL.format(limit=max(1, min(args.limit, 500))),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as resp:
        payload = json.load(resp)
    if not isinstance(payload, list):
        print("news_fetcher: unexpected payload shape", file=sys.stderr)
        return 2

    items = []
    seen = set()
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        normalized = normalize(raw)
        if normalized is None or normalized["id"] in seen:
            continue
        seen.add(normalized["id"])
        items.append(normalized)
    items.sort(key=lambda entry: entry["ts"], reverse=True)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(
        dir=str(OUT_PATH.parent), prefix=".news-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as tmp:
            for entry in items:
                tmp.write(json.dumps(entry, ensure_ascii=False) + "\n")
        os.replace(tmp_name, OUT_PATH)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    if not args.quiet:
        high = sum(1 for entry in items if entry["impact"] == 3)
        medium = sum(1 for entry in items if entry["impact"] == 2)
        print("news_fetcher: wrote %d items (%d high, %d medium) -> %s"
              % (len(items), high, medium, OUT_PATH))
    return 0


if __name__ == "__main__":
    sys.exit(main())
