from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

import pytest
import requests

from tradingagents.integrations.dynamic_agent_api import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_READ_TIMEOUT_SECONDS,
    RUN_PATH,
    DynamicAgentApiError,
    DynamicAgentApiSettings,
    DynamicAgentClient,
    DynamicAgentResultUncertain,
    call_dynamic_agent,
)


class _FakeResponse:
    def __init__(self, status_code: int, body: Any, *, json_error: ValueError | None = None):
        self.status_code = status_code
        self._body = body
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._body


class _FakeSession:
    def __init__(self, response: _FakeResponse | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def post(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response

    def close(self) -> None:
        self.closed = True


def _settings() -> DynamicAgentApiSettings:
    return DynamicAgentApiSettings(
        base_url="http://etf-agent-runtime-tunnel:18001",
        internal_token="internal-test-token",
    )


def _success_body(model: str = "openrouter/auto") -> dict[str, Any]:
    return {
        "success": True,
        "run_id": "run-123",
        "key_id": "key-123",
        "model": model,
        "content": "review complete",
        "usage": {
            "llm_call_count": 1,
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "cached_input_tokens": 0,
        },
        "duration_ms": 250,
    }


@contextmanager
def _recording_server(
    *,
    status_code: int = 200,
    response_body: dict[str, Any] | None = None,
    response_headers: dict[str, str] | None = None,
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    records: list[dict[str, Any]] = []
    body = json.dumps(response_body or {}).encode()
    headers = response_headers or {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            content_length = int(self.headers.get("Content-Length", "0"))
            records.append(
                {
                    "path": self.path,
                    "headers": dict(self.headers.items()),
                    "body": self.rfile.read(content_length),
                }
            )
            self.send_response(status_code)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", records
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.unit
def test_settings_load_complete_environment_and_normalize_base_url():
    settings = DynamicAgentApiSettings.from_env(
        {
            "AGENT_RUNTIME_BASE_URL": "http://etf-agent-runtime-tunnel:18001/",
            "INTERNAL_AGENT_TOKEN": "internal-token",
        }
    )

    assert settings == DynamicAgentApiSettings(
        base_url="http://etf-agent-runtime-tunnel:18001",
        internal_token="internal-token",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "missing_name",
    ["AGENT_RUNTIME_BASE_URL", "INTERNAL_AGENT_TOKEN"],
)
def test_settings_reject_missing_required_environment(missing_name: str):
    values = {
        "AGENT_RUNTIME_BASE_URL": "http://etf-agent-runtime-tunnel:18001",
        "INTERNAL_AGENT_TOKEN": "internal-token",
    }
    values.pop(missing_name)

    with pytest.raises(RuntimeError, match=missing_name):
        DynamicAgentApiSettings.from_env(values)


@pytest.mark.unit
@pytest.mark.parametrize(
    "base_url",
    [
        "etf-agent-runtime-tunnel:18001",
        "ftp://etf-agent-runtime-tunnel:18001",
        "http://user:pass@etf-agent-runtime-tunnel:18001",
        "http://etf-agent-runtime-tunnel:18001/api",
        "http://etf-agent-runtime-tunnel:18001?debug=true",
    ],
)
def test_settings_reject_invalid_base_url(base_url: str):
    with pytest.raises(ValueError, match="AGENT_RUNTIME_BASE_URL"):
        DynamicAgentApiSettings.from_env(
            {
                "AGENT_RUNTIME_BASE_URL": base_url,
                "INTERNAL_AGENT_TOKEN": "internal-token",
            }
        )


@pytest.mark.unit
def test_run_posts_dynamic_model_authentication_and_payload():
    session = _FakeSession(_FakeResponse(200, _success_body("openrouter/auto")))
    client = DynamicAgentClient(_settings(), session=session)

    result = client.run(
        model="openrouter/auto",
        prompt="review",
        content="signal",
        openrouter_api_key="openrouter-test-key",
        temperature=0.2,
        max_tokens=600,
        key_alias="strategy-a",
        caller_request_id="strategy-a-001",
    )

    assert result.content == "review complete"
    assert result.model == "openrouter/auto"
    assert session.calls == [
        {
            "url": f"http://etf-agent-runtime-tunnel:18001{RUN_PATH}",
            "headers": {
                "Authorization": "Bearer internal-test-token",
                "X-OpenRouter-Api-Key": "openrouter-test-key",
            },
            "json": {
                "model": "openrouter/auto",
                "prompt": "review",
                "content": "signal",
                "caller_request_id": "strategy-a-001",
                "temperature": 0.2,
                "max_tokens": 600,
                "key_alias": "strategy-a",
            },
            "timeout": (DEFAULT_CONNECT_TIMEOUT_SECONDS, DEFAULT_READ_TIMEOUT_SECONDS),
            "allow_redirects": False,
        }
    ]


@pytest.mark.unit
def test_convenience_call_loads_environment_and_closes_owned_session(monkeypatch):
    session = _FakeSession(_FakeResponse(200, _success_body()))
    monkeypatch.setenv("AGENT_RUNTIME_BASE_URL", "http://etf-agent-runtime-tunnel:18001")
    monkeypatch.setenv("INTERNAL_AGENT_TOKEN", "internal-test-token")
    monkeypatch.setattr(
        "tradingagents.integrations.dynamic_agent_api.requests.Session",
        lambda: session,
    )

    result = call_dynamic_agent(
        model="openrouter/auto",
        prompt="review",
        content="signal",
        openrouter_api_key="openrouter-test-key",
        temperature=0.35,
        max_tokens=500,
        caller_request_id="strategy-a-convenience",
    )

    assert result.run_id == "run-123"
    assert session.calls[0]["headers"] == {
        "Authorization": "Bearer internal-test-token",
        "X-OpenRouter-Api-Key": "openrouter-test-key",
    }
    assert session.calls[0]["json"]["temperature"] == 0.35
    assert session.calls[0]["json"]["max_tokens"] == 500
    assert session.closed is True


@pytest.mark.unit
def test_run_omits_optional_request_fields_when_not_supplied():
    session = _FakeSession(_FakeResponse(200, _success_body()))
    client = DynamicAgentClient(_settings(), session=session)

    client.run(
        model="openrouter/auto",
        prompt="review",
        content="signal",
        openrouter_api_key="openrouter-test-key",
        caller_request_id="strategy-a-002",
    )

    assert session.calls[0]["json"] == {
        "model": "openrouter/auto",
        "prompt": "review",
        "content": "signal",
        "caller_request_id": "strategy-a-002",
    }


@pytest.mark.unit
def test_run_exposes_structured_api_error_without_credentials():
    session = _FakeSession(
        _FakeResponse(
            502,
            {
                "success": False,
                "run_id": "run-failed",
                "error_code": "openrouter_request_failed",
                "message": ("provider rejected internal-test-token and openrouter-test-key"),
            },
        )
    )
    client = DynamicAgentClient(_settings(), session=session)

    with pytest.raises(DynamicAgentApiError) as captured:
        client.run(
            model="provider/model",
            prompt="review",
            content="signal",
            openrouter_api_key="openrouter-test-key",
            caller_request_id="strategy-a-003",
        )

    assert captured.value.status_code == 502
    assert captured.value.error_code == "openrouter_request_failed"
    assert captured.value.run_id == "run-failed"
    assert str(captured.value) == "Dynamic Agent API request failed"
    assert "internal-test-token" not in str(captured.value)
    assert "openrouter-test-key" not in str(captured.value)


@pytest.mark.unit
def test_owned_session_ignores_environment_http_proxy(monkeypatch):
    with (
        _recording_server(response_body=_success_body()) as (target_url, target_records),
        _recording_server(response_body=_success_body()) as (proxy_url, proxy_records),
    ):
        monkeypatch.setenv("HTTP_PROXY", proxy_url)
        monkeypatch.setenv("NO_PROXY", "")
        settings = DynamicAgentApiSettings(
            base_url=target_url,
            internal_token="internal-test-token",
        )

        with DynamicAgentClient(settings) as client:
            result = client.run(
                model="openrouter/auto",
                prompt="review",
                content="signal",
                openrouter_api_key="openrouter-test-key",
                caller_request_id="strategy-proxy-test",
            )

    assert result.run_id == "run-123"
    assert len(target_records) == 1
    assert proxy_records == []


@pytest.mark.unit
def test_cross_origin_redirect_does_not_receive_credentials():
    with (
        _recording_server() as (redirect_target_url, redirect_target_records),
        _recording_server(
            status_code=307,
            response_headers={"Location": f"{redirect_target_url}/capture"},
        ) as (source_url, source_records),
    ):
        settings = DynamicAgentApiSettings(
            base_url=source_url,
            internal_token="internal-test-token",
        )

        with (
            DynamicAgentClient(settings) as client,
            pytest.raises(DynamicAgentApiError) as captured,
        ):
            client.run(
                model="openrouter/auto",
                prompt="review",
                content="signal",
                openrouter_api_key="openrouter-test-key",
                caller_request_id="strategy-redirect-test",
            )

    assert captured.value.status_code == 307
    assert captured.value.error_code == "unexpected_redirect"
    assert len(source_records) == 1
    assert source_records[0]["headers"]["Authorization"] == ("Bearer internal-test-token")
    assert source_records[0]["headers"]["X-OpenRouter-Api-Key"] == ("openrouter-test-key")
    assert redirect_target_records == []


@pytest.mark.unit
def test_run_marks_transport_failure_uncertain_and_does_not_retry():
    session = _FakeSession(error=requests.Timeout("timed out"))
    client = DynamicAgentClient(_settings(), session=session)

    with pytest.raises(DynamicAgentResultUncertain) as captured:
        client.run(
            model="provider/model",
            prompt="review",
            content="signal",
            openrouter_api_key="openrouter-test-key",
            caller_request_id="strategy-a-004",
        )

    assert captured.value.caller_request_id == "strategy-a-004"
    assert len(session.calls) == 1


@pytest.mark.unit
def test_run_rejects_non_json_response():
    session = _FakeSession(_FakeResponse(200, None, json_error=ValueError("not json")))
    client = DynamicAgentClient(_settings(), session=session)

    with pytest.raises(DynamicAgentApiError) as captured:
        client.run(
            model="provider/model",
            prompt="review",
            content="signal",
            openrouter_api_key="openrouter-test-key",
            caller_request_id="strategy-a-005",
        )

    assert captured.value.error_code == "non_json_response"


@pytest.mark.unit
def test_run_rejects_invalid_success_contract():
    session = _FakeSession(_FakeResponse(200, {"success": True, "content": "incomplete"}))
    client = DynamicAgentClient(_settings(), session=session)

    with pytest.raises(DynamicAgentApiError) as captured:
        client.run(
            model="provider/model",
            prompt="review",
            content="signal",
            openrouter_api_key="openrouter-test-key",
            caller_request_id="strategy-a-006",
        )

    assert captured.value.error_code == "invalid_response_contract"


@pytest.mark.unit
def test_run_rejects_oversized_caller_request_id_before_http():
    session = _FakeSession(_FakeResponse(200, _success_body()))
    client = DynamicAgentClient(_settings(), session=session)

    with pytest.raises(ValueError, match="128"):
        client.run(
            model="provider/model",
            prompt="review",
            content="signal",
            openrouter_api_key="openrouter-test-key",
            caller_request_id="x" * 129,
        )

    assert session.calls == []


@pytest.mark.unit
def test_run_rejects_empty_openrouter_key_before_http():
    session = _FakeSession(_FakeResponse(200, _success_body()))
    client = DynamicAgentClient(_settings(), session=session)

    with pytest.raises(ValueError, match="openrouter_api_key"):
        client.run(
            model="provider/model",
            prompt="review",
            content="signal",
            openrouter_api_key="   ",
            caller_request_id="strategy-a-007",
        )

    assert session.calls == []
