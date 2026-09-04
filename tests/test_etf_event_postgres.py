from __future__ import annotations

from datetime import datetime

import pytest

from tradingagents.dataflows.config import set_config


class _FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return self.rows


class _FakeConnection:
    def __init__(self, rows):
        self.cursor_obj = _FakeCursor(rows)
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.closed = True


def _set_env(monkeypatch):
    monkeypatch.setenv("ETF_EVENT_POSTGRES_HOST", "db.example")
    monkeypatch.setenv("ETF_EVENT_POSTGRES_PORT", "5432")
    monkeypatch.setenv("ETF_EVENT_POSTGRES_DB", "events")
    monkeypatch.setenv("ETF_EVENT_POSTGRES_USER", "reader")
    monkeypatch.setenv("ETF_EVENT_POSTGRES_PASSWORD", "secret")


def _set_aliyun_env(monkeypatch):
    monkeypatch.setenv("ALIYUN_STOCK_POSTGRES_HOST", "db.example")
    monkeypatch.setenv("ALIYUN_STOCK_POSTGRES_PORT", "5432")
    monkeypatch.setenv("ALIYUN_STOCK_POSTGRES_DB", "etf_data_prod")
    monkeypatch.setenv("ALIYUN_STOCK_POSTGRES_USER", "reader")
    monkeypatch.setenv("ALIYUN_STOCK_POSTGRES_PASSWORD", "secret")


@pytest.mark.unit
def test_reads_raw_news_read_only_and_formats_markdown(monkeypatch):
    from tradingagents.dataflows import etf_event_postgres

    set_config({"news_article_limit": 20})
    _set_env(monkeypatch)
    conn = _FakeConnection(
        [
            (
                "news-1",
                "Company update",
                "  Original   article\nbody  ",
                "tonghuashun-news",
                "https://example.invalid/news-1",
                datetime(2026, 7, 10, 9, 30),
            )
        ]
    )
    monkeypatch.setattr(etf_event_postgres, "_connect", lambda _: conn)

    result = etf_event_postgres.get_news("600519.SS", "2026-07-04", "2026-07-10")

    calls = conn.cursor_obj.calls
    assert calls[0][0] == "BEGIN READ ONLY"
    assert "FROM raw_events" in calls[1][0]
    assert "event_companies" in calls[1][0]
    assert "ec.stock_code = ANY(%s)" in calls[1][0]
    assert calls[1][1] == (
        list(etf_event_postgres.NEWS_SOURCES),
        "2026-07-04",
        "2026-07-10",
        "2026-07-10",
        ["600519.SH", "600519"],
        "2026-07-10",
        20,
    )
    assert calls[-1][0] == "ROLLBACK"
    assert conn.closed is True
    assert "Company update" in result
    assert "Original article body" in result
    assert "https://example.invalid/news-1" in result


@pytest.mark.unit
def test_empty_raw_news_is_explicit(monkeypatch):
    from tradingagents.dataflows import etf_event_postgres

    set_config({"news_article_limit": 20})
    _set_env(monkeypatch)
    monkeypatch.setattr(etf_event_postgres, "_connect", lambda _: _FakeConnection([]))

    result = etf_event_postgres.get_news("300750.SZ", "2026-07-04", "2026-07-10")

    assert result == "No China raw news found for 300750.SZ between 2026-07-04 and 2026-07-10"


@pytest.mark.unit
def test_uses_aliyun_connection_with_event_database_when_event_config_is_absent(monkeypatch):
    from tradingagents.dataflows import etf_event_postgres

    for name in (
        "ETF_EVENT_POSTGRES_HOST",
        "ETF_EVENT_POSTGRES_PORT",
        "ETF_EVENT_POSTGRES_DB",
        "ETF_EVENT_POSTGRES_USER",
        "ETF_EVENT_POSTGRES_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    _set_aliyun_env(monkeypatch)

    assert etf_event_postgres._required_config() == {
        "host": "db.example",
        "port": "5432",
        "dbname": "etf_event_analysis",
        "user": "reader",
        "password": "secret",
    }


@pytest.mark.unit
def test_mysql_talks_is_a_raw_news_source():
    from tradingagents.dataflows import etf_event_postgres

    assert "mysql-talks" in etf_event_postgres.NEWS_SOURCES


@pytest.mark.unit
def test_reads_global_raw_news_read_only_and_formats_markdown(monkeypatch):
    from tradingagents.dataflows import etf_event_postgres

    set_config({"global_news_lookback_days": 7, "global_news_article_limit": 10})
    _set_env(monkeypatch)
    conn = _FakeConnection(
        [
            (
                "news-2",
                "Market update",
                "  China   market\nsummary  ",
                "eastmoney-finance-news",
                "https://example.invalid/news-2",
                datetime(2026, 7, 10, 9, 30),
            )
        ]
    )
    monkeypatch.setattr(etf_event_postgres, "_connect", lambda _: conn)

    result = etf_event_postgres.get_global_news("2026-07-11")

    calls = conn.cursor_obj.calls
    assert calls[0][0] == "BEGIN READ ONLY"
    assert "FROM raw_events" in calls[1][0]
    assert "event_companies" not in calls[1][0]
    assert calls[1][1] == (
        list(etf_event_postgres.NEWS_SOURCES),
        "2026-07-04",
        "2026-07-11",
        "2026-07-11",
        10,
    )
    assert calls[-1][0] == "ROLLBACK"
    assert conn.closed is True
    assert "China market" in result
    assert "China market summary" in result


@pytest.mark.unit
def test_news_tools_route_only_to_raw_events():
    from tradingagents.dataflows.interface import VENDOR_METHODS

    assert set(VENDOR_METHODS["get_news"]) == {"etf_event_postgres"}
    assert set(VENDOR_METHODS["get_global_news"]) == {"etf_event_postgres"}
