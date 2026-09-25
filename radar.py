import os
import json
import re
import time
import gzip
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta


UA = "Capital Flow Radar veronicajyde@gmail.com"
STATE_FILE = "radar_state.json"


CAPITAL_WORDS = [
    "acquisition",
    "merger",
    "definitive agreement",
    "investment",
    "funding",
    "financing",
    "offering",
    "private placement",
    "capital expenditure",
    "capex",
    "infrastructure",
    "data center",
    "datacenter",
    "ai infrastructure",
    "artificial intelligence",
    "cloud",
    "factory",
    "manufacturing",
    "contract",
    "purchase agreement",
    "strategic investment",
]


BENEFICIARY_WORDS = [
    "supplier",
    "supplies",
    "provider",
    "customer",
    "partner",
    "partnership",
    "contractor",
    "equipment",
    "vendor",
    "manufacturer",
    "service provider",
]


MONEY_RE = re.compile(
    r"\$?\s*([0-9]+(?:\.[0-9]+)?)\s*"
    r"(trillion|billion|million|bn|mm|m|b)",
    re.I,
)


def fetch(url, timeout=20, retries=3):
    headers = {
        "User-Agent": UA,
        "Accept-Encoding": "gzip",
        "Accept": "*/*",
    }

    for attempt in range(retries):
        try:
            request = urllib.request.Request(
                url,
                headers=headers,
            )

            with urllib.request.urlopen(
                request,
                timeout=timeout,
            ) as response:

                data = response.read()

                if response.headers.get(
                    "Content-Encoding"
                ) == "gzip":
                    data = gzip.decompress(data)

                return data.decode(
                    "utf-8",
                    errors="ignore",
                )

        except Exception as error:
            print("FETCH ERROR:", error)

            if attempt < retries - 1:
                time.sleep(2 ** attempt)

    return ""


def parse_date(value):
    if not value:
        return None

    value = value.strip()

    formats = [
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y%m%d%H%M%S",
    ]

    for fmt in formats:
        try:
            result = datetime.strptime(
                value,
                fmt,
            )

            if result.tzinfo is None:
                result = result.replace(
                    tzinfo=timezone.utc,
                )

            return result.astimezone(
                timezone.utc,
            )

        except Exception:
            pass

    return None


def fresh(dt):
    if not dt:
        return False

    return (
        datetime.now(timezone.utc) - dt
        <= timedelta(hours=24)
    )


def find_words(text, words):
    text = (text or "").lower()

    return [
        word
        for word in words
        if word.lower() in text
    ]


def money_value(text):
    match = MONEY_RE.search(text or "")

    if not match:
        return 0

    number = float(match.group(1))
    unit = match.group(2).lower()

    if unit == "trillion":
        return number * 1_000_000_000_000

    if unit in ("billion", "bn", "b"):
        return number * 1_000_000_000

    return number * 1_000_000


def money_label(value):
    if value >= 1_000_000_000_000:
        return f"${value / 1_000_000_000_000:.1f}T"

    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}B"

    if value >= 1_000_000:
        return f"${value / 1_000_000:.0f}M"

    return "Undisclosed"


def find_tickers(text):
    found = set()

    for ticker in re.findall(
        r"\(([A-Z]{1,5})\)",
        text or "",
    ):
        found.add(ticker)

    for ticker in re.findall(
        r"(?:NASDAQ|NYSE|AMEX|NYSEAMERICAN)"
        r"[:\s]+([A-Z]{1,5})",
        text or "",
        re.I,
    ):
        found.add(ticker.upper())

    return sorted(found)


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"seen": []}

    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)

        if isinstance(data, list):
            return {"seen": data}

        if isinstance(data, dict):
            data.setdefault("seen", [])
            return data

    except Exception as error:
        print("STATE ERROR:", error)

    return {"seen": []}


def save_state(state):
    with open(
        STATE_FILE,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            state,
            file,
            indent=2,
        )


def sec_events():
    feeds = {
        "8-K":
            "https://www.sec.gov/cgi-bin/"
            "browse-edgar?action=getcurrent"
            "&type=8-K&owner=exclude&count=100",

        "6-K":
            "https://www.sec.gov/cgi-bin/"
            "browse-edgar?action=getcurrent"
            "&type=6-K&owner=exclude&count=100",

        "S-1":
            "https://www.sec.gov/cgi-bin/"
            "browse-edgar?action=getcurrent"
            "&type=S-1&owner=exclude&count=100",

        "S-3":
            "https://www.sec.gov/cgi-bin/"
            "browse-edgar?action=getcurrent"
            "&type=S-3&owner=exclude&count=100",

        "424B4":
            "https://www.sec.gov/cgi-bin/"
            "browse-edgar?action=getcurrent"
            "&type=424B4&owner=exclude&count=100",
    }

    events = []

    for form, url in feeds.items():

        print("Checking SEC", form)

        raw = fetch(
            url,
            timeout=25,
            retries=2,
        )

        if not raw:
            continue

        entries = re.findall(
            r"<entry>(.*?)</entry>",
            raw,
            re.S | re.I,
        )

        for entry in entries:

            title_match = re.search(
                r"<title>(.*?)</title>",
                entry,
                re.S | re.I,
            )

            updated_match = re.search(
                r"<updated>(.*?)</updated>",
                entry,
                re.S | re.I,
            )

            id_match = re.search(
                r"<id>(.*?)</id>",
                entry,
                re.S | re.I,
            )

            if not title_match:
                continue

            title = re.sub(
                r"<.*?>",
                " ",
                title_match.group(1),
            ).strip()

            updated = (
                updated_match.group(1).strip()
                if updated_match
                else ""
            )

            dt = parse_date(updated)

            if not fresh(dt):
                continue

            capital_hits = find_words(
                title,
                CAPITAL_WORDS,
            )

            if not capital_hits:
                continue

            identifier = (
                id_match.group(1).strip()
                if id_match
                else title
            )

            events.append({
                "source": "SEC",
                "form": form,
                "title": title,
                "url": identifier,
                "date": (
                    dt.isoformat()
                    if dt
                    else ""
                ),
                "capital": money_value(title),
                "capital_hits": capital_hits,
                "tickers": find_tickers(title),
                "beneficiary": "POSSIBLE",
            })

    print(
        "SEC qualifying events:",
        len(events),
    )

    return events


def news_events():
    current = datetime.now(timezone.utc)

    github_event = os.environ.get(
        "GITHUB_EVENT_NAME",
        "",
    )

    if (
        github_event != "workflow_dispatch"
        and current.minute % 15 != 0
    ):
        print("GDELT skipped this cycle.")
        return []

    query = (
        '("billion" OR "million" OR '
        '"investment" OR "acquisition" OR '
        '"contract" OR "capital expenditure" OR '
        '"data center" OR "AI infrastructure") '
        '("supplier" OR "provider" OR '
        '"contractor" OR "customer" OR '
        '"partnership" OR "equipment")'
    )

    url = (
        "https://api.gdeltproject.org/api/v2/doc/doc"
        "?query="
        + urllib.parse.quote(query)
        + "&mode=artlist"
        + "&maxrecords=50"
        + "&format=json"
        + "&timespan=24h"
        + "&sort=datedesc"
    )

    print("Checking GDELT news...")

    raw = fetch(
        url,
        timeout=20,
        retries=2,
    )

    if not raw:
        return []

    try:
        data = json.loads(raw)
    except Exception as error:
        print("GDELT JSON ERROR:", error)
        return []

    events = []

    for article in data.get(
        "articles",
        [],
    ):

        title = article.get(
            "title",
            "",
        ).strip()

        link = article.get(
            "url",
            "",
        ).strip()

        if not title:
            continue

        dt = parse_date(
            article.get(
                "seendate",
                "",
            )
        )

        if not fresh(dt):
            continue

        capital_hits = find_words(
            title,
            CAPITAL_WORDS,
        )

        beneficiary_hits = find_words(
            title,
            BENEFICIARY_WORDS,
        )

        if not capital_hits:
            continue

        if len(beneficiary_hits) >= 3:
            beneficiary = "DIRECT"
        elif len(beneficiary_hits) >= 2:
            beneficiary = "STRONG"
        elif len(beneficiary_hits) >= 1:
            beneficiary = "POSSIBLE"
        else:
            continue

        events.append({
            "source": "NEWS",
            "form": "NEWS",
            "title": title,
            "url": link,
            "date": (
                dt.isoformat()
                if dt
                else ""
            ),
            "capital": money_value(title),
            "capital_hits": capital_hits,
            "tickers": find_tickers(title),
            "beneficiary": beneficiary,
        })

    print(
        "News qualifying events:",
        len(events),
    )

    return events


def repricing(ticker):
    if not ticker:
        return None

    url = (
        "https://query1.finance.yahoo.com/"
        "v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?range=5d&interval=1d"
    )

    raw = fetch(
        url,
        timeout=10,
        retries=1,
    )

    if not raw:
        return None

    try:
        data = json.loads(raw)

        closes = (
            data["chart"]["result"][0]
            ["indicators"]["quote"][0]
            ["close"]
        )

        closes = [
            x for x in closes
            if x is not None
        ]

        if len(closes) < 2:
            return None

        return (
            closes[-1] / closes[-2] - 1
        ) * 100

    except Exception:
        return None


def calculate_score(event):

    capital = event.get(
        "capital",
        0,
    )

    beneficiary = event.get(
        "beneficiary",
        "POSSIBLE",
    )

    move = event.get(
        "move",
    )

    score = 0

    if capital >= 10_000_000_000:
        score += 35
    elif capital >= 1_000_000_000:
        score += 28
    elif capital >= 250_000_000:
        score += 20
    elif capital > 0:
        score += 10

    if beneficiary == "DIRECT":
        score += 30
    elif beneficiary == "STRONG":
        score += 22
    elif beneficiary == "POSSIBLE":
        score += 12

    if move is None:
        score += 20
    elif move < 3:
        score += 20
    elif move < 8:
        score += 10

    if score >= 75:
        label = "HIGH SIGNAL"
    elif score >= 55:
        label = "STRONG SIGNAL"
    elif score >= 35:
        label = "WATCH"
    else:
        label = "LOWER SIGNAL"

    return score, label


def send_telegram(message):

    token = os.environ.get(
        "TELEGRAM_BOT_TOKEN",
    )

    chat_id = os.environ.get(
        "TELEGRAM_CHAT_ID",
    )

    if not token or not chat_id:
        print("Telegram secrets missing.")
        return

    url = (
        "https://api.telegram.org/bot"
        + token
        + "/sendMessage"
    )

    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": "false",
    }).encode()

    try:
        request = urllib.request.Request(
            url,
            data=payload,
            method="POST",
        )

        urllib.request.urlopen(
            request,
            timeout=15,
        )

        print("Telegram alert sent.")

    except Exception as error:
        print("TELEGRAM ERROR:", error)


def format_alert(event):

    ticker = (
        event["tickers"][0]
        if event["tickers"]
        else "NOT IDENTIFIED"
    )

    move = event.get("move")

    if move is None:
        repricing_text = "NOT VERIFIED"
        window = "OPEN / CHECK PRICE"
        entry = "CHECK LIVE PRICE BEFORE ENTRY"

    elif move >= 8:
        repricing_text = (
            f"ALREADY REPRICED (+{move:.1f}%)"
        )
        window = "NARROW — DO NOT CHASE"
        entry = "WAIT FOR PULLBACK / CONFIRMATION"

    elif move >= 3:
        repricing_text = (
            f"EARLY REPRICING (+{move:.1f}%)"
        )
        window = "NARROWING"
        entry = (
            "SMALL INITIAL POSITION / "
            "WATCH FOLLOW-THROUGH"
        )

    else:
        repricing_text = (
            f"LIMITED REPRICING ({move:+.1f}%)"
        )
        window = "OPEN / EARLY"
        entry = "EARLY-WINDOW CANDIDATE"

    return f"""🚨 CAPITAL FLOW RADAR

{event["label"]} — Alpha Score {event["score"]}/100

📌 EVENT
{event["title"]}

🏦 SOURCE
{event["source"]} / {event["form"]}

💰 CAPITAL
{money_label(event["capital"])}

🔎 CAPITAL SIGNALS
{", ".join(event["capital_hits"][:6])}

🎯 BENEFICIARY
{ticker}

📊 BENEFICIARY EVIDENCE
{event.get("beneficiary", "POSSIBLE")}

📈 REPRICING
{repricing_text}

⚡ FIRST-MOVER WINDOW
{window}

🟢 ENTRY
{entry}

🔴 EXIT
Take partial profit after material repricing.
Exit if the catalyst fades or the thesis breaks.

🇳🇬 BAMBOO
CHECK REQUIRED — verify live ticker/order availability.

🕐 ANNOUNCED
{event["date"]}

🔗 SOURCE
{event["url"]}

⚠️ Catalyst alert — not a guaranteed trade outcome."""


def main():

    print("================================")
    print("STARTING CAPITAL FLOW RADAR")
    print("================================")

    state = load_state()

    seen = set(
        state.get(
            "seen",
            [],
        )
    )

    events = sec_events()

    events.extend(
        news_events()
    )

    print(
        "Raw qualifying events:",
        len(events),
    )

    print(
        "Previously seen events:",
        len(seen),
    )

    for event in events:

        ticker = (
            event["tickers"][0]
            if event["tickers"]
            else None
        )

        event["move"] = repricing(
            ticker
        )

        (
            event["score"],
            event["label"],
        ) = calculate_score(event)

    events.sort(
        key=lambda event: event.get(
            "score",
            0,
        ),
        reverse=True,
    )

    new_events = []

    for event in events:

        key = (
            event["source"]
            + "|"
            + event["form"]
            + "|"
            + event["title"]
            + "|"
            + event["url"]
        ).lower()

        if key in seen:
            continue

        seen.add(key)
        new_events.append(event)

        if len(new_events) >= 5:
            break

    print(
        "New alerts to send:",
        len(new_events),
    )

    for event in new_events:

        message = format_alert(
            event
        )

        print("\n" + message + "\n")

        send_telegram(
            message
        )

    state["seen"] = list(seen)[-2000:]

    state["last_run"] = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    save_state(state)

    print("Radar completed successfully.")


if __name__ == "__main__":
    main()
