import logging

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.vendors.alpha_vantage import (
    get_balance_sheet as get_alpha_vantage_balance_sheet,
    get_cashflow as get_alpha_vantage_cashflow,
    get_fundamentals as get_alpha_vantage_fundamentals,
    get_global_news as get_alpha_vantage_global_news,
    get_income_statement as get_alpha_vantage_income_statement,
    get_indicator as get_alpha_vantage_indicator,
    get_insider_transactions as get_alpha_vantage_insider_transactions,
    get_news as get_alpha_vantage_news,
    get_stock as get_alpha_vantage_stock,
)
from tradingagents.dataflows.vendors.fred import get_macro_data as get_fred_macro_data
from tradingagents.dataflows.vendors.polymarket import (
    get_prediction_markets as get_polymarket_prediction_markets,
)
from tradingagents.dataflows.vendors.sec_edgar import (
    get_balance_sheet as get_sec_edgar_balance_sheet,
    get_cashflow as get_sec_edgar_cashflow,
    get_income_statement as get_sec_edgar_income_statement,
)
from tradingagents.dataflows.vendors.yahoo.fundamentals import (
    get_balance_sheet as get_yfinance_balance_sheet,
    get_cashflow as get_yfinance_cashflow,
    get_fundamentals as get_yfinance_fundamentals,
    get_income_statement as get_yfinance_income_statement,
    get_insider_transactions as get_yfinance_insider_transactions,
)
from tradingagents.dataflows.vendors.yahoo.market import (
    get_stock_stats_indicators_window,
    get_YFin_data_online,
)
from tradingagents.dataflows.vendors.yahoo.news import get_global_news_yfinance, get_news_yfinance

logger = logging.getLogger(__name__)

# Tools organized by category
TOOLS_CATEGORIES = {
    "core_stock_apis": {
        "description": "OHLCV stock price data",
        "tools": [
            "get_stock_data"
        ]
    },
    "technical_indicators": {
        "description": "Technical analysis indicators",
        "tools": [
            "get_indicators"
        ]
    },
    "fundamental_data": {
        "description": "Company fundamentals",
        "tools": [
            "get_fundamentals",
            "get_balance_sheet",
            "get_cashflow",
            "get_income_statement"
        ]
    },
    "news_data": {
        "description": "News and insider data",
        "tools": [
            "get_news",
            "get_global_news",
            "get_insider_transactions",
        ]
    },
    "macro_data": {
        "description": "Macroeconomic indicators (rates, inflation, labor, growth)",
        "tools": [
            "get_macro_indicators",
        ]
    },
    "prediction_markets": {
        "description": "Market-implied probabilities for forward-looking events",
        "tools": [
            "get_prediction_markets",
        ]
    }
}

# Optional enrichment categories. These add macro/event context to the news
# analyst but are not core to a decision, so a vendor failure here degrades to a
# sentinel instead of aborting the run (a bad LLM-supplied indicator, a missing
# key, or a network blip should not crash an analysis over flavour data). Core
# categories (prices, fundamentals, news) still raise so a broken primary is loud.
OPTIONAL_CATEGORIES = {"macro_data", "prediction_markets"}

# Mapping of methods to their vendor-specific implementations
VENDOR_METHODS = {
    # core_stock_apis
    "get_stock_data": {
        "alpha_vantage": get_alpha_vantage_stock,
        "yfinance": get_YFin_data_online,
    },
    # technical_indicators
    "get_indicators": {
        "alpha_vantage": get_alpha_vantage_indicator,
        "yfinance": get_stock_stats_indicators_window,
    },
    # fundamental_data
    "get_fundamentals": {
        "alpha_vantage": get_alpha_vantage_fundamentals,
        "yfinance": get_yfinance_fundamentals,
    },
    "get_balance_sheet": {
        "alpha_vantage": get_alpha_vantage_balance_sheet,
        "sec_edgar": get_sec_edgar_balance_sheet,
        "yfinance": get_yfinance_balance_sheet,
    },
    "get_cashflow": {
        "alpha_vantage": get_alpha_vantage_cashflow,
        "sec_edgar": get_sec_edgar_cashflow,
        "yfinance": get_yfinance_cashflow,
    },
    "get_income_statement": {
        "alpha_vantage": get_alpha_vantage_income_statement,
        "sec_edgar": get_sec_edgar_income_statement,
        "yfinance": get_yfinance_income_statement,
    },
    # news_data
    "get_news": {
        "alpha_vantage": get_alpha_vantage_news,
        "yfinance": get_news_yfinance,
    },
    "get_global_news": {
        "yfinance": get_global_news_yfinance,
        "alpha_vantage": get_alpha_vantage_global_news,
    },
    "get_insider_transactions": {
        "alpha_vantage": get_alpha_vantage_insider_transactions,
        "yfinance": get_yfinance_insider_transactions,
    },
    # macro_data
    "get_macro_indicators": {
        "fred": get_fred_macro_data,
    },
    # prediction_markets
    "get_prediction_markets": {
        "polymarket": get_polymarket_prediction_markets,
    },
}


def get_category_for_method(method: str) -> str:
    """Get the category that contains the specified method."""
    for category, info in TOOLS_CATEGORIES.items():
        if method in info["tools"]:
            return category
    raise ValueError(f"Method '{method}' not found in any category")


def get_vendor(category: str, method: str = None) -> str:
    """Get the configured vendor for a data category or specific tool method.
    Tool-level configuration takes precedence over category-level.
    """
    config = get_config()

    # Check tool-level configuration first (if method provided)
    if method:
        tool_vendors = config.get("tool_vendors", {})
        if method in tool_vendors:
            return tool_vendors[method]

    # Fall back to category-level configuration
    return config.get("data_vendors", {}).get(category, "default")


def vendor_unavailable(method: str, error: Exception) -> str:
    """What a call returns when every vendor was throttled or unreachable."""
    return (
        f"DATA_UNAVAILABLE: no configured vendor could serve {method} right now "
        f"({error}). This says nothing about the instrument; report the "
        f"data as unavailable and do not estimate or fabricate values."
    )


def no_data_available(error: NoMarketDataError) -> str:
    """What a call returns when every vendor that answered had no usable data."""
    resolved = "" if error.canonical == error.symbol else f" (resolved to '{error.canonical}')"
    # Surface the typed error's detail (e.g. "latest row is 2025-06-11 ...
    # stale") so the agent sees the specific reason — invalid symbol, no
    # coverage, or stale data — not just a generic "unavailable".
    reason = f" ({error.detail})" if error.detail else ""
    return (
        f"NO_DATA_AVAILABLE: No usable market data for '{error.symbol}'{resolved} from "
        f"any configured vendor{reason}. The symbol may be invalid, delisted, "
        f"not covered, or the vendor returned stale data. Do not estimate or "
        f"fabricate values — report that data is unavailable for this symbol."
    )


def _call_vendor(method: str, vendor: str, args, kwargs):
    """One vendor call: (payload, None) on answer, (None, error) on failure."""
    impl = VENDOR_METHODS[method][vendor]
    func = impl[0] if isinstance(impl, list) else impl
    try:
        return func(*args, **kwargs), None
    except VendorUnavailableError as e:
        logger.warning("Vendor %r unavailable for %s: %s", vendor, method, e)
        return None, e
    except VendorNotConfiguredError as e:
        logger.warning("Vendor %r not configured for %s", vendor, method)
        return None, e
    except NoMarketDataError as e:
        return None, e
    except Exception as e:
        logger.warning("Vendor %r failed for %s: %s", vendor, method, e)
        return None, e


def _merge_payloads(pairs: list[tuple[str, object]]):
    """Combine vendor answers without ever dropping the primary's content.

    The first vendor in the group is the primary: its answer stands, and later
    vendors contribute only what it does not already carry. A key the primary
    set is never overwritten, and text already present is not repeated.
    """
    usable = [(vendor, payload) for vendor, payload in pairs if not _is_gap(payload)]
    if not usable:
        return None
    _, primary = usable[0]
    if len(usable) == 1:
        return primary

    if isinstance(primary, dict):
        merged = dict(primary)
        for vendor, payload in usable[1:]:
            if isinstance(payload, dict):
                for key, value in payload.items():
                    merged.setdefault(key, value)
            elif str(payload).strip():
                merged.setdefault(f"supplement_{vendor}", payload)
        return merged

    if isinstance(primary, str):
        text = primary
        for vendor, payload in usable[1:]:
            extra = payload if isinstance(payload, str) else str(payload)
            if extra.strip() and extra.strip() not in text:
                text += f"\n\n---\n\n## Supplemental data from {vendor}\n\n{extra.strip()}"
        return text

    return primary


def _route_with_merge(method: str, vendor_config: str, *args, **kwargs):
    """Run every vendor in each '+' group and merge, then fall to the next group.

    Every vendor in a group is called on every run, so a healthy primary still
    receives whatever the others can add. A group that yields nothing usable
    moves the call to the next comma-separated group.
    """
    category = get_category_for_method(method)
    if method not in VENDOR_METHODS:
        raise ValueError(f"Method '{method}' not supported")
    available = VENDOR_METHODS[method]

    groups = [
        [name.strip() for name in part.split("+") if name.strip()]
        for part in vendor_config.split(",")
    ]
    # A vendor named at the category level may have no implementation for this
    # tool (sec_edgar serves statements but not the overview), so drop it here
    # rather than failing a chain that is valid for its siblings.
    groups = [
        [name for name in group if name == "default" or name in available]
        for group in groups
    ]
    groups = [group for group in groups if group]
    if not groups:
        raise ValueError(
            f"Configured vendor(s) {vendor_config} not available for '{method}'. "
            f"Available: {list(available)}."
        )
    if not any(name != "default" for group in groups for name in group):
        groups = [list(available)]

    last_gap = None
    last_no_data: NoMarketDataError | None = None
    last_unavailable: VendorUnavailableError | None = None
    first_error: Exception | None = None

    for group in groups:
        names = [name for name in group if name in available]
        if not names:
            continue
        pairs = []
        for vendor in names:
            payload, error = _call_vendor(method, vendor, args, kwargs)
            if error is not None:
                if isinstance(error, VendorUnavailableError):
                    last_unavailable = error
                elif isinstance(error, NoMarketDataError):
                    last_no_data = error
                if first_error is None:
                    first_error = error
                continue
            pairs.append((vendor, payload))
        merged = _merge_payloads(pairs)
        if merged is not None and not _is_gap(merged):
            return merged
        if merged is not None:
            last_gap = merged

    if last_unavailable is not None:
        return vendor_unavailable(method, last_unavailable)
    if last_gap is not None:
        return last_gap
    if last_no_data is not None:
        return no_data_available(last_no_data)
    if first_error is not None:
        if category in OPTIONAL_CATEGORIES:
            logger.warning("Optional %s unavailable for %s: %s", category, method, first_error)
            return (
                f"DATA_UNAVAILABLE: optional {category} could not be retrieved "
                f"({first_error}). Proceed without it; do not fabricate values."
            )
        raise first_error
    raise RuntimeError(f"No available vendor for '{method}'")


# A vendor answer that carries no data: a point-in-time withholding notice, an
# all-vendor-unavailable sentinel, or an empty payload. These are the gaps a
# later vendor in the chain is allowed to fill.
_GAP_MARKERS = ("# Point-in-time as of:", "DATA_UNAVAILABLE", "NO_DATA_AVAILABLE")


def _is_gap(result) -> bool:
    """True when a vendor answered without serving any data."""
    if result is None:
        return True
    if isinstance(result, dict):
        return not result
    if isinstance(result, str):
        text = result.strip()
        return not text or any(marker in text[:400] for marker in _GAP_MARKERS)
    return False


def route_to_vendor(method: str, *args, **kwargs):
    """Route method calls to appropriate vendor implementation with fallback support."""
    category = get_category_for_method(method)
    vendor_config = get_vendor(category, method)
    if "+" in str(vendor_config):
        return _route_with_merge(method, str(vendor_config), *args, **kwargs)
    primary_vendors = [v.strip() for v in vendor_config.replace("+", ",").split(",")]

    if method not in VENDOR_METHODS:
        raise ValueError(f"Method '{method}' not supported")

    all_available_vendors = list(VENDOR_METHODS[method].keys())

    # The configured vendor list IS the chain: we do NOT silently fall back to
    # vendors the user did not choose (#988/#289) — that returned data from an
    # unexpected source and caused cross-vendor inconsistencies. For multi-vendor
    # fallback, list them in order, e.g. data_vendors="yfinance,alpha_vantage".
    # The "default" sentinel (no explicit config) uses all available vendors.
    explicit = [v for v in primary_vendors if v and v != "default"]
    if explicit:
        vendor_chain = [v for v in explicit if v in VENDOR_METHODS[method]]
        if not vendor_chain:
            raise ValueError(
                f"Configured vendor(s) {explicit} not available for '{method}'. "
                f"Available: {all_available_vendors}."
            )
    else:
        vendor_chain = all_available_vendors

    last_no_data: NoMarketDataError | None = None
    last_unavailable: VendorUnavailableError | None = None
    failed: Exception | None = None     # a vendor that raised something untyped
    first_error: Exception | None = None
    gap_result: str | None = None    # last answer that carried no data
    for vendor in vendor_chain:
        vendor_impl = VENDOR_METHODS[method][vendor]
        impl_func = vendor_impl[0] if isinstance(vendor_impl, list) else vendor_impl

        try:
            result = impl_func(*args, **kwargs)
        except VendorUnavailableError as e:
            logger.warning("Vendor %r unavailable for %s: %s; trying next vendor.", vendor, method, e)
            # Kept so an all-unavailable chain can say the vendor was the
            # problem, rather than reporting nothing about the symbol.
            last_unavailable = e
            continue
        except VendorNotConfiguredError as e:
            logger.warning("Vendor %r not configured for %s; trying next vendor.", vendor, method)
            if first_error is None:
                first_error = e  # Surface it if no other vendor can serve the call.
            continue
        except NoMarketDataError as e:
            last_no_data = e  # No data here; another configured vendor may have it
            continue
        except Exception as e:
            # Don't let one vendor's failure crash the call when another can
            # serve it, but never swallow silently: a broken primary must be
            # visible in the logs (#989), not hidden behind a fallback's verdict.
            logger.warning("Vendor %r failed for %s: %s", vendor, method, e)
            if first_error is None:
                first_error = e
            failed = e
            continue
        if _is_gap(result):
            logger.info(
                "Vendor %r answered %s with no data; the next vendor in the chain may fill it.",
                vendor, method,
            )
            gap_result = result
            continue
        return result

    # A vendor that throttled or failed the request never said whether it has
    # the symbol, so no other vendor's "no data" can speak for the whole chain:
    # report the vendors as the problem, not the instrument. It must not end
    # the run either.
    if last_unavailable is not None:
        return vendor_unavailable(method, last_unavailable)
    if failed is not None and (last_no_data is not None or gap_result is not None):
        return vendor_unavailable(method, failed)

    # Every vendor that answered reported "no data": the symbol is genuinely unavailable.
    # Return one explicit, instructive sentinel rather than a vendor-specific
    # empty string, so the agent reports "unavailable" instead of inventing a
    # value. This takes precedence over incidental fallback errors.
    if last_no_data is not None:
        if first_error is not None:
            # A vendor also hit a real error; surface it in logs so the no-data
            # verdict can't hide a broken primary (network/auth/etc.).
            logger.warning(
                "Returning NO_DATA for %s, but a vendor errored earlier: %s",
                method, first_error,
            )
        return no_data_available(last_no_data)

    # Every vendor answered without serving data: hand back the last notice so
    # the agent sees the real reason (point-in-time withholding included).
    if gap_result is not None:
        return gap_result

    # No vendor returned data and none reported clean "no data" — surface the
    # first real error (e.g. the primary vendor's network failure). Optional
    # enrichment categories degrade to a sentinel instead, so flavour data can't
    # abort the run.
    if first_error is not None:
        if category in OPTIONAL_CATEGORIES:
            logger.warning("Optional %s unavailable for %s: %s", category, method, first_error)
            return (
                f"DATA_UNAVAILABLE: optional {category} could not be retrieved "
                f"({first_error}). Proceed without it; do not fabricate values."
            )
        raise first_error

    raise RuntimeError(f"No available vendor for '{method}'")
