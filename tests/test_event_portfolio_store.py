from datetime import datetime, timezone

import pytest

from tests.test_event_portfolio_schemas import valid_portfolio_request_payload
from tradingagents.api.portfolio_schemas import (
    AgentPortfolioTarget,
    EventPortfolioPlanRequest,
    EventPortfolioPlanResponse,
)
from tradingagents.api.portfolio_store import PortfolioPlanStore
from tradingagents.api.schemas import PlanProvenance
from tradingagents.api.store import EventPlanStore


def response_for(request: EventPortfolioPlanRequest, request_hash: str):
    return EventPortfolioPlanResponse(
        schema_version="1.0",
        request_id=request.request_id,
        request_sha256=request_hash,
        decision_id="d" * 64,
        decided_at=datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc),
        reason_code="approved",
        positions=[
            AgentPortfolioTarget(
                stock_code="000001",
                target_weight_percent="30.00",
                rating="Hold",
                rationale="Keep a smaller holding.",
            ),
            AgentPortfolioTarget(
                stock_code="600000",
                target_weight_percent="60.00",
                rating="Buy",
                rationale="Allocate to the stronger candidate.",
            ),
        ],
        cash_weight_percent="10.00",
        portfolio_summary="Diversified target.",
        risk_note="Partial event coverage.",
        provenance=PlanProvenance(
            llm_provider="fake",
            deep_model="fake-deep",
            quick_model="fake-quick",
        ),
    )


def test_portfolio_store_is_idempotent_and_separate(tmp_path) -> None:
    path = tmp_path / "requests.sqlite3"
    legacy_store = EventPlanStore(path)
    store = PortfolioPlanStore(path)
    request = EventPortfolioPlanRequest.model_validate(valid_portfolio_request_payload())
    request_hash = "a" * 64

    assert store.claim(request.request_id, request_hash).status == "started"
    store.complete(response_for(request, request_hash))
    cached = store.claim(request.request_id, request_hash)
    assert cached.status == "completed"
    assert cached.response is not None
    assert cached.response.positions[1].stock_code == "600000"
    assert store.claim(request.request_id, "b" * 64).status == "conflict"
    assert legacy_store.claim("legacy-request", "c" * 64).status == "started"


def test_portfolio_store_fails_interrupted_request_on_reopen(tmp_path) -> None:
    path = tmp_path / "requests.sqlite3"
    request = EventPortfolioPlanRequest.model_validate(valid_portfolio_request_payload())
    request_hash = "e" * 64
    first = PortfolioPlanStore(path)
    assert first.claim(request.request_id, request_hash).status == "started"

    reopened = PortfolioPlanStore(path)

    claim = reopened.claim(request.request_id, request_hash)
    assert claim.status == "failed"
    assert claim.error_code == "process_interrupted"


def test_portfolio_store_rejects_unbounded_failure_text(tmp_path) -> None:
    path = tmp_path / "requests.sqlite3"
    request = EventPortfolioPlanRequest.model_validate(valid_portfolio_request_payload())
    request_hash = "f" * 64
    store = PortfolioPlanStore(path)
    assert store.claim(request.request_id, request_hash).status == "started"

    with pytest.raises(ValueError, match="not allowlisted"):
        store.fail(request.request_id, request_hash, "secret provider response")

    assert store.claim(request.request_id, request_hash).status == "running"
    store.fail(request.request_id, request_hash, "planner_failed_instrument_graph")
    claim = store.claim(request.request_id, request_hash)
    assert claim.status == "failed"
    assert claim.error_code == "planner_failed_instrument_graph"


@pytest.mark.parametrize(
    "error_code",
    [
        "planner_failed_allocator_invoke",
        "planner_failed_allocator_schema",
        "planner_failed_allocator_binding",
        "planner_failed_allocator_total",
    ],
)
def test_portfolio_store_accepts_closed_allocator_substage_codes(
    tmp_path,
    error_code: str,
) -> None:
    path = tmp_path / "requests.sqlite3"
    request = EventPortfolioPlanRequest.model_validate(valid_portfolio_request_payload())
    request_hash = "1" * 64
    store = PortfolioPlanStore(path)
    assert store.claim(request.request_id, request_hash).status == "started"

    store.fail(request.request_id, request_hash, error_code)

    claim = store.claim(request.request_id, request_hash)
    assert claim.status == "failed"
    assert claim.error_code == error_code
