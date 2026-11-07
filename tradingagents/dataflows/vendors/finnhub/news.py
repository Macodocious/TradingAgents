"""Finnhub company-news fetcher.

Finnhub serves per-symbol news from ``/api/v1/company-news`` on its free tier.
It is the vendor that still answers while Yahoo's quote-news endpoint returns
404 for a plain client and 429 for a browser-shaped one.
"""

import json
import logging
import os
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import coverage_gap, in_window
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.symbols import normalize_symbol

logger = logging.getLogger(__name__)

COMPANY_NEWS_URL = "https://finnhub.io/api/v1/company-news"
API_KEY_ENV = "FINNHUB_API_KEY"
REQUEST_TIMEOUT_SECONDS: int = 30
USER_AGENT = "tradingagents/0.2"


def get_api_key() -> str:
    """The Finnhub key, or a vendor-unavailable error naming the variable."""
    api_key = os.getenv(API_KEY_ENV)
    if not api_key:
        raise VendorUnavailableError(f"{API_KEY_ENV} environment variable is not set")
    return api_key


def _fetch_articles(canonical: str, start_date: str, end_date: str) -> list:
    """Finnhub's article payload for the symbol and the window.

    A rejected key or an exhausted quota still gets an HTTP answer, so the
    payload's shape is checked: anything but a list is a failure to report, never
    an absence of news.
    """
    query = urlencode(
        {"symbol": canonical, "from": start_date, "to": end_date, "token": get_api_key()}
    )
    request = Request(f"{COMPANY_NEWS_URL}?{query}", headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        logger.warning("Finnhub news request failed for %s: %s", canonical, error)
        raise VendorUnavailableError(
            f"Finnhub news request failed: {type(error).__name__}"
        ) from error
    if not isinstance(payload, list):
        logger.warning(
            "Finnhub news returned a non-list payload for %s: %s", canonical, str(payload)[:200]
        )
        raise VendorUnavailableError("Finnhub news returned an error payload")
    return payload


def _extract_article_data(article: dict) -> dict:
    """One Finnhub article in the shape the shared formatter reads."""
    published = article.get("datetime")
    pub_date = None
    if isinstance(published, (int, float)):
        # Epoch seconds are UTC; keep the timestamp aware so the window check
        # does not shift with the host timezone.
        pub_date = datetime.fromtimestamp(published, tz=UTC)
    return {
        "title": article.get("headline") or "No title",
        "summary": article.get("summary") or "",
        "publisher": article.get("source") or "Unknown",
        "link": article.get("url") or "",
        "pub_date": pub_date,
    }


def _format_articles(articles: list) -> str:
    out = ""
    for data in articles:
        out += f"### {data['title']} (source: {data['publisher']})\n"
        if data["summary"]:
            out += f"{data['summary']}\n"
        if data["link"]:
            out += f"Link: {data['link']}\n"
        out += "\n"
    return out


def get_news_finnhub(ticker: str, start_date: str, end_date: str) -> str:
    """News about ``ticker`` published in the window, from Finnhub.

    The window filter keeps the block look-ahead safe: Finnhub serves articles
    dated after the analysis date, and none of those may reach the report.
    """
    article_limit = get_config()["news_article_limit"]
    canonical = normalize_symbol(ticker)
    resolved = "" if canonical == ticker else f" (resolved to {canonical})"
    subject = f"news for {ticker}{resolved}"

    articles = [
        _extract_article_data(article)
        for article in _fetch_articles(canonical, start_date, end_date)
    ]
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    in_range = [a for a in articles if in_window(a["pub_date"], start_dt, end_dt)]
    if in_range:
        return (
            f"## {ticker}{resolved} News, from {start_date} to {end_date}:\n\n"
            f"{_format_articles(in_range[:article_limit])}"
        )

    gap = coverage_gap(
        (a["pub_date"] for a in in_range or articles),
        start_date,
        end_date,
        "Finnhub news",
        subject,
    )
    return gap or f"No news found for {ticker}{resolved} between {start_date} and {end_date}"
