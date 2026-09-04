#!/usr/bin/env bash
set -Eeuo pipefail

DEPLOY_DIR="${DEPLOY_DIR:?DEPLOY_DIR is required}"
DEPLOY_LOCK_TIMEOUT_SECONDS="${DEPLOY_LOCK_TIMEOUT_SECONDS:-1800}"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-a-stock-rachtrader}"
APP_ENV_FILE="$DEPLOY_DIR/.env"
API_ENV_FILE="$DEPLOY_DIR/.event-plan-api.env"
AGENT_RUNTIME_ENV_FILE="${AGENT_RUNTIME_ENV_FILE:-/opt/etf-agent-runtime-tunnel/secrets/internal-agent-caller.env}"

RUN_DIR="$DEPLOY_DIR/run"
LOCK_FILE="$RUN_DIR/deploy-global.lock"
STATE_FILE="$DEPLOY_DIR/.deploy-current"
CURRENT_SHA_FILE="$DEPLOY_DIR/.deploy-current-sha"

die() {
  printf '[deploy-preflight][error] %s\n' "$*" >&2
  exit 1
}

[[ "$DEPLOY_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] \
  || die "DEPLOY_DIR must be a normalized absolute path"
[[ "$DEPLOY_DIR" != *"//"* && "$DEPLOY_DIR" != *"/../"* && "$DEPLOY_DIR" != */.. \
  && "$DEPLOY_DIR" != *"/./"* && "$DEPLOY_DIR" != */. ]] \
  || die "DEPLOY_DIR must not contain traversal or duplicate separators"
[[ "$DEPLOY_LOCK_TIMEOUT_SECONDS" =~ ^[1-9][0-9]{0,3}$ ]] \
  || die "DEPLOY_LOCK_TIMEOUT_SECONDS must be between 1 and 9999"
[[ "$COMPOSE_PROJECT_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] \
  || die "COMPOSE_PROJECT_NAME is invalid"

for required_command in flock docker; do
  command -v "$required_command" >/dev/null 2>&1 \
    || die "required command not found: $required_command"
done

[ -d "$DEPLOY_DIR" ] || die "deployment directory does not exist: $DEPLOY_DIR"
cd -- "$DEPLOY_DIR"
mkdir -p "$RUN_DIR"

exec 9>"$LOCK_FILE"
flock -w "$DEPLOY_LOCK_TIMEOUT_SECONDS" 9 \
  || die "timed out waiting for the server deployment lock"

for state_path in "$STATE_FILE" "$CURRENT_SHA_FILE"; do
  [ ! -L "$state_path" ] || die "deployment state must not be a symlink: $state_path"
  if [ -e "$state_path" ] && [ ! -f "$state_path" ]; then
    die "deployment state must be a regular file: $state_path"
  fi
done

state_present=0
marker_present=0
[ -f "$STATE_FILE" ] && state_present=1
[ -f "$CURRENT_SHA_FILE" ] && marker_present=1

container_output="$(
  docker ps -aq \
    --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME"
)" || die "cannot query the current managed containers"
container_ids=()
if [ -n "$container_output" ]; then
  mapfile -t container_ids <<< "$container_output"
fi

if [ "$state_present" -eq 0 ] && [ "$marker_present" -eq 0 ]; then
  [ "${#container_ids[@]}" -eq 0 ] \
    || die "managed project container exists but deployment state is missing"
  printf 'DEPLOY_BASE_STATUS=bootstrap\n'
  exit 0
fi

if [ "$state_present" -ne 1 ] || [ "$marker_present" -ne 1 ]; then
  die ".deploy-current and .deploy-current-sha must either both exist or both be absent"
fi

sha=""
image_uri=""
release_dir=""
deployed_at=""
while IFS='=' read -r key value || [ -n "${key:-}${value:-}" ]; do
  [ -n "${key:-}" ] || die "deployment state contains an empty key"
  case "$key" in
    sha)
      [ -z "$sha" ] || die "deployment state contains duplicate sha"
      sha="$value"
      ;;
    image_uri)
      [ -z "$image_uri" ] || die "deployment state contains duplicate image_uri"
      image_uri="$value"
      ;;
    release_dir)
      [ -z "$release_dir" ] || die "deployment state contains duplicate release_dir"
      release_dir="$value"
      ;;
    deployed_at)
      [ -z "$deployed_at" ] || die "deployment state contains duplicate deployed_at"
      deployed_at="$value"
      ;;
    *) die "deployment state contains an unsupported key: $key" ;;
  esac
done < "$STATE_FILE"

[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || die "deployment state SHA is invalid"
[[ "$image_uri" =~ ^[A-Za-z0-9._:-]+/[A-Za-z0-9._/-]+:${sha}$ ]] \
  || die "deployment state image does not match its SHA"
[ "$release_dir" = "$DEPLOY_DIR/releases/$sha" ] \
  || die "deployment state release path does not match its SHA"
[ -n "$deployed_at" ] || die "deployment state is missing deployed_at"
if [ -L "$release_dir" ] || [ ! -d "$release_dir" ]; then
  die "deployment release directory is missing or is a symlink"
fi
compose_file="$release_dir/docker-compose.server.yml"
if [ -L "$compose_file" ] || [ ! -f "$compose_file" ]; then
  die "deployment release Compose file is missing or is a symlink"
fi

compose() {
  TRADINGAGENTS_IMAGE="$image_uri" \
  TRADINGAGENTS_ENV_FILE="$APP_ENV_FILE" \
  TRADINGAGENTS_API_ENV_FILE="$API_ENV_FILE" \
  AGENT_RUNTIME_ENV_FILE="$AGENT_RUNTIME_ENV_FILE" \
  GIT_SHA="$sha" \
    docker compose \
      -p "$COMPOSE_PROJECT_NAME" \
      --project-directory "$DEPLOY_DIR" \
      -f "$compose_file" \
      "$@"
}

service_output="$(compose config --services)" \
  || die "cannot resolve expected services from the current release Compose file"
expected_services=()
if [ -n "$service_output" ]; then
  mapfile -t expected_services <<< "$service_output"
fi
[ "${#expected_services[@]}" -gt 0 ] \
  || die "current release Compose file has no services"

mapfile -t marker_lines < "$CURRENT_SHA_FILE"
if [ "${#marker_lines[@]}" -ne 1 ] || [ "${marker_lines[0]}" != "$sha" ]; then
  die ".deploy-current-sha does not exactly match deployment state"
fi

[ "${#container_ids[@]}" -eq "${#expected_services[@]}" ] \
  || die "managed container count does not match the current release service set"

for service_name in "${expected_services[@]}"; do
  service_container_output="$(
    docker ps -aq \
      --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" \
      --filter "label=com.docker.compose.service=$service_name"
  )" || die "cannot query managed service container: $service_name"
  service_container_ids=()
  if [ -n "$service_container_output" ]; then
    mapfile -t service_container_ids <<< "$service_container_output"
  fi
  [ "${#service_container_ids[@]}" -eq 1 ] \
    || die "expected exactly one managed container for service: $service_name"
  observed_identity="$(
    docker inspect \
      --format '{{.Config.Image}}|{{index .Config.Labels "io.rachel.deploy.sha"}}' \
      "${service_container_ids[0]}"
  )" || die "cannot inspect managed service container: $service_name"
  [ "$observed_identity" = "$image_uri|$sha" ] \
    || die "managed service image identity does not match deployment state: $service_name"
done

printf 'DEPLOY_BASE_STATUS=present\n'
printf 'DEPLOY_BASE_SHA=%s\n' "$sha"
