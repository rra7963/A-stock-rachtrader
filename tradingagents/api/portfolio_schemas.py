"""Closed wire schemas for the batch event-portfolio plan API."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, model_validator
from typing_extensions import Self

from tradingagents.agents.schemas import PortfolioRating

from .schemas import (
    ClosedModel,
    EventId,
    PlanProvenance,
    RequestId,
    WireNonNegativeMoney,
    WirePositiveMoney,
    WirePositivePrice,
    _require_aware,
    _require_wire_decimal_string,
)

PORTFOLIO_SCHEMA_VERSION = "1.1"
PortfolioSchemaVersion = Literal["1.0", "1.1"]
PortfolioStockCode = Annotated[
    str,
    Field(pattern=(r"^(?:(?:000|001|002|003|300|301|600|601|603|605|688|689)[0-9]{3})$")),
]
MAX_BATCH_EVENTS = 100
MAX_PORTFOLIO_UNIVERSE = 20
MAX_EVENT_LINKS_PER_INSTRUMENT = 20
MAX_DECISION_HORIZON = timedelta(hours=8)
MAX_CLOCK_SKEW = timedelta(minutes=2)
PORTFOLIO_TOTAL_BASIS_POINTS = 10_000

NonNegativeWeight = Annotated[
    Decimal,
    Field(ge=Decimal("0"), le=Decimal("100"), max_digits=5, decimal_places=2),
]
WireNonNegativeWeight = Annotated[
    NonNegativeWeight,
    BeforeValidator(_require_wire_decimal_string),
]


class PortfolioEventWindow(ClosedModel):
    starts_at: datetime
    ends_at: datetime
    coverage_status: Literal["complete", "partial"]
    coverage_gap_count: Annotated[int, Field(ge=0, le=10_000)]

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        _require_aware(self.starts_at, "event_window.starts_at")
        _require_aware(self.ends_at, "event_window.ends_at")
        if self.starts_at >= self.ends_at:
            raise ValueError("event_window must be non-empty")
        if self.coverage_status == "complete" and self.coverage_gap_count != 0:
            raise ValueError("complete coverage cannot report gaps")
        if self.coverage_status == "partial" and self.coverage_gap_count < 1:
            raise ValueError("partial coverage must report at least one gap")
        return self


class PortfolioEventEnvelope(ClosedModel):
    event_id: EventId
    signal_level: Literal["S", "A"]
    title: Annotated[str, Field(min_length=1, max_length=300)]
    summary: Annotated[str, Field(min_length=1, max_length=2000)]
    source: Annotated[str, Field(min_length=1, max_length=128)]
    published_at: datetime
    received_at: datetime

    @model_validator(mode="after")
    def validate_times(self) -> Self:
        _require_aware(self.published_at, "event.published_at")
        _require_aware(self.received_at, "event.received_at")
        if self.received_at < self.published_at - MAX_CLOCK_SKEW:
            raise ValueError("event.received_at materially precedes published_at")
        return self


class PortfolioEventLink(ClosedModel):
    event_id: EventId
    relevance: Literal["high"]
    relevance_reason: Annotated[str, Field(min_length=1, max_length=1000)]


class PortfolioInstrumentSnapshot(ClosedModel):
    stock_code: PortfolioStockCode
    stock_name: Annotated[str, Field(min_length=1, max_length=80)]
    currently_held: bool
    current_quantity: Annotated[int, Field(ge=0, le=100_000_000)]
    sellable_quantity: Annotated[int, Field(ge=0, le=100_000_000)] | None
    confirmed_cost: WirePositivePrice | None
    reference_price: WirePositivePrice
    upper_limit_price: WirePositivePrice
    lower_limit_price: WirePositivePrice
    quote_at: datetime
    current_weight_percent: WireNonNegativeWeight
    event_links: Annotated[
        list[PortfolioEventLink],
        Field(max_length=MAX_EVENT_LINKS_PER_INSTRUMENT),
    ]

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        _require_aware(self.quote_at, "instrument.quote_at")
        if not self.lower_limit_price <= self.reference_price <= self.upper_limit_price:
            raise ValueError("instrument reference price must be within price limits")
        if self.currently_held:
            if self.current_quantity <= 0:
                raise ValueError("held instrument requires positive current_quantity")
            if (
                self.sellable_quantity is not None
                and self.sellable_quantity > self.current_quantity
            ):
                raise ValueError("sellable_quantity cannot exceed current_quantity")
        elif (
            self.current_quantity != 0
            or self.sellable_quantity not in {None, 0}
            or self.confirmed_cost is not None
            or self.current_weight_percent != 0
        ):
            raise ValueError("non-held instrument cannot carry holding facts")
        if not self.currently_held and not self.event_links:
            raise ValueError("non-held instrument requires event provenance")
        event_ids = [link.event_id for link in self.event_links]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("instrument event links must be unique")
        return self


class PortfolioAccountSnapshot(ClosedModel):
    available_cash_cny: WireNonNegativeMoney
    total_equity_cny: WirePositiveMoney

    @model_validator(mode="after")
    def validate_cash(self) -> Self:
        if self.available_cash_cny > self.total_equity_cny:
            raise ValueError("available cash cannot exceed total equity")
        return self


class EventPortfolioPlanRequest(ClosedModel):
    schema_version: PortfolioSchemaVersion
    request_id: RequestId
    requested_at: datetime
    decision_deadline: datetime
    event_window: PortfolioEventWindow
    events: Annotated[list[PortfolioEventEnvelope], Field(max_length=MAX_BATCH_EVENTS)]
    instruments: Annotated[
        list[PortfolioInstrumentSnapshot],
        Field(min_length=1, max_length=MAX_PORTFOLIO_UNIVERSE),
    ]
    portfolio: PortfolioAccountSnapshot

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        _require_aware(self.requested_at, "requested_at")
        _require_aware(self.decision_deadline, "decision_deadline")
        if not self.requested_at < self.decision_deadline:
            raise ValueError("decision_deadline must follow requested_at")
        if self.decision_deadline - self.requested_at > MAX_DECISION_HORIZON:
            raise ValueError("decision_deadline exceeds the maximum horizon")
        if self.event_window.ends_at > self.requested_at + MAX_CLOCK_SKEW:
            raise ValueError("event window ends after the request snapshot")

        event_ids = [event.event_id for event in self.events]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("events must be unique")
        event_id_set = set(event_ids)
        for event in self.events:
            if not self.event_window.starts_at <= event.received_at < self.event_window.ends_at:
                raise ValueError("event receipt is outside the half-open event window")

        codes = [instrument.stock_code for instrument in self.instruments]
        if len(codes) != len(set(codes)):
            raise ValueError("instrument stock codes must be unique")
        seen_non_held = False
        current_weight = Decimal("0")
        for instrument in self.instruments:
            if not instrument.currently_held:
                seen_non_held = True
            elif seen_non_held:
                raise ValueError("all held instruments must precede new candidates")
            current_weight += instrument.current_weight_percent
            unknown_links = {
                link.event_id
                for link in instrument.event_links
                if link.event_id not in event_id_set
            }
            if unknown_links:
                raise ValueError("instrument links reference unknown events")
        if current_weight > Decimal("100.00"):
            raise ValueError("current instrument weights cannot exceed 100 percent")
        if self.schema_version == "1.0":
            if any(event.signal_level != "S" for event in self.events):
                raise ValueError("portfolio schema 1.0 accepts only S events")
            if any(_is_star_market_code(code) for code in codes):
                raise ValueError("portfolio schema 1.0 does not accept STAR Market instruments")
        return self


class AgentPortfolioTarget(ClosedModel):
    stock_code: PortfolioStockCode
    target_weight_percent: NonNegativeWeight
    rating: PortfolioRating
    rationale: Annotated[str, Field(min_length=1, max_length=1000)]


class AgentPortfolioWeightProposal(ClosedModel):
    """Allocator-owned fields for one request-bound portfolio member."""

    stock_code: PortfolioStockCode
    target_weight_basis_points: Annotated[
        int,
        Field(strict=True, ge=0, le=PORTFOLIO_TOTAL_BASIS_POINTS),
    ]
    rationale: Annotated[str, Field(min_length=1, max_length=1000)]


class AgentEventPortfolioAllocationProposal(ClosedModel):
    """Strict internal allocator result before graph ratings are rebound."""

    positions: Annotated[
        list[AgentPortfolioWeightProposal],
        Field(min_length=1, max_length=MAX_PORTFOLIO_UNIVERSE),
    ]
    cash_weight_basis_points: Annotated[
        int,
        Field(strict=True, ge=0, le=PORTFOLIO_TOTAL_BASIS_POINTS),
    ]
    portfolio_summary: Annotated[str, Field(min_length=1, max_length=2000)]
    risk_note: Annotated[str, Field(min_length=1, max_length=1000)]


class AgentEventPortfolioAllocation(ClosedModel):
    positions: Annotated[
        list[AgentPortfolioTarget],
        Field(min_length=1, max_length=MAX_PORTFOLIO_UNIVERSE),
    ]
    cash_weight_percent: NonNegativeWeight
    portfolio_summary: Annotated[str, Field(min_length=1, max_length=2000)]
    risk_note: Annotated[str, Field(min_length=1, max_length=1000)]

    @model_validator(mode="after")
    def validate_total(self) -> Self:
        total = self.cash_weight_percent + sum(
            (position.target_weight_percent for position in self.positions),
            start=Decimal("0"),
        )
        if total != Decimal("100.00"):
            raise ValueError("target stock and cash weights must total exactly 100.00")
        return self


class EventPortfolioPlanResponse(ClosedModel):
    schema_version: PortfolioSchemaVersion
    request_id: RequestId
    request_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    decision_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    decided_at: datetime
    reason_code: Literal["approved"]
    positions: Annotated[
        list[AgentPortfolioTarget],
        Field(min_length=1, max_length=MAX_PORTFOLIO_UNIVERSE),
    ]
    cash_weight_percent: NonNegativeWeight
    portfolio_summary: Annotated[str, Field(min_length=1, max_length=2000)]
    risk_note: Annotated[str, Field(min_length=1, max_length=1000)]
    provenance: PlanProvenance

    @model_validator(mode="after")
    def validate_response(self) -> Self:
        _require_aware(self.decided_at, "decided_at")
        total = self.cash_weight_percent + sum(
            (position.target_weight_percent for position in self.positions),
            start=Decimal("0"),
        )
        if total != Decimal("100.00"):
            raise ValueError("response stock and cash weights must total exactly 100.00")
        codes = [position.stock_code for position in self.positions]
        if len(codes) != len(set(codes)):
            raise ValueError("response positions must be unique")
        if self.schema_version == "1.0" and any(_is_star_market_code(code) for code in codes):
            raise ValueError("portfolio schema 1.0 does not accept STAR Market positions")
        return self


def _is_star_market_code(stock_code: str) -> bool:
    return stock_code.startswith(("688", "689"))
