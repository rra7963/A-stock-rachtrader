"""Cross-sectional allocator for a multi-stock portfolio.

The existing Portfolio Manager deliberately makes one decision for one ticker.
This module sits one level above it: after all eight single-ticker graphs have
finished, it compares their final theses and converts them into target weights.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from tradingagents.agents.schemas import (
    PortfolioAllocation,
    PortfolioPosition,
    PortfolioRating,
)
from tradingagents.agents.utils.agent_utils import get_language_instruction
from tradingagents.agents.utils.rating import parse_rating
from tradingagents.agents.utils.structured import NO_EXTERNAL_TOOLS, bind_structured
from tradingagents.llm_clients.base_client import normalize_content

logger = logging.getLogger(__name__)

MIN_PORTFOLIO_SIZE = 2
MAX_PORTFOLIO_SIZE = 20
_RATING_SCORE = {
    "Buy": 5.0,
    "Overweight": 4.0,
    "Hold": 2.5,
    "Underweight": 1.0,
    "Sell": 0.0,
}


def _rating_for_state(state: dict[str, Any]) -> PortfolioRating:
    rating = parse_rating(str(state.get("final_trade_decision", "")))
    return PortfolioRating(rating)


def _allocation_prompt(
    tickers: list[str],
    analyses: dict[str, dict[str, Any]],
    trade_date: str,
    max_position_percent: float,
) -> str:
    evidence = []
    for ticker in tickers:
        state = analyses[ticker]
        decision = str(state.get("final_trade_decision", "")).strip()
        evidence.append(
            f"## {ticker}\n"
            f"Individual final rating: {_rating_for_state(state).value}\n"
            f"Individual Portfolio Manager decision:\n{decision or 'No decision text available.'}"
        )

    joined_evidence = "\n\n".join(evidence)
    candidate_count = len(tickers)
    return f"""You are the cross-sectional Portfolio Allocation Manager. Convert {candidate_count} completed
single-stock research decisions into one long-only target portfolio for {trade_date}.

Candidate tickers, in required output order: {", ".join(tickers)}

Construction rules:
- Return exactly one position for each of the {candidate_count} candidates, in the supplied order.
- Copy each stock's individual final rating exactly; do not invent a new rating.
- Assign a target weight from 0% to {max_position_percent:.2f}% to every stock.
- Include an explicit cash weight from 0% to 100%.
- The {candidate_count} stock weights plus cash must total 100%.
- A Sell or Underweight candidate may receive 0%; do not force capital into weak ideas.
- Compare conviction, downside risk, and concentration across the candidates. Avoid
  false diversification when several names share the same underlying risk factor.
- This is a target allocation, not a list of Buy/Hold/Sell actions.

Completed single-stock evidence:

{joined_evidence}

{NO_EXTERNAL_TOOLS}{get_language_instruction()}"""


def _extract_json(text: str) -> PortfolioAllocation:
    """Parse a JSON object from a plain-model fallback response."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("portfolio allocator returned no JSON object")
    return PortfolioAllocation.model_validate(json.loads(cleaned[start : end + 1]))


def _capped_normalize(
    weights: list[float],
    caps: list[float],
    total: float = 100.0,
) -> list[float]:
    """Cap weights and normalize conservatively, treating the last item as cash.

    Cash expresses an intentional decision not to invest. When an invalid model
    proposal exceeds 100%, preserve that cash target and reduce stock weights
    proportionally instead of redeploying cash into the candidates.
    """
    if len(weights) != len(caps):
        raise ValueError("weights and caps must have the same length")
    values = [
        max(0.0, min(float(weight), float(cap))) for weight, cap in zip(weights, caps, strict=True)
    ]
    if sum(caps) + 1e-9 < total:
        raise ValueError("component caps cannot accommodate the requested total")

    current_total = sum(values)
    reduced_overweight = current_total > total
    if current_total > total:
        cash_weight = values[-1]
        stock_total = sum(values[:-1])
        target_stock_total = max(0.0, total - cash_weight)
        if stock_total > target_stock_total and stock_total > 0:
            scale = target_stock_total / stock_total
            values[:-1] = [value * scale for value in values[:-1]]
        elif cash_weight > total:
            values[-1] = total
    elif current_total < total:
        # The last component is cash. Sending an arithmetic shortfall to cash is
        # safer and preserves the model's intended stock weights.
        remaining = total - current_total
        for index in [len(values) - 1, *range(len(values) - 1)]:
            addition = min(remaining, caps[index] - values[index])
            values[index] += addition
            remaining -= addition
            if remaining <= 1e-9:
                break
        if remaining > 1e-9:
            raise ValueError("component caps cannot accommodate the requested total")

    rounded = [round(value, 2) for value in values]
    residual = round(total - sum(rounded), 2)
    if residual:
        # Prefer cash for rounding residuals. If cash is at a boundary, use the
        # first stock with room in the required input order.
        candidates = (
            [*range(len(rounded) - 1), len(rounded) - 1]
            if reduced_overweight
            else [len(rounded) - 1, *range(len(rounded) - 1)]
        )
        for index in candidates:
            adjusted = rounded[index] + residual
            if -1e-9 <= adjusted <= caps[index] + 1e-9:
                rounded[index] = round(adjusted, 2)
                break
    return rounded


def _normalize_allocation(
    allocation: PortfolioAllocation,
    tickers: list[str],
    analyses: dict[str, dict[str, Any]],
    max_position_percent: float,
) -> PortfolioAllocation:
    """Validate membership/order and normalize weights to exactly 100%."""
    expected = {ticker.casefold(): ticker for ticker in tickers}
    supplied: dict[str, PortfolioPosition] = {}
    for position in allocation.positions:
        key = position.ticker.strip().casefold()
        if key not in expected:
            raise ValueError(f"allocator returned unknown ticker: {position.ticker}")
        if key in supplied:
            raise ValueError(f"allocator returned duplicate ticker: {position.ticker}")
        supplied[key] = position
    if set(supplied) != set(expected):
        missing = [ticker for ticker in tickers if ticker.casefold() not in supplied]
        raise ValueError(f"allocator omitted ticker(s): {', '.join(missing)}")

    ordered = [supplied[ticker.casefold()] for ticker in tickers]
    normalized = _capped_normalize(
        [position.weight_percent for position in ordered] + [allocation.cash_weight_percent],
        [max_position_percent] * len(tickers) + [100.0],
    )
    positions = []
    for index, ticker in enumerate(tickers):
        positions.append(
            PortfolioPosition(
                ticker=ticker,
                weight_percent=normalized[index],
                rating=_rating_for_state(analyses[ticker]),
                rationale=ordered[index].rationale,
            )
        )
    return PortfolioAllocation(
        positions=positions,
        cash_weight_percent=normalized[-1],
        portfolio_summary=allocation.portfolio_summary,
        risk_note=allocation.risk_note,
    )


def _rating_fallback(
    tickers: list[str],
    analyses: dict[str, dict[str, Any]],
    max_position_percent: float,
) -> PortfolioAllocation:
    """Produce a safe deterministic allocation when the LLM cannot return JSON."""
    ratings = [_rating_for_state(analyses[ticker]) for ticker in tickers]
    scores = [_RATING_SCORE[rating.value] for rating in ratings]
    invested_percent = (sum(scores) / (len(tickers) * _RATING_SCORE["Buy"])) * 100.0
    if sum(scores) > 0:
        stock_weights = [invested_percent * score / sum(scores) for score in scores]
    else:
        stock_weights = [0.0] * len(tickers)
    weights = _capped_normalize(
        stock_weights + [100.0 - invested_percent],
        [max_position_percent] * len(tickers) + [100.0],
    )
    positions = [
        PortfolioPosition(
            ticker=ticker,
            weight_percent=weights[index],
            rating=ratings[index],
            rationale=(
                f"Fallback sizing based on the completed individual {ratings[index].value} "
                "rating because the cross-sectional model did not return valid structured data."
            ),
        )
        for index, ticker in enumerate(tickers)
    ]
    return PortfolioAllocation(
        positions=positions,
        cash_weight_percent=weights[-1],
        portfolio_summary=(
            "Deterministic fallback allocation derived from the completed ratings. "
            "Higher-conviction ratings receive more capital and weak ratings increase cash."
        ),
        risk_note=(
            "The cross-sectional LLM result was unavailable or invalid; review correlations "
            "and company-specific risks before using these fallback weights."
        ),
    )


class PortfolioAllocator:
    """Compare completed stock analyses and return normalized target weights."""

    def __init__(self, llm: Any, max_position_percent: float = 25.0):
        if not 0 < float(max_position_percent) <= 100.0:
            raise ValueError("max_position_percent must be greater than 0 and at most 100")
        self.llm = llm
        self.max_position_percent = float(max_position_percent)
        self.structured_llm = bind_structured(
            llm, PortfolioAllocation, "Portfolio Allocation Manager"
        )

    def allocate(
        self,
        tickers: list[str],
        analyses: dict[str, dict[str, Any]],
        trade_date: str,
    ) -> PortfolioAllocation:
        if not MIN_PORTFOLIO_SIZE <= len(tickers) <= MAX_PORTFOLIO_SIZE:
            raise ValueError(
                f"portfolio mode requires {MIN_PORTFOLIO_SIZE} to "
                f"{MAX_PORTFOLIO_SIZE} tickers"
            )
        prompt = _allocation_prompt(tickers, analyses, trade_date, self.max_position_percent)

        if self.structured_llm is not None:
            try:
                result = self.structured_llm.invoke(prompt)
                if result is None:
                    raise ValueError("structured output returned no parsed result")
                parsed = (
                    result
                    if isinstance(result, PortfolioAllocation)
                    else PortfolioAllocation.model_validate(result)
                )
                return _normalize_allocation(parsed, tickers, analyses, self.max_position_percent)
            except Exception as exc:
                logger.warning(
                    "Portfolio Allocation Manager structured output failed (%s); "
                    "retrying once as JSON",
                    exc,
                )

        try:
            json_shape = {
                "positions": [
                    {
                        "ticker": "AAPL",
                        "weight_percent": 12.5,
                        "rating": "Buy",
                        "rationale": "Relative allocation rationale",
                    }
                ],
                "cash_weight_percent": 0.0,
                "portfolio_summary": "Portfolio construction summary",
                "risk_note": "Portfolio-level risk",
            }
            response = normalize_content(
                self.llm.invoke(
                    prompt
                    + f"\nReturn JSON only, using exactly this object shape (with {len(tickers)} "
                    + f"position objects): {json.dumps(json_shape)}"
                )
            )
            parsed = _extract_json(response.content)
            return _normalize_allocation(parsed, tickers, analyses, self.max_position_percent)
        except Exception as exc:
            logger.warning(
                "Portfolio Allocation Manager JSON fallback failed (%s); using "
                "deterministic rating-based weights",
                exc,
            )
            return _rating_fallback(tickers, analyses, self.max_position_percent)
