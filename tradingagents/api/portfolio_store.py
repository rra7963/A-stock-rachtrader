"""Independent persistent idempotency state for portfolio-plan requests."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .portfolio_schemas import EventPortfolioPlanResponse

ClaimStatus = Literal["started", "completed", "conflict", "running", "failed"]
ALLOWED_PORTFOLIO_FAILURE_CODES = frozenset(
    {
        "process_interrupted",
        "analysis_deadline_expired",
        "analysis_busy",
        "planner_unavailable",
        "analysis_timeout",
        "analysis_cancelled",
        "planner_failed",
        "planner_failed_request",
        "planner_failed_deadline",
        "planner_failed_instrument_context",
        "planner_failed_instrument_graph",
        "planner_failed_instrument_rating",
        "planner_failed_allocator",
        "planner_failed_allocator_invoke",
        "planner_failed_allocator_schema",
        "planner_failed_allocator_binding",
        "planner_failed_allocator_total",
        "planner_failed_response_binding",
        "planner_failed_worker_execution",
    }
)


@dataclass(frozen=True)
class PortfolioClaimResult:
    status: ClaimStatus
    response: EventPortfolioPlanResponse | None = None
    error_code: str | None = None


class PortfolioPlanStore:
    """SQLite state machine isolated from the legacy single-event response schema."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS event_portfolio_plan_requests (
                    request_id TEXT PRIMARY KEY,
                    request_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
                    response_json TEXT,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (
                        (status = 'completed' AND response_json IS NOT NULL AND error_code IS NULL)
                        OR
                        (status = 'failed' AND response_json IS NULL AND error_code IS NOT NULL)
                        OR
                        (status = 'running' AND response_json IS NULL AND error_code IS NULL)
                    )
                )
                """
            )
            connection.execute(
                """
                UPDATE event_portfolio_plan_requests
                SET status = 'failed', error_code = 'process_interrupted', updated_at = ?
                WHERE status = 'running'
                """,
                (_utc_now_text(),),
            )
        os.chmod(self.path, 0o600)

    def ready(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1 FROM event_portfolio_plan_requests LIMIT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    def claim(self, request_id: str, request_sha256: str) -> PortfolioClaimResult:
        now = _utc_now_text()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT request_sha256, status, response_json, error_code
                FROM event_portfolio_plan_requests WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO event_portfolio_plan_requests (
                        request_id, request_sha256, status, response_json,
                        error_code, created_at, updated_at
                    ) VALUES (?, ?, 'running', NULL, NULL, ?, ?)
                    """,
                    (request_id, request_sha256, now, now),
                )
                connection.execute("COMMIT")
                return PortfolioClaimResult("started")
            connection.execute("COMMIT")
            if row["request_sha256"] != request_sha256:
                return PortfolioClaimResult("conflict")
            if row["status"] == "completed":
                return PortfolioClaimResult(
                    "completed",
                    response=EventPortfolioPlanResponse.model_validate_json(row["response_json"]),
                )
            if row["status"] == "running":
                return PortfolioClaimResult("running")
            return PortfolioClaimResult("failed", error_code=row["error_code"])

    def complete(self, response: EventPortfolioPlanResponse) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE event_portfolio_plan_requests
                SET status = 'completed', response_json = ?, error_code = NULL, updated_at = ?
                WHERE request_id = ? AND request_sha256 = ? AND status = 'running'
                """,
                (
                    response.model_dump_json(),
                    _utc_now_text(),
                    response.request_id,
                    response.request_sha256,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("portfolio request is not in the expected running state")

    def fail(self, request_id: str, request_sha256: str, error_code: str) -> None:
        if error_code not in ALLOWED_PORTFOLIO_FAILURE_CODES:
            raise ValueError("portfolio failure code is not allowlisted")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE event_portfolio_plan_requests
                SET status = 'failed', response_json = NULL, error_code = ?, updated_at = ?
                WHERE request_id = ? AND request_sha256 = ? AND status = 'running'
                """,
                (error_code, _utc_now_text(), request_id, request_sha256),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("portfolio request is not in the expected running state")


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()
