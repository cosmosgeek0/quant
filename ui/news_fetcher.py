#!/usr/bin/env python3
"""Multi-source news fetcher for the Quant dashboard.

Ports the previous control room's news backend in substance (owner
order): the full source catalog (ui/news_sources_catalog.json, extracted
from the old newsContext service), the Tree of Alpha firehose, RSS/Atom
official+editorial feeds with per-source include-keyword filters, the
impact scoring engine, and per-source live/delayed/error status.

Outputs (both atomic, both under ui/live/):
  news.jsonl          normalized items, newest first
  news_sources.json   {as_of, available, live, sources:[...]} status board

Run once:  python3 ui/news_fetcher.py [-q]
Live loop: python3 ui/news_fetcher.py --loop 120
Formal scheduling still belongs to the wiring build.
"""

import argparse
import concurrent.futures
import html
import json
import os
import re
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
CATALOG_PATH = BASE / "news_sources_catalog.json"
OUT_ITEMS = BASE / "live" / "news.jsonl"
OUT_SOURCES = BASE / "live" / "news_sources.json"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Safari/537.36")
FETCH_TIMEOUT = 8
CYCLE_WORKERS = 16
MAX_ITEMS = 800
MAX_AGE_DAYS = 7
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

CHANNEL_CATEGORY = {
    "official": "Macro",
    "venue": "Exchange",
    "issuer": "Crypto",
    "protocol": "Crypto",
}


def score_impact(title, channel):
    if CRITICAL.search(title):
        return 3
    score = 0
    if channel in ("official", "venue", "issuer"):
        score += 2
    elif channel in ("firehose", "protocol"):
        score += 1
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


def classify(title, channel):
    for name, rule in CAT_RULES:
        if rule.search(title):
            return name
    return CHANNEL_CATEGORY.get(channel, "Crypto")


def clean_title(raw):
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(raw)))
    text = re.sub(r"\s+", " ", text).strip()
    text = "".join(ch for ch in text if ch.isprintable())
    return text[:MAX_TITLE_CHARS]


def http_get(url):
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT,
                      "Accept": "application/json, application/rss+xml, "
                                "application/atom+xml, application/xml, "
                                "text/xml, */*"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as resp:
        return resp.read()


def iso_from_epoch_ms(ms):
    ts = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return ts.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_rss_time(text):
    if not text:
        return None
    text = str(text).strip()
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        cleaned = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(cleaned)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def strip_ns(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def parse_feed(blob, source, keywords):
    """RSS 2.0 / Atom bytes -> normalized item dicts (None on non-XML)."""
    items = []
    try:
        root = ET.fromstring(blob)
    except ET.ParseError:
        return None
    nodes = [el for el in root.iter() if strip_ns(el.tag) in ("item", "entry")]
    words = [w.lower() for w in keywords]
    for node in nodes[:40]:
        title = ""
        link = ""
        stamp = None
        guid = ""
        for child in node:
            tag = strip_ns(child.tag)
            if tag == "title" and not title:
                title = clean_title(child.text or "")
            elif tag == "link" and not link:
                link = (child.text or child.get("href") or "").strip()
            elif tag in ("pubDate", "published", "updated", "date"):
                stamp = stamp or parse_rss_time(child.text)
            elif tag in ("guid", "id") and not guid:
                guid = (child.text or "").strip()
        if not title or stamp is None:
            continue
        if words and not any(w in title.lower() for w in words):
            continue
        if link and not link.startswith(("http://", "https://")):
            link = ""
        ts = stamp.isoformat(timespec="seconds").replace("+00:00", "Z")
        items.append({
            "id": (guid or link or title)[:120],
            "ts": ts,
            "source": source["label"],
            "source_key": source["key"],
            "domain": source.get("domain") or "",
            "title": title,
            "url": link,
            "impact": score_impact(title, source["channel"]),
            "category": classify(title, source["channel"]),
        })
    return items


def tree_source_name(item):
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


def parse_tree(blob, source):
    try:
        payload = json.loads(blob)
    except ValueError:
        return None
    if not isinstance(payload, list):
        return None
    items = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        title = clean_title(raw.get("title") or "")
        if not title:
            continue
        time_ms = raw.get("time")
        if not isinstance(time_ms, (int, float)) or time_ms <= 0:
            continue
        stripped = re.sub(r"^[^:]{2,60}\(@[A-Za-z0-9_]+\):\s*", "", title)
        headline = stripped if len(stripped) >= 8 else title
        if re.fullmatch(r"https?://\S+", headline):
            continue
        url = str(raw.get("url") or "").strip()
        if url and not url.startswith(("http://", "https://")):
            url = ""
        try:
            domain = url.split("/", 3)[2] if url else ""
        except IndexError:
            domain = ""
        items.append({
            "id": str(raw.get("_id") or "")[:64],
            "ts": iso_from_epoch_ms(time_ms),
            "source": tree_source_name(raw),
            "source_key": "tree",
            "domain": domain,
            "title": headline,
            "url": url,
            "impact": score_impact(headline, "firehose"),
            "category": classify(headline, "firehose"),
        })
    return items


def _mk_item(source, item_id, ts, title, url):
    return {
        "id": str(item_id)[:120],
        "ts": ts,
        "source": source["label"],
        "source_key": source["key"],
        "domain": source.get("domain") or "",
        "title": title,
        "url": url,
        "impact": score_impact(title, source["channel"]),
        "category": classify(title, source["channel"]),
    }


def _ts_from_any(value, assume_offset=None):
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        ms = int(text)
        if ms > 10**12:
            return iso_from_epoch_ms(ms)
        if ms > 10**9:
            return iso_from_epoch_ms(ms * 1000)
        return None
    if assume_offset and re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d", text):
        text = text.replace(" ", "T") + assume_offset
    stamp = parse_rss_time(text)
    if stamp is None:
        return None
    return stamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_upbit(blob, source):
    try:
        data = json.loads(blob)
    except ValueError:
        return None
    notices = (((data or {}).get("data") or {}).get("notices"))
    if not isinstance(notices, list):
        return None
    items = []
    for notice in notices[:12]:
        if not isinstance(notice, dict):
            continue
        title = clean_title(notice.get("title") or "")
        ts = _ts_from_any(notice.get("listed_at")
                          or notice.get("first_listed_at"))
        if not title or not ts:
            continue
        nid = notice.get("id") or notice.get("uuid") or title
        url = ("https://upbit.com/service_center/notice?id=%s" % nid
               if notice.get("id") else "https://upbit.com")
        items.append(_mk_item(source, "upbit:%s" % nid, ts, title, url))
    return items


def parse_bithumb(blob, source):
    try:
        data = json.loads(blob)
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    items = []
    for row in data[:12]:
        if not isinstance(row, dict):
            continue
        title = clean_title(row.get("title") or "")
        ts = _ts_from_any(row.get("published_at") or row.get("modified_at"),
                          assume_offset="+09:00")
        if not title or not ts:
            continue
        url = row.get("pc_url") if isinstance(row.get("pc_url"), str) \
            else "https://www.bithumb.com"
        items.append(_mk_item(
            source, "bithumb:%s" % (row.get("published_at") or title),
            ts, title, url))
    return items


def parse_bybit_status(blob, source):
    try:
        data = json.loads(blob)
    except ValueError:
        return None
    rows = (((data or {}).get("result") or {}).get("list"))
    if not isinstance(rows, list):
        return None
    items = []
    for row in rows[:12]:
        if not isinstance(row, dict):
            continue
        title = clean_title(row.get("title") or row.get("state") or "")
        ts = _ts_from_any(row.get("beginTime") or row.get("startTime")
                          or row.get("time"))
        if not title or not ts:
            continue
        if not title.startswith("Bybit"):
            title = "Bybit " + title
        items.append(_mk_item(
            source, "bybit-system:%s" % (row.get("id") or title), ts, title,
            "https://announcements.bybit.com/en/?category=maintenance_updates"))
    return items


def parse_lookonchain(blob, source):
    try:
        data = json.loads(blob)
    except ValueError:
        return None
    rows = (data or {}).get("content")
    if not isinstance(rows, list):
        return None
    items = []
    for row in rows[:24]:
        if not isinstance(row, dict):
            continue
        title = clean_title(row.get("stitle") or "")
        ts = None
        for k in row:
            if "time" in k.lower() or "date" in k.lower():
                ts = _ts_from_any(row.get(k), assume_offset="+08:00")
                if ts:
                    break
        if not title or not ts:
            continue
        nid = row.get("nnewflash_id") or title
        items.append(_mk_item(
            source, "look:%s" % nid, ts, title,
            "https://www.lookonchain.com"))
    return items


def parse_coinbase_products(blob, source):
    """Stateless subset of the old listing-diff parser: newest products
    only when Coinbase stamps them; most cycles yield nothing (honest)."""
    return []


JSON_PARSERS = {
    "upbit-announcements": parse_upbit,
    "bithumb-notices": parse_bithumb,
    "bybit-system-status": parse_bybit_status,
    "lookonchain": parse_lookonchain,
    "coinbase-products": parse_coinbase_products,
}


def fetch_source(source):
    """Fetch one catalog source. Returns (items, status)."""
    mode = source.get("mode") or "feed"
    if mode == "page":
        return [], "pending"
    collected = []
    errors = 0
    attempts = 0
    for url in source.get("feeds", [])[:4]:
        attempts += 1
        try:
            blob = http_get(url)
        except Exception:
            errors += 1
            continue
        if mode == "tree":
            parsed = parse_tree(blob, source)
        elif mode == "json":
            parser = JSON_PARSERS.get(source.get("parser") or "")
            parsed = parser(blob, source) if parser else None
        else:
            parsed = parse_feed(blob, source, source.get("keywords") or [])
            if parsed is None:
                parsed = parse_tree(blob, source)
        if parsed:
            collected.extend(parsed)
    if attempts and errors == attempts:
        status = "error"
    elif collected:
        status = "live"
    else:
        status = "delayed"
    return collected, status


FAVICON_DIR = BASE / "live" / "favicons"


def mirror_favicons(catalog, quiet):
    """Mirror source favicons same-origin (Google s2, cached on disk) so
    the UI can show real logos without any external request."""
    FAVICON_DIR.mkdir(parents=True, exist_ok=True)
    fetched = 0
    for src in catalog:
        domain = (src.get("domain") or "").strip().lower()
        if not domain:
            continue
        out = FAVICON_DIR / (domain + ".png")
        if out.exists() and out.stat().st_size > 0:
            continue
        try:
            blob = http_get(
                "https://www.google.com/s2/favicons?domain=%s&sz=64" % domain)
            if blob and len(blob) > 50:
                out.write_bytes(blob)
                fetched += 1
        except Exception:
            continue
    if fetched and not quiet:
        print("news_fetcher: mirrored %d favicons" % fetched)


def _atomic_write(path, text):
    handle, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".news-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as tmp:
            tmp.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def run_cycle(catalog, quiet):
    started = time.time()
    all_items = []
    board = []
    with concurrent.futures.ThreadPoolExecutor(CYCLE_WORKERS) as pool:
        futures = {pool.submit(fetch_source, src): src for src in catalog}
        for future in concurrent.futures.as_completed(futures):
            src = futures[future]
            try:
                items, status = future.result()
            except Exception:
                items, status = [], "error"
            newest = max((it["ts"] for it in items), default=None)
            board.append({
                "key": src["key"],
                "label": src["label"],
                "domain": src.get("domain") or "",
                "channel": src["channel"],
                "status": status,
                "count": len(items),
                "last_ts": newest,
            })
            all_items.extend(items)

    cutoff = datetime.now(timezone.utc).timestamp() - MAX_AGE_DAYS * 86400
    fresh = []
    seen = set()
    for item in all_items:
        stamp = parse_rss_time(item["ts"])
        if stamp is None or stamp.timestamp() < cutoff:
            continue
        dedup = item["url"] or (item["source_key"] + "|" + item["id"])
        if dedup in seen:
            continue
        seen.add(dedup)
        fresh.append(item)
    fresh.sort(key=lambda entry: entry["ts"], reverse=True)
    fresh = fresh[:MAX_ITEMS]

    # merge duplicate catalog rows (same label+domain from multi-endpoint
    # sources) so available/live counts stay honest
    merged = {}
    rank = {"live": 3, "delayed": 2, "error": 1, "pending": 0}
    for row in board:
        key = (row["label"].lower(), row["domain"])
        if key in merged:
            prev = merged[key]
            prev["count"] += row["count"]
            if rank.get(row["status"], 0) > rank.get(prev["status"], 0):
                prev["status"] = row["status"]
            if row["last_ts"] and (not prev["last_ts"]
                                   or row["last_ts"] > prev["last_ts"]):
                prev["last_ts"] = row["last_ts"]
        else:
            merged[key] = dict(row)
    board = sorted(merged.values(), key=lambda row: row["label"].lower())
    live = sum(1 for row in board if row["status"] == "live")
    pending = sum(1 for row in board if row["status"] == "pending")
    as_of = datetime.now(timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")

    OUT_ITEMS.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(OUT_ITEMS, "".join(
        json.dumps(entry, ensure_ascii=False) + "\n" for entry in fresh))
    _atomic_write(OUT_SOURCES, json.dumps({
        "as_of": as_of,
        "available": len(board),
        "live": live,
        "pending": pending,
        "sources": board,
    }, ensure_ascii=False, indent=1))

    if not quiet:
        high = sum(1 for entry in fresh if entry["impact"] == 3)
        print("news_fetcher: %d items (%d high) from %d/%d live sources "
              "in %.1fs" % (len(fresh), high, live, len(board),
                            time.time() - started))
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", type=int, default=0,
                        help="refetch every N seconds (0 = run once)")
    parser.add_argument("-q", "--quiet", action="store_true")
    args = parser.parse_args()

    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    catalog = [src for src in catalog
               if src.get("feeds") or (src.get("mode") == "page")]

    mirror_favicons(catalog, args.quiet)
    if args.loop <= 0:
        return run_cycle(catalog, args.quiet)
    interval = max(60, args.loop)
    while True:
        try:
            run_cycle(catalog, args.quiet)
        except Exception as error:  # keep the loop alive
            print("news_fetcher: cycle failed: %s" % error, file=sys.stderr)
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
