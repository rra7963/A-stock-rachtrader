"""FastAPI transport for the version-1 TradingAgents event plan contract."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .planner import (
    DEFAULT_PORTFOLIO_ANALYSIS_CONCURRENCY,
    MAX_PORTFOLIO_ANALYSIS_CONCURRENCY,
    EventPlanner,
    PlannerFailed,
    PlannerUnavailable,
    TradingAgentsEventPlanner,
)
from .portfolio_schemas import EventPortfolioPlanRequest, EventPortfolioPlanResponse
from .portfolio_store import PortfolioPlanStore
from .schemas import ApiError, EventTradePlanRequest, EventTradePlanResponse
from .store import EventPlanStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApiSettings:
    bearer_token: str
    state_path: Path
    timeout_seconds: int
    host: str
    port: int
    portfolio_timeout_seconds: int = 19_800
    portfolio_analysis_concurrency: int = DEFAULT_PORTFOLIO_ANALYSIS_CONCURRENCY

    @classmethod
    def from_env(cls) -> ApiSettings:
        timeout_seconds = _bounded_int(
            os.getenv("TRADINGAGENTS_API_TIMEOUT_SECONDS", "900"),
            "TRADINGAGENTS_API_TIMEOUT_SECONDS",
            minimum=1,
            maximum=900,
        )
        port = _bounded_int(
            os.getenv("TRADINGAGENTS_API_PORT", "8787"),
            "TRADINGAGENTS_API_PORT",
            minimum=1,
            maximum=65535,
        )
        portfolio_timeout_seconds = _bounded_int(
            os.getenv("TRADINGAGENTS_PORTFOLIO_API_TIMEOUT_SECONDS", "19800"),
            "TRADINGAGENTS_PORTFOLIO_API_TIMEOUT_SECONDS",
            minimum=1,
            maximum=19_800,
        )
        portfolio_analysis_concurrency = _bounded_int(
            os.getenv(
                "TRADINGAGENTS_PORTFOLIO_ANALYSIS_CONCURRENCY",
                str(DEFAULT_PORTFOLIO_ANALYSIS_CONCURRENCY),
            ),
            "TRADINGAGENTS_PORTFOLIO_ANALYSIS_CONCURRENCY",
            minimum=1,
            maximum=MAX_PORTFOLIO_ANALYSIS_CONCURRENCY,
        )
        return cls(
            bearer_token=os.getenv("TRADINGAGENTS_API_BEARER_TOKEN", ""),
            state_path=Path(
                os.getenv(
                    "TRADINGAGENTS_API_STATE_PATH",
                    "~/.tradingagents/event-plan-api/requests.sqlite3",
                )
            ).expanduser(),
            timeout_seconds=timeout_seconds,
            host=os.getenv("TRADINGAGENTS_API_HOST", "0.0.0.0"),
            port=port,
            portfolio_timeout_seconds=portfolio_timeout_seconds,
            portfolio_analysis_concurrency=portfolio_analysis_concurrency,
        )


def create_app(
    *,
    settings: ApiSettings | None = None,
    store: EventPlanStore | None = None,
    portfolio_store: PortfolioPlanStore | None = None,
    planner_factory: Callable[[], EventPlanner] | None = None,
) -> FastAPI:
    resolved_settings = settings or ApiSettings.from_env()
    resolved_store = store
    resolved_portfolio_store = portfolio_store
    resolved_planner: EventPlanner | None = None
    resource_lock = threading.Lock()
    analysis_lock = asyncio.Lock()
    analysis_admission_lock = asyncio.Lock()
    if planner_factory is None:

        def factory() -> EventPlanner:
            return TradingAgentsEventPlanner(
                portfolio_concurrency=resolved_settings.portfolio_analysis_concurrency
            )
    else:
        factory = planner_factory

    app = FastAPI(
        title="TradingAgents Event Trade Plan API",
        version="1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def authenticate_machine_routes(request: Request, call_next):
        if request.url.path in {
            "/v1/event-trade-plans",
            "/v1/event-portfolio-plans",
        }:
            unauthorized = _authorize(
                request.headers.get("Authorization"),
                resolved_settings.bearer_token,
            )
            if unauthorized is not None:
                return unauthorized
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def sanitized_validation_error(_request: Request, _exc: RequestValidationError):
        return _error(
            422,
            "invalid_request",
            "request does not satisfy the version-1 event plan contract",
        )

    def get_store() -> EventPlanStore:
        nonlocal resolved_store
        if resolved_store is None:
            with resource_lock:
                if resolved_store is None:
                    resolved_store = EventPlanStore(resolved_settings.state_path)
        return resolved_store

    def get_portfolio_store() -> PortfolioPlanStore:
        nonlocal resolved_portfolio_store
        if resolved_portfolio_store is None:
            with resource_lock:
                if resolved_portfolio_store is None:
                    resolved_portfolio_store = PortfolioPlanStore(resolved_settings.state_path)
        return resolved_portfolio_store

    def get_planner() -> EventPlanner:
        nonlocal resolved_planner
        if resolved_planner is None:
            with resource_lock:
                if resolved_planner is None:
                    resolved_planner = factory()
        return resolved_planner

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        if len(resolved_settings.bearer_token) < 32:
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        try:
            store_ready = get_store().ready() and get_portfolio_store().ready()
            get_planner()
        except Exception:
            store_ready = False
        if not store_ready:
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        return {"status": "ready"}

    @app.post(
        "/v1/event-trade-plans",
        response_model=EventTradePlanResponse,
        responses={
            401: {"model": ApiError},
            422: {"model": ApiError},
            409: {"model": ApiError},
            429: {"model": ApiError},
            503: {"model": ApiError},
            504: {"model": ApiError},
        },
    )
    async def create_plan(
        request: EventTradePlanRequest,
    ):
        request_sha256 = _request_sha256(request)
        request_store = get_store()
        claim = request_store.claim(request.request_id, request_sha256)
        if claim.status == "completed":
            return claim.response
        if claim.status == "conflict":
            return _error(
                409,
                "request_id_conflict",
                "request_id is already bound to different normalized input",
                request.request_id,
            )
        if claim.status == "running":
            return _error(
                409,
                "request_in_progress",
                "request_id is already being analyzed",
                request.request_id,
            )
        if claim.status == "failed":
            return _error(
                409,
                "request_previously_failed",
                "request_id previously failed and cannot be run automatically again",
                request.request_id,
            )

        if not await _try_acquire_analysis_slot(analysis_lock, analysis_admission_lock):
            request_store.fail(request.request_id, request_sha256, "analysis_busy")
            return _error(
                429,
                "analysis_busy",
                "another event analysis is already running",
                request.request_id,
            )

        release_lock = True
        try:
            try:
                planner = get_planner()
            except PlannerUnavailable:
                request_store.fail(request.request_id, request_sha256, "planner_unavailable")
                return _error(
                    503,
                    "planner_unavailable",
                    "configured planner cannot satisfy the strict response contract",
                    request.request_id,
                )

            worker = asyncio.create_task(asyncio.to_thread(planner.plan, request, request_sha256))
            try:
                response = await asyncio.wait_for(
                    asyncio.shield(worker),
                    timeout=resolved_settings.timeout_seconds,
                )
            except asyncio.TimeoutError:
                release_lock = False
                try:
                    request_store.fail(request.request_id, request_sha256, "analysis_timeout")
                finally:
                    _release_lock_after_worker(worker, analysis_lock)
                return _error(
                    504,
                    "analysis_timeout",
                    "event analysis exceeded the configured deadline",
                    request.request_id,
                )
            except asyncio.CancelledError:
                release_lock = False
                try:
                    request_store.fail(request.request_id, request_sha256, "analysis_cancelled")
                finally:
                    _release_lock_after_worker(worker, analysis_lock)
                raise
            except PlannerFailed:
                request_store.fail(request.request_id, request_sha256, "planner_failed")
                return _error(
                    503,
                    "planner_failed",
                    "event analysis failed closed",
                    request.request_id,
                )
            except Exception:
                request_store.fail(request.request_id, request_sha256, "planner_failed")
                return _error(
                    503,
                    "planner_failed",
                    "event analysis failed closed",
                    request.request_id,
                )

            request_store.complete(response)
            return response
        finally:
            if release_lock:
                analysis_lock.release()

    @app.post(
        "/v1/event-portfolio-plans",
        response_model=EventPortfolioPlanResponse,
        responses={
            401: {"model": ApiError},
            422: {"model": ApiError},
            409: {"model": ApiError},
            429: {"model": ApiError},
            503: {"model": ApiError},
            504: {"model": ApiError},
        },
    )
    async def create_portfolio_plan(request: EventPortfolioPlanRequest):
        request_sha256 = _request_sha256(request)
        request_store = get_portfolio_store()
        claim = request_store.claim(request.request_id, request_sha256)
        if claim.status == "completed":
            return claim.response
        if claim.status == "conflict":
            return _error(
                409,
                "request_id_conflict",
                "request_id is already bound to different normalized input",
                request.request_id,
            )
        if claim.status == "running":
            return _error(
                409,
                "request_in_progress",
                "request_id is already being analyzed",
                request.request_id,
            )
        if claim.status == "failed":
            return _error(
                409,
                "request_previously_failed",
                "request_id previously failed and cannot be run automatically again",
                request.request_id,
            )

        remaining_seconds = (
            request.decision_deadline.astimezone(timezone.utc) - datetime.now(timezone.utc)
        ).total_seconds()
        timeout_seconds = min(
            float(resolved_settings.portfolio_timeout_seconds),
            remaining_seconds,
        )
        if timeout_seconds <= 0:
            request_store.fail(request.request_id, request_sha256, "analysis_deadline_expired")
            return _error(
                504,
                "analysis_deadline_expired",
                "portfolio analysis deadline has expired",
                request.request_id,
            )
        if not await _try_acquire_analysis_slot(analysis_lock, analysis_admission_lock):
            request_store.fail(request.request_id, request_sha256, "analysis_busy")
            return _error(
                429,
                "analysis_busy",
                "another event analysis is already running",
                request.request_id,
            )

        release_lock = True
        try:
            try:
                planner = get_planner()
            except PlannerUnavailable:
                request_store.fail(request.request_id, request_sha256, "planner_unavailable")
                return _error(
                    503,
                    "planner_unavailable",
                    "configured planner cannot satisfy the strict response contract",
                    request.request_id,
                )

            worker = asyncio.create_task(
                asyncio.to_thread(planner.plan_portfolio, request, request_sha256)
            )
            try:
                response = await asyncio.wait_for(
                    asyncio.shield(worker),
                    timeout=timeout_seconds,
                )
                response = _validate_portfolio_response_binding(
                    request,
                    request_sha256,
                    response,
                )
                if datetime.now(timezone.utc) >= request.decision_deadline.astimezone(timezone.utc):
                    raise PlannerFailed(
                        "portfolio result arrived at or after its deadline",
                        safe_stage="deadline",
                        completed_count=len(request.instruments),
                        total_count=len(request.instruments),
                    )
            except asyncio.TimeoutError:
                release_lock = False
                try:
                    request_store.fail(request.request_id, request_sha256, "analysis_timeout")
                finally:
                    _release_lock_after_worker(worker, analysis_lock)
                return _error(
                    504,
                    "analysis_timeout",
                    "portfolio analysis exceeded the configured deadline",
                    request.request_id,
                )
            except asyncio.CancelledError:
                release_lock = False
                try:
                    request_store.fail(request.request_id, request_sha256, "analysis_cancelled")
                finally:
                    _release_lock_after_worker(worker, analysis_lock)
                raise
            except PlannerFailed as exc:
                request_store.fail(
                    request.request_id,
                    request_sha256,
                    exc.persistence_error_code,
                )
                logger.warning(
                    "event_portfolio_api stage=failed failure_stage=%s completed=%s total=%s",
                    exc.safe_stage,
                    _safe_count(exc.completed_count),
                    _safe_count(exc.total_count),
                )
                return _error(
                    503,
                    "planner_failed",
                    "portfolio analysis failed closed",
                    request.request_id,
                )
            except Exception:
                request_store.fail(request.request_id, request_sha256, "planner_failed")
                logger.warning(
                    "event_portfolio_api stage=failed failure_stage=planner "
                    "completed=unknown total=unknown"
                )
                return _error(
                    503,
                    "planner_failed",
                    "portfolio analysis failed closed",
                    request.request_id,
                )

            request_store.complete(response)
            return response
        finally:
            if release_lock:
                analysis_lock.release()

    return app


def _authorize(authorization: str | None, expected_token: str) -> JSONResponse | None:
    if len(expected_token) < 32:
        return _error(
            503,
            "planner_unavailable",
            "event plan API is not configured",
        )
    prefix = "Bearer "
    if authorization is None or not authorization.startswith(prefix):
        return _error(401, "unauthorized", "valid bearer authentication is required")
    supplied = authorization[len(prefix) :]
    if not secrets.compare_digest(supplied, expected_token):
        return _error(401, "unauthorized", "valid bearer authentication is required")
    return None


def _request_sha256(request: EventTradePlanRequest | EventPortfolioPlanRequest) -> str:
    canonical = json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _validate_portfolio_response_binding(
    request: EventPortfolioPlanRequest,
    request_sha256: str,
    response: object,
) -> EventPortfolioPlanResponse:
    """Fail closed before persistence when a planner escapes its request boundary."""

    try:
        validated = EventPortfolioPlanResponse.model_validate(response)
    except Exception as exc:
        raise PlannerFailed(
            "portfolio planner returned an invalid response",
            safe_stage="response_binding",
            total_count=len(request.instruments),
        ) from exc
    if validated.request_id != request.request_id:
        raise PlannerFailed(
            "portfolio response request id does not match",
            safe_stage="response_binding",
            total_count=len(request.instruments),
        )
    if validated.request_sha256 != request_sha256:
        raise PlannerFailed(
            "portfolio response request hash does not match",
            safe_stage="response_binding",
            total_count=len(request.instruments),
        )
    if validated.schema_version != request.schema_version:
        raise PlannerFailed(
            "portfolio response schema version does not match",
            safe_stage="response_binding",
            total_count=len(request.instruments),
        )
    expected_codes = [instrument.stock_code for instrument in request.instruments]
    actual_codes = [position.stock_code for position in validated.positions]
    if actual_codes != expected_codes:
        raise PlannerFailed(
            "portfolio response membership or order does not match",
            safe_stage="response_binding",
            total_count=len(request.instruments),
        )
    if validated.decided_at.astimezone(timezone.utc) >= request.decision_deadline.astimezone(
        timezone.utc
    ):
        raise PlannerFailed(
            "portfolio response decision is at or after its deadline",
            safe_stage="deadline",
            completed_count=len(request.instruments),
            total_count=len(request.instruments),
        )
    return validated


async def _try_acquire_analysis_slot(
    analysis_lock: asyncio.Lock,
    admission_lock: asyncio.Lock,
) -> bool:
    """Atomically reject contention instead of queueing an analysis request."""

    async with admission_lock:
        if analysis_lock.locked():
            return False
        await analysis_lock.acquire()
        return True


def _release_lock_after_worker(worker: asyncio.Task, analysis_lock: asyncio.Lock) -> None:
    """Transfer lock ownership to a detached worker until its real completion."""
    worker.add_done_callback(lambda task: _release_after_late_result(task, analysis_lock))


def _release_after_late_result(task: asyncio.Task, analysis_lock: asyncio.Lock) -> None:
    """Discard a detached thread result and release its single-analysis lock."""
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.result()
    analysis_lock.release()


def _error(
    status_code: int,
    error_code: str,
    message: str,
    request_id: str | None = None,
) -> JSONResponse:
    body = ApiError(error_code=error_code, message=message, request_id=request_id)
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


def _bounded_int(raw: str, name: str, *, minimum: int, maximum: int) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _safe_count(value: int | None) -> int | str:
    return value if value is not None else "unknown"


app = create_app()


def run() -> None:
    settings = ApiSettings.from_env()
    uvicorn.run(app, host=settings.host, port=settings.port, workers=1)
