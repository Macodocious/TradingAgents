import json
import logging
import os
import tempfile
import time
from datetime import datetime
from io import StringIO

import pandas as pd

from tradingagents.dataflows.errors import VendorNotConfiguredError, VendorUnavailableError
from tradingagents.dataflows.net import get_scrubbed

API_BASE_URL = "https://www.alphavantage.co/query"

# Network timeout (seconds) so a stalled Alpha Vantage request can't hang the
# CLI/agents indefinitely (#990).
REQUEST_TIMEOUT = 30


class AlphaVantageNotConfiguredError(VendorNotConfiguredError):
    """Raised when Alpha Vantage is selected but no API key is configured.

    A VendorNotConfiguredError (and thus still a ValueError), so the routing
    layer's "vendor unavailable" handling and existing ValueError callers both
    keep working.
    """
    pass


def get_api_key() -> str:
    """Retrieve the API key for Alpha Vantage from environment variables."""
    api_key = os.getenv("ALPHA_VANTAGE_API_KEY")
    if not api_key:
        raise AlphaVantageNotConfiguredError(
            "ALPHA_VANTAGE_API_KEY environment variable is not set."
        )
    return api_key


def format_datetime_for_api(date_input, end_of_day: bool = False) -> str:
    """Convert various date formats to the YYYYMMDDTHHMM Alpha Vantage expects.

    A plain date means midnight, which is the start of that day. For the end of
    a window pass ``end_of_day`` so the day itself is inside it, rather than
    dropping everything published on the analysis date.
    """
    if isinstance(date_input, str):
        # If already in correct format, return as-is
        if len(date_input) == 13 and 'T' in date_input:
            return date_input
        # Try to parse common date formats
        try:
            dt = datetime.strptime(date_input, "%Y-%m-%d")
            return dt.strftime("%Y%m%dT2359" if end_of_day else "%Y%m%dT0000")
        except ValueError:
            try:
                dt = datetime.strptime(date_input, "%Y-%m-%d %H:%M")
                return dt.strftime("%Y%m%dT%H%M")
            except ValueError:
                raise ValueError(f"Unsupported date format: {date_input}") from None
    elif isinstance(date_input, datetime):
        return date_input.strftime("%Y%m%dT%H%M")
    else:
        raise ValueError(f"Date must be string or datetime object, got {type(date_input)}")


class AlphaVantageRateLimitError(VendorUnavailableError):
    """Raised when the Alpha Vantage API rate limit is exceeded."""
    pass


logger = logging.getLogger(__name__)

# Functions Alpha Vantage serves only on a paid plan. Asking for one on a free
# key returns "This is a premium endpoint" and still spends a daily request, so
# these are refused here before any network call. Verified against a free key:
# NEWS_SENTIMENT, OVERVIEW and MACD refuse; TIME_SERIES_DAILY, SMA, RSI, BBANDS
# and ATR answer normally.
PREMIUM_FUNCTIONS = frozenset({"NEWS_SENTIMENT", "OVERVIEW", "MACD"})

# The free plan allows 25 requests per day. A per-process counter cannot see the
# runs before it, so the count lives in a small file keyed by UTC date.
DAILY_REQUEST_LIMIT = int(os.getenv("ALPHA_VANTAGE_DAILY_LIMIT", "25"))
USAGE_PATH = os.getenv("ALPHA_VANTAGE_USAGE_FILE") or os.path.join(
    tempfile.gettempdir(), "alpha_vantage_usage.json"
)

# The free plan throttles bursts; one request per second is the advertised safe
# pace, and this module leaves a margin on it.
MIN_INTERVAL_SECONDS = 1.2
_last_request_at = 0.0


class AlphaVantagePremiumError(VendorUnavailableError):
    """Raised when a call targets a function the current plan does not include."""


def _read_usage() -> tuple[str, int]:
    """(date, count) from the usage file; a missing file is simply zero."""
    try:
        with open(USAGE_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return "", 0
    except (OSError, ValueError) as error:
        logger.warning("Unreadable Alpha Vantage usage file %s: %s; counting from zero", USAGE_PATH, error)
        return "", 0
    try:
        return str(data.get("date", "")), int(data.get("count", 0))
    except (AttributeError, TypeError, ValueError) as error:
        logger.warning("Malformed Alpha Vantage usage file %s: %s; counting from zero", USAGE_PATH, error)
        return "", 0


def _write_usage(day: str, count: int) -> None:
    try:
        directory = os.path.dirname(USAGE_PATH)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temporary = f"{USAGE_PATH}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump({"date": day, "count": count}, handle)
        os.replace(temporary, USAGE_PATH)
    except OSError as error:
        logger.warning("Could not record Alpha Vantage usage at %s: %s", USAGE_PATH, error)


def _respect_free_plan(function_name: str) -> None:
    """Refuse a premium function, and refuse any call past the daily allowance."""
    if function_name in PREMIUM_FUNCTIONS:
        raise AlphaVantagePremiumError(
            f"Alpha Vantage serves {function_name} on paid plans only; the free plan "
            f"does not include it, so the call was skipped instead of spending quota."
        )
    today = datetime.utcnow().strftime("%Y-%m-%d")
    day, count = _read_usage()
    if day != today:
        day, count = today, 0
    if count >= DAILY_REQUEST_LIMIT:
        raise AlphaVantageRateLimitError(
            f"Alpha Vantage free plan allows {DAILY_REQUEST_LIMIT} requests per day; "
            f"{count} already used today ({today} UTC)."
        )


def _record_request() -> None:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    day, count = _read_usage()
    _write_usage(today, 1 if day != today else count + 1)


def _pace_request() -> None:
    """Space requests so a burst does not trip the free plan's throttle."""
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_INTERVAL_SECONDS:
        time.sleep(MIN_INTERVAL_SECONDS - elapsed)
    _last_request_at = time.monotonic()


def _make_api_request(function_name: str, params: dict) -> dict | str:
    """Helper function to make API requests and handle responses.

    Raises:
        AlphaVantageRateLimitError: When API rate limit is exceeded
    """
    # Create a copy of params to avoid modifying the original
    api_key = get_api_key()
    _respect_free_plan(function_name)
    _pace_request()
    api_params = params.copy()
    api_params.update({
        "function": function_name,
        "apikey": api_key,
        "source": "trading_agents",
    })

    # Handle entitlement parameter if present in params or global variable
    current_entitlement = globals().get('_current_entitlement')
    entitlement = api_params.get("entitlement") or current_entitlement

    if entitlement:
        api_params["entitlement"] = entitlement
    elif "entitlement" in api_params:
        # Remove entitlement if it's None or empty
        api_params.pop("entitlement", None)

    response = get_scrubbed(
        API_BASE_URL, params=api_params, timeout=REQUEST_TIMEOUT, secret=api_key
    )
    _record_request()

    response_text = response.text

    # Error responses are JSON; data responses are usually CSV (or data-keyed
    # JSON). A non-JSON body is normal data.
    try:
        response_json = json.loads(response_text)
    except json.JSONDecodeError:
        return response_text

    # Alpha Vantage reports problems via "Information" / "Note". Classify so a
    # genuine rate limit and an invalid/missing key aren't conflated (#991):
    # rate-limit phrasing is checked first because those notices also mention
    # "API key" ("your API key ... 25 requests per day").
    def scrubbed(text: str) -> str:
        # Alpha Vantage can echo the key in its notices; error text reaches logs.
        return text.replace(api_key, "***") if api_key else text

    notice = response_json.get("Information") or response_json.get("Note")
    if notice:
        notice = scrubbed(notice)
        low = notice.lower()
        if "premium" in low:
            raise AlphaVantagePremiumError(
                f"Alpha Vantage refused {function_name}: this endpoint is not on the free "
                f"plan. Add it to PREMIUM_FUNCTIONS so it is skipped before the request."
            )
        if any(m in low for m in ("rate limit", "requests per day", "call frequency")):
            raise AlphaVantageRateLimitError(f"Alpha Vantage rate limit exceeded: {notice}")
        if "api key" in low or "apikey" in low:
            # Reuse the existing "not configured" error so a bad key surfaces as
            # a real, actionable failure rather than a mislabeled rate limit (#991).
            raise AlphaVantageNotConfiguredError(f"Alpha Vantage API key invalid or missing: {notice}")

    # A call it rejects comes back as {"Error Message": ...}, for an unknown
    # symbol and for a malformed call alike, so it says nothing about the
    # instrument (#1442).
    rejection = response_json.get("Error Message")
    if rejection:
        raise VendorUnavailableError(f"Alpha Vantage rejected the request: {scrubbed(rejection)}")

    return response_text


def _filter_csv_by_date_range(csv_data: str, start_date: str, end_date: str) -> str:
    """
    Filter CSV data to include only rows within the specified date range.

    Args:
        csv_data: CSV string from Alpha Vantage API
        start_date: Start date in yyyy-mm-dd format
        end_date: End date in yyyy-mm-dd format

    Returns:
        Filtered CSV string
    """
    if not csv_data or csv_data.strip() == "":
        return csv_data

    # Deliberately unguarded: TIME_SERIES_DAILY_ADJUSTED returns the full series
    # up to today, so this trim is the only thing keeping bars after end_date out
    # of a historical run. Swallowing a parse failure would serve the untrimmed
    # body, and with it future prices.
    df = pd.read_csv(StringIO(csv_data))

    # Assume the first column is the date column (timestamp)
    date_col = df.columns[0]
    df[date_col] = pd.to_datetime(df[date_col])

    start_dt = pd.to_datetime(start_date)
    end_dt = pd.to_datetime(end_date)
    filtered_df = df[(df[date_col] >= start_dt) & (df[date_col] <= end_dt)]

    return filtered_df.to_csv(index=False)
