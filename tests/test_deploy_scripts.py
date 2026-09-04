from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
LINUX_SHELL = pytest.mark.skipif(
    os.name == "nt" or BASH is None or shutil.which("flock") is None,
    reason="deployment shell integration tests require Linux bash and flock",
)
SHA = "a" * 40
NEWER_SHA = "b" * 40


def _write(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)


def _fake_docker(fake_bin: Path) -> None:
    _write(
        fake_bin / "docker",
        """#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  ps)
    if [[ " $* " == *" label=com.docker.compose.service=tradingagents "* ]]; then
      printf '%s' "${FAKE_TRADINGAGENTS_CONTAINER_IDS:-${FAKE_CONTAINER_IDS:-}}"
    elif [[ " $* " == *" label=com.docker.compose.service=event-plan-api "* ]]; then
      printf '%s' "${FAKE_EVENT_PLAN_API_CONTAINER_IDS:-}"
    else
      printf '%s' "${FAKE_CONTAINER_IDS:-}"
    fi
    ;;
  compose)
    if [ -n "${FAKE_COMPOSE_LOG:-}" ]; then
      printf 'agent_runtime_env=%s command=%s\n' \
        "${AGENT_RUNTIME_ENV_FILE:-}" "$*" >>"$FAKE_COMPOSE_LOG"
    fi
    if [[ " $* " == *" config --services "* ]]; then
      printf '%s\n' "${FAKE_SERVICES:-tradingagents}"
    else
      printf 'unexpected docker compose command: %s\n' "$*" >&2
      exit 2
    fi
    ;;
  inspect)
    printf '%s\n' "${FAKE_CONTAINER_IMAGE:-}|${FAKE_CONTAINER_SHA:-}"
    ;;
  *)
    printf 'unexpected docker command: %s\n' "$*" >&2
    exit 2
    ;;
esac
""",
        0o755,
    )


def _run_baseline(deploy_dir: Path, fake_bin: Path, **extra_env: str):
    env = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DEPLOY_DIR": str(deploy_dir),
        "DEPLOY_LOCK_TIMEOUT_SECONDS": "2",
        "COMPOSE_PROJECT_NAME": "a-stock-rachtrader",
    }
    env.update(extra_env)
    return subprocess.run(
        [BASH, str(ROOT / "scripts/read-deploy-baseline.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=8,
    )


def _prepare_background(tmp_path: Path, deploy_env: str | None = None):
    deploy_dir = tmp_path / "deploy"
    release_dir = deploy_dir / "releases" / SHA
    fake_bin = tmp_path / "bin"
    image_uri = f"registry.example/team/a-stock-rachtrader:{SHA}"
    _write(release_dir / "docker-compose.server.yml", "services: {}\n")
    _write(deploy_dir / ".env", "OPENROUTER_API_KEY=test\n", 0o600)
    _write(
        deploy_dir / ".event-plan-api.env",
        f"TRADINGAGENTS_API_BEARER_TOKEN={'t' * 32}\n",
        0o600,
    )
    _write(
        deploy_dir / ".deploy.env",
        deploy_env or "ACR_PULL_USER=test\nACR_PULL_PASSWORD=test\n",
        0o600,
    )
    agent_runtime_env = tmp_path / "agent-runtime.env"
    _write(
        agent_runtime_env,
        "AGENT_RUNTIME_BASE_URL=http://agent-runtime.test:18001\n"
        "INTERNAL_AGENT_TOKEN=test-token\n",
        0o600,
    )
    _write(
        deploy_dir / "run/deploy-acr.latest-request",
        "\n".join(
            [
                "request_id=100.1",
                "run_id=100",
                "run_attempt=1",
                f"sha={SHA}",
                f"image_uri={image_uri}",
                "",
            ]
        ),
        0o600,
    )
    env = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DEPLOY_DIR": str(deploy_dir),
        "RELEASE_DIR": str(release_dir),
        "GIT_SHA": SHA,
        "IMAGE_URI": image_uri,
        "DEPLOY_REQUEST_ID": "100.1",
        "DEPLOY_LOCK_TIMEOUT_SECONDS": "2",
        "PULL_MAX_ATTEMPTS": "1",
        "HEALTH_TIMEOUT_SECONDS": "8",
        "HEALTH_INTERVAL_SECONDS": "1",
        "STABLE_HEALTH_SECONDS": "1",
        "FAKE_DEPLOY_DIR": str(deploy_dir),
        "AGENT_RUNTIME_ENV_FILE": str(agent_runtime_env),
    }
    return deploy_dir, release_dir, fake_bin, image_uri, env


def _write_wait_result(
    deploy_dir: Path,
    status: str,
    *,
    content_request_id: str = "100.1",
    sha: str = SHA,
    image_uri: str | None = None,
    message: str = "deployment result",
    mode: int = 0o600,
) -> Path:
    image_uri = image_uri or f"registry.example/team/a-stock-rachtrader:{sha}"
    result_file = deploy_dir / "run/deploy-results/acr/100.1.result"
    _write(
        result_file,
        "\n".join(
            [
                f"status={status}",
                "pid=123",
                f"request_id={content_request_id}",
                f"sha={sha}",
                f"image_uri={image_uri}",
                "previous_sha=",
                f"release_dir={deploy_dir / 'releases' / sha}",
                "time=2026-09-02T12:00:00+08:00",
                f"message={message}",
                "",
            ]
        ),
        mode,
    )
    return result_file


def _run_result_waiter(deploy_dir: Path, **extra_env: str):
    env = os.environ | {
        "DEPLOY_DIR": str(deploy_dir),
        "GIT_SHA": SHA,
        "DEPLOY_RUN_ID": "100",
        "DEPLOY_RUN_ATTEMPT": "1",
        "IMAGE_URI": f"registry.example/team/a-stock-rachtrader:{SHA}",
        "DEPLOY_RESULT_TIMEOUT_SECONDS": "1",
        "DEPLOY_RESULT_POLL_INTERVAL_SECONDS": "1",
    }
    env.update(extra_env)
    return subprocess.run(
        [BASH, str(ROOT / "scripts/wait-acr-deploy-result.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )


def _fake_background_docker(fake_bin: Path) -> None:
    _write(
        fake_bin / "docker",
        """#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  login|pull|logout)
    exit 0
    ;;
  compose)
    if [ -n "${FAKE_COMPOSE_LOG:-}" ]; then
      printf 'agent_runtime_env=%s command=%s\n' \
        "${AGENT_RUNTIME_ENV_FILE:-}" "$*" >>"$FAKE_COMPOSE_LOG"
    fi
    if [ -n "${FAKE_SMOKE_FAIL_MATCH:-}" ] \
      && [[ "$*" == *"$FAKE_SMOKE_FAIL_MATCH"* ]]; then
      attempts_file="${FAKE_SMOKE_ATTEMPTS_FILE:?}"
      attempts=0
      if [ -f "$attempts_file" ]; then
        attempts="$(cat "$attempts_file")"
      fi
      attempts=$((attempts + 1))
      printf '%s\n' "$attempts" >"$attempts_file"
      if [ "$attempts" -le "${FAKE_SMOKE_FAIL_COUNT:-0}" ]; then
        exit 23
      fi
    fi
    if [[ " $* " == *" config --services "* ]]; then
      printf '%s\n' "${FAKE_SERVICES:-tradingagents}"
    elif [[ " $* " == *" ps -q tradingagents "* ]]; then
      printf 'container-1\n'
    elif [[ " $* " == *" ps -q event-plan-api "* ]]; then
      printf 'container-2\n'
    elif [[ " $* " == *" up -d --no-build --remove-orphans "* ]]; then
      printf '%s\n' "$TRADINGAGENTS_IMAGE" >"$FAKE_DEPLOY_DIR/current-image"
      printf '%s\n' "$GIT_SHA" >"$FAKE_DEPLOY_DIR/current-sha"
    elif [[ " $* " == *" down --remove-orphans "* ]]; then
      : >"$FAKE_DEPLOY_DIR/down-called"
    elif [[ " $* " == *" ps "* ]] && [ -f "$FAKE_DEPLOY_DIR/.deploy-current" ]; then
      exit 42
    fi
    ;;
  inspect)
    format="${3:-}"
    case "$format" in
      *State.Running*) printf 'true\n' ;;
      *State.Health*) printf 'healthy\n' ;;
      *Config.Image*) cat "$FAKE_DEPLOY_DIR/current-image" ;;
      *io.rachel.deploy.sha*) cat "$FAKE_DEPLOY_DIR/current-sha" ;;
      *.Image*) printf 'sha256:container-image\n' ;;
      *) printf 'unexpected docker inspect format: %s\n' "$format" >&2; exit 2 ;;
    esac
    ;;
  image)
    case "${2:-}" in
      inspect) exit 0 ;;
      ls) exit 0 ;;
      rm) exit 0 ;;
      *) printf 'unexpected docker image command: %s\n' "$*" >&2; exit 2 ;;
    esac
    ;;
  ps)
    if [[ "${FAKE_SERVICES:-tradingagents}" == *"event-plan-api"* ]]; then
      printf 'container-1\ncontainer-2\n'
    else
      printf 'container-1\n'
    fi
    ;;
  *)
    printf 'unexpected docker command: %s\n' "$*" >&2
    exit 2
    ;;
esac
""",
        0o755,
    )


@LINUX_SHELL
@pytest.mark.unit
def test_deploy_baseline_allows_only_an_empty_bootstrap(tmp_path: Path):
    deploy_dir = tmp_path / "deploy"
    fake_bin = tmp_path / "bin"
    deploy_dir.mkdir()
    _fake_docker(fake_bin)

    result = _run_baseline(deploy_dir, fake_bin)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "DEPLOY_BASE_STATUS=bootstrap\n"

    result = _run_baseline(deploy_dir, fake_bin, FAKE_CONTAINER_IDS="container-1\n")
    assert result.returncode != 0
    assert "managed project container exists but deployment state is missing" in result.stderr


@LINUX_SHELL
@pytest.mark.unit
def test_deploy_baseline_reads_matching_state_and_runtime_identity(tmp_path: Path):
    deploy_dir = tmp_path / "deploy"
    release_dir = deploy_dir / "releases" / SHA
    fake_bin = tmp_path / "bin"
    image_uri = f"registry.example/team/a-stock-rachtrader:{SHA}"
    _write(release_dir / "docker-compose.server.yml", "services: {}\n")
    _write(
        deploy_dir / ".deploy-current",
        "\n".join(
            [
                f"sha={SHA}",
                f"image_uri={image_uri}",
                f"release_dir={release_dir}",
                "deployed_at=2026-08-07T12:00:00+08:00",
                "",
            ]
        ),
        0o600,
    )
    _write(deploy_dir / ".deploy-current-sha", f"{SHA}\n", 0o600)
    _fake_docker(fake_bin)

    result = _run_baseline(
        deploy_dir,
        fake_bin,
        FAKE_CONTAINER_IDS="container-1\n",
        FAKE_CONTAINER_IMAGE=image_uri,
        FAKE_CONTAINER_SHA=SHA,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"DEPLOY_BASE_STATUS=present\nDEPLOY_BASE_SHA={SHA}\n"

    result = _run_baseline(
        deploy_dir,
        fake_bin,
        FAKE_CONTAINER_IDS="container-1\n",
        FAKE_CONTAINER_IMAGE="registry.example/team/a-stock-rachtrader:wrong",
        FAKE_CONTAINER_SHA=SHA,
    )
    assert result.returncode != 0
    assert "image identity does not match deployment state" in result.stderr


@LINUX_SHELL
@pytest.mark.unit
def test_deploy_baseline_verifies_each_service_from_release_compose(tmp_path: Path):
    deploy_dir = tmp_path / "deploy"
    release_dir = deploy_dir / "releases" / SHA
    fake_bin = tmp_path / "bin"
    image_uri = f"registry.example/team/a-stock-rachtrader:{SHA}"
    _write(release_dir / "docker-compose.server.yml", "services: {}\n")
    _write(
        deploy_dir / ".deploy-current",
        "\n".join(
            [
                f"sha={SHA}",
                f"image_uri={image_uri}",
                f"release_dir={release_dir}",
                "deployed_at=2026-08-13T12:00:00+08:00",
                "",
            ]
        ),
        0o600,
    )
    _write(deploy_dir / ".deploy-current-sha", f"{SHA}\n", 0o600)
    _fake_docker(fake_bin)

    result = _run_baseline(
        deploy_dir,
        fake_bin,
        FAKE_SERVICES="tradingagents\nevent-plan-api",
        FAKE_CONTAINER_IDS="container-1\ncontainer-2\n",
        FAKE_TRADINGAGENTS_CONTAINER_IDS="container-1\n",
        FAKE_EVENT_PLAN_API_CONTAINER_IDS="container-2\n",
        FAKE_CONTAINER_IMAGE=image_uri,
        FAKE_CONTAINER_SHA=SHA,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"DEPLOY_BASE_STATUS=present\nDEPLOY_BASE_SHA={SHA}\n"

    missing_api = _run_baseline(
        deploy_dir,
        fake_bin,
        FAKE_SERVICES="tradingagents\nevent-plan-api",
        FAKE_CONTAINER_IDS="container-1\n",
        FAKE_TRADINGAGENTS_CONTAINER_IDS="container-1\n",
        FAKE_CONTAINER_IMAGE=image_uri,
        FAKE_CONTAINER_SHA=SHA,
    )
    assert missing_api.returncode != 0
    assert "container count does not match" in missing_api.stderr


@LINUX_SHELL
@pytest.mark.unit
def test_deploy_baseline_rejects_partial_or_mismatched_state(tmp_path: Path):
    deploy_dir = tmp_path / "deploy"
    fake_bin = tmp_path / "bin"
    deploy_dir.mkdir()
    _write(deploy_dir / ".deploy-current-sha", f"{SHA}\n", 0o600)
    _fake_docker(fake_bin)

    result = _run_baseline(deploy_dir, fake_bin)
    assert result.returncode != 0
    assert "must either both exist or both be absent" in result.stderr


@LINUX_SHELL
@pytest.mark.unit
def test_deploy_baseline_times_out_behind_active_deployment(tmp_path: Path):
    deploy_dir = tmp_path / "deploy"
    run_dir = deploy_dir / "run"
    fake_bin = tmp_path / "bin"
    ready_file = tmp_path / "lock-ready"
    run_dir.mkdir(parents=True)
    _fake_docker(fake_bin)
    holder = subprocess.Popen(
        [
            BASH,
            "-c",
            'exec 9>"$1"; flock 9; : >"$2"; sleep 10',
            "baseline-lock-holder",
            str(run_dir / "deploy-global.lock"),
            str(ready_file),
        ],
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 3
        while not ready_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready_file.exists()
        result = _run_baseline(
            deploy_dir,
            fake_bin,
            DEPLOY_LOCK_TIMEOUT_SECONDS="1",
        )
        assert result.returncode != 0
        assert "timed out waiting for the server deployment lock" in result.stderr
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(holder.pid, signal.SIGTERM)
        holder.wait(timeout=3)


@LINUX_SHELL
@pytest.mark.unit
def test_launcher_detaches_and_returns_after_startup_window(tmp_path: Path):
    deploy_dir = tmp_path / "deploy"
    release_dir = deploy_dir / "releases" / SHA
    _write(release_dir / "docker-compose.server.yml", "services: {}\n")
    _write(
        release_dir / "scripts/deploy-acr-background.sh",
        "#!/usr/bin/env bash\nsleep 20\n",
        0o755,
    )
    env = os.environ | {
        "DEPLOY_DIR": str(deploy_dir),
        "GIT_SHA": SHA,
        "DEPLOY_RUN_ID": "100",
        "DEPLOY_RUN_ATTEMPT": "1",
        "IMAGE_URI": f"registry.example/team/a-stock-rachtrader:{SHA}",
        "DEPLOY_STARTUP_WAIT_SECONDS": "1",
    }

    started = time.monotonic()
    result = subprocess.run(
        [BASH, str(ROOT / "scripts/launch-acr-deploy.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=8,
    )
    elapsed = time.monotonic() - started
    assert result.returncode == 0, result.stderr
    assert elapsed < 5
    assert "ACR deploy launched" in result.stdout

    pid = int((deploy_dir / "run/deploy-results/acr/100.1.pid").read_text().strip())
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGTERM)


@LINUX_SHELL
@pytest.mark.unit
@pytest.mark.parametrize("status", ["success", "success_superseded", "superseded"])
def test_result_waiter_accepts_only_allowed_non_failure_terminal_statuses(
    tmp_path: Path,
    status: str,
):
    deploy_dir = tmp_path / "deploy"
    _write_wait_result(deploy_dir, status)

    result = _run_result_waiter(deploy_dir)

    assert result.returncode == 0, result.stderr
    assert f"status={status}" in result.stdout
    assert "sha=" + SHA in result.stdout


@LINUX_SHELL
@pytest.mark.unit
@pytest.mark.parametrize(
    "status",
    ["failed", "rolled_back", "rollback_failed", "launch_failed"],
)
def test_result_waiter_fails_on_server_failure_terminal_statuses(
    tmp_path: Path,
    status: str,
):
    deploy_dir = tmp_path / "deploy"
    _write(deploy_dir / ".deploy-last-result.acr", "status=success\n", 0o600)
    _write_wait_result(deploy_dir, status, message="safe server failure")

    result = _run_result_waiter(deploy_dir)

    assert result.returncode != 0
    assert f"status={status}" in result.stderr
    assert "safe\\ server\\ failure" in result.stderr


@LINUX_SHELL
@pytest.mark.unit
@pytest.mark.parametrize("invalid_kind", ["identity", "permissions", "symlink"])
def test_result_waiter_rejects_untrusted_or_mismatched_exact_result(
    tmp_path: Path,
    invalid_kind: str,
):
    deploy_dir = tmp_path / "deploy"
    if invalid_kind == "identity":
        _write_wait_result(
            deploy_dir,
            "success",
            sha=NEWER_SHA,
            image_uri=f"registry.example/team/a-stock-rachtrader:{NEWER_SHA}",
        )
    elif invalid_kind == "permissions":
        _write_wait_result(deploy_dir, "success", mode=0o644)
    else:
        target = tmp_path / "foreign-result"
        _write(target, "status=success\n", 0o600)
        result_file = deploy_dir / "run/deploy-results/acr/100.1.result"
        result_file.parent.mkdir(parents=True)
        result_file.symlink_to(target)

    result = _run_result_waiter(deploy_dir)

    assert result.returncode != 0
    assert "deployment result" in result.stderr


@LINUX_SHELL
@pytest.mark.unit
def test_result_waiter_times_out_on_non_terminal_exact_result(tmp_path: Path):
    deploy_dir = tmp_path / "deploy"
    _write_wait_result(deploy_dir, "running")

    result = _run_result_waiter(deploy_dir)

    assert result.returncode != 0
    assert "last_status=running" in result.stderr


@LINUX_SHELL
@pytest.mark.unit
def test_superseded_background_cannot_overwrite_latest_result(tmp_path: Path):
    deploy_dir = tmp_path / "deploy"
    release_dir = deploy_dir / "releases" / SHA
    result_dir = deploy_dir / "run/deploy-results/acr"
    fake_bin = tmp_path / "bin"
    _write(release_dir / "docker-compose.server.yml", "services: {}\n")
    _write(deploy_dir / ".env", "OPENROUTER_API_KEY=test\n", 0o600)
    _write(
        deploy_dir / ".event-plan-api.env",
        f"TRADINGAGENTS_API_BEARER_TOKEN={'t' * 32}\n",
        0o600,
    )
    _write(
        deploy_dir / ".deploy.env",
        "ACR_PULL_USER=test\nACR_PULL_PASSWORD=test\n",
        0o600,
    )
    agent_runtime_env = tmp_path / "agent-runtime.env"
    _write(
        agent_runtime_env,
        "AGENT_RUNTIME_BASE_URL=http://agent-runtime.test:18001\n"
        "INTERNAL_AGENT_TOKEN=test-token\n",
        0o600,
    )
    _write(
        deploy_dir / "run/deploy-acr.latest-request",
        "\n".join(
            [
                "request_id=101.1",
                "run_id=101",
                "run_attempt=1",
                f"sha={NEWER_SHA}",
                f"image_uri=registry.example/team/a-stock-rachtrader:{NEWER_SHA}",
                "",
            ]
        ),
        0o600,
    )
    latest_result = deploy_dir / ".deploy-last-result.acr"
    _write(latest_result, "status=queued\nrequest_id=101.1\n", 0o600)
    _write(fake_bin / "docker", "#!/usr/bin/env bash\nexit 0\n", 0o755)
    env = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DEPLOY_DIR": str(deploy_dir),
        "RELEASE_DIR": str(release_dir),
        "GIT_SHA": SHA,
        "IMAGE_URI": f"registry.example/team/a-stock-rachtrader:{SHA}",
        "DEPLOY_REQUEST_ID": "100.1",
        "AGENT_RUNTIME_ENV_FILE": str(agent_runtime_env),
    }

    result = subprocess.run(
        [BASH, str(ROOT / "scripts/deploy-acr-background.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert (result_dir / "100.1.result").read_text().startswith("status=superseded\n")
    assert latest_result.read_text() == "status=queued\nrequest_id=101.1\n"


@LINUX_SHELL
@pytest.mark.unit
def test_background_commits_success_before_best_effort_status_output(tmp_path: Path):
    deploy_dir, _, fake_bin, _, env = _prepare_background(tmp_path)
    _fake_background_docker(fake_bin)

    result = subprocess.run(
        [BASH, str(ROOT / "scripts/deploy-acr-background.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert (deploy_dir / ".deploy-current-sha").read_text().strip() == SHA
    assert (deploy_dir / ".deploy-last-result.acr").read_text().startswith("status=success\n")
    assert not (deploy_dir / "down-called").exists()
    assert not list((deploy_dir / "run").glob("docker-auth.*"))


@LINUX_SHELL
@pytest.mark.unit
def test_background_verifies_cli_and_api_services_as_one_release(tmp_path: Path):
    deploy_dir, _, fake_bin, _, env = _prepare_background(tmp_path)
    _fake_background_docker(fake_bin)
    env["FAKE_SERVICES"] = "tradingagents\nevent-plan-api"

    result = subprocess.run(
        [BASH, str(ROOT / "scripts/deploy-acr-background.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert (deploy_dir / ".deploy-last-result.acr").read_text().startswith("status=success\n")


@LINUX_SHELL
@pytest.mark.unit
def test_background_smokes_dynamic_agent_config_and_tunnel_for_new_release(tmp_path: Path):
    deploy_dir, release_dir, fake_bin, _, env = _prepare_background(tmp_path)
    compose_log = tmp_path / "compose.log"
    _write(
        release_dir / "docker-compose.server.yml",
        "# AGENT_RUNTIME_ENV_FILE\nservices: {}\n",
    )
    _fake_background_docker(fake_bin)
    env["FAKE_COMPOSE_LOG"] = str(compose_log)

    result = subprocess.run(
        [BASH, str(ROOT / "scripts/deploy-acr-background.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    commands = compose_log.read_text()
    assert f"agent_runtime_env={env['AGENT_RUNTIME_ENV_FILE']}" in commands
    assert "DynamicAgentApiSettings.from_env()" in commands
    assert "AGENT_RUNTIME_BASE_URL" in commands
    assert "OPENROUTER_API_KEY" in commands
    assert "tradingagents-dynamic-agent-example --help" in commands


@LINUX_SHELL
@pytest.mark.unit
def test_background_retries_transient_event_plan_readiness_with_stage_receipts(
    tmp_path: Path,
):
    deploy_dir, _, fake_bin, _, env = _prepare_background(tmp_path)
    attempts_file = tmp_path / "event-plan-attempts"
    compose_log = tmp_path / "compose.log"
    _fake_background_docker(fake_bin)
    env.update(
        {
            "FAKE_SERVICES": "tradingagents\nevent-plan-api",
            "FAKE_SMOKE_FAIL_MATCH": "/health/ready",
            "FAKE_SMOKE_FAIL_COUNT": "2",
            "FAKE_SMOKE_ATTEMPTS_FILE": str(attempts_file),
            "FAKE_COMPOSE_LOG": str(compose_log),
            "SMOKE_HEALTH_MAX_ATTEMPTS": "3",
            "SMOKE_HEALTH_INTERVAL_SECONDS": "1",
        }
    )

    result = subprocess.run(
        [BASH, str(ROOT / "scripts/deploy-acr-background.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert attempts_file.read_text().strip() == "3"
    assert "stage=event-plan-readiness attempt=1/3 status=failed" in result.stderr
    assert "stage=event-plan-readiness attempt=2/3 status=failed" in result.stderr
    assert "stage=event-plan-readiness attempt=3/3 status=passed" in result.stdout
    commands = compose_log.read_text()
    assert commands.count("import tradingagents.api.app") == 1
    assert commands.count("/health/ready") == 3
    assert (deploy_dir / ".deploy-last-result.acr").read_text().startswith("status=success\n")


@LINUX_SHELL
@pytest.mark.unit
def test_background_reports_terminal_stage_after_bounded_smoke_retries(tmp_path: Path):
    deploy_dir, _, fake_bin, _, env = _prepare_background(tmp_path)
    attempts_file = tmp_path / "event-plan-attempts"
    _fake_background_docker(fake_bin)
    env.update(
        {
            "FAKE_SERVICES": "tradingagents\nevent-plan-api",
            "FAKE_SMOKE_FAIL_MATCH": "/health/ready",
            "FAKE_SMOKE_FAIL_COUNT": "3",
            "FAKE_SMOKE_ATTEMPTS_FILE": str(attempts_file),
            "SMOKE_HEALTH_MAX_ATTEMPTS": "3",
            "SMOKE_HEALTH_INTERVAL_SECONDS": "1",
        }
    )

    result = subprocess.run(
        [BASH, str(ROOT / "scripts/deploy-acr-background.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )

    assert result.returncode != 0
    assert attempts_file.read_text().strip() == "3"
    result_text = (deploy_dir / ".deploy-last-result.acr").read_text()
    assert result_text.startswith("status=failed\n")
    assert "message=service smoke failed at stage=event-plan-readiness\n" in result_text
    assert (deploy_dir / "down-called").exists()


@LINUX_SHELL
@pytest.mark.unit
def test_background_does_not_retry_deterministic_smoke_failures(tmp_path: Path):
    deploy_dir, _, fake_bin, _, env = _prepare_background(tmp_path)
    attempts_file = tmp_path / "cli-import-attempts"
    _fake_background_docker(fake_bin)
    env.update(
        {
            "FAKE_SMOKE_FAIL_MATCH": "import tradingagents; import cli.main",
            "FAKE_SMOKE_FAIL_COUNT": "3",
            "FAKE_SMOKE_ATTEMPTS_FILE": str(attempts_file),
            "SMOKE_HEALTH_MAX_ATTEMPTS": "3",
            "SMOKE_HEALTH_INTERVAL_SECONDS": "1",
        }
    )

    result = subprocess.run(
        [BASH, str(ROOT / "scripts/deploy-acr-background.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )

    assert result.returncode != 0
    assert attempts_file.read_text().strip() == "1"
    result_text = (deploy_dir / ".deploy-last-result.acr").read_text()
    assert "message=service smoke failed at stage=cli-import\n" in result_text


@LINUX_SHELL
@pytest.mark.unit
def test_background_requires_dynamic_agent_environment_before_mutation(tmp_path: Path):
    deploy_dir, _, fake_bin, _, env = _prepare_background(tmp_path)
    Path(env["AGENT_RUNTIME_ENV_FILE"]).unlink()
    _fake_background_docker(fake_bin)

    result = subprocess.run(
        [BASH, str(ROOT / "scripts/deploy-acr-background.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert result.returncode != 0
    assert not (deploy_dir / "current-image").exists()


@LINUX_SHELL
@pytest.mark.unit
def test_background_controlled_failure_restores_verified_previous_release(tmp_path: Path):
    previous_sha = "c" * 40
    deploy_dir, _, fake_bin, _, env = _prepare_background(tmp_path)
    previous_release = deploy_dir / "releases" / previous_sha
    previous_image = f"registry.example/team/a-stock-rachtrader:{previous_sha}"
    _write(previous_release / "docker-compose.server.yml", "services: {}\n")
    _write(
        deploy_dir / ".deploy-current",
        "\n".join(
            [
                f"sha={previous_sha}",
                f"image_uri={previous_image}",
                f"release_dir={previous_release}",
                "deployed_at=2026-08-07T12:00:00+08:00",
                "",
            ]
        ),
        0o600,
    )
    _write(deploy_dir / ".deploy-current-sha", f"{previous_sha}\n", 0o600)
    _fake_background_docker(fake_bin)
    env["DEPLOY_TEST_FORCE_FAILURE_AFTER_UP"] = "1"

    result = subprocess.run(
        [BASH, str(ROOT / "scripts/deploy-acr-background.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )

    assert result.returncode != 0
    assert (deploy_dir / ".deploy-last-result.acr").read_text().startswith("status=rolled_back\n")
    assert (deploy_dir / "current-image").read_text().strip() == previous_image
    assert (deploy_dir / "current-sha").read_text().strip() == previous_sha
    assert (deploy_dir / ".deploy-current-sha").read_text().strip() == previous_sha
    assert not (deploy_dir / "down-called").exists()


@LINUX_SHELL
@pytest.mark.unit
def test_background_reports_primary_and_rollback_smoke_failure_stage(tmp_path: Path):
    previous_sha = "c" * 40
    deploy_dir, _, fake_bin, _, env = _prepare_background(tmp_path)
    previous_release = deploy_dir / "releases" / previous_sha
    previous_image = f"registry.example/team/a-stock-rachtrader:{previous_sha}"
    _write(previous_release / "docker-compose.server.yml", "services: {}\n")
    _write(
        deploy_dir / ".deploy-current",
        "\n".join(
            [
                f"sha={previous_sha}",
                f"image_uri={previous_image}",
                f"release_dir={previous_release}",
                "deployed_at=2026-08-07T12:00:00+08:00",
                "",
            ]
        ),
        0o600,
    )
    _write(deploy_dir / ".deploy-current-sha", f"{previous_sha}\n", 0o600)
    attempts_file = tmp_path / "rollback-cli-import-attempts"
    _fake_background_docker(fake_bin)
    env.update(
        {
            "DEPLOY_TEST_FORCE_FAILURE_AFTER_UP": "1",
            "FAKE_SMOKE_FAIL_MATCH": "import tradingagents; import cli.main",
            "FAKE_SMOKE_FAIL_COUNT": "1",
            "FAKE_SMOKE_ATTEMPTS_FILE": str(attempts_file),
        }
    )

    result = subprocess.run(
        [BASH, str(ROOT / "scripts/deploy-acr-background.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )

    assert result.returncode != 0
    assert attempts_file.read_text().strip() == "1"
    result_text = (deploy_dir / ".deploy-last-result.acr").read_text()
    assert result_text.startswith("status=rollback_failed\n")
    assert (
        "message=rollback failure: rollback smoke failed at stage=cli-import; "
        "primary failure: controlled failure injected after compose up\n" in result_text
    )


@LINUX_SHELL
@pytest.mark.unit
def test_background_rejects_unknown_deploy_env_keys(tmp_path: Path):
    deploy_dir, _, fake_bin, _, env = _prepare_background(
        tmp_path,
        "ACR_PULL_USER=test\nACR_PULL_PASSWORD=test\nDEPLOY_DIR=/tmp/override\n",
    )
    _fake_background_docker(fake_bin)

    result = subprocess.run(
        [BASH, str(ROOT / "scripts/deploy-acr-background.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert result.returncode != 0
    assert "unsupported key in .deploy.env: DEPLOY_DIR" in result.stderr
    assert (deploy_dir / "run/deploy-results/acr/100.1.result").exists()


@LINUX_SHELL
@pytest.mark.unit
def test_background_notifies_when_previous_state_is_invalid(tmp_path: Path):
    webhook = "https://example.invalid/deploy-hook"
    deploy_dir, _, fake_bin, _, env = _prepare_background(
        tmp_path,
        f"ACR_PULL_USER=test\nACR_PULL_PASSWORD=test\nDEPLOY_FEISHU_WEBHOOK_URL={webhook}\n",
    )
    curl_log = tmp_path / "curl.log"
    _fake_background_docker(fake_bin)
    _write(
        fake_bin / "curl",
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >>"$FAKE_CURL_LOG"\n',
        0o755,
    )
    _write(deploy_dir / ".deploy-current", "sha=invalid\n", 0o600)
    env["FAKE_CURL_LOG"] = str(curl_log)

    result = subprocess.run(
        [BASH, str(ROOT / "scripts/deploy-acr-background.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert result.returncode != 0
    assert (deploy_dir / ".deploy-last-result.acr").read_text().startswith("status=failed\n")
    assert "invalid previous deployment state" in curl_log.read_text()


@LINUX_SHELL
@pytest.mark.unit
def test_background_notifies_when_deployment_lock_times_out(tmp_path: Path):
    webhook = "https://example.invalid/deploy-hook"
    deploy_dir, _, fake_bin, _, env = _prepare_background(
        tmp_path,
        f"ACR_PULL_USER=test\nACR_PULL_PASSWORD=test\nDEPLOY_FEISHU_WEBHOOK_URL={webhook}\n",
    )
    curl_log = tmp_path / "curl.log"
    ready_file = tmp_path / "lock-ready"
    _fake_background_docker(fake_bin)
    _write(
        fake_bin / "curl",
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >>"$FAKE_CURL_LOG"\n',
        0o755,
    )
    env["DEPLOY_LOCK_TIMEOUT_SECONDS"] = "1"
    env["FAKE_CURL_LOG"] = str(curl_log)
    holder = subprocess.Popen(
        [
            BASH,
            "-c",
            'exec 9>"$1"; flock 9; : >"$2"; sleep 10',
            "background-lock-holder",
            str(deploy_dir / "run/deploy-global.lock"),
            str(ready_file),
        ],
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 3
        while not ready_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready_file.exists()
        result = subprocess.run(
            [BASH, str(ROOT / "scripts/deploy-acr-background.sh")],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=8,
        )
        assert result.returncode != 0
        assert "deployment lock timed out" in curl_log.read_text()
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(holder.pid, signal.SIGTERM)
        holder.wait(timeout=3)


@LINUX_SHELL
@pytest.mark.unit
def test_background_rejects_release_path_outside_sha_directory(tmp_path: Path):
    deploy_dir = tmp_path / "deploy"
    wrong_release = deploy_dir / "releases" / NEWER_SHA
    _write(wrong_release / "docker-compose.server.yml", "services: {}\n")
    env = os.environ | {
        "DEPLOY_DIR": str(deploy_dir),
        "RELEASE_DIR": str(wrong_release),
        "GIT_SHA": SHA,
        "IMAGE_URI": f"registry.example/team/a-stock-rachtrader:{SHA}",
        "DEPLOY_REQUEST_ID": "100.1",
    }
    result = subprocess.run(
        [BASH, str(ROOT / "scripts/deploy-acr-background.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode != 0
