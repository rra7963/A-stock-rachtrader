from __future__ import annotations

import os
from datetime import datetime
from io import StringIO
from pathlib import Path

import pandas as pd
from dateutil.relativedelta import relativedelta
from stockstats import wrap

from .config import get_config
from .errors import NoMarketDataError, VendorNotConfiguredError
from .stockstats_utils import _assert_ohlcv_not_stale
from .utils import safe_ticker_component

READ_ONLY_OPTIONS = "-c default_transaction_read_only=on -c statement_timeout=15000"
QUERY_DAILY_QUOTES = """
SELECT trade_date, open, high, low, close, volume, amount
FROM daily_quotes
WHERE ts_code = %s
  AND trade_date >= %s
  AND trade_date <= %s
ORDER BY trade_date ASC
"""
QUERY_IDENTITY = """
SELECT sb.name, sb.industry, sb.market
FROM stock_basic sb
WHERE sb.ts_code = %s
LIMIT 1
"""
CACHE_SUBDIR = "aliyun_stock_postgres"


class AliyunStockPostgresNotConfiguredError(VendorNotConfiguredError):
    """Raised when the Aliyun stock PostgreSQL source is missing configuration."""


def _required_config() -> dict[str, str]:
    keys = {
        "host": "ALIYUN_STOCK_POSTGRES_HOST",
        "port": "ALIYUN_STOCK_POSTGRES_PORT",
        "dbname": "ALIYUN_STOCK_POSTGRES_DB",
        "user": "ALIYUN_STOCK_POSTGRES_USER",
        "password": "ALIYUN_STOCK_POSTGRES_PASSWORD",
    }
    values: dict[str, str] = {}
    missing: list[str] = []
    for field, env_var in keys.items():
        value = os.getenv(env_var)
        if value:
            values[field] = value
        else:
            missing.append(env_var)
    if missing:
        raise AliyunStockPostgresNotConfiguredError(
            f"{', '.join(missing)} environment variable(s) are not set."
        )
    return values


def _normalize_ts_code(symbol: str) -> str:
    candidate = symbol.strip().upper()
    if candidate.endswith(".SS"):
        return candidate[:-3] + ".SH"
    if candidate.endswith((".SH", ".SZ", ".BJ")):
        return candidate
    if candidate.isdigit() and len(candidate) == 6:
        if candidate.startswith("6"):
            return f"{candidate}.SH"
        if candidate.startswith(("0", "3")):
            return f"{candidate}.SZ"
        if candidate.startswith(("4", "8")):
            return f"{candidate}.BJ"
    return candidate


def _sql_date(value: str) -> str:
    return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")


def _cache_path(ts_code: str, start_date: str, end_date: str) -> Path:
    cache_dir = Path(get_config()["data_cache_dir"]) / CACHE_SUBDIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe_symbol = safe_ticker_component(ts_code)
    return cache_dir / f"{safe_symbol}-{start_date}-{end_date}.csv"


def _read_cached_csv(path: Path) -> str | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    return path.read_text(encoding="utf-8").strip()


def _connect(config: dict[str, str]):
    try:
        import psycopg
    except ImportError as exc:
        raise AliyunStockPostgresNotConfiguredError(
            "psycopg is required for aliyun_stock_postgres; install "
            "'psycopg[binary]' or install this package with its declared dependencies."
        ) from exc

    return psycopg.connect(
        host=config["host"],
        port=int(config["port"]),
        dbname=config["dbname"],
        user=config["user"],
        password=config["password"],
        options=READ_ONLY_OPTIONS,
    )


def _fetch_daily_quotes(
    config: dict[str, str],
    ts_code: str,
    start_date: str,
    end_date: str,
) -> list[tuple]:
    conn = _connect(config)
    try:
        with conn.cursor() as cur:
            cur.execute("BEGIN READ ONLY")
            try:
                cur.execute(QUERY_DAILY_QUOTES, (ts_code, start_date, end_date))
                return cur.fetchall()
            finally:
                cur.execute("ROLLBACK")
    finally:
        conn.close()


def _format_daily_csv(rows: list[tuple], symbol: str, ts_code: str, end_date: str) -> str:
    if not rows:
        raise NoMarketDataError(
            symbol,
            ts_code,
            f"no rows between the requested dates and {end_date}",
        )

    df = pd.DataFrame(
        rows,
        columns=["Date", "Open", "High", "Low", "Close", "Volume", "Amount"],
    )
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date", "Close"])
    if df.empty:
        raise NoMarketDataError(symbol, ts_code, "daily rows did not contain usable dates/prices")

    _assert_ohlcv_not_stale(df, end_date, symbol, ts_code)

    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
    return df[["Date", "Open", "High", "Low", "Close", "Volume", "Amount"]].to_csv(
        index=False
    ).strip()


def get_stock(symbol: str, start_date: str, end_date: str) -> str:
    """Return Aliyun PostgreSQL daily A-share OHLCV data as CSV."""
    ts_code = _normalize_ts_code(symbol)
    start = _sql_date(start_date)
    end = _sql_date(end_date)
    config = _required_config()
    cache_file = _cache_path(ts_code, start, end)
    cached = _read_cached_csv(cache_file)
    if cached:
        return cached
    rows = _fetch_daily_quotes(config, ts_code, start, end)
    csv_data = _format_daily_csv(rows, symbol, ts_code, end)
    cache_file.write_text(csv_data + "\n", encoding="utf-8")
    return csv_data


def get_indicators(
    symbol: str,
    indicator: str,
    curr_date: str,
    look_back_days: int = 30,
) -> str:
    """Calculate technical indicators locally from Aliyun PostgreSQL OHLCV."""
    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    start_date = (curr_dt - relativedelta(years=5)).strftime("%Y-%m-%d")
    df = pd.read_csv(StringIO(get_stock(symbol, start_date, curr_date)), comment="#")
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    for column in ("Open", "High", "Low", "Close", "Volume"):
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=["Date", "Close"]).sort_values("Date")

    selected = indicator.strip().lower()
    stock_df = wrap(df.copy())
    stock_df[selected]  # trigger stockstats calculation
    stock_df["Date"] = pd.to_datetime(stock_df["Date"], errors="coerce")
    window_start = curr_dt - relativedelta(days=look_back_days)
    window = stock_df[(stock_df["Date"] >= window_start) & (stock_df["Date"] <= curr_dt)]
    by_date = {row["Date"].strftime("%Y-%m-%d"): row[selected] for _, row in window.iterrows()}

    lines = [f"## {selected} values from {window_start.strftime('%Y-%m-%d')} to {curr_date}:", ""]
    cursor = curr_dt
    while cursor >= window_start:
        date_str = cursor.strftime("%Y-%m-%d")
        value = by_date.get(date_str, "N/A: Not a trading day (weekend or holiday)")
        lines.append(f"{date_str}: {value}")
        cursor -= relativedelta(days=1)
    return "\n".join(lines)


def get_instrument_identity(symbol: str) -> dict[str, str] | None:
    """Return deterministic A-share identity metadata without using Yahoo."""
    ts_code = _normalize_ts_code(symbol)
    if not ts_code.endswith((".SH", ".SZ", ".BJ")):
        return None

    config = _required_config()
    conn = _connect(config)
    try:
        with conn.cursor() as cur:
            cur.execute("BEGIN READ ONLY")
            try:
                cur.execute(QUERY_IDENTITY, (ts_code,))
                row = cur.fetchone()
            finally:
                cur.execute("ROLLBACK")
    finally:
        conn.close()

    if row is None:
        return None

    name, industry, market = row
    exchange = {".SH": "SSE", ".SZ": "SZSE", ".BJ": "BSE"}[ts_code[-3:]]
    return {
        key: value
        for key, value in {
            "company_name": name,
            "industry": industry,
            "market": market,
            "exchange": exchange,
        }.items()
        if value
    }
