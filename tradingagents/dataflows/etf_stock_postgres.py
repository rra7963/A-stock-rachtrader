from __future__ import annotations

import os
from collections.abc import Iterable
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
CACHE_SUBDIR = "etf_stock_postgres"

QUERY_DAILY_QUOTES = """
SELECT trade_date, open, high, low, close, volume, amount
FROM daily_quotes
WHERE ts_code = %s
  AND trade_date >= %s
  AND trade_date <= %s
ORDER BY trade_date ASC
"""

QUERY_IDENTITY = """
SELECT sb.ts_code, sb.name, sb.industry, sb.market, sb.list_date, ci.main_business
FROM stock_basic sb
LEFT JOIN company_info ci ON ci.ts_code = sb.ts_code
WHERE sb.ts_code = %s
LIMIT 1
"""

QUERY_VALUATION_LATEST = """
SELECT total_mv, pe_ttm, pb, ps_ttm, trade_date
FROM valuation_indicators
WHERE ts_code = %s
  AND trade_date <= %s
ORDER BY trade_date DESC
LIMIT 1
"""

QUERY_FINANCIAL_STATEMENT_LATEST = """
SELECT ann_date, end_date, revenue, net_profit, total_assets, total_liabilities,
       basic_eps
FROM financial_statements
WHERE ts_code = %s
  AND ann_date <= %s
ORDER BY ann_date DESC NULLS LAST, end_date DESC
LIMIT 1
"""

QUERY_FINANCIAL_STATEMENTS = """
SELECT ann_date, end_date, revenue, operating_profit, total_profit, net_profit,
       basic_eps, total_assets, total_liabilities, total_equity,
       operating_cashflow, investing_cashflow, financing_cashflow, cash_end
FROM financial_statements
WHERE ts_code = %s
  AND ann_date <= %s
ORDER BY ann_date DESC NULLS LAST, end_date DESC
LIMIT %s
"""

QUERY_FINANCIAL_STATEMENTS_ANNUAL = """
SELECT ann_date, end_date, revenue, operating_profit, total_profit, net_profit,
       basic_eps, total_assets, total_liabilities, total_equity,
       operating_cashflow, investing_cashflow, financing_cashflow, cash_end
FROM financial_statements
WHERE ts_code = %s
  AND ann_date <= %s
  AND EXTRACT(MONTH FROM end_date) = 12
  AND EXTRACT(DAY FROM end_date) = 31
ORDER BY ann_date DESC NULLS LAST, end_date DESC
LIMIT %s
"""

QUERY_FINANCIAL_INDICATOR_LATEST = """
SELECT ann_date, end_date, roe, roa, net_margin, current_ratio, debt_to_assets,
       revenue_yoy, netprofit_yoy
FROM financial_indicators
WHERE ts_code = %s
  AND ann_date <= %s
ORDER BY ann_date DESC NULLS LAST, end_date DESC
LIMIT 1
"""

QUERY_FUNDAMENTAL_FACTOR_POOL_LATEST = """
SELECT ann_date, end_date, pe_ttm, pb, ps_ttm, pcf_ttm, dividend_yield_ttm,
       roe, roa, revenue_yoy, netprofit_yoy, debt_to_assets,
       institution_holding_ratio, fund_holding_ratio, holder_num
FROM stock_fundamental_factor_pool
WHERE ts_code = %s
  AND ann_date <= %s
ORDER BY ann_date DESC NULLS LAST, end_date DESC
LIMIT 1
"""

QUERY_OWNERSHIP_LATEST = """
SELECT ann_date, end_date, top10_total_holding_ratio, top10_float_holding_ratio,
       institution_holding_ratio, fund_holding_ratio, holder_num,
       avg_holding_per_account
FROM stock_ownership_factors
WHERE ts_code = %s
  AND ann_date <= %s
ORDER BY ann_date DESC NULLS LAST, end_date DESC
LIMIT 1
"""

QUERY_FACTOR_VALUES = """
SELECT factor_name, factor_value
FROM factor_values_daily
WHERE entity_type = 'stock'
  AND entity_code = %s
  AND asof_date = %s
  AND factor_name = ANY(%s)
ORDER BY factor_name
"""

QUERY_DAILY_QUOTES_LOOKBACK = """
SELECT trade_date, open, high, low, close, volume, amount
FROM daily_quotes
WHERE ts_code = %s
  AND trade_date <= %s
ORDER BY trade_date DESC
LIMIT %s
"""


class ETFStockPostgresNotConfiguredError(VendorNotConfiguredError):
    """Raised when the ETF stock PostgreSQL source is missing configuration."""


def _required_config() -> dict[str, str]:
    etf_keys = {
        "host": "ETF_STOCK_POSTGRES_HOST",
        "port": "ETF_STOCK_POSTGRES_PORT",
        "dbname": "ETF_STOCK_POSTGRES_DB",
        "user": "ETF_STOCK_POSTGRES_USER",
        "password": "ETF_STOCK_POSTGRES_PASSWORD",
    }
    aliyun_keys = {
        "host": "ALIYUN_STOCK_POSTGRES_HOST",
        "port": "ALIYUN_STOCK_POSTGRES_PORT",
        "dbname": "ALIYUN_STOCK_POSTGRES_DB",
        "user": "ALIYUN_STOCK_POSTGRES_USER",
        "password": "ALIYUN_STOCK_POSTGRES_PASSWORD",
    }

    def configured_values(keys: dict[str, str]) -> tuple[dict[str, str], list[str]]:
        values: dict[str, str] = {}
        missing: list[str] = []
        for field, env_var in keys.items():
            value = os.getenv(env_var)
            if value:
                values[field] = value
            else:
                missing.append(env_var)
        return values, missing

    values, missing = configured_values(etf_keys)
    if not missing:
        return values
    if len(missing) != len(etf_keys):
        raise ETFStockPostgresNotConfiguredError(
            f"{', '.join(missing)} environment variable(s) are not set."
        )

    values, missing = configured_values(aliyun_keys)
    if not missing:
        return values

    raise ETFStockPostgresNotConfiguredError(
        "ETF_STOCK_POSTGRES_HOST or ALIYUN_STOCK_POSTGRES_HOST environment variable(s) are not set."
    )


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
        raise ETFStockPostgresNotConfiguredError(
            "psycopg is required for etf_stock_postgres; install "
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


def _execute_fetchall(config: dict[str, str], sql: str, params: tuple) -> list[tuple]:
    conn = _connect(config)
    try:
        with conn.cursor() as cur:
            cur.execute("BEGIN READ ONLY")
            try:
                cur.execute(sql, params)
                return cur.fetchall()
            finally:
                cur.execute("ROLLBACK")
    finally:
        conn.close()


def _execute_fetchone(config: dict[str, str], sql: str, params: tuple) -> tuple | None:
    rows = _execute_fetchall(config, sql, params)
    return rows[0] if rows else None


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
    """Return ETF stock PostgreSQL daily A-share OHLCV data as CSV."""
    ts_code = _normalize_ts_code(symbol)
    start = _sql_date(start_date)
    end = _sql_date(end_date)
    config = _required_config()
    cache_file = _cache_path(ts_code, start, end)
    cached = _read_cached_csv(cache_file)
    if cached:
        return cached
    rows = _execute_fetchall(config, QUERY_DAILY_QUOTES, (ts_code, start, end))
    csv_data = _format_daily_csv(rows, symbol, ts_code, end)
    cache_file.write_text(csv_data + "\n", encoding="utf-8")
    return csv_data


def _fmt(value) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    if hasattr(value, "isoformat"):
        return str(value)
    return str(value)


def get_fundamentals(ticker: str, curr_date: str | None = None) -> str:
    """Return DB-backed A-share profile, valuation, and latest as-of financials."""
    as_of = _sql_date(curr_date or datetime.now().strftime("%Y-%m-%d"))
    ts_code = _normalize_ts_code(ticker)
    config = _required_config()

    identity = _execute_fetchone(config, QUERY_IDENTITY, (ts_code,))
    if identity is None:
        raise NoMarketDataError(ticker, ts_code, "stock_basic/company_info returned no rows")

    valuation = _execute_fetchone(config, QUERY_VALUATION_LATEST, (ts_code, as_of))
    statement = _execute_fetchone(config, QUERY_FINANCIAL_STATEMENT_LATEST, (ts_code, as_of))
    indicators = _execute_fetchone(config, QUERY_FINANCIAL_INDICATOR_LATEST, (ts_code, as_of))
    factor_pool = _execute_fetchone(config, QUERY_FUNDAMENTAL_FACTOR_POOL_LATEST, (ts_code, as_of))
    ownership = _execute_fetchone(config, QUERY_OWNERSHIP_LATEST, (ts_code, as_of))

    _, name, industry, market, list_date, main_business = identity
    lines = [
        f"## ETF stock PostgreSQL fundamentals for {ts_code}",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Name | {_fmt(name)} |",
        f"| Industry | {_fmt(industry)} |",
        f"| Market | {_fmt(market)} |",
        f"| List date | {_fmt(list_date)} |",
        f"| Main business | {_fmt(main_business)} |",
        "",
        f"### Latest valuation as of {as_of}",
        "",
        "| Trade date | Total market value | PE TTM | PB | PS TTM |",
        "|---|---:|---:|---:|---:|",
    ]
    if valuation:
        total_mv, pe_ttm, pb, ps_ttm, trade_date = valuation
        lines.append(
            f"| {_fmt(trade_date)} | {_fmt(total_mv)} | {_fmt(pe_ttm)} | {_fmt(pb)} | {_fmt(ps_ttm)} |"
        )
    else:
        lines.append("| N/A | N/A | N/A | N/A | N/A |")

    lines += [
        "",
        f"### Latest financial statement as of {as_of}",
        "",
        "| Ann date | End date | Revenue | Net profit | Total assets | Total liabilities | EPS |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    if statement:
        ann_date, end_date, revenue, net_profit, total_assets, total_liabilities, basic_eps = statement
        lines.append(
            f"| {_fmt(ann_date)} | {_fmt(end_date)} | {_fmt(revenue)} | {_fmt(net_profit)} | "
            f"{_fmt(total_assets)} | {_fmt(total_liabilities)} | {_fmt(basic_eps)} |"
        )
    else:
        lines.append("| N/A | N/A | N/A | N/A | N/A | N/A | N/A |")

    lines += [
        "",
        f"### Latest financial indicators as of {as_of}",
        "",
        "| Ann date | End date | ROE | ROA | Net margin | Current ratio | Debt/assets | Revenue YoY | Net profit YoY |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    if indicators:
        ann_date, end_date, roe, roa, net_margin, current_ratio, debt_to_assets, revenue_yoy, netprofit_yoy = indicators
        lines.append(
            f"| {_fmt(ann_date)} | {_fmt(end_date)} | {_fmt(roe)} | {_fmt(roa)} | "
            f"{_fmt(net_margin)} | {_fmt(current_ratio)} | {_fmt(debt_to_assets)} | "
            f"{_fmt(revenue_yoy)} | {_fmt(netprofit_yoy)} |"
        )
    else:
        lines.append("| N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |")

    lines += [
        "",
        f"### Latest fundamental factor pool as of {as_of}",
        "",
        "| Ann date | End date | PE TTM | PB | PS TTM | PCF TTM | Dividend yield TTM | ROE | ROA | Revenue YoY | Net profit YoY | Debt/assets | Institution holding | Fund holding | Holders |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    if factor_pool:
        (
            ann_date,
            end_date,
            pe_ttm,
            pb,
            ps_ttm,
            pcf_ttm,
            dividend_yield_ttm,
            roe,
            roa,
            revenue_yoy,
            netprofit_yoy,
            debt_to_assets,
            institution_holding_ratio,
            fund_holding_ratio,
            holder_num,
        ) = factor_pool
        lines.append(
            f"| {_fmt(ann_date)} | {_fmt(end_date)} | {_fmt(pe_ttm)} | {_fmt(pb)} | {_fmt(ps_ttm)} | "
            f"{_fmt(pcf_ttm)} | {_fmt(dividend_yield_ttm)} | {_fmt(roe)} | {_fmt(roa)} | "
            f"{_fmt(revenue_yoy)} | {_fmt(netprofit_yoy)} | {_fmt(debt_to_assets)} | "
            f"{_fmt(institution_holding_ratio)} | {_fmt(fund_holding_ratio)} | {_fmt(holder_num)} |"
        )
    else:
        lines.append("| N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |")

    lines += [
        "",
        f"### Latest ownership factors as of {as_of}",
        "",
        "| Ann date | End date | Top 10 total holding | Top 10 float holding | Institution holding | Fund holding | Holders | Average holding/account |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    if ownership:
        (
            ann_date,
            end_date,
            top10_total_holding_ratio,
            top10_float_holding_ratio,
            institution_holding_ratio,
            fund_holding_ratio,
            holder_num,
            avg_holding_per_account,
        ) = ownership
        lines.append(
            f"| {_fmt(ann_date)} | {_fmt(end_date)} | {_fmt(top10_total_holding_ratio)} | "
            f"{_fmt(top10_float_holding_ratio)} | {_fmt(institution_holding_ratio)} | "
            f"{_fmt(fund_holding_ratio)} | {_fmt(holder_num)} | {_fmt(avg_holding_per_account)} |"
        )
    else:
        lines.append("| N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |")

    lines.append("")
    lines.append("Financial rows are filtered with ann_date <= analysis date to avoid look-ahead bias.")
    return "\n".join(lines)


def _statement_rows(ticker: str, freq: str, curr_date: str | None) -> tuple[str, str, list[tuple]]:
    normalized_freq = freq.strip().lower()
    if normalized_freq not in {"annual", "quarterly"}:
        raise ValueError("freq must be 'annual' or 'quarterly'")

    as_of = _sql_date(curr_date or datetime.now().strftime("%Y-%m-%d"))
    ts_code = _normalize_ts_code(ticker)
    sql = QUERY_FINANCIAL_STATEMENTS_ANNUAL if normalized_freq == "annual" else QUERY_FINANCIAL_STATEMENTS
    rows = _execute_fetchall(_required_config(), sql, (ts_code, as_of, 4))
    return ts_code, as_of, rows


def get_balance_sheet(ticker: str, freq: str = "quarterly", curr_date: str | None = None) -> str:
    ts_code, as_of, rows = _statement_rows(ticker, freq, curr_date)
    lines = [
        f"## {freq.lower()} balance sheet for {ts_code} as of {as_of}",
        "",
        "| Ann date | End date | Total assets | Total liabilities | Total equity | Cash at period end |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for ann_date, end_date, _, _, _, _, _, total_assets, total_liabilities, total_equity, _, _, _, cash_end in rows:
        lines.append(
            f"| {_fmt(ann_date)} | {_fmt(end_date)} | {_fmt(total_assets)} | {_fmt(total_liabilities)} | "
            f"{_fmt(total_equity)} | {_fmt(cash_end)} |"
        )
    if not rows:
        lines.append("| N/A | N/A | N/A | N/A | N/A | N/A |")
    return "\n".join(lines)


def get_cashflow(ticker: str, freq: str = "quarterly", curr_date: str | None = None) -> str:
    ts_code, as_of, rows = _statement_rows(ticker, freq, curr_date)
    lines = [
        f"## {freq.lower()} cash flow statement for {ts_code} as of {as_of}",
        "",
        "| Ann date | End date | Operating cash flow | Investing cash flow | Financing cash flow | Cash at period end |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for ann_date, end_date, _, _, _, _, _, _, _, _, operating_cashflow, investing_cashflow, financing_cashflow, cash_end in rows:
        lines.append(
            f"| {_fmt(ann_date)} | {_fmt(end_date)} | {_fmt(operating_cashflow)} | "
            f"{_fmt(investing_cashflow)} | {_fmt(financing_cashflow)} | {_fmt(cash_end)} |"
        )
    if not rows:
        lines.append("| N/A | N/A | N/A | N/A | N/A | N/A |")
    return "\n".join(lines)


def get_income_statement(ticker: str, freq: str = "quarterly", curr_date: str | None = None) -> str:
    ts_code, as_of, rows = _statement_rows(ticker, freq, curr_date)
    lines = [
        f"## {freq.lower()} income statement for {ts_code} as of {as_of}",
        "",
        "| Ann date | End date | Revenue | Operating profit | Total profit | Net profit | EPS |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for ann_date, end_date, revenue, operating_profit, total_profit, net_profit, basic_eps, *_ in rows:
        lines.append(
            f"| {_fmt(ann_date)} | {_fmt(end_date)} | {_fmt(revenue)} | {_fmt(operating_profit)} | "
            f"{_fmt(total_profit)} | {_fmt(net_profit)} | {_fmt(basic_eps)} |"
        )
    if not rows:
        lines.append("| N/A | N/A | N/A | N/A | N/A | N/A | N/A |")
    return "\n".join(lines)


def _load_csv_ohlcv(csv_data: str) -> pd.DataFrame:
    df = pd.read_csv(StringIO(csv_data), comment="#")
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    for col in ("Open", "High", "Low", "Close", "Volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["Date", "Close"]).sort_values("Date")


def get_indicators(
    symbol: str,
    indicator: str,
    curr_date: str,
    look_back_days: int = 30,
) -> str:
    """Calculate technical indicators locally from ETF PostgreSQL OHLCV."""
    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    start_date = (curr_dt - relativedelta(years=5)).strftime("%Y-%m-%d")
    df = _load_csv_ohlcv(get_stock(symbol, start_date, curr_date))
    stock_df = wrap(df.copy())
    selected = indicator.strip().lower()
    stock_df[selected]  # trigger stockstats calculation
    stock_df["Date"] = pd.to_datetime(stock_df["Date"], errors="coerce")
    window_start = curr_dt - relativedelta(days=look_back_days)
    window = stock_df[(stock_df["Date"] >= window_start) & (stock_df["Date"] <= curr_dt)]
    lines = [
        f"## {selected} values from {window_start.strftime('%Y-%m-%d')} to {curr_date}:",
        "",
    ]
    by_date = {row["Date"].strftime("%Y-%m-%d"): row[selected] for _, row in window.iterrows()}
    cursor = curr_dt
    while cursor >= window_start:
        date_str = cursor.strftime("%Y-%m-%d")
        value = by_date.get(date_str, "N/A: Not a trading day (weekend or holiday)")
        lines.append(f"{date_str}: {value}")
        cursor -= relativedelta(days=1)
    return "\n".join(lines)


def _local_factor_values(df: pd.DataFrame) -> dict[str, float]:
    closes = pd.to_numeric(df["Close"], errors="coerce").reset_index(drop=True)
    result: dict[str, float] = {}
    if len(closes) >= 2 and closes.iloc[-2]:
        result["stock_ret_1d"] = float(closes.iloc[-1] / closes.iloc[-2] - 1)
    if len(closes) >= 6 and closes.iloc[-6]:
        result["stock_ret_5d"] = float(closes.iloc[-1] / closes.iloc[-6] - 1)
    if len(closes) >= 21 and closes.iloc[-21]:
        result["stock_ret_20d"] = float(closes.iloc[-1] / closes.iloc[-21] - 1)
    if len(closes) >= 5:
        ma5 = closes.tail(5).mean()
        if ma5:
            result["stock_close_vs_ma5"] = float(closes.iloc[-1] / ma5 - 1)
    if len(closes) >= 20:
        ma20 = closes.tail(20).mean()
        if ma20:
            result["stock_close_vs_ma20"] = float(closes.iloc[-1] / ma20 - 1)
    try:
        stock_df = wrap(df.copy())
        stock_df["macd"]
        stock_df["macds"]
        stock_df["macdh"]
        latest = stock_df.iloc[-1]
        result["stock_macd_dif"] = float(latest["macd"])
        result["stock_macd_dea"] = float(latest["macds"])
        # ETF factor_values_daily stores the conventional MACD histogram
        # (DIF - DEA) * 2, while stockstats exposes macdh as DIF - DEA.
        result["stock_macd"] = float(latest["macdh"]) * 2
    except Exception:
        pass
    return result


def compare_calculated_indicators_to_db(
    symbol: str,
    curr_date: str,
    indicators: Iterable[str] = ("stock_ret_1d", "stock_ret_5d", "stock_close_vs_ma5"),
    lookback_rows: int = 260,
    rel_tolerance: float = 0.02,
    abs_tolerance: float = 1e-6,
) -> str:
    """Compare locally calculated factors with factor_values_daily rows."""
    ts_code = _normalize_ts_code(symbol)
    as_of = _sql_date(curr_date)
    config = _required_config()
    wanted = tuple(indicators)

    rows = _execute_fetchall(config, QUERY_DAILY_QUOTES_LOOKBACK, (ts_code, as_of, lookback_rows))
    if not rows:
        raise NoMarketDataError(symbol, ts_code, f"no daily rows on or before {as_of}")
    rows = list(reversed(rows))
    df = pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close", "Volume", "Amount"])
    local_values = _local_factor_values(df)

    db_rows = _execute_fetchall(config, QUERY_FACTOR_VALUES, (ts_code, as_of, list(wanted)))
    db_values = {name: float(value) for name, value in db_rows if value is not None}

    lines = [
        f"## Indicator comparison for {ts_code} on {as_of}",
        "",
        "| Indicator | Local | DB factor | Abs diff | Rel diff | Status |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for name in wanted:
        local = local_values.get(name)
        db = db_values.get(name)
        if local is None or db is None:
            lines.append(f"| {name} | {_fmt(local)} | {_fmt(db)} | N/A | N/A | MISSING |")
            continue
        abs_diff = abs(local - db)
        denom = max(abs(db), abs_tolerance)
        rel_diff = abs_diff / denom
        status = "PASS" if abs_diff <= abs_tolerance or rel_diff <= rel_tolerance else "FAIL"
        lines.append(
            f"| {name} | {local:.10f} | {db:.10f} | {abs_diff:.10f} | {rel_diff:.4%} | {status} |"
        )
    return "\n".join(lines)
