from __future__ import annotations

import pytest

from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import VendorNotConfiguredError


class _FakeCursor:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return self.responses.pop(0)

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None


class _FakeConnection:
    def __init__(self, responses):
        self.cursor_obj = _FakeCursor(responses)
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.closed = True


def _set_env(monkeypatch):
    monkeypatch.setenv("ETF_STOCK_POSTGRES_HOST", "db.example")
    monkeypatch.setenv("ETF_STOCK_POSTGRES_PORT", "5432")
    monkeypatch.setenv("ETF_STOCK_POSTGRES_DB", "etf_data_prod")
    monkeypatch.setenv("ETF_STOCK_POSTGRES_USER", "reader")
    monkeypatch.setenv("ETF_STOCK_POSTGRES_PASSWORD", "secret")


def _set_aliyun_env(monkeypatch):
    monkeypatch.setenv("ALIYUN_STOCK_POSTGRES_HOST", "db.example")
    monkeypatch.setenv("ALIYUN_STOCK_POSTGRES_PORT", "5432")
    monkeypatch.setenv("ALIYUN_STOCK_POSTGRES_DB", "etf_data_prod")
    monkeypatch.setenv("ALIYUN_STOCK_POSTGRES_USER", "reader")
    monkeypatch.setenv("ALIYUN_STOCK_POSTGRES_PASSWORD", "secret")


@pytest.mark.unit
def test_etf_stock_postgres_requires_connection_config(monkeypatch):
    from tradingagents.dataflows import etf_stock_postgres

    for key in (
        "ETF_STOCK_POSTGRES_HOST",
        "ETF_STOCK_POSTGRES_PORT",
        "ETF_STOCK_POSTGRES_DB",
        "ETF_STOCK_POSTGRES_USER",
        "ETF_STOCK_POSTGRES_PASSWORD",
        "ALIYUN_STOCK_POSTGRES_HOST",
        "ALIYUN_STOCK_POSTGRES_PORT",
        "ALIYUN_STOCK_POSTGRES_DB",
        "ALIYUN_STOCK_POSTGRES_USER",
        "ALIYUN_STOCK_POSTGRES_PASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(VendorNotConfiguredError, match="ETF_STOCK_POSTGRES_HOST"):
        etf_stock_postgres.get_stock("688037.SS", "2026-06-15", "2026-06-22")


@pytest.mark.unit
def test_etf_stock_postgres_can_reuse_complete_aliyun_connection_config(monkeypatch):
    from tradingagents.dataflows import etf_stock_postgres

    for key in (
        "ETF_STOCK_POSTGRES_HOST",
        "ETF_STOCK_POSTGRES_PORT",
        "ETF_STOCK_POSTGRES_DB",
        "ETF_STOCK_POSTGRES_USER",
        "ETF_STOCK_POSTGRES_PASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)
    _set_aliyun_env(monkeypatch)

    assert etf_stock_postgres._required_config() == {
        "host": "db.example",
        "port": "5432",
        "dbname": "etf_data_prod",
        "user": "reader",
        "password": "secret",
    }


@pytest.mark.unit
def test_etf_stock_postgres_reads_daily_quotes_read_only_and_formats_csv(monkeypatch, tmp_path):
    from tradingagents.dataflows import etf_stock_postgres

    set_config({"data_cache_dir": str(tmp_path)})
    _set_env(monkeypatch)
    conn = _FakeConnection(
        [
            [
                ("2026-06-18", 284.58, 299.13, 278.0, 293.05, 86351, 2478866.27),
                ("2026-06-22", 293.0, 305.0, 276.01, 288.12, 85458, 2481467.31),
            ]
        ]
    )
    monkeypatch.setattr(etf_stock_postgres, "_connect", lambda _: conn)

    result = etf_stock_postgres.get_stock("688037.SS", "2026-06-15", "2026-06-22")

    calls = conn.cursor_obj.calls
    assert calls[0][0] == "BEGIN READ ONLY"
    assert "FROM daily_quotes" in calls[1][0]
    assert calls[1][1] == ("688037.SH", "2026-06-15", "2026-06-22")
    assert calls[-1][0] == "ROLLBACK"
    assert conn.closed is True
    assert result.splitlines() == [
        "Date,Open,High,Low,Close,Volume,Amount",
        "2026-06-18,284.58,299.13,278.0,293.05,86351,2478866.27",
        "2026-06-22,293.0,305.0,276.01,288.12,85458,2481467.31",
    ]


@pytest.mark.unit
def test_etf_stock_postgres_fundamentals_use_asof_financials(monkeypatch):
    from tradingagents.dataflows import etf_stock_postgres

    _set_env(monkeypatch)
    conn = _FakeConnection(
        [
            [("688037.SH", "芯源微", "半导体", "科创板", "2019-12-16", "涂胶显影设备")],
            [(5809285.7676, 823.3812, 20.7406, 28.9919, "2026-06-22")],
            [("2026-04-30", "2026-03-31", 330586511.36, 3508887.8, 6416071231.2, 3468378908.77, 0.02)],
            [("2026-04-30", "2026-03-31", 0.1255, 0.0799, 3.0884, 1.8162, 54.0577, 20.0751, -24.7021)],
            [("2026-04-30", "2026-03-31", 823.3812, 20.7406, 28.9919, 30.2, 0.0, 0.1255, 0.0799, 20.0751, -24.7021, 54.0577, 12.3, 8.4, 12345)],
            [("2026-04-30", "2026-03-31", 45.6, 33.2, 12.3, 8.4, 12345, 6789.0)],
        ]
    )
    monkeypatch.setattr(etf_stock_postgres, "_connect", lambda _: conn)

    report = etf_stock_postgres.get_fundamentals("688037.SS", "2026-06-22")

    sql_text = "\n".join(call[0] for call in conn.cursor_obj.calls)
    params = [call[1] for call in conn.cursor_obj.calls if call[1]]
    assert "ann_date <= %s" in sql_text
    assert ("688037.SH", "2026-06-22") in params
    assert "芯源微" in report
    assert "Latest financial statement as of 2026-06-22" in report
    assert "2026-04-30" in report
    assert "2026-03-31" in report
    assert "823.3812" in report
    assert "Latest fundamental factor pool as of 2026-06-22" in report
    assert "Latest ownership factors as of 2026-06-22" in report


@pytest.mark.unit
def test_etf_stock_postgres_statement_tools_are_frequency_aware(monkeypatch):
    from tradingagents.dataflows import etf_stock_postgres

    _set_env(monkeypatch)
    row = (
        "2026-04-30",
        "2026-03-31",
        330586511.36,
        20000000.0,
        21000000.0,
        3508887.8,
        0.02,
        6416071231.2,
        3468378908.77,
        2947692322.43,
        5000000.0,
        -2000000.0,
        1000000.0,
        600000000.0,
    )
    conn = _FakeConnection([[row], [row], [row], [row]])
    monkeypatch.setattr(etf_stock_postgres, "_connect", lambda _: conn)

    balance_sheet = etf_stock_postgres.get_balance_sheet("688037.SS", "annual", "2026-06-22")
    cashflow = etf_stock_postgres.get_cashflow("688037.SS", "quarterly", "2026-06-22")
    income = etf_stock_postgres.get_income_statement("688037.SS", "quarterly", "2026-06-22")

    sql_text = "\n".join(call[0] for call in conn.cursor_obj.calls)
    params = [call[1] for call in conn.cursor_obj.calls if call[1]]
    assert "EXTRACT(MONTH FROM end_date) = 12" in sql_text
    assert ("688037.SH", "2026-06-22", 4) in params
    assert "annual balance sheet" in balance_sheet
    assert "Operating cash flow" in cashflow
    assert "Operating profit" in income
    assert "330586511.36" in income


@pytest.mark.unit
def test_etf_stock_postgres_statement_tools_reject_unknown_frequency(monkeypatch):
    from tradingagents.dataflows import etf_stock_postgres

    _set_env(monkeypatch)

    with pytest.raises(ValueError, match="annual.*quarterly"):
        etf_stock_postgres.get_income_statement("688037.SH", "monthly", "2026-06-22")


@pytest.mark.unit
def test_compare_calculated_indicators_to_db_reports_diffs(monkeypatch, tmp_path):
    from tradingagents.dataflows import etf_stock_postgres

    set_config({"data_cache_dir": str(tmp_path)})
    _set_env(monkeypatch)
    conn = _FakeConnection(
        [
            [
                ("2026-06-22", 293.0, 305.0, 276.01, 288.12, 85458, 2481467.31),
                ("2026-06-18", 284.58, 299.13, 278.0, 293.05, 86351, 2478866.27),
                ("2026-06-17", 267.59, 288.0, 262.09, 287.3, 94552, 2616328.22),
                ("2026-06-16", 267.35, 277.48, 260.99, 272.0, 79174, 2132532.98),
                ("2026-06-15", 255.58, 269.63, 248.0, 267.35, 78290, 2060126.58),
                ("2026-06-12", 250.0, 258.0, 248.0, 255.59, 72000, 1800000.0),
            ],
            [
                ("stock_ret_1d", -0.016823),
                ("stock_ret_5d", 0.127274),
            ],
        ]
    )
    monkeypatch.setattr(etf_stock_postgres, "_connect", lambda _: conn)

    report = etf_stock_postgres.compare_calculated_indicators_to_db(
        "688037.SS",
        "2026-06-22",
        indicators=("stock_ret_1d", "stock_ret_5d"),
        lookback_rows=10,
        rel_tolerance=0.05,
    )

    assert "Indicator comparison for 688037.SH on 2026-06-22" in report
    assert "| stock_ret_1d |" in report
    assert "| stock_ret_5d |" in report
    assert "PASS" in report


@pytest.mark.unit
def test_router_can_select_etf_stock_postgres(monkeypatch):
    from tradingagents.dataflows import interface

    set_config({"data_vendors": {"core_stock_apis": "etf_stock_postgres"}})
    monkeypatch.setitem(
        interface.VENDOR_METHODS["get_stock_data"],
        "etf_stock_postgres",
        lambda symbol, start, end: f"ETF:{symbol}:{start}:{end}",
    )

    result = interface.route_to_vendor("get_stock_data", "688037.SS", "2026-06-15", "2026-06-22")

    assert result == "ETF:688037.SS:2026-06-15:2026-06-22"
