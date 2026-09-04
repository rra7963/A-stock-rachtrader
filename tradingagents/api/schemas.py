"""Closed version-1 wire and agent schemas for the event trade-plan API."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator
from typing_extensions import Self

SCHEMA_VERSION = "1.0"
MAX_EVENT_RECEIPT_DELAY = timedelta(minutes=15)
MAX_PORTFOLIO_POSITIONS = 10
MAX_PER_STOCK_CNY = Decimal("100000.00")

RequestId = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
]
EventId = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
]
StockCode = Annotated[
    str,
    Field(pattern=r"^(?:(?:000|001|002|003|300|301|600|601|603|605)[0-9]{3})$"),
]
PositiveMoney = Annotated[
    Decimal,
    Field(gt=Decimal("0"), max_digits=14, decimal_places=2),
]
NonNegativeMoney = Annotated[
    Decimal,
    Field(ge=Decimal("0"), max_digits=14, decimal_places=2),
]
PositivePrice = Annotated[
    Decimal,
    Field(gt=Decimal("0"), max_digits=12, decimal_places=4),
]


def _require_wire_decimal_string(value: object) -> object:
    if isinstance(value, Decimal):
        return value
    if not isinstance(value, str):
        raise ValueError("decimal wire values must be JSON strings")
    return value


WirePositiveMoney = Annotated[PositiveMoney, BeforeValidator(_require_wire_decimal_string)]
WireNonNegativeMoney = Annotated[
    NonNegativeMoney,
    BeforeValidator(_require_wire_decimal_string),
]
WirePositivePrice = Annotated[PositivePrice, BeforeValidator(_require_wire_decimal_string)]


class ClosedModel(BaseModel):
    """Base model for versioned API objects; unknown fields are never ignored."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_encoders={Decimal: lambda value: format(value, "f")},
    )


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone offset")


class EventEnvelope(ClosedModel):
    event_id: EventId
    signal_level: Literal["S"]
    title: Annotated[str, Field(min_length=1, max_length=300)]
    summary: Annotated[str, Field(min_length=1, max_length=4000)]
    content: Annotated[str, Field(max_length=12000)] = ""
    source: Annotated[str, Field(min_length=1, max_length=128)]
    published_at: datetime
    received_at: datetime

    @model_validator(mode="after")
    def validate_event_times(self) -> Self:
        _require_aware(self.published_at, "published_at")
        _require_aware(self.received_at, "received_at")
        if self.received_at < self.published_at:
            raise ValueError("received_at must not precede published_at")
        if self.received_at - self.published_at > MAX_EVENT_RECEIPT_DELAY:
            raise ValueError("event was not first received within 15 minutes of publication")
        return self


class CandidateSnapshot(ClosedModel):
    stock_code: StockCode
    stock_name: Annotated[str, Field(min_length=1, max_length=80)]
    relevance: Literal["high"]
    relevance_reason: Annotated[str, Field(min_length=1, max_length=1000)]
    reference_price: WirePositivePrice
    upper_limit_price: WirePositivePrice
    quote_at: datetime

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        _require_aware(self.quote_at, "quote_at")
        if self.upper_limit_price < self.reference_price:
            raise ValueError("upper_limit_price must not be below reference_price")
        return self


class PortfolioContext(ClosedModel):
    available_cash_cny: WireNonNegativeMoney
    current_position_count: Annotated[int, Field(ge=0, le=MAX_PORTFOLIO_POSITIONS)]
    max_position_count: Annotated[int, Field(ge=1, le=MAX_PORTFOLIO_POSITIONS)]
    per_stock_cap_cny: Annotated[
        WirePositiveMoney,
        Field(
            gt=Decimal("0"),
            le=MAX_PER_STOCK_CNY,
            max_digits=8,
            decimal_places=2,
        ),
    ]
    held_stock_codes: Annotated[list[StockCode], Field(max_length=MAX_PORTFOLIO_POSITIONS)]
    pending_buy_stock_codes: Annotated[
        list[StockCode],
        Field(max_length=MAX_PORTFOLIO_POSITIONS),
    ]

    @model_validator(mode="after")
    def validate_positions(self) -> Self:
        held = set(self.held_stock_codes)
        pending = set(self.pending_buy_stock_codes)
        if len(held) != len(self.held_stock_codes):
            raise ValueError("held_stock_codes must be unique")
        if len(pending) != len(self.pending_buy_stock_codes):
            raise ValueError("pending_buy_stock_codes must be unique")
        if held & pending:
            raise ValueError("held and pending stock codes must be disjoint")
        if self.current_position_count != len(held):
            raise ValueError("current_position_count must equal the complete held_stock_codes list")
        return self

    @property
    def reserved_position_count(self) -> int:
        return self.current_position_count + len(self.pending_buy_stock_codes)


class EventTradePlanRequest(ClosedModel):
    schema_version: Literal["1.0"]
    request_id: RequestId
    requested_at: datetime
    event: EventEnvelope
    candidates: Annotated[list[CandidateSnapshot], Field(min_length=1, max_length=20)]
    portfolio: PortfolioContext

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        _require_aware(self.requested_at, "requested_at")
        if self.requested_at < self.event.received_at - timedelta(minutes=2):
            raise ValueError("requested_at materially precedes received_at")

        codes = [candidate.stock_code for candidate in self.candidates]
        if len(set(codes)) != len(codes):
            raise ValueError("candidate stock codes must be unique")
        excluded = set(self.portfolio.held_stock_codes) | set(
            self.portfolio.pending_buy_stock_codes
        )
        overlap = sorted(set(codes) & excluded)
        if overlap:
            raise ValueError(
                "candidates must exclude held and pending stock codes: " + ",".join(overlap)
            )
        return self


class AgentCandidateChoice(ClosedModel):
    """Strict output of the allowlist-bound candidate selector."""

    action: Literal["analyze", "decline"]
    stock_code: StockCode | None = None
    rationale: Annotated[str, Field(min_length=1, max_length=1000)]

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        if self.action == "analyze" and self.stock_code is None:
            raise ValueError("analyze requires stock_code")
        if self.action == "decline" and self.stock_code is not None:
            raise ValueError("decline must not include stock_code")
        return self


class AgentPlanChoice(ClosedModel):
    """Strict output of the graph-to-machine-plan adapter."""

    action: Literal["buy", "decline"]
    stock_code: StockCode | None = None
    cash_amount_cny: PositiveMoney | None = None
    max_entry_price: PositivePrice | None = None
    rationale: Annotated[str, Field(min_length=1, max_length=1000)]

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        plan_fields = (self.stock_code, self.cash_amount_cny, self.max_entry_price)
        if self.action == "buy" and any(value is None for value in plan_fields):
            raise ValueError("buy requires stock_code, cash_amount_cny and max_entry_price")
        if self.action == "decline" and any(value is not None for value in plan_fields):
            raise ValueError("decline must not include plan fields")
        return self


class BuyPlan(ClosedModel):
    stock_code: StockCode
    cash_amount_cny: PositiveMoney
    max_entry_price: PositivePrice
    order_type: Literal["limit"] = "limit"
    time_in_force: Literal["DAY"] = "DAY"


class PlanProvenance(ClosedModel):
    llm_provider: Annotated[str, Field(min_length=1, max_length=64)]
    deep_model: Annotated[str, Field(min_length=1, max_length=128)]
    quick_model: Annotated[str, Field(min_length=1, max_length=128)]


class EventTradePlanResponse(ClosedModel):
    schema_version: Literal["1.0"]
    request_id: RequestId
    request_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    decision_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    decided_at: datetime
    outcome: Literal["buy", "decline"]
    selected_stock_code: StockCode | None = None
    graph_rating: Literal["Buy", "Overweight", "Hold", "Underweight", "Sell"] | None = None
    reason_code: Literal[
        "approved",
        "no_capacity",
        "insufficient_cash",
        "candidate_declined",
        "graph_not_buy",
        "plan_declined",
    ]
    rationale: Annotated[str, Field(min_length=1, max_length=1000)]
    provenance: PlanProvenance
    plan: BuyPlan | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        _require_aware(self.decided_at, "decided_at")
        if self.outcome == "buy":
            if self.plan is None or self.selected_stock_code is None:
                raise ValueError("buy response requires plan and selected_stock_code")
            if self.plan.stock_code != self.selected_stock_code:
                raise ValueError("plan stock_code must match selected_stock_code")
            if self.graph_rating != "Buy" or self.reason_code != "approved":
                raise ValueError("buy response requires Buy graph rating and approved reason")
        elif self.plan is not None:
            raise ValueError("decline response must not contain a plan")
        return self


class ApiError(ClosedModel):
    error_code: Literal[
        "unauthorized",
        "invalid_request",
        "request_id_conflict",
        "request_in_progress",
        "request_previously_failed",
        "analysis_busy",
        "analysis_timeout",
        "analysis_deadline_expired",
        "planner_unavailable",
        "planner_failed",
    ]
    message: Annotated[str, Field(min_length=1, max_length=240)]
    request_id: RequestId | None = None
