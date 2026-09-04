#!/usr/bin/env bash
set -euo pipefail

DEPLOY_DIR="${DEPLOY_DIR:?DEPLOY_DIR is required}"
GIT_SHA="${GIT_SHA:?GIT_SHA is required}"
DEPLOY_RUN_ID="${DEPLOY_RUN_ID:?DEPLOY_RUN_ID is required}"
DEPLOY_RUN_ATTEMPT="${DEPLOY_RUN_ATTEMPT:?DEPLOY_RUN_ATTEMPT is required}"
IMAGE_URI="${IMAGE_URI:?IMAGE_URI is required}"
STARTUP_WAIT_SECONDS="${DEPLOY_STARTUP_WAIT_SECONDS:-2}"
TEST_FORCE_FAILURE_AFTER_UP="${DEPLOY_TEST_FORCE_FAILURE_AFTER_UP:-0}"

fail() {
  printf '::error::%s\n' "$*" >&2
  exit 1
}

sanitize_message() {
  local message="$1"
  message="${message//$'\n'/ }"
  printf '%.500s' "$message"
}

[[ "$DEPLOY_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail "invalid DEPLOY_DIR"
[ "$DEPLOY_DIR" != "/" ] || fail "DEPLOY_DIR must not be root"
[[ "$DEPLOY_DIR" != *".."* && "$DEPLOY_DIR" != *"//"* ]] \
  || fail "DEPLOY_DIR must be a normalized absolute path"
[[ "$DEPLOY_DIR" != */ ]] || fail "DEPLOY_DIR must not end with a slash"
[[ "$GIT_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "invalid GIT_SHA"
[[ "$DEPLOY_RUN_ID" =~ ^[1-9][0-9]*$ ]] || fail "invalid DEPLOY_RUN_ID"
[[ "$DEPLOY_RUN_ATTEMPT" =~ ^[1-9][0-9]*$ ]] || fail "invalid DEPLOY_RUN_ATTEMPT"
[[ "$STARTUP_WAIT_SECONDS" =~ ^([1-9]|10)$ ]] \
  || fail "DEPLOY_STARTUP_WAIT_SECONDS must be between 1 and 10"
[[ "$TEST_FORCE_FAILURE_AFTER_UP" =~ ^[01]$ ]] \
  || fail "DEPLOY_TEST_FORCE_FAILURE_AFTER_UP must be 0 or 1"
[[ "$IMAGE_URI" =~ ^[A-Za-z0-9._:-]+/[A-Za-z0-9._/-]+:"$GIT_SHA"$ ]] \
  || fail "IMAGE_URI must be an immutable image tagged with GIT_SHA"

for command in flock setsid; do
  command -v "$command" >/dev/null 2>&1 || fail "missing command: $command"
done

release_dir="$DEPLOY_DIR/releases/$GIT_SHA"
run_script="$release_dir/scripts/deploy-acr-background.sh"
compose_file="$release_dir/docker-compose.server.yml"
request_id="${DEPLOY_RUN_ID}.${DEPLOY_RUN_ATTEMPT}"
run_dir="$DEPLOY_DIR/run"
result_dir="$run_dir/deploy-results/acr"
request_file="$run_dir/deploy-acr.latest-request"
request_lock="$run_dir/deploy-acr.request.lock"
result_file="$result_dir/${request_id}.result"
latest_result_file="$DEPLOY_DIR/.deploy-last-result.acr"
pid_file="$result_dir/${request_id}.pid"
latest_pid_file="$run_dir/deploy-acr.pid"
log_file="$DEPLOY_DIR/logs/deploy-acr.${request_id}.$(date +%Y%m%d%H%M%S).log"

mkdir -p "$DEPLOY_DIR/logs" "$result_dir"
chmod 700 "$DEPLOY_DIR/logs" "$run_dir" "$result_dir"
test -f "$run_script" || fail "missing server deploy script: $run_script"
test -f "$compose_file" || fail "missing server compose file: $compose_file"

write_result() {
  local status="$1"
  local message="$2"
  local pid_value="${3:-}"
  local output_file="$4"
  local tmp="${output_file}.tmp.$$"
  {
    printf 'status=%s\n' "$status"
    printf 'pid=%s\n' "$pid_value"
    printf 'request_id=%s\n' "$request_id"
    printf 'sha=%s\n' "$GIT_SHA"
    printf 'image_uri=%s\n' "$IMAGE_URI"
    printf 'release_dir=%s\n' "$release_dir"
    printf 'time=%s\n' "$(date -Is)"
    printf 'message=%s\n' "$(sanitize_message "$message")"
  } > "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$output_file"
}

publish_latest_result() {
  local tmp="${latest_result_file}.tmp.$$"
  cp "$result_file" "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$latest_result_file"
}

read_current_request() {
  local key value
  current_request_id=""
  current_run_id=""
  current_run_attempt=""
  current_sha=""
  current_image=""
  [ -f "$request_file" ] || return 1
  while IFS='=' read -r key value || [ -n "${key:-}" ]; do
    case "$key" in
      request_id) current_request_id="$value" ;;
      run_id) current_run_id="$value" ;;
      run_attempt) current_run_attempt="$value" ;;
      sha) current_sha="$value" ;;
      image_uri) current_image="$value" ;;
    esac
  done < "$request_file"
  [[ "$current_request_id" =~ ^[1-9][0-9]*\.[1-9][0-9]*$ ]] \
    && [[ "$current_run_id" =~ ^[1-9][0-9]*$ ]] \
    && [[ "$current_run_attempt" =~ ^[1-9][0-9]*$ ]] \
    && [[ "$current_sha" =~ ^[0-9a-f]{40}$ ]] \
    && [[ "$current_image" =~ ^[A-Za-z0-9._:-]+/[A-Za-z0-9._/-]+:"$current_sha"$ ]]
}

current_request_is_this() {
  read_current_request \
    && [ "$current_request_id" = "$request_id" ] \
    && [ "$current_sha" = "$GIT_SHA" ] \
    && [ "$current_image" = "$IMAGE_URI" ]
}

handle_duplicate_request() {
  local status existing_pid
  status="$(sed -n 's/^status=//p' "$result_file" 2>/dev/null | tail -1)"
  existing_pid="$(cat "$pid_file" 2>/dev/null || true)"
  case "$status" in
    success|success_superseded|superseded)
      printf 'ACR deploy request already completed: request_id=%s status=%s\n' \
        "$request_id" "$status"
      return 0
      ;;
    queued|running)
      if [[ "$existing_pid" =~ ^[1-9][0-9]*$ ]] \
        && kill -0 "$existing_pid" 2>/dev/null; then
        printf 'ACR deploy request already active: request_id=%s pid=%s status=%s\n' \
          "$request_id" "$existing_pid" "$status"
        return 0
      fi
      ;;
  esac
  fail "request is registered but has no successful or live deployment task: $request_id"
}

exec 9>"$request_lock"
flock -w 30 9 || fail "request registration lock timed out"

if read_current_request; then
  if [ "$DEPLOY_RUN_ID" -lt "$current_run_id" ] \
    || { [ "$DEPLOY_RUN_ID" -eq "$current_run_id" ] \
      && [ "$DEPLOY_RUN_ATTEMPT" -lt "$current_run_attempt" ]; }; then
    write_result "superseded" "older than the latest registered request" "" "$result_file"
    flock -u 9
    printf 'ACR deploy request superseded before launch: %s\n' "$request_id"
    exit 0
  fi
  if [ "$DEPLOY_RUN_ID" -eq "$current_run_id" ] \
    && [ "$DEPLOY_RUN_ATTEMPT" -eq "$current_run_attempt" ]; then
    if [ "$GIT_SHA" != "$current_sha" ] || [ "$IMAGE_URI" != "$current_image" ]; then
      fail "request identity collision"
    fi
    flock -u 9
    handle_duplicate_request
    exit 0
  fi
fi

request_tmp="${request_file}.tmp.$$"
{
  printf 'request_id=%s\n' "$request_id"
  printf 'run_id=%s\n' "$DEPLOY_RUN_ID"
  printf 'run_attempt=%s\n' "$DEPLOY_RUN_ATTEMPT"
  printf 'sha=%s\n' "$GIT_SHA"
  printf 'image_uri=%s\n' "$IMAGE_URI"
  printf 'registered_at=%s\n' "$(date -Is)"
} > "$request_tmp"
chmod 600 "$request_tmp"
write_result "queued" "registered for detached server deployment" "" "$result_file"
mv "$request_tmp" "$request_file"
publish_latest_result
flock -u 9
exec 9>&-

umask 077
: > "$log_file"
setsid env \
  DEPLOY_DIR="$DEPLOY_DIR" \
  RELEASE_DIR="$release_dir" \
  GIT_SHA="$GIT_SHA" \
  IMAGE_URI="$IMAGE_URI" \
  DEPLOY_REQUEST_ID="$request_id" \
  DEPLOY_TEST_FORCE_FAILURE_AFTER_UP="$TEST_FORCE_FAILURE_AFTER_UP" \
  bash "$run_script" > "$log_file" 2>&1 </dev/null &
pid=$!
printf '%s\n' "$pid" > "$pid_file"
chmod 600 "$pid_file"
cp "$pid_file" "$latest_pid_file"
chmod 600 "$latest_pid_file"

waited=0
while [ "$waited" -lt "$STARTUP_WAIT_SECONDS" ] && kill -0 "$pid" 2>/dev/null; do
  sleep 1
  waited=$((waited + 1))
done

if ! kill -0 "$pid" 2>/dev/null; then
  status="$(sed -n 's/^status=//p' "$result_file" 2>/dev/null | tail -1)"
  case "$status" in
    success|success_superseded|superseded)
      printf 'ACR deploy completed during startup window: request_id=%s status=%s\n' \
        "$request_id" "$status"
      exit 0
      ;;
    queued|running|"")
      write_result "launch_failed" "background process exited during startup" "$pid" "$result_file"
      if current_request_is_this; then
        publish_latest_result
      fi
      ;;
  esac
  printf '::error::background deploy exited during startup; log tail follows\n' >&2
  tail -n 120 "$log_file" >&2 || true
  exit 1
fi

printf 'ACR deploy launched: request_id=%s pid=%s log=%s result=%s\n' \
  "$request_id" "$pid" "$log_file" "$result_file"
