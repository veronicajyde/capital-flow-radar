import os
import json
import re
import time
import gzip
import hashlib
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, timedelta


# ============================================================
# CAPITAL FLOW RADAR — FINAL ENGINE
# ============================================================

UA = "Capital Flow Radar veronicajyde@gmail.com"

STATE_FILE = "radar_state.json"

LOOKBACK_HOURS = 24
MAX_ALERTS_PER_RUN = 5

REQUEST_TIMEOUT = 18

SEC_RETRIES = 4
SEC_BACKOFF = 1.5

NEWS_RETRIES = 3
NEWS_BACKOFF = 2.0

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SEC_FORMS = {
    "8-K",
    "6-K",
    "S-1",
    "S-3",
    "424B4",
    "424B5",
}

# ------------------------------------------------------------
# Terms that indicate actual capital deployment.
# ------------------------------------------------------------

CAPITAL_TERMS = [
    "acquisition",
    "acquire",
    "acquired",
    "merger",
    "definitive agreement",
    "strategic investment",
    "investment",
    "funding",
    "financing",
    "capital raise",
    "capital raising",
    "offering",
    "private placement",
    "registered direct",
    "secondary offering",
    "convertible",
    "credit facility",
    "debt financing",
    "capital expenditure",
    "capex",
    "infrastructure",
    "data center",
    "datacenter",
    "ai infrastructure",
    "artificial intelligence infrastructure",
    "cloud infrastructure",
    "factory",
    "manufacturing facility",
    "facility expansion",
    "capacity expansion",
    "construction",
    "purchase agreement",
    "supply agreement",
    "master services agreement",
    "major contract",
    "long-term contract",
    "strategic partnership",
    "expansion",
    "production expansion",
]

# ------------------------------------------------------------
# Beneficiary language.
# ------------------------------------------------------------

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

# ------------------------------------------------------------
# High-value signals.
# ------------------------------------------------------------

HIGH_VALUE_TERMS = [
    "billion",
    "multi-billion",
    "multibillion",
    "$1 billion",
    "$2 billion",
    "$5 billion",
    "$10 billion",
    "large-scale",
    "major contract",
    "long-term agreement",
    "definitive agreement",
    "data center",
    "ai infrastructure",
    "capital expenditure",
    "capacity expansion",
]

# ------------------------------------------------------------
# Entities we generally do NOT want as trading beneficiaries.
# ------------------------------------------------------------

EXCLUDED_ENTITY_TERMS = [
    "acquisition corp",
    "acquisition corporation",
    "blank check",
    "special purpose acquisition",
    "spac",
    "securitization trust",
    "auto securitization trust",
    "funding llc",
    "finance llc",
    "funding corporation",
    "trust 20",
    "trust 202",
    "warehouse trust",
    "receivables trust",
]

# ------------------------------------------------------------
# Filings that frequently contain administrative noise.
# ------------------------------------------------------------

LOW_SIGNAL_PHRASES = [
    "change in control",
    "director appointment",
    "officer appointment",
    "resignation",
    "annual meeting",
    "quarterly report",
    "financial statements",
    "amendment to articles",
    "bylaws",
    "restatement",
    "compensation",
]


# ============================================================
# TIME
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def parse_date(value):
    if not value:
        return None

    value = str(value).strip()

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
                dt = dt.replace(
                    tzinfo=timezone.utc
                )

            return dt.astimezone(
                timezone.utc
            )

        except Exception:
            pass

    try:
        dt = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )

        return dt.astimezone(
            timezone.utc
        )

    except Exception:
        return None


def is_fresh(dt):
    if not dt:
        return False

    age = now_utc() - dt

    return (
        timedelta(hours=0)
        <= age
        <= timedelta(hours=LOOKBACK_HOURS)
    )


def age_hours(dt):
    if not dt:
        return 999

    return max(
        0,
        (now_utc() - dt).total_seconds() / 3600
    )


# ============================================================
# HTTP
# ============================================================

def fetch(url, retries=3, backoff=1.5):
    headers = {
        "User-Agent": UA,
        "Accept-Encoding": "gzip",
        "Accept": "*/*",
        "Connection": "close",
    }

    last_error = None

    for attempt in range(retries):
        try:
            request = urllib.request.Request(
                url,
                headers=headers
            )

            with urllib.request.urlopen(
                request,
                timeout=REQUEST_TIMEOUT
            ) as response:

                raw = response.read()

                encoding = response.headers.get(
                    "Content-Encoding",
                    ""
                )

                if "gzip" in encoding.lower():
                    raw = gzip.decompress(raw)

                return raw.decode(
                    "utf-8",
                    errors="replace"
                )

        except urllib.error.HTTPError as e:

            last_error = (
                f"HTTP {e.code}: {e.reason}"
            )

            if e.code in {
                429,
                500,
                502,
                503,
                504,
            }:
                if attempt < retries - 1:
                    wait = (
                        backoff
                        * (2 ** attempt)
                    )

                    time.sleep(wait)
                    continue

            break

        except Exception as e:

            last_error = str(e)

            if attempt < retries - 1:
                wait = (
                    backoff
                    * (2 ** attempt)
                )

                time.sleep(wait)

    print(
        f"FETCH ERROR: {last_error}"
    )

    return None


# ============================================================
# TEXT
# ============================================================

def clean_text(value):
    if not value:
        return ""

    value = re.sub(
        r"<[^>]+>",
        " ",
        str(value)
    )

    value = (
        value
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
    )

    value = re.sub(
        r"\s+",
        " ",
        value
    )

    return value.strip()


def contains_any(text, terms):
    low = text.lower()

    return any(
        term.lower() in low
        for term in terms
    )


def find_terms(text, terms):
    low = text.lower()

    found = []

    for term in terms:
        if term.lower() in low:
            found.append(term)

    return list(
        dict.fromkeys(found)
    )


# ============================================================
# MONEY
# ============================================================

def money_value(text):
    if not text:
        return 0

    text = text.lower()
    text = text.replace(",", "")

    patterns = [
        r"\$\s*(\d+(?:\.\d+)?)\s*(billion|bn)",
        r"\$\s*(\d+(?:\.\d+)?)\s*(million|mn)",
        r"(\d+(?:\.\d+)?)\s*(billion|bn)\s*dollars",
        r"(\d+(?:\.\d+)?)\s*(million|mn)\s*dollars",
    ]

    best = 0

    for pattern in patterns:

        for match in re.finditer(
            pattern,
            text
        ):

            number = float(
                match.group(1)
            )

            unit = match.group(2)

            if unit in {
                "billion",
                "bn",
            }:
                value = (
                    number
                    * 1_000_000_000
                )

            else:
                value = (
                    number
                    * 1_000_000
                )

            best = max(
                best,
                value
            )

    return best


def money_label(value):
    if value >= 1_000_000_000:
        return (
            f"${value / 1_000_000_000:.2f}B"
        )

    if value >= 1_000_000:
        return (
            f"${value / 1_000_000:.2f}M"
        )

    if value > 0:
        return f"${value:,.0f}"

    return "Not disclosed"


# ============================================================
# SEC COMPANY / TICKER MAP
# ============================================================

def load_sec_ticker_map():
    print(
        "Loading SEC ticker/CIK map..."
    )

    urls = [
        "https://www.sec.gov/files/company_tickers.json",
        "https://www.sec.gov/files/company_tickers_exchange.json",
    ]

    mapping = {}

    for url in urls:

        data = fetch(
            url,
            retries=4,
            backoff=1.5
        )

        if not data:
            continue

        try:
            obj = json.loads(data)

            if isinstance(obj, dict):

                values = obj.values()

                for row in values:

                    if not isinstance(
                        row,
                        dict
                    ):
                        continue

                    cik = (
                        row.get("cik_str")
                        or row.get("cik")
                    )

                    ticker = (
                        row.get("ticker")
                        or row.get("symbol")
                    )

                    name = (
                        row.get(
                            "title"
                        )
                        or row.get(
                            "name"
                        )
                    )

                    if cik and ticker:

                        cik = str(cik).zfill(
                            10
                        )

                        mapping[cik] = {
                            "ticker": str(
                                ticker
                            ).upper(),
                            "name": (
                                name or ""
                            ),
                        }

        except Exception as e:
            print(
                f"TICKER MAP ERROR: {e}"
            )

    print(
        f"SEC ticker map entries: "
        f"{len(mapping)}"
    )

    return mapping


# ============================================================
# CIK
# ============================================================

def extract_cik(text):
    if not text:
        return ""

    # CIK frequently appears in:
    # (0002036973)
    match = re.search(
        r"\((\d{7,10})\)",
        text
    )

    if match:
        return match.group(1).zfill(
            10
        )

    return ""


# ============================================================
# SEC RSS
# ============================================================

def sec_feed_url(form):
    return (
        "https://www.sec.gov/cgi-bin/"
        "browse-edgar"
        "?action=getcurrent"
        f"&type={urllib.parse.quote(form)}"
        "&output=atom"
        "&count=100"
    )


def parse_sec_feed(
    xml_text,
    form,
    ticker_map
):
    events = []

    if not xml_text:
        return events

    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(
            xml_text
        )

    except Exception as e:
        print(
            f"SEC XML ERROR {form}: {e}"
        )

        return events

    ns = {
        "atom":
        "http://www.w3.org/2005/Atom"
    }

    entries = root.findall(
        "atom:entry",
        ns
    )

    for entry in entries:

        try:

            title = clean_text(
                entry.findtext(
                    "atom:title",
                    "",
                    ns
                )
            )

            updated = (
                entry.findtext(
                    "atom:updated",
                    "",
                    ns
                )
            )

            published = (
                entry.findtext(
                    "atom:published",
                    "",
                    ns
                )
            )

            summary = clean_text(
                entry.findtext(
                    "atom:summary",
                    "",
                    ns
                )
            )

            dt = (
                parse_date(updated)
                or parse_date(published)
            )

            if not is_fresh(dt):
                continue

            cik = extract_cik(
                title
            )

            company_info = (
                ticker_map.get(
                    cik,
                    {}
                )
            )

            ticker = (
                company_info
                .get("ticker", "")
            )

            company_name = (
                company_info
                .get("name", "")
            )

            if not company_name:

                match = re.search(
                    r"^(?:8-K|6-K|S-1|S-3|424B4)"
                    r"\s*-\s*(.+?)\s+\(",
                    title,
                    flags=re.I
                )

                if match:
                    company_name = (
                        match.group(1)
                        .strip()
                    )
                else:
                    company_name = title

            # We DO NOT treat the title itself as proof
            # of an actionable capital deployment.
            #
            # We collect the candidate here and perform
            # a stricter classification below.

            link = ""

            for link_node in entry.findall(
                "atom:link",
                ns
            ):

                href = link_node.attrib.get(
                    "href",
                    ""
                )

                if href:
                    link = href
                    break

            events.append({
                "source": "SEC",
                "form": form,
                "title": title,
                "company": company_name,
                "description": summary,
                "datetime": dt,
                "link": link,
                "cik": cik,
                "ticker": ticker,
                "capital_hits": [],
                "beneficiary_hits": [],
                "filing_text": "",
            })

        except Exception as e:

            print(
                f"SEC ENTRY ERROR: {e}"
            )

    return events


# ============================================================
# SEC FILING DOCUMENT
# ============================================================

def accession_from_link(link):
    if not link:
        return ""

    match = re.search(
        r"/(\d{10}-\d{2}-\d{6})",
        link
    )

    if match:
        return match.group(1)

    return ""


def filing_index_url(
    cik,
    accession
):
    if not cik or not accession:
        return ""

    accession_nodash = (
        accession.replace(
            "-",
            ""
        )
    )

    return (
        "https://www.sec.gov/Archives/"
        f"edgar/data/"
        f"{int(cik)}/"
        f"{accession_nodash}/"
        f"{accession}-index.html"
    )


def get_primary_document(
    event
):
    """
    Uses the SEC filing index page to discover
    the primary document.

    This is deliberately done only for candidates,
    rather than crawling every filing.
    """

    cik = event.get(
        "cik",
        ""
    )

    link = event.get(
        "link",
        ""
    )

    accession = accession_from_link(
        link
    )

    if not cik or not accession:
        return ""

    index_url = filing_index_url(
        cik,
        accession
    )

    if not index_url:
        return ""

    html_text = fetch(
        index_url,
        retries=3,
        backoff=1.5
    )

    if not html_text:
        return ""

    # Look for document hrefs.
    matches = re.findall(
        r'href="([^"]+)"',
        html_text,
        flags=re.I
    )

    candidates = []

    for href in matches:

        href = (
            href
            .replace("&amp;", "&")
        )

        low = href.lower()

        if any(
            low.endswith(ext)
            for ext in [
                ".htm",
                ".html",
                ".txt",
            ]
        ):
            candidates.append(
                href
            )

    if not candidates:
        return ""

    # Prefer htm/html over raw submission text.
    candidates.sort(
        key=lambda x: (
            0
            if x.lower().endswith(
                (".htm", ".html")
            )
            else 1,
            len(x),
        )
    )

    chosen = candidates[0]

    if chosen.startswith(
        "http://"
    ) or chosen.startswith(
        "https://"
    ):
        return chosen

    if chosen.startswith("/"):
        return (
            "https://www.sec.gov"
            + chosen
        )

    # Relative path.
    base = index_url.rsplit(
        "/",
        1
    )[0]

    return (
        base
        + "/"
        + chosen
    )


def filing_text(event):
    document_url = get_primary_document(
        event
    )

    if not document_url:
        return ""

    text = fetch(
        document_url,
        retries=3,
        backoff=1.5
    )

    if not text:
        return ""

    text = clean_text(
        text
    )

    # Prevent gigantic messages / memory usage.
    return text[:500_000]


# ============================================================
# EVENT CLASSIFICATION
# ============================================================

def is_excluded_entity(event):
    text = (
        event.get("company", "")
        + " "
        + event.get("title", "")
    ).lower()

    return any(
        term in text
        for term in EXCLUDED_ENTITY_TERMS
    )


def classify_event(event):
    """
    Returns:
        qualifying: bool
        category: str
        confidence: int
        reason: str
    """

    title = event.get(
        "title",
        ""
    )

    company = event.get(
        "company",
        ""
    )

    filing = event.get(
        "filing_text",
        ""
    )

    combined = (
        title
        + " "
        + company
        + " "
        + filing
    )

    low = combined.lower()

    capital_hits = find_terms(
        combined,
        CAPITAL_TERMS
    )

    beneficiary_hits = find_terms(
        combined,
        BENEFICIARY_TERMS
    )

    value = money_value(
        combined
    )

    event["capital_hits"] = (
        capital_hits
    )

    event["beneficiary_hits"] = (
        beneficiary_hits
    )

    event["capital_value"] = value

    # --------------------------------------------------------
    # Hard exclusion.
    # --------------------------------------------------------

    if is_excluded_entity(
        event
    ):
        return (
            False,
            "EXCLUDED ENTITY",
            0,
            "SPAC/securitization/financing entity"
        )

    # --------------------------------------------------------
    # Ignore obvious administrative noise.
    # --------------------------------------------------------

    if (
        len(capital_hits) == 0
        and contains_any(
            combined,
            LOW_SIGNAL_PHRASES
        )
    ):
        return (
            False,
            "ADMINISTRATIVE",
            0,
            "No capital-flow evidence"
        )

    # --------------------------------------------------------
    # Major acquisition / merger.
    # --------------------------------------------------------

    if any(
        x in low
        for x in [
            "acquisition",
            "acquire",
            "acquired",
            "merger",
        ]
    ):

        if (
            value >= 100_000_000
            or "definitive agreement" in low
            or "purchase agreement" in low
        ):

            return (
                True,
                "M&A / CAPITAL DEPLOYMENT",
                90,
                "Material acquisition/transaction"
            )

    # --------------------------------------------------------
    # Major contract / supply agreement.
    # --------------------------------------------------------

    if any(
        x in low
        for x in [
            "major contract",
            "long-term contract",
            "supply agreement",
            "purchase agreement",
            "master services agreement",
        ]
    ):

        if (
            value >= 100_000_000
            or "billion" in low
            or "million" in low
        ):

            return (
                True,
                "MAJOR CONTRACT",
                88,
                "Material commercial agreement"
            )

    # --------------------------------------------------------
    # AI / data-center / infrastructure.
    # --------------------------------------------------------

    infrastructure = any(
        x in low
        for x in [
            "data center",
            "datacenter",
            "ai infrastructure",
            "artificial intelligence infrastructure",
            "cloud infrastructure",
            "infrastructure expansion",
        ]
    )

    if infrastructure:

        if (
            value >= 50_000_000
            or "billion" in low
            or "multi-billion" in low
        ):

            return (
                True,
                "AI / INFRASTRUCTURE CAPEX",
                92,
                "Material infrastructure capital deployment"
            )

    # --------------------------------------------------------
    # Factory / manufacturing expansion.
    # --------------------------------------------------------

    manufacturing = any(
        x in low
        for x in [
            "manufacturing facility",
            "factory",
            "production expansion",
            "capacity expansion",
            "new facility",
        ]
    )

    if manufacturing:

        if (
            value >= 50_000_000
            or "billion" in low
        ):

            return (
                True,
                "MANUFACTURING CAPEX",
                86,
                "Material capacity/facility expansion"
            )

    # --------------------------------------------------------
    # Strategic investment.
    # --------------------------------------------------------

    if (
        "strategic investment" in low
        or "strategic partnership" in low
    ):

        if (
            value >= 50_000_000
            or "billion" in low
        ):

            return (
                True,
                "STRATEGIC CAPITAL",
                82,
                "Material strategic investment"
            )

    # --------------------------------------------------------
    # Financing / offering.
    # --------------------------------------------------------

    financing = any(
        x in low
        for x in [
            "financing",
            "funding",
            "offering",
            "private placement",
            "registered direct",
            "credit facility",
            "debt financing",
        ]
    )

    if financing:

        # A financing becomes interesting only when
        # the use of proceeds indicates actual expansion,
        # acquisition, infrastructure, or production.
        deployment = any(
            x in low
            for x in [
                "use of proceeds",
                "acquisition",
                "capital expenditure",
                "capex",
                "expansion",
                "data center",
                "infrastructure",
                "manufacturing",
                "production",
            ]
        )

        if (
            deployment
            and value >= 50_000_000
        ):

            return (
                True,
                "CAPITAL FINANCING",
                78,
                "Large financing linked to deployment"
            )

    # --------------------------------------------------------
    # No strong capital-flow thesis.
    # --------------------------------------------------------

    return (
        False,
        "LOW SIGNAL",
        0,
        "No sufficiently material actionable capital deployment"
    )


# ============================================================
# SEC EVENTS
# ============================================================

def sec_events(ticker_map):
    print("")
    print("================================")
    print("SEC CAPITAL-FLOW SCAN")
    print("================================")

    events = []

    successful = 0
    failed = 0

    for form in SEC_FORMS:

        print(
            f"Checking SEC {form}"
        )

        xml_text = fetch(
            sec_feed_url(form),
            retries=SEC_RETRIES,
            backoff=SEC_BACKOFF
        )

        if not xml_text:

            failed += 1
            continue

        successful += 1

        candidates = parse_sec_feed(
            xml_text,
            form,
            ticker_map
        )

        print(
            f"{form}: "
            f"{len(candidates)} fresh candidates"
        )

        # We don't download every filing.
        # We first eliminate obvious SPAC / trust /
        # administrative entities.
        candidates = [
            e
            for e in candidates
            if not is_excluded_entity(e)
        ]

        # Examine only a reasonable number per form.
        # The newest candidates are already first in SEC feeds.
        candidates = candidates[:30]

        for event in candidates:

            try:

                # Fetch filing body so that
                # "acquisition" alone isn't enough.
                body = filing_text(
                    event
                )

                event["filing_text"] = body

                qualifying, category, confidence, reason = (
                    classify_event(
                        event
                    )
                )

                if qualifying:

                    event["category"] = (
                        category
                    )

                    event["classification_confidence"] = (
                        confidence
                    )

                    event["classification_reason"] = (
                        reason
                    )

                    events.append(
                        event
                    )

            except Exception as e:

                print(
                    f"SEC CANDIDATE ERROR: {e}"
                )

            time.sleep(
                0.35
            )

    print(
        f"SEC feeds available: "
        f"{successful}/{len(SEC_FORMS)}"
    )

    print(
        f"SEC qualifying events: "
        f"{len(events)}"
    )

    return events


# ============================================================
# NEWS
# ============================================================

def google_news_url():
    query = (
        '"acquisition" OR '
        '"merger" OR '
        '"major contract" OR '
        '"supply agreement" OR '
        '"data center" OR '
        '"AI infrastructure" OR '
        '"capital expenditure" OR '
        '"factory expansion" OR '
        '"strategic investment"'
        ' when:1d'
    )

    return (
        "https://news.google.com/rss/search?"
        + urllib.parse.urlencode({
            "q": query,
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        })
    )


def news_events():
    print("")
    print("================================")
    print("NEWS CAPITAL-FLOW SCAN")
    print("================================")

    url = google_news_url()

    xml_text = fetch(
        url,
        retries=NEWS_RETRIES,
        backoff=NEWS_BACKOFF
    )

    if not xml_text:

        print(
            "NEWS SOURCE UNAVAILABLE"
        )

        return [], False

    try:

        import xml.etree.ElementTree as ET

        root = ET.fromstring(
            xml_text
        )

    except Exception as e:

        print(
            f"NEWS XML ERROR: {e}"
        )

        return [], False

    events = []

    for item in root.findall(
        ".//item"
    ):

        try:

            title = clean_text(
                item.findtext(
                    "title",
                    ""
                )
            )

            description = clean_text(
                item.findtext(
                    "description",
                    ""
                )
            )

            pubdate = clean_text(
                item.findtext(
                    "pubDate",
                    ""
                )
            )

            link = clean_text(
                item.findtext(
                    "link",
                    ""
                )
            )

            dt = parse_date(
                pubdate
            )

            if not is_fresh(dt):
                continue

            event = {
                "source": "NEWS",
                "form": "NEWS",
                "title": title,
                "company": "",
                "description": description,
                "datetime": dt,
                "link": link,
                "ticker": "",
                "cik": "",
                "filing_text": "",
            }

            qualifying, category, confidence, reason = (
                classify_event(
                    event
                )
            )

            if not qualifying:
                continue

            event["category"] = category
            event["classification_confidence"] = confidence
            event["classification_reason"] = reason

            events.append(
                event
            )

        except Exception as e:

            print(
                f"NEWS ENTRY ERROR: {e}"
            )

    print(
        f"News qualifying events: "
        f"{len(events)}"
    )

    return events, True


# ============================================================
# TICKERS
# ============================================================

def extract_tickers_from_text(
    text,
    ticker_map
):
    found = []

    if not text:
        return found

    # $TICKER
    for ticker in re.findall(
        r"\$([A-Z]{1,5})\b",
        text
    ):

        if ticker not in found:
            found.append(
                ticker
            )

    # Exchange:TICKER
    for ticker in re.findall(
        r"(?:NASDAQ|NYSE|AMEX|NYSE American|OTC)"
        r"[:\s]+([A-Z]{1,5})",
        text,
        flags=re.I
    ):

        ticker = ticker.upper()

        if ticker not in found:
            found.append(
                ticker
            )

    # Match company names against the SEC map,
    # but only if the exact company name is substantial.
    low = text.lower()

    for info in ticker_map.values():

        name = str(
            info.get(
                "name",
                ""
            )
        ).strip()

        ticker = str(
            info.get(
                "ticker",
                ""
            )
        ).upper()

        if (
            len(name) >= 6
            and name.lower() in low
            and ticker
        ):

            if ticker not in found:
                found.append(
                    ticker
                )

        if len(found) >= 8:
            break

    blacklist = {
        "THE",
        "AND",
        "FOR",
        "WITH",
        "FROM",
        "THIS",
        "THAT",
        "SEC",
        "CEO",
        "CFO",
        "USA",
        "USD",
        "AI",
        "US",
    }

    return [
        x
        for x in found
        if x not in blacklist
    ][:8]


# ============================================================
# BENEFICIARY
# ============================================================

def beneficiary_analysis(
    event,
    ticker_map
):
    text = (
        event.get(
            "title",
            ""
        )
        + " "
        + event.get(
            "description",
            ""
        )
        + " "
        + event.get(
            "filing_text",
            ""
        )
    )

    hits = find_terms(
        text,
        BENEFICIARY_TERMS
    )

    name = ""

    patterns = [
        r"(?:provided by|supplied by|"
        r"contract with|agreement with|"
        r"partnered with)\s+"
        r"([A-Z][A-Za-z0-9&.,' -]{2,70})",

        r"(?:supplier|vendor|contractor|"
        r"provider)\s+"
        r"([A-Z][A-Za-z0-9&.,' -]{2,70})",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text
        )

        if match:

            name = (
                match.group(1)
                .strip()
            )

            name = re.split(
                r"\s+(?:for|to|as|that|which|and)\s+",
                name,
                maxsplit=1,
                flags=re.I
            )[0].strip()

            break

    tickers = extract_tickers_from_text(
        text,
        ticker_map
    )

    strength = 0

    if hits:
        strength += 15

    if any(
        x in text.lower()
        for x in [
            "will provide",
            "will supply",
            "selected",
            "awarded",
            "contracted",
            "supplier",
            "vendor",
        ]
    ):
        strength += 20

    if name:
        strength += 15

    return {
        "name": name,
        "hits": hits,
        "strength": min(
            50,
            strength
        ),
        "tickers": tickers,
    }


# ============================================================
# PRICE / REPRICING
# ============================================================

def yahoo_chart(ticker):
    try:

        period1 = int(
            (
                now_utc()
                - timedelta(days=7)
            ).timestamp()
        )

        period2 = int(
            now_utc().timestamp()
        )

        url = (
            "https://query1.finance.yahoo.com/"
            "v8/finance/chart/"
            + urllib.parse.quote(
                ticker
            )
            + "?"
            + urllib.parse.urlencode({
                "period1": period1,
                "period2": period2,
                "interval": "1d",
                "events": "history",
            })
        )

        text = fetch(
            url,
            retries=2,
            backoff=1
        )

        if not text:
            return None

        data = json.loads(
            text
        )

        result = (
            data
            .get("chart", {})
            .get("result", [])
        )

        if not result:
            return None

        quote = (
            result[0]
            .get("indicators", {})
            .get("quote", [{}])[0]
        )

        closes = [
            float(x)
            for x in quote.get(
                "close",
                []
            )
            if x is not None
        ]

        if len(closes) < 2:
            return None

        previous = closes[-2]
        current = closes[-1]

        if previous == 0:
            return None

        move = (
            (
                current
                - previous
            )
            / previous
        ) * 100

        return {
            "ticker": ticker,
            "current": current,
            "previous": previous,
            "move": move,
        }

    except Exception:
        return None


def repricing(
    tickers
):
    for ticker in tickers[:5]:

        result = yahoo_chart(
            ticker
        )

        if result:
            return result

    return {
        "ticker": (
            tickers[0]
            if tickers
            else ""
        ),
        "move": None,
    }


# ============================================================
# SCORING
# ============================================================

def calculate_score(
    event,
    beneficiary,
    repricing_data
):
    score = 0

    value = event.get(
        "capital_value",
        0
    )

    # Capital size.
    if value >= 10_000_000_000:
        score += 35
    elif value >= 5_000_000_000:
        score += 32
    elif value >= 1_000_000_000:
        score += 28
    elif value >= 500_000_000:
        score += 24
    elif value >= 100_000_000:
        score += 18
    elif value >= 50_000_000:
        score += 12

    # Category quality.
    category = event.get(
        "category",
        ""
    )

    category_points = {
        "AI / INFRASTRUCTURE CAPEX": 25,
        "MAJOR CONTRACT": 23,
        "M&A / CAPITAL DEPLOYMENT": 22,
        "MANUFACTURING CAPEX": 21,
        "STRATEGIC CAPITAL": 19,
        "CAPITAL FINANCING": 14,
    }

    score += category_points.get(
        category,
        5
    )

    # Beneficiary evidence.
    score += min(
        20,
        beneficiary.get(
            "strength",
            0
        )
    )

    # Repricing.
    move = repricing_data.get(
        "move"
    )

    if move is not None:

        absolute = abs(move)

        if absolute < 2:
            score += 15
        elif absolute < 5:
            score += 10
        elif absolute < 8:
            score += 5
        else:
            score -= 5

    # Early information.
    age = age_hours(
        event.get(
            "datetime"
        )
    )

    if age <= 2:
        score += 10
    elif age <= 6:
        score += 8
    elif age <= 12:
        score += 5
    elif age <= 24:
        score += 2

    return max(
        0,
        min(
            100,
            score
        )
    )


# ============================================================
# FIRST-MOVER / ENTRY / EXIT
# ============================================================

def first_mover(event):
    age = age_hours(
        event.get(
            "datetime"
        )
    )

    if age <= 2:
        return "VERY EARLY — <2 HOURS"

    if age <= 6:
        return "EARLY — <6 HOURS"

    if age <= 12:
        return "ACTIVE — <12 HOURS"

    if age <= 24:
        return "LATE — <24 HOURS"

    return "EXPIRED"


def entry_plan(
    repricing_data,
    has_ticker
):
    if not has_ticker:
        return (
            "NO ENTRY — public ticker has not been "
            "verified. Identify the tradable beneficiary first."
        )

    move = repricing_data.get(
        "move"
    )

    if move is None:
        return (
            "WAIT FOR PRICE CONFIRMATION. "
            "Do not chase until live price and ticker "
            "are verified."
        )

    if abs(move) < 3:
        return (
            "EARLY: monitor the first reaction and "
            "consider staged entry after confirmation."
        )

    if abs(move) < 7:
        return (
            "PARTIALLY REPRICED: avoid chasing; "
            "look for a controlled pullback/retest."
        )

    return (
        "HEAVILY REPRICED: do not chase the move; "
        "wait for a pullback or fresh confirmation."
    )


def exit_plan(
    repricing_data
):
    move = repricing_data.get(
        "move"
    )

    if move is not None and abs(move) >= 7:
        return (
            "Consider taking partial profit after "
            "material repricing. Protect the remainder "
            "with a predefined stop/trailing rule. "
            "Exit if the catalyst or original thesis breaks."
        )

    return (
        "Take partial profit after material repricing. "
        "Exit if the catalyst fades, the transaction "
        "changes materially, or the original thesis breaks."
    )


# ============================================================
# BAMBOO
# ============================================================

def bamboo_status(
    tickers
):
    if not tickers:
        return (
            "NOT VERIFIED — no public ticker identified."
        )

    return (
        "CHECK REQUIRED — ticker identified, but "
        "live Bamboo tradability/order availability "
        "must be verified before entry."
    )


# ============================================================
# DEDUPLICATION
# ============================================================

def event_key(event):
    raw = "|".join([
        str(event.get("source", "")),
        str(event.get("form", "")),
        str(event.get("cik", "")),
        str(event.get("title", "")),
        str(event.get("link", "")),
        str(event.get("datetime", "")),
    ])

    return hashlib.sha256(
        raw.encode(
            "utf-8"
        )
    ).hexdigest()


def deduplicate(
    events
):
    output = {}
    for event in events:

        key = event_key(
            event
        )

        if key not in output:
            output[key] = event

    return list(
        output.values()
    )


# ============================================================
# STATE
# ============================================================

def load_state():
    if not os.path.exists(
        STATE_FILE
    ):
        return {
            "seen": []
        }

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(
                f
            )

        if isinstance(
            data,
            list
        ):
            return {
                "seen": data[-2000:]
            }

        if isinstance(
            data,
            dict
        ):

            seen = data.get(
                "seen",
                []
            )

            if not isinstance(
                seen,
                list
            ):
                seen = []

            return {
                "seen": seen[-2000:]
            }

    except Exception as e:

        print(
            f"STATE LOAD ERROR: {e}"
        )

    return {
        "seen": []
    }


def save_state(
    state
):
    try:

        with open(
            STATE_FILE,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                state,
                f,
                indent=2
            )

    except Exception as e:

        print(
            f"STATE SAVE ERROR: {e}"
        )


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(
    message
):
    if (
        not TELEGRAM_BOT_TOKEN
        or not TELEGRAM_CHAT_ID
    ):

        print(
            "TELEGRAM NOT CONFIGURED"
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
    }).encode(
        "utf-8"
    )

    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "User-Agent":
                "Capital Flow Radar",
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

            result = json.loads(
                body
            )

            if result.get(
                "ok"
            ):

                print(
                    "Telegram alert sent."
                )

                return True

            print(
                "TELEGRAM ERROR:",
                result
            )

    except Exception as e:

        print(
            f"TELEGRAM SEND ERROR: {e}"
        )

    return False


# ============================================================
# ALERT FORMAT
# ============================================================

def format_alert(
    event
):
    dt = event.get(
        "datetime"
    )

    timestamp = (
        dt.strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        if dt
        else "Unknown"
    )

    beneficiary = event.get(
        "beneficiary",
        {}
    )

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
            f"{ticker}: "
            f"{move:+.2f}% latest daily move"
        )

    tickers = event.get(
        "tickers",
        []
    )

    lines = [
        "🚨 CAPITAL FLOW RADAR",
        "",
        f"🔥 {event.get('category', 'CAPITAL FLOW')}",
        f"Alpha Score: {event.get('score', 0)}/100",
        "",
        "📌 EVENT",
        event.get(
            "title",
            "Untitled event"
        ),
        f"🕐 {timestamp}",
        f"📄 {event.get('form', 'NEWS')}",
        "",
        "💰 CAPITAL",
        (
            f"Disclosed/identified amount: "
            f"{money_label(event.get('capital_value', 0))}"
        ),
        (
            "Signals: "
            + ", ".join(
                event.get(
                    "capital_hits",
                    []
                )[:8]
            )
        ),
        "",
        "🎯 BENEFICIARY",
    ]

    if beneficiary.get(
        "name"
    ):
        lines.append(
            "Potential beneficiary: "
            + beneficiary["name"]
        )
    else:
        lines.append(
            "No specific beneficiary confidently identified."
        )

    if tickers:
        lines.append(
            "Public ticker(s): "
            + ", ".join(
                tickers[:8]
            )
        )
    else:
        lines.append(
            "Public ticker: NOT IDENTIFIED"
        )

    lines.extend([
        "",
        "📈 REPRICING",
        repricing_text,
        "",
        "⚡ FIRST-MOVER WINDOW",
        first_mover(event),
        "",
        "🟢 ENTRY",
        entry_plan(
            repricing_data,
            bool(tickers)
        ),
        "",
        "🔴 EXIT",
        exit_plan(
            repricing_data
        ),
        "",
        "🇳🇬 BAMBOO",
        bamboo_status(
            tickers
        ),
    ])

    if event.get(
        "classification_reason"
    ):
        lines.extend([
            "",
            "🧠 WHY IT QUALIFIED",
            event[
                "classification_reason"
            ],
        ])

    if event.get(
        "link"
    ):
        lines.extend([
            "",
            "🔗 SOURCE",
            event["link"],
        ])

    return "\n".join(
        lines
    )


# ============================================================
# ENRICHMENT
# ============================================================

def enrich_event(
    event,
    ticker_map
):
    text = (
        event.get(
            "title",
            ""
        )
        + " "
        + event.get(
            "description",
            ""
        )
        + " "
        + event.get(
            "filing_text",
            ""
        )
    )

    tickers = []

    if event.get(
        "ticker"
    ):
        tickers.append(
            event["ticker"]
        )

    tickers.extend(
        extract_tickers_from_text(
            text,
            ticker_map
        )
    )

    event["tickers"] = list(
        dict.fromkeys(
            tickers
        )
    )

    beneficiary = (
        beneficiary_analysis(
            event,
            ticker_map
        )
    )

    # Add beneficiary-derived tickers.
    event["tickers"] = list(
        dict.fromkeys(
            event["tickers"]
            + beneficiary.get(
                "tickers",
                []
            )
        )
    )

    event["beneficiary"] = (
        beneficiary
    )

    event["repricing"] = (
        repricing(
            event["tickers"]
        )
    )

    event["score"] = (
        calculate_score(
            event,
            beneficiary,
            event["repricing"]
        )
    )

    return event


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "================================"
    )

    print(
        "STARTING CAPITAL FLOW RADAR"
    )

    print(
        "================================"
    )

    state = load_state()

    seen = set(
        state.get(
            "seen",
            []
        )
    )

    # --------------------------------------------------------
    # Load official SEC ticker map once.
    # --------------------------------------------------------

    ticker_map = (
        load_sec_ticker_map()
    )

    # --------------------------------------------------------
    # Detection.
    # --------------------------------------------------------

    sec = sec_events(
        ticker_map
    )

    news, news_available = (
        news_events()
    )

    raw = deduplicate(
        sec + news
    )

    print("")
    print(
        f"Raw qualifying events: "
        f"{len(raw)}"
    )

    # --------------------------------------------------------
    # Strict 24-hour window.
    # --------------------------------------------------------

    fresh = [
        event
        for event in raw
        if is_fresh(
            event.get(
                "datetime"
            )
        )
    ]

    print(
        f"Fresh events: "
        f"{len(fresh)}"
    )

    # --------------------------------------------------------
    # Deduplicate previously alerted events.
    # --------------------------------------------------------

    new_events = [
        event
        for event in fresh
        if event_key(event)
        not in seen
    ]

    print(
        f"Previously seen events: "
        f"{len(seen)}"
    )

    print(
        f"New alerts to analyze: "
        f"{len(new_events)}"
    )

    # --------------------------------------------------------
    # Enrichment.
    # --------------------------------------------------------

    analyzed = []

    for event in new_events:

        try:

            analyzed.append(
                enrich_event(
                    event,
                    ticker_map
                )
            )

        except Exception as e:

            print(
                f"ENRICHMENT ERROR: {e}"
            )

    # --------------------------------------------------------
    # Highest-confidence events first.
    # --------------------------------------------------------

    analyzed.sort(
        key=lambda e: (
            e.get(
                "score",
                0
            ),
            e.get(
                "classification_confidence",
                0
            ),
            -age_hours(
                e.get(
                    "datetime"
                )
            ),
        ),
        reverse=True
    )

    # --------------------------------------------------------
    # Telegram.
    # --------------------------------------------------------

    sent = 0

    for event in analyzed[
        :MAX_ALERTS_PER_RUN
    ]:

        try:

            message = format_alert(
                event
            )

            if send_telegram(
                message
            ):

                sent += 1

        except Exception as e:

            print(
                f"ALERT ERROR: {e}"
            )

    # --------------------------------------------------------
    # Persist only events we actually processed.
    # --------------------------------------------------------

    for event in fresh:

        seen.add(
            event_key(
                event
            )
        )

    state["seen"] = list(
        seen
    )[-2000:]

    save_state(
        state
    )

    # --------------------------------------------------------
    # Final diagnostics.
    # --------------------------------------------------------

    print("")
    print(
        "================================"
    )
    print(
        "RADAR STATUS"
    )
    print(
        "================================"
    )

    print(
        f"SEC qualifying: {len(sec)}"
    )

    print(
        f"News qualifying: {len(news)}"
    )

    print(
        "News source available: "
        + (
            "YES"
            if news_available
            else "NO"
        )
    )

    print(
        f"Fresh events: {len(fresh)}"
    )

    print(
        f"New events: {len(new_events)}"
    )

    print(
        f"Telegram alerts sent: {sent}"
    )

    if not news_available:

        print(
            "WARNING: NEWS SOURCE UNAVAILABLE"
        )

    print("")
    print(
        "Radar completed successfully."
    )


if __name__ == "__main__":
    main()
