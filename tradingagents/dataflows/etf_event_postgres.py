"""Read-only China raw-news vendor backed by ETF event ``raw_events``."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from .config import get_config
from .errors import VendorNotConfiguredError

READ_ONLY_OPTIONS = "-c default_transaction_read_only=on -c statement_timeout=15000"
NEWS_SOURCES = (
    "tonghuashun-news",
    "baidu-express-news",
    "eastmoney-finance-news",
    "mysql-talks",
)
CONTENT_EXCERPT_CHARS = 1_200
ALIYUN_EVENT_DATABASE = "etf_event_analysis"

QUERY_RAW_NEWS = """
SELECT r.event_id, r.title, r.content, r.source, r.doc_url, r.publish_date
FROM raw_events r
WHERE r.source = ANY(%s)
  AND r.publish_date >= %s::date
  AND r.publish_date < %s::date + INTERVAL '1 day'
  AND r.created_at < %s::date + INTERVAL '1 day'
  AND EXISTS (
      SELECT 1
      FROM event_companies ec
      WHERE ec.event_id = r.event_id
        AND ec.stock_code = ANY(%s)
        AND ec.created_at < %s::date + INTERVAL '1 day'
  )
ORDER BY r.publish_date DESC, r.event_id DESC
LIMIT %s
"""

QUERY_GLOBAL_RAW_NEWS = """
SELECT r.event_id, r.title, r.content, r.source, r.doc_url, r.publish_date
FROM raw_events r
WHERE r.source = ANY(%s)
  AND r.publish_date >= %s::date
  AND r.publish_date < %s::date + INTERVAL '1 day'
  AND r.created_at < %s::date + INTERVAL '1 day'
ORDER BY r.publish_date DESC, r.event_id DESC
LIMIT %s
"""


class ETFEventPostgresNotConfiguredError(VendorNotConfiguredError):
    """Raised when the ETF event PostgreSQL source lacks connection settings."""


def _required_config() -> dict[str, str]:
    event_keys = {
        "host": "ETF_EVENT_POSTGRES_HOST",
        "port": "ETF_EVENT_POSTGRES_PORT",
        "dbname": "ETF_EVENT_POSTGRES_DB",
        "user": "ETF_EVENT_POSTGRES_USER",
        "password": "ETF_EVENT_POSTGRES_PASSWORD",
    }
    aliyun_keys = {
        "host": "ALIYUN_STOCK_POSTGRES_HOST",
        "port": "ALIYUN_STOCK_POSTGRES_PORT",
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

    values, missing = configured_values(event_keys)
    if not missing:
        return values
    if len(missing) != len(event_keys):
        raise ETFEventPostgresNotConfiguredError(
            f"{', '.join(missing)} environment variable(s) are not set."
        )

    values, missing = configured_values(aliyun_keys)
    if not missing:
        values["dbname"] = ALIYUN_EVENT_DATABASE
        return values

    raise ETFEventPostgresNotConfiguredError(
        "ETF_EVENT_POSTGRES_HOST or ALIYUN_STOCK_POSTGRES_HOST environment variable(s) are not set."
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


def _stock_code_candidates(symbol: str) -> tuple[str, ...]:
    ts_code = _normalize_ts_code(symbol)
    if ts_code.endswith((".SH", ".SZ", ".BJ")):
        return ts_code, ts_code[:6]
    return (ts_code,)


def _connect(config: dict[str, str]):
    try:
        import psycopg
    except ImportError as exc:
        raise ETFEventPostgresNotConfiguredError(
            "psycopg is required for etf_event_postgres; install "
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


def _excerpt(content: object) -> str:
    compact = " ".join(str(content or "").split())
    if len(compact) <= CONTENT_EXCERPT_CHARS:
        return compact
    return compact[:CONTENT_EXCERPT_CHARS].rstrip() + "…"


def _format_news(rows: list[tuple], ticker: str, start_date: str, end_date: str) -> str:
    if not rows:
        return f"No China raw news found for {ticker} between {start_date} and {end_date}"

    articles: list[str] = []
    for _, title, content, source, doc_url, publish_date in rows:
        published = publish_date.isoformat() if hasattr(publish_date, "isoformat") else str(publish_date)
        article = f"### {title} (source: {source}; published: {published})\n{_excerpt(content)}"
        if doc_url:
            article += f"\nLink: {doc_url}"
        articles.append(article)
    return (
        f"## {ticker} China raw news, from {start_date} to {end_date}:\n\n"
        + "\n\n".join(articles)
    )


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    """Return stock-associated raw news facts without using derived event content.

    ``event_companies`` is used solely as a ticker index. Its association and
    the raw event must both have existed by the requested end date; this gives
    the best available as-of boundary even though the upstream association is
    not historically versioned.
    """
    start = _sql_date(start_date)
    end = _sql_date(end_date)
    limit = get_config()["news_article_limit"]
    conn = _connect(_required_config())
    try:
        with conn.cursor() as cur:
            cur.execute("BEGIN READ ONLY")
            try:
                cur.execute(
                    QUERY_RAW_NEWS,
                    (
                        list(NEWS_SOURCES),
                        start,
                        end,
                        end,
                        list(_stock_code_candidates(ticker)),
                        end,
                        limit,
                    ),
                )
                rows = cur.fetchall()
            finally:
                cur.execute("ROLLBACK")
    finally:
        conn.close()
    return _format_news(rows, ticker, start, end)


def get_global_news(
    curr_date: str,
    look_back_days: int | None = None,
    limit: int | None = None,
) -> str:
    """Return China market raw-news facts without using derived event content."""
    config = get_config()
    look_back = (
        look_back_days
        if look_back_days is not None
        else config["global_news_lookback_days"]
    )
    article_limit = (
        limit if limit is not None else config["global_news_article_limit"]
    )
    end = _sql_date(curr_date)
    start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=look_back)).strftime(
        "%Y-%m-%d"
    )
    conn = _connect(_required_config())
    try:
        with conn.cursor() as cur:
            cur.execute("BEGIN READ ONLY")
            try:
                cur.execute(
                    QUERY_GLOBAL_RAW_NEWS,
                    (list(NEWS_SOURCES), start, end, end, article_limit),
                )
                rows = cur.fetchall()
            finally:
                cur.execute("ROLLBACK")
    finally:
        conn.close()
    return _format_news(rows, "China market", start, end)
