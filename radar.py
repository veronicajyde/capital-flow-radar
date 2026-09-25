import os
import json
import re
import time
import gzip
import html
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta


# ============================================================
# CAPITAL FLOW RADAR
# Production resilient version
# ============================================================

UA = "Capital Flow Radar veronicajyde@gmail.com"

STATE_FILE = "radar_state.json"

LOOKBACK_HOURS = 24
MAX_ALERTS_PER_RUN = 5

SEC_DELAY = 0.35
REQUEST_TIMEOUT = 18

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SEC_FORMS = ["8-K", "6-K", "S-1", "S-3", "424B4"]

CAPITAL_TERMS = [
    "acquisition",
    "acquire",
    "merger",
    "definitive agreement",
    "investment",
    "strategic investment",
    "funding",
    "financing",
    "capital",
    "offering",
    "private placement",
    "public offering",
    "secondary offering",
    "registered direct",
    "convertible",
    "debt financing",
    "credit facility",
    "capex",
    "capital expenditure",
    "infrastructure",
    "data center",
    "datacenter",
    "ai infrastructure",
    "artificial intelligence infrastructure",
    "cloud infrastructure",
    "factory",
    "manufacturing",
    "facility",
    "construction",
    "contract",
    "purchase agreement",
    "supply agreement",
    "strategic partnership",
    "expansion",
    "capacity expansion",
    "production",
]

BENEFICIARY_TERMS = [
    "supplier",
    "supplies",
    "provided by",
    "provider",
    "customer",
    "partner",
    "partnership",
    "contractor",
    "equipment",
    "vendor",
    "manufacturer",
    "service provider",
    "technology provider",
    "infrastructure provider",
    "cloud provider",
]

HIGH_VALUE_TERMS = [
    "billion",
    "$1 billion",
    "$2 billion",
    "$5 billion",
    "$10 billion",
    "multi-billion",
    "multibillion",
    "large-scale",
    "major contract",
    "long-term agreement",
    "definitive agreement",
    "acquisition",
    "merger",
    "data center",
    "ai infrastructure",
]


# ============================================================
# TIME
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def parse_date(value):
    if not value:
        return None

    value = value.strip()

    formats = [
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass

    try:
        value2 = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(value2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def fresh(dt):
    if not dt:
        return False

    age = utc_now() - dt
    return timedelta(hours=0) <= age <= timedelta(hours=LOOKBACK_HOURS)


def age_hours(dt):
    if not dt:
        return 999

    return max(0, (utc_now() - dt).total_seconds() / 3600)


# ============================================================
# HTTP
# ============================================================

def fetch(url, retries=3, delay=1.5):
    last_error = None

    headers = {
        "User-Agent": UA,
        "Accept-Encoding": "gzip",
        "Accept": "*/*",
        "Connection": "close",
    }

    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=headers)

            with urllib.request.urlopen(
                request,
                timeout=REQUEST_TIMEOUT
            ) as response:

                raw = response.read()

                encoding = response.headers.get("Content-Encoding", "")

                if "gzip" in encoding.lower():
                    raw = gzip.decompress(raw)

                return raw.decode("utf-8", errors="replace")

        except urllib.error.HTTPError as e:
            last_error = f"HTTP Error {e.code}: {e.reason}"

            # Rate limits / temporary server failures.
            if e.code in (429, 500, 502, 503, 504):
                wait = delay * (2 ** attempt)
                time.sleep(wait)
                continue

            break

        except Exception as e:
            last_error = str(e)

            if attempt < retries - 1:
                wait = delay * (2 ** attempt)
                time.sleep(wait)

    print(f"FETCH ERROR: {last_error}")
    return None


# ============================================================
# TEXT UTILITIES
# ============================================================

def clean_text(value):
    if not value:
        return ""

    value = html.unescape(value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def find_words(text, words):
    low = text.lower()

    found = []

    for word in words:
        if word.lower() in low:
            found.append(word)

    return list(dict.fromkeys(found))


def money_value(text):
    if not text:
        return 0

    text = text.lower().replace(",", "")

    patterns = [
        r"\$\s*(\d+(?:\.\d+)?)\s*(billion|bn)",
        r"\$\s*(\d+(?:\.\d+)?)\s*(million|mn)",
        r"(\d+(?:\.\d+)?)\s*(billion|bn)\s*(?:dollars|usd)",
        r"(\d+(?:\.\d+)?)\s*(million|mn)\s*(?:dollars|usd)",
    ]

    best = 0

    for pattern in patterns:
        for match in re.finditer(pattern, text):
            number = float(match.group(1))
            unit = match.group(2)

            if unit in ("billion", "bn"):
                value = number * 1_000_000_000
            else:
                value = number * 1_000_000

            best = max(best, value)

    return best


def money_label(value):
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value > 0:
        return f"${value:,.0f}"

    return "Not disclosed"


def find_tickers(text):
    if not text:
        return []

    candidates = []

    # $TICKER
    for x in re.findall(r"\$([A-Z]{1,5})\b", text):
        candidates.append(x)

    # Common "Company (NASDAQ: ABC)" format.
    for x in re.findall(
        r"(?:NASDAQ|NYSE|AMEX|NYSE American|OTC)[:\s]+([A-Z]{1,5})",
        text,
        flags=re.I
    ):
        candidates.append(x.upper())

    # Avoid obvious English words.
    blacklist = {
        "THE", "AND", "FOR", "WITH", "FROM", "THIS",
        "THAT", "SEC", "CEO", "CFO", "USA", "USD",
        "AI", "US", "A", "I"
    }

    output = []

    for ticker in candidates:
        ticker = ticker.upper()

        if ticker not in blacklist and ticker not in output:
            output.append(ticker)

    return output[:10]


# ============================================================
# STATE
# ============================================================

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"seen": []}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            return {"seen": data[-2000:]}

        if isinstance(data, dict):
            seen = data.get("seen", [])

            if not isinstance(seen, list):
                seen = []

            return {"seen": seen[-2000:]}

    except Exception as e:
        print(f"STATE LOAD ERROR: {e}")

    return {"seen": []}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"STATE SAVE ERROR: {e}")


# ============================================================
# SEC
# ============================================================

def sec_feed_url(form):
    encoded = urllib.parse.quote(form)

    return (
        "https://www.sec.gov/cgi-bin/browse-edgar"
        f"?action=getcurrent&type={encoded}"
        "&output=atom&count=100"
    )


def parse_sec_feed(xml_text, form):
    events = []

    try:
        root = ET.fromstring(xml_text)
    except Exception as e:
        print(f"SEC XML ERROR ({form}): {e}")
        return events

    ns = {
        "atom": "http://www.w3.org/2005/Atom"
    }

    entries = root.findall("atom:entry", ns)

    if not entries:
        # Try namespace-free parsing as fallback.
        entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")

    for entry in entries:
        try:
            title = entry.findtext("atom:title", "", ns)
            updated = entry.findtext("atom:updated", "", ns)
            summary = entry.findtext("atom:summary", "", ns)
            published = entry.findtext("atom:published", "", ns)

            title = clean_text(title)
            summary = clean_text(summary)

            dt = parse_date(updated) or parse_date(published)

            if not fresh(dt):
                continue

            combined = f"{title} {summary}"

            capital_hits = find_words(
                combined,
                CAPITAL_TERMS
            )

            if not capital_hits:
                continue

            link = ""

            for link_node in entry.findall("atom:link", ns):
                href = link_node.attrib.get("href")

                if href:
                    link = href
                    break

            if not link:
                link_node = entry.find("atom:link", ns)

                if link_node is not None:
                    link = link_node.attrib.get("href", "")

            # SEC title often contains:
            # "8-K - Company Name (CIK) (Filer)"
            company = title

            match = re.search(
                r"^(?:8-K|6-K|S-1|S-3|424B4)\s*-\s*(.+?)\s+\(",
                title,
                flags=re.I
            )

            if match:
                company = match.group(1).strip()

            event = {
                "source": "SEC",
                "form": form,
                "title": title,
                "company": company,
                "description": summary,
                "datetime": dt,
                "link": link,
                "capital_hits": capital_hits,
                "beneficiary_hits": find_words(
                    combined,
                    BENEFICIARY_TERMS
                ),
                "tickers": find_tickers(combined),
            }

            events.append(event)

        except Exception as e:
            print(f"SEC ENTRY ERROR: {e}")

    return events


def sec_events():
    events = []

    print("================================")
    print("SEC CAPITAL-FLOW SCAN")
    print("================================")

    successful = 0
    failed = 0

    for form in SEC_FORMS:
        print(f"Checking SEC {form}")

        url = sec_feed_url(form)

        xml_text = fetch(
            url,
            retries=3,
            delay=1.2
        )

        if not xml_text:
            failed += 1
            continue

        successful += 1

        parsed = parse_sec_feed(
            xml_text,
            form
        )

        events.extend(parsed)

        time.sleep(SEC_DELAY)

    print(
        f"SEC feeds: {successful}/{len(SEC_FORMS)} available"
    )

    if failed:
        print(
            f"SEC feeds unavailable: {failed}"
        )

    print(
        f"SEC qualifying events: {len(events)}"
    )

    return events


# ============================================================
# NEWS
# ============================================================

def news_query():
    terms = [
        '"acquisition"',
        '"merger"',
        '"definitive agreement"',
        '"strategic investment"',
        '"funding"',
        '"financing"',
        '"capital expenditure"',
        '"capex"',
        '"data center"',
        '"AI infrastructure"',
        '"major contract"',
        '"supply agreement"',
        '"purchase agreement"',
        '"manufacturing facility"',
    ]

    return "(" + " OR ".join(terms) + ")"


def google_news_url():
    query = news_query()

    encoded = urllib.parse.quote(
        query + " when:1d"
    )

    return (
        "https://news.google.com/rss/search"
        f"?q={encoded}"
        "&hl=en-US"
        "&gl=US"
        "&ceid=US:en"
    )


def parse_news_rss(xml_text):
    events = []

    try:
        root = ET.fromstring(xml_text)
    except Exception as e:
        print(f"NEWS XML ERROR: {e}")
        return events

    for item in root.findall(".//item"):
        try:
            title = clean_text(
                item.findtext("title", "")
            )

            description = clean_text(
                item.findtext("description", "")
            )

            pubdate = clean_text(
                item.findtext("pubDate", "")
            )

            link = clean_text(
                item.findtext("link", "")
            )

            dt = parse_date(pubdate)

            if not fresh(dt):
                continue

            combined = f"{title} {description}"

            capital_hits = find_words(
                combined,
                CAPITAL_TERMS
            )

            if not capital_hits:
                continue

            beneficiary_hits = find_words(
                combined,
                BENEFICIARY_TERMS
            )

            high_value_hits = find_words(
                combined,
                HIGH_VALUE_TERMS
            )

            events.append({
                "source": "NEWS",
                "form": "News",
                "title": title,
                "company": "",
                "description": description,
                "datetime": dt,
                "link": link,
                "capital_hits": capital_hits,
                "beneficiary_hits": beneficiary_hits,
                "high_value_hits": high_value_hits,
                "tickers": find_tickers(combined),
            })

        except Exception as e:
            print(f"NEWS ENTRY ERROR: {e}")

    return events


def news_events():
    print("================================")
    print("NEWS CAPITAL-FLOW SCAN")
    print("================================")

    # IMPORTANT:
    # We intentionally avoid the previous five-request GDELT
    # pattern that produced repeated HTTP 429 errors.
    #
    # Google News RSS is used as the primary broad announcement
    # layer. It requires no API key and gives publication timestamps.

    url = google_news_url()

    xml_text = fetch(
        url,
        retries=3,
        delay=2
    )

    if not xml_text:
        print(
            "NEWS SOURCE UNAVAILABLE — "
            "this is NOT counted as zero events."
        )

        return [], False

    events = parse_news_rss(xml_text)

    print(
        f"News qualifying events: {len(events)}"
    )

    return events, True


# ============================================================
# BENEFICIARY ANALYSIS
# ============================================================

def beneficiary_analysis(event):
    text = (
        event.get("title", "")
        + " "
        + event.get("description", "")
    )

    low = text.lower()

    hits = find_words(
        text,
        BENEFICIARY_TERMS
    )

    strength = 0

    if hits:
        strength += 15

    if any(
        x in low
        for x in [
            "supplier",
            "supplies",
            "equipment",
            "vendor",
            "contractor",
            "technology provider",
            "service provider",
        ]
    ):
        strength += 15

    if any(
        x in low
        for x in [
            "will provide",
            "will supply",
            "selected",
            "awarded",
            "contracted",
            "agreement with",
        ]
    ):
        strength += 20

    strength = min(50, strength)

    # Try to identify a named beneficiary from phrases such as:
    # "provided by XYZ"
    # "contract with XYZ"
    # "partnered with XYZ"

    beneficiary = ""

    patterns = [
        r"(?:provided by|supplied by|provided through|contract with|agreement with|partnered with)\s+([A-Z][A-Za-z0-9&.,' -]{2,60})",
        r"(?:supplier|vendor|contractor|provider)\s+([A-Z][A-Za-z0-9&.,' -]{2,60})",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text
        )

        if match:
            beneficiary = match.group(1).strip()
            beneficiary = re.split(
                r"\s+(?:for|to|as|that|which|and)\s+",
                beneficiary,
                maxsplit=1,
                flags=re.I
            )[0].strip()

            break

    return {
        "name": beneficiary,
        "hits": hits,
        "strength": strength,
    }


# ============================================================
# REPRICING
# ============================================================

def yahoo_chart(ticker):
    try:
        period1 = int(
            (utc_now() - timedelta(days=7)).timestamp()
        )

        period2 = int(
            utc_now().timestamp()
        )

        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{urllib.parse.quote(ticker)}"
            f"?period1={period1}"
            f"&period2={period2}"
            "&interval=1d"
            "&events=history"
        )

        text = fetch(
            url,
            retries=2,
            delay=1
        )

        if not text:
            return None

        data = json.loads(text)

        result = (
            data
            .get("chart", {})
            .get("result", [])
        )

        if not result:
            return None

        result = result[0]

        closes = (
            result
            .get("indicators", {})
            .get("quote", [{}])[0]
            .get("close", [])
        )

        closes = [
            float(x)
            for x in closes
            if x is not None
        ]

        if len(closes) < 2:
            return None

        previous = closes[-2]
        current = closes[-1]

        if previous == 0:
            return None

        move = (
            (current - previous)
            / previous
        ) * 100

        return {
            "current": current,
            "previous": previous,
            "move": move,
        }

    except Exception:
        return None


def repricing(tickers):
    if not tickers:
        return {
            "ticker": "",
            "move": None,
        }

    for ticker in tickers[:5]:
        result = yahoo_chart(ticker)

        if result:
            return {
                "ticker": ticker,
                "move": result["move"],
            }

    return {
        "ticker": tickers[0],
        "move": None,
    }


# ============================================================
# FIRST-MOVER WINDOW
# ============================================================

def first_mover(event):
    dt = event.get("datetime")

    if not dt:
        return "UNKNOWN"

    hours = age_hours(dt)

    if hours <= 2:
        return "VERY EARLY — <2 HOURS"

    if hours <= 6:
        return "EARLY — <6 HOURS"

    if hours <= 12:
        return "ACTIVE — <12 HOURS"

    if hours <= 24:
        return "LATE — <24 HOURS"

    return "EXPIRED"


# ============================================================
# SCORE
# ============================================================

def calculate_score(event):
    score = 20

    text = (
        event.get("title", "")
        + " "
        + event.get("description", "")
    )

    value = money_value(text)

    # Capital size.
    if value >= 10_000_000_000:
        score += 30
    elif value >= 5_000_000_000:
        score += 27
    elif value >= 1_000_000_000:
        score += 24
    elif value >= 500_000_000:
        score += 19
    elif value >= 100_000_000:
        score += 13
    elif value > 0:
        score += 7

    # Strategic / capital-flow language.
    high_hits = find_words(
        text,
        HIGH_VALUE_TERMS
    )

    score += min(15, len(high_hits) * 3)

    beneficiary = beneficiary_analysis(event)

    score += beneficiary["strength"]

    repricing_data = event.get("repricing", {})

    move = repricing_data.get("move")

    if move is not None:
        absolute_move = abs(move)

        if absolute_move < 2:
            score += 10
        elif absolute_move < 5:
            score += 7
        elif absolute_move < 10:
            score += 3
        else:
            score -= 3

    return max(0, min(100, score))


# ============================================================
# ENTRY / EXIT
# ============================================================

def entry_plan(event):
    move = (
        event
        .get("repricing", {})
        .get("move")
    )

    if move is None:
        return (
            "WAIT FOR PRICE CONFIRMATION. "
            "Do not chase until the ticker and catalyst are verified."
        )

    if abs(move) < 3:
        return (
            "EARLY ENTRY ZONE: monitor the first reaction; "
            "consider a staged entry only after ticker/catalyst verification."
        )

    if abs(move) < 7:
        return (
            "PARTIALLY REPRICED: avoid chasing the initial move; "
            "look for a controlled pullback/retest."
        )

    return (
        "HEAVILY REPRICED: do not chase the spike; "
        "wait for a pullback or confirmation."
    )


def exit_plan(event):
    move = (
        event
        .get("repricing", {})
        .get("move")
    )

    if move is not None and abs(move) >= 7:
        return (
            "Take partial profit after material repricing. "
            "Protect gains with a trailing/technical stop. "
            "Exit if the catalyst fades or the thesis breaks."
        )

    return (
        "Take partial profit after material repricing. "
        "Exit if the catalyst fades, guidance weakens, "
        "or the original capital-flow thesis breaks."
    )


# ============================================================
# BAMBOO
# ============================================================

def bamboo_status(tickers):
    if not tickers:
        return "CHECK REQUIRED — no verified ticker extracted."

    return (
        "CHECK REQUIRED — verify live ticker/order availability "
        "in Bamboo before entering."
    )


# ============================================================
# EVENT KEY
# ============================================================

def event_key(event):
    source = event.get("source", "")
    form = event.get("form", "")
    title = event.get("title", "")
    link = event.get("link", "")
    dt = event.get("datetime")

    date_text = ""

    if dt:
        date_text = dt.isoformat()

    raw = (
        f"{source}|{form}|{title}|{link}|{date_text}"
    )

    import hashlib

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


# ============================================================
# ALERT FORMAT
# ============================================================

def format_alert(event):
    dt = event.get("datetime")

    timestamp = (
        dt.strftime("%Y-%m-%d %H:%M UTC")
        if dt
        else "Unknown"
    )

    value = money_value(
        event.get("title", "")
        + " "
        + event.get("description", "")
    )

    beneficiary = beneficiary_analysis(event)

    score = event.get("score", 0)

    repricing_data = event.get(
        "repricing",
        {}
    )

    ticker = repricing_data.get(
        "ticker",
        ""
    )

    move = repricing_data.get(
        "move"
    )

    if move is None:
        repricing_text = (
            "PRICE DATA UNAVAILABLE"
        )
    else:
        repricing_text = (
            f"{ticker}: {move:+.2f}% latest daily move"
        )

    link = event.get(
        "link",
        ""
    )

    alert = []

    alert.append("🚨 CAPITAL FLOW RADAR")
    alert.append("")
    alert.append(
        f"{event.get('source', 'EVENT')} "
        f"— Alpha Score {score}/100"
    )

    alert.append("")
    alert.append("📌 EVENT")
    alert.append(
        event.get(
            "title",
            "Untitled event"
        )
    )

    alert.append(
        f"🕐 {timestamp}"
    )

    alert.append(
        f"📄 Form: {event.get('form', 'News')}"
    )

    alert.append("")
    alert.append("💰 CAPITAL")

    if value:
        alert.append(
            f"Estimated disclosed amount: {money_label(value)}"
        )
    else:
        alert.append(
            "Amount: Not clearly disclosed"
        )

    hits = event.get(
        "capital_hits",
        []
    )

    if hits:
        alert.append(
            "Signals: " + ", ".join(hits[:8])
        )

    alert.append("")
    alert.append("🎯 BENEFICIARY")

    if beneficiary["name"]:
        alert.append(
            f"Potential beneficiary: {beneficiary['name']}"
        )
    else:
        alert.append(
            "No specific supplier/beneficiary confidently identified."
        )

    if beneficiary["hits"]:
        alert.append(
            "Evidence: "
            + ", ".join(
                beneficiary["hits"][:6]
            )
        )

    alert.append("")
    alert.append("📈 REPRICING")
    alert.append(repricing_text)

    alert.append("")
    alert.append("⚡ FIRST-MOVER WINDOW")
    alert.append(first_mover(event))

    alert.append("")
    alert.append("🟢 ENTRY")
    alert.append(entry_plan(event))

    alert.append("")
    alert.append("🔴 EXIT")
    alert.append(exit_plan(event))

    alert.append("")
    alert.append("🇳🇬 BAMBOO")
    alert.append(
        bamboo_status(
            event.get("tickers", [])
        )
    )

    if event.get("tickers"):
        alert.append(
            "Ticker candidates: "
            + ", ".join(event["tickers"][:8])
        )

    if link:
        alert.append("")
        alert.append("🔗 SOURCE")
        alert.append(link)

    return "\n".join(alert)


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(
            "TELEGRAM NOT CONFIGURED — "
            "alert was not sent."
        )
        return False

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_BOT_TOKEN
        + "/sendMessage"
    )

    payload = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": "true",
    }).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "User-Agent": "Capital Flow Radar",
            "Content-Type":
                "application/x-www-form-urlencoded",
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=15
        ) as response:

            body = response.read().decode(
                "utf-8",
                errors="replace"
            )

            data = json.loads(body)

            if data.get("ok"):
                print("Telegram alert sent.")
                return True

            print(
                "TELEGRAM ERROR:",
                data
            )

    except Exception as e:
        print(
            f"TELEGRAM SEND ERROR: {e}"
        )

    return False


# ============================================================
# DEDUPLICATION
# ============================================================

def deduplicate(events):
    unique = {}
    for event in events:
        key = event_key(event)

        if key not in unique:
            unique[key] = event

    return list(unique.values())


# ============================================================
# MAIN ANALYSIS
# ============================================================

def enrich_event(event):
    # Merge all relevant text.
    text = (
        event.get("title", "")
        + " "
        + event.get("description", "")
    )

    event["capital_hits"] = find_words(
        text,
        CAPITAL_TERMS
    )

    event["beneficiary_hits"] = find_words(
        text,
        BENEFICIARY_TERMS
    )

    event["tickers"] = list(
        dict.fromkeys(
            event.get("tickers", [])
            + find_tickers(text)
        )
    )

    event["repricing"] = repricing(
        event["tickers"]
    )

    event["score"] = calculate_score(
        event
    )

    return event


# ============================================================
# MAIN
# ============================================================

def main():
    print("================================")
    print("STARTING CAPITAL FLOW RADAR")
    print("================================")

    state = load_state()

    seen = set(
        state.get(
            "seen",
            []
        )
    )

    sec = sec_events()

    news, news_available = news_events()

    raw_events = deduplicate(
        sec + news
    )

    print(
        f"Raw qualifying events: {len(raw_events)}"
    )

    print(
        f"Previously seen events: {len(seen)}"
    )

    # Only events inside the strict 24-hour window.
    fresh_events = []

    for event in raw_events:
        if fresh(event.get("datetime")):
            fresh_events.append(event)

    # New only.
    new_events = []

    for event in fresh_events:
        key = event_key(event)

        if key not in seen:
            new_events.append(event)

    print(
        f"New alerts to analyze: {len(new_events)}"
    )

    # Analyze only new events.
    analyzed = []

    for event in new_events:
        try:
            analyzed.append(
                enrich_event(event)
            )
        except Exception as e:
            print(
                f"EVENT ANALYSIS ERROR: {e}"
            )

    analyzed.sort(
        key=lambda x: x.get(
            "score",
            0
        ),
        reverse=True
    )

    # Send alerts.
    sent = 0

    for event in analyzed[:MAX_ALERTS_PER_RUN]:
        try:
            message = format_alert(
                event
            )

            if send_telegram(message):
                sent += 1

        except Exception as e:
            print(
                f"ALERT ERROR: {e}"
            )

    # IMPORTANT:
    # Mark all analyzed events as seen even if Telegram
    # temporarily fails, preventing duplicate floods.
    #
    # Events not successfully retrieved because a source
    # failed are NOT marked as seen.

    for event in fresh_events:
        seen.add(
            event_key(event)
        )

    state["seen"] = list(seen)[-2000:]

    save_state(state)

    print("")
    print("================================")
    print("RADAR STATUS")
    print("================================")

    print(
        f"SEC events: {len(sec)}"
    )

    print(
        f"News events: {len(news)}"
    )

    print(
        f"News source available: "
        f"{'YES' if news_available else 'NO'}"
    )

    print(
        f"Fresh events: {len(fresh_events)}"
    )

    print(
        f"New alerts: {len(new_events)}"
    )

    print(
        f"Telegram alerts sent: {sent}"
    )

    if not news_available:
        print(
            "WARNING: News source unavailable. "
            "Zero news events must NOT be interpreted "
            "as evidence that no news events exist."
        )

    print("")
    print(
        "Radar completed successfully."
    )


if __name__ == "__main__":
    main()
