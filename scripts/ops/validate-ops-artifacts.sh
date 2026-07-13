#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

# Multi-architecture upstream images are pinned to immutable manifest digests.
# They make PromQL and Alertmanager semantic validation reproducible even when
# the developer host does not have the Go binaries installed.
PROMTOOL_IMAGE="${PROMTOOL_IMAGE:-prom/prometheus@sha256:a75c5a35bc21d7afe69551eefa3cb1e1fb1775fe759408007a66b54ec3de1f29}"
AMTOOL_IMAGE="${AMTOOL_IMAGE:-prom/alertmanager@sha256:9e082985f56f4c8c9f724e18f2288c6708f472e56a5286b8863d080434ea065d}"
RUNBOOK_BRANCH="${RUNBOOK_BRANCH:-master}"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

for script in scripts/ops/*.sh; do
  bash -n "$script"
done
for script in ops/dr/*.sh; do
  bash -n "$script"
done
printf '%s\n' 'PASS: Bash syntax'

python_bin="${OPS_PYTHON:-python3}"
"$python_bin" -m py_compile \
  scripts/ops/outbox_retention.py \
  scripts/ops/audit_delivery_worker.py \
  query_api/app/control_plane/audit_delivery_worker.py
"$python_bin" -m pytest -q tests/ops
printf '%s\n' 'PASS: retention CLI unit and guardrail tests'

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  DR_POSTGRES_PASSWORD=static-placeholder \
  DR_WORK_DIR=/tmp \
  DR_HOST_UID="$(id -u)" \
  DR_HOST_GID="$(id -g)" \
    docker compose -f ops/dr/docker-compose.dr.yml config --quiet
  printf '%s\n' 'PASS: DR Compose contract'
else
  printf '%s\n' 'WARN: Docker Compose unavailable; DR Compose contract was not rendered.' >&2
fi

jq empty monitoring/grafana/provisioning/dashboards/imposbro-enterprise-sre.json
printf '%s\n' 'PASS: Grafana dashboard JSON syntax'

yaml_files=(
  monitoring/alertmanager/alertmanager.enterprise.example.yml
  monitoring/json-exporter/imposbro-query-health.yml
  monitoring/postgres-exporter/queries.yml
  monitoring/prometheus/prometheus.enterprise.yml
  monitoring/prometheus/rules/imposbro-enterprise.rules.yml
  monitoring/prometheus/tests/imposbro-enterprise.rules.test.yml
)
if command -v ruby >/dev/null 2>&1; then
  ruby -e 'require "psych"; ARGV.each { |path| Psych.parse_file(path) or abort("empty YAML: #{path}") }' "${yaml_files[@]}"
  printf '%s\n' 'PASS: YAML syntax (Ruby Psych parser)'
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import yaml' >/dev/null 2>&1; then
  python3 -c 'import sys, yaml; [yaml.safe_load(open(path, encoding="utf-8")) for path in sys.argv[1:]]' "${yaml_files[@]}"
  printf '%s\n' 'PASS: YAML syntax (PyYAML parser)'
else
  printf '%s\n' 'WARN: no Ruby Psych or Python PyYAML; generic YAML syntax was not parsed.' >&2
fi

promtool_cmd=()
if command -v promtool >/dev/null 2>&1; then
  promtool_cmd=(promtool)
elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  promtool_cmd=(docker run --rm --volume "$REPO_ROOT:/workspace:ro" --workdir /workspace --entrypoint promtool "$PROMTOOL_IMAGE")
fi

if ((${#promtool_cmd[@]})); then
  "${promtool_cmd[@]}" check rules monitoring/prometheus/rules/imposbro-enterprise.rules.yml
  "${promtool_cmd[@]}" check config --syntax-only monitoring/prometheus/prometheus.enterprise.yml
  (
    cd monitoring/prometheus/tests
    if command -v promtool >/dev/null 2>&1; then
      promtool test rules imposbro-enterprise.rules.test.yml
    else
      "${promtool_cmd[@]}" test rules monitoring/prometheus/tests/imposbro-enterprise.rules.test.yml
    fi
  )
  dashboard_rules="$(mktemp "$REPO_ROOT/.imposbro-dashboard-rules.XXXXXX.yml")"
  trap 'rm -f "$dashboard_rules"' EXIT
  {
    printf '%s\n' \
      'groups:' \
      '  - name: imposbro.dashboard.syntax' \
      '    rules:'
    jq -r '.panels[].targets[]?.expr' \
      monitoring/grafana/provisioning/dashboards/imposbro-enterprise-sre.json | \
      awk '{count += 1; print "      - record: imposbro_dashboard_expr_" count; print "        expr: |"; print "          " $0}'
  } > "$dashboard_rules"
  # mktemp defaults to mode 0600. The containerized promtool runs as a
  # non-root user on Linux runners and must be able to read this generated,
  # non-sensitive rules file through the read-only workspace mount.
  chmod 0644 "$dashboard_rules"
  if command -v promtool >/dev/null 2>&1; then
    promtool check rules "$dashboard_rules"
  else
    "${promtool_cmd[@]}" check rules "${dashboard_rules#"$REPO_ROOT/"}"
  fi
  rm -f "$dashboard_rules"
  trap - EXIT
  printf '%s\n' 'PASS: Prometheus rules, unit tests, config, and dashboard PromQL (promtool)'
else
  printf '%s\n' 'WARN: promtool unavailable; PromQL semantic validation was not executed.' >&2
fi

amtool_cmd=()
if command -v amtool >/dev/null 2>&1; then
  amtool_cmd=(amtool)
elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  amtool_cmd=(docker run --rm --volume "$REPO_ROOT:/workspace:ro" --workdir /workspace --entrypoint amtool "$AMTOOL_IMAGE")
fi

if ((${#amtool_cmd[@]})); then
  "${amtool_cmd[@]}" check-config monitoring/alertmanager/alertmanager.enterprise.example.yml
  printf '%s\n' 'PASS: Alertmanager config (amtool)'
else
  printf '%s\n' 'WARN: amtool unavailable; Alertmanager semantic validation was not executed.' >&2
fi

for metric in \
  imposbro_control_plane_outbox_pending \
  imposbro_control_plane_outbox_oldest_unpublished_age_seconds \
  imposbro_indexing_event_outbox_oldest_unpublished_age_seconds; do
  grep -q "$metric" monitoring/postgres-exporter/queries.yml || fail "missing postgres exporter metric $metric"
  grep -q "$metric" monitoring/prometheus/rules/imposbro-enterprise.rules.yml || fail "missing alert rule reference for $metric"
done

for metric in \
  imposbro_query_api_control_plane_authoritative_revision \
  imposbro_query_api_control_plane_applied_revision; do
  grep -q "$metric" monitoring/json-exporter/imposbro-query-health.yml || fail "missing JSON exporter metric $metric"
  grep -q "$metric" monitoring/prometheus/rules/imposbro-enterprise.rules.yml || fail "missing convergence rule reference for $metric"
done
printf '%s\n' 'PASS: exporter-to-alert metric contracts'

for metric in \
  imposbro_routing_rollouts \
  imposbro_routing_rollout_max_phase_age_seconds \
  imposbro_routing_rollout_max_deadline_overrun_seconds; do
  grep -q "$metric" monitoring/postgres-exporter/queries.yml || fail "missing routing exporter metric $metric"
  grep -q "$metric" monitoring/prometheus/rules/imposbro-enterprise.rules.yml || fail "missing routing alert reference for $metric"
  grep -q "$metric" monitoring/grafana/provisioning/dashboards/imposbro-enterprise-sre.json || fail "missing routing dashboard reference for $metric"
done
for metric in indexing_dlq_messages_total kafka_consumergroup_lag; do
  grep -q "$metric" monitoring/prometheus/rules/imposbro-enterprise.rules.yml || fail "missing pipeline alert reference for $metric"
  grep -q "$metric" monitoring/grafana/provisioning/dashboards/imposbro-enterprise-sre.json || fail "missing pipeline dashboard reference for $metric"
done
printf '%s\n' 'PASS: routing, Kafka lag, and DLQ observability contracts'

for metric in \
  imposbro_audit_delivery_head_sequence \
  imposbro_audit_delivery_destinations \
  imposbro_audit_delivery_max_backlog \
  imposbro_audit_delivery_failed_destinations \
  imposbro_audit_delivery_max_failure_attempts; do
  grep -q "$metric" monitoring/postgres-exporter/queries.yml || fail "missing audit delivery exporter metric $metric"
  grep -q "$metric" monitoring/prometheus/rules/imposbro-enterprise.rules.yml || fail "missing audit delivery alert reference for $metric"
done
printf '%s\n' 'PASS: audit delivery checkpoint observability contracts'

while IFS= read -r runbook; do
  [[ -f "$runbook" ]] || fail "alert references missing runbook: $runbook"
done < <(grep -Eo 'docs/runbooks/[a-z0-9-]+\.md' monitoring/prometheus/rules/imposbro-enterprise.rules.yml | sort -u)
printf '%s\n' 'PASS: alert-to-runbook links'

while IFS= read -r branch; do
  [[ "$branch" == "$RUNBOOK_BRANCH" ]] || \
    fail "operational link targets branch '$branch', expected '$RUNBOOK_BRANCH'"
done < <(
  grep -hEo 'https://github\.com/[^/]+/[^/]+/blob/[^/]+/docs/' \
    monitoring/prometheus/rules/imposbro-enterprise.rules.yml \
    monitoring/grafana/provisioning/dashboards/imposbro-enterprise-sre.json |
    sed -E 's#^.*/blob/([^/]+)/docs/$#\1#' |
    sort -u
)
printf '%s\n' "PASS: operational links target default branch $RUNBOOK_BRANCH"

diff -u \
  <(sed -n 's/^      - alert: //p' monitoring/prometheus/rules/imposbro-enterprise.rules.yml | sort) \
  <(sed -n 's/^| `\([^`]*\)` |.*/\1/p' docs/runbooks/README.md | sort)
printf '%s\n' 'PASS: every alert is indexed exactly once by name'

"$SCRIPT_DIR/test-backup-restore-guardrails.sh"

printf '%s\n' 'STATIC VALIDATION COMPLETE'
printf '%s\n' 'NOT EXECUTED BY THIS STATIC COMMAND: live Prometheus reload, Alertmanager delivery, paging, or DR drill; use ops/dr/run-live-postgres-dr.sh for the isolated PostgreSQL exercise.'
