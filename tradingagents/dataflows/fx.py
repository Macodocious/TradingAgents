"""Currency conversion where a figure's money differs from the quote's.

A statement carries the currency of the filing: a Canadian bank reports in CAD,
a cross-listed issuer such as Shopify in USD. A price is quoted in the listing's
currency. Comparing the two without converting is wrong by the exchange rate, so
a figure is converted whenever the two differ, and the conversion is stated in
the output rather than applied silently.

Only the USD/CAD pair is served, because that is the pair a Canadian run meets;
any other pair returns None, which callers must surface rather than ignore.
"""

import csv
import io
import logging
from datetime import datetime
from functools import lru_cache

from tradingagents.dataflows.net import get_scrubbed

logger = logging.getLogger(__name__)

# FRED's public CSV endpoint serves a series without an API key; the same
# vintage discipline is kept by choosing the last observation on or before the
# analysis date rather than the newest one.
_FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
_REQUEST_TIMEOUT = 30

# Quote currency of a listing, by Yahoo exchange suffix. TSX and TSX-V trade in
# Canadian dollars; everything else is treated as USD, which is what the vendor
# rows assume when a suffix is absent.
_QUOTE_BY_SUFFIX = {".TO": "CAD", ".V": "CAD"}

# Pairs this module can convert: (source, target) -> (series, multiply).
# DEXCAUS is Canadian dollars per US dollar, so USD->CAD multiplies and the
# reverse divides.
_PAIRS = {
    ("USD", "CAD"): ("DEXCAUS", True),
    ("CAD", "USD"): ("DEXCAUS", False),
}


def quote_currency(ticker: str) -> str:
    """The currency a listing's price is quoted in."""
    symbol = ticker.strip().upper()
    for suffix, currency in _QUOTE_BY_SUFFIX.items():
        if symbol.endswith(suffix):
            return currency
    return "USD"


@lru_cache(maxsize=64)
def _series_through(series: str) -> tuple[tuple[str, float], ...]:
    """The series as (date, value) pairs, oldest first; empty when unreachable."""
    try:
        response = get_scrubbed(
            _FRED_CSV.format(series=series),
            params={},
            timeout=_REQUEST_TIMEOUT,
            secret="",
        )
    except Exception as error:  # noqa: BLE001 - a rate is optional, the run is not
        logger.warning("FX series %s unavailable: %s", series, error)
        return ()

    rows: list[tuple[str, float]] = []
    for row in csv.reader(io.StringIO(response.text)):
        if len(row) != 2 or not row[0] or row[0].lower().startswith("observation"):
            continue
        try:
            rows.append((row[0], float(row[1])))
        except ValueError:
            continue  # FRED writes "." for a missing observation
    return tuple(rows)


def fx_rate(source: str, target: str, as_of_date: str) -> float | None:
    """The rate that turns ``source`` money into ``target`` money, or None.

    The rate is the last observation on or before ``as_of_date``, so a
    backdated run never converts with a rate the market had not set yet.
    """
    if source == target:
        return 1.0
    pair = _PAIRS.get((source.upper(), target.upper()))
    if pair is None:
        logger.warning("No FX series for %s->%s; figures stay in %s", source, target, source)
        return None
    series, multiply = pair

    latest: tuple[str, float] | None = None
    for day, value in _series_through(series):
        if day <= as_of_date:
            latest = (day, value)
        else:
            break
    if latest is None or latest[1] == 0:
        return None
    return latest[1] if multiply else 1.0 / latest[1]


def rate_date(source: str, target: str, as_of_date: str) -> str | None:
    """The observation date behind :func:`fx_rate`, for stating it in output."""
    if source == target:
        return as_of_date
    pair = _PAIRS.get((source.upper(), target.upper()))
    if pair is None:
        return None
    series, _ = pair
    latest = None
    for day, _value in _series_through(series):
        if day <= as_of_date:
            latest = day
        else:
            break
    return latest


def convert(value: float, rate: float) -> float:
    return value * rate
