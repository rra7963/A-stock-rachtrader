"""Tests for the eight-stock portfolio allocation mode."""

from copy import deepcopy
from unittest.mock import MagicMock, call

import pytest
from pydantic import ValidationError

from tradingagents.agents.managers.portfolio_allocator import (
    PortfolioAllocator,
    _capped_normalize,
)
from tradingagents.agents.schemas import (
    PortfolioAllocation,
    PortfolioPosition,
    PortfolioRating,
    render_portfolio_allocation,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.reporting import write_portfolio_report

TICKERS = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "JPM"]


def _state(rating: str) -> dict:
    return {
        "final_trade_decision": (
            f"**Rating**: {rating}\n\n"
            f"**Executive Summary**: {rating} setup.\n\n"
            "**Investment Thesis**: Evidence from the completed analysis."
        )
    }


def _analyses() -> dict[str, dict]:
    ratings = ["Buy", "Overweight", "Buy", "Hold", "Hold", "Underweight", "Sell", "Hold"]
    return {ticker: _state(rating) for ticker, rating in zip(TICKERS, ratings, strict=True)}


def _allocation(tickers=TICKERS) -> PortfolioAllocation:
    return PortfolioAllocation(
        positions=[
            PortfolioPosition(
                ticker=ticker,
                weight_percent=90 if ticker == "AAPL" else 1,
                rating=PortfolioRating.SELL,
                rationale=f"Relative case for {ticker}.",
            )
            for ticker in reversed(tickers)
        ],
        cash_weight_percent=3,
        portfolio_summary="Favor the strongest completed theses.",
        risk_note="Several candidates share technology-sector exposure.",
    )


@pytest.mark.unit
def test_schema_requires_between_two_and_twenty_positions():
    with pytest.raises(ValidationError):
        PortfolioAllocation(
            positions=_allocation().positions[:1],
            cash_weight_percent=10,
            portfolio_summary="summary",
            risk_note="risk",
        )


@pytest.mark.unit
def test_allocator_reorders_caps_and_normalizes_structured_result():
    structured = MagicMock()
    structured.invoke.return_value = _allocation()
    llm = MagicMock()
    llm.with_structured_output.return_value = structured

    result = PortfolioAllocator(llm, max_position_percent=25).allocate(
        TICKERS, _analyses(), "2026-08-07"
    )

    assert [position.ticker for position in result.positions] == TICKERS
    assert [position.rating.value for position in result.positions] == [
        "Buy",
        "Overweight",
        "Buy",
        "Hold",
        "Hold",
        "Underweight",
        "Sell",
        "Hold",
    ]
    assert max(position.weight_percent for position in result.positions) <= 25
    assert (
        sum(position.weight_percent for position in result.positions) + result.cash_weight_percent
        == 100
    )


@pytest.mark.unit
def test_allocator_sends_weight_shortfall_to_cash_instead_of_levering_stocks():
    proposed = _allocation()
    for position in proposed.positions:
        position.weight_percent = 1
    proposed.cash_weight_percent = 0
    structured = MagicMock()
    structured.invoke.return_value = proposed
    llm = MagicMock()
    llm.with_structured_output.return_value = structured

    result = PortfolioAllocator(llm).allocate(TICKERS, _analyses(), "2026-08-07")

    assert [position.weight_percent for position in result.positions] == [1] * 8
    assert result.cash_weight_percent == 92


@pytest.mark.unit
def test_overweight_proposal_preserves_explicit_cash_before_stocks():
    assert _capped_normalize(
        weights=[100, 100, 100],
        caps=[25, 25, 100],
    ) == [0.0, 0.0, 100.0]


@pytest.mark.unit
def test_overweight_proposal_scales_stocks_without_cutting_partial_cash():
    assert _capped_normalize(
        weights=[50, 50, 60],
        caps=[100, 100, 100],
    ) == [20.0, 20.0, 60.0]


@pytest.mark.unit
def test_allocator_has_deterministic_fallback_when_llm_output_is_invalid():
    llm = MagicMock()
    llm.with_structured_output.side_effect = NotImplementedError("unsupported")
    llm.invoke.return_value = MagicMock(content="not JSON")

    result = PortfolioAllocator(llm).allocate(TICKERS, _analyses(), "2026-08-07")

    assert len(result.positions) == 8
    assert (
        sum(position.weight_percent for position in result.positions) + result.cash_weight_percent
        == 100
    )
    assert result.positions[0].weight_percent > result.positions[6].weight_percent
    assert "fallback" in result.risk_note.lower()


@pytest.mark.unit
def test_rendered_result_leads_with_weights_not_actions():
    rendered = render_portfolio_allocation(_allocation())
    assert "| Ticker | Target weight | Rating | Rationale |" in rendered
    assert "| AAPL | 90.00% |" in rendered
    assert "| Cash | 3.00% |" in rendered


@pytest.mark.unit
def test_renderer_does_not_claim_an_invalid_total_is_normalized():
    allocation = PortfolioAllocation(
        positions=[
            PortfolioPosition(
                ticker=ticker,
                weight_percent=10,
                rating=PortfolioRating.HOLD,
                rationale="Test position.",
            )
            for ticker in ("AAPL", "MSFT")
        ],
        cash_weight_percent=0,
        portfolio_summary="Unnormalized programmatic input.",
        risk_note="The caller must normalize this result.",
    )

    rendered = render_portfolio_allocation(allocation)

    assert "Target weights sum to 100%" not in rendered
    assert "Reported target weights total 20.00%" in rendered


@pytest.mark.unit
def test_graph_rejects_wrong_count_before_running_any_analysis():
    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    with pytest.raises(ValueError, match="2 to 20"):
        graph.propagate_portfolio(TICKERS[:1], "2026-08-07")


@pytest.mark.unit
def test_graph_rejects_duplicate_tickers_before_running_any_analysis():
    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    with pytest.raises(ValueError, match="unique"):
        graph.propagate_portfolio([*TICKERS[:7], "AAPL"], "2026-08-07")


@pytest.mark.unit
@pytest.mark.parametrize(
    "tickers",
    [
        ["600519.SS", "600519.SH"],
        ["600519", "600519.SS"],
    ],
)
def test_graph_rejects_equivalent_a_share_symbols_before_analysis(tickers):
    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    graph.propagate = MagicMock()

    with pytest.raises(ValueError, match="unique"):
        graph.propagate_portfolio(tickers, "2026-08-07")

    graph.propagate.assert_not_called()


@pytest.mark.unit
def test_graph_rejects_mixed_market_tushare_only_before_analysis():
    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    graph.config = {
        "data_vendors": {
            "core_stock_apis": "tushare",
            "technical_indicators": "tushare",
            "fundamental_data": "tushare",
        }
    }
    graph.selected_analysts = ("market", "fundamentals")
    graph.propagate = MagicMock()

    with pytest.raises(
        ValueError,
        match="cannot cover non-A-share tickers for get_stock_data",
    ):
        graph.propagate_portfolio(["600519.SH", "AAPL"], "2026-08-07")

    graph.propagate.assert_not_called()


@pytest.mark.unit
def test_graph_checks_each_selected_analyst_vendor_chain_before_analysis():
    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    graph.config = {
        "data_vendors": {
            "core_stock_apis": "tushare,yfinance",
            "technical_indicators": "tushare",
            "fundamental_data": "tushare,yfinance",
        }
    }
    graph.selected_analysts = ("market", "fundamentals")
    graph.propagate = MagicMock()

    with pytest.raises(
        ValueError,
        match="cannot cover non-A-share tickers for get_indicators",
    ):
        graph.propagate_portfolio(["600519.SH", "AAPL"], "2026-08-07")

    graph.propagate.assert_not_called()


@pytest.mark.unit
def test_graph_honors_tool_level_vendor_override_during_preflight():
    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    graph.config = {
        "data_vendors": {
            "core_stock_apis": "tushare,yfinance",
            "technical_indicators": "tushare,yfinance",
            "fundamental_data": "tushare,yfinance",
        },
        "tool_vendors": {"get_indicators": "tushare"},
    }
    graph.selected_analysts = ("market",)
    graph.propagate = MagicMock()

    with pytest.raises(
        ValueError,
        match="cannot cover non-A-share tickers for get_indicators",
    ):
        graph.propagate_portfolio(["600519.SH", "AAPL"], "2026-08-07")

    graph.propagate.assert_not_called()


@pytest.mark.unit
def test_graph_accepts_explicit_cross_market_vendor_coverage():
    tickers = ["600519.SH", "AAPL"]
    analyses = {ticker: _state("Hold") for ticker in tickers}
    structured = MagicMock()
    structured.invoke.return_value = _allocation(tickers)
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    graph.config = {
        "data_vendors": {
            "core_stock_apis": "tushare,yfinance",
            "technical_indicators": "tushare,yfinance",
            "fundamental_data": "tushare,yfinance",
        }
    }
    graph.selected_analysts = ("market", "fundamentals")
    graph.deep_thinking_llm = llm
    graph.propagate = MagicMock(
        side_effect=[(analyses[ticker], "ignored") for ticker in tickers]
    )

    graph.propagate_portfolio(tickers, "2026-08-07")

    assert graph.propagate.call_args_list == [
        call(ticker, "2026-08-07", asset_type="stock") for ticker in tickers
    ]


@pytest.mark.unit
def test_graph_routes_bare_a_shares_to_provider_symbols_before_analysis():
    display_tickers = ["600519", "000001"]
    equivalent_suffixed_tickers = ["600519.SH", "000001.SZ"]
    analysis_tickers = ["600519.SS", "000001.SZ"]
    analyses = {ticker: _state("Hold") for ticker in display_tickers}
    structured = MagicMock()
    structured.invoke.side_effect = [
        _allocation(display_tickers),
        _allocation(equivalent_suffixed_tickers),
    ]
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    graph.config = deepcopy(DEFAULT_CONFIG)
    graph.deep_thinking_llm = llm
    graph.propagate = MagicMock(
        side_effect=[
            (analyses[ticker], "ignored")
            for ticker in [*display_tickers, *display_tickers]
        ]
    )

    states, _ = graph.propagate_portfolio(display_tickers, "2026-08-07")

    assert graph.propagate.call_args_list == [
        call(ticker, "2026-08-07", asset_type="stock")
        for ticker in analysis_tickers
    ]
    assert list(states) == display_tickers

    bare_benchmark = graph._resolve_benchmark(analysis_tickers[0])
    graph.propagate.reset_mock()
    graph.propagate_portfolio(equivalent_suffixed_tickers, "2026-08-07")

    assert graph.propagate.call_args_list == [
        call(ticker, "2026-08-07", asset_type="stock")
        for ticker in analysis_tickers
    ]
    assert bare_benchmark == graph._resolve_benchmark(analysis_tickers[0])


@pytest.mark.unit
def test_graph_runs_each_stock_then_allocates_the_combined_portfolio():
    analyses = _analyses()
    structured = MagicMock()
    structured.invoke.return_value = _allocation()
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    graph.deep_thinking_llm = llm
    graph.propagate = MagicMock(side_effect=[(analyses[ticker], "ignored") for ticker in TICKERS])
    progress = MagicMock()

    states, allocation = graph.propagate_portfolio(
        TICKERS,
        "2026-08-07",
        progress_callback=progress,
    )

    assert list(states) == TICKERS
    assert graph.propagate.call_count == 8
    assert progress.call_count == 8
    assert allocation is graph.curr_portfolio_allocation
    assert (
        sum(position.weight_percent for position in allocation.positions)
        + allocation.cash_weight_percent
        == 100
    )


@pytest.mark.unit
def test_a_share_tushare_portfolio_still_runs_full_graph_per_stock():
    tickers = ["600519.SH", "000001.SZ"]
    analyses = {ticker: _state("Hold") for ticker in tickers}
    structured = MagicMock()
    structured.invoke.return_value = _allocation(tickers)
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    graph.config = {"data_vendors": {"core_stock_apis": "tushare"}}
    graph.deep_thinking_llm = llm
    graph.propagate = MagicMock(
        side_effect=[(analyses[ticker], "ignored") for ticker in tickers]
    )

    graph.propagate_portfolio(tickers, "2026-08-07")

    assert graph.propagate.call_args_list == [
        call(ticker, "2026-08-07", asset_type="stock") for ticker in tickers
    ]


@pytest.mark.unit
def test_portfolio_report_writes_markdown_and_json(tmp_path):
    path = write_portfolio_report(_allocation(), tmp_path)
    assert path.name == "portfolio_allocation.md"
    assert path.exists()
    assert (tmp_path / "portfolio_allocation.json").exists()
    assert "Target weight" in path.read_text(encoding="utf-8")
