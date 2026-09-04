from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from tradingagents.api.schemas import EventTradePlanResponse, PlanProvenance
from tradingagents.api.store import EventPlanStore


def decline_response(request_id: str, request_hash: str) -> EventTradePlanResponse:
    return EventTradePlanResponse(
        schema_version="1.0",
        request_id=request_id,
        request_sha256=request_hash,
        decision_id="d" * 64,
        decided_at=datetime(2026, 8, 13, 2, 2, tzinfo=timezone.utc),
        outcome="decline",
        selected_stock_code=None,
        graph_rating=None,
        reason_code="candidate_declined",
        rationale="No purchase is justified.",
        provenance=PlanProvenance(
            llm_provider="fake",
            deep_model="fake-deep",
            quick_model="fake-quick",
        ),
        plan=None,
    )


def test_store_claim_complete_and_cached_response(tmp_path: Path):
    store = EventPlanStore(tmp_path / "requests.sqlite3")
    request_hash = "a" * 64

    assert store.claim("request-1", request_hash).status == "started"
    assert store.claim("request-1", request_hash).status == "running"

    expected = decline_response("request-1", request_hash)
    store.complete(expected)
    cached = store.claim("request-1", request_hash)

    assert cached.status == "completed"
    assert cached.response == expected
    assert store.claim("request-1", "b" * 64).status == "conflict"


def test_failed_request_id_never_restarts_automatically(tmp_path: Path):
    store = EventPlanStore(tmp_path / "requests.sqlite3")
    request_hash = "c" * 64
    assert store.claim("request-2", request_hash).status == "started"

    store.fail("request-2", request_hash, "planner_failed")
    failed = store.claim("request-2", request_hash)

    assert failed.status == "failed"
    assert failed.error_code == "planner_failed"


def test_process_interrupted_rows_fail_closed_on_store_reopen(tmp_path: Path):
    path = tmp_path / "requests.sqlite3"
    first = EventPlanStore(path)
    request_hash = "e" * 64
    assert first.claim("request-3", request_hash).status == "started"

    reopened = EventPlanStore(path)
    failed = reopened.claim("request-3", request_hash)

    assert failed.status == "failed"
    assert failed.error_code == "process_interrupted"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
