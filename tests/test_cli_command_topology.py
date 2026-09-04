"""Regression coverage for the multi-command CLI's legacy root entry point."""

from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

import cli.main as cli_main
from tradingagents.agents.schemas import (
    PortfolioAllocation,
    PortfolioPosition,
    PortfolioRating,
)


def test_root_without_arguments_still_runs_analysis(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli_main,
        "_execute_analysis_command",
        lambda checkpoint=None, clear_checkpoints=False: calls.append(
            (checkpoint, clear_checkpoints)
        ),
    )

    result = CliRunner().invoke(cli_main.app, [])

    assert result.exit_code == 0
    assert calls == [(None, False)]


def test_root_preserves_legacy_checkpoint_options(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli_main,
        "_execute_analysis_command",
        lambda checkpoint=None, clear_checkpoints=False: calls.append(
            (checkpoint, clear_checkpoints)
        ),
    )

    result = CliRunner().invoke(
        cli_main.app, ["--checkpoint", "--clear-checkpoints"]
    )

    assert result.exit_code == 0
    assert calls == [(True, True)]


def test_explicit_analyze_command_remains_available(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli_main,
        "_execute_analysis_command",
        lambda checkpoint=None, clear_checkpoints=False: calls.append(
            (checkpoint, clear_checkpoints)
        ),
    )

    result = CliRunner().invoke(cli_main.app, ["analyze", "--no-checkpoint"])

    assert result.exit_code == 0
    assert calls == [(False, False)]


def test_portfolio_command_is_registered():
    result = CliRunner().invoke(cli_main.app, ["portfolio", "--help"])

    assert result.exit_code == 0
    assert "2 to 20 comma-separated stock symbols" in result.output


@pytest.mark.parametrize(
    ("user_input", "expected_tickers"),
    [
        ("600519,000001", ["600519.SH", "000001.SZ"]),
        ("600519.SH,000001.SZ", ["600519.SH", "000001.SZ"]),
        ("600519.SS,920047", ["600519.SH", "920047.BJ"]),
    ],
)
def test_portfolio_routes_bare_and_suffixed_a_shares_to_tushare(
    monkeypatch,
    tmp_path,
    user_input,
    expected_tickers,
):
    captured = {}
    allocation = PortfolioAllocation(
        positions=[
            PortfolioPosition(
                ticker=ticker,
                weight_percent=40,
                rating=PortfolioRating.HOLD,
                rationale="CLI routing test.",
            )
            for ticker in expected_tickers
        ],
        cash_weight_percent=20,
        portfolio_summary="Test portfolio.",
        risk_note="Test only.",
    )

    class FakeGraph:
        def __init__(self, *, selected_analysts, config, debug):
            captured["config"] = config

        def propagate_portfolio(
            self,
            tickers,
            trade_date,
            *,
            max_position_percent,
            progress_callback,
        ):
            captured["tickers"] = tickers
            return {}, allocation

        def save_portfolio_report(self, allocation, save_path):
            return Path(save_path) / "portfolio_allocation.md"

    monkeypatch.setattr(cli_main, "TradingAgentsGraph", FakeGraph)
    monkeypatch.setattr(cli_main, "ensure_api_key", lambda *_: None)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "portfolio",
            user_input,
            "--date",
            "2026-08-07",
            "--save-path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["tickers"] == expected_tickers
    assert captured["config"]["data_vendors"]["core_stock_apis"] == "tushare"


def test_legacy_cli_normalization_preserves_yahoo_shanghai_suffix():
    assert cli_main.normalize_ticker_symbol("600519.SS") == "600519.SS"


def test_mixed_market_portfolio_normalizes_each_a_share_for_yahoo(
    monkeypatch,
    tmp_path,
):
    captured = {}
    expected_tickers = ["600519.SS", "AAPL"]
    allocation = PortfolioAllocation(
        positions=[
            PortfolioPosition(
                ticker=ticker,
                weight_percent=40,
                rating=PortfolioRating.HOLD,
                rationale="Mixed-market CLI routing test.",
            )
            for ticker in expected_tickers
        ],
        cash_weight_percent=20,
        portfolio_summary="Test portfolio.",
        risk_note="Test only.",
    )

    class FakeGraph:
        def __init__(self, *, selected_analysts, config, debug):
            captured["config"] = config

        def propagate_portfolio(
            self,
            tickers,
            trade_date,
            *,
            max_position_percent,
            progress_callback,
        ):
            captured["tickers"] = tickers
            return {}, allocation

        def save_portfolio_report(self, allocation, save_path):
            return Path(save_path) / "portfolio_allocation.md"

    monkeypatch.setattr(cli_main, "TradingAgentsGraph", FakeGraph)
    monkeypatch.setattr(cli_main, "ensure_api_key", lambda *_: None)
    compatible_config = deepcopy(cli_main.DEFAULT_CONFIG)
    for category in (
        "core_stock_apis",
        "technical_indicators",
        "fundamental_data",
    ):
        compatible_config["data_vendors"][category] = "tushare,yfinance"
    monkeypatch.setattr(cli_main, "DEFAULT_CONFIG", compatible_config)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "portfolio",
            "600519,AAPL",
            "--date",
            "2026-08-07",
            "--save-path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["tickers"] == expected_tickers
    assert (
        captured["config"]["data_vendors"]["core_stock_apis"]
        == cli_main.DEFAULT_CONFIG["data_vendors"]["core_stock_apis"]
    )


def test_mixed_market_portfolio_rejects_tushare_only_before_graph_or_llm(
    monkeypatch,
    tmp_path,
):
    incompatible_config = deepcopy(cli_main.DEFAULT_CONFIG)
    for category in (
        "core_stock_apis",
        "technical_indicators",
        "fundamental_data",
    ):
        incompatible_config["data_vendors"][category] = "tushare"
    graph_factory = MagicMock()
    ensure_key = MagicMock()
    monkeypatch.setattr(cli_main, "DEFAULT_CONFIG", incompatible_config)
    monkeypatch.setattr(cli_main, "TradingAgentsGraph", graph_factory)
    monkeypatch.setattr(cli_main, "ensure_api_key", ensure_key)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "portfolio",
            "600519,AAPL",
            "--date",
            "2026-08-07",
            "--save-path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 2
    assert "cannot cover" in result.output
    assert "non-A-share" in result.output
    assert "get_stock_data" in result.output
    graph_factory.assert_not_called()
    ensure_key.assert_not_called()
