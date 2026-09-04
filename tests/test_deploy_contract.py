from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "validate_deploy_contract",
    Path(__file__).resolve().parents[1] / "scripts/validate_deploy_contract.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
ROOT = _MODULE.ROOT
validate = _MODULE.validate


DEPLOYMENT_FILES = (
    "Dockerfile",
    ".dockerignore",
    ".deploy.env.example",
    ".event-plan-api.env.example",
    "docker-compose.server.yml",
    ".github/workflows/ci.yml",
    ".github/workflows/deploy-acr.yml",
    "README.md",
    "scripts/read-deploy-baseline.sh",
    "scripts/launch-acr-deploy.sh",
    "scripts/deploy-acr-background.sh",
    "scripts/wait-acr-deploy-result.sh",
    "docs/docker-deployment.md",
    "docs/server-acr-deployment-plan.md",
    "docs/dynamic-agent-api.md",
    "docs/event-trade-plan-api.md",
    "docs/event-portfolio-plan-api.md",
)


def _copy_contract_tree(tmp_path: Path) -> Path:
    for relative in DEPLOYMENT_FILES:
        source = ROOT / relative
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return tmp_path


def _replace(root: Path, relative: str, old: str, new: str) -> None:
    path = root / relative
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


@pytest.mark.unit
def test_repository_deployment_contract_is_valid():
    assert validate() == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("relative", "old", "new", "expected"),
    [
        (
            "docker-compose.server.yml",
            "    image: ${TRADINGAGENTS_IMAGE:?TRADINGAGENTS_IMAGE is required}",
            "    build: .",
            "server compose must use a prebuilt image",
        ),
        (
            "scripts/launch-acr-deploy.sh",
            '> "$log_file" 2>&1 </dev/null &',
            '> "$log_file" 2>&1 &',
            "missing required fragment",
        ),
        (
            ".github/workflows/deploy-acr.yml",
            "  workflow_run:",
            "  push:",
            "missing required fragment: workflow_run:",
        ),
        (
            "scripts/deploy-acr-background.sh",
            "run_service_smokes()",
            "python --version",
            "missing required fragment: run_service_smokes",
        ),
        (
            ".github/workflows/deploy-acr.yml",
            "actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5",
            "actions/checkout@v4",
            "third-party action must be pinned",
        ),
        (
            ".github/workflows/deploy-acr.yml",
            "steps.immutable-image.outputs.exists != 'true'",
            "true",
            "missing required fragment: steps.immutable-image.outputs.exists != 'true'",
        ),
        (
            ".event-plan-api.env.example",
            "TRADINGAGENTS_PORTFOLIO_ANALYSIS_CONCURRENCY=4",
            "TRADINGAGENTS_PORTFOLIO_ANALYSIS_CONCURRENCY=5",
            "missing required fragment: TRADINGAGENTS_PORTFOLIO_ANALYSIS_CONCURRENCY=4",
        ),
        (
            ".github/workflows/deploy-acr.yml",
            "script_path: scripts/read-deploy-baseline.sh",
            "script_path: scripts/launch-acr-deploy.sh",
            "missing required fragment: script_path: scripts/read-deploy-baseline.sh",
        ),
        (
            ".github/workflows/deploy-acr.yml",
            "script_path: scripts/wait-acr-deploy-result.sh",
            "script_path: scripts/launch-acr-deploy.sh",
            "missing required fragment: script_path: scripts/wait-acr-deploy-result.sh",
        ),
        (
            "scripts/wait-acr-deploy-result.sh",
            "      failed|rolled_back|rollback_failed|launch_failed)\n",
            "      failed|rollback_failed|launch_failed)\n",
            "failed|rolled_back|rollback_failed|launch_failed)",
        ),
        (
            "scripts/deploy-acr-background.sh",
            '"event-plan-readiness" "$SMOKE_HEALTH_MAX_ATTEMPTS"',
            '"event-plan-readiness" 1',
            'missing required fragment: "event-plan-readiness" "$SMOKE_HEALTH_MAX_ATTEMPTS"',
        ),
        (
            ".github/workflows/deploy-acr.yml",
            "        required: true",
            "        required: false",
            "missing required fragment: existing_sha:",
        ),
        (
            ".github/workflows/deploy-acr.yml",
            '            --label "org.opencontainers.image.revision=$DEPLOY_SHA" \\\n',
            '            --cache-to type=gha,mode=max \\\n            --label "org.opencontainers.image.revision=$DEPLOY_SHA" \\\n',
            "deployment workflow contains forbidden mutable/source behavior: --cache-to type=gha",
        ),
    ],
)
def test_contract_validator_rejects_deployment_regressions(
    tmp_path: Path,
    relative: str,
    old: str,
    new: str,
    expected: str,
):
    root = _copy_contract_tree(tmp_path)
    _replace(root, relative, old, new)
    assert any(expected in error for error in validate(root))


@pytest.mark.unit
@pytest.mark.parametrize(
    "obsolete",
    [
        "Actions 会提前结束",
        "Actions 在后台任务受理后快速结束",
        "不让 Actions 等待",
        "GitHub Actions 在约 2–10 秒的远程登记窗口后返回",
        "Actions 提前成功但服务器随后失败",
        "GitHub Actions 只登记任务并立即返回",
        "Actions 快速返回之后",
    ],
)
def test_contract_validator_rejects_obsolete_deployment_success_semantics(
    tmp_path: Path,
    obsolete: str,
):
    root = _copy_contract_tree(tmp_path)
    plan = root / "docs/server-acr-deployment-plan.md"
    plan.write_text(f"{plan.read_text(encoding='utf-8')}\n{obsolete}\n", encoding="utf-8")

    assert any(
        "server deployment plan contains obsolete deployment success semantics" in error
        and obsolete in error
        for error in validate(root)
    )
