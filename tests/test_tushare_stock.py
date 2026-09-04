import json
from unittest import mock

import pandas as pd
import pytest

from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import VendorNotConfiguredError


@pytest.mark.unit
def test_tushare_stock_requires_token(monkeypatch):
    from tradingagents.dataflows import tushare_stock

    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)

    with pytest.raises(VendorNotConfiguredError, match="TUSHARE_TOKEN"):
        tushare_stock.get_stock("600519.SH", "2026-01-01", "2026-01-10")


@pytest.mark.unit
def test_tushare_stock_classifies_invalid_token_response(monkeypatch):
    from tradingagents.dataflows import tushare_stock

    monkeypatch.setenv("TUSHARE_TOKEN", "bad-token")

    response = mock.Mock()
    response.json.return_value = {
        "code": 40101,
        "msg": "您的token不对，请确认。",
        "data": None,
    }

    with (
        mock.patch("tradingagents.dataflows.tushare_stock.requests.post", return_value=response),
        pytest.raises(VendorNotConfiguredError, match="token"),
    ):
        tushare_stock.get_stock("600519.SH", "2026-01-01", "2026-01-10")


@pytest.mark.unit
def test_tushare_stock_posts_daily_query_and_formats_csv(monkeypatch):
    from tradingagents.dataflows import tushare_stock

    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")

    response = mock.Mock()
    response.json.return_value = {
        "code": 0,
        "msg": None,
        "data": {
            "fields": [
                "ts_code",
                "trade_date",
                "open",
                "high",
                "low",
                "close",
                "vol",
                "amount",
            ],
            "items": [
                ["600519.SH", "20260105", 1500.0, 1510.0, 1490.0, 1505.0, 1200.0, 1806000.0],
                ["600519.SH", "20260102", 1490.0, 1500.0, 1480.0, 1495.0, 1100.0, 1644500.0],
            ],
        },
    }

    with mock.patch("tradingagents.dataflows.tushare_stock.requests.post", return_value=response) as post:
        result = tushare_stock.get_stock("600519.SS", "2026-01-01", "2026-01-10")

    payload = json.loads(post.call_args.kwargs["data"])
    assert post.call_args.args == ("http://api.tushare.pro",)
    assert payload["api_name"] == "daily"
    assert payload["token"] == "test-token"
    assert payload["params"] == {
        "ts_code": "600519.SH",
        "start_date": "20260101",
        "end_date": "20260110",
    }
    assert payload["fields"] == "ts_code,trade_date,open,high,low,close,vol,amount"
    assert result.splitlines() == [
        "Date,Open,High,Low,Close,Volume,Amount",
        "2026-01-02,1490.0,1500.0,1480.0,1495.0,1100.0,1644500.0",
        "2026-01-05,1500.0,1510.0,1490.0,1505.0,1200.0,1806000.0",
    ]


@pytest.mark.unit
def test_tushare_return_prices_adjust_across_corporate_action(monkeypatch):
    """A split-like raw-price jump must not become a portfolio return loss."""
    from tradingagents.dataflows import tushare_stock

    monkeypatch.setattr(
        tushare_stock,
        "_request_daily",
        lambda *_: {
            "data": {
                "fields": ["ts_code", "trade_date", "close"],
                "items": [
                    ["600519.SH", "20260106", 50.0],
                    ["600519.SH", "20260105", 100.0],
                ],
            }
        },
    )
    monkeypatch.setattr(
        tushare_stock,
        "_request_adjustment_factors",
        lambda *_: {
            "data": {
                "fields": ["ts_code", "trade_date", "adj_factor"],
                "items": [
                    ["600519.SH", "20260106", 2.0],
                    ["600519.SH", "20260105", 1.0],
                ],
            }
        },
    )

    prices = tushare_stock.get_close_prices(
        "600519.SH", "2026-01-05", "2026-01-06"
    )

    assert prices.tolist() == [50.0, 50.0]


@pytest.mark.unit
def test_tushare_index_return_prices_keep_raw_index_levels(monkeypatch):
    from tradingagents.dataflows import tushare_stock

    monkeypatch.setattr(
        tushare_stock,
        "_request_index_daily",
        lambda *_: {
            "data": {
                "fields": ["ts_code", "trade_date", "close"],
                "items": [
                    ["000001.SH", "20260106", 3100.0],
                    ["000001.SH", "20260105", 3000.0],
                ],
            }
        },
    )
    adjustment_request = mock.Mock()
    monkeypatch.setattr(tushare_stock, "_request_adjustment_factors", adjustment_request)

    prices = tushare_stock.get_index_close_prices(
        "000001.SH", "2026-01-05", "2026-01-06"
    )

    assert prices.tolist() == [3000.0, 3100.0]
    adjustment_request.assert_not_called()


@pytest.mark.unit
def test_custom_a_share_benchmark_uses_index_daily(monkeypatch):
    from tradingagents.dataflows import tushare_stock

    request_api = mock.Mock(
        return_value={
            "data": {
                "fields": ["ts_code", "trade_date", "close"],
                "items": [["000300.SH", "20260105", 4500.0]],
            }
        }
    )
    monkeypatch.setattr(tushare_stock, "_request_api", request_api)

    prices = tushare_stock.get_index_close_prices(
        "000300.SH", "2026-01-05", "2026-01-06"
    )

    assert prices.tolist() == [4500.0]
    assert request_api.call_args.args[0] == "index_daily"


@pytest.mark.unit
def test_router_can_select_tushare_for_core_stock_data(monkeypatch):
    from tradingagents.dataflows import interface

    set_config({"data_vendors": {"core_stock_apis": "tushare"}})
    monkeypatch.setitem(
        interface.VENDOR_METHODS["get_stock_data"],
        "tushare",
        lambda symbol, start, end: f"TUSHARE:{symbol}:{start}:{end}",
    )

    result = interface.route_to_vendor("get_stock_data", "600519.SH", "2026-01-01", "2026-01-10")

    assert result == "TUSHARE:600519.SH:2026-01-01:2026-01-10"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("600519.SS", "600519.SH"),
        ("688037", "688037.SH"),
        ("000001", "000001.SZ"),
        ("430047", "430047.BJ"),
        ("830799", "830799.BJ"),
        ("920047", "920047.BJ"),
    ],
)
def test_tushare_symbol_normalization_covers_public_contract(symbol, expected):
    from tradingagents.dataflows.tushare_stock import _normalize_ts_code

    assert _normalize_ts_code(symbol) == expected


@pytest.mark.unit
def test_tushare_benchmark_loader_distinguishes_indices_funds_and_stocks(monkeypatch):
    from tradingagents.dataflows import tushare_stock

    index_loader = mock.Mock(return_value=pd.Series([1.0]))
    fund_loader = mock.Mock(return_value=pd.Series([2.0]))
    stock_loader = mock.Mock(return_value=pd.Series([3.0]))
    monkeypatch.setattr(tushare_stock, "get_index_close_prices", index_loader)
    monkeypatch.setattr(tushare_stock, "get_fund_close_prices", fund_loader)
    monkeypatch.setattr(tushare_stock, "get_close_prices", stock_loader)

    tushare_stock.get_benchmark_close_prices(
        "000300.SH", "2026-01-05", "2026-01-06"
    )
    tushare_stock.get_benchmark_close_prices(
        "510300.SH", "2026-01-05", "2026-01-06"
    )
    tushare_stock.get_benchmark_close_prices(
        "600519.SH", "2026-01-05", "2026-01-06"
    )

    index_loader.assert_called_once_with("000300.SH", "2026-01-05", "2026-01-06")
    fund_loader.assert_called_once_with("510300.SH", "2026-01-05", "2026-01-06")
    stock_loader.assert_called_once_with("600519.SH", "2026-01-05", "2026-01-06")


@pytest.mark.unit
def test_tushare_fund_return_prices_use_fund_daily(monkeypatch):
    from tradingagents.dataflows import tushare_stock

    request_api = mock.Mock(
        return_value={
            "data": {
                "fields": ["ts_code", "trade_date", "close"],
                "items": [["510300.SH", "20260105", 4.5]],
            }
        }
    )
    monkeypatch.setattr(tushare_stock, "_request_api", request_api)

    prices = tushare_stock.get_fund_close_prices(
        "510300.SH", "2026-01-05", "2026-01-06"
    )

    assert prices.tolist() == [4.5]
    assert request_api.call_args.args[0] == "fund_daily"


@pytest.mark.unit
def test_tushare_modules_import_without_sdk_or_token(monkeypatch):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)

    from tradingagents.dataflows import (
        interface,
        tushare_fundamentals,
        tushare_indicators,
        tushare_price,
    )

    assert callable(tushare_price.get_price_data)
    assert callable(tushare_indicators.get_stock_stats_indicators_window)
    assert callable(tushare_fundamentals.get_fundamentals)
    assert "tushare" in interface.VENDOR_METHODS["get_stock_data"]


@pytest.mark.unit
def test_tushare_price_wrapper_uses_unadjusted_routed_daily_data(monkeypatch):
    from tradingagents.dataflows import tushare_price

    calls = []

    def fake_get_stock(symbol, start, end):
        calls.append((symbol, start, end))
        return "\n".join(
            [
                "Date,Open,High,Low,Close,Volume,Amount",
                "2026-01-02,10,12,9,11,1000,11000",
            ]
        )

    monkeypatch.setattr(tushare_price, "get_stock", fake_get_stock)
    result = tushare_price.get_price_data("600519.SS", "2026-01-01", "2026-01-02")

    assert calls == [("600519.SS", "2026-01-01", "2026-01-02")]
    assert result.index.tolist() == [pd.Timestamp("2026-01-02")]
    assert result.iloc[0]["Close"] == 11


def _indicator_ohlcv() -> pd.DataFrame:
    dates = pd.bdate_range("2023-01-02", periods=600)
    close = pd.Series(range(100, 700), dtype=float).to_numpy()
    return pd.DataFrame(
        {
            "Open": close - 0.5,
            "High": close + 1.0,
            "Low": close - 1.0,
            "Close": close,
            "Volume": pd.Series(range(1_000, 1_600), dtype=float).to_numpy(),
        },
        index=dates,
    ).rename_axis("Date")


@pytest.mark.unit
@pytest.mark.parametrize(
    "indicator",
    [
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
    ],
)
def test_tushare_indicators_implement_every_public_name(monkeypatch, indicator):
    from tradingagents.dataflows import tushare_indicators

    frame = _indicator_ohlcv()
    curr_date = frame.index[-1].strftime("%Y-%m-%d")
    monkeypatch.setattr(tushare_indicators, "get_price_data", lambda *args: frame)

    result = tushare_indicators.get_stock_stats_indicators_window(
        "600519.SH", indicator, curr_date, 1
    )

    assert f"## {indicator} values" in result
    assert "Latest close" not in result


@pytest.mark.unit
def test_tushare_indicators_reject_unknown_name():
    from tradingagents.dataflows import tushare_indicators

    with pytest.raises(ValueError, match="not supported"):
        tushare_indicators.get_stock_stats_indicators_window(
            "600519.SH", "made_up_indicator", "2026-01-02", 30
        )


@pytest.mark.unit
def test_tushare_fundamentals_filter_by_actual_announcement_date(monkeypatch):
    from tradingagents.dataflows import tushare_fundamentals

    response = {
        "code": 0,
        "data": {
            "fields": [
                "ts_code",
                "ann_date",
                "f_ann_date",
                "end_date",
                "revenue",
            ],
            "items": [
                ["600519.SH", "20240420", "20240420", "20231231", 100.0],
                # Nominal date is before cutoff, but actual publication is after it.
                ["600519.SH", "20240520", "20240701", "20240331", 200.0],
                ["600519.SH", "20250420", "20250420", "20241231", 300.0],
            ],
        },
    }
    monkeypatch.setattr(tushare_fundamentals, "_request_api", lambda *a, **k: response)

    result = tushare_fundamentals.get_income_statement(
        "600519.SS", "quarterly", "2024-06-01"
    )

    assert "20231231" in result
    assert "20240331" not in result
    assert "20241231" not in result
    assert "Point-in-time cutoff: 2024-06-01" in result


@pytest.mark.unit
def test_tushare_fundamentals_respect_frequency(monkeypatch):
    from tradingagents.dataflows import tushare_fundamentals

    response = {
        "code": 0,
        "data": {
            "fields": ["ts_code", "ann_date", "end_date", "revenue"],
            "items": [
                ["600519.SH", "20240420", "20231231", 100.0],
                ["600519.SH", "20241020", "20240930", 80.0],
            ],
        },
    }
    monkeypatch.setattr(tushare_fundamentals, "_request_api", lambda *a, **k: response)

    result = tushare_fundamentals.get_income_statement(
        "600519.SH", "annual", "2024-12-31"
    )

    assert "20231231" in result
    assert "20240930" not in result


@pytest.mark.unit
@pytest.mark.parametrize("latest_first", [True, False])
def test_tushare_fundamentals_prefer_latest_same_day_revision(latest_first):
    from tradingagents.dataflows import tushare_fundamentals

    latest = {
        "ts_code": "600519.SH",
        "ann_date": "20240420",
        "f_ann_date": "20240420",
        "end_date": "20231231",
        "revenue": 120.0,
        "update_flag": "1",
    }
    superseded = {
        "ts_code": "600519.SH",
        "ann_date": "20240420",
        "f_ann_date": "20240420",
        "end_date": "20231231",
        "revenue": 100.0,
        "update_flag": "0",
    }
    rows = [latest, superseded] if latest_first else [superseded, latest]
    reports = pd.DataFrame(rows)

    filtered = tushare_fundamentals._filter_point_in_time(
        reports, pd.Timestamp("2024-06-01"), "quarterly"
    )

    assert filtered.to_dict("records") == [latest]


@pytest.mark.unit
def test_tushare_fundamentals_use_update_flag_only_for_same_day_ties():
    from tradingagents.dataflows import tushare_fundamentals

    reports = pd.DataFrame(
        [
            {
                "end_date": "20231231",
                "f_ann_date": "20240420",
                "revenue": 100.0,
                "update_flag": "1",
            },
            {
                "end_date": "20231231",
                "f_ann_date": "20240520",
                "revenue": 120.0,
                "update_flag": "0",
            },
        ]
    )

    filtered = tushare_fundamentals._filter_point_in_time(
        reports, pd.Timestamp("2024-06-01"), "quarterly"
    )

    assert filtered.to_dict("records") == [
        {
            "end_date": "20231231",
            "f_ann_date": "20240520",
            "revenue": 120.0,
            "update_flag": "0",
        }
    ]


@pytest.mark.unit
def test_tushare_is_wired_for_indicators_and_fundamentals():
    from tradingagents.dataflows.interface import VENDOR_METHODS

    for method in (
        "get_indicators",
        "get_fundamentals",
        "get_balance_sheet",
        "get_cashflow",
        "get_income_statement",
    ):
        assert "tushare" in VENDOR_METHODS[method]


@pytest.mark.unit
def test_missing_tushare_token_falls_back_through_vendor_router(monkeypatch):
    from tradingagents.dataflows import interface

    def missing_token(*args, **kwargs):
        raise VendorNotConfiguredError("TUSHARE_TOKEN is missing")

    monkeypatch.setattr(interface, "get_vendor", lambda category, method=None: "tushare,yfinance")
    monkeypatch.setitem(
        interface.VENDOR_METHODS,
        "get_indicators",
        {
            "tushare": missing_token,
            "yfinance": lambda *args, **kwargs: "YFINANCE_FALLBACK",
        },
    )

    result = interface.route_to_vendor(
        "get_indicators", "600519.SH", "rsi", "2026-01-02", 30
    )

    assert result == "YFINANCE_FALLBACK"
