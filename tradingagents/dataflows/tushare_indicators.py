"""Technical indicators calculated from Tushare daily OHLCV."""

from __future__ import annotations

import pandas as pd
from dateutil.relativedelta import relativedelta
from stockstats import wrap

from .errors import NoMarketDataError
from .tushare_price import get_price_data, normalize_symbol

SUPPORTED_INDICATORS = {
    "close_50_sma",
    "close_200_sma",
    "close_10_ema",
    "macd",
    "macds",
    "macdh",
    "rsi",
    "boll",
    "boll_ub",
    "boll_lb",
    "atr",
    "vwma",
    "mfi",
}


def _indicator_frame(symbol: str, indicator: str, curr_date: str) -> pd.DataFrame:
    end = pd.Timestamp(curr_date).normalize()
    # Three calendar years comfortably covers the 200-trading-day warm-up.
    start = end - relativedelta(years=3)
    data = get_price_data(
        symbol,
        start.strftime("%Y-%m-%d"),
        end.strftime("%Y-%m-%d"),
    ).reset_index()
    data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
    data = data.dropna(subset=["Date", "Close"])
    data = data[data["Date"] <= end].sort_values("Date").reset_index(drop=True)
    if data.empty:
        raise NoMarketDataError(symbol, normalize_symbol(symbol), "no rows for indicators")

    stock_df = wrap(data.copy())
    stock_df[indicator]  # triggers stockstats calculation
    result = data[["Date"]].copy()
    result["Value"] = pd.to_numeric(stock_df[indicator], errors="coerce").to_numpy()
    return result


def get_stock_stats_indicators_window(
    symbol: str,
    indicator: str,
    curr_date: str,
    look_back_days: int = 30,
) -> str:
    """Return one supported indicator for each day in the requested window."""
    selected = indicator.strip().lower()
    if selected not in SUPPORTED_INDICATORS:
        raise ValueError(
            f"Indicator {indicator!r} is not supported. Choose from: "
            f"{sorted(SUPPORTED_INDICATORS)}"
        )

    end = pd.Timestamp(curr_date).normalize()
    window = max(0, int(look_back_days))
    start = end - pd.Timedelta(days=window)
    values = _indicator_frame(symbol, selected, curr_date)
    values = values[(values["Date"] >= start) & (values["Date"] <= end)]
    by_date = {
        row.Date.strftime("%Y-%m-%d"): row.Value
        for row in values.itertuples(index=False)
        if not pd.isna(row.Value)
    }

    lines = [
        f"## {selected} values from {start.strftime('%Y-%m-%d')} "
        f"to {end.strftime('%Y-%m-%d')}:",
        "",
    ]
    current = end
    while current >= start:
        date_str = current.strftime("%Y-%m-%d")
        value = by_date.get(date_str)
        rendered = f"{value:.6g}" if value is not None else "N/A: Not a trading day or warm-up"
        lines.append(f"{date_str}: {rendered}")
        current -= pd.Timedelta(days=1)

    lines += [
        "",
        "Calculated with stockstats from unadjusted Tushare daily OHLCV; "
        "no unsupported indicator is substituted with the latest close.",
    ]
    return "\n".join(lines)
