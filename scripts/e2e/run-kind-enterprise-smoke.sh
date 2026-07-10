#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT_DIR"

CLUSTER_NAME=${KIND_CLUSTER_NAME:-imposbro-enterprise}
NAMESPACE=${KIND_NAMESPACE:-imposbro-kind}
RELEASE_NAME=${KIND_RELEASE_NAME:-imposbro-kind}
REGISTRY_NAME=${KIND_REGISTRY_NAME:-imposbro-kind-registry}
REGISTRY_PORT=${KIND_REGISTRY_PORT:-5001}
KIND_NODE_IMAGE="kindest/node:v1.34.0@sha256:7416a61b42b1662ca6ca89f02028ac133a309a2a30ba309614e8ec94d976dc5a"
REGISTRY_IMAGE="registry:2@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373"
EVIDENCE_FILE=${KIND_EVIDENCE_FILE:-$ROOT_DIR/docs/evidence/kind-enterprise-smoke-live-latest.json}
BENCHMARK_PROFILE=${KIND_BENCHMARK_PROFILE:-$ROOT_DIR/ops/kind/benchmark-profile.json}
BENCHMARK_JSON=${KIND_BENCHMARK_JSON:-$ROOT_DIR/docs/evidence/kind-enterprise-benchmark-live-latest.json}
BENCHMARK_MARKDOWN=${KIND_BENCHMARK_MARKDOWN:-$ROOT_DIR/docs/evidence/kind-enterprise-benchmark-live-latest.md}
KEEP_CLUSTER=${KIND_KEEP_CLUSTER:-0}
TIMEOUT=${KIND_TIMEOUT:-600s}
RUN_ID=${KIND_RUN_ID:-"kind-$(date -u +%Y%m%dT%H%M%SZ)-$$"}
TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/imposbro-kind.XXXXXX")
STATE_FILE="$TMP_DIR/evidence-state.json"
RECORDER="$ROOT_DIR/tests/kind/evidence_recorder.py"
RUNTIME_ASSERTIONS="$ROOT_DIR/tests/kind/runtime_assertions.py"
TLS_HOST=kind-api.imposbro.test
PF_PIDS=""
BUILT_TAGS=""
LAST_IMAGE_REF=""
CURRENT_ASSERTION=""
CURRENT_STARTED_MS=0
CURRENT_RECORDED=0
CLUSTER_CREATED=0
REGISTRY_CREATED=0
REGISTRY_CONNECTED=0
REQUIRED_ASSERTIONS="preflight cluster_created immutable_application_images dependencies_ready helm_install_ready migration_init_completed secure_runtime_context network_tls_controls api_data_plane_smoke performance_profile_gate tls_edge_verified single_pod_restart rolling_restart pdb_eviction_blocked workload_node_drain cleanup"

iso_now() {
  python3 -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))'
}

epoch_ms() {
  python3 -c 'import time; print(int(time.time() * 1000))'
}

duration_since() {
  python3 - "$1" <<'PY'
import sys
import time
print(max(0.0, (time.time() * 1000 - int(sys.argv[1])) / 1000))
PY
}

begin_assertion() {
  CURRENT_ASSERTION=$1
  CURRENT_STARTED_MS=$(epoch_ms)
  CURRENT_RECORDED=0
  printf '==> %s\n' "$CURRENT_ASSERTION"
}

record_current() {
  status=$1
  evidence=$2
  if [ -z "$CURRENT_ASSERTION" ] || [ "$CURRENT_RECORDED" -eq 1 ]; then
    return 0
  fi
  if python3 "$RECORDER" contains --state "$STATE_FILE" --id "$CURRENT_ASSERTION"; then
    CURRENT_RECORDED=1
    CURRENT_ASSERTION=""
    return 0
  fi
  python3 "$RECORDER" assert \
    --state "$STATE_FILE" \
    --id "$CURRENT_ASSERTION" \
    --status "$status" \
    --duration "$(duration_since "$CURRENT_STARTED_MS")" \
    --evidence "$evidence"
  CURRENT_RECORDED=1
  CURRENT_ASSERTION=""
}

pass_assertion() {
  record_current passed "$1"
}

on_error() {
  rc=$?
  failed_line=${BASH_LINENO[0]:-unknown}
  set +e
  record_current failed "assertion failed at shell line $failed_line; inspect the local command output"
  set -e
  return "$rc"
}

port_is_free() {
  python3 - "$1" <<'PY'
import socket
import sys
port = int(sys.argv[1])
with socket.socket() as sock:
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        raise SystemExit(1)
PY
}

random_port() {
  python3 - <<'PY'
import socket
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}

profile_value() {
  key=$1
  python3 - "$BENCHMARK_PROFILE" "$key" <<'PY'
import json
import sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
for part in sys.argv[2].split("."):
    value = value[part]
if isinstance(value, bool):
    print("true" if value else "false")
else:
    print(value)
PY
}

publish_benchmark_evidence() {
  source_json=$1
  source_markdown=$2
  destination_json=$3
  destination_markdown=$4
  image_set=$5
  python3 - "$source_json" "$source_markdown" "$destination_json" \
    "$destination_markdown" "$BENCHMARK_PROFILE" "$GIT_COMMIT" "$GIT_DIRTY" \
    "$image_set" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

source_json, source_markdown, destination_json, destination_markdown = map(Path, sys.argv[1:5])
profile_path = Path(sys.argv[5])
commit, dirty_raw, image_set = sys.argv[6:9]
profile = json.loads(profile_path.read_text(encoding="utf-8"))
summary = json.loads(source_json.read_text(encoding="utf-8"))
if summary.get("status") != "passed" or summary.get("slo_violations"):
    raise SystemExit("benchmark cannot be published because its threshold gate did not pass")
metadata = summary.get("metadata", {})
required_metadata = ("environment", "release", "cluster_shape", "helm_values_ref", "image_set")
missing = [key for key in required_metadata if not metadata.get(key)]
if missing:
    raise SystemExit(f"benchmark metadata is incomplete: {missing}")
if metadata["release"] != commit or metadata["image_set"] != image_set:
    raise SystemExit("benchmark release/image metadata does not match the measured deployment")
expected_thresholds = profile["thresholds"].copy()
expected_thresholds.pop("allow_partial")
if metadata.get("slo_thresholds") != expected_thresholds:
    raise SystemExit("benchmark thresholds differ from the declared profile")
if metadata.get("mode", {}).get("allow_partial") is not profile["thresholds"]["allow_partial"]:
    raise SystemExit("benchmark partial-response policy differs from the declared profile")

summary["evidence_type"] = "kind-enterprise-benchmark"
summary["production_certification"] = False
summary["profile"] = profile
summary["git"] = {"commit": commit, "dirty": dirty_raw == "true"}

markdown = source_markdown.read_text(encoding="utf-8").rstrip() + "\n\n"
markdown += "## Certification Boundary\n\n"
markdown += f"- Declared profile: `{profile['id']}`\n"
markdown += f"- Git commit: `{commit}`\n"
markdown += f"- Dirty worktree: `{dirty_raw}`\n"
markdown += "- Production certification: **false**\n"
markdown += "- This is a black-box disposable-kind acceptance profile, not a production capacity claim.\n"

def atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

atomic_write(destination_json, json.dumps(summary, indent=2, sort_keys=True) + "\n")
atomic_write(destination_markdown, markdown)
PY
}

wait_url() {
  url=$1
  attempts=${2:-120}
  i=0
  while [ "$i" -lt "$attempts" ]; do
    if curl --fail --silent --show-error --max-time 3 "$url" >/dev/null 2>&1; then
      return 0
    fi
    i=$((i + 1))
    sleep 1
  done
  return 1
}

continuous_cluster_tls_probe() {
  output=$1
  iterations=$2
  interval=$3
  kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" exec -i kind-tls-probe -- \
    python - "$TLS_HOST" "$iterations" "$interval" >"$output" <<'PY'
import http.client
import ssl
import sys
import time

host = sys.argv[1]
iterations = int(sys.argv[2])
interval = float(sys.argv[3])
context = ssl.create_default_context(cafile="/tls/tls.crt")
failures = 0

for iteration in range(iterations):
    connection = http.client.HTTPSConnection(host, 443, timeout=2, context=context)
    try:
        connection.request("GET", "/ready")
        response = connection.getresponse()
        response.read()
        if response.status != 200:
            failures += 1
            print(
                f"TLS readiness probe {iteration + 1} returned HTTP {response.status}",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001 - preserve the live network failure in output
        failures += 1
        print(f"TLS readiness probe {iteration + 1} failed: {exc!r}", file=sys.stderr)
    finally:
        connection.close()
    time.sleep(interval)

print(failures)
PY
}

start_tls_probe_runner() {
  tls_service_ip=$(kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" get service kind-tls-edge \
    -o jsonpath='{.spec.clusterIP}')
  [ -n "$tls_service_ip" ]
  cat <<EOF | kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: kind-tls-probe
  labels:
    imposbro.dev/test-resource: kind-enterprise-smoke
spec:
  automountServiceAccountToken: false
  enableServiceLinks: false
  restartPolicy: Never
  nodeSelector:
    imposbro.dev/role: infra
  hostAliases:
    - ip: "$tls_service_ip"
      hostnames: ["$TLS_HOST"]
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001
    runAsGroup: 10001
    seccompProfile: {type: RuntimeDefault}
  containers:
    - name: probe
      image: "$QUERY_REF"
      imagePullPolicy: IfNotPresent
      command: [python, -c, "import time; time.sleep(3600)"]
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities: {drop: [ALL]}
      resources:
        requests: {cpu: 10m, memory: 32Mi}
        limits: {cpu: 100m, memory: 128Mi}
      volumeMounts:
        - {name: tls, mountPath: /tls, readOnly: true}
  volumes:
    - name: tls
      secret: {secretName: kind-edge-tls}
EOF
  kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" wait \
    --for=condition=Ready pod/kind-tls-probe --timeout "$TIMEOUT" >/dev/null
}

start_port_forward() {
  service=$1
  local_port=$2
  remote_port=$3
  log_file=$4
  kubectl -n "$NAMESPACE" port-forward "service/$service" \
    "$local_port:$remote_port" --address 127.0.0.1 >"$log_file" 2>&1 &
  pid=$!
  PF_PIDS="$PF_PIDS $pid"
}

build_and_push() {
  component=$1
  context=$2
  repository="localhost:$REGISTRY_PORT/imposbro-$component-kind"
  tag="$repository:$RUN_ID"
  log="$TMP_DIR/build-$component.log"
  docker build --pull=false --tag "$tag" "$context" >"$log" 2>&1
  docker push "$tag" >>"$log" 2>&1
  reference=$(docker image inspect "$tag" --format '{{range .RepoDigests}}{{println .}}{{end}}' \
    | awk -v prefix="$repository@" 'index($0, prefix) == 1 {print; exit}')
  if ! printf '%s\n' "$reference" | grep -Eq '@sha256:[a-f0-9]{64}$'; then
    printf 'No immutable repository digest found for %s\n' "$component" >&2
    return 1
  fi
  BUILT_TAGS="$BUILT_TAGS $tag"
  LAST_IMAGE_REF=$reference
}

cleanup() {
  original_rc=$?
  trap - ERR EXIT INT TERM
  set +e

  for pid in $PF_PIDS; do
    kill "$pid" >/dev/null 2>&1 || true
    wait "$pid" >/dev/null 2>&1 || true
  done

  cleanup_started=$(epoch_ms)
  cluster_deleted=false
  registry_deleted=false
  network_deleted=false
  cleanup_status=completed

  if kind get clusters 2>/dev/null | grep -Fxq "$CLUSTER_NAME"; then
    kubectl --context "kind-$CLUSTER_NAME" get nodes -o name 2>/dev/null \
      | while IFS= read -r node; do
          kubectl --context "kind-$CLUSTER_NAME" uncordon "${node#node/}" >/dev/null 2>&1 || true
        done
  fi

  if [ "$KEEP_CLUSTER" = "1" ]; then
    cleanup_status=retained
  else
    if kind get clusters 2>/dev/null | grep -Fxq "$CLUSTER_NAME"; then
      kind delete cluster --name "$CLUSTER_NAME" >/dev/null 2>&1
    fi
    if ! kind get clusters 2>/dev/null | grep -Fxq "$CLUSTER_NAME"; then
      cluster_deleted=true
    else
      cleanup_status=failed
    fi
    if docker inspect "$REGISTRY_NAME" >/dev/null 2>&1; then
      docker rm -f "$REGISTRY_NAME" >/dev/null 2>&1
    fi
    if ! docker inspect "$REGISTRY_NAME" >/dev/null 2>&1; then
      registry_deleted=true
    else
      cleanup_status=failed
    fi
    if ! kind get clusters 2>/dev/null | grep -q . && docker network inspect kind >/dev/null 2>&1; then
      docker network rm kind >/dev/null 2>&1 || true
    fi
    if ! docker network inspect kind >/dev/null 2>&1; then
      network_deleted=true
    else
      cleanup_status=failed
    fi
    for tag in $BUILT_TAGS; do
      docker image rm "$tag" >/dev/null 2>&1 || true
    done
  fi

  if [ -f "$STATE_FILE" ]; then
    CURRENT_ASSERTION=cleanup
    CURRENT_STARTED_MS=$cleanup_started
    CURRENT_RECORDED=0
    if [ "$cleanup_status" = completed ]; then
      record_current passed "disposable kind cluster and local registry removed"
    elif [ "$cleanup_status" = retained ]; then
      record_current not_run "resources retained by explicit operator setting"
    else
      record_current failed "one or more disposable resources could not be removed"
    fi

    final_status=passed
    if [ "$original_rc" -ne 0 ] || [ "$cleanup_status" = failed ]; then
      final_status=failed
      original_rc=1
    elif [ "$cleanup_status" = retained ]; then
      final_status=incomplete
    fi
    python3 "$RECORDER" finish \
      --state "$STATE_FILE" \
      --output "$EVIDENCE_FILE" \
      --status "$final_status" \
      --finished-at "$(iso_now)" \
      --cleanup-status "$cleanup_status" \
      --cluster-deleted "$cluster_deleted" \
      --registry-deleted "$registry_deleted" \
      --network-deleted "$network_deleted"
    printf 'Evidence: %s\n' "$EVIDENCE_FILE"
  fi

  rm -rf "$TMP_DIR"
  exit "$original_rc"
}

trap on_error ERR
trap cleanup EXIT INT TERM

for command in docker kind kubectl helm openssl curl python3 git awk grep; do
  if ! command -v "$command" >/dev/null 2>&1; then
    printf 'Required command not found: %s\n' "$command" >&2
    exit 2
  fi
done

GIT_COMMIT=$(git rev-parse HEAD)
if [ -z "$(git status --porcelain --untracked-files=normal)" ]; then
  GIT_DIRTY=false
else
  GIT_DIRTY=true
fi
DOCKER_VERSION=$(docker version --format '{{.Client.Version}}/{{.Server.Version}}')
KIND_VERSION=$(kind version | awk '{print $2}')
KUBECTL_VERSION=$(kubectl version --client -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["clientVersion"]["gitVersion"])')
HELM_VERSION=$(helm version --short)

RECORDER_INIT_ARGS=""
for assertion_id in $REQUIRED_ASSERTIONS; do
  RECORDER_INIT_ARGS="$RECORDER_INIT_ARGS --required-assertion $assertion_id"
done
# shellcheck disable=SC2086 # assertion IDs are validated constants, expanded as argv pairs.
python3 "$RECORDER" init \
  --state "$STATE_FILE" \
  --run-id "$RUN_ID" \
  --started-at "$(iso_now)" \
  --commit "$GIT_COMMIT" \
  --dirty "$GIT_DIRTY" \
  --cluster-name "$CLUSTER_NAME" \
  --namespace "$NAMESPACE" \
  --node-image "$KIND_NODE_IMAGE" \
  --tool "docker=$DOCKER_VERSION" \
  --tool "kind=$KIND_VERSION" \
  --tool "kubectl=$KUBECTL_VERSION" \
  --tool "helm=$HELM_VERSION" \
  $RECORDER_INIT_ARGS

begin_assertion preflight
docker info >/dev/null
port_is_free "$REGISTRY_PORT"
if kind get clusters 2>/dev/null | grep -Fxq "$CLUSTER_NAME"; then
  printf 'Refusing to replace existing kind cluster %s\n' "$CLUSTER_NAME" >&2
  false
fi
if docker inspect "$REGISTRY_NAME" >/dev/null 2>&1; then
  printf 'Refusing to replace existing container %s\n' "$REGISTRY_NAME" >&2
  false
fi
RUNNING_COMPOSE=$(docker ps --filter label=com.docker.compose.project --format '{{.Label "com.docker.compose.project"}}' \
  | grep -E '^imposbro' || true)
if [ -n "$RUNNING_COMPOSE" ]; then
  printf 'Refusing to contend with a running IMPOSBRO Compose project: %s\n' "$RUNNING_COMPOSE" >&2
  false
fi
pass_assertion "toolchain reachable, Docker exclusive guard passed, target names unused"

begin_assertion cluster_created
docker run --detach --restart=always \
  --publish "127.0.0.1:$REGISTRY_PORT:5000" \
  --name "$REGISTRY_NAME" \
  --label imposbro.dev/owner=kind-enterprise-smoke \
  "$REGISTRY_IMAGE" >/dev/null
REGISTRY_CREATED=1
kind create cluster \
  --name "$CLUSTER_NAME" \
  --image "$KIND_NODE_IMAGE" \
  --config "$ROOT_DIR/ops/kind/cluster.yaml" \
  --wait 300s
CLUSTER_CREATED=1
docker network connect kind "$REGISTRY_NAME"
REGISTRY_CONNECTED=1
for node in $(kind get nodes --name "$CLUSTER_NAME"); do
  registry_dir="/etc/containerd/certs.d/localhost:$REGISTRY_PORT"
  docker exec "$node" mkdir -p "$registry_dir"
  printf 'server = "http://localhost:%s"\n[host."http://%s:5000"]\n  capabilities = ["pull", "resolve"]\n' \
    "$REGISTRY_PORT" "$REGISTRY_NAME" \
    | docker exec -i "$node" sh -c "cp /dev/stdin '$registry_dir/hosts.toml'"
done
kubectl --context "kind-$CLUSTER_NAME" create configmap local-registry-hosting \
  --namespace kube-public \
  --from-literal="localRegistryHosting.v1=host: localhost:$REGISTRY_PORT" >/dev/null
NODE_COUNT=$(kubectl --context "kind-$CLUSTER_NAME" get nodes --no-headers | wc -l | tr -d ' ')
[ "$NODE_COUNT" -eq 5 ]
[ "$(kubectl --context "kind-$CLUSTER_NAME" get nodes -l imposbro.dev/role=infra --no-headers | wc -l | tr -d ' ')" -eq 1 ]
[ "$(kubectl --context "kind-$CLUSTER_NAME" get nodes -l imposbro.dev/role=workload --no-headers | wc -l | tr -d ' ')" -eq 3 ]
pass_assertion "five-node cluster ready: one control-plane, one infra worker, three workload workers"

begin_assertion immutable_application_images
build_and_push query "$ROOT_DIR/query_api"
QUERY_REF=$LAST_IMAGE_REF
build_and_push indexing "$ROOT_DIR/indexing_service"
INDEXING_REF=$LAST_IMAGE_REF
build_and_push admin "$ROOT_DIR/admin_ui"
ADMIN_REF=$LAST_IMAGE_REF
pass_assertion "three locally built application images pushed to the isolated registry with immutable digests"

begin_assertion dependencies_ready
kubectl --context "kind-$CLUSTER_NAME" create namespace "$NAMESPACE" >/dev/null
POSTGRES_PASSWORD=$(openssl rand -hex 24)
TYPESENSE_KEY=$(openssl rand -hex 24)
ADMIN_KEY=$(openssl rand -hex 24)
DATA_KEY=$(openssl rand -hex 24)
SESSION_VALUE=$(openssl rand -hex 32)
kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" create secret generic kind-dependencies \
  --from-literal="POSTGRES_PASSWORD=$POSTGRES_PASSWORD" \
  --from-literal="TYPESENSE_API_KEY=$TYPESENSE_KEY" >/dev/null
DATABASE_URL="postgresql+psycopg://imposbro:$POSTGRES_PASSWORD@postgres:5432/imposbro"
kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" create secret generic imposbro-kind-runtime \
  --from-literal="CONTROL_PLANE_DATABASE_URL=$DATABASE_URL" \
  --from-literal="REDIS_URL=redis://redis:6379/0" \
  --from-literal="INTERNAL_STATE_API_KEY=$TYPESENSE_KEY" \
  --from-literal="DEFAULT_DATA_CLUSTER_API_KEY=$TYPESENSE_KEY" \
  --from-literal="DEFAULT_DATA2_CLUSTER_API_KEY=$TYPESENSE_KEY" \
  --from-literal="ADMIN_API_KEY=$ADMIN_KEY" \
  --from-literal="DATA_API_KEY=$DATA_KEY" \
  --from-literal="INTERNAL_QUERY_API_ADMIN_API_KEY=$ADMIN_KEY" \
  --from-literal="INTERNAL_QUERY_API_DATA_API_KEY=$DATA_KEY" \
  --from-literal="ADMIN_UI_SESSION_SECRET=$SESSION_VALUE" >/dev/null
openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 1 \
  -subj "/CN=$TLS_HOST" \
  -addext "subjectAltName=DNS:$TLS_HOST,DNS:kind-admin.imposbro.test" \
  -keyout "$TMP_DIR/tls.key" -out "$TMP_DIR/tls.crt" >/dev/null 2>&1
kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" create secret tls kind-edge-tls \
  --key "$TMP_DIR/tls.key" --cert "$TMP_DIR/tls.crt" >/dev/null
kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" apply \
  -f "$ROOT_DIR/ops/kind/dependencies.yaml" >/dev/null
for deployment in postgres redis kafka typesense-a typesense-b; do
  kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" rollout status \
    "deployment/$deployment" --timeout "$TIMEOUT"
done
pass_assertion "PostgreSQL, Redis, Kafka, and two independent Typesense services are ready on the isolated infra node"

begin_assertion helm_install_ready
helm --kube-context "kind-$CLUSTER_NAME" upgrade --install "$RELEASE_NAME" "$ROOT_DIR/helm" \
  --namespace "$NAMESPACE" \
  --values "$ROOT_DIR/ops/kind/values.yaml" \
  --set-string "queryApi.image=$QUERY_REF" \
  --set-string "indexingService.image=$INDEXING_REF" \
  --set-string "adminUi.image=$ADMIN_REF" \
  --atomic --wait --timeout "$TIMEOUT"
kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" apply \
  -f "$ROOT_DIR/ops/kind/tls-edge.yaml" >/dev/null
kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" rollout status \
  deployment/kind-tls-edge --timeout "$TIMEOUT"
kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" get pods \
  -l "app.kubernetes.io/instance=$RELEASE_NAME" -o json \
  | python3 "$RUNTIME_ASSERTIONS" placement
pass_assertion "Helm release converged with two ready replicas per component and cross-node placement"

begin_assertion migration_init_completed
kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" get pods \
  -l "app.kubernetes.io/instance=$RELEASE_NAME,app.kubernetes.io/component=query-api" -o json \
  | python3 "$RUNTIME_ASSERTIONS" migrations
POSTGRES_POD=$(kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" get pods \
  -l app.kubernetes.io/name=postgres -o jsonpath='{.items[0].metadata.name}')
MIGRATION_REV=$(kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" exec "$POSTGRES_POD" -- \
  sh -ec 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U imposbro -d imposbro -Atc "select version_num from alembic_version"')
[ -n "$MIGRATION_REV" ]
pass_assertion "both migration init containers completed and PostgreSQL reports an Alembic revision"

begin_assertion secure_runtime_context
APP_SELECTOR="app.kubernetes.io/instance=$RELEASE_NAME"
kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" get pods -l "$APP_SELECTOR" -o json \
  | python3 "$RUNTIME_ASSERTIONS" security
for component in query-api admin-ui indexing-service; do
  pod=$(kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" get pods \
    -l "$APP_SELECTOR,app.kubernetes.io/component=$component" \
    -o jsonpath='{.items[0].metadata.name}')
  uid=$(kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" exec "$pod" -- id -u)
  [ "$uid" -ne 0 ]
  kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" exec "$pod" -- \
    sh -ec 'test ! -e /var/run/secrets/kubernetes.io/serviceaccount/token'
done
pass_assertion "all application containers run non-root with immutable roots, dropped capabilities, and no mounted service-account token"

begin_assertion network_tls_controls
[ "$(kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" get networkpolicy --no-headers | wc -l | tr -d ' ')" -eq 3 ]
[ "$(kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" get poddisruptionbudget --no-headers | wc -l | tr -d ' ')" -eq 3 ]
[ "$(kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" get ingress --no-headers | wc -l | tr -d ' ')" -eq 2 ]
kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" get ingress -o json \
  | python3 -c 'import json,sys; p=json.load(sys.stdin); assert len(p["items"]) == 2; assert all(i["spec"].get("tls") for i in p["items"])'
pass_assertion "three default-deny application policies, three disruption budgets, and two TLS ingress contracts are active"

QUERY_PORT=$(random_port)
start_port_forward "$RELEASE_NAME-imposbro-search-query-api" "$QUERY_PORT" 80 "$TMP_DIR/query-port-forward.log"
wait_url "http://127.0.0.1:$QUERY_PORT/ready" 180

begin_assertion api_data_plane_smoke
SMOKE_ADMIN_API_KEY="$ADMIN_KEY" \
SMOKE_INGEST_API_KEY="$DATA_KEY" \
SMOKE_SEARCH_API_KEY="$DATA_KEY" \
python3 "$ROOT_DIR/scripts/smoke-vector-search.py" \
  --query-api-url "http://127.0.0.1:$QUERY_PORT" \
  --skip-admin-ui \
  --timeout-seconds 180
pass_assertion "collection create, routing preview, Kafka ingest, two-cluster vector search, and cleanup completed"

begin_assertion performance_profile_gate
BENCHMARK_DOCUMENTS=$(profile_value workload.documents)
BENCHMARK_INGEST_CONCURRENCY=$(profile_value workload.ingest_concurrency)
BENCHMARK_INGEST_BATCH_SIZE=$(profile_value workload.ingest_batch_size)
BENCHMARK_SEARCH_REQUESTS=$(profile_value workload.search_requests)
BENCHMARK_SEARCH_CONCURRENCY=$(profile_value workload.search_concurrency)
BENCHMARK_SEARCH_PER_PAGE=$(profile_value workload.search_per_page)
BENCHMARK_TIMEOUT_SECONDS=$(profile_value workload.timeout_seconds)
BENCHMARK_SEED=$(profile_value workload.seed)
BENCHMARK_MIN_INGEST_DOCS_PER_SECOND=$(profile_value thresholds.min_ingest_docs_per_second)
BENCHMARK_MAX_INDEXING_VISIBLE_SECONDS=$(profile_value thresholds.max_indexing_visible_seconds)
BENCHMARK_MAX_SEARCH_P95_MS=$(profile_value thresholds.max_search_p95_ms)
BENCHMARK_MAX_SEARCH_ERROR_RATE=$(profile_value thresholds.max_search_error_rate)
BENCHMARK_CLUSTER_SHAPE=$(profile_value cluster_shape)
BENCHMARK_PROFILE_ID=$(profile_value id)
BENCHMARK_IMAGE_SET="query=$QUERY_REF,indexing=$INDEXING_REF,admin=$ADMIN_REF"
BENCHMARK_TMP_JSON="$TMP_DIR/kind-benchmark.json"
BENCHMARK_TMP_MARKDOWN="$TMP_DIR/kind-benchmark.md"
SMOKE_ADMIN_API_KEY="$ADMIN_KEY" \
SMOKE_INGEST_API_KEY="$DATA_KEY" \
SMOKE_SEARCH_API_KEY="$DATA_KEY" \
BENCHMARK_DOCUMENTS="$BENCHMARK_DOCUMENTS" \
BENCHMARK_INGEST_CONCURRENCY="$BENCHMARK_INGEST_CONCURRENCY" \
BENCHMARK_INGEST_BATCH_SIZE="$BENCHMARK_INGEST_BATCH_SIZE" \
BENCHMARK_SEARCH_REQUESTS="$BENCHMARK_SEARCH_REQUESTS" \
BENCHMARK_SEARCH_CONCURRENCY="$BENCHMARK_SEARCH_CONCURRENCY" \
BENCHMARK_SEARCH_PER_PAGE="$BENCHMARK_SEARCH_PER_PAGE" \
BENCHMARK_TIMEOUT_SECONDS="$BENCHMARK_TIMEOUT_SECONDS" \
BENCHMARK_SEED="$BENCHMARK_SEED" \
BENCHMARK_MIN_INGEST_DOCS_PER_SECOND="$BENCHMARK_MIN_INGEST_DOCS_PER_SECOND" \
BENCHMARK_MAX_INDEXING_VISIBLE_SECONDS="$BENCHMARK_MAX_INDEXING_VISIBLE_SECONDS" \
BENCHMARK_MAX_SEARCH_P95_MS="$BENCHMARK_MAX_SEARCH_P95_MS" \
BENCHMARK_MAX_SEARCH_ERROR_RATE="$BENCHMARK_MAX_SEARCH_ERROR_RATE" \
BENCHMARK_ALLOW_PARTIAL=false \
BENCHMARK_ENVIRONMENT=kind-disposable-ent12 \
BENCHMARK_RELEASE="$GIT_COMMIT" \
BENCHMARK_CLUSTER_SHAPE="$BENCHMARK_CLUSTER_SHAPE" \
BENCHMARK_HELM_VALUES_REF=ops/kind/values.yaml \
BENCHMARK_IMAGE_SET="$BENCHMARK_IMAGE_SET" \
BENCHMARK_EVIDENCE_NOTES="profile=$BENCHMARK_PROFILE_ID;dirty_worktree=$GIT_DIRTY;black_box=true" \
BENCHMARK_OUTPUT_JSON="$BENCHMARK_TMP_JSON" \
BENCHMARK_OUTPUT_MARKDOWN="$BENCHMARK_TMP_MARKDOWN" \
python3 "$ROOT_DIR/scripts/benchmark-k8s.py" \
  --query-api-url "http://127.0.0.1:$QUERY_PORT"
publish_benchmark_evidence "$BENCHMARK_TMP_JSON" "$BENCHMARK_TMP_MARKDOWN" \
  "$BENCHMARK_JSON" "$BENCHMARK_MARKDOWN" "$BENCHMARK_IMAGE_SET"
python3 - "$BENCHMARK_JSON" <<'PY'
import json
import sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
assert payload["status"] == "passed"
assert payload["production_certification"] is False
assert not payload["slo_violations"]
PY
pass_assertion "declared black-box Kubernetes performance profile met every throughput, convergence, latency, error, and partial-response threshold"

begin_assertion tls_edge_verified
TLS_PORT=$(random_port)
start_port_forward kind-tls-edge "$TLS_PORT" 443 "$TMP_DIR/tls-port-forward.log"
i=0
until curl --fail --silent --show-error --max-time 5 \
  --cacert "$TMP_DIR/tls.crt" \
  --resolve "$TLS_HOST:$TLS_PORT:127.0.0.1" \
  "https://$TLS_HOST:$TLS_PORT/ready" >"$TMP_DIR/tls-ready.json"; do
  i=$((i + 1))
  [ "$i" -lt 120 ]
  sleep 1
done
python3 - "$TMP_DIR/tls-ready.json" <<'PY'
import json
import sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
assert payload.get("ready") is True
PY
pass_assertion "HTTPS service path validated with hostname verification and the run-specific trust anchor"
start_tls_probe_runner

begin_assertion single_pod_restart
OLD_QUERY_POD=$(kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" get pods \
  -l "$APP_SELECTOR,app.kubernetes.io/component=query-api" \
  -o jsonpath='{.items[0].metadata.name}')
continuous_cluster_tls_probe "$TMP_DIR/restart-failures" 60 0.25 &
RESTART_PROBE_PID=$!
kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" delete pod "$OLD_QUERY_POD" --wait=false >/dev/null
kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" rollout status \
  "deployment/$RELEASE_NAME-imposbro-search-query-api" --timeout "$TIMEOUT"
wait "$RESTART_PROBE_PID"
read -r RESTART_FAILURES <"$TMP_DIR/restart-failures"
[ "$RESTART_FAILURES" -eq 0 ]
if kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" get pod "$OLD_QUERY_POD" >/dev/null 2>&1; then
  false
fi
pass_assertion "one Query API pod was replaced with zero failed readiness probes through the service"

begin_assertion rolling_restart
continuous_cluster_tls_probe "$TMP_DIR/rollout-failures" 120 0.25 &
ROLLOUT_PROBE_PID=$!
for component in query-api admin-ui indexing-service; do
  kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" rollout restart \
    "deployment/$RELEASE_NAME-imposbro-search-$component" >/dev/null
done
for component in query-api admin-ui indexing-service; do
  kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" rollout status \
    "deployment/$RELEASE_NAME-imposbro-search-$component" --timeout "$TIMEOUT"
done
wait "$ROLLOUT_PROBE_PID"
read -r ROLLOUT_FAILURES <"$TMP_DIR/rollout-failures"
[ "$ROLLOUT_FAILURES" -eq 0 ]
pass_assertion "rolling restart completed for all three deployments with zero failed service probes"

begin_assertion pdb_eviction_blocked
PDB_NAME="$RELEASE_NAME-imposbro-search-query-api"
kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" patch poddisruptionbudget "$PDB_NAME" \
  --type merge -p '{"spec":{"minAvailable":2}}' >/dev/null
i=0
while [ "$i" -lt 60 ]; do
  ALLOWED=$(kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" get poddisruptionbudget "$PDB_NAME" \
    -o jsonpath='{.status.disruptionsAllowed}')
  [ "$ALLOWED" = "0" ] && break
  i=$((i + 1))
  sleep 1
done
[ "$ALLOWED" = "0" ]
EVICT_POD=$(kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" get pods \
  -l "$APP_SELECTOR,app.kubernetes.io/component=query-api" \
  -o jsonpath='{.items[0].metadata.name}')
printf '{"apiVersion":"policy/v1","kind":"Eviction","metadata":{"name":"%s","namespace":"%s"}}\n' \
  "$EVICT_POD" "$NAMESPACE" >"$TMP_DIR/eviction.json"
if EVICTION_OUTPUT=$(trap - ERR; kubectl --context "kind-$CLUSTER_NAME" create --raw \
  "/api/v1/namespaces/$NAMESPACE/pods/$EVICT_POD/eviction" \
  -f "$TMP_DIR/eviction.json" 2>&1); then
  EVICTION_RC=0
else
  EVICTION_RC=$?
fi
kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" patch poddisruptionbudget "$PDB_NAME" \
  --type merge -p '{"spec":{"minAvailable":1}}' >/dev/null
[ "$EVICTION_RC" -ne 0 ]
printf '%s\n' "$EVICTION_OUTPUT" | grep -Eqi 'disruption budget|too many requests|429|cannot evict'
pass_assertion "the eviction API rejected a voluntary disruption when the budget allowed zero disruptions"

begin_assertion workload_node_drain
DRAIN_NODE=$(kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" get pods \
  -l "$APP_SELECTOR,app.kubernetes.io/component=query-api" \
  -o jsonpath='{.items[0].spec.nodeName}')
[ "$(kubectl --context "kind-$CLUSTER_NAME" get node "$DRAIN_NODE" \
  -o jsonpath='{.metadata.labels.imposbro\.dev/role}')" = workload ]
continuous_cluster_tls_probe "$TMP_DIR/drain-failures" 240 0.25 &
DRAIN_PROBE_PID=$!
kubectl --context "kind-$CLUSTER_NAME" cordon "$DRAIN_NODE" >/dev/null
kubectl --context "kind-$CLUSTER_NAME" drain "$DRAIN_NODE" \
  --ignore-daemonsets --delete-emptydir-data --force --timeout 240s >/dev/null
[ "$(kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" get pods \
  -l "$APP_SELECTOR,app.kubernetes.io/component=query-api" \
  --field-selector=status.phase=Running --no-headers | wc -l | tr -d ' ')" -ge 1 ]
kubectl --context "kind-$CLUSTER_NAME" uncordon "$DRAIN_NODE" >/dev/null
for component in query-api admin-ui indexing-service; do
  kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" rollout status \
    "deployment/$RELEASE_NAME-imposbro-search-$component" --timeout "$TIMEOUT"
done
wait "$DRAIN_PROBE_PID"
read -r DRAIN_FAILURES <"$TMP_DIR/drain-failures"
[ "$DRAIN_FAILURES" -eq 0 ]
kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" get pods -l "$APP_SELECTOR" -o json \
  | python3 "$RUNTIME_ASSERTIONS" placement
continuous_cluster_tls_probe "$TMP_DIR/post-drain-failures" 1 0
read -r POST_DRAIN_FAILURES <"$TMP_DIR/post-drain-failures"
[ "$POST_DRAIN_FAILURES" -eq 0 ]
pass_assertion "one workload node was cordoned and drained with zero failed probes; replicas recovered after uncordon"

printf 'All kind enterprise smoke assertions passed.\n'
