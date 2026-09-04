"""Client for ETF Platform's internal Dynamic Agent API."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

import requests
from pydantic import BaseModel, ConfigDict, Field, ValidationError

RUN_PATH = "/api/internal/agent-runtime/runs"
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10
DEFAULT_READ_TIMEOUT_SECONDS = 650


class DynamicAgentUsage(BaseModel):
    """Token usage returned by one Dynamic Agent run."""

    model_config = ConfigDict(extra="ignore")

    llm_call_count: int | None = Field(default=None, ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)


class DynamicAgentRunResult(BaseModel):
    """Validated success response from the Dynamic Agent API."""

    model_config = ConfigDict(extra="ignore")

    success: Literal[True]
    run_id: str = Field(min_length=1)
    key_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    content: str
    usage: DynamicAgentUsage
    duration_ms: int = Field(ge=0)


class DynamicAgentApiError(RuntimeError):
    """The API returned an explicit HTTP or response-contract failure."""

    def __init__(
        self,
        message: str,
        *,
        caller_request_id: str,
        status_code: int | None = None,
        error_code: str | None = None,
        run_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.caller_request_id = caller_request_id
        self.status_code = status_code
        self.error_code = error_code
        self.run_id = run_id


class DynamicAgentResultUncertain(RuntimeError):
    """The caller did not receive a response, so execution may have happened."""

    def __init__(self, *, caller_request_id: str) -> None:
        super().__init__(
            "Dynamic Agent API did not return a final response; do not retry automatically "
            f"(caller_request_id={caller_request_id})"
        )
        self.caller_request_id = caller_request_id


@dataclass(frozen=True)
class DynamicAgentApiSettings:
    """Connection settings loaded from the process environment."""

    base_url: str
    internal_token: str

    @classmethod
    def from_env(
        cls,
        values: Mapping[str, str] | None = None,
    ) -> DynamicAgentApiSettings:
        source = os.environ if values is None else values
        base_url = _required_value(source, "AGENT_RUNTIME_BASE_URL").rstrip("/")
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("AGENT_RUNTIME_BASE_URL must be an HTTP(S) origin without credentials")
        return cls(
            base_url=base_url,
            internal_token=_required_value(source, "INTERNAL_AGENT_TOKEN"),
        )


class DynamicAgentClient:
    """Reusable synchronous client for the Dynamic Agent API.

    An internally created session ignores environment proxy settings. A
    caller-supplied session is not mutated; its proxy and ``trust_env`` safety
    are the caller's responsibility because both credentials are sent through it.
    """

    def __init__(
        self,
        settings: DynamicAgentApiSettings,
        *,
        session: requests.Session | None = None,
    ) -> None:
        self.settings = settings
        if session is None:
            self._session = requests.Session()
            self._session.trust_env = False
            self._owns_session = True
        else:
            self._session = session
            self._owns_session = False

    @classmethod
    def from_env(cls) -> DynamicAgentClient:
        return cls(DynamicAgentApiSettings.from_env())

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def __enter__(self) -> DynamicAgentClient:
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def run(
        self,
        *,
        model: str,
        prompt: str,
        content: str,
        openrouter_api_key: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        key_alias: str | None = None,
        caller_request_id: str | None = None,
    ) -> DynamicAgentRunResult:
        """Run one Agent request and return a validated response."""

        request_id = caller_request_id or f"tradingagents-{uuid4()}"
        if len(request_id) > 128:
            raise ValueError("caller_request_id must not exceed 128 characters")
        resolved_openrouter_api_key = openrouter_api_key.strip()
        if not resolved_openrouter_api_key:
            raise ValueError("openrouter_api_key must not be empty")

        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "content": content,
            "caller_request_id": request_id,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if key_alias is not None:
            payload["key_alias"] = key_alias

        try:
            response = self._session.post(
                f"{self.settings.base_url}{RUN_PATH}",
                headers={
                    "Authorization": f"Bearer {self.settings.internal_token}",
                    "X-OpenRouter-Api-Key": resolved_openrouter_api_key,
                },
                json=payload,
                timeout=(DEFAULT_CONNECT_TIMEOUT_SECONDS, DEFAULT_READ_TIMEOUT_SECONDS),
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            # A transport failure does not prove that the synchronous server stopped.
            raise DynamicAgentResultUncertain(caller_request_id=request_id) from exc

        if 300 <= response.status_code < 400:
            raise DynamicAgentApiError(
                "Dynamic Agent API returned an unexpected redirect",
                caller_request_id=request_id,
                status_code=response.status_code,
                error_code="unexpected_redirect",
            )

        response_body = _response_json(response, request_id)
        if response.status_code >= 400:
            error_code = _optional_string(response_body, "error_code") or "unknown_error"
            run_id = _optional_string(response_body, "run_id")
            raise DynamicAgentApiError(
                "Dynamic Agent API request failed",
                caller_request_id=request_id,
                status_code=response.status_code,
                error_code=error_code,
                run_id=run_id,
            )

        try:
            return DynamicAgentRunResult.model_validate(response_body)
        except ValidationError as exc:
            raise DynamicAgentApiError(
                "Dynamic Agent API returned an invalid success response",
                caller_request_id=request_id,
                status_code=response.status_code,
                error_code="invalid_response_contract",
            ) from exc


def call_dynamic_agent(
    *,
    model: str,
    prompt: str,
    content: str,
    openrouter_api_key: str,
    temperature: float | None = None,
    max_tokens: int | None = None,
    key_alias: str | None = None,
    caller_request_id: str | None = None,
) -> DynamicAgentRunResult:
    """Convenience entry point for occasional one-off calls.

    ``key_alias`` is an optional, non-sensitive label such as
    ``tradingagents-default``; it must never contain the real API key.
    ``caller_request_id`` is an optional unique business trace ID. When omitted,
    the client generates one; it is not an idempotency key.
    """

    with DynamicAgentClient.from_env() as client:
        return client.run(
            model=model,
            prompt=prompt,
            content=content,
            openrouter_api_key=openrouter_api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            key_alias=key_alias,
            caller_request_id=caller_request_id,
        )


def _required_value(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def _response_json(response: requests.Response, caller_request_id: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise DynamicAgentApiError(
            "Dynamic Agent API returned a non-JSON response",
            caller_request_id=caller_request_id,
            status_code=response.status_code,
            error_code="non_json_response",
        ) from exc
    if not isinstance(body, dict):
        raise DynamicAgentApiError(
            "Dynamic Agent API returned a non-object response",
            caller_request_id=caller_request_id,
            status_code=response.status_code,
            error_code="invalid_response_contract",
        )
    return body


def _optional_string(payload: Mapping[str, Any], name: str) -> str | None:
    value = payload.get(name)
    return value if isinstance(value, str) and value else None
