#!/usr/bin/env python3
"""
Capital Flow Radar
- SEC filings + Google News RSS
- 24-hour event window
- distinguishes NEW MONEY from existing-project/background/risk/commentary
- clusters duplicate/syndicated news
- extracts public-company beneficiaries/exposure
- repricing check
- Telegram alerts
- persistent state
"""

import os
import re
import json
import time
import gzip
import html
import hashlib
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher

SEC_UA = "Capital Flow Radar veronicajyde@gmail.com"
LOOKBACK_HOURS = 24
NEWS_LIMIT = 30
SEC_LIMIT_PER_FEED = 20
STATE_FILE = "radar_state.json"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SEC_FEEDS = {
    "8-K": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&owner=exclude&count=100&output=atom",
    "6-K": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=6-K&owner=exclude&count=100&output=atom",
    "S-1": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=S-1&owner=exclude&count=100&output=atom",
    "S-3": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=S-3&owner=exclude&count=100&output=atom",
    "424B4": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=424B4&owner=exclude&count=100&output=atom",
    "424B5": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=424B5&owner=exclude&count=100&output=atom",
}

# High-confidence mappings for relationships that are often described without tickers.

SEC_COMPANY_CACHE = {}

def sec_cik_from_url(url):
    m = re.search(r"/data/(\d+)/", url or "")
    return m.group(1).lstrip("0") if m else ""

def sec_company_metadata(cik):
    cik = str(cik).zfill(10)
    if cik in SEC_COMPANY_CACHE:
        return SEC_COMPANY_CACHE[cik]
    try:
        raw = http_get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers={"User-Agent": SEC_UA, "Accept-Encoding": "gzip"},
            timeout=20,
            retries=2,
        )
        data = json.loads(raw.decode("utf-8"))
        name = data.get("name", "")
        tickers = data.get("tickers", []) or []
        exchanges = data.get("exchanges", []) or []
        result = {
            "name": name,
            "tickers": [str(x).upper() for x in tickers if x],
            "exchanges": exchanges,
        }
        SEC_COMPANY_CACHE[cik] = result
        return result
    except Exception as e:
        print("SEC company metadata error", cik, e)
        return {"name": "", "tickers": [], "exchanges": []}

def sec_index_documents(index_html, base_url):
    docs = []
    # SEC index pages expose document links and descriptions. Prefer HTML/text exhibits.
    pattern = re.compile(
        r'href=["\']([^"\']+)["\'][^>]*>([^<]{1,180})</a>',
        re.I
    )
    for href, label in pattern.findall(index_html):
        href = html.unescape(href)
        label = clean_text(label)
        if not href or href.startswith("#"):
            continue
        full = urllib.parse.urljoin(base_url, href)
        if re.search(r'\.(?:htm|html|txt)$', full, re.I):
            docs.append((full, label))
    # De-duplicate while preserving order.
    out, seen_docs = [], set()
    for x in docs:
        if x[0] not in seen_docs:
            out.append(x)
            seen_docs.add(x[0])
    return out

def sec_accepted_datetime(index_html):
    m = re.search(
        r'(?:Accepted|Acceptance Date(?:\s*/\s*Time)?|ACCEPTANCE-DATETIME)'
        r'.{0,500}?(\d{4})[-/]?(\d{2})[-/]?(\d{2}).{0,120}?(\d{2}):?(\d{2}):?(\d{2})',
        index_html,
        re.I | re.S
    )
    if m:
        try:
            return datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4)), int(m.group(5)), int(m.group(6)),
                tzinfo=timezone.utc
            )
        except Exception:
            pass

    # Complete submission text/header is a more reliable fallback.
    m = re.search(r'ACCEPTANCE-DATETIME:\s*(\d{14})', index_html, re.I)
    if m:
        raw = m.group(1)
        try:
            # SEC displays acceptance time as Eastern; convert conservatively.
            from zoneinfo import ZoneInfo
            return datetime.strptime(raw, "%Y%m%d%H%M%S").replace(
                tzinfo=ZoneInfo("America/New_York")
            ).astimezone(timezone.utc)
        except Exception:
            pass
    return None

def sec_items(index_html):
    items = set()
    for m in re.finditer(r'Item\s+(\d+\.\d+)\s*:\s*([^<\n\r]+)', index_html, re.I):
        items.add((m.group(1), clean_text(m.group(2))))
    return sorted(items)

def sec_document_priority(label, url):
    x = (label + " " + url).lower()
    if "press release" in x or "exhibit 99" in x or "ex-99" in x:
        return 0
    if "exhibit 10" in x or "ex-10" in x:
        return 1
    if "exhibit 2" in x or "ex-2" in x:
        return 2
    if "8-k" in x or "6-k" in x or "current report" in x:
        return 3
    return 4

def sec_extract_relevant_text(index_url):
    try:
        raw_index = http_get(index_url, headers={"User-Agent": SEC_UA}, timeout=25, retries=2)
        index_html = raw_index.decode("utf-8", errors="ignore")
    except Exception as e:
        print("SEC index fetch failed:", e)
        return "", None, [], []

    accepted = sec_accepted_datetime(index_html)
    items = sec_items(index_html)
    docs = sec_index_documents(index_html, index_url)

    texts = []
    for url, label in sorted(docs, key=lambda x: sec_document_priority(x[1], x[0]))[:8]:
        # Avoid image/data/XBRL files.
        if re.search(r'\.(?:jpg|jpeg|png|gif|xml|xsd|json)$', url, re.I):
            continue
        try:
            raw = http_get(url, headers={"User-Agent": SEC_UA}, timeout=20, retries=2)
            txt = clean_text(raw.decode("utf-8", errors="ignore"))
            if len(txt) >= 200:
                texts.append(txt[:120000])
        except Exception as e:
            print("SEC exhibit fetch failed:", url, e)

    return "\n ".join(texts), accepted, items, docs

def sec_material_signal(form, items, text):
    low = text.lower()
    item_numbers = {x[0] for x in items}

    # These are the SEC sections most likely to contain capital/deal events.
    material_items = {"1.01", "2.01", "2.03", "3.02", "5.02", "7.01", "8.01"}
    has_material_item = bool(item_numbers & material_items)

    if form in ("S-1", "S-3", "424B4", "424B5"):
        return True
    if form in ("8-K", "6-K") and not has_material_item:
        return False

    # A 6-K can disclose financing/contract events without a 1.01-style item.
    financing = any(x in low for x in [
        "securities purchase agreement", "registered direct offering",
        "private placement", "public offering", "ordinary shares",
        "preferred shares", "warrants", "convertible", "subscription agreement",
        "financing", "funding", "investment agreement", "purchase order",
        "framework agreement", "definitive agreement", "material contract",
    ])
    return has_material_item or financing

def sec_classify_precise(form, items, text):
    low = text.lower()

    # Hard exclusions: filings that frequently create noise but do not represent
    # fresh capital deployment.
    if any(x in low for x in [
        "auditor", "accounting firm", "board committee", "director resignation",
        "director appointment", "executive officer", "annual meeting",
        "compensation", "employment agreement", "bylaws", "articles of incorporation",
        "restated certificate", "shareholder voting", "proxy", "earnings",
        "financial results", "quarterly results",
    ]):
        # Do not exclude if the same document contains a strong financing/transaction signal.
        if not any(x in low for x in [
            "securities purchase agreement", "private placement", "registered direct",
            "underwriting agreement", "acquisition agreement", "merger agreement",
            "purchase order", "awarded a contract", "new contract",
            "financing agreement", "credit agreement",
        ]):
            return "NON_CAPITAL", False

    if any(x in low for x in [
        "force majeure", "termination", "terminated", "cancelled", "canceled",
        "suspended", "suspension", "delay", "delayed", "withdrawn",
        "rescinded", "default", "bankruptcy",
    ]):
        # A termination can itself be capital-flow relevant, but it is not new money.
        return "CAPITAL_RISK_OR_REVERSAL", False

    if any(x in low for x in [
        "securities purchase agreement", "registered direct offering",
        "private placement", "public offering", "underwritten offering",
        "shares of common stock", "preferred stock", "convertible notes",
        "warrants", "subscription agreement", "gross proceeds",
        "net proceeds", "purchase price per share",
    ]):
        return "NEW_FUNDING", True

    if any(x in low for x in [
        "purchase order", "awarded", "award agreement", "supply agreement",
        "customer agreement", "definitive agreement", "framework agreement",
        "master services agreement", "long-term agreement", "contract with",
        "selected as", "selected to provide", "procurement",
    ]):
        # Only count as a contract if there is a commercial counterparty/event,
        # not merely an exhibit containing generic legal language.
        commercial = any(x in low for x in [
            "contract", "customer", "purchase order", "purchase commitment",
            "revenue", "backlog", "megawatt", "mw", "gigawatt", "gw",
            "supply", "services", "facility", "data center", "construction",
        ])
        return ("NEW_CONTRACT", False) if commercial else ("NON_CAPITAL", False)

    if any(x in low for x in [
        "acquisition agreement", "merger agreement", "business combination",
        "acquire", "acquisition", "merger", "transaction value",
    ]):
        return "M&A_OR_DEAL", False

    if any(x in low for x in [
        "capital expenditure", "capital expenditures", "capex",
        "construct", "construction", "build a", "builds a", "expansion",
        "expand capacity", "new facility", "data center",
    ]):
        return "NEW_CAPEX", False

    return "NON_CAPITAL", False

KNOWN_TICKERS = {
    "oracle": "ORCL",
    "oracle corporation": "ORCL",
    "bloom energy": "BE",
    "blue owl": "OWL",
    "blue owl capital": "OWL",
    "caesars entertainment": "CZR",
    "caesars": "CZR",
    "nvidia": "NVDA",
    "microsoft": "MSFT",
    "amazon": "AMZN",
    "amazon web services": "AMZN",
    "alphabet": "GOOGL",
    "google": "GOOGL",
    "meta platforms": "META",
    "meta": "META",
    "microsoft azure": "MSFT",
    "dell": "DELL",
    "supermicro": "SMCI",
    "vertiv": "VRT",
    "eaton": "ETN",
    "quanta services": "PWR",
    "constellation energy": "CEG",
    "vistra": "VST",
    "equinix": "EQIX",
    "digital realty": "DLR",
    "arista networks": "ANET",
    "broadcom": "AVGO",
    "amd": "AMD",
    "advanced micro devices": "AMD",
}

# Language that strongly indicates the article is not announcing fresh capital deployment.
NEGATIVE_EVENT_TERMS = [
    "force majeure", "warning sign", "flashing a warning", "trouble", "collapse",
    "shutdown", "shut down", "delayed", "delay", "cancellation", "cancelled",
    "canceled", "scraps", "shelved", "halts", "halted", "suspends", "suspended",
    "risk", "risks", "concern", "concerns", "under pressure", "debt pressure",
    "debt woes", "struggles", "struggling", "misses", "failure", "failed",
    "lawsuit", "probe", "investigation", "downgrade", "default", "bankruptcy",
    "force-majeure", "reconsider", "reconsidering", "could delay", "may delay",
]

COMMENTARY_TERMS = [
    "opinion", "analysis", "why", "what investors need to know", "warning sign",
    "flashing a warning", "explained", "everything you need to know", "should",
    "could", "might", "here's why", "here is why", "what it means",
]

POSITIVE_ANNOUNCEMENT_TERMS = [
    "announced", "announcement", "signs", "signed", "agreement", "definitive agreement",
    "awarded", "wins contract", "won contract", "selected", "chosen", "orders",
    "ordered", "purchase order", "commits", "committed", "commitment", "invest",
    "investment", "invests", "plans to invest", "will invest", "funding",
    "financing", "raises", "raised", "offering", "acquisition", "acquire",
    "merger", "joint venture", "partnership", "launches", "launch", "builds",
    "build", "construction begins", "groundbreaking", "expansion", "capacity",
    "procure", "procurement", "contract extension", "backlog",
]

CAPITAL_PATTERN = re.compile(
    r"(?P<prefix>up to\s+|more than\s+|over\s+|approximately\s+|about\s+|nearly\s+|at least\s+)?"
    r"\$?\s*(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>trillion|billion|million|bn|mm|m|tn)\b",
    re.I,
)

DATE_PATTERNS = [
    re.compile(r"\b(?:on|as of|dated|announced on)\s+([A-Z][a-z]+ \d{1,2}, \d{4})", re.I),
    re.compile(r"\b([A-Z][a-z]+ \d{1,2}, \d{4})\b"),
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
]

def now_utc():
    return datetime.now(timezone.utc)

def parse_dt(value):
    if not value:
        return None
    value = value.strip()
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value[:-1] + "+00:00")
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S GMT"):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    return None

def clean_text(s):
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()

def http_get(url, headers=None, timeout=25, retries=3):
    headers = headers or {}
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                enc = (r.headers.get("Content-Encoding") or "").lower()
                if enc == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last

def telegram_send(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram secrets are not configured.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": "true",
    }).encode()
    try:
        req = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print("Telegram error:", e)
        return False

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {"seen": data}
    except Exception:
        pass
    return {"seen": []}

def save_state(state):
    state["seen"] = state.get("seen", [])[-3000:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)

def canonical_title(title):
    s = clean_text(title).lower()
    s = re.sub(r"https?://\S+", " ", s)
    s = re.sub(r"\$[\d.,]+\s*(?:billion|million|trillion|bn|mm|m|tn)?", " money ", s)
    s = re.sub(r"\b\d+(?:\.\d+)?\s*(?:billion|million|trillion|bn|mm|m|tn)\b", " money ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    stop = {
        "the","a","an","and","or","of","to","in","for","on","as","is","are","with",
        "its","this","that","from","by","at","after","over","more","than","says",
        "said","new","latest","update","report","reports"
    }
    words = [w for w in s.split() if w not in stop]
    return " ".join(words[:35])

def similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()

def extract_capital(text):
    vals = []
    for m in CAPITAL_PATTERN.finditer(text):
        n = float(m.group("num"))
        unit = m.group("unit").lower()
        if unit in ("trillion", "tn"):
            amount = n * 1_000_000_000_000
        elif unit in ("billion", "bn"):
            amount = n * 1_000_000_000
        else:
            amount = n * 1_000_000
        vals.append((amount, m.group(0)))
    if not vals:
        return 0, ""
    amount, raw = max(vals, key=lambda x: x[0])
    return amount, raw

def fmt_money(amount):
    if not amount:
        return "Not disclosed"
    if amount >= 1e12:
        return f"${amount/1e12:.1f}T"
    if amount >= 1e9:
        return f"${amount/1e9:.1f}B"
    return f"${amount/1e6:.0f}M"

def find_event_date(text, fallback):
    # Conservative: only use explicit calendar dates when present.
    for p in DATE_PATTERNS:
        m = p.search(text)
        if m:
            raw = m.group(1)
            for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
                try:
                    return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
                except Exception:
                    pass
    return fallback

def is_recent(dt):
    if not dt:
        return False
    age = now_utc() - dt
    return timedelta(hours=-1) <= age <= timedelta(hours=LOOKBACK_HOURS)

def get_tickers(text):
    low = clean_text(text).lower()
    found = set()
    for name, ticker in KNOWN_TICKERS.items():
        if re.search(r"\b" + re.escape(name) + r"\b", low):
            found.add(ticker)

    # Common explicit "(NYSE: ABC)" / "NASDAQ: ABC" forms.
    for m in re.finditer(r"(?:NYSE|NASDAQ|AMEX|TSX|LSE)\s*:\s*([A-Z]{1,6})", text):
        found.add(m.group(1).upper())

    # Explicit ticker after company name, e.g. "Bloom Energy (BE)".
    for name, ticker in KNOWN_TICKERS.items():
        pattern = re.escape(name) + r"\s*\(\s*" + re.escape(ticker) + r"\s*\)"
        if re.search(pattern, text, re.I):
            found.add(ticker)

    return sorted(found)[:8]

def classify_news(title, desc):
    text = clean_text(title + " " + desc)
    low = text.lower()

    negative_hits = [x for x in NEGATIVE_EVENT_TERMS if x in low]
    commentary_hits = [x for x in COMMENTARY_TERMS if x in low]
    positive_hits = [x for x in POSITIVE_ANNOUNCEMENT_TERMS if x in low]

    # Risk/reversal takes precedence over generic capital words.
    if negative_hits:
        return "RISK_OR_REVERSAL", negative_hits, positive_hits

    if len(commentary_hits) >= 1 and not positive_hits:
        return "COMMENTARY", commentary_hits, positive_hits

    # A capital number alone is never enough.
    if not positive_hits:
        return "BACKGROUND", [], []

    if any(x in low for x in [
        "acquisition", "acquire", "merger", "definitive agreement",
        "joint venture", "partnership"
    ]):
        return "M&A_OR_DEAL", [], positive_hits

    if any(x in low for x in [
        "contract", "awarded", "selected", "purchase order", "orders",
        "procure", "procurement", "agreement"
    ]):
        return "CONTRACT_OR_PROCUREMENT", [], positive_hits

    if any(x in low for x in [
        "invest", "investment", "funding", "financing", "raises",
        "raised", "offering", "commits", "committed", "commitment"
    ]):
        return "NEW_CAPITAL", [], positive_hits

    if any(x in low for x in ["build", "construction", "capacity", "expansion", "launch"]):
        return "CAPITAL_PROJECT", [], positive_hits

    return "BACKGROUND", [], positive_hits

def capital_is_new_money(text):
    low = clean_text(text).lower()

    new_money_phrases = [
        "will invest", "plans to invest", "invests", "investing",
        "commits", "committed", "commitment of", "funding of",
        "financing of", "raised", "raises", "offering of",
        "awarded contract worth", "contract worth", "purchase order worth",
        "acquisition for", "merger valued at", "transaction valued at",
        "agreement worth",
    ]
    existing_project_phrases = [
        "worth $", "valued at $", "project is", "project worth",
        "planned", "previously announced", "already announced",
        "broader agreement", "part of a", "under construction",
        "existing project", "existing agreement", "up to $",
    ]

    has_new = any(x in low for x in new_money_phrases)
    has_existing = any(x in low for x in existing_project_phrases)

    # "up to" is usually a maximum contractual capacity rather than fresh cash.
    if "up to" in low and not any(x in low for x in [
        "new funding", "new investment", "new financing", "new capital"
    ]):
        return False

    if has_new and not (
        "force majeure" in low or "delay" in low or "shutdown" in low or "warning" in low
    ):
        return True

    return False

def score_event(kind, amount, tickers, beneficiaries, title, desc):
    text = (title + " " + desc).lower()
    score = 0

    if kind == "NEW_CAPITAL":
        score += 30
    elif kind == "CONTRACT_OR_PROCUREMENT":
        score += 25
    elif kind == "M&A_OR_DEAL":
        score += 18
    elif kind == "CAPITAL_PROJECT":
        score += 10
    else:
        score += 0

    # Dollar size only contributes when the event contains credible new-money language.
    if kind in ("NEW_CAPITAL", "CONTRACT_OR_PROCUREMENT", "M&A_OR_DEAL"):
        if amount >= 10e9:
            score += 25
        elif amount >= 1e9:
            score += 20
        elif amount >= 250e6:
            score += 12
        elif amount >= 50e6:
            score += 7

    if tickers:
        score += 15
    if beneficiaries:
        score += min(15, 5 * len(beneficiaries))

    if any(x in text for x in ["definitive agreement", "awarded", "signed", "selected"]):
        score += 10

    return min(score, 100)

def first_mover_text(event_dt, repricing):
    if not event_dt:
        return "UNKNOWN"
    age = now_utc() - event_dt
    if age <= timedelta(hours=6):
        base = "VERY EARLY"
    elif age <= timedelta(hours=12):
        base = "EARLY"
    elif age <= timedelta(hours=24):
        base = "LATE"
    else:
        base = "OUTSIDE WINDOW"

    if repricing is not None:
        if abs(repricing) >= 8:
            return f"{base} / HEAVILY REPRICED"
        if abs(repricing) >= 4:
            return f"{base} / REPRICING UNDERWAY"
    return base

def yahoo_change(ticker):
    try:
        end = int(time.time())
        start = end - 3 * 86400
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}"
            f"?period1={start}&period2={end}&interval=15m&events=history"
        )
        raw = http_get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15, retries=2)
        data = json.loads(raw.decode("utf-8"))
        result = data["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        vals = [x for x in closes if x is not None]
        if len(vals) < 2:
            return None
        return (vals[-1] / vals[-2] - 1) * 100
    except Exception as e:
        print("Yahoo repricing unavailable for", ticker, e)
        return None

def bamboo_status(tickers):
    if not tickers:
        return "CHECK REQUIRED — no public ticker identified"
    return "CHECK REQUIRED — verify ticker is tradable in Bamboo"

def entry_exit(score, repricing):
    if repricing is None:
        if score >= 60:
            return (
                "VERIFY FIRST",
                "No live repricing check. Confirm the event, beneficiary relationship, and Bamboo tradability before any execution."
            )
        return (
            "NO ENTRY",
            "Signal is not sufficiently verified for an execution decision."
        )

    if repricing >= 10:
        return (
            "DO NOT CHASE",
            "Already strongly repriced; wait for consolidation/retest or a new catalyst."
        )

    if repricing >= 5:
        return (
            "WAIT / PULLBACK",
            "Repricing is underway; avoid chasing the first move."
        )

    if score >= 60:
        return (
            "EARLY-WINDOW WATCH",
            "Verify the event and Bamboo tradability before execution."
        )

    return (
        "WATCH",
        "Wait for stronger confirmation."
    )

def parse_atom(raw):
    root = ET.fromstring(raw)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    items = []
    for e in root.findall("a:entry", ns):
        title = clean_text(e.findtext("a:title", default="", namespaces=ns))
        link_el = e.find("a:link", ns)
        link = link_el.attrib.get("href", "") if link_el is not None else ""
        updated = parse_dt(e.findtext("a:updated", default="", namespaces=ns))
        summary = clean_text(e.findtext("a:summary", default="", namespaces=ns))
        items.append((title, link, updated, summary))
    return items

def sec_events():
    events = []

    for form, url in SEC_FEEDS.items():
        try:
            raw = http_get(
                url,
                headers={"User-Agent": SEC_UA, "Accept-Encoding": "gzip"},
                timeout=30,
                retries=3,
            )
            items = parse_atom(raw)[:SEC_LIMIT_PER_FEED]
        except Exception as e:
            print("SEC feed error", form, e)
            continue

        for title, link, feed_dt, summary in items:
            if not link:
                continue

            # The Atom feed date is only a discovery hint. The official EDGAR
            # acceptance timestamp is the authoritative freshness timestamp.
            try:
                filing_text, accepted, sec_items_list, docs = sec_extract_relevant_text(link)
            except Exception as e:
                print("SEC extraction error:", e)
                continue

            event_dt = accepted or feed_dt
            if not is_recent(event_dt):
                continue

            # Ignore routine filings before doing expensive classification.
            if not sec_material_signal(form, sec_items_list, filing_text):
                continue

            classification, fresh_money = sec_classify_precise(
                form, sec_items_list, filing_text
            )

            if classification in ("NON_CAPITAL", "CAPITAL_RISK_OR_REVERSAL"):
                # Risk/reversal is deliberately not sent as a capital-deployment alert.
                continue

            combined = clean_text(title + " " + summary + " " + filing_text)

            amount, raw_amount = extract_capital(combined)

            # Funding: require explicit proceeds/funding language.
            if classification == "NEW_FUNDING":
                funding_language = any(x in combined.lower() for x in [
                    "gross proceeds", "net proceeds", "will receive",
                    "aggregate purchase price", "offering proceeds",
                    "raise approximately", "raising approximately",
                    "financing of", "funding of",
                ])
                if not funding_language:
                    # It may still be a financing document, but without disclosed
                    # proceeds it is too ambiguous for the capital-flow radar.
                    amount = 0
                    raw_amount = ""

            # For contracts/capex, a project value is useful only when tied to
            # an actual agreement/commitment rather than historical background.
            if classification in ("NEW_CONTRACT", "NEW_CAPEX"):
                if not any(x in combined.lower() for x in [
                    "signed", "entered into", "entered an agreement",
                    "awarded", "purchase order", "selected", "committed",
                    "will build", "will construct", "will invest",
                    "framework agreement", "supply agreement",
                ]):
                    continue

            cik = sec_cik_from_url(link)
            meta = sec_company_metadata(cik) if cik else {"name": "", "tickers": []}

            tickers = set(meta.get("tickers", []))
            tickers.update(get_tickers(combined))

            # Beneficiaries are companies other than the filer when possible.
            beneficiaries = []
            filer_name = meta.get("name", "").lower()
            for name, ticker in KNOWN_TICKERS.items():
                if re.search(r"\b" + re.escape(name) + r"\b", combined, re.I):
                    if name.lower() not in filer_name:
                        beneficiaries.append(f"{name.title()} ({ticker})")

            # If the filer itself is the public beneficiary, include it separately.
            if meta.get("tickers"):
                filer_label = meta.get("name", "Filer")
                beneficiaries.insert(
                    0,
                    f"{filer_label} ({', '.join(meta['tickers'][:3])})"
                )

            beneficiaries = list(dict.fromkeys(beneficiaries))[:8]

            # SEC title + accession number is the stable event identity.
            accession = re.search(r"/Archives/edgar/data/\d+/([^/]+)/", link)
            accession_id = accession.group(1) if accession else link

            key = hashlib.sha256(
                (
                    "SEC|" + accession_id + "|" +
                    classification + "|" +
                    ",".join(sorted(tickers))
                ).encode()
            ).hexdigest()[:20]

            score = score_event(
                "NEW_CAPITAL" if classification == "NEW_FUNDING" else (
                    "CONTRACT_OR_PROCUREMENT"
                    if classification in ("NEW_CONTRACT", "NEW_CAPEX")
                    else "M&A_OR_DEAL"
                ),
                amount,
                sorted(tickers),
                beneficiaries,
                title,
                combined[:30000],
            )

            events.append({
                "source_type": "SEC",
                "form": form,
                "title": title,
                "url": link,
                "published": feed_dt.isoformat() if feed_dt else event_dt.isoformat(),
                "event_dt": event_dt.isoformat(),
                "kind": (
                    "NEW_CAPITAL" if classification == "NEW_FUNDING"
                    else "CONTRACT_OR_PROCUREMENT" if classification in ("NEW_CONTRACT", "NEW_CAPEX")
                    else "M&A_OR_DEAL"
                ),
                "sec_classification": classification,
                "amount": amount,
                "raw_amount": raw_amount,
                "new_money": fresh_money or classification in ("NEW_CONTRACT", "NEW_CAPEX", "M&A_OR_DEAL"),
                "tickers": sorted(tickers)[:8],
                "beneficiaries": beneficiaries,
                "score": score,
                "key": key,
            })

    return events

def google_news_events():
    queries = [
        '"announced" investment billion company',
        '"signed" contract billion company',
        '"awarded" contract billion company',
        '"commits" billion investment company',
        '"plans to invest" billion company',
        '"definitive agreement" billion company',
        '"purchase order" billion company',
        '"data center" contract billion',
        '"AI infrastructure" investment billion',
        '"capacity expansion" billion company',
    ]

    events = []
    seen_story_titles = []

    for q in queries:
        rss_url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({
            "q": q,
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        })

        try:
            raw = http_get(
                rss_url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=25,
                retries=3,
            )
            root = ET.fromstring(raw)
        except Exception as e:
            print("Google News error:", e)
            continue

        for item in root.findall("./channel/item"):
            title = clean_text(item.findtext("title", default=""))
            link = clean_text(item.findtext("link", default=""))
            pub = parse_dt(item.findtext("pubDate", default=""))
            desc = clean_text(item.findtext("description", default=""))

            if not title or not pub or not is_recent(pub):
                continue

            # De-duplicate near-identical article titles across queries.
            canon = canonical_title(title)
            if any(similarity(canon, x) >= 0.86 for x in seen_story_titles):
                continue
            seen_story_titles.append(canon)

            kind, neg, pos = classify_news(title, desc)

            # This radar is for fresh capital-flow events. Pure risk, commentary,
            # background and recycled project coverage are not capital-flow alerts.
            if kind in ("RISK_OR_REVERSAL", "COMMENTARY", "BACKGROUND"):
                continue

            combined = title + " " + desc
            amount, raw_amount = extract_capital(combined)
            new_money = capital_is_new_money(combined)

            # Capital project stories can be valid if the article actually announces
            # a new build/expansion. Existing project value alone is insufficient.
            if kind == "NEW_CAPITAL" and not new_money:
                continue

            # For M&A, the transaction value itself is a deal-flow signal.
            if kind == "M&A_OR_DEAL":
                new_money = True

            tickers = get_tickers(combined)

            beneficiaries = []
            for name, ticker in KNOWN_TICKERS.items():
                if re.search(r"\b" + re.escape(name) + r"\b", combined, re.I):
                    beneficiaries.append(f"{name.title()} ({ticker})")
            beneficiaries = list(dict.fromkeys(beneficiaries))[:6]

            # Do not treat a dated historical reference inside a news article as the
            # event date unless it is explicitly presented as the announcement date.
            event_dt = find_event_date(combined, pub)
            if event_dt and event_dt < now_utc() - timedelta(hours=LOOKBACK_HOURS):
                # The article itself is fresh but the underlying event is old.
                # Reject unless the article contains strong present-tense announcement language.
                present = any(x in combined.lower() for x in [
                    "announced today", "announced on", "today announced",
                    "has announced", "signed today", "awarded today",
                    "confirmed today", "disclosed today"
                ])
                if not present:
                    continue

            score = score_event(
                kind,
                amount if new_money else 0,
                tickers,
                beneficiaries,
                title,
                desc,
            )

            events.append({
                "source_type": "NEWS",
                "form": "NEWS",
                "title": title,
                "url": link,
                "published": pub.isoformat(),
                "event_dt": event_dt.isoformat() if event_dt else pub.isoformat(),
                "kind": kind,
                "amount": amount if new_money else 0,
                "raw_amount": raw_amount if new_money else "",
                "new_money": new_money,
                "tickers": tickers,
                "beneficiaries": beneficiaries,
                "score": score,
                "key": hashlib.sha256(
                    ("NEWS|" + canon).encode()
                ).hexdigest()[:20],
            })

    return events

def cluster_events(events):
    """
    Collapse the same underlying event even when several publishers report it.
    Keep the highest-scoring / most direct source.
    """
    clusters = []

    for ev in sorted(events, key=lambda x: (x["score"], x.get("published", "")), reverse=True):
        title = canonical_title(ev["title"])
        merged = False

        for cluster in clusters:
            rep = cluster[0]
            rep_title = canonical_title(rep["title"])

            same_ticker = bool(set(ev.get("tickers", [])) & set(rep.get("tickers", [])))
            same_company_words = 0
            a = set(title.split())
            b = set(rep_title.split())
            if a and b:
                same_company_words = len(a & b) / max(1, len(a | b))

            close_time = True
            try:
                t1 = parse_dt(ev.get("published"))
                t2 = parse_dt(rep.get("published"))
                close_time = abs((t1 - t2).total_seconds()) <= 24 * 3600
            except Exception:
                pass

            same_project = any(
                phrase in (ev["title"] + " " + rep["title"]).lower()
                for phrase in ["project jupiter", "stargate", "data center", "fuel cell"]
            )

            if close_time and (
                similarity(title, rep_title) >= 0.72
                or (same_ticker and same_company_words >= 0.30)
                or (same_project and same_company_words >= 0.20)
            ):
                cluster.append(ev)
                merged = True
                break

        if not merged:
            clusters.append([ev])

    final = []
    for cluster in clusters:
        best = cluster[0]

        # Merge tickers/beneficiaries found in another article in the same cluster.
        ticks = set()
        bens = []
        for ev in cluster:
            ticks.update(ev.get("tickers", []))
            for b in ev.get("beneficiaries", []):
                if b not in bens:
                    bens.append(b)

        best["tickers"] = sorted(ticks)[:8]
        best["beneficiaries"] = bens[:8]
        best["score"] = score_event(
            best["kind"],
            best["amount"] if best["new_money"] else 0,
            best["tickers"],
            best["beneficiaries"],
            best["title"],
            best["title"],
        )
        final.append(best)

    return final

def format_alert(ev):
    tickers = ", ".join(ev.get("tickers", [])) or "None identified"
    beneficiaries = ", ".join(ev.get("beneficiaries", [])) or "None identified"

    repricing = None
    repricing_parts = []
    for ticker in ev.get("tickers", [])[:4]:
        ch = yahoo_change(ticker)
        if ch is not None:
            repricing_parts.append(f"{ticker} {ch:+.2f}%")
            if repricing is None:
                repricing = ch

    entry, exit_plan = entry_exit(ev["score"], repricing)
    event_dt = parse_dt(ev.get("event_dt")) or parse_dt(ev.get("published"))
    first = first_mover_text(event_dt, repricing)

    source = ev.get("source_type", "UNKNOWN")
    url = ev.get("url", "")

    lines = [
        "🚨 CAPITAL FLOW RADAR",
        "",
        f"Alpha Score: {ev['score']}/100",
        f"Source: {source} | Form: {ev.get('form', 'NEWS')}",
        "",
        "📌 EVENT",
        ev["title"][:350],
        "",
        f"🕐 Event time: {event_dt.strftime('%Y-%m-%d %H:%M UTC') if event_dt else 'Unknown'}",
        f"💰 New capital / deal value: {fmt_money(ev.get('amount', 0))}",
        f"🧾 Event class: {ev['kind']}",
        f"💵 Fresh-money signal: {'YES' if ev.get('new_money') else 'NO'}",
        "",
        "🎯 PUBLIC EXPOSURE / BENEFICIARIES",
        beneficiaries,
        f"Tickers: {tickers}",
        "",
        "📈 REPRICING",
        ", ".join(repricing_parts) if repricing_parts else "No reliable market-price check",
        f"First-mover window: {first}",
        "",
        "🟢 ENTRY PLAN",
        entry,
        "",
        "🔴 EXIT / RISK PLAN",
        exit_plan,
        "",
        "🟡 BAMBOO",
        bamboo_status(ev.get("tickers", [])),
    ]

    if url:
        lines += ["", f"🔗 {url}"]

    return "\n".join(lines)

def main():
    print("Capital Flow Radar starting...")
    print("UTC now:", now_utc().isoformat())

    state = load_state()
    seen = set(state.get("seen", []))

    sec = sec_events()
    news = google_news_events()

    print(f"SEC candidates: {len(sec)}")
    print(f"News candidates: {len(news)}")

    all_events = cluster_events(sec + news)

    # Final safety filters.
    fresh = []
    for ev in all_events:
        if ev["key"] in seen:
            continue

        # Never alert on a capital figure unless the event has a credible new/deal signal.
        if ev["amount"] <= 0 and ev["kind"] not in ("CONTRACT_OR_PROCUREMENT", "M&A_OR_DEAL"):
            continue

        # No ticker and no identified beneficiary = informational signal only.
        # Keep the event if score is high enough, but make no execution claim.
        fresh.append(ev)

    fresh.sort(key=lambda x: x["score"], reverse=True)

    sent = 0
    for ev in fresh[:10]:
        message = format_alert(ev)
        print("\n--- ALERT ---\n" + message + "\n--- END ALERT ---\n")

        if telegram_send(message):
            seen.add(ev["key"])
            sent += 1
        else:
            print("Alert not marked seen because Telegram delivery failed.")

        # Avoid hammering external endpoints.
        time.sleep(0.8)

    state["seen"] = list(seen)
    state["last_run"] = now_utc().isoformat()
    state["last_counts"] = {
        "sec_candidates": len(sec),
        "news_candidates": len(news),
        "clustered_events": len(all_events),
        "fresh_alerts": len(fresh),
        "sent": sent,
    }
    save_state(state)

    print(
        f"Done. SEC={len(sec)} News={len(news)} "
        f"Clustered={len(all_events)} Fresh={len(fresh)} Sent={sent}"
    )

if __name__ == "__main__":
    main()
