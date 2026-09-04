import json
import os
from datetime import datetime

import pandas as pd
import requests

from .errors import NoMarketDataError, VendorNotConfiguredError, VendorRateLimitError
from .symbol_utils import normalize_a_share_symbol

API_BASE_URL = "http://api.tushare.pro"
REQUEST_TIMEOUT = 30
FIELDS = "ts_code,trade_date,open,high,low,close,vol,amount"


class TushareNotConfiguredError(VendorNotConfiguredError):
    """Raised when Tushare is selected but no token is configured."""


class TushareRateLimitError(VendorRateLimitError):
    """Raised when Tushare reports a quota or frequency limit."""


def _get_token() -> str:
    token = os.getenv("TUSHARE_TOKEN")
    if not token:
        raise TushareNotConfiguredError(
            "TUSHARE_TOKEN environment variable is not set."
        )
    return token


def _normalize_ts_code(symbol: str) -> str:
    return normalize_a_share_symbol(symbol) or symbol.strip().upper()


def _api_date(value: str) -> str:
    return datetime.strptime(value, "%Y-%m-%d").strftime("%Y%m%d")


def _request_api(
    api_name: str,
    params: dict[str, str],
    *,
    fields: str | None = None,
) -> dict:
    """Call a Tushare Pro HTTP endpoint with lazy authentication.

    Keeping the token lookup inside the request path lets the package import and
    the vendor router fall back normally when Tushare is not configured.
    """
    payload = {
        "api_name": api_name,
        "token": _get_token(),
        "params": params,
    }
    if fields is not None:
        payload["fields"] = fields
    response = requests.post(
        API_BASE_URL,
        data=json.dumps(payload),
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()

    code = data.get("code")
    msg = data.get("msg") or ""
    if code == 0:
        return data
    if code in (2002, 2010, 40101) or "token" in msg.lower():
        raise TushareNotConfiguredError(f"Tushare authorization failed: {msg}")
    if code in (2003, 2004) or "频" in msg or "limit" in msg.lower():
        raise TushareRateLimitError(f"Tushare rate limit exceeded: {msg}")
    raise ValueError(f"Tushare request failed with code {code}: {msg}")


def _request_daily(ts_code: str, start_date: str, end_date: str) -> dict:
    return _request_api(
        "daily",
        {
            "ts_code": ts_code,
            "start_date": _api_date(start_date),
            "end_date": _api_date(end_date),
        },
        fields=FIELDS,
    )


def _request_index_daily(ts_code: str, start_date: str, end_date: str) -> dict:
    """Fetch an explicitly identified A-share benchmark index."""
    return _request_api(
        "index_daily",
        {
            "ts_code": ts_code,
            "start_date": _api_date(start_date),
            "end_date": _api_date(end_date),
        },
        fields=FIELDS,
    )


def _request_fund_daily(ts_code: str, start_date: str, end_date: str) -> dict:
    """Fetch an explicitly identified exchange-traded fund benchmark."""
    return _request_api(
        "fund_daily",
        {
            "ts_code": ts_code,
            "start_date": _api_date(start_date),
            "end_date": _api_date(end_date),
        },
        fields=FIELDS,
    )


def _request_adjustment_factors(
    ts_code: str,
    start_date: str,
    end_date: str,
) -> dict:
    """Return corporate-action factors for an A-share stock."""
    return _request_api(
        "adj_factor",
        {
            "ts_code": ts_code,
            "start_date": _api_date(start_date),
            "end_date": _api_date(end_date),
        },
        fields="ts_code,trade_date,adj_factor",
    )


def _format_daily_csv(data: dict, symbol: str, ts_code: str) -> str:
    raw = data.get("data") or {}
    fields = raw.get("fields") or []
    items = raw.get("items") or []
    if not items:
        raise NoMarketDataError(symbol, ts_code, "no daily rows returned")

    df = pd.DataFrame(items, columns=fields)
    df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")
    df = df.sort_values("trade_date")
    df = df.rename(
        columns={
            "trade_date": "Date",
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "vol": "Volume",
            "amount": "Amount",
        }
    )
    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
    return df[["Date", "Open", "High", "Low", "Close", "Volume", "Amount"]].to_csv(
        index=False
    ).strip()


def get_stock(symbol: str, start_date: str, end_date: str) -> str:
    """Return unadjusted Tushare daily A-share OHLCV data as CSV.

    The HTTP ``daily`` endpoint is explicitly unadjusted. We do not pass an
    ignored ``adj`` parameter or label these prices as qfq; Tushare's qfq
    ``pro_bar`` helper is SDK-only and is not used by this lightweight client.
    """
    ts_code = _normalize_ts_code(symbol)
    data = _request_daily(ts_code, start_date, end_date)
    return _format_daily_csv(data, symbol, ts_code)


def get_close_prices(symbol: str, start_date: str, end_date: str) -> pd.Series:
    """Return an ascending adjusted stock return series.

    Stock closes are forward-adjusted with Tushare ``adj_factor`` so dividends,
    splits, and other corporate actions do not create mechanical return jumps.
    Display-oriented OHLCV remains deliberately unadjusted in :func:`get_stock`.
    Use :func:`get_index_close_prices` for benchmark indices.
    """
    ts_code = _normalize_ts_code(symbol)
    data = _request_daily(ts_code, start_date, end_date)
    raw = data.get("data") or {}
    fields = raw.get("fields") or []
    items = raw.get("items") or []
    if not items:
        raise NoMarketDataError(symbol, ts_code, "no daily rows returned")
    frame = pd.DataFrame(items, columns=fields)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], format="%Y%m%d")
    frame = frame.sort_values("trade_date")
    closes = frame.set_index("trade_date")["close"].astype(float)

    adjustment_data = _request_adjustment_factors(ts_code, start_date, end_date)
    adjustment_raw = adjustment_data.get("data") or {}
    adjustment_fields = adjustment_raw.get("fields") or []
    adjustment_items = adjustment_raw.get("items") or []
    if not adjustment_items:
        raise NoMarketDataError(symbol, ts_code, "no adjustment factors returned")
    adjustments = pd.DataFrame(adjustment_items, columns=adjustment_fields)
    adjustments["trade_date"] = pd.to_datetime(
        adjustments["trade_date"], format="%Y%m%d"
    )
    factors = (
        adjustments.set_index("trade_date")["adj_factor"]
        .astype(float)
        .reindex(closes.index)
    )
    if factors.isna().any() or not float(factors.iloc[-1]):
        raise NoMarketDataError(symbol, ts_code, "incomplete adjustment-factor history")
    return closes * factors / float(factors.iloc[-1])


def get_index_close_prices(
    symbol: str,
    start_date: str,
    end_date: str,
) -> pd.Series:
    """Return ascending raw index levels from Tushare ``index_daily``."""
    ts_code = _normalize_ts_code(symbol)
    data = _request_index_daily(ts_code, start_date, end_date)
    raw = data.get("data") or {}
    fields = raw.get("fields") or []
    items = raw.get("items") or []
    if not items:
        raise NoMarketDataError(symbol, ts_code, "no index daily rows returned")
    frame = pd.DataFrame(items, columns=fields)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], format="%Y%m%d")
    return (
        frame.sort_values("trade_date")
        .set_index("trade_date")["close"]
        .astype(float)
    )


def get_fund_close_prices(
    symbol: str,
    start_date: str,
    end_date: str,
) -> pd.Series:
    """Return ascending raw ETF levels from Tushare ``fund_daily``."""
    ts_code = _normalize_ts_code(symbol)
    data = _request_fund_daily(ts_code, start_date, end_date)
    raw = data.get("data") or {}
    fields = raw.get("fields") or []
    items = raw.get("items") or []
    if not items:
        raise NoMarketDataError(symbol, ts_code, "no fund daily rows returned")
    frame = pd.DataFrame(items, columns=fields)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], format="%Y%m%d")
    return (
        frame.sort_values("trade_date")
        .set_index("trade_date")["close"]
        .astype(float)
    )


def get_benchmark_close_prices(
    symbol: str,
    start_date: str,
    end_date: str,
) -> pd.Series:
    """Route an A-share benchmark to its Tushare instrument endpoint.

    Exchange-qualified symbols alone do not identify an index: stocks, indices,
    and ETFs all share ``.SH``/``.SZ``/``.BJ``.  The exchange code families do:
    Shanghai ``000xxx``, Shenzhen ``399xxx``, and Beijing ``899xxx`` are index
    series; Shanghai ``5xxxxx`` and Shenzhen ``1xxxxx`` are exchange-traded
    funds.  Other supported codes use the adjusted stock-return path.
    """
    ts_code = _normalize_ts_code(symbol)
    code, separator, exchange = ts_code.partition(".")
    if separator and (
        (exchange == "SH" and code.startswith("000"))
        or (exchange == "SZ" and code.startswith("399"))
        or (exchange == "BJ" and code.startswith("899"))
    ):
        return get_index_close_prices(ts_code, start_date, end_date)
    if separator and (
        (exchange == "SH" and code.startswith("5"))
        or (exchange == "SZ" and code.startswith("1"))
    ):
        return get_fund_close_prices(ts_code, start_date, end_date)
    return get_close_prices(ts_code, start_date, end_date)


def get_instrument_identity(symbol: str) -> dict[str, str]:
    """Return basic A-share identity metadata from Tushare ``stock_basic``."""
    ts_code = _normalize_ts_code(symbol)
    data = _request_api(
        "stock_basic",
        {"ts_code": ts_code},
        fields="ts_code,name,industry,market,exchange",
    )
    raw = data.get("data") or {}
    fields = raw.get("fields") or []
    items = raw.get("items") or []
    if not items:
        return {}
    row = dict(zip(fields, items[0], strict=False))
    return {
        "company_name": row.get("name") or "",
        "industry": row.get("industry") or "",
        "sector": row.get("market") or "",
        "exchange": row.get("exchange") or "",
    }
