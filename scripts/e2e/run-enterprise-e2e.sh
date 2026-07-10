#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/ops/e2e/docker-compose.enterprise.yml"
RUNNER="$ROOT_DIR/tests/enterprise/run_live.py"
MODE="${E2E_DEPENDENCY_MODE:-compose}"
RUN_ID="${E2E_RUN_ID:-$(python3 -c 'import secrets; print(secrets.token_hex(12))')}"
# Compose interpolates E2E_RUN_ID directly; keep it identical to the validated
# canonical run ID even when the caller lets the harness generate the value.
E2E_RUN_ID="$RUN_ID"
EVIDENCE_BASENAME="${E2E_EVIDENCE_BASENAME:-enterprise-e2e-${RUN_ID}.json}"
EVIDENCE_FILE="$ROOT_DIR/docs/evidence/$EVIDENCE_BASENAME"
E2E_SECRET_LOG_BASENAME=".enterprise-e2e-${RUN_ID}-application.log"
SECRET_LOG_FILE="$ROOT_DIR/docs/evidence/$E2E_SECRET_LOG_BASENAME"
E2E_COMPOSE_PROJECT_NAME="${E2E_COMPOSE_PROJECT_NAME:-imposbro-e2e-${RUN_ID:0:12}}"
E2E_HOST_UID="${E2E_HOST_UID:-$(id -u)}"
E2E_HOST_GID="${E2E_HOST_GID:-$(id -g)}"
if [[ "$MODE" == external ]]; then
  E2E_POSTGRES_PASSWORD="${E2E_POSTGRES_PASSWORD:-}"
  E2E_TYPESENSE_API_KEY="${E2E_TYPESENSE_API_KEY:-}"
  E2E_ADMIN_API_KEY="${E2E_ADMIN_API_KEY:-}"
  E2E_DATA_API_KEY="${E2E_DATA_API_KEY:-}"
else
  E2E_POSTGRES_PASSWORD="${E2E_POSTGRES_PASSWORD:-$(python3 -c 'import secrets; print(secrets.token_hex(24))')}"
  E2E_TYPESENSE_API_KEY="${E2E_TYPESENSE_API_KEY:-$(python3 -c 'import secrets; print(secrets.token_hex(24))')}"
  E2E_ADMIN_API_KEY="${E2E_ADMIN_API_KEY:-$(python3 -c 'import secrets; print(secrets.token_hex(24))')}"
  E2E_DATA_API_KEY="${E2E_DATA_API_KEY:-$(python3 -c 'import secrets; print(secrets.token_hex(24))')}"
fi
E2E_GIT_COMMIT="${E2E_GIT_COMMIT:-$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || printf unknown)}"
if [[ -z "$(git -C "$ROOT_DIR" status --porcelain 2>/dev/null)" ]]; then
  E2E_GIT_DIRTY="${E2E_GIT_DIRTY:-false}"
else
  E2E_GIT_DIRTY="${E2E_GIT_DIRTY:-true}"
fi

export RUN_ID E2E_RUN_ID E2E_COMPOSE_PROJECT_NAME E2E_HOST_UID E2E_HOST_GID
export E2E_POSTGRES_PASSWORD E2E_TYPESENSE_API_KEY E2E_ADMIN_API_KEY E2E_DATA_API_KEY
export E2E_GIT_COMMIT E2E_GIT_DIRTY
export E2E_SECRET_LOG_BASENAME

COMPOSE_STARTED=false
REDIS_STOPPED=false
EVIDENCE_INITIALIZED=false
EXTERNAL_REDIS_STOPPED=false
FINALIZED=false

case "$EVIDENCE_BASENAME" in
  ""|*/*|.*) printf 'E2E_EVIDENCE_BASENAME must be a plain JSON filename\n' >&2; exit 64 ;;
  *.json) ;;
  *) printf 'E2E_EVIDENCE_BASENAME must end in .json\n' >&2; exit 64 ;;
esac
if [[ ! "$RUN_ID" =~ ^[a-zA-Z0-9_-]{8,64}$ ]]; then
  printf 'E2E_RUN_ID must contain 8-64 letters, numbers, underscore, or hyphen\n' >&2
  exit 64
fi
if [[ ! "$E2E_COMPOSE_PROJECT_NAME" =~ ^[a-z0-9][a-z0-9_-]{2,62}$ ]]; then
  printf 'E2E_COMPOSE_PROJECT_NAME must be a safe lower-case project name\n' >&2
  exit 64
fi

compose() {
  docker compose --project-name "$E2E_COMPOSE_PROJECT_NAME" -f "$COMPOSE_FILE" "$@"
}

record_not_run() {
  python3 "$RUNNER" --evidence-file "$EVIDENCE_FILE" record-not-run \
    --id "$1" --reason "$2"
}

record_result() {
  local identifier=$1
  local status=$2
  local reason=${3:-}
  local duration_ms=${4:-0}
  local assertions_json="{}"
  if [[ $# -ge 5 ]]; then
    assertions_json=$5
  fi
  local arguments=(
    --evidence-file "$EVIDENCE_FILE" record-result
    --id "$identifier" --status "$status"
    --duration-ms "$duration_ms" --assertions-json "$assertions_json"
  )
  if [[ -n "$reason" ]]; then
    arguments+=(--reason "$reason")
  fi
  python3 "$RUNNER" "${arguments[@]}"
}

milliseconds() {
  python3 -c 'import time; print(time.time_ns() // 1000000)'
}

finalize_evidence() {
  if [[ "$EVIDENCE_INITIALIZED" == true && "$FINALIZED" != true ]]; then
    python3 "$RUNNER" --evidence-file "$EVIDENCE_FILE" finalize >/dev/null || true
    FINALIZED=true
  fi
}

cleanup() {
  local exit_code=$?
  set +e
  if [[ "$EXTERNAL_REDIS_STOPPED" == true && -n "${E2E_START_REDIS_HOOK:-}" ]]; then
    "$E2E_START_REDIS_HOOK"
    EXTERNAL_REDIS_STOPPED=false
  fi
  if [[ "$REDIS_STOPPED" == true ]]; then
    compose start redis >/dev/null
    REDIS_STOPPED=false
  fi
  if [[ "$exit_code" -ne 0 && "$EVIDENCE_INITIALIZED" == true ]]; then
    record_result harness_execution failed \
      "Harness exited with code $exit_code before every required scenario passed" \
      0 "{\"exit_code\":$exit_code}" || true
  fi
  if [[ "$exit_code" -ne 0 && "$COMPOSE_STARTED" == true && \
        "$EVIDENCE_INITIALIZED" == true ]]; then
    local diagnostics_file
    diagnostics_file="$(mktemp)"
    chmod 600 "$diagnostics_file"
    {
      compose ps --all
      for service in postgres redis kafka migrate query-a query-b typesense-tls \
          otel-capture indexing-service; do
        printf '\n[%s logs]\n' "$service"
        compose logs --no-color --tail 60 "$service"
      done
    } >"$diagnostics_file" 2>&1
    python3 "$RUNNER" --evidence-file "$EVIDENCE_FILE" attach-diagnostics \
      --input-file "$diagnostics_file" || true
    rm -f "$diagnostics_file"
  fi
  if [[ "$COMPOSE_STARTED" == true && "${E2E_KEEP_STACK:-false}" != true ]]; then
    if [[ "${E2E_PRUNE_VOLUMES:-false}" == true ]]; then
      compose down --remove-orphans --volumes >/dev/null
    else
      compose down --remove-orphans >/dev/null
    fi
  fi
  finalize_evidence
  rm -f "$SECRET_LOG_FILE"
  printf 'Enterprise E2E evidence: %s\n' "$EVIDENCE_FILE"
  exit "$exit_code"
}
trap cleanup EXIT

initialize_evidence() {
  local runtime=$1
  python3 "$RUNNER" --evidence-file "$EVIDENCE_FILE" init \
    --dependency-mode "$MODE" --runtime "$runtime"
  EVIDENCE_INITIALIZED=true
}

static_validate() {
  local require_compose=${1:-true}
  python3 -m py_compile \
    "$ROOT_DIR/tests/enterprise/run_live.py" \
    "$ROOT_DIR/tests/enterprise/generate_tls_material.py" \
    "$ROOT_DIR/tests/enterprise/otlp_capture.py" \
    "$ROOT_DIR/tests/enterprise/write_runtime_secret.py"
  bash -n "$ROOT_DIR/scripts/e2e/run-enterprise-e2e.sh"
  python3 -m unittest discover -s "$ROOT_DIR/tests/enterprise" -p 'test_*.py'
  if [[ "$require_compose" == true ]] && command -v docker >/dev/null 2>&1 && \
      docker compose version >/dev/null 2>&1; then
    compose config --quiet
  elif [[ "$require_compose" == true ]]; then
    return 2
  fi
}

compose_runner() {
  compose --profile tools run --rm --no-deps enterprise-tests \
    --evidence-file "/evidence/$EVIDENCE_BASENAME" "$@"
}

compose_image_provenance() {
  local container_ids=()
  local container_id
  while IFS= read -r container_id; do
    if [[ -n "$container_id" ]]; then
      container_ids+=("$container_id")
    fi
  done < <(compose ps --all -q)
  if [[ ${#container_ids[@]} -eq 0 ]]; then
    return 1
  fi
  docker inspect "${container_ids[@]}" | python3 -c '
import json, sys
items = []
for container in json.load(sys.stdin):
    labels = container.get("Config", {}).get("Labels", {}) or {}
    items.append({
        "service": labels.get("com.docker.compose.service", "unknown"),
        "image_reference": container.get("Config", {}).get("Image", "unknown"),
        "image_id": container.get("Image", "unknown"),
    })
items.sort(key=lambda item: item["service"])
print(json.dumps({"container_count": len(items), "container_images": items}, separators=(",", ":")))
'
}

external_runner() {
  local python_bin="${E2E_PYTHON:-}"
  if [[ -z "$python_bin" ]]; then
    if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
      python_bin="$ROOT_DIR/.venv/bin/python"
    else
      python_bin=python3
    fi
  fi
  PYTHONPATH="$ROOT_DIR/query_api/app:$ROOT_DIR/indexing_service/app${PYTHONPATH:+:$PYTHONPATH}" \
    "$python_bin" "$RUNNER" --evidence-file "$EVIDENCE_FILE" "$@"
}

require_executable_hook() {
  local variable_name=$1
  local value=${!variable_name:-}
  if [[ -z "$value" || ! -x "$value" ]]; then
    return 1
  fi
}

run_compose_mode() {
  initialize_evidence docker-compose
  local static_started
  static_started=$(milliseconds)
  if ! static_validate true; then
    record_result static_harness_validation failed \
      "Static harness or Compose configuration validation failed" \
      "$(($(milliseconds) - static_started))"
    return 1
  fi
  record_result static_harness_validation passed "" \
    "$(($(milliseconds) - static_started))" \
    '{"python_compile":true,"shell_parse":true,"unit_test_suite":true,"compose_config_resolved":true}'
  if ! docker info >/dev/null 2>&1; then
    for scenario in platform_readiness oidc_tenant_isolation \
      postgres_migration_cas_and_sequence \
      typesense_secret_ref_rotation \
      config_mutation_without_notification config_convergence_without_redis \
      restart_convergence_without_redis kafka_worker_v2_delivery \
      otlp_w3c_trace_continuity \
      typesense_dual_write_backfill_cutover_rollback typesense_tls_transport; do
      record_not_run "$scenario" "Docker daemon is unavailable; live evidence was not executed"
    done
    return 2
  fi
  # Set before `up`: a failed image build or partial create must still trigger
  # non-destructive removal of any containers/network already created.
  COMPOSE_STARTED=true
  if ! compose up -d --build postgres redis kafka typesense-a typesense-b migrate \
      runtime-secret-init \
      query-a query-b indexing-service tls-init otel-capture typesense-tls; then
    record_result compose_stack_bootstrap failed "Compose dependency startup failed"
    return 1
  fi
  local image_provenance
  if ! image_provenance=$(compose_image_provenance); then
    record_result compose_stack_bootstrap failed \
      "Could not capture container image provenance after startup"
    return 1
  fi
  record_result compose_stack_bootstrap passed "" 0 "$image_provenance"
  compose_runner platform-readiness
  compose_runner oidc-tenant-isolation
  compose_runner postgres-contract
  compose_runner secret-ref-rotation
  compose logs --no-color query-a query-b indexing-service >"$SECRET_LOG_FILE"
  compose_runner secret-ref-log-check
  rm -f "$SECRET_LOG_FILE"

  REDIS_STOPPED=true
  compose stop redis
  compose_runner mutate-without-notification
  compose_runner assert-convergence
  compose restart query-b
  compose_runner assert-restart-convergence
  compose start redis
  REDIS_STOPPED=false

  compose_runner kafka-worker-v2
  compose_runner otlp-trace
  compose_runner routing-lifecycle
  compose_runner tls-smoke
  compose_runner finalize --require-complete
  FINALIZED=true
}

run_external_mode() {
  initialize_evidence external-services
  local required_variable
  for required_variable in E2E_POSTGRES_URL E2E_REDIS_URL \
      E2E_QUERY_A_URL E2E_QUERY_B_URL \
      E2E_WORKER_HEALTH_URL E2E_TYPESENSE_A_URL E2E_TYPESENSE_B_URL \
      E2E_TYPESENSE_API_KEY \
      E2E_TYPESENSE_A_API_KEY E2E_TYPESENSE_B_API_KEY \
      E2E_OTLP_CAPTURE_URL E2E_OTLP_CA_FILE \
      E2E_TENANT_A_TOKEN E2E_TENANT_B_TOKEN E2E_TENANT_NO_CLAIM_TOKEN \
      E2E_TYPESENSE_SECRET_FILE \
      E2E_ADMIN_API_KEY E2E_DATA_API_KEY; do
    if [[ -z "${!required_variable:-}" ]]; then
      record_result external_configuration failed \
        "Required external variable $required_variable is missing"
      return 64
    fi
  done
  local static_started
  static_started=$(milliseconds)
  if ! static_validate false; then
    record_result static_harness_validation failed \
      "Static harness validation failed" "$(($(milliseconds) - static_started))"
    return 1
  fi
  record_result static_harness_validation passed "" \
    "$(($(milliseconds) - static_started))" \
    '{"python_compile":true,"shell_parse":true,"unit_test_suite":true,"compose_config_required":false}'
  external_runner platform-readiness
  external_runner oidc-tenant-isolation
  external_runner postgres-contract
  if require_executable_hook E2E_CAPTURE_APPLICATION_LOGS_HOOK; then
    external_runner secret-ref-rotation
    "$E2E_CAPTURE_APPLICATION_LOGS_HOOK" "$SECRET_LOG_FILE"
    export E2E_APPLICATION_LOGS_FILE="$SECRET_LOG_FILE"
    external_runner secret-ref-log-check
    unset E2E_APPLICATION_LOGS_FILE
    rm -f "$SECRET_LOG_FILE"
  else
    record_not_run typesense_secret_ref_rotation \
      "External mode requires E2E_CAPTURE_APPLICATION_LOGS_HOOK"
  fi

  if require_executable_hook E2E_STOP_REDIS_HOOK && \
     require_executable_hook E2E_START_REDIS_HOOK && \
     require_executable_hook E2E_RESTART_QUERY_B_HOOK; then
    EXTERNAL_REDIS_STOPPED=true
    "$E2E_STOP_REDIS_HOOK"
    external_runner mutate-without-notification
    external_runner assert-convergence
    "$E2E_RESTART_QUERY_B_HOOK"
    external_runner assert-restart-convergence
    "$E2E_START_REDIS_HOOK"
    EXTERNAL_REDIS_STOPPED=false
  else
    record_not_run config_mutation_without_notification \
      "External mode requires executable stop/start Redis and restart Query B hooks"
    record_not_run config_convergence_without_redis \
      "Redis notification-loss probe was not authorized by operator hooks"
    record_not_run restart_convergence_without_redis \
      "Query B restart hook was not configured"
  fi

  external_runner kafka-worker-v2
  external_runner otlp-trace
  external_runner routing-lifecycle
  external_runner tls-smoke || true
  external_runner finalize --require-complete
  FINALIZED=true
}

run_static_mode() {
  initialize_evidence docker-client-only
  local static_started
  static_started=$(milliseconds)
  if static_validate true; then
    record_result static_harness_validation passed "" \
      "$(($(milliseconds) - static_started))" \
      '{"python_compile":true,"shell_parse":true,"unit_test_suite":true,"compose_config_resolved":true}'
  else
    record_result static_harness_validation failed \
      "Python, shell, unit, or Compose static validation failed" \
      "$(($(milliseconds) - static_started))"
    return 1
  fi
  for scenario in platform_readiness oidc_tenant_isolation \
    postgres_migration_cas_and_sequence \
    typesense_secret_ref_rotation \
    config_mutation_without_notification config_convergence_without_redis \
    restart_convergence_without_redis kafka_worker_v2_delivery \
    otlp_w3c_trace_continuity \
    typesense_dual_write_backfill_cutover_rollback typesense_tls_transport; do
    record_not_run "$scenario" \
      "No container daemon, Kubernetes context, or external dependency endpoints were available"
  done
  python3 "$RUNNER" --evidence-file "$EVIDENCE_FILE" finalize >/dev/null
  FINALIZED=true
}

case "$MODE" in
  compose) run_compose_mode ;;
  external) run_external_mode ;;
  static) run_static_mode ;;
  *) printf 'E2E_DEPENDENCY_MODE must be compose, external, or static\n' >&2; exit 64 ;;
esac
