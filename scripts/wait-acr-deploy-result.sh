#!/usr/bin/env bash
set -euo pipefail

DEPLOY_DIR="${DEPLOY_DIR:?DEPLOY_DIR is required}"
GIT_SHA="${GIT_SHA:?GIT_SHA is required}"
DEPLOY_RUN_ID="${DEPLOY_RUN_ID:?DEPLOY_RUN_ID is required}"
DEPLOY_RUN_ATTEMPT="${DEPLOY_RUN_ATTEMPT:?DEPLOY_RUN_ATTEMPT is required}"
IMAGE_URI="${IMAGE_URI:?IMAGE_URI is required}"
RESULT_TIMEOUT_SECONDS="${DEPLOY_RESULT_TIMEOUT_SECONDS:-2700}"
RESULT_POLL_INTERVAL_SECONDS="${DEPLOY_RESULT_POLL_INTERVAL_SECONDS:-5}"

fail() {
  printf '::error::%s\n' "$*" >&2
  exit 1
}

[[ "$DEPLOY_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail "invalid DEPLOY_DIR"
[ "$DEPLOY_DIR" != "/" ] || fail "DEPLOY_DIR must not be root"
[[ "$DEPLOY_DIR" != *".."* && "$DEPLOY_DIR" != *"//"* ]] \
  || fail "DEPLOY_DIR must be a normalized absolute path"
[[ "$DEPLOY_DIR" != */ ]] || fail "DEPLOY_DIR must not end with a slash"
[[ "$GIT_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "invalid GIT_SHA"
[[ "$DEPLOY_RUN_ID" =~ ^[1-9][0-9]*$ ]] || fail "invalid DEPLOY_RUN_ID"
[[ "$DEPLOY_RUN_ATTEMPT" =~ ^[1-9][0-9]*$ ]] || fail "invalid DEPLOY_RUN_ATTEMPT"
[[ "$IMAGE_URI" =~ ^[A-Za-z0-9._:-]+/[A-Za-z0-9._/-]+:"$GIT_SHA"$ ]] \
  || fail "IMAGE_URI must be an immutable image tagged with GIT_SHA"
if ! [[ "$RESULT_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] \
  || [ "$RESULT_TIMEOUT_SECONDS" -gt 3600 ]; then
  fail "DEPLOY_RESULT_TIMEOUT_SECONDS must be between 1 and 3600"
fi
if ! [[ "$RESULT_POLL_INTERVAL_SECONDS" =~ ^[1-9][0-9]*$ ]] \
  || [ "$RESULT_POLL_INTERVAL_SECONDS" -gt 60 ]; then
  fail "DEPLOY_RESULT_POLL_INTERVAL_SECONDS must be between 1 and 60"
fi

for command in id stat; do
  command -v "$command" >/dev/null 2>&1 || fail "missing command: $command"
done

request_id="${DEPLOY_RUN_ID}.${DEPLOY_RUN_ATTEMPT}"
release_dir="$DEPLOY_DIR/releases/$GIT_SHA"
result_file="$DEPLOY_DIR/run/deploy-results/acr/${request_id}.result"
expected_owner_uid="$(id -u)"

read_exact_result() {
  local line key value mode owner_uid
  local status_seen=0 pid_seen=0 request_seen=0 sha_seen=0 image_seen=0
  local previous_seen=0 release_seen=0 time_seen=0 message_seen=0

  RESULT_STATUS=""
  RESULT_PID=""
  RESULT_REQUEST_ID=""
  RESULT_SHA=""
  RESULT_IMAGE_URI=""
  RESULT_PREVIOUS_SHA=""
  RESULT_RELEASE_DIR=""
  RESULT_TIME=""
  RESULT_MESSAGE=""

  [ ! -L "$result_file" ] || return 1
  [ -f "$result_file" ] || return 1
  mode="$(stat -c '%a' "$result_file" 2>/dev/null)" || return 1
  owner_uid="$(stat -c '%u' "$result_file" 2>/dev/null)" || return 1
  [ "$mode" = "600" ] || return 1
  [ "$owner_uid" = "$expected_owner_uid" ] || return 1

  while IFS= read -r line || [ -n "${line:-}" ]; do
    [[ "$line" == *=* ]] || return 1
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      status)
        [ "$status_seen" -eq 0 ] || return 1
        RESULT_STATUS="$value"
        status_seen=1
        ;;
      pid)
        [ "$pid_seen" -eq 0 ] || return 1
        RESULT_PID="$value"
        pid_seen=1
        ;;
      request_id)
        [ "$request_seen" -eq 0 ] || return 1
        RESULT_REQUEST_ID="$value"
        request_seen=1
        ;;
      sha)
        [ "$sha_seen" -eq 0 ] || return 1
        RESULT_SHA="$value"
        sha_seen=1
        ;;
      image_uri)
        [ "$image_seen" -eq 0 ] || return 1
        RESULT_IMAGE_URI="$value"
        image_seen=1
        ;;
      previous_sha)
        [ "$previous_seen" -eq 0 ] || return 1
        RESULT_PREVIOUS_SHA="$value"
        previous_seen=1
        ;;
      release_dir)
        [ "$release_seen" -eq 0 ] || return 1
        RESULT_RELEASE_DIR="$value"
        release_seen=1
        ;;
      time)
        [ "$time_seen" -eq 0 ] || return 1
        RESULT_TIME="$value"
        time_seen=1
        ;;
      message)
        [ "$message_seen" -eq 0 ] || return 1
        RESULT_MESSAGE="$value"
        message_seen=1
        ;;
      *) return 1 ;;
    esac
  done < "$result_file"

  [ "$status_seen" -eq 1 ] \
    && [ "$pid_seen" -eq 1 ] \
    && [ "$request_seen" -eq 1 ] \
    && [ "$sha_seen" -eq 1 ] \
    && [ "$image_seen" -eq 1 ] \
    && [ "$release_seen" -eq 1 ] \
    && [ "$time_seen" -eq 1 ] \
    && [ "$message_seen" -eq 1 ] \
    || return 1
  case "$RESULT_STATUS" in
    queued|running|success|success_superseded|superseded|failed|rolled_back|rollback_failed|launch_failed) ;;
    *) return 1 ;;
  esac
  [ -z "$RESULT_PID" ] || [[ "$RESULT_PID" =~ ^[1-9][0-9]*$ ]] || return 1
  [ "$RESULT_REQUEST_ID" = "$request_id" ] || return 1
  [ "$RESULT_SHA" = "$GIT_SHA" ] || return 1
  [ "$RESULT_IMAGE_URI" = "$IMAGE_URI" ] || return 1
  [ -z "$RESULT_PREVIOUS_SHA" ] \
    || [[ "$RESULT_PREVIOUS_SHA" =~ ^[0-9a-f]{40}$ ]] \
    || return 1
  [ "$RESULT_RELEASE_DIR" = "$release_dir" ] || return 1
  [ -n "$RESULT_TIME" ] && [ "${#RESULT_TIME}" -le 80 ] || return 1
  [ -n "$RESULT_MESSAGE" ] && [ "${#RESULT_MESSAGE}" -le 500 ] || return 1
  [[ "$RESULT_TIME" != *$'\r'* && "$RESULT_MESSAGE" != *$'\r'* ]] || return 1
}

deadline=$((SECONDS + RESULT_TIMEOUT_SECONDS))
last_reported_status=""
observed_status="missing"

while :; do
  if [ -L "$result_file" ]; then
    fail "refuse symlink deployment result for request $request_id"
  fi
  if [ -e "$result_file" ]; then
    read_exact_result \
      || fail "invalid or mismatched deployment result for request $request_id"
    observed_status="$RESULT_STATUS"
    if [ "$RESULT_STATUS" != "$last_reported_status" ]; then
      printf 'ACR deploy progress: request_id=%s status=%s sha=%s\n' \
        "$RESULT_REQUEST_ID" "$RESULT_STATUS" "$RESULT_SHA"
      last_reported_status="$RESULT_STATUS"
    fi
    case "$RESULT_STATUS" in
      success|success_superseded|superseded)
        printf 'ACR deploy final result: request_id=%s status=%s sha=%s message=%q\n' \
          "$RESULT_REQUEST_ID" "$RESULT_STATUS" "$RESULT_SHA" "$RESULT_MESSAGE"
        exit 0
        ;;
      failed|rolled_back|rollback_failed|launch_failed)
        printf '::error::ACR deploy terminal failure: request_id=%s status=%s sha=%s message=%q\n' \
          "$RESULT_REQUEST_ID" "$RESULT_STATUS" "$RESULT_SHA" "$RESULT_MESSAGE" >&2
        exit 1
        ;;
    esac
  fi

  if [ "$SECONDS" -ge "$deadline" ]; then
    fail "timed out waiting for deployment result request=$request_id last_status=$observed_status"
  fi
  sleep "$RESULT_POLL_INTERVAL_SECONDS"
done
