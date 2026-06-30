from __future__ import annotations

import datetime as dt
import email.utils
import html
from functools import lru_cache
from html.parser import HTMLParser
import json
import os
import re
from pathlib import Path
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from efsr.models import FilingItem, NewsItem, PriceStats


SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_USER_AGENT = "auto-trading-research/0.1 lvyongyu@gmail.com"
SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
LIVE_UNIVERSE_SPEC = {"sp500-live", "live-sp500", "sp500"}
ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / ".cache" / "event-first"
CACHE_TTL_DAYS = 7

EVENT_KEYWORDS = {
    "earnings_miss": {
        "miss", "misses", "missed", "disappoint", "disappoints", "weak guidance",
        "cuts forecast", "cut forecast", "lowers guidance", "guidance cut",
    },
    "earnings_recoverable": {
        "earnings", "revenue", "sales", "guidance", "margin", "forecast",
        "outlook", "profit", "free cash flow",
    },
    "analyst_negative": {
        "downgrade", "downgrades", "price target cut", "target cut", "sell rating",
        "underperform", "bearish",
    },
    "analyst_positive": {
        "upgrade", "upgrades", "price target raised", "target raised", "buy rating",
        "outperform", "bullish",
    },
    "company_action_positive": {
        "buyback", "repurchase", "dividend increase", "raises dividend", "spin off",
        "spinoff", "strategic review", "asset sale", "activist",
    },
    "legal_regulatory": {
        "lawsuit", "sues", "sued", "investigation", "probe", "doj", "ftc", "sec",
        "antitrust", "fda", "warning letter", "recall",
    },
    "terminal_risk": {
        "bankruptcy", "chapter 11", "going concern", "delisting", "fraud",
        "accounting irregularities", "restatement", "halted", "insolvency",
    },
    "macro_sector": {
        "tariff", "rates", "inflation", "oil", "chip", "semiconductor", "ai",
        "consumer spending", "housing", "drug pricing",
    },
}

NEGATIVE_WORDS = {
    "miss", "misses", "missed", "falls", "drops", "tumbles", "plunges", "slumps",
    "cuts", "cut", "lowers", "downgrade", "downgrades", "probe", "investigation",
    "lawsuit", "recall", "weak", "disappointing", "concern", "pressure",
}

POSITIVE_WORDS = {
    "beats", "beat", "raises", "raised", "upgrade", "upgrades", "surges", "jumps",
    "buyback", "repurchase", "dividend", "approval", "record", "strong",
}


def fetch_url(url: str, timeout: int = 12) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 event-bottom-fishing-agent/0.1",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def fetch_sec_url(url: str, timeout: int = 12) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": SEC_USER_AGENT,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def _ensure_cache_dir() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_path(name: str) -> Path:
    return CACHE_DIR / name


def _read_json_cache(name: str) -> tuple[object | None, dt.datetime | None]:
    path = _cache_path(name)
    if not path.exists():
        return None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    data = payload.get("data") if isinstance(payload, dict) else None
    fetched_at_raw = payload.get("fetched_at") if isinstance(payload, dict) else None
    if not isinstance(fetched_at_raw, str):
        return data, None
    try:
        fetched_at = dt.datetime.fromisoformat(fetched_at_raw)
    except ValueError:
        fetched_at = None
    return data, fetched_at


def _write_json_cache(name: str, data: object) -> None:
    _ensure_cache_dir()
    payload = {
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "data": data,
    }
    _cache_path(name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _is_cache_fresh(fetched_at: dt.datetime | None, ttl_days: int = CACHE_TTL_DAYS) -> bool:
    if not fetched_at:
        return False
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=dt.timezone.utc)
    age = dt.datetime.now(dt.timezone.utc) - fetched_at.astimezone(dt.timezone.utc)
    return age <= dt.timedelta(days=ttl_days)


def load_universe_file(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as handle:
        tickers = []
        for line in handle:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                tickers.append(stripped.upper())
        return tickers


class _WikiTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._inside_table = False
        self._table_depth = 0
        self._current_table: list[list[str]] = []
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None
        self._capture_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {key: value or "" for key, value in attrs}
        if tag == "table":
            classes = attrs_map.get("class", "")
            if not self._inside_table and "wikitable" in classes:
                self._inside_table = True
                self._table_depth = 1
                self._current_table = []
            elif self._inside_table:
                self._table_depth += 1
        elif self._inside_table and self._table_depth == 1:
            if tag == "tr":
                self._current_row = []
            elif tag in {"td", "th"} and self._current_row is not None:
                self._capture_cell = True
                self._current_cell = []

    def handle_endtag(self, tag: str) -> None:
        if not self._inside_table:
            return
        if tag == "table":
            self._table_depth -= 1
            if self._table_depth == 0:
                if self._current_table:
                    self.tables.append(self._current_table)
                self._inside_table = False
                self._current_table = []
                self._current_row = None
                self._current_cell = None
                self._capture_cell = False
        elif self._table_depth == 1:
            if tag in {"td", "th"} and self._capture_cell:
                if self._current_row is not None and self._current_cell is not None:
                    self._current_row.append(" ".join("".join(self._current_cell).split()))
                self._current_cell = None
                self._capture_cell = False
            elif tag == "tr" and self._current_row is not None:
                if any(cell.strip() for cell in self._current_row):
                    self._current_table.append(self._current_row)
                self._current_row = None

    def handle_data(self, data: str) -> None:
        if self._capture_cell and self._current_cell is not None:
            self._current_cell.append(data)


@lru_cache(maxsize=1)
def fetch_sp500_universe() -> list[str]:
    raw = fetch_url(SP500_WIKI_URL)
    parser = _WikiTableParser()
    parser.feed(raw.decode("utf-8", errors="ignore"))
    for table in parser.tables:
        if not table or len(table[0]) < 2:
            continue
        header = [cell.strip().lower() for cell in table[0]]
        if "symbol" not in header or "security" not in header:
            continue
        symbol_index = header.index("symbol")
        symbols: list[str] = []
        for row in table[1:]:
            if len(row) <= symbol_index:
                continue
            symbol = row[symbol_index].strip().upper()
            if not symbol or symbol == "SYMBOL":
                continue
            if symbol not in symbols:
                symbols.append(symbol)
        if symbols:
            return symbols
    raise RuntimeError("Unable to parse live S&P 500 constituents from Wikipedia.")


def load_universe(spec: str, fallback_path: str | None = None) -> list[str]:
    normalized = (spec or "").strip().lower()
    if normalized in LIVE_UNIVERSE_SPEC:
        cached, fetched_at = _read_json_cache("sp500_universe.json")
        if isinstance(cached, list) and _is_cache_fresh(fetched_at):
            return [str(item).upper() for item in cached if str(item).strip()]
        try:
            symbols = fetch_sp500_universe()
            _write_json_cache("sp500_universe.json", symbols)
            return symbols
        except Exception:
            if isinstance(cached, list) and cached:
                return [str(item).upper() for item in cached if str(item).strip()]
            if fallback_path and os.path.exists(fallback_path):
                return load_universe_file(fallback_path)
            raise
    if os.path.exists(spec):
        return load_universe_file(spec)
    if fallback_path and os.path.exists(fallback_path):
        return load_universe_file(fallback_path)
    raise FileNotFoundError(spec)


def load_alias_file(path: str) -> dict[str, list[str]]:
    if not path or not re.search(r"\.json$", path) or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return {str(key).upper(): [str(item) for item in value] for key, value in payload.items()}


@lru_cache(maxsize=1)
def load_sec_company_records() -> dict[str, dict[str, str]]:
    cached, fetched_at = _read_json_cache("sec_company_records.json")
    if isinstance(cached, dict) and _is_cache_fresh(fetched_at):
        return {str(key).upper(): {str(k): str(v) for k, v in value.items()} for key, value in cached.items() if isinstance(value, dict)}
    try:
        payload = json.loads(fetch_sec_url(SEC_TICKERS_URL).decode("utf-8"))
        records: dict[str, dict[str, str]] = {}
        for record in payload.values():
            ticker = str(record.get("ticker", "")).upper()
            cik = str(record.get("cik_str", "")).zfill(10)
            title = str(record.get("title", "")).strip()
            if ticker and cik:
                records[ticker] = {"cik": cik, "title": title}
        _write_json_cache("sec_company_records.json", records)
        return records
    except Exception:
        if isinstance(cached, dict):
            return {str(key).upper(): {str(k): str(v) for k, v in value.items()} for key, value in cached.items() if isinstance(value, dict)}
        raise


@lru_cache(maxsize=1)
def load_sec_ticker_map() -> dict[str, str]:
    return {ticker: record["cik"] for ticker, record in load_sec_company_records().items()}


def _clean_company_alias(value: str) -> str:
    cleaned = html.unescape(value)
    cleaned = re.sub(r"\s*\((?:the )?company\)\s*$", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*class [a-z0-9]+\s*$", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*series [a-z0-9]+\s*$", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(?:inc|inc\.|corp|corp\.|co|co\.|ltd|ltd\.|plc|limited|corporation|company|holding|holdings|group)\b\.?\s*$", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.-")
    cleaned = re.sub(r"^the\s+", "", cleaned, flags=re.I)
    return cleaned.strip()


def _alias_variants(title: str) -> list[str]:
    variants: list[str] = []
    base = re.sub(r"\s+", " ", html.unescape(title)).strip()
    if not base:
        return variants
    variants.append(base)
    cleaned = _clean_company_alias(base)
    if cleaned and cleaned not in variants:
        variants.append(cleaned)
    no_parenthetical = re.sub(r"\s*\([^)]*\)", "", cleaned).strip()
    if no_parenthetical and no_parenthetical not in variants:
        variants.append(no_parenthetical)
    for candidate in list(variants):
        alt = candidate.replace("&", "and")
        if alt and alt not in variants:
            variants.append(alt)
    return [variant for variant in variants if len(variant) >= 2]


def build_auto_aliases(universe: list[str] | None = None) -> dict[str, list[str]]:
    records = load_sec_company_records()
    selected = universe or list(records)
    aliases: dict[str, list[str]] = {}
    for ticker in selected:
        record = records.get(ticker.upper())
        if not record:
            continue
        variants = _alias_variants(record.get("title", ""))
        if variants:
            aliases[ticker.upper()] = variants
    return aliases


def merge_alias_maps(primary: dict[str, list[str]], overrides: dict[str, list[str]]) -> dict[str, list[str]]:
    merged = {key.upper(): list(dict.fromkeys(value)) for key, value in primary.items()}
    for ticker, aliases in overrides.items():
        ticker = ticker.upper()
        current = merged.setdefault(ticker, [])
        for alias in aliases:
            alias = str(alias).strip()
            if alias and alias not in current:
                current.append(alias)
    return merged


def load_aliases(spec: str, universe: list[str] | None = None, manual_override_path: str | None = None) -> dict[str, list[str]]:
    normalized = (spec or "").strip().lower()
    if normalized == "auto":
        auto_aliases = build_auto_aliases(universe)
        manual_aliases = load_alias_file(manual_override_path) if manual_override_path else {}
        return merge_alias_maps(auto_aliases, manual_aliases)
    if os.path.exists(spec):
        return load_alias_file(spec)
    if manual_override_path and os.path.exists(manual_override_path):
        return load_alias_file(manual_override_path)
    return {}


def parse_rss_date(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def categorize(title: str) -> list[str]:
    lower = title.lower()
    categories = []
    for category, words in EVENT_KEYWORDS.items():
        if any(word in lower for word in words):
            categories.append(category)
    return categories


def headline_sentiment(title: str) -> int:
    words = set(re.findall(r"[a-z0-9]+", title.lower()))
    neg = len(words & NEGATIVE_WORDS)
    pos = len(words & POSITIVE_WORDS)
    return max(-3, min(3, pos - neg))


def is_relevant_title(ticker: str, aliases: list[str], title: str) -> bool:
    lower = title.lower()
    normalized_ticker = ticker.replace("-", ".").lower()
    ticker_pattern = re.compile(rf"(?<![a-z0-9]){re.escape(normalized_ticker)}(?![a-z0-9])")
    if ticker_pattern.search(lower):
        return True
    return any(alias.lower() in lower for alias in aliases)


def fetch_news(
    ticker: str,
    aliases: list[str],
    max_items: int,
    lookback_days: int,
    allow_broad_news: bool,
) -> list[NewsItem]:
    symbol = ticker.replace("-", ".")
    params = urllib.parse.urlencode({"s": symbol, "region": "US", "lang": "en-US"})
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?{params}"
    raw = fetch_url(url)
    root = ET.fromstring(raw)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
    items = []
    for item in root.findall("./channel/item"):
        title = html.unescape((item.findtext("title") or "").strip())
        if not allow_broad_news and not is_relevant_title(ticker, aliases, title):
            continue
        link = (item.findtext("link") or "").strip()
        published = parse_rss_date(item.findtext("pubDate"))
        if published and published < cutoff:
            continue
        categories = categorize(title)
        if not categories:
            continue
        items.append(
            NewsItem(
                title=title,
                link=link,
                published=published,
                categories=categories,
                sentiment=headline_sentiment(title),
            )
        )
        if len(items) >= max_items:
            break
    return items


def fetch_price_stats(ticker: str) -> PriceStats | None:
    symbol = urllib.parse.quote(ticker.replace("-", "-"))
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{symbol}?range=6mo&interval=1d&includePrePost=false"
    )
    raw = fetch_url(url)
    payload = json.loads(raw.decode("utf-8"))
    result = payload.get("chart", {}).get("result") or []
    if not result:
        return None
    quote = result[0]["indicators"]["quote"][0]
    closes = [x for x in quote.get("close", []) if isinstance(x, (int, float))]
    volumes = [x for x in quote.get("volume", []) if isinstance(x, (int, float))]
    if len(closes) < 61 or len(volumes) < 25:
        return None
    last = closes[-1]
    high_60 = max(closes[-60:])
    low_5 = min(closes[-5:])
    avg_vol_5 = sum(volumes[-5:]) / 5
    avg_vol_20 = sum(volumes[-20:]) / 20
    return PriceStats(
        last_close=last,
        change_5d=(last / closes[-6] - 1) * 100,
        change_20d=(last / closes[-21] - 1) * 100,
        drawdown_60d=(last / high_60 - 1) * 100,
        above_5d_low=(last / low_5 - 1) * 100,
        volume_ratio_5d_20d=avg_vol_5 / avg_vol_20 if avg_vol_20 else 0,
    )


def parse_csv_rows(raw: bytes) -> list[dict[str, str]]:
    lines = raw.decode("utf-8").strip().splitlines()
    if len(lines) < 2:
        return []
    headers = lines[0].split(",")
    rows = []
    for line in lines[1:]:
        values = line.split(",")
        if len(values) == len(headers):
            rows.append(dict(zip(headers, values)))
    return rows


def stooq_symbol(ticker: str) -> str:
    return ticker.lower().replace("-", ".") + ".us"


def fetch_stooq_price_stats(ticker: str) -> PriceStats | None:
    end = dt.datetime.now(dt.timezone.utc).date()
    start = end - dt.timedelta(days=220)
    params = urllib.parse.urlencode(
        {
            "s": stooq_symbol(ticker),
            "d1": start.strftime("%Y%m%d"),
            "d2": end.strftime("%Y%m%d"),
            "i": "d",
        }
    )
    url = f"https://stooq.com/q/d/l/?{params}"
    rows = parse_csv_rows(fetch_url(url))
    closes = []
    volumes = []
    for row in rows:
        try:
            closes.append(float(row["Close"]))
            volumes.append(float(row.get("Volume") or 0))
        except (KeyError, TypeError, ValueError):
            continue
    if len(closes) < 61 or len(volumes) < 25:
        return None
    last = closes[-1]
    high_60 = max(closes[-60:])
    low_5 = min(closes[-5:])
    avg_vol_5 = sum(volumes[-5:]) / 5
    avg_vol_20 = sum(volumes[-20:]) / 20
    return PriceStats(
        last_close=last,
        change_5d=(last / closes[-6] - 1) * 100,
        change_20d=(last / closes[-21] - 1) * 100,
        drawdown_60d=(last / high_60 - 1) * 100,
        above_5d_low=(last / low_5 - 1) * 100,
        volume_ratio_5d_20d=avg_vol_5 / avg_vol_20 if avg_vol_20 else 0,
    )


def fetch_recent_sec_filings(ticker: str, cik_by_ticker: dict[str, str], lookback_days: int) -> list[FilingItem]:
    cik = cik_by_ticker.get(ticker.upper())
    if not cik:
        return []
    payload = json.loads(fetch_sec_url(SEC_SUBMISSIONS_URL.format(cik=cik)).decode("utf-8"))
    recent = payload.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    filing_dates = recent.get("filingDate", [])
    report_dates = recent.get("reportDate", [])
    accession_numbers = recent.get("accessionNumber", [])
    descriptions = recent.get("primaryDocDescription", [])
    cutoff = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=lookback_days)
    interesting_forms = {"8-K", "10-Q", "10-K", "6-K", "20-F"}
    filings = []
    for index, form in enumerate(forms):
        filing_date = filing_dates[index] if index < len(filing_dates) else ""
        try:
            parsed_date = dt.date.fromisoformat(filing_date)
        except ValueError:
            continue
        if parsed_date < cutoff or form not in interesting_forms:
            continue
        filings.append(
            FilingItem(
                form=form,
                filing_date=filing_date,
                report_date=report_dates[index] if index < len(report_dates) else "",
                accession_number=accession_numbers[index] if index < len(accession_numbers) else "",
                description=descriptions[index] if index < len(descriptions) else "",
            )
        )
        if len(filings) >= 5:
            break
    return filings


def fetch_sec_company_facts(ticker: str, cik_by_ticker: dict[str, str]) -> dict | None:
    cik = cik_by_ticker.get(ticker.upper())
    if not cik:
        return None
    return json.loads(fetch_sec_url(SEC_COMPANY_FACTS_URL.format(cik=cik)).decode("utf-8"))


def fact_units(payload: dict, tag_names: list[str], unit: str = "USD") -> list[dict]:
    facts = payload.get("facts", {})
    for taxonomy in ("us-gaap", "dei"):
        taxonomy_facts = facts.get(taxonomy, {})
        for tag in tag_names:
            units = taxonomy_facts.get(tag, {}).get("units", {})
            if unit in units:
                return units[unit]
    return []


def latest_annual_value(payload: dict, tag_names: list[str], unit: str = "USD") -> tuple[float | None, str | None]:
    values = [
        item for item in fact_units(payload, tag_names, unit)
        if item.get("fy") and item.get("fp") == "FY" and isinstance(item.get("val"), (int, float))
    ]
    values.sort(key=lambda item: (item.get("fy", 0), item.get("filed", "")), reverse=True)
    if not values:
        return None, None
    return float(values[0]["val"]), str(values[0].get("fy"))


def latest_two_annual_values(payload: dict, tag_names: list[str], unit: str = "USD") -> list[tuple[int, float]]:
    values_by_year: dict[int, float] = {}
    values = [
        item for item in fact_units(payload, tag_names, unit)
        if item.get("fy") and item.get("fp") == "FY" and isinstance(item.get("val"), (int, float))
    ]
    values.sort(key=lambda item: (item.get("fy", 0), item.get("filed", "")), reverse=True)
    for item in values:
        year = int(item["fy"])
        values_by_year.setdefault(year, float(item["val"]))
        if len(values_by_year) >= 2:
            break
    return sorted(values_by_year.items(), reverse=True)


def latest_value(payload: dict, tag_names: list[str], unit: str) -> float | None:
    values = [
        item for item in fact_units(payload, tag_names, unit)
        if isinstance(item.get("val"), (int, float))
    ]
    values.sort(key=lambda item: item.get("filed", ""), reverse=True)
    return float(values[0]["val"]) if values else None
