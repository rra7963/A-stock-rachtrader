from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def require_fragments(
    root: Path,
    path: Path,
    fragments: tuple[str, ...],
    errors: list[str],
) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"cannot read {path.relative_to(root)}: {exc}")
        return ""
    for fragment in fragments:
        if fragment not in text:
            errors.append(f"{path.relative_to(root)} is missing required fragment: {fragment}")
    return text


def validate(root: Path = ROOT) -> list[str]:
    root = root.resolve()
    errors: list[str] = []
    required_files = (
        root / "Dockerfile",
        root / ".dockerignore",
        root / ".deploy.env.example",
        root / ".event-plan-api.env.example",
        root / "docker-compose.server.yml",
        root / ".github/workflows/ci.yml",
        root / ".github/workflows/deploy-acr.yml",
        root / "README.md",
        root / "scripts/read-deploy-baseline.sh",
        root / "scripts/launch-acr-deploy.sh",
        root / "scripts/deploy-acr-background.sh",
        root / "scripts/wait-acr-deploy-result.sh",
        root / "docs/docker-deployment.md",
        root / "docs/server-acr-deployment-plan.md",
        root / "docs/dynamic-agent-api.md",
        root / "docs/event-trade-plan-api.md",
        root / "docs/event-portfolio-plan-api.md",
    )
    for path in required_files:
        if not path.is_file():
            errors.append(f"missing deployment file: {path.relative_to(root)}")

    dockerfile = require_fragments(
        root,
        root / "Dockerfile",
        ("FROM python:3.12-slim", "COPY --from=builder /opt/venv /opt/venv", "USER appuser"),
        errors,
    )
    if "COPY --from=builder /build" in dockerfile:
        errors.append("production image must not copy the source/build tree into the runtime stage")
    if dockerfile and dockerfile.rfind("USER appuser") < dockerfile.rfind("COPY --from=builder"):
        errors.append("Dockerfile must finish copying runtime artifacts before dropping privileges")

    dockerignore = require_fragments(
        root,
        root / ".dockerignore",
        (".env*", ".deploy.env", ".event-plan-api.env", ".git", "release-bundle"),
        errors,
    )
    if "!.env\n" in dockerignore:
        errors.append(".dockerignore must never re-include a populated .env")

    server_compose = require_fragments(
        root,
        root / "docker-compose.server.yml",
        (
            "TRADINGAGENTS_IMAGE:?TRADINGAGENTS_IMAGE is required",
            "TRADINGAGENTS_ENV_FILE:?TRADINGAGENTS_ENV_FILE is required",
            "TRADINGAGENTS_API_ENV_FILE:?TRADINGAGENTS_API_ENV_FILE is required",
            "AGENT_RUNTIME_ENV_FILE:?AGENT_RUNTIME_ENV_FILE is required",
            "GIT_SHA:?GIT_SHA is required",
            "pull_policy: never",
            "restart: unless-stopped",
            "healthcheck:",
            "no-new-privileges:true",
            "cap_drop:",
            "name: a-stock-rachtrader-data",
            "name: a-stock-rachtrader-reports",
            "event-plan-api:",
            "entrypoint: [\"tradingagents-api\"]",
            "name: a-stock-rachtrader-api-data",
            "trading-agents-event-plan-api",
            "external: true",
        ),
        errors,
    )
    if "build:" in server_compose:
        errors.append("server compose must use a prebuilt image and must not contain build")
    if ":latest" in server_compose:
        errors.append("server compose must not use a mutable latest tag")
    if re.search(r"^\s*ports:\s*$", server_compose, re.MULTILINE):
        errors.append("server compose must not publish the event plan API on a host port")

    require_fragments(
        root,
        root / ".event-plan-api.env.example",
        (
            "TRADINGAGENTS_API_BEARER_TOKEN=",
            "TRADINGAGENTS_PORTFOLIO_ANALYSIS_CONCURRENCY=4",
        ),
        errors,
    )

    workflow = require_fragments(
        root,
        root / ".github/workflows/deploy-acr.yml",
        (
            "workflow_run:",
            "workflows: [CI]",
            "github.ref == 'refs/heads/main'",
            "existing_sha:\n        description: Existing deployed SHA to redeploy/roll back without rebuilding\n        required: true",
            "github.event.workflow_run.conclusion == 'success'",
            "github.event.workflow_run.event == 'push'",
            "github.event.workflow_run.head_repository.full_name == github.repository",
            "DEPLOY_SHA:",
            "SOURCE_SHA:",
            "docker buildx build",
            "--platform linux/amd64",
            "--push",
            "docker buildx imagetools inspect",
            "Verify the immutable image revision label",
            "Immutable image revision label does not match DEPLOY_SHA",
            "Preserve an existing immutable SHA image",
            "steps.immutable-image.outputs.exists != 'true'",
            "a-stock-rachtrader:${{ env.DEPLOY_SHA }}",
            "releases/${{ env.DEPLOY_SHA }}",
            "cp docker-compose.server.yml release-bundle/",
            "cp scripts/deploy-acr-background.sh release-bundle/scripts/",
            "Read and validate current server deployment state before build",
            "command_timeout: 31m",
            "script_path: scripts/read-deploy-baseline.sh",
            "script_path: scripts/launch-acr-deploy.sh",
            "command_timeout: 2m",
            "Wait for the exact server deployment result",
            "script_path: scripts/wait-acr-deploy-result.sh",
            "DEPLOY_RESULT_TIMEOUT_SECONDS: \"2700\"",
            "command_timeout: 46m",
        ),
        errors,
    )
    for forbidden in (
        "git pull",
        "cp -r",
        "cp . release-bundle",
        "a-stock-rachtrader:latest",
        "--cache-from type=gha",
        "--cache-to type=gha",
    ):
        if forbidden in workflow:
            errors.append(
                f"deployment workflow contains forbidden mutable/source behavior: {forbidden}"
            )
    for match in re.finditer(r"^\s*uses:\s*([^\s#]+)@([^\s#]+)", workflow, re.MULTILINE):
        action, ref = match.groups()
        if not FULL_SHA.fullmatch(ref):
            errors.append(f"third-party action must be pinned to a full commit SHA: {action}@{ref}")

    baseline = require_fragments(
        root,
        root / "scripts/read-deploy-baseline.sh",
        (
            'flock -w "$DEPLOY_LOCK_TIMEOUT_SECONDS"',
            ".deploy-current",
            ".deploy-current-sha",
            "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME",
            "config --services",
            "expected_services",
            "AGENT_RUNTIME_ENV_FILE",
            "label=com.docker.compose.service=$service_name",
            "DEPLOY_BASE_STATUS=bootstrap",
            "DEPLOY_BASE_STATUS=present",
            "managed service image identity does not match deployment state",
        ),
        errors,
    )
    for forbidden in ("docker pull", "docker compose up", "docker rm", "docker stop"):
        if forbidden in baseline:
            errors.append(f"deployment preflight must be read-only: {forbidden}")

    launch = require_fragments(
        root,
        root / "scripts/launch-acr-deploy.sh",
        (
            "setsid env",
            '> "$log_file" 2>&1 </dev/null &',
            "DEPLOY_STARTUP_WAIT_SECONDS must be between 1 and 10",
            "request registration lock",
            "launch_failed",
        ),
        errors,
    )
    if "docker pull" in launch or "docker compose" in launch:
        errors.append(
            "launcher must return quickly and leave Docker operations to the background script"
        )

    waiter = require_fragments(
        root,
        root / "scripts/wait-acr-deploy-result.sh",
        (
            "DEPLOY_RESULT_TIMEOUT_SECONDS",
            "DEPLOY_RESULT_POLL_INTERVAL_SECONDS",
            'result_file="$DEPLOY_DIR/run/deploy-results/acr/${request_id}.result"',
            '[ ! -L "$result_file" ]',
            "invalid or mismatched deployment result",
            "success|success_superseded|superseded",
            "      failed|rolled_back|rollback_failed|launch_failed)\n",
            "timed out waiting for deployment result",
        ),
        errors,
    )
    if ".deploy-last-result.acr" in waiter:
        errors.append("result waiter must read only the exact per-request result")
    for forbidden in ("docker ", "kill ", "rm ", "curl "):
        if forbidden in waiter:
            errors.append(f"result waiter must remain read-only: {forbidden.strip()}")

    background = require_fragments(
        root,
        root / "scripts/deploy-acr-background.sh",
        (
            'flock -w "$DEPLOY_LOCK_TIMEOUT_SECONDS"',
            "load_deploy_env",
            "unsupported key in .deploy.env",
            "docker login",
            "docker logout",
            "docker pull",
            "up -d --no-build --remove-orphans",
            "tradingagents --help",
            "run_service_smokes()",
            "run_smoke_stage()",
            '"agent-runtime-health" "$SMOKE_HEALTH_MAX_ATTEMPTS"',
            '"event-plan-import" 1',
            '"event-plan-readiness" "$SMOKE_HEALTH_MAX_ATTEMPTS"',
            "service smoke failed at stage=",
            "event-plan-api",
            "/health/ready",
            "TRADINGAGENTS_API_ENV_FILE",
            "AGENT_RUNTIME_ENV_FILE",
            "DynamicAgentApiSettings.from_env()",
            "OPENROUTER_API_KEY",
            '"/health"',
            "config --services",
            "container_is_verified",
            "rollback",
            "latest_request_matches",
            "cleanup_old_releases",
            "cleanup_old_images",
            "notify_feishu",
            "DEPLOY_TEST_FORCE_FAILURE_AFTER_UP",
            "must never replace",
            "the global result",
            'notify_feishu "failed" "deployment lock timed out"',
            'notify_feishu "failed" "invalid previous deployment state"',
            "persist_current_state\nMUTATION_STARTED=0\ntrap - ERR",
            'compose "$IMAGE_URI" "$GIT_SHA" "$COMPOSE_FILE" ps || true',
        ),
        errors,
    )
    for forbidden in (
        "git pull",
        "docker build",
        "docker image prune -a",
        "docker system prune",
        "docker volume prune",
        '. "$DEPLOY_ENV_FILE"',
        'source "$DEPLOY_ENV_FILE"',
    ):
        if forbidden in background:
            errors.append(f"background deployment contains forbidden server behavior: {forbidden}")

    ci = require_fragments(
        root,
        root / ".github/workflows/ci.yml",
        (
            "pull_request:",
            "deployment contract and image build",
            "uv run --frozen pytest -q tests/test_deploy_contract.py tests/test_deploy_scripts.py",
            "shellcheck scripts/read-deploy-baseline.sh",
            "scripts/wait-acr-deploy-result.sh",
            "docker run --rm a-stock-rachtrader:ci --help",
            "tradingagents-dynamic-agent-example",
            "TRADINGAGENTS_API_ENV_FILE",
            "AGENT_RUNTIME_ENV_FILE",
            "import tradingagents.api.app",
            "import tradingagents.integrations",
            "name: CI Gate",
        ),
        errors,
    )
    if "continue-on-error: true" in ci:
        errors.append("required CI jobs must fail closed")

    operator_command_fragments = (
        "deploy_dir=/opt/a-stock-rachtrader",
        'image_uri="$(sed -n \'s/^image_uri=//p\' "$deploy_dir/.deploy-current")"',
        'TRADINGAGENTS_IMAGE="$image_uri"',
        'TRADINGAGENTS_ENV_FILE="$deploy_dir/.env"',
        'TRADINGAGENTS_API_ENV_FILE="$deploy_dir/.event-plan-api.env"',
        'AGENT_RUNTIME_ENV_FILE="/opt/etf-agent-runtime-tunnel/secrets/internal-agent-caller.env"',
        'GIT_SHA="$sha"',
        '--project-directory "$deploy_dir"',
    )
    require_fragments(
        root,
        root / "docs/docker-deployment.md",
        operator_command_fragments + ("--ref main",),
        errors,
    )
    server_deployment_plan = require_fragments(
        root,
        root / "docs/server-acr-deployment-plan.md",
        (
            "workflow concurrency 在正常运行期间串行化同一仓库的部署 workflow",
            "Actions 侧调度，不是服务器后台任务的租约或取消机制",
            "launcher 在约 2–10 秒的远程登记窗口后返回；GitHub workflow 随后有界等待本次精确终态。",
            "launcher 只登记并启动 detached task 后快速返回，GitHub workflow 再有界等待本次精确终态；",
            "GitHub workflow 只读本次精确结果并据终态给出绿色或红色结论",
        ),
        errors,
    )
    for obsolete in (
        "Actions 会提前结束",
        "Actions 在后台任务受理后快速结束",
        "不让 Actions 等待",
        "GitHub Actions 在约 2–10 秒的远程登记窗口后返回",
        "Actions 提前成功但服务器随后失败",
        "GitHub Actions 只登记任务并立即返回",
        "Actions 快速返回之后",
    ):
        if obsolete in server_deployment_plan:
            errors.append(
                "server deployment plan contains obsolete deployment success semantics: "
                f"{obsolete}"
            )
    require_fragments(
        root,
        root / "docs/dynamic-agent-api.md",
        (
            "from tradingagents.integrations import call_dynamic_agent",
            "AGENT_RUNTIME_BASE_URL",
            "INTERNAL_AGENT_TOKEN",
            "OPENROUTER_API_KEY",
            "caller_request_id is not an idempotency key",
            "tradingagents/examples/dynamic_agent_api.py",
        ),
        errors,
    )
    require_fragments(
        root,
        root / "docs/event-trade-plan-api.md",
        (
            "Rachel downstream execution service owns:",
            "POST /v1/event-trade-plans",
            "No host port is published",
            "never owns a broker account",
            "The same id with a different hash returns HTTP 409",
        ),
        errors,
    )
    require_fragments(
        root,
        root / "docs/event-portfolio-plan-api.md",
        (
            "POST /v1/event-portfolio-plans",
            "TRADINGAGENTS_PORTFOLIO_ANALYSIS_CONCURRENCY",
            "planner_failed_instrument_graph",
            "process-wide analysis lock",
        ),
        errors,
    )
    require_fragments(
        root,
        root / "README.md",
        operator_command_fragments,
        errors,
    )

    return errors


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("deployment contract OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
