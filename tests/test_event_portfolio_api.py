from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

import tradingagents.api.app as app_module
from tests.test_event_plan_schemas import valid_request_payload
from tests.test_event_portfolio_schemas import valid_portfolio_request_payload
from tradingagents.api.app import ApiSettings, create_app
from tradingagents.api.planner import PlannerFailed
from tradingagents.api.portfolio_schemas import (
    AgentPortfolioTarget,
    EventPortfolioPlanRequest,
    EventPortfolioPlanResponse,
)
from tradingagents.api.portfolio_store import PortfolioPlanStore
from tradingagents.api.schemas import PlanProvenance
from tradingagents.api.store import EventPlanStore

TOKEN = "p" * 32


class FakePortfolioPlanner:
    def __init__(self, *, delay: float = 0):
        self.calls = 0
        self.delay = delay

    def plan_portfolio(self, request, request_hash):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return EventPortfolioPlanResponse(
            schema_version=request.schema_version,
            request_id=request.request_id,
            request_sha256=request_hash,
            decision_id="d" * 64,
            decided_at=datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc),
            reason_code="approved",
            positions=[
                AgentPortfolioTarget(
                    stock_code=request.instruments[0].stock_code,
                    target_weight_percent="30.00",
                    rating="Hold",
                    rationale="Keep a smaller holding.",
                ),
                AgentPortfolioTarget(
                    stock_code=request.instruments[1].stock_code,
                    target_weight_percent="60.00",
                    rating="Buy",
                    rationale="Allocate to the linked event candidate.",
                ),
            ],
            cash_weight_percent="10.00",
            portfolio_summary="Favor the event-linked candidate.",
            risk_note="Coverage was partial.",
            provenance=PlanProvenance(
                llm_provider="fake",
                deep_model="fake-deep",
                quick_model="fake-quick",
            ),
        )


class BlockingPortfolioPlanner(FakePortfolioPlanner):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def plan_portfolio(self, request, request_hash):
        self.started.set()
        assert self.release.wait(timeout=5)
        return super().plan_portfolio(request, request_hash)


def make_client(tmp_path, planner, *, portfolio_timeout_seconds: int = 19_800):
    settings = ApiSettings(
        bearer_token=TOKEN,
        state_path=tmp_path / "requests.sqlite3",
        timeout_seconds=5,
        host="127.0.0.1",
        port=8787,
        portfolio_timeout_seconds=portfolio_timeout_seconds,
    )
    app = create_app(
        settings=settings,
        store=EventPlanStore(settings.state_path),
        portfolio_store=PortfolioPlanStore(settings.state_path),
        planner_factory=lambda: planner,
    )
    return TestClient(app)


def headers():
    return {"Authorization": f"Bearer {TOKEN}"}


def future_payload():
    payload = valid_portfolio_request_payload()
    payload["requested_at"] = "2099-08-17T08:31:00+08:00"
    payload["decision_deadline"] = "2099-08-17T14:00:00+08:00"
    payload["event_window"]["starts_at"] = "2099-08-14T08:30:00+08:00"
    payload["event_window"]["ends_at"] = "2099-08-17T08:30:00+08:00"
    payload["events"][0]["published_at"] = "2099-08-16T10:00:00+08:00"
    payload["events"][0]["received_at"] = "2099-08-16T10:01:00+08:00"
    for instrument in payload["instruments"]:
        instrument["quote_at"] = "2099-08-17T08:30:30+08:00"
    return payload


def test_portfolio_endpoint_authenticates_before_parsing(tmp_path) -> None:
    planner = FakePortfolioPlanner()
    client = make_client(tmp_path, planner)

    unauthorized = client.post(
        "/v1/event-portfolio-plans",
        json={"events": [{"summary": "sensitive malformed input"}]},
    )
    assert unauthorized.status_code == 401
    assert "sensitive malformed input" not in unauthorized.text
    assert planner.calls == 0


def test_portfolio_endpoint_caches_and_conflicts_by_canonical_request(tmp_path) -> None:
    planner = FakePortfolioPlanner()
    client = make_client(tmp_path, planner)
    payload = future_payload()

    first = client.post("/v1/event-portfolio-plans", json=payload, headers=headers())
    second = client.post("/v1/event-portfolio-plans", json=payload, headers=headers())
    changed = deepcopy(payload)
    changed["events"][0]["summary"] = "Changed evidence."
    conflict = client.post("/v1/event-portfolio-plans", json=changed, headers=headers())

    assert first.status_code == 200
    assert second.json() == first.json()
    assert planner.calls == 1
    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "request_id_conflict"


def test_numeric_wire_value_is_rejected_without_planning(tmp_path) -> None:
    planner = FakePortfolioPlanner()
    client = make_client(tmp_path, planner)
    payload = future_payload()
    payload["portfolio"]["total_equity_cny"] = 50000.0

    result = client.post("/v1/event-portfolio-plans", json=payload, headers=headers())

    assert result.status_code == 422
    assert result.json()["error_code"] == "invalid_request"
    assert planner.calls == 0


def test_portfolio_endpoint_accepts_schema_1_1_a_event_and_star_market(tmp_path) -> None:
    planner = FakePortfolioPlanner()
    client = make_client(tmp_path, planner)
    payload = future_payload()
    payload["schema_version"] = "1.1"
    payload["events"][0]["signal_level"] = "A"
    payload["instruments"][1]["stock_code"] = "689001"

    result = client.post("/v1/event-portfolio-plans", json=payload, headers=headers())

    assert result.status_code == 200
    assert result.json()["schema_version"] == "1.1"
    assert result.json()["positions"][1]["stock_code"] == "689001"
    assert planner.calls == 1


@pytest.mark.parametrize(
    "mutation",
    ["request_id", "request_sha256", "schema_version", "order", "deadline"],
)
def test_portfolio_endpoint_rejects_unbound_or_late_planner_response(
    tmp_path,
    mutation: str,
) -> None:
    class MutatingPlanner(FakePortfolioPlanner):
        def plan_portfolio(self, request, request_hash):
            response = super().plan_portfolio(request, request_hash)
            if mutation == "request_id":
                return response.model_copy(update={"request_id": "different-request"})
            if mutation == "request_sha256":
                return response.model_copy(update={"request_sha256": "f" * 64})
            if mutation == "schema_version":
                return response.model_copy(update={"schema_version": "1.1"})
            if mutation == "order":
                return response.model_copy(update={"positions": list(reversed(response.positions))})
            return response.model_copy(update={"decided_at": request.decision_deadline})

    planner = MutatingPlanner()
    client = make_client(tmp_path, planner)
    payload = future_payload()

    failed = client.post("/v1/event-portfolio-plans", json=payload, headers=headers())
    repeated = client.post("/v1/event-portfolio-plans", json=payload, headers=headers())

    assert failed.status_code == 503
    assert failed.json()["error_code"] == "planner_failed"
    assert repeated.status_code == 409
    assert repeated.json()["error_code"] == "request_previously_failed"
    assert planner.calls == 1


def test_expired_portfolio_deadline_fails_before_planning(tmp_path) -> None:
    planner = FakePortfolioPlanner()
    client = make_client(tmp_path, planner)
    payload = valid_portfolio_request_payload()
    payload["requested_at"] = "2020-08-17T08:31:00+08:00"
    payload["decision_deadline"] = "2020-08-17T14:00:00+08:00"
    payload["event_window"]["starts_at"] = "2020-08-14T08:30:00+08:00"
    payload["event_window"]["ends_at"] = "2020-08-17T08:30:00+08:00"
    payload["events"][0]["published_at"] = "2020-08-16T10:00:00+08:00"
    payload["events"][0]["received_at"] = "2020-08-16T10:01:00+08:00"
    for instrument in payload["instruments"]:
        instrument["quote_at"] = "2020-08-17T08:30:30+08:00"

    result = client.post("/v1/event-portfolio-plans", json=payload, headers=headers())

    assert result.status_code == 504
    assert result.json()["error_code"] == "analysis_deadline_expired"
    assert planner.calls == 0


def test_portfolio_and_single_event_endpoints_share_one_analysis_slot(tmp_path) -> None:
    planner = BlockingPortfolioPlanner()
    client = make_client(tmp_path, planner)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first_future = pool.submit(
            client.post,
            "/v1/event-portfolio-plans",
            json=future_payload(),
            headers=headers(),
        )
        assert planner.started.wait(timeout=2)
        busy = client.post(
            "/v1/event-trade-plans",
            json=valid_request_payload(),
            headers=headers(),
        )
        planner.release.set()
        first = first_future.result(timeout=5)

    assert first.status_code == 200
    assert busy.status_code == 429
    assert busy.json()["error_code"] == "analysis_busy"


def test_portfolio_server_timeout_is_permanent_and_retains_shared_slot(tmp_path) -> None:
    async def scenario() -> None:
        planner = FakePortfolioPlanner(delay=1.3)
        settings = ApiSettings(
            bearer_token=TOKEN,
            state_path=tmp_path / "requests.sqlite3",
            timeout_seconds=5,
            host="127.0.0.1",
            port=8787,
            portfolio_timeout_seconds=1,
        )
        app = create_app(
            settings=settings,
            store=EventPlanStore(settings.state_path),
            portfolio_store=PortfolioPlanStore(settings.state_path),
            planner_factory=lambda: planner,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            payload = future_payload()
            timed_out = await client.post(
                "/v1/event-portfolio-plans",
                json=payload,
                headers=headers(),
            )
            repeated = await client.post(
                "/v1/event-portfolio-plans",
                json=payload,
                headers=headers(),
            )
            different = future_payload()
            different["request_id"] = "rachel_executor:portfolio:2099-08-18"
            busy = await client.post(
                "/v1/event-portfolio-plans",
                json=different,
                headers=headers(),
            )
            await asyncio.sleep(0.4)

            assert timed_out.status_code == 504
            assert timed_out.json()["error_code"] == "analysis_timeout"
            assert repeated.status_code == 409
            assert repeated.json()["error_code"] == "request_previously_failed"
            assert busy.status_code == 429
            assert busy.json()["error_code"] == "analysis_busy"

    asyncio.run(scenario())


def test_portfolio_concurrency_setting_defaults_to_four_and_is_closed(monkeypatch) -> None:
    monkeypatch.delenv("TRADINGAGENTS_PORTFOLIO_ANALYSIS_CONCURRENCY", raising=False)
    assert ApiSettings.from_env().portfolio_analysis_concurrency == 4

    for accepted in ("1", "4"):
        monkeypatch.setenv("TRADINGAGENTS_PORTFOLIO_ANALYSIS_CONCURRENCY", accepted)
        assert ApiSettings.from_env().portfolio_analysis_concurrency == int(accepted)

    for rejected in ("0", "5", "not-an-integer"):
        monkeypatch.setenv("TRADINGAGENTS_PORTFOLIO_ANALYSIS_CONCURRENCY", rejected)
        with pytest.raises(ValueError, match="TRADINGAGENTS_PORTFOLIO_ANALYSIS_CONCURRENCY"):
            ApiSettings.from_env()


def test_default_app_factory_receives_configured_portfolio_concurrency(
    tmp_path,
    monkeypatch,
) -> None:
    observed: list[int] = []

    class StubPlanner:
        def __init__(self, *, portfolio_concurrency: int):
            observed.append(portfolio_concurrency)

    monkeypatch.setattr(app_module, "TradingAgentsEventPlanner", StubPlanner)
    settings = ApiSettings(
        bearer_token=TOKEN,
        state_path=tmp_path / "requests.sqlite3",
        timeout_seconds=5,
        host="127.0.0.1",
        port=8787,
        portfolio_analysis_concurrency=3,
    )
    app = create_app(
        settings=settings,
        store=EventPlanStore(settings.state_path),
        portfolio_store=PortfolioPlanStore(settings.state_path),
    )

    assert TestClient(app).get("/health/ready").json() == {"status": "ready"}
    assert observed == [3]


@pytest.mark.parametrize(
    ("safe_stage", "expected_error_code"),
    [
        ("instrument_graph", "planner_failed_instrument_graph"),
        ("allocator_invoke", "planner_failed_allocator_invoke"),
    ],
)
def test_portfolio_failure_persists_safe_stage_without_exposing_details(
    tmp_path,
    caplog,
    safe_stage: str,
    expected_error_code: str,
) -> None:
    class FailingPlanner(FakePortfolioPlanner):
        def plan_portfolio(self, request, request_hash):
            self.calls += 1
            raise PlannerFailed(
                "secret provider response",
                safe_stage=safe_stage,
                completed_count=2,
                total_count=len(request.instruments),
            )

    planner = FailingPlanner()
    settings = ApiSettings(
        bearer_token=TOKEN,
        state_path=tmp_path / "requests.sqlite3",
        timeout_seconds=5,
        host="127.0.0.1",
        port=8787,
    )
    portfolio_store = PortfolioPlanStore(settings.state_path)
    app = create_app(
        settings=settings,
        store=EventPlanStore(settings.state_path),
        portfolio_store=portfolio_store,
        planner_factory=lambda: planner,
    )
    payload = future_payload()
    payload["events"][0]["summary"] = "secret event payload"
    request = EventPortfolioPlanRequest.model_validate(payload)
    request_hash = app_module._request_sha256(request)

    with caplog.at_level("WARNING", logger="tradingagents.api.app"):
        response = TestClient(app).post(
            "/v1/event-portfolio-plans",
            json=payload,
            headers=headers(),
        )

    assert response.status_code == 503
    assert response.json()["error_code"] == "planner_failed"
    assert "secret provider response" not in response.text
    assert "secret event payload" not in response.text
    claim = portfolio_store.claim(request.request_id, request_hash)
    assert claim.status == "failed"
    assert claim.error_code == expected_error_code
    assert f"failure_stage={safe_stage} completed=2 total=2" in caplog.text
    assert "secret provider response" not in caplog.text
    assert "secret event payload" not in caplog.text
