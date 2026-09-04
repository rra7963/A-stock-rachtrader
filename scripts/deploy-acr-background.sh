#!/usr/bin/env bash
set -Eeuo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-/opt/a-stock-rachtrader}"
RELEASE_DIR="${RELEASE_DIR:?RELEASE_DIR is required}"
GIT_SHA="${GIT_SHA:?GIT_SHA is required}"
IMAGE_URI="${IMAGE_URI:?IMAGE_URI is required}"
DEPLOY_REQUEST_ID="${DEPLOY_REQUEST_ID:?DEPLOY_REQUEST_ID is required}"
TEST_FORCE_FAILURE_AFTER_UP="${DEPLOY_TEST_FORCE_FAILURE_AFTER_UP:-0}"

COMPOSE_FILE="$RELEASE_DIR/docker-compose.server.yml"
APP_ENV_FILE="$DEPLOY_DIR/.env"
API_ENV_FILE="$DEPLOY_DIR/.event-plan-api.env"
AGENT_RUNTIME_ENV_FILE="${AGENT_RUNTIME_ENV_FILE:-/opt/etf-agent-runtime-tunnel/secrets/internal-agent-caller.env}"
DEPLOY_ENV_FILE="$DEPLOY_DIR/.deploy.env"
RUN_DIR="$DEPLOY_DIR/run"
RESULT_DIR="$RUN_DIR/deploy-results/acr"
DESIRED_REQUEST_FILE="$RUN_DIR/deploy-acr.latest-request"
REQUEST_LOCK_FILE="$RUN_DIR/deploy-acr.request.lock"
RESULT_FILE="$RESULT_DIR/${DEPLOY_REQUEST_ID}.result"
LATEST_RESULT_FILE="$DEPLOY_DIR/.deploy-last-result.acr"
LOCK_FILE="$RUN_DIR/deploy-global.lock"
STATE_FILE="$DEPLOY_DIR/.deploy-current"
CURRENT_SHA_FILE="$DEPLOY_DIR/.deploy-current-sha"
PROJECT_NAME="a-stock-rachtrader"
DEPLOY_LOCK_TIMEOUT_SECONDS="${DEPLOY_LOCK_TIMEOUT_SECONDS:-1800}"
PULL_MAX_ATTEMPTS="${PULL_MAX_ATTEMPTS:-5}"
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-150}"
HEALTH_INTERVAL_SECONDS="${HEALTH_INTERVAL_SECONDS:-5}"
STABLE_HEALTH_SECONDS="${STABLE_HEALTH_SECONDS:-10}"
SMOKE_HEALTH_MAX_ATTEMPTS="${SMOKE_HEALTH_MAX_ATTEMPTS:-3}"
SMOKE_HEALTH_INTERVAL_SECONDS="${SMOKE_HEALTH_INTERVAL_SECONDS:-5}"
RELEASE_RETENTION_COUNT="${RELEASE_RETENTION_COUNT:-5}"
DEPLOY_ARTIFACT_RETENTION_DAYS="${DEPLOY_ARTIFACT_RETENTION_DAYS:-30}"
MUTATION_STARTED=0
FAILED_SMOKE_STAGE=""
SMOKE_SERVICES_OUTPUT=""
PREVIOUS_SHA=""
PREVIOUS_IMAGE=""
PREVIOUS_RELEASE=""
ACR_PULL_USER=""
ACR_PULL_PASSWORD=""
DEPLOY_FEISHU_WEBHOOK_URL=""
ACR_REGISTRY="${IMAGE_URI%%/*}"
DOCKER_CONFIG_DIR=""

sanitize_message() {
  local message="$1"
  message="${message//$'\n'/ }"
  printf '%.500s' "$message"
}

load_deploy_env() {
  local key value
  while IFS='=' read -r key value || [ -n "${key:-}" ]; do
    value="${value%$'\r'}"
    case "$key" in
      ''|'#'*) continue ;;
      ACR_PULL_USER) ACR_PULL_USER="$value" ;;
      ACR_PULL_PASSWORD) ACR_PULL_PASSWORD="$value" ;;
      DEPLOY_FEISHU_WEBHOOK_URL) DEPLOY_FEISHU_WEBHOOK_URL="$value" ;;
      *)
        printf '[deploy][error] unsupported key in .deploy.env: %s\n' "$key" >&2
        return 1
        ;;
    esac
  done < "$DEPLOY_ENV_FILE"

  [ -n "$ACR_PULL_USER" ] || {
    printf '[deploy][error] ACR_PULL_USER is required in .deploy.env\n' >&2
    return 1
  }
  [ -n "$ACR_PULL_PASSWORD" ] || {
    printf '[deploy][error] ACR_PULL_PASSWORD is required in .deploy.env\n' >&2
    return 1
  }
  export ACR_PULL_USER ACR_PULL_PASSWORD DEPLOY_FEISHU_WEBHOOK_URL
}

cleanup_docker_auth() {
  local rc=$?
  if [ -n "$DOCKER_CONFIG_DIR" ]; then
    docker logout "$ACR_REGISTRY" >/dev/null 2>&1 || true
    rm -f -- "$DOCKER_CONFIG_DIR/config.json"
    rmdir -- "$DOCKER_CONFIG_DIR" 2>/dev/null || true
  fi
  return "$rc"
}
trap cleanup_docker_auth EXIT

latest_request_matches() {
  local key value request_id="" sha="" image_uri=""
  exec 8>"$REQUEST_LOCK_FILE"
  flock -w 30 8 || return 1
  if [ -f "$DESIRED_REQUEST_FILE" ]; then
    while IFS='=' read -r key value || [ -n "${key:-}" ]; do
      case "$key" in
        request_id) request_id="$value" ;;
        sha) sha="$value" ;;
        image_uri) image_uri="$value" ;;
      esac
    done < "$DESIRED_REQUEST_FILE"
  fi
  flock -u 8
  exec 8>&-
  [ "$request_id" = "$DEPLOY_REQUEST_ID" ] \
    && [ "$sha" = "$GIT_SHA" ] \
    && [ "$image_uri" = "$IMAGE_URI" ]
}

write_result() {
  local status="$1"
  local message="$2"
  local tmp="${RESULT_FILE}.tmp.$$"
  {
    printf 'status=%s\n' "$status"
    printf 'pid=%s\n' "$$"
    printf 'request_id=%s\n' "$DEPLOY_REQUEST_ID"
    printf 'sha=%s\n' "$GIT_SHA"
    printf 'image_uri=%s\n' "$IMAGE_URI"
    printf 'previous_sha=%s\n' "$PREVIOUS_SHA"
    printf 'release_dir=%s\n' "$RELEASE_DIR"
    printf 'time=%s\n' "$(date -Is)"
    printf 'message=%s\n' "$(sanitize_message "$message")"
  } > "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$RESULT_FILE"

  # A superseded task keeps its per-request audit record but must never replace
  # the global result that belongs to the newer desired request.
  if latest_request_matches; then
    tmp="${LATEST_RESULT_FILE}.tmp.$$"
    cp "$RESULT_FILE" "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$LATEST_RESULT_FILE"
  fi
}

notify_feishu() {
  local status="$1"
  local message="${2:-}"
  local webhook="${DEPLOY_FEISHU_WEBHOOK_URL:-}"
  local text payload

  if [ "${DEPLOY_FEISHU_NOTIFY:-1}" = "0" ] || [ -z "$webhook" ]; then
    return 0
  fi
  if ! command -v curl >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
    printf '[deploy][warn] skip Feishu notify: curl or python3 not found\n' >&2
    return 0
  fi

  text="TradingAgents ACR deploy ${status}
request_id=${DEPLOY_REQUEST_ID}
sha=${GIT_SHA}
previous_sha=${PREVIOUS_SHA}
image=${IMAGE_URI}
message=$(sanitize_message "$message")"
  payload="$(
    TEXT="$text" python3 - <<'PY'
import json
import os

print(json.dumps({"msg_type": "text", "content": {"text": os.environ["TEXT"]}}))
PY
  )"

  if ! curl -fsS --max-time 10 \
    -H 'Content-Type: application/json' \
    -d "$payload" \
    "$webhook" >/dev/null; then
    printf '[deploy][warn] Feishu notify failed status=%s\n' "$status" >&2
  fi
}

compose() {
  TRADINGAGENTS_IMAGE="$1" \
  TRADINGAGENTS_ENV_FILE="$APP_ENV_FILE" \
  TRADINGAGENTS_API_ENV_FILE="$API_ENV_FILE" \
  AGENT_RUNTIME_ENV_FILE="$AGENT_RUNTIME_ENV_FILE" \
  GIT_SHA="$2" \
    docker compose \
      -p "$PROJECT_NAME" \
      --project-directory "$DEPLOY_DIR" \
      -f "$3" \
      "${@:4}"
}

compose_services() {
  compose "$1" "$2" "$3" config --services
}

pull_image() {
  local image="$1" attempt delay
  for ((attempt = 1; attempt <= PULL_MAX_ATTEMPTS; attempt++)); do
    if docker pull "$image"; then
      return 0
    fi
    if [ "$attempt" -lt "$PULL_MAX_ATTEMPTS" ]; then
      delay=$((attempt * 15))
      printf '[deploy] image pull attempt %s/%s failed; retry in %ss\n' \
        "$attempt" "$PULL_MAX_ATTEMPTS" "$delay" >&2
      sleep "$delay"
    fi
  done
  return 1
}

container_is_verified() {
  local image="$1" sha="$2" compose_file="$3"
  local services_output service container_output container_id
  local running health observed_image observed_sha project_output
  local expected_count=0 project_count=0

  services_output="$(compose_services "$image" "$sha" "$compose_file" 2>/dev/null)" \
    || return 1
  [ -n "$services_output" ] || return 1

  while IFS= read -r service; do
    [ -n "$service" ] || continue
    expected_count=$((expected_count + 1))
    container_output="$(
      compose "$image" "$sha" "$compose_file" ps -q "$service" 2>/dev/null
    )" || return 1
    [ -n "$container_output" ] || return 1
    [ "$(printf '%s\n' "$container_output" | wc -l)" -eq 1 ] || return 1
    container_id="$container_output"
    running="$(docker inspect --format '{{.State.Running}}' "$container_id" 2>/dev/null)" \
      || return 1
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$container_id" 2>/dev/null)" \
      || return 1
    observed_image="$(docker inspect --format '{{.Config.Image}}' "$container_id" 2>/dev/null)" \
      || return 1
    observed_sha="$(docker inspect --format '{{index .Config.Labels "io.rachel.deploy.sha"}}' "$container_id" 2>/dev/null)" \
      || return 1
    [ "$running" = "true" ] \
      && [ "$health" = "healthy" ] \
      && [ "$observed_image" = "$image" ] \
      && [ "$observed_sha" = "$sha" ] \
      || return 1
  done <<< "$services_output"

  project_output="$(
    docker ps -aq --filter "label=com.docker.compose.project=$PROJECT_NAME"
  )" || return 1
  if [ -n "$project_output" ]; then
    project_count="$(printf '%s\n' "$project_output" | wc -l)"
  fi
  [ "$expected_count" -eq "$project_count" ]
}

wait_for_health() {
  local image="$1" sha="$2" compose_file="$3"
  local deadline stable_since=0 now
  deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if container_is_verified "$image" "$sha" "$compose_file"; then
      now=$SECONDS
      if [ "$stable_since" -eq 0 ]; then
        stable_since=$now
      elif [ $((now - stable_since)) -ge "$STABLE_HEALTH_SECONDS" ]; then
        return 0
      fi
    else
      stable_since=0
    fi
    sleep "$HEALTH_INTERVAL_SECONDS"
  done
  return 1
}

run_smoke_stage() {
  local stage="$1" max_attempts="$2" attempt
  shift 2
  [[ "$stage" =~ ^[a-z0-9-]+$ ]] || return 1
  for ((attempt = 1; attempt <= max_attempts; attempt++)); do
    printf '[deploy][smoke] stage=%s attempt=%s/%s status=started\n' \
      "$stage" "$attempt" "$max_attempts"
    if "$@" >/dev/null 2>&1; then
      printf '[deploy][smoke] stage=%s attempt=%s/%s status=passed\n' \
        "$stage" "$attempt" "$max_attempts"
      return 0
    fi
    printf '[deploy][smoke] stage=%s attempt=%s/%s status=failed\n' \
      "$stage" "$attempt" "$max_attempts" >&2
    if [ "$attempt" -lt "$max_attempts" ]; then
      sleep "$SMOKE_HEALTH_INTERVAL_SECONDS"
    fi
  done
  FAILED_SMOKE_STAGE="$stage"
  return 1
}

discover_smoke_services() {
  SMOKE_SERVICES_OUTPUT="$(compose_services "$1" "$2" "$3")" \
    && [ -n "$SMOKE_SERVICES_OUTPUT" ]
}

smoke_service_is_declared() {
  printf '%s\n' "$SMOKE_SERVICES_OUTPUT" | grep -Fxq "$1"
}

run_service_smokes() {
  local image="$1" sha="$2" compose_file="$3"
  FAILED_SMOKE_STAGE=""
  SMOKE_SERVICES_OUTPUT=""

  run_smoke_stage "service-discovery" 1 \
    discover_smoke_services "$image" "$sha" "$compose_file" \
    || return 1
  run_smoke_stage "tradingagents-service" 1 \
    smoke_service_is_declared tradingagents \
    || return 1
  run_smoke_stage "container-identity" 1 \
    container_is_verified "$image" "$sha" "$compose_file" \
    || return 1
  run_smoke_stage "cli-import" 1 \
    compose "$image" "$sha" "$compose_file" exec -T tradingagents \
      python -c 'import tradingagents; import cli.main' \
    || return 1
  run_smoke_stage "cli-help" 1 \
    compose "$image" "$sha" "$compose_file" exec -T tradingagents \
      tradingagents --help \
    || return 1

  if grep -Fq 'AGENT_RUNTIME_ENV_FILE' "$compose_file"; then
    run_smoke_stage "agent-runtime-config" 1 \
      compose "$image" "$sha" "$compose_file" exec -T tradingagents \
      python -c 'import os; from tradingagents.integrations import DynamicAgentApiSettings; DynamicAgentApiSettings.from_env(); assert os.environ.get("OPENROUTER_API_KEY", "").strip()' \
      || return 1
    run_smoke_stage "agent-runtime-health" "$SMOKE_HEALTH_MAX_ATTEMPTS" \
      compose "$image" "$sha" "$compose_file" exec -T tradingagents \
        python -c 'import os, urllib.request; urllib.request.urlopen(os.environ["AGENT_RUNTIME_BASE_URL"].rstrip("/") + "/health", timeout=5).close()' \
      || return 1
    run_smoke_stage "dynamic-agent-help" 1 \
      compose "$image" "$sha" "$compose_file" exec -T tradingagents \
        tradingagents-dynamic-agent-example --help \
      || return 1
  fi

  if smoke_service_is_declared event-plan-api; then
    run_smoke_stage "event-plan-import" 1 \
      compose "$image" "$sha" "$compose_file" exec -T event-plan-api \
        python -c 'import tradingagents.api.app' \
      || return 1
    run_smoke_stage "event-plan-readiness" "$SMOKE_HEALTH_MAX_ATTEMPTS" \
      compose "$image" "$sha" "$compose_file" exec -T event-plan-api \
        python -c 'import json, urllib.request; body=json.load(urllib.request.urlopen("http://127.0.0.1:8787/health/ready", timeout=5)); assert body == {"status": "ready"}' \
      || return 1
  fi
}

read_previous_state() {
  local key value marker_sha
  if [ ! -f "$STATE_FILE" ]; then
    [ ! -e "$CURRENT_SHA_FILE" ]
    return
  fi
  while IFS='=' read -r key value || [ -n "${key:-}" ]; do
    case "$key" in
      sha) PREVIOUS_SHA="$value" ;;
      image_uri) PREVIOUS_IMAGE="$value" ;;
      release_dir) PREVIOUS_RELEASE="$value" ;;
    esac
  done < "$STATE_FILE"
  [[ "$PREVIOUS_SHA" =~ ^[0-9a-f]{40}$ ]] || return 1
  [[ "$PREVIOUS_IMAGE" =~ ^[A-Za-z0-9._:-]+/[A-Za-z0-9._/-]+:"$PREVIOUS_SHA"$ ]] \
    || return 1
  [ "$PREVIOUS_RELEASE" = "$DEPLOY_DIR/releases/$PREVIOUS_SHA" ] || return 1
  [ -f "$PREVIOUS_RELEASE/docker-compose.server.yml" ] || return 1
  marker_sha="$(cat "$CURRENT_SHA_FILE" 2>/dev/null || true)"
  [ "$marker_sha" = "$PREVIOUS_SHA" ]
}

persist_current_state() {
  local state_tmp="${STATE_FILE}.tmp.$$" sha_tmp="${CURRENT_SHA_FILE}.tmp.$$"
  {
    printf 'sha=%s\n' "$GIT_SHA"
    printf 'image_uri=%s\n' "$IMAGE_URI"
    printf 'release_dir=%s\n' "$RELEASE_DIR"
    printf 'deployed_at=%s\n' "$(date -Is)"
  } > "$state_tmp"
  chmod 600 "$state_tmp"
  mv "$state_tmp" "$STATE_FILE"
  printf '%s\n' "$GIT_SHA" > "$sha_tmp"
  chmod 600 "$sha_tmp"
  mv "$sha_tmp" "$CURRENT_SHA_FILE"
  sync -f "$DEPLOY_DIR" 2>/dev/null || sync
}

rollback() {
  local reason="$1" rollback_reason="" failure_message=""
  if [ -n "$PREVIOUS_SHA" ]; then
    printf '[deploy][rollback] restore sha=%s after: %s\n' "$PREVIOUS_SHA" "$reason" >&2
    if ! pull_image "$PREVIOUS_IMAGE"; then
      rollback_reason="rollback image pull failed after retries"
    elif ! compose "$PREVIOUS_IMAGE" "$PREVIOUS_SHA" \
      "$PREVIOUS_RELEASE/docker-compose.server.yml" \
      up -d --no-build --remove-orphans; then
      rollback_reason="rollback docker compose up failed"
    elif ! wait_for_health "$PREVIOUS_IMAGE" "$PREVIOUS_SHA" \
      "$PREVIOUS_RELEASE/docker-compose.server.yml"; then
      rollback_reason="rollback service health or image identity failed"
    elif ! run_service_smokes "$PREVIOUS_IMAGE" "$PREVIOUS_SHA" \
      "$PREVIOUS_RELEASE/docker-compose.server.yml"; then
      rollback_reason="rollback smoke failed at stage=${FAILED_SMOKE_STAGE:-unknown}"
    else
      write_result "rolled_back" "$reason"
      notify_feishu "rolled_back" "$reason"
      return 0
    fi
    failure_message="rollback failure: $rollback_reason; primary failure: $reason"
    write_result "rollback_failed" "$failure_message"
    notify_feishu "rollback_failed" "$failure_message"
    return 1
  fi

  compose "$IMAGE_URI" "$GIT_SHA" "$COMPOSE_FILE" down --remove-orphans || true
  write_result "failed" "$reason"
  notify_feishu "failed" "$reason"
  return 1
}

cleanup_old_releases() {
  local releases_dir="$DEPLOY_DIR/releases" entry sha resolved
  local -A keep=()
  keep["$GIT_SHA"]=1
  if [ -n "$PREVIOUS_SHA" ]; then
    keep["$PREVIOUS_SHA"]=1
  fi

  while IFS= read -r sha; do
    [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || continue
    if [ "${#keep[@]}" -lt "$RELEASE_RETENTION_COUNT" ]; then
      keep["$sha"]=1
    fi
  done < <(
    find "$releases_dir" -mindepth 1 -maxdepth 1 -type d \
      -printf '%T@|%f\n' | sort -t'|' -k1,1nr | cut -d'|' -f2-
  )

  for entry in "$releases_dir"/*; do
    [ -d "$entry" ] || continue
    sha="${entry##*/}"
    [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || continue
    [ -z "${keep[$sha]:-}" ] || continue
    resolved="$(readlink -f "$entry")"
    if [ "$resolved" != "$releases_dir/$sha" ]; then
      printf '[deploy][cleanup][warn] refuse unexpected release path: %s\n' "$entry" >&2
      continue
    fi
    printf '[deploy][cleanup] remove old release: %s\n' "$entry"
    rm -rf -- "$entry" || printf '[deploy][cleanup][warn] could not remove %s\n' "$entry" >&2
  done
}

cleanup_old_images() {
  local repository tag image_id ref used_id image_is_used
  local image_repository="${IMAGE_URI%:*}"
  local -a used_image_ids=()

  docker image inspect "$IMAGE_URI" >/dev/null 2>&1 || return 1
  if [ -n "$PREVIOUS_IMAGE" ]; then
    docker image inspect "$PREVIOUS_IMAGE" >/dev/null 2>&1 || return 1
  fi
  mapfile -t used_image_ids < <(
    docker ps -aq | while IFS= read -r container_id; do
      [ -n "$container_id" ] || continue
      docker inspect --format '{{.Image}}' "$container_id" 2>/dev/null || true
    done | sed 's/^sha256://' | sort -u
  )

  while IFS='|' read -r repository tag image_id; do
    [ "$repository" = "$image_repository" ] || continue
    [[ "$tag" =~ ^[0-9a-f]{40}$ ]] || continue
    if [ "$tag" = "$GIT_SHA" ] \
      || { [ -n "$PREVIOUS_SHA" ] && [ "$tag" = "$PREVIOUS_SHA" ]; }; then
      continue
    fi
    image_id="${image_id#sha256:}"
    image_is_used=0
    for used_id in "${used_image_ids[@]}"; do
      if [[ "$used_id" == "$image_id"* ]] || [[ "$image_id" == "$used_id"* ]]; then
        image_is_used=1
        break
      fi
    done
    if [ "$image_is_used" -eq 1 ]; then
      printf '[deploy][cleanup] container protects image: %s:%s\n' "$repository" "$tag"
      continue
    fi
    ref="$repository:$tag"
    printf '[deploy][cleanup] remove old project image: %s\n' "$ref"
    docker image rm "$ref" \
      || printf '[deploy][cleanup][warn] could not remove %s\n' "$ref" >&2
  done < <(docker image ls --no-trunc --format '{{.Repository}}|{{.Tag}}|{{.ID}}')
}

cleanup_old_audit_files() {
  find "$DEPLOY_DIR/logs" -maxdepth 1 -type f -name 'deploy-acr.*.log' \
    -mtime "+$DEPLOY_ARTIFACT_RETENTION_DAYS" -delete || true
  find "$RESULT_DIR" -maxdepth 1 -type f \
    \( -name '*.result' -o -name '*.pid' \) \
    -mtime "+$DEPLOY_ARTIFACT_RETENTION_DAYS" -delete || true
}

cleanup_project_artifacts() {
  cleanup_old_releases || printf '[deploy][cleanup][warn] release cleanup failed\n' >&2
  cleanup_old_images || printf '[deploy][cleanup][warn] image cleanup skipped or failed\n' >&2
  cleanup_old_audit_files
}

on_error() {
  local rc=$?
  trap - ERR
  if [ "$MUTATION_STARTED" -eq 1 ]; then
    rollback "unexpected failure rc=$rc" || true
  else
    write_result "failed" "unexpected failure rc=$rc"
    notify_feishu "failed" "unexpected failure rc=$rc"
  fi
  exit "$rc"
}
trap on_error ERR

[[ "$DEPLOY_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] || exit 1
[ "$DEPLOY_DIR" != "/" ] || exit 1
[[ "$DEPLOY_DIR" != *".."* && "$DEPLOY_DIR" != *"//"* && "$DEPLOY_DIR" != */ ]] \
  || exit 1
[[ "$GIT_SHA" =~ ^[0-9a-f]{40}$ ]] || exit 1
[[ "$DEPLOY_REQUEST_ID" =~ ^[1-9][0-9]*\.[1-9][0-9]*$ ]] || exit 1
[ "$RELEASE_DIR" = "$DEPLOY_DIR/releases/$GIT_SHA" ] || exit 1
[[ "$IMAGE_URI" =~ ^[A-Za-z0-9._:-]+/[A-Za-z0-9._/-]+:"$GIT_SHA"$ ]] || exit 1
[[ "$TEST_FORCE_FAILURE_AFTER_UP" =~ ^[01]$ ]] || exit 1
for value in DEPLOY_LOCK_TIMEOUT_SECONDS PULL_MAX_ATTEMPTS HEALTH_TIMEOUT_SECONDS HEALTH_INTERVAL_SECONDS \
  STABLE_HEALTH_SECONDS RELEASE_RETENTION_COUNT DEPLOY_ARTIFACT_RETENTION_DAYS; do
  [[ "${!value}" =~ ^[1-9][0-9]*$ ]] || exit 1
done
[[ "$SMOKE_HEALTH_MAX_ATTEMPTS" =~ ^([1-9]|10)$ ]] || exit 1
[[ "$SMOKE_HEALTH_INTERVAL_SECONDS" =~ ^([1-9]|[12][0-9]|30)$ ]] || exit 1

for command in docker flock readlink; do
  command -v "$command" >/dev/null 2>&1 || exit 1
done
test -f "$COMPOSE_FILE" || exit 1
test -s "$APP_ENV_FILE" || exit 1
test -s "$API_ENV_FILE" || exit 1
test -s "$AGENT_RUNTIME_ENV_FILE" || exit 1
test -s "$DEPLOY_ENV_FILE" || exit 1
mkdir -p "$RESULT_DIR"
chmod 700 "$RUN_DIR" "$RESULT_DIR"

load_deploy_env
DOCKER_CONFIG_DIR="$RUN_DIR/docker-auth.${DEPLOY_REQUEST_ID}.$$"
mkdir "$DOCKER_CONFIG_DIR"
chmod 700 "$DOCKER_CONFIG_DIR"
export DOCKER_CONFIG="$DOCKER_CONFIG_DIR"

exec 9>"$LOCK_FILE"
printf '[deploy] wait for server deployment lock: %s\n' "$LOCK_FILE"
if ! flock -w "$DEPLOY_LOCK_TIMEOUT_SECONDS" 9; then
  write_result "failed" "deployment lock timed out"
  notify_feishu "failed" "deployment lock timed out"
  exit 1
fi

if ! latest_request_matches; then
  write_result "superseded" "a newer deployment was registered before execution"
  exit 0
fi

if ! read_previous_state; then
  write_result "failed" "invalid previous deployment state"
  notify_feishu "failed" "invalid previous deployment state"
  exit 1
fi
write_result "running" "server deployment started"

printf '%s\n' "$ACR_PULL_PASSWORD" \
  | docker login "$ACR_REGISTRY" -u "$ACR_PULL_USER" --password-stdin >/dev/null

printf '[deploy] pull immutable image: %s\n' "$IMAGE_URI"
if ! pull_image "$IMAGE_URI"; then
  write_result "failed" "image pull failed after retries"
  notify_feishu "failed" "image pull failed after retries"
  exit 1
fi

if ! latest_request_matches; then
  write_result "superseded" "a newer deployment was registered before container mutation"
  exit 0
fi

MUTATION_STARTED=1
if ! compose "$IMAGE_URI" "$GIT_SHA" "$COMPOSE_FILE" \
  up -d --no-build --remove-orphans; then
  trap - ERR
  rollback "docker compose up failed" || true
  exit 1
fi

if [ "$TEST_FORCE_FAILURE_AFTER_UP" = "1" ]; then
  trap - ERR
  rollback "controlled failure injected after compose up" || true
  exit 1
fi

failure_reason=""
if ! wait_for_health "$IMAGE_URI" "$GIT_SHA" "$COMPOSE_FILE"; then
  failure_reason="service health or image identity failed"
elif ! run_service_smokes "$IMAGE_URI" "$GIT_SHA" "$COMPOSE_FILE"; then
  failure_reason="service smoke failed at stage=${FAILED_SMOKE_STAGE:-unknown}"
fi
if [ -n "$failure_reason" ]; then
  compose "$IMAGE_URI" "$GIT_SHA" "$COMPOSE_FILE" ps || true
  compose "$IMAGE_URI" "$GIT_SHA" "$COMPOSE_FILE" logs --tail 120 || true
  trap - ERR
  rollback "$failure_reason" || true
  exit 1
fi

status="success"
latest_request_matches || status="success_superseded"
persist_current_state
MUTATION_STARTED=0
trap - ERR
write_result "$status" "services healthy with the requested immutable image and smokes passed"

if [ "$status" = "success" ]; then
  notify_feishu "success" "services healthy and smokes passed"
  cleanup_project_artifacts
fi

compose "$IMAGE_URI" "$GIT_SHA" "$COMPOSE_FILE" ps || true
printf '[deploy] %s sha=%s image=%s\n' "$status" "$GIT_SHA" "$IMAGE_URI"
