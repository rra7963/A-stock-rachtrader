from __future__ import annotations

import pytest

from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorNotConfiguredError


class _FakeCursor:
    description = [
        ("trade_date",),
        ("open",),
        ("high",),
        ("low",),
        ("close",),
        ("volume",),
        ("amount",),
    ]

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

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _FakeConnection:
    def __init__(self, rows):
        self.cursor_obj = _FakeCursor(rows)
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.closed = True


def _set_env(monkeypatch):
    monkeypatch.setenv("ALIYUN_STOCK_POSTGRES_HOST", "db.example")
    monkeypatch.setenv("ALIYUN_STOCK_POSTGRES_PORT", "5432")
    monkeypatch.setenv("ALIYUN_STOCK_POSTGRES_DB", "stocks")
    monkeypatch.setenv("ALIYUN_STOCK_POSTGRES_USER", "reader")
    monkeypatch.setenv("ALIYUN_STOCK_POSTGRES_PASSWORD", "secret")


@pytest.mark.unit
def test_aliyun_stock_postgres_requires_connection_config(monkeypatch):
    from tradingagents.dataflows import aliyun_stock_postgres

    for key in (
        "ALIYUN_STOCK_POSTGRES_HOST",
        "ALIYUN_STOCK_POSTGRES_PORT",
        "ALIYUN_STOCK_POSTGRES_DB",
        "ALIYUN_STOCK_POSTGRES_USER",
        "ALIYUN_STOCK_POSTGRES_PASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(VendorNotConfiguredError, match="ALIYUN_STOCK_POSTGRES_HOST"):
        aliyun_stock_postgres.get_stock("600519.SH", "2026-07-01", "2026-07-02")


@pytest.mark.unit
def test_aliyun_stock_postgres_reads_daily_quotes_read_only_and_formats_csv(monkeypatch, tmp_path):
    from tradingagents.dataflows import aliyun_stock_postgres

    set_config({"data_cache_dir": str(tmp_path)})
    _set_env(monkeypatch)
    conn = _FakeConnection(
        [
            ("2026-07-01", 1180.1, 1196.8, 1166.33, 1193.01, 42474, 5033838.24),
            ("2026-07-02", 1193.01, 1215.52, 1190.51, 1203.0, 50870, 6122360.93),
        ]
    )
    monkeypatch.setattr(aliyun_stock_postgres, "_connect", lambda _: conn)

    result = aliyun_stock_postgres.get_stock("600519.SS", "2026-07-01", "2026-07-02")

    calls = conn.cursor_obj.calls
    assert calls[0][0] == "BEGIN READ ONLY"
    assert "FROM daily_quotes" in calls[1][0]
    assert calls[1][1] == ("600519.SH", "2026-07-01", "2026-07-02")
    assert calls[-1][0] == "ROLLBACK"
    assert conn.closed is True
    assert result.splitlines() == [
        "Date,Open,High,Low,Close,Volume,Amount",
        "2026-07-01,1180.1,1196.8,1166.33,1193.01,42474,5033838.24",
        "2026-07-02,1193.01,1215.52,1190.51,1203.0,50870,6122360.93",
    ]


@pytest.mark.unit
def test_aliyun_stock_postgres_rejects_stale_rows(monkeypatch, tmp_path):
    from tradingagents.dataflows import aliyun_stock_postgres

    set_config({"data_cache_dir": str(tmp_path)})
    _set_env(monkeypatch)
    conn = _FakeConnection(
        [("2026-06-01", 10.0, 11.0, 9.0, 10.5, 100, 1000.0)]
    )
    monkeypatch.setattr(aliyun_stock_postgres, "_connect", lambda _: conn)

    with pytest.raises(NoMarketDataError, match="stale"):
        aliyun_stock_postgres.get_stock("000001.SZ", "2026-06-01", "2026-07-02")


@pytest.mark.unit
def test_aliyun_stock_postgres_caches_successful_query(monkeypatch, tmp_path):
    from tradingagents.dataflows import aliyun_stock_postgres

    set_config({"data_cache_dir": str(tmp_path)})
    _set_env(monkeypatch)
    connections = []

    def connect(_config):
        conn = _FakeConnection(
            [("2026-07-02", 11.0, 12.0, 10.0, 11.5, 1000, 11500.0)]
        )
        connections.append(conn)
        return conn

    monkeypatch.setattr(aliyun_stock_postgres, "_connect", connect)

    first = aliyun_stock_postgres.get_stock("000001.SZ", "2026-07-01", "2026-07-02")
    second = aliyun_stock_postgres.get_stock("000001.SZ", "2026-07-01", "2026-07-02")

    assert first == second
    assert len(connections) == 1
    assert list((tmp_path / "aliyun_stock_postgres").glob("*.csv"))


@pytest.mark.unit
def test_aliyun_stock_postgres_calculates_indicators_from_its_ohlcv(monkeypatch):
    from tradingagents.dataflows import aliyun_stock_postgres

    rows = [
        f"2026-07-{day:02d},{day},{day + 1},{day - 1},{day},{day * 100},1000"
        for day in range(1, 12)
    ]
    monkeypatch.setattr(
        aliyun_stock_postgres,
        "get_stock",
        lambda symbol, start, end: "Date,Open,High,Low,Close,Volume,Amount\n" + "\n".join(rows),
    )

    result = aliyun_stock_postgres.get_indicators(
        "600519.SS", "close_10_ema", "2026-07-11", look_back_days=2
    )

    assert result.startswith("## close_10_ema values from 2026-07-09 to 2026-07-11:")
    assert "2026-07-11:" in result


@pytest.mark.unit
def test_aliyun_stock_postgres_reads_a_share_identity_read_only(monkeypatch):
    from tradingagents.dataflows import aliyun_stock_postgres

    _set_env(monkeypatch)
    conn = _FakeConnection([("贵州茅台", "白酒", "主板")])
    monkeypatch.setattr(aliyun_stock_postgres, "_connect", lambda _: conn)

    identity = aliyun_stock_postgres.get_instrument_identity("600519.SS")

    calls = conn.cursor_obj.calls
    assert calls[0][0] == "BEGIN READ ONLY"
    assert "FROM stock_basic" in calls[1][0]
    assert calls[1][1] == ("600519.SH",)
    assert calls[-1][0] == "ROLLBACK"
    assert conn.closed is True
    assert identity == {
        "company_name": "贵州茅台",
        "industry": "白酒",
        "market": "主板",
        "exchange": "SSE",
    }


@pytest.mark.unit
def test_router_can_select_aliyun_stock_postgres(monkeypatch):
    from tradingagents.dataflows import interface

    set_config({"data_vendors": {"core_stock_apis": "aliyun_stock_postgres"}})
    monkeypatch.setitem(
        interface.VENDOR_METHODS["get_stock_data"],
        "aliyun_stock_postgres",
        lambda symbol, start, end: f"ALIYUN:{symbol}:{start}:{end}",
    )

    result = interface.route_to_vendor("get_stock_data", "600519.SH", "2026-07-01", "2026-07-02")

    assert result == "ALIYUN:600519.SH:2026-07-01:2026-07-02"


@pytest.mark.unit
def test_router_can_select_aliyun_stock_postgres_indicators(monkeypatch):
    from tradingagents.dataflows import interface

    set_config({"data_vendors": {"technical_indicators": "aliyun_stock_postgres"}})
    monkeypatch.setitem(
        interface.VENDOR_METHODS["get_indicators"],
        "aliyun_stock_postgres",
        lambda symbol, indicator, curr_date, lookback: f"ALIYUN:{symbol}:{indicator}:{curr_date}:{lookback}",
    )

    result = interface.route_to_vendor("get_indicators", "600519.SH", "rsi", "2026-07-02", 30)

    assert result == "ALIYUN:600519.SH:rsi:2026-07-02:30"
