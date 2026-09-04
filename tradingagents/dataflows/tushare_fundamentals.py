"""Point-in-time-safe Tushare fundamentals adapters."""

from __future__ import annotations

import pandas as pd
from dateutil.relativedelta import relativedelta

from .errors import NoMarketDataError
from .tushare_stock import _normalize_ts_code, _request_api


def normalize_symbol(symbol: str) -> str:
    return _normalize_ts_code(symbol)


def _cutoff(curr_date: str | None) -> pd.Timestamp:
    value = pd.Timestamp(curr_date) if curr_date else pd.Timestamp.today()
    return value.normalize()


def _response_frame(response: dict, symbol: str, ts_code: str, api_name: str) -> pd.DataFrame:
    raw = response.get("data") or {}
    fields = raw.get("fields") or []
    items = raw.get("items") or []
    if not fields or not items:
        raise NoMarketDataError(symbol, ts_code, f"no {api_name} rows returned")
    return pd.DataFrame(items, columns=fields)


def _filter_point_in_time(
    df: pd.DataFrame,
    cutoff: pd.Timestamp,
    freq: str,
) -> pd.DataFrame:
    """Keep only reports actually announced by the analysis date."""
    normalized_freq = freq.strip().lower()
    if normalized_freq not in {"annual", "quarterly"}:
        raise ValueError("freq must be 'annual' or 'quarterly'")

    announced = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
    if "ann_date" in df.columns:
        announced = pd.to_datetime(df["ann_date"], format="%Y%m%d", errors="coerce")
    if "f_ann_date" in df.columns:
        actual = pd.to_datetime(df["f_ann_date"], format="%Y%m%d", errors="coerce")
        announced = actual.fillna(announced)
    if announced.isna().all():
        raise ValueError("Tushare fundamentals response has no usable announcement date")

    if "end_date" not in df.columns:
        raise ValueError("Tushare fundamentals response has no report-period end_date")
    periods = pd.to_datetime(df["end_date"], format="%Y%m%d", errors="coerce")
    filtered = df[(announced <= cutoff) & (periods <= cutoff)].copy()
    filtered["_announced"] = announced.loc[filtered.index]
    filtered["_period"] = periods.loc[filtered.index]
    if normalized_freq == "annual":
        filtered = filtered[
            (filtered["_period"].dt.month == 12) & (filtered["_period"].dt.day == 31)
        ]

    # Revised statements can share both a period and an announcement date.
    # ``update_flag=1`` marks the latest revision, but it is only a tie-breaker
    # after the point-in-time cutoff: globally filtering to it could discard the
    # version that was actually available at an earlier analysis date.
    revision_rank = pd.Series(0, index=filtered.index, dtype="int8")
    if "update_flag" in filtered.columns:
        revision_rank = (
            pd.to_numeric(filtered["update_flag"], errors="coerce")
            .eq(1)
            .astype("int8")
        )
    filtered["_revision_rank"] = revision_rank

    # Prefer the latest public announcement, then the latest revision when two
    # versions were announced on the same day. ``mergesort`` keeps exact ties
    # stable without making normal, non-revision records depend on this key.
    filtered = filtered.sort_values(
        ["_period", "_announced", "_revision_rank"], kind="mergesort"
    )
    filtered = filtered.drop_duplicates(subset=["end_date"], keep="last")
    filtered = filtered.sort_values("_period", ascending=False).head(8)
    return filtered.drop(columns=["_announced", "_period", "_revision_rank"])


def _query_report(
    api_name: str,
    symbol: str,
    freq: str,
    curr_date: str | None,
) -> tuple[pd.DataFrame, str, pd.Timestamp]:
    ts_code = normalize_symbol(symbol)
    cutoff = _cutoff(curr_date)
    start = cutoff - relativedelta(years=8)
    response = _request_api(
        api_name,
        {
            "ts_code": ts_code,
            "start_date": start.strftime("%Y%m%d"),
            "end_date": cutoff.strftime("%Y%m%d"),
        },
    )
    frame = _response_frame(response, symbol, ts_code, api_name)
    frame = _filter_point_in_time(frame, cutoff, freq)
    if frame.empty:
        raise NoMarketDataError(
            symbol,
            ts_code,
            f"no {api_name} reports announced by {cutoff.strftime('%Y-%m-%d')}",
        )
    return frame, ts_code, cutoff


def _format_report(title: str, df: pd.DataFrame, cutoff: pd.Timestamp) -> str:
    return (
        f"# {title}\n"
        f"# Point-in-time cutoff: {cutoff.strftime('%Y-%m-%d')} "
        "(filtered by actual/announced date)\n\n"
        + df.to_csv(index=False).strip()
    )


def get_fundamentals(symbol: str, curr_date: str | None = None) -> str:
    """Return Tushare financial indicators known by ``curr_date``."""
    df, ts_code, cutoff = _query_report(
        "fina_indicator", symbol, "quarterly", curr_date
    )
    return _format_report(f"Company Fundamentals for {ts_code}", df, cutoff)


def get_income_statement(
    symbol: str,
    freq: str = "quarterly",
    curr_date: str | None = None,
) -> str:
    df, ts_code, cutoff = _query_report("income", symbol, freq, curr_date)
    return _format_report(f"{freq.title()} Income Statement for {ts_code}", df, cutoff)


def get_balance_sheet(
    symbol: str,
    freq: str = "quarterly",
    curr_date: str | None = None,
) -> str:
    df, ts_code, cutoff = _query_report("balancesheet", symbol, freq, curr_date)
    return _format_report(f"{freq.title()} Balance Sheet for {ts_code}", df, cutoff)


def get_cashflow(
    symbol: str,
    freq: str = "quarterly",
    curr_date: str | None = None,
) -> str:
    df, ts_code, cutoff = _query_report("cashflow", symbol, freq, curr_date)
    return _format_report(f"{freq.title()} Cash Flow for {ts_code}", df, cutoff)
