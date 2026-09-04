# TradingAgents/graph/trading_graph.py

import hashlib
import json
import logging
import os
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf
from langgraph.prebuilt import ToolNode

# Import the abstract tool methods from agent_utils
from tradingagents.agents.managers.portfolio_allocator import (
    MAX_PORTFOLIO_SIZE,
    MIN_PORTFOLIO_SIZE,
    PortfolioAllocator,
)
from tradingagents.agents.schemas import PortfolioAllocation
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_balance_sheet,
    get_cashflow,
    get_fundamentals,
    get_global_news,
    get_income_statement,
    get_indicators,
    get_insider_transactions,
    get_macro_indicators,
    get_news,
    get_prediction_markets,
    get_stock_data,
    get_verified_market_snapshot,
    resolve_instrument_identity,
)
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.utils import safe_ticker_component
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients import create_llm_client
from tradingagents.reporting import write_portfolio_report, write_report_tree

from .checkpointer import checkpoint_step, clear_checkpoint, get_checkpointer, thread_id
from .conditional_logic import ConditionalLogic
from .propagation import Propagator
from .reflection import Reflector
from .setup import GraphSetup
from .signal_processing import SignalProcessor

logger = logging.getLogger(__name__)

_PORTFOLIO_ANALYST_DATA_METHODS = {
    "market": (
        ("core_stock_apis", "get_stock_data"),
        ("technical_indicators", "get_indicators"),
    ),
    "fundamentals": (
        ("fundamental_data", "get_fundamentals"),
        ("fundamental_data", "get_balance_sheet"),
        ("fundamental_data", "get_cashflow"),
        ("fundamental_data", "get_income_statement"),
    ),
}
_A_SHARE_NATIVE_DATA_VENDORS = frozenset(
    {"aliyun_stock_postgres", "etf_stock_postgres", "tushare"}
)
_NON_A_SHARE_DATA_VENDORS = frozenset({"alpha_vantage", "yfinance"})
_DATA_VENDOR_ENV_BY_CATEGORY = {
    "core_stock_apis": "TRADINGAGENTS_DATA_VENDOR_CORE_STOCK_APIS",
    "technical_indicators": "TRADINGAGENTS_DATA_VENDOR_TECHNICAL_INDICATORS",
    "fundamental_data": "TRADINGAGENTS_DATA_VENDOR_FUNDAMENTAL_DATA",
}


def _configured_portfolio_vendor_chain(
    config: dict[str, Any],
    category: str,
    method: str,
) -> tuple[str, ...]:
    """Return the effective explicit chain using the shared router contract."""
    from tradingagents.dataflows.interface import (
        VENDOR_METHODS,
        get_vendor_from_config,
    )

    configured = get_vendor_from_config(config, category, method)
    if isinstance(configured, str):
        requested = tuple(
            part.strip() for part in configured.split(",") if part.strip()
        )
    else:
        requested = tuple(
            str(part).strip() for part in configured if str(part).strip()
        )

    explicit = tuple(vendor for vendor in requested if vendor != "default")
    available = VENDOR_METHODS[method]
    if explicit:
        chain = tuple(vendor for vendor in explicit if vendor in available)
        if not chain:
            raise ValueError(
                f"configured vendor chain {explicit!r} has no provider for {method}"
            )
        return chain
    return tuple(available)


def validate_portfolio_data_vendor_coverage(
    tickers: Iterable[str],
    config: dict[str, Any],
    *,
    selected_analysts: Iterable[str] = ("market", "fundamentals"),
) -> str:
    """Fail before analysis when configured vendors cannot cover every market.

    The check does not add or reorder providers. It applies the same category and
    tool-level precedence as the data router, then verifies only the methods used
    by the selected portfolio analysts. The returned value is the first effective
    core-price vendor, which also determines the A-share symbol form used below.
    """
    from tradingagents.dataflows.symbol_utils import normalize_a_share_symbol

    ticker_list = [str(ticker).strip() for ticker in tickers]
    a_share_flags = [
        normalize_a_share_symbol(ticker) is not None for ticker in ticker_list
    ]
    requires_a_share = any(a_share_flags)
    requires_non_a_share = any(not flag for flag in a_share_flags)

    core_chain = _configured_portfolio_vendor_chain(
        config,
        "core_stock_apis",
        "get_stock_data",
    )
    yahoo_a_share_symbols = core_chain[0] == "yfinance"

    required_methods: list[tuple[str, str]] = []
    for analyst in selected_analysts:
        for requirement in _PORTFOLIO_ANALYST_DATA_METHODS.get(str(analyst), ()):
            if requirement not in required_methods:
                required_methods.append(requirement)

    for category, method in required_methods:
        chain = _configured_portfolio_vendor_chain(config, category, method)
        missing_markets = []
        supports_a_share = bool(
            _A_SHARE_NATIVE_DATA_VENDORS.intersection(chain)
            or (yahoo_a_share_symbols and "yfinance" in chain)
        )
        supports_non_a_share = bool(_NON_A_SHARE_DATA_VENDORS.intersection(chain))
        if requires_a_share and not supports_a_share:
            missing_markets.append("A-share")
        if requires_non_a_share and not supports_non_a_share:
            missing_markets.append("non-A-share")
        if missing_markets:
            env_name = _DATA_VENDOR_ENV_BY_CATEGORY[category]
            configured_text = ",".join(chain)
            missing_text = " and ".join(missing_markets)
            raise ValueError(
                "portfolio data-vendor configuration cannot cover "
                f"{missing_text} tickers for {method} "
                f"(effective {category} chain: {configured_text}). "
                f"Set {env_name} to a compatible explicit chain such as "
                "'tushare,yfinance', or split the portfolio by market."
            )

    return core_chain[0]


def _coerce_max_retries(value):
    """Validate an ``llm_max_retries`` value to a non-negative int.

    Accepts an int or a numeric string (env vars arrive as strings). Rejects
    booleans and negatives loudly so a misconfiguration fails at startup rather
    than silently disabling retries.
    """
    if isinstance(value, bool):
        raise ValueError(f"llm_max_retries must be an integer, not a boolean: {value!r}")
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"llm_max_retries must be an integer, got {value!r}") from exc
    if n < 0:
        raise ValueError(f"llm_max_retries must be >= 0, got {n}")
    return n


class TradingAgentsGraph:
    """Main class that orchestrates the trading agents framework."""

    def __init__(
        self,
        selected_analysts=("market", "social", "news", "fundamentals"),
        debug=False,
        config: dict[str, Any] = None,
        callbacks: list | None = None,
    ):
        """Initialize the trading agents graph and components.

        Args:
            selected_analysts: List of analyst types to include
            debug: Whether to run in debug mode
            config: Configuration dictionary. If None, uses default config
            callbacks: Optional list of callback handlers (e.g., for tracking LLM/tool stats)
        """
        self.debug = debug
        self.config = config or DEFAULT_CONFIG
        self.callbacks = callbacks or []

        # Update the interface's config
        set_config(self.config)

        # Create necessary directories
        os.makedirs(self.config["data_cache_dir"], exist_ok=True)
        os.makedirs(self.config["results_dir"], exist_ok=True)

        # Initialize LLMs with provider-specific thinking configuration
        llm_kwargs = self._get_provider_kwargs()

        # Add callbacks to kwargs if provided (passed to LLM constructor)
        if self.callbacks:
            llm_kwargs["callbacks"] = self.callbacks

        deep_client = create_llm_client(
            provider=self.config["llm_provider"],
            model=self.config["deep_think_llm"],
            base_url=self.config.get("backend_url"),
            **llm_kwargs,
        )
        quick_client = create_llm_client(
            provider=self.config["llm_provider"],
            model=self.config["quick_think_llm"],
            base_url=self.config.get("backend_url"),
            **llm_kwargs,
        )

        self.deep_thinking_llm = deep_client.get_llm()
        self.quick_thinking_llm = quick_client.get_llm()

        self.memory_log = TradingMemoryLog(self.config)

        # Create tool nodes
        self.tool_nodes = self._create_tool_nodes()

        # Initialize components
        self.conditional_logic = ConditionalLogic(
            max_debate_rounds=self.config["max_debate_rounds"],
            max_risk_discuss_rounds=self.config["max_risk_discuss_rounds"],
        )
        self.graph_setup = GraphSetup(
            self.quick_thinking_llm,
            self.deep_thinking_llm,
            self.tool_nodes,
            self.conditional_logic,
        )

        self.propagator = Propagator(
            max_recur_limit=self.config.get("max_recur_limit", 100),
        )
        self.reflector = Reflector(self.quick_thinking_llm)
        self.signal_processor = SignalProcessor(self.quick_thinking_llm)

        # State tracking
        self.curr_state = None
        self.curr_portfolio_allocation: PortfolioAllocation | None = None
        self.ticker = None
        self.log_states_dict = {}  # date to full state dict

        # Graph-shape-affecting run choices, kept for the checkpoint signature.
        self.selected_analysts = tuple(selected_analysts)

        # Set up the graph: keep the workflow for recompilation with a checkpointer.
        self.workflow = self.graph_setup.setup_graph(selected_analysts)
        self.graph = self.workflow.compile()
        self._checkpointer_ctx = None

    def _get_provider_kwargs(self) -> dict[str, Any]:
        """Get provider-specific kwargs for LLM client creation."""
        kwargs = {}
        provider = self.config.get("llm_provider", "").lower()

        if provider == "google":
            thinking_level = self.config.get("google_thinking_level")
            if thinking_level:
                kwargs["thinking_level"] = thinking_level

        elif provider == "openai":
            reasoning_effort = self.config.get("openai_reasoning_effort")
            if reasoning_effort:
                kwargs["reasoning_effort"] = reasoning_effort

        elif provider == "anthropic":
            effort = self.config.get("anthropic_effort")
            if effort:
                kwargs["effort"] = effort

        # Sampling temperature is cross-provider: forward it whenever set.
        # float() here so a value coming from a TRADINGAGENTS_TEMPERATURE env
        # string ("0.2") works the same as a programmatic float.
        temperature = self.config.get("temperature")
        if temperature is not None and temperature != "":
            kwargs["temperature"] = float(temperature)

        # SDK retry budget is cross-provider. Forward it only when explicitly set
        # so each provider keeps its own default (usually 2) otherwise (#1091).
        max_retries = self.config.get("llm_max_retries")
        if max_retries is not None and max_retries != "":
            kwargs["max_retries"] = _coerce_max_retries(max_retries)

        return kwargs

    def _create_tool_nodes(self) -> dict[str, ToolNode]:
        """Create tool nodes for different data sources using abstract methods."""
        return {
            "market": ToolNode(
                [
                    # Core stock data tools
                    get_stock_data,
                    # Technical indicators
                    get_indicators,
                    # Deterministic verification snapshot (bound to the analyst
                    # LLM and required by its prompt; must be executable here or
                    # the call fails and the model reports it "unavailable").
                    get_verified_market_snapshot,
                ]
            ),
            "social": ToolNode(
                [
                    # News tools for social media analysis
                    get_news,
                ]
            ),
            "news": ToolNode(
                [
                    # News and insider information
                    get_news,
                    get_global_news,
                    get_insider_transactions,
                    get_macro_indicators,
                    get_prediction_markets,
                ]
            ),
            "fundamentals": ToolNode(
                [
                    # Fundamental analysis tools
                    get_fundamentals,
                    get_balance_sheet,
                    get_cashflow,
                    get_income_statement,
                ]
            ),
        }

    def _resolve_benchmark(self, ticker: str) -> str:
        """Pick the benchmark ticker for alpha calculation against ``ticker``.

        ``config["benchmark_ticker"]`` overrides everything when set; otherwise
        the suffix map matches the ticker's exchange suffix (e.g. ``.T`` for
        Tokyo). US-listed tickers without a dotted suffix fall through to the
        empty-suffix entry (SPY by default). Unrecognised suffixes (including
        US tickers with dots like ``BRK.B``) also fall back to the empty-suffix
        entry, which is the right default because the alpha calculation works
        in USD.
        """
        explicit = self.config.get("benchmark_ticker")
        if explicit:
            return explicit
        benchmark_map = self.config.get("benchmark_map", {})
        ticker_upper = ticker.upper()
        for suffix, benchmark in benchmark_map.items():
            if suffix and ticker_upper.endswith(suffix.upper()):
                return benchmark
        return benchmark_map.get("", "SPY")

    def _fetch_returns(
        self,
        ticker: str,
        trade_date: str,
        holding_days: int = 5,
        benchmark: str = "SPY",
    ) -> tuple[float | None, float | None, int | None]:
        """Fetch raw and alpha return for ticker over holding_days from trade_date.

        ``benchmark`` is the index used as the alpha baseline (resolved by the
        caller via ``_resolve_benchmark``). Returns ``(raw_return, alpha_return,
        actual_holding_days)`` or ``(None, None, None)`` if price data is
        unavailable (too recent, delisted, or network error).
        """
        try:
            start = datetime.strptime(trade_date, "%Y-%m-%d")
            end = start + timedelta(days=holding_days + 7)  # buffer for weekends/holidays
            end_str = end.strftime("%Y-%m-%d")

            def configured_a_share_prices(
                symbol: str,
                *,
                is_benchmark: bool,
            ) -> pd.Series:
                from tradingagents.dataflows.interface import (
                    VENDOR_METHODS,
                    get_vendor_from_config,
                )
                from tradingagents.dataflows.symbol_utils import normalize_a_share_symbol

                config = getattr(self, "config", {})
                if not isinstance(config, dict):
                    config = {}
                configured = get_vendor_from_config(
                    config,
                    "core_stock_apis",
                    "get_stock_data",
                )
                if isinstance(configured, str):
                    vendors = [v.strip() for v in configured.split(",") if v.strip()]
                else:
                    vendors = [str(v).strip() for v in configured if str(v).strip()]
                if not vendors or vendors == ["default"]:
                    vendors = list(VENDOR_METHODS["get_stock_data"])

                canonical = normalize_a_share_symbol(symbol) or symbol.strip().upper()
                last_error: Exception | None = None
                for vendor in vendors:
                    provider_symbol = canonical
                    try:
                        if vendor == "tushare":
                            from tradingagents.dataflows.tushare_stock import (
                                get_benchmark_close_prices,
                                get_close_prices,
                            )

                            loader = (
                                get_benchmark_close_prices
                                if is_benchmark
                                else get_close_prices
                            )
                            return loader(canonical, trade_date, end_str)

                        vendor_impl = VENDOR_METHODS["get_stock_data"].get(vendor)
                        if vendor_impl is None:
                            continue
                        if isinstance(vendor_impl, list):
                            vendor_impl = vendor_impl[0]
                        if vendor == "yfinance" and provider_symbol.endswith(".SH"):
                            # Tushare and the A-share databases use Shanghai
                            # ``.SH`` while Yahoo Finance expects ``.SS``.
                            provider_symbol = provider_symbol[:-3] + ".SS"
                        csv_data = vendor_impl(provider_symbol, trade_date, end_str)
                        frame = pd.read_csv(StringIO(str(csv_data)), comment="#")
                        if not {"Date", "Close"}.issubset(frame.columns):
                            raise ValueError(f"{vendor} returned no Date/Close columns")
                        frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
                        frame["Close"] = pd.to_numeric(frame["Close"], errors="coerce")
                        series = (
                            frame.dropna(subset=["Date", "Close"])
                            .sort_values("Date")
                            .set_index("Date")["Close"]
                        )
                        if len(series) < 2:
                            raise ValueError(f"{vendor} returned insufficient close history")
                        return series
                    except Exception as exc:  # try the next explicitly configured vendor
                        logger.warning(
                            "Return-price vendor %s failed for %s: %s",
                            vendor,
                            provider_symbol,
                            exc,
                        )
                        last_error = exc
                if last_error is not None:
                    raise last_error
                raise ValueError(f"no configured return-price vendor for {canonical}")

            def close_prices_for(symbol: str, *, is_benchmark: bool) -> pd.Series:
                from tradingagents.dataflows.symbol_utils import is_a_share_symbol

                if is_a_share_symbol(symbol):
                    return configured_a_share_prices(
                        symbol,
                        is_benchmark=is_benchmark,
                    )

                from tradingagents.dataflows.symbol_utils import normalize_symbol

                return yf.Ticker(normalize_symbol(symbol)).history(
                    start=trade_date, end=end_str
                )["Close"]

            stock_close = close_prices_for(ticker, is_benchmark=False)
            bench_close = close_prices_for(benchmark, is_benchmark=True)

            def normalize_dates(series: pd.Series) -> pd.Series:
                values = pd.to_numeric(series, errors="coerce")
                if isinstance(values.index, pd.RangeIndex):
                    # Preserve compatibility with minimal/custom providers that
                    # return ordered closes without a date index.
                    dates = pd.bdate_range(trade_date, periods=len(values))
                else:
                    # Align by each market's local trading-session date. Converting
                    # Tokyo midnight to UTC first would move it to the previous day
                    # and compare different sessions against a New York benchmark.
                    def local_session_date(raw_date: object) -> pd.Timestamp:
                        try:
                            stamp = pd.Timestamp(raw_date)
                        except (TypeError, ValueError):
                            return pd.NaT
                        if pd.isna(stamp):
                            return pd.NaT
                        if stamp.tzinfo is not None:
                            stamp = stamp.tz_localize(None)
                        return stamp.normalize()

                    dates = pd.DatetimeIndex(
                        [local_session_date(raw_date) for raw_date in values.index]
                    )
                normalized = pd.Series(
                    values.to_numpy(),
                    index=dates.normalize(),
                    dtype=float,
                )
                return normalized[~normalized.index.isna()].dropna().groupby(level=0).last()

            aligned = pd.concat(
                {
                    "stock": normalize_dates(stock_close),
                    "benchmark": normalize_dates(bench_close),
                },
                axis=1,
                join="inner",
            ).dropna()
            if len(aligned) < 2:
                return None, None, None

            actual_days = min(holding_days, len(aligned) - 1)
            raw = float(
                (aligned["stock"].iloc[actual_days] - aligned["stock"].iloc[0])
                / aligned["stock"].iloc[0]
            )
            bench_ret = float(
                (aligned["benchmark"].iloc[actual_days] - aligned["benchmark"].iloc[0])
                / aligned["benchmark"].iloc[0]
            )
            alpha = raw - bench_ret
            return raw, alpha, actual_days
        except Exception as e:
            logger.warning(
                "Could not resolve outcome for %s on %s vs %s (will retry next run): %s",
                ticker,
                trade_date,
                benchmark,
                e,
            )
            return None, None, None

    def _resolve_pending_entries(self, ticker: str) -> None:
        """Resolve pending log entries for ticker at the start of a new run.

        Fetches returns for each same-ticker pending entry, generates reflections,
        then writes all updates in a single atomic batch write to avoid redundant I/O.
        Skips entries whose price data is not yet available (too recent or delisted).

        Trade-off: only same-ticker entries are resolved per run.  Entries for
        other tickers accumulate until that ticker is run again.
        """
        pending = [e for e in self.memory_log.get_pending_entries() if e["ticker"] == ticker]
        if not pending:
            return

        benchmark = self._resolve_benchmark(ticker)
        updates = []
        for entry in pending:
            raw, alpha, days = self._fetch_returns(
                ticker,
                entry["date"],
                benchmark=benchmark,
            )
            if raw is None:
                continue  # price not available yet — try again next run
            reflection = self.reflector.reflect_on_final_decision(
                final_decision=entry.get("decision", ""),
                raw_return=raw,
                alpha_return=alpha,
                benchmark_name=benchmark,
            )
            updates.append(
                {
                    "ticker": ticker,
                    "trade_date": entry["date"],
                    "raw_return": raw,
                    "alpha_return": alpha,
                    "holding_days": days,
                    "reflection": reflection,
                }
            )

        if updates:
            self.memory_log.batch_update_with_outcomes(updates)

    def resolve_instrument_context(self, ticker: str, asset_type: str = "stock") -> str:
        """Resolve ticker identity once and return the full instrument context.

        Deterministic yfinance lookup (cached, fail-open) injected into a
        context string so every agent anchors to the real company instead of
        hallucinating one from the price chart (#814). Both the propagate()
        path and the CLI call this so the resolved identity reaches the whole
        graph regardless of entry point.
        """
        identity = resolve_instrument_identity(ticker)
        return build_instrument_context(ticker, asset_type, identity)

    def _run_signature(self, asset_type: str, trigger_event_context: str = "") -> str:
        """Graph-shape inputs that must invalidate a checkpoint if changed.

        Keyed into the checkpoint thread ID so a resume under a different analyst
        selection, debate/risk depth, or asset mode starts fresh instead of
        silently continuing the previous graph (#1089).
        """
        trigger_digest = hashlib.sha256(trigger_event_context.encode()).hexdigest()[:16]
        return "|".join([
            "analysts=" + ",".join(self.selected_analysts),
            f"debate={self.config['max_debate_rounds']}",
            f"risk={self.config['max_risk_discuss_rounds']}",
            f"asset={asset_type}",
            f"trigger={trigger_digest}",
        ])

    def propagate(
        self,
        company_name,
        trade_date,
        asset_type: str = "stock",
        trigger_event_context: str = "",
    ):
        """Run the trading agents graph for a company on a specific date.

        ``asset_type`` selects between the stock pipeline (default) and the
        crypto pipeline (``"crypto"``) shipped in #567 — the CLI auto-detects
        from the ticker; programmatic callers pass it explicitly.
        ``trigger_event_context`` supplies optional, explicitly untrusted event
        evidence for machine callers. It is part of checkpoint identity so two
        events for the same ticker/date cannot share graph state. When
        ``checkpoint_enabled`` is set in config, the graph is recompiled with
        a per-ticker SqliteSaver so a crashed run can resume from the last
        successful node on a subsequent invocation with the same ticker+date.
        """
        if not isinstance(trigger_event_context, str):
            raise TypeError("trigger_event_context must be a string")
        if len(trigger_event_context) > 24000:
            raise ValueError("trigger_event_context exceeds 24000 characters")

        self.ticker = company_name

        # Resolve any pending memory-log entries for this ticker before the pipeline runs.
        self._resolve_pending_entries(company_name)

        # Recompile with a checkpointer if the user opted in.
        if self.config.get("checkpoint_enabled"):
            self._checkpointer_ctx = get_checkpointer(self.config["data_cache_dir"], company_name)
            saver = self._checkpointer_ctx.__enter__()
            self.graph = self.workflow.compile(checkpointer=saver)

            step = checkpoint_step(
                self.config["data_cache_dir"], company_name, str(trade_date),
                self._run_signature(asset_type, trigger_event_context),
            )
            if step is not None:
                logger.info("Resuming from step %d for %s on %s", step, company_name, trade_date)
            else:
                logger.info("Starting fresh for %s on %s", company_name, trade_date)

        try:
            return self._run_graph(
                company_name,
                trade_date,
                asset_type=asset_type,
                trigger_event_context=trigger_event_context,
            )
        finally:
            if self._checkpointer_ctx is not None:
                self._checkpointer_ctx.__exit__(None, None, None)
                self._checkpointer_ctx = None
                self.graph = self.workflow.compile()

    def propagate_portfolio(
        self,
        tickers: Iterable[str],
        trade_date: str,
        *,
        max_position_percent: float = 25.0,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> tuple[dict[str, dict[str, Any]], PortfolioAllocation]:
        """Analyze multiple stocks and convert the results into target weights.

        Each ticker first runs through the unchanged single-stock graph. A
        cross-sectional allocator then compares the completed Portfolio
        Manager decisions and returns long-only stock weights plus cash. The
        weights are deterministically capped and normalized to exactly 100%.

        Args:
            tickers: Between two and twenty unique stock symbols.
            trade_date: Shared analysis date in ``YYYY-MM-DD`` form.
            max_position_percent: Maximum target weight for any one stock.
            progress_callback: Optional ``(ticker, index, total)`` callback fired
                before each single-stock analysis.
        """
        if isinstance(tickers, (str, bytes)):
            raise ValueError("tickers must be an iterable of stock symbols")

        from tradingagents.dataflows.symbol_utils import (
            normalize_a_share_symbol,
            normalize_symbol,
        )

        # Keep the caller-facing symbol separate from the security identity and
        # the symbol sent to the configured market-data provider.  In
        # particular, a bare A-share code is a valid public input but is not a
        # valid Yahoo query symbol and must not fall through to the US benchmark.
        display_tickers = [normalize_symbol(str(ticker).strip()) for ticker in tickers]
        if not MIN_PORTFOLIO_SIZE <= len(display_tickers) <= MAX_PORTFOLIO_SIZE:
            raise ValueError(
                f"portfolio mode requires {MIN_PORTFOLIO_SIZE} to "
                f"{MAX_PORTFOLIO_SIZE} tickers; "
                f"received {len(display_tickers)}"
            )
        if any(not ticker for ticker in display_tickers):
            raise ValueError("portfolio tickers cannot be empty")

        a_share_tickers = [
            normalize_a_share_symbol(ticker) for ticker in display_tickers
        ]
        canonical_tickers = [
            a_share_ticker or display_ticker
            for display_ticker, a_share_ticker in zip(
                display_tickers,
                a_share_tickers,
                strict=True,
            )
        ]
        canonical_keys = [ticker.casefold() for ticker in canonical_tickers]
        if len(set(canonical_keys)) != len(display_tickers):
            raise ValueError("portfolio tickers must be unique")

        config = getattr(self, "config", {})
        if not isinstance(config, dict):
            config = {}
        primary_vendor = validate_portfolio_data_vendor_coverage(
            display_tickers,
            config,
            selected_analysts=getattr(
                self,
                "selected_analysts",
                ("market", "fundamentals"),
            ),
        )

        analysis_tickers = []
        for display_ticker, a_share_ticker in zip(
            display_tickers,
            a_share_tickers,
            strict=True,
        ):
            if a_share_ticker is None:
                analysis_tickers.append(display_ticker)
            elif primary_vendor == "yfinance" and a_share_ticker.endswith(".SH"):
                analysis_tickers.append(a_share_ticker[:-3] + ".SS")
            else:
                analysis_tickers.append(a_share_ticker)

        allocator = PortfolioAllocator(
            self.deep_thinking_llm,
            max_position_percent=max_position_percent,
        )
        analyses: dict[str, dict[str, Any]] = {}
        for index, (display_ticker, analysis_ticker) in enumerate(
            zip(display_tickers, analysis_tickers, strict=True),
            start=1,
        ):
            if progress_callback is not None:
                progress_callback(display_ticker, index, len(display_tickers))
            state, _ = self.propagate(
                analysis_ticker,
                trade_date,
                asset_type="stock",
            )
            analyses[display_ticker] = state

        allocation = allocator.allocate(display_tickers, analyses, str(trade_date))
        self.curr_portfolio_allocation = allocation
        return analyses, allocation

    def save_reports(self, final_state, ticker, save_path=None) -> Path:
        """Write the markdown report tree for a completed run, like the CLI does.

        Programmatic callers get the same on-disk reports the CLI produces. Pass
        an explicit ``save_path`` or let it default under ``results_dir``.
        """
        if save_path is None:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            save_path = (
                Path(self.config["results_dir"])
                / "reports"
                / f"{safe_ticker_component(ticker)}_{stamp}"
            )
        return write_report_tree(final_state, ticker, save_path)

    def save_portfolio_report(
        self,
        allocation: PortfolioAllocation,
        save_path=None,
    ) -> Path:
        """Save a portfolio allocation as both markdown and machine-readable JSON."""
        if save_path is None:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            save_path = Path(self.config["results_dir"]) / "portfolios" / stamp
        return write_portfolio_report(allocation, save_path)

    def _run_graph(
        self,
        company_name,
        trade_date,
        asset_type: str = "stock",
        trigger_event_context: str = "",
    ):
        """Execute the graph and write the resulting state to disk and memory log."""
        # Initialize state — inject memory log context for PM and the
        # deterministically resolved instrument identity for all agents.
        past_context = self.memory_log.get_past_context(company_name)
        instrument_context = self.resolve_instrument_context(company_name, asset_type)
        init_agent_state = self.propagator.create_initial_state(
            company_name,
            trade_date,
            asset_type=asset_type,
            past_context=past_context,
            instrument_context=instrument_context,
            trigger_event_context=trigger_event_context,
        )
        args = self.propagator.get_graph_args()

        # Inject thread_id so same ticker+date+graph-shape resumes; a different
        # date or graph shape starts fresh (#1089).
        if self.config.get("checkpoint_enabled"):
            tid = thread_id(
                company_name,
                str(trade_date),
                self._run_signature(asset_type, trigger_event_context),
            )
            args.setdefault("config", {}).setdefault("configurable", {})["thread_id"] = tid

        if self.debug:
            trace = []
            last_printed = None
            for chunk in self.graph.stream(init_agent_state, **args):
                if chunk["messages"]:
                    msg = chunk["messages"][-1]
                    # Nodes after the trader don't append to messages, so the
                    # same trailing message repeats across chunks. Print it only
                    # when it changes (#1027); the trace/state merge is unchanged.
                    signature = (type(msg).__name__, getattr(msg, "content", None))
                    if signature != last_printed:
                        msg.pretty_print()
                        last_printed = signature
                    trace.append(chunk)
            # Streamed chunks are per-node deltas. Merge them so the returned
            # state matches what graph.invoke() yields in the non-debug path.
            final_state = {}
            for chunk in trace:
                final_state.update(chunk)
        else:
            final_state = self.graph.invoke(init_agent_state, **args)

        # Store current state for reflection.
        self.curr_state = final_state

        # Log state to disk.
        self._log_state(trade_date, final_state)

        # Store decision for deferred reflection on the next same-ticker run.
        self.memory_log.store_decision(
            ticker=company_name,
            trade_date=trade_date,
            final_trade_decision=final_state["final_trade_decision"],
        )

        # Clear checkpoint on successful completion to avoid stale state.
        if self.config.get("checkpoint_enabled"):
            clear_checkpoint(
                self.config["data_cache_dir"], company_name, str(trade_date),
                self._run_signature(asset_type, trigger_event_context),
            )

        return final_state, self.process_signal(final_state["final_trade_decision"])

    def _log_state(self, trade_date, final_state):
        """Log the final state to a JSON file."""
        self.log_states_dict[str(trade_date)] = {
            "company_of_interest": final_state["company_of_interest"],
            "trade_date": final_state["trade_date"],
            "market_report": final_state["market_report"],
            "sentiment_report": final_state["sentiment_report"],
            "news_report": final_state["news_report"],
            "fundamentals_report": final_state["fundamentals_report"],
            "investment_debate_state": {
                "bull_history": final_state["investment_debate_state"]["bull_history"],
                "bear_history": final_state["investment_debate_state"]["bear_history"],
                "history": final_state["investment_debate_state"]["history"],
                "current_response": final_state["investment_debate_state"]["current_response"],
                "judge_decision": final_state["investment_debate_state"]["judge_decision"],
            },
            "trader_investment_decision": final_state["trader_investment_plan"],
            "risk_debate_state": {
                "aggressive_history": final_state["risk_debate_state"]["aggressive_history"],
                "conservative_history": final_state["risk_debate_state"]["conservative_history"],
                "neutral_history": final_state["risk_debate_state"]["neutral_history"],
                "history": final_state["risk_debate_state"]["history"],
                "judge_decision": final_state["risk_debate_state"]["judge_decision"],
            },
            "investment_plan": final_state["investment_plan"],
            "final_trade_decision": final_state["final_trade_decision"],
        }

        # Save to file. Reject ticker values that would escape the
        # results directory when joined as a path component.
        safe_ticker = safe_ticker_component(self.ticker)
        directory = Path(self.config["results_dir"]) / safe_ticker / "TradingAgentsStrategy_logs"
        directory.mkdir(parents=True, exist_ok=True)

        log_path = directory / f"full_states_log_{trade_date}.json"
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(self.log_states_dict[str(trade_date)], f, indent=4)

    def process_signal(self, full_signal):
        """Process a signal to extract the core decision."""
        return self.signal_processor.process_signal(full_signal)
