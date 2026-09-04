"""DataFrame compatibility wrapper for the routed Tushare price adapter."""

from __future__ import annotations

from io import StringIO

import pandas as pd

from .tushare_stock import _normalize_ts_code, get_stock


def normalize_symbol(symbol: str) -> str:
    """Normalize A-share symbols, including Yahoo's ``.SS`` spelling."""
    return _normalize_ts_code(symbol)


def get_price_data(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Return unadjusted daily OHLCV without requiring the Tushare SDK."""
    csv_data = get_stock(symbol, start, end)
    df = pd.read_csv(StringIO(csv_data))
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    for column in ("Open", "High", "Low", "Close", "Volume"):
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=["Date", "Close"]).sort_values("Date")
    return df.set_index("Date")[["Open", "High", "Low", "Close", "Volume"]]
