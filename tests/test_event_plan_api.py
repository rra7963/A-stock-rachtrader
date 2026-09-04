from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.test_event_plan_schemas import valid_request_payload
from tradingagents.api.app import ApiSettings, create_app
from tradingagents.api.planner import PlannerFailed
from tradingagents.api.schemas import EventTradePlanResponse, PlanProvenance
from tradingagents.api.store import EventPlanStore

TOKEN = "t" * 32


def make_response(request, request_hash) -> EventTradePlanResponse:
    return EventTradePlanResponse(
        schema_version="1.0",
        request_id=request.request_id,
        request_sha256=request_hash,
        decision_id=(request_hash[:63] + "d"),
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


class FakePlanner:
    def __init__(self, *, error: Exception | None = None, delay: float = 0):
        self.error = error
        self.delay = delay
        self.calls = 0

    def plan(self, request, request_hash):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return make_response(request, request_hash)


class BlockingPlanner:
    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self._counter_lock = threading.Lock()

    def plan(self, request, request_hash):
        with self._counter_lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.started.set()
            assert self.release.wait(timeout=5)
            if self.error:
                raise self.error
            return make_response(request, request_hash)
        finally:
            with self._counter_lock:
                self.active -= 1
            self.finished.set()


def make_client(tmp_path: Path, planner, *, token: str = TOKEN, timeout: int = 5):
    settings = ApiSettings(
        bearer_token=token,
        state_path=tmp_path / "requests.sqlite3",
        timeout_seconds=timeout,
        host="127.0.0.1",
        port=8787,
    )
    store = EventPlanStore(settings.state_path)
    app = create_app(settings=settings, store=store, planner_factory=lambda: planner)
    return TestClient(app)


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_health_and_authentication_are_fail_closed(tmp_path: Path):
    planner = FakePlanner()
    client = make_client(tmp_path, planner)

    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health/ready").json() == {"status": "ready"}

    missing = client.post("/v1/event-trade-plans", json=valid_request_payload())
    malformed_unauthenticated = client.post(
        "/v1/event-trade-plans",
        json={"event": {"content": "must not be parsed before auth"}},
    )
    wrong = client.post(
        "/v1/event-trade-plans",
        json=valid_request_payload(),
        headers={"Authorization": "Bearer " + "x" * 32},
    )
    assert missing.status_code == 401
    assert malformed_unauthenticated.status_code == 401
    assert "must not be parsed" not in malformed_unauthenticated.text
    assert wrong.status_code == 401
    assert planner.calls == 0

    malformed_authenticated = client.post(
        "/v1/event-trade-plans",
        json={"event": {"content": "sensitive invalid input"}},
        headers=auth_headers(),
    )
    assert malformed_authenticated.status_code == 422
    assert malformed_authenticated.json()["error_code"] == "invalid_request"
    assert "sensitive invalid input" not in malformed_authenticated.text


def test_json_number_for_decimal_wire_field_is_rejected_before_planning(tmp_path: Path):
    planner = FakePlanner()
    client = make_client(tmp_path, planner)
    payload = valid_request_payload()
    payload["portfolio"]["available_cash_cny"] = 500000.0

    response = client.post(
        "/v1/event-trade-plans",
        json=payload,
        headers=auth_headers(),
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "invalid_request"
    assert planner.calls == 0


def test_missing_server_token_keeps_readiness_and_plans_closed(tmp_path: Path):
    client = make_client(tmp_path, FakePlanner(), token="")

    assert client.get("/health/ready").status_code == 503
    response = client.post(
        "/v1/event-trade-plans",
        json=valid_request_payload(),
        headers={"Authorization": "Bearer anything"},
    )
    assert response.status_code == 503
    assert response.json()["error_code"] == "planner_unavailable"


def test_completed_request_is_cached_and_conflicting_payload_is_rejected(tmp_path: Path):
    planner = FakePlanner()
    client = make_client(tmp_path, planner)
    payload = valid_request_payload()

    first = client.post("/v1/event-trade-plans", json=payload, headers=auth_headers())
    second = client.post("/v1/event-trade-plans", json=payload, headers=auth_headers())
    conflict_payload = deepcopy(payload)
    conflict_payload["event"]["summary"] = "Different normalized input."
    conflict = client.post(
        "/v1/event-trade-plans",
        json=conflict_payload,
        headers=auth_headers(),
    )

    assert first.status_code == 200
    assert second.json() == first.json()
    assert planner.calls == 1
    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "request_id_conflict"


def test_planner_failure_is_sanitized_and_same_id_cannot_restart(tmp_path: Path):
    planner = FakePlanner(error=PlannerFailed("secret provider detail"))
    client = make_client(tmp_path, planner)
    payload = valid_request_payload()

    failed = client.post("/v1/event-trade-plans", json=payload, headers=auth_headers())
    repeated = client.post("/v1/event-trade-plans", json=payload, headers=auth_headers())

    assert failed.status_code == 503
    assert failed.json()["error_code"] == "planner_failed"
    assert "secret provider detail" not in failed.text
    assert repeated.status_code == 409
    assert repeated.json()["error_code"] == "request_previously_failed"
    assert planner.calls == 1


def test_server_timeout_is_permanent_for_the_request_id(tmp_path: Path):
    async def scenario():
        planner = FakePlanner(delay=1.5)
        settings = ApiSettings(
            bearer_token=TOKEN,
            state_path=tmp_path / "requests.sqlite3",
            timeout_seconds=1,
            host="127.0.0.1",
            port=8787,
        )
        app = create_app(
            settings=settings,
            store=EventPlanStore(settings.state_path),
            planner_factory=lambda: planner,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            payload = valid_request_payload()
            timed_out = await client.post(
                "/v1/event-trade-plans",
                json=payload,
                headers=auth_headers(),
            )
            repeated = await client.post(
                "/v1/event-trade-plans",
                json=payload,
                headers=auth_headers(),
            )

            assert timed_out.status_code == 504
            assert timed_out.json()["error_code"] == "analysis_timeout"
            assert repeated.status_code == 409
            assert repeated.json()["error_code"] == "request_previously_failed"

            while_worker_is_late = deepcopy(payload)
            while_worker_is_late["request_id"] = "rachel_executor:event-late:2026-08-13"
            while_worker_is_late["event"]["event_id"] = "event-late"
            busy = await client.post(
                "/v1/event-trade-plans",
                json=while_worker_is_late,
                headers=auth_headers(),
            )
            assert busy.status_code == 429

            await asyncio.sleep(0.6)
            planner.delay = 0
            after_worker_exit = deepcopy(payload)
            after_worker_exit["request_id"] = "rachel_executor:event-after:2026-08-13"
            after_worker_exit["event"]["event_id"] = "event-after"
            accepted = await client.post(
                "/v1/event-trade-plans",
                json=after_worker_exit,
                headers=auth_headers(),
            )
            assert accepted.status_code == 200

    asyncio.run(scenario())


def test_only_one_analysis_runs_at_a_time(tmp_path: Path):
    planner = BlockingPlanner()
    client = make_client(tmp_path, planner)
    first_payload = valid_request_payload()
    second_payload = deepcopy(first_payload)
    second_payload["request_id"] = "rachel_executor:event-456:2026-08-13"
    second_payload["event"]["event_id"] = "event-456"

    with ThreadPoolExecutor(max_workers=1) as pool:
        first_future = pool.submit(
            client.post,
            "/v1/event-trade-plans",
            json=first_payload,
            headers=auth_headers(),
        )
        assert planner.started.wait(timeout=2)
        busy = client.post(
            "/v1/event-trade-plans",
            json=second_payload,
            headers=auth_headers(),
        )
        planner.release.set()
        first = first_future.result(timeout=5)

    assert first.status_code == 200
    assert busy.status_code == 429
    assert busy.json()["error_code"] == "analysis_busy"
    repeated = client.post(
        "/v1/event-trade-plans",
        json=second_payload,
        headers=auth_headers(),
    )
    assert repeated.status_code == 409
    assert repeated.json()["error_code"] == "request_previously_failed"


@pytest.mark.parametrize("late_error", [None, RuntimeError("late provider failure")])
def test_client_cancellation_keeps_lock_until_worker_finishes(tmp_path: Path, late_error):
    async def scenario():
        planner = BlockingPlanner(error=late_error)
        settings = ApiSettings(
            bearer_token=TOKEN,
            state_path=tmp_path / "requests.sqlite3",
            timeout_seconds=5,
            host="127.0.0.1",
            port=8787,
        )
        app = create_app(
            settings=settings,
            store=EventPlanStore(settings.state_path),
            planner_factory=lambda: planner,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            first_payload = valid_request_payload()
            first = asyncio.create_task(
                client.post(
                    "/v1/event-trade-plans",
                    json=first_payload,
                    headers=auth_headers(),
                )
            )
            assert await asyncio.to_thread(planner.started.wait, 2)

            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first

            repeated = await client.post(
                "/v1/event-trade-plans",
                json=first_payload,
                headers=auth_headers(),
            )
            assert repeated.status_code == 409
            assert repeated.json()["error_code"] == "request_previously_failed"

            while_worker_runs = deepcopy(first_payload)
            while_worker_runs["request_id"] = "rachel_executor:event-cancelled-late:2026-08-13"
            while_worker_runs["event"]["event_id"] = "event-cancelled-late"
            busy = await client.post(
                "/v1/event-trade-plans",
                json=while_worker_runs,
                headers=auth_headers(),
            )
            assert busy.status_code == 429
            assert busy.json()["error_code"] == "analysis_busy"
            assert planner.calls == 1
            assert planner.max_active == 1

            planner.release.set()
            assert await asyncio.to_thread(planner.finished.wait, 2)
            planner.error = None
            await asyncio.sleep(0)

            after_worker_exit = deepcopy(first_payload)
            after_worker_exit["request_id"] = "rachel_executor:event-after-cancel:2026-08-13"
            after_worker_exit["event"]["event_id"] = "event-after-cancel"
            accepted = await client.post(
                "/v1/event-trade-plans",
                json=after_worker_exit,
                headers=auth_headers(),
            )
            assert accepted.status_code == 200
            assert planner.calls == 2
            assert planner.max_active == 1

    asyncio.run(scenario())
