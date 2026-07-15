"""
IMPOSBRO Federated Search & Admin API.

Federated search and control-plane services built around Typesense data
clusters. Enterprise readiness is governed by docs/ENTERPRISE_DELIVERY_CONTRACT.md
and must not be inferred from this module docstring.

Key Features:
- Document-level routing with configurable rules
- Fan-out routing to multiple clusters
- Asynchronous indexing via Kafka
- High availability state management
- Real-time config synchronization via Redis Pub/Sub
- Full admin UI support

Author: IMPOSBRO Team
Version: 4.0.0
"""

import copy
import asyncio
import logging
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import asynccontextmanager
from typing import Optional
from unittest.mock import MagicMock

import typesense
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.routing import Match

from api_contract import (
    EXCEPTION_HANDLERS,
    build_openapi_schema,
    is_versioned_request,
    stable_operation_id,
)
from constants import (
    API_MAJOR_VERSION,
    API_VERSION_HEADER,
    API_VERSION_PREFIX,
    APP_NAME,
    VERSION,
)
from observability import normalize_request_id, normalize_traceparent
from control_plane import PostgresControlPlaneStore
from indexing_events import InMemoryIndexingEventStore, PostgresIndexingEventStore
from settings import settings
from secret_resolver import build_secret_resolver, materialize_cluster_secret
from structured_logging import bind_log_context, configure_logging, reset_log_context
from telemetry import (
    OpenTelemetryMiddleware,
    TelemetryConfig,
    configure_tracing,
    current_trace_carrier,
    get_runtime as get_telemetry_runtime,
)
from services import (
    StateManager,
    StateLoadError,
    FederationService,
    KafkaService,
    ConfigSyncService,
    SyncConfigNotifier,
    FixedWindowRateLimiter,
)
from routers import admin_router, search_router

configure_logging(
    service="query-api",
    version=VERSION,
    level=settings.LOG_LEVEL,
    json_logs=settings.LOG_FORMAT == "json",
)
configure_tracing(
    TelemetryConfig(
        deployment_profile=settings.DEPLOYMENT_PROFILE,
        sdk_disabled=settings.OTEL_SDK_DISABLED,
        traces_exporter=settings.OTEL_TRACES_EXPORTER,
        service_name=settings.OTEL_SERVICE_NAME,
        service_version=settings.OTEL_SERVICE_VERSION,
        deployment_environment=settings.OTEL_DEPLOYMENT_ENVIRONMENT,
        build_id=settings.OTEL_BUILD_ID,
        revision=settings.OTEL_SERVICE_REVISION,
        endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
        traces_endpoint=settings.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT,
        exporter_timeout_millis=settings.OTEL_EXPORTER_OTLP_TIMEOUT,
        schedule_delay_millis=settings.OTEL_BSP_SCHEDULE_DELAY,
        export_timeout_millis=settings.OTEL_BSP_EXPORT_TIMEOUT,
        max_queue_size=settings.OTEL_BSP_MAX_QUEUE_SIZE,
        max_export_batch_size=settings.OTEL_BSP_MAX_EXPORT_BATCH_SIZE,
        sampler=settings.OTEL_TRACES_SAMPLER,
        sampler_argument=settings.OTEL_TRACES_SAMPLER_ARG,
    )
)
logger = logging.getLogger(__name__)

typesense_secret_resolver = build_secret_resolver(
    settings.TYPESENSE_SECRET_FILE_ROOT
)
allow_inline_cluster_secrets = settings.DEPLOYMENT_PROFILE == "development"

# Global service instances
federation_service: Optional[FederationService] = None
state_manager: Optional[StateManager] = None
kafka_service: Optional[KafkaService] = None
config_sync_service: Optional[ConfigSyncService] = None
config_notifier: Optional[SyncConfigNotifier] = None
rate_limiter_service: Optional[FixedWindowRateLimiter] = None
indexing_outbox_task: Optional[asyncio.Task] = None
control_plane_outbox_task: Optional[asyncio.Task] = None

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "Total HTTP requests handled by the Query API.",
    ["handler", "method", "status"],
)
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds for the Query API.",
    ["handler", "method"],
)
INDEXING_OUTBOX_PENDING = Gauge(
    "indexing_event_outbox_pending",
    "Durable indexing envelopes waiting for Kafka acknowledgement.",
)
INDEXING_OUTBOX_REPLAY_FAILURES = Counter(
    "indexing_event_outbox_replay_failures_total",
    "Failed background attempts to replay durable indexing envelopes.",
)
CONTROL_PLANE_OUTBOX_PENDING = Gauge(
    "control_plane_outbox_pending",
    "Committed control-plane notifications awaiting Redis delivery.",
)
CONTROL_PLANE_OUTBOX_DELIVERY_FAILURES = Counter(
    "control_plane_outbox_delivery_failures_total",
    "Failed durable control-plane notification deliveries.",
)


def _positive_float_env(name: str, default: float) -> float:
    """Read a strictly positive float used by the bounded health checker."""
    raw_value = os.environ.get(name, "").strip()
    if not raw_value:
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive number") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero")
    return value


def _positive_int_env(name: str, default: int) -> int:
    """Read a strictly positive integer used to cap health-check concurrency."""
    raw_value = os.environ.get(name, "").strip()
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero")
    return value


QUERY_API_HEALTH_CACHE_TTL_SECONDS = _positive_float_env(
    "QUERY_API_HEALTH_CACHE_TTL_SECONDS", 5.0
)
QUERY_API_HEALTH_CHECK_BUDGET_SECONDS = _positive_float_env(
    "QUERY_API_HEALTH_CHECK_BUDGET_SECONDS", 1.0
)
QUERY_API_HEALTH_DEPENDENCY_TIMEOUT_SECONDS = _positive_float_env(
    "QUERY_API_HEALTH_DEPENDENCY_TIMEOUT_SECONDS", 0.5
)
QUERY_API_HEALTH_MAX_WORKERS = _positive_int_env(
    "QUERY_API_HEALTH_MAX_WORKERS", 16
)
if QUERY_API_HEALTH_MAX_WORKERS < 3:
    raise RuntimeError("QUERY_API_HEALTH_MAX_WORKERS must be at least 3")

_dependency_health_cache_lock = threading.Lock()
_dependency_health_refresh_lock = threading.Lock()
_dependency_health_cache: Optional[dict] = None
_dependency_health_cache_at = 0.0


def create_state_client() -> typesense.Client:
    """
    Create and return a Typesense client for the internal state cluster.
    """
    nodes = [
        {
            "host": h.strip(),
            "port": "8108",
            "protocol": settings.INTERNAL_STATE_PROTOCOL,
        }
        for h in settings.INTERNAL_STATE_NODES.split(",")
    ]

    api_key = materialize_cluster_secret(
        {
            "api_key": settings.INTERNAL_STATE_API_KEY,
            "api_key_ref": settings.INTERNAL_STATE_API_KEY_REF,
        },
        allow_inline=allow_inline_cluster_secrets,
        resolver=typesense_secret_resolver,
    )
    return typesense.Client(
        {
            "nodes": nodes,
            "api_key": api_key,
            "connection_timeout_seconds": 5,
        }
    )


def wait_for_typesense() -> typesense.Client:
    """
    Wait for Typesense cluster to be ready and return the client.
    Uses GET /collections on every configured node to avoid accepting a partially
    initialized Raft cluster as ready.
    """
    while True:
        try:
            logger.info("Checking Typesense cluster readiness...")
            api_key = materialize_cluster_secret(
                {
                    "api_key": settings.INTERNAL_STATE_API_KEY,
                    "api_key_ref": settings.INTERNAL_STATE_API_KEY_REF,
                },
                allow_inline=allow_inline_cluster_secrets,
                resolver=typesense_secret_resolver,
            )
            node_statuses = FederationService.node_statuses(
                settings.INTERNAL_STATE_NODES,
                "8108",
                api_key,
                settings.INTERNAL_STATE_PROTOCOL,
            )
            failed_nodes = [
                node for node in node_statuses if node.get("status") != "ok"
            ]
            if failed_nodes:
                raise typesense.exceptions.ServiceUnavailable(str(failed_nodes))
            logger.info("Typesense cluster is ready.")
            return create_state_client()
        except (
            typesense.exceptions.ServiceUnavailable,
            typesense.exceptions.Timeout,
        ) as e:
            logger.warning(
                "Typesense cluster not ready (%s). Retrying in 5s...", type(e).__name__
            )
            time.sleep(5)
        except Exception as e:
            logger.warning(
                "Typesense health check failed (%s): %s. Retrying in 5s...",
                type(e).__name__,
                e,
            )
            time.sleep(5)


def create_state_manager() -> StateManager:
    """Create the configured authoritative control-plane persistence service."""
    if settings.CONTROL_PLANE_STORE_BACKEND == "postgres":
        if not settings.CONTROL_PLANE_DATABASE_URL:
            raise RuntimeError(
                "CONTROL_PLANE_DATABASE_URL is required for the PostgreSQL store"
            )
        store = PostgresControlPlaneStore(settings.CONTROL_PLANE_DATABASE_URL)
        return StateManager(
            store=store,
            secret_resolver=typesense_secret_resolver,
            allow_inline_cluster_secrets=allow_inline_cluster_secrets,
        )
    return StateManager(
        wait_for_typesense(),
        secret_resolver=typesense_secret_resolver,
        allow_inline_cluster_secrets=allow_inline_cluster_secrets,
    )


def create_indexing_event_store():
    """Create the configured durable sequencer/outbox adapter."""
    backend = settings.INDEXING_EVENT_STORE_BACKEND
    if backend == "disabled":
        return None
    if backend == "memory":
        store = InMemoryIndexingEventStore()
    else:
        if not settings.CONTROL_PLANE_DATABASE_URL:
            raise RuntimeError(
                "CONTROL_PLANE_DATABASE_URL is required for indexing event storage"
            )
        store = PostgresIndexingEventStore(settings.CONTROL_PLANE_DATABASE_URL)
    store.check_ready()
    return store


async def dispatch_indexing_outbox() -> None:
    """Continuously replay unpublished envelopes; Kafka delivery is at-least-once."""
    while True:
        try:
            if kafka_service and kafka_service.event_store:
                await asyncio.to_thread(
                    kafka_service.publish_pending,
                    limit=settings.INDEXING_OUTBOX_BATCH_SIZE,
                )
                INDEXING_OUTBOX_PENDING.set(
                    kafka_service.event_store.pending_count()
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            INDEXING_OUTBOX_REPLAY_FAILURES.inc()
            logger.error("Indexing outbox replay failed: %s", exc)
        await asyncio.sleep(settings.INDEXING_OUTBOX_POLL_SECONDS)


def dispatch_control_plane_outbox_once(
    manager: StateManager,
    notifier: SyncConfigNotifier,
    *,
    limit: int,
) -> tuple[int, int]:
    """Deliver a bounded durable page while allowing later records to progress."""
    delivered = 0
    failed = 0
    records = manager.unpublished_outbox(limit=limit)
    for record in records:
        try:
            notifier.publish_durable(
                event_type=record.event_type,
                revision=record.revision,
                event_id=record.event_id,
            )
            if manager.mark_outbox_published(record.event_id):
                delivered += 1
        except Exception as exc:
            failed += 1
            CONTROL_PLANE_OUTBOX_DELIVERY_FAILURES.inc()
            try:
                manager.record_outbox_failure(record.event_id, str(exc))
            except Exception:
                logger.exception(
                    "Failed to record control-plane outbox delivery error event_id=%s",
                    record.event_id,
                )
            logger.error(
                "Control-plane outbox delivery failed event_id=%s revision=%s: %s",
                record.event_id,
                record.revision,
                exc,
            )
    return delivered, failed


async def dispatch_control_plane_outbox() -> None:
    """Continuously deliver transactional state notifications to Redis."""
    while True:
        try:
            if state_manager and config_notifier and state_manager.transactional:
                await asyncio.to_thread(
                    dispatch_control_plane_outbox_once,
                    state_manager,
                    config_notifier,
                    limit=settings.CONTROL_PLANE_OUTBOX_BATCH_SIZE,
                )
                pending = len(
                    state_manager.unpublished_outbox(
                        limit=settings.CONTROL_PLANE_OUTBOX_MAX_PENDING + 1
                    )
                )
                CONTROL_PLANE_OUTBOX_PENDING.set(pending)
        except asyncio.CancelledError:
            raise
        except Exception:
            CONTROL_PLANE_OUTBOX_DELIVERY_FAILURES.inc()
            logger.exception("Control-plane outbox dispatcher failed")
        await asyncio.sleep(settings.CONTROL_PLANE_OUTBOX_POLL_SECONDS)


async def reload_configuration():
    """
    Callback invoked when configuration changes are detected via Redis Pub/Sub.

    Reloads the federation configuration from the Typesense state store,
    ensuring all API instances have consistent configuration.
    """
    global federation_service, state_manager

    if not state_manager or not federation_service:
        logger.warning("Cannot reload config: services not initialized")
        return

    logger.info("Reloading configuration from state store...")
    try:
        (
            clusters_config,
            routing_rules,
            collection_schemas,
            collection_aliases,
        ) = state_manager.load_state()
    except StateLoadError as exc:
        logger.error("Failed to reload configuration from state store: %s", exc)
        return

    if clusters_config is not None and routing_rules is not None:
        federation_service.reload_from_state(
            clusters_config,
            routing_rules,
            collection_schemas,
            collection_aliases,
            state_manager.routing_rollouts,
            revision=state_manager.current_revision,
        )
        _reset_dependency_health_cache()
        logger.info("Configuration reloaded successfully")
    else:
        logger.warning("Failed to reload configuration: no state found")


@asynccontextmanager
async def lifespan_test(app: FastAPI):
    """Minimal lifespan for testing: injects mocks so no external services are required."""
    global federation_service, state_manager, kafka_service, config_sync_service, config_notifier, rate_limiter_service, indexing_outbox_task, control_plane_outbox_task

    indexing_outbox_task = None
    control_plane_outbox_task = None

    mock_federation = MagicMock()
    mock_federation.clients = {"default-data-cluster": MagicMock()}
    mock_federation.clusters_config = {}  # GET /admin/federation/clusters iterates this
    mock_federation.routing_rules = {}
    mock_federation.collection_schemas = {}
    mock_federation.collection_aliases = {}
    mock_federation.routing_rollouts = {}
    mock_federation.applied_revision = 0
    mock_federation.get_client_for_document = MagicMock(
        return_value=(MagicMock(), "default-data-cluster")
    )
    mock_federation.get_named_clients_for_search = MagicMock(return_value=[])
    mock_federation.get_named_clients_for_delete = MagicMock(return_value=[])
    mock_federation.get_clients_for_search = MagicMock(return_value=[])
    test_materializer = FederationService(
        secret_resolver=typesense_secret_resolver,
        allow_inline_secrets=True,
    )
    mock_federation.materialize_cluster_config.side_effect = (
        test_materializer.materialize_cluster_config
    )

    federation_service = mock_federation
    state_manager = MagicMock()
    state_manager.current_revision = 0
    state_manager.state_digest = ""
    state_manager.transactional = False
    state_manager.authoritative_revision.return_value = 0
    kafka_service = MagicMock()
    config_sync_service = MagicMock()
    config_notifier = MagicMock()
    rate_limiter_service = FixedWindowRateLimiter(settings.REDIS_URL)

    app.state.federation_service = federation_service
    app.state.state_manager = state_manager
    app.state.kafka_service = kafka_service
    app.state.config_notifier = config_notifier
    app.state.rate_limiter = rate_limiter_service
    _reset_dependency_health_cache()

    try:
        yield
    finally:
        _reset_dependency_health_cache()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan handler.

    Initializes all services on startup and cleans up on shutdown.
    Includes Redis Pub/Sub for multi-instance configuration sync.
    """
    global federation_service, state_manager, kafka_service, config_sync_service, config_notifier, rate_limiter_service, indexing_outbox_task, control_plane_outbox_task

    indexing_outbox_task = None
    control_plane_outbox_task = None

    logger.info("=" * 60)
    logger.info("%s %s Starting...", APP_NAME, VERSION)
    logger.info("=" * 60)

    # Initialize the authoritative control-plane state manager.
    state_manager = create_state_manager()

    # Initialize federation service
    federation_service = FederationService(
        secret_resolver=typesense_secret_resolver,
        allow_inline_secrets=allow_inline_cluster_secrets,
    )

    # Try to load existing state
    (
        clusters_config,
        routing_rules,
        collection_schemas,
        collection_aliases,
    ) = state_manager.load_state()

    if clusters_config is not None and routing_rules is not None:
        federation_service.load_from_state(
            clusters_config,
            routing_rules,
            collection_schemas,
            collection_aliases,
            state_manager.routing_rollouts,
            revision=state_manager.current_revision,
        )
    else:
        # Bootstrap with default data cluster
        logger.info("No existing state found. Bootstrapping default configuration...")

        config = FederationService.create_client_config(
            "default-data-cluster",
            settings.DEFAULT_DATA_CLUSTER_NODES,
            settings.DEFAULT_DATA_CLUSTER_API_KEY,
            settings.DEFAULT_DATA_CLUSTER_PROTOCOL,
            api_key_ref=settings.DEFAULT_DATA_CLUSTER_API_KEY_REF,
            allow_inline_secrets=allow_inline_cluster_secrets,
        )
        federation_service.clusters_config["default-data-cluster"] = config
        federation_service.clients["default-data-cluster"] = (
            federation_service.build_client(config)
        )

        # Add second default cluster if configured
        if settings.DEFAULT_DATA2_CLUSTER_NODES:
            config2 = FederationService.create_client_config(
                "default-data-cluster-2",
                settings.DEFAULT_DATA2_CLUSTER_NODES,
                settings.DEFAULT_DATA2_CLUSTER_API_KEY,
                settings.DEFAULT_DATA2_CLUSTER_PROTOCOL,
                api_key_ref=settings.DEFAULT_DATA2_CLUSTER_API_KEY_REF,
                allow_inline_secrets=allow_inline_cluster_secrets,
            )
            federation_service.clusters_config["default-data-cluster-2"] = config2
            federation_service.clients["default-data-cluster-2"] = (
                federation_service.build_client(config2)
            )

        if not state_manager.save_state(
            federation_service.clusters_config,
            federation_service.routing_rules,
            federation_service.collection_schemas,
            federation_service.collection_aliases,
            federation_service.routing_rollouts,
        ):
            raise RuntimeError("Failed to persist default federation configuration.")
        federation_service.mark_applied_revision(state_manager.current_revision)
        logger.info("Default data clusters bootstrapped.")

    # Initialize Kafka service
    kafka_service = KafkaService(
        broker_url=settings.KAFKA_BROKER_URL,
        topic_prefix=settings.KAFKA_TOPIC_PREFIX,
        security_protocol=settings.KAFKA_SECURITY_PROTOCOL,
        sasl_mechanism=settings.KAFKA_SASL_MECHANISM,
        sasl_username=settings.KAFKA_SASL_USERNAME,
        sasl_password=settings.KAFKA_SASL_PASSWORD,
        ssl_cafile=settings.KAFKA_SSL_CAFILE,
        ssl_certfile=settings.KAFKA_SSL_CERTFILE,
        ssl_keyfile=settings.KAFKA_SSL_KEYFILE,
        ssl_check_hostname=settings.KAFKA_SSL_CHECK_HOSTNAME,
        client_id=settings.KAFKA_CLIENT_ID,
        event_store=create_indexing_event_store(),
    )
    _ = kafka_service.producer
    if kafka_service.event_store is not None:
        indexing_outbox_task = asyncio.create_task(dispatch_indexing_outbox())

    config_sync_source_id = (
        settings.CONFIG_SYNC_SOURCE_ID
        or f"{socket.gethostname()}:{os.getpid()}"
    )

    # Initialize Redis Pub/Sub for config sync across instances
    config_sync_service = ConfigSyncService(
        redis_url=settings.REDIS_URL,
        on_config_change=reload_configuration,
        source_id=config_sync_source_id,
        local_revision=lambda: federation_service.applied_revision,
        authoritative_revision=state_manager.authoritative_revision,
        reconcile_interval_seconds=settings.CONTROL_PLANE_RECONCILE_SECONDS,
    )
    await config_sync_service.start()

    # Create synchronous notifier for use in route handlers
    config_notifier = SyncConfigNotifier(
        settings.REDIS_URL,
        source_id=config_sync_source_id,
    )
    if state_manager.transactional:
        control_plane_outbox_task = asyncio.create_task(
            dispatch_control_plane_outbox()
        )
    rate_limiter_service = FixedWindowRateLimiter(settings.REDIS_URL)

    app.state.federation_service = federation_service
    app.state.state_manager = state_manager
    app.state.kafka_service = kafka_service
    app.state.config_notifier = config_notifier
    app.state.rate_limiter = rate_limiter_service
    _reset_dependency_health_cache()

    logger.info("=" * 60)
    logger.info("IMPOSBRO Search API Ready!")
    logger.info(f"  Clusters: {len(federation_service.clients)}")
    logger.info("  Config sync: durable revision polling + Redis wake-up enabled")
    logger.info("=" * 60)

    yield

    # Cleanup on shutdown
    logger.info("Shutting down IMPOSBRO Search API...")
    if config_sync_service:
        await config_sync_service.stop()
    if indexing_outbox_task:
        indexing_outbox_task.cancel()
        try:
            await indexing_outbox_task
        except asyncio.CancelledError:
            pass
        indexing_outbox_task = None
    if control_plane_outbox_task:
        control_plane_outbox_task.cancel()
        try:
            await control_plane_outbox_task
        except asyncio.CancelledError:
            pass
        control_plane_outbox_task = None
    if config_notifier:
        config_notifier.close()
    if rate_limiter_service:
        rate_limiter_service.close()
    if kafka_service:
        kafka_service.close()
    get_telemetry_runtime().shutdown()
    _reset_dependency_health_cache()
    logger.info("Shutdown complete.")


def _get_lifespan():
    """Use test lifespan when TESTING=1 to avoid external service dependencies."""
    if os.environ.get("TESTING") == "1":
        return lifespan_test
    return lifespan


def _status_family(status_code: int) -> str:
    """Return low-cardinality status labels compatible with existing alerts."""
    return f"{status_code // 100}xx"


def _route_template_from_routes(scope, routes) -> Optional[str]:
    """
    Resolve a stable route template for Prometheus labels.

    FastAPI 0.138 keeps included routers as internal wrapper routes, so the
    matcher must inspect the original router routes instead of assuming every
    top-level route has a public ``path`` attribute.
    """
    partial_template = None
    for route in routes:
        match, _child_scope = route.matches(scope)
        template = getattr(route, "path", None)
        if match == Match.FULL:
            if template:
                return template
            original_router = getattr(route, "original_router", None)
            if original_router:
                nested = _route_template_from_routes(scope, original_router.routes)
                if nested:
                    return nested
            nested_routes = getattr(route, "routes", None)
            if nested_routes:
                nested = _route_template_from_routes(scope, nested_routes)
                if nested:
                    return nested
        elif match == Match.PARTIAL and partial_template is None and template:
            partial_template = template
    return partial_template


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    if template:
        return template
    return _route_template_from_routes(request.scope, request.app.routes) or request.url.path


# Create FastAPI application
app = FastAPI(
    title="IMPOSBRO Federated Search & Admin API",
    description="""
Federated search and revisioned control-plane API built around Typesense data
clusters. Enterprise release claims are governed by the repository delivery
contract and its linked evidence.

## Features
- **Document-level sharding** with configurable routing rules
- **Federated search** across multiple clusters with scatter-gather
- **Deep pagination** with correct result merging
- **Asynchronous indexing** via Kafka for high throughput
- **Config synchronization** via Redis Pub/Sub for multi-instance consistency
- **Transactional control-plane state** with PostgreSQL in enterprise deployments
    """,
    version=VERSION,
    lifespan=_get_lifespan(),
    exception_handlers=EXCEPTION_HANDLERS,
    generate_unique_id_function=stable_operation_id,
)

if settings.CORS_ORIGINS.strip():
    origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[
            settings.REQUEST_ID_HEADER,
            "traceparent",
            API_VERSION_HEADER,
            "Deprecation",
            "ETag",
            "Link",
            "X-Control-Plane-Revision",
            "Retry-After",
            "X-Pagination-Info",
            "X-Pagination-Warning",
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
        ],
    )


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Attach a stable, safe request id to every response and async ingest message."""
    request_id = normalize_request_id(request.headers.get(settings.REQUEST_ID_HEADER))
    fallback_traceparent = normalize_traceparent(request.headers.get("traceparent"))
    traceparent = current_trace_carrier(
        {"traceparent": fallback_traceparent}
    ).get("traceparent", fallback_traceparent)
    request.state.request_id = request_id
    request.state.traceparent = traceparent
    tokens = bind_log_context(request_id=request_id, traceparent=traceparent)
    try:
        response = await call_next(request)
        response.headers[settings.REQUEST_ID_HEADER] = request_id
        response.headers["traceparent"] = traceparent
        response.headers[API_VERSION_HEADER] = str(API_MAJOR_VERSION)
        path = request.scope.get("path", "")
        if (
            path == "/admin"
            or path.startswith("/admin/")
            or path == f"{API_VERSION_PREFIX}/admin"
            or path.startswith(f"{API_VERSION_PREFIX}/admin/")
        ):
            manager = getattr(request.app.state, "state_manager", None)
            revision = getattr(manager, "current_revision", None)
            if isinstance(revision, int) and revision >= 0:
                response.headers.setdefault("ETag", f'"{revision}"')
                response.headers.setdefault(
                    "X-Control-Plane-Revision",
                    str(revision),
                )
        if not is_versioned_request(request) and (
            path == "/admin"
            or path.startswith("/admin/")
            or path == "/collections"
            or path.startswith("/collections/")
        ):
            response.headers["Deprecation"] = "true"
            response.headers["Link"] = (
                f'<{API_VERSION_PREFIX}{path}>; rel="successor-version"'
            )
        return response
    finally:
        reset_log_context(tokens)


@app.middleware("http")
async def http_metrics_middleware(request: Request, call_next):
    """Record low-cardinality HTTP metrics without relying on route internals."""
    started_at = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        handler = _route_template(request)
        method = request.method
        HTTP_REQUESTS.labels(
            handler=handler,
            method=method,
            status=_status_family(status_code),
        ).inc()
        HTTP_REQUEST_DURATION.labels(handler=handler, method=method).observe(
            time.perf_counter() - started_at
        )


# Registered last so the server span encloses request correlation, metrics, and
# routing. The middleware reads the active runtime dynamically for testability.
app.add_middleware(OpenTelemetryMiddleware)


# The versioned contract is authoritative. Unversioned aliases remain mounted
# for compatibility and advertise their successor through response headers.
app.include_router(admin_router, prefix=API_VERSION_PREFIX)
app.include_router(search_router, prefix=API_VERSION_PREFIX)
app.include_router(admin_router)
app.include_router(search_router)


def _application_openapi():
    if app.openapi_schema is None:
        app.openapi_schema = build_openapi_schema(app, versioned_only=False)
    return app.openapi_schema


app.openapi = _application_openapi


@app.get(
    f"{API_VERSION_PREFIX}/openapi.json",
    tags=["API Contract"],
    include_in_schema=False,
)
def versioned_openapi():
    """Return the canonical major-version contract without legacy aliases."""
    return JSONResponse(build_openapi_schema(app, versioned_only=True))


@app.get("/metrics", include_in_schema=False)
def metrics():
    """Expose Prometheus metrics for Query API and data-plane counters."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/", tags=["Health"])
def root():
    """Health check endpoint."""
    return {
        "service": APP_NAME,
        "version": VERSION,
        "status": "healthy",
    }


def _check_redis_ok() -> bool:
    """Quick Redis connectivity check for health endpoint."""
    try:
        import redis
        r = redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=QUERY_API_HEALTH_DEPENDENCY_TIMEOUT_SECONDS,
            socket_timeout=QUERY_API_HEALTH_DEPENDENCY_TIMEOUT_SECONDS,
        )
        r.ping()
        r.close()
        return True
    except Exception:
        return False


def _check_kafka_ok() -> bool:
    """Quick Kafka connectivity check for health endpoint."""
    try:
        if kafka_service and kafka_service._producer:
            kafka_service.producer.metrics()
            return True
        return False
    except Exception:
        return False


def _check_control_plane() -> dict:
    """Validate authoritative state availability and replica convergence."""
    if state_manager is None or federation_service is None:
        return {
            "status": "error",
            "error": "control-plane services are not initialized",
        }
    try:
        transactional = bool(getattr(state_manager, "transactional", False))
        store = getattr(state_manager, "store", None)
        if transactional and store is not None:
            store.check_ready()
        authoritative_revision = int(state_manager.authoritative_revision())
        applied_revision = int(
            getattr(federation_service, "applied_revision", authoritative_revision)
        )
        converged = applied_revision == authoritative_revision
        pending_control_events = 0
        outbox_within_budget = True
        if transactional:
            pending_control_events = len(
                state_manager.unpublished_outbox(
                    limit=settings.CONTROL_PLANE_OUTBOX_MAX_PENDING + 1
                )
            )
            CONTROL_PLANE_OUTBOX_PENDING.set(pending_control_events)
            outbox_within_budget = (
                pending_control_events <= settings.CONTROL_PLANE_OUTBOX_MAX_PENDING
            )
        event_store_status = "disabled"
        pending_events = 0
        if isinstance(kafka_service, KafkaService) and kafka_service.event_store:
            kafka_service.event_store.check_ready()
            pending_events = kafka_service.event_store.pending_count()
            INDEXING_OUTBOX_PENDING.set(pending_events)
            event_store_status = "ok"
        return {
            "status": (
                "ok"
                if converged and outbox_within_budget
                else "stale" if not converged else "outbox_backlog"
            ),
            "backend": "postgres" if transactional else "legacy_typesense",
            "authoritative_revision": authoritative_revision,
            "applied_revision": applied_revision,
            "state_digest": str(getattr(state_manager, "state_digest", "")),
            "converged": converged,
            "indexing_event_store": event_store_status,
            "pending_indexing_events": pending_events,
            "pending_control_plane_events": pending_control_events,
            "control_plane_outbox_within_budget": outbox_within_budget,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }


def _check_typesense_client_ok(client) -> bool:
    """Return whether a Typesense client can list collections."""
    try:
        client.collections.retrieve()
        return True
    except Exception:
        return False


def _single_client_node_status(name: str, client) -> list[dict]:
    """Fallback node status for tests or legacy clients without stored node config."""
    return [
        {
            "host": name,
            "status": "ok" if _check_typesense_client_ok(client) else "error",
        }
    ]


def _check_data_cluster(name: str, client, clusters_config: dict) -> list[dict]:
    """Check one data cluster using a health-specific per-node timeout."""
    cluster_node_statuses = getattr(
        federation_service, "cluster_node_statuses", None
    )
    if callable(cluster_node_statuses) and name in clusters_config:
        return cluster_node_statuses(
            name,
            connection_timeout_seconds=QUERY_API_HEALTH_DEPENDENCY_TIMEOUT_SECONDS,
        )
    return _single_client_node_status(name, client)


def _check_data_clusters(data_cluster_nodes: dict) -> dict:
    """Return aggregate per-data-cluster readiness from node-level statuses."""
    statuses = {}
    for name, nodes in data_cluster_nodes.items():
        statuses[name] = (
            "ok"
            if nodes and all(node.get("status") == "ok" for node in nodes)
            else "error"
        )
    return statuses


def _core_services_initialized() -> bool:
    """Return whether startup completed far enough for the API to serve traffic."""
    return all(
        service is not None
        for service in (federation_service, state_manager, kafka_service)
    )


def _dependency_failure_nodes(name: str, error: str) -> list[dict]:
    """Return the stable node-shaped error used for bounded health failures."""
    return [{"host": name, "status": "error", "error": error}]


def _dependency_health_placeholder(reason: str) -> dict:
    """Build a dependency snapshot without performing any network calls."""
    clients = (
        dict(getattr(federation_service, "clients", {}) or {})
        if federation_service
        else {}
    )
    routing_rules = (
        dict(getattr(federation_service, "routing_rules", {}) or {})
        if federation_service
        else {}
    )
    data_cluster_nodes = {
        name: _dependency_failure_nodes(name, reason) for name in clients
    }
    return {
        "status": "degraded",
        "clusters": len(clients),
        "collections": len(routing_rules),
        "redis": "error",
        "kafka": "error",
        "control_plane": {"status": "error", "error": reason},
        "data_clusters": _check_data_clusters(data_cluster_nodes),
        "data_cluster_nodes": data_cluster_nodes,
        "checks_timed_out": [],
        "dependency_errors": {"health": reason},
        "checked_at_unix_ms": int(time.time() * 1000),
    }


def _probe_dependency_health() -> dict:
    """Probe dependencies concurrently within one hard wall-clock budget."""
    clients = dict(getattr(federation_service, "clients", {}) or {})
    clusters_config = dict(
        getattr(federation_service, "clusters_config", {}) or {}
    )
    routing_rules = dict(getattr(federation_service, "routing_rules", {}) or {})
    task_count = len(clients) + 3
    worker_count = min(QUERY_API_HEALTH_MAX_WORKERS, max(1, task_count))
    executor = ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="dependency-health",
    )
    futures = {
        executor.submit(_check_redis_ok): ("redis", "redis"),
        executor.submit(_check_kafka_ok): ("kafka", "kafka"),
        executor.submit(_check_control_plane): ("control_plane", "control_plane"),
    }
    for name, client in clients.items():
        futures[
            executor.submit(_check_data_cluster, name, client, clusters_config)
        ] = ("data_cluster", name)

    _done, pending = wait(
        futures,
        timeout=QUERY_API_HEALTH_CHECK_BUDGET_SECONDS,
    )
    redis_ok = False
    kafka_ok = False
    control_plane = {
        "status": "error",
        "error": "control-plane health check did not complete",
    }
    data_cluster_nodes = {}
    checks_timed_out = []
    dependency_errors = {}

    for future, (kind, name) in futures.items():
        label = f"{kind}:{name}"
        if future in pending:
            future.cancel()
            checks_timed_out.append(label)
            if kind == "data_cluster":
                data_cluster_nodes[name] = _dependency_failure_nodes(
                    name,
                    "health check exceeded the hard time budget",
                )
            elif kind == "control_plane":
                control_plane = {
                    "status": "error",
                    "error": "health check exceeded the hard time budget",
                }
            continue
        try:
            result = future.result()
        except Exception as exc:
            dependency_errors[label] = f"{type(exc).__name__}: {exc}"
            if kind == "data_cluster":
                data_cluster_nodes[name] = _dependency_failure_nodes(
                    name,
                    dependency_errors[label],
                )
            elif kind == "control_plane":
                control_plane = {
                    "status": "error",
                    "error": dependency_errors[label],
                }
            continue

        if kind == "redis":
            redis_ok = bool(result)
        elif kind == "kafka":
            kafka_ok = bool(result)
        elif kind == "control_plane":
            control_plane = result
        else:
            data_cluster_nodes[name] = result or _dependency_failure_nodes(
                name,
                "health check returned no node status",
            )

    executor.shutdown(wait=False, cancel_futures=True)
    data_clusters = _check_data_clusters(data_cluster_nodes)
    data_clusters_ready = bool(data_clusters) and all(
        status == "ok" for status in data_clusters.values()
    )
    status = (
        "healthy"
        if (
            clients
            and redis_ok
            and kafka_ok
            and data_clusters_ready
            and control_plane.get("status") == "ok"
        )
        else "degraded"
    )
    return {
        "status": status,
        "clusters": len(clients),
        "collections": len(routing_rules),
        "redis": "ok" if redis_ok else "error",
        "kafka": "ok" if kafka_ok else "error",
        "control_plane": control_plane,
        "data_clusters": data_clusters,
        "data_cluster_nodes": data_cluster_nodes,
        "checks_timed_out": sorted(checks_timed_out),
        "dependency_errors": dependency_errors,
        "checked_at_unix_ms": int(time.time() * 1000),
    }


def _reset_dependency_health_cache() -> None:
    """Invalidate cached health after startup, tests, or topology changes."""
    global _dependency_health_cache, _dependency_health_cache_at
    with _dependency_health_cache_lock:
        _dependency_health_cache = None
        _dependency_health_cache_at = 0.0


def _cached_dependency_health() -> dict:
    """Return a fresh-enough snapshot, coalescing concurrent refresh attempts."""
    global _dependency_health_cache, _dependency_health_cache_at
    now = time.monotonic()
    with _dependency_health_cache_lock:
        cached = copy.deepcopy(_dependency_health_cache)
        cache_age = now - _dependency_health_cache_at
    if cached is not None and cache_age <= QUERY_API_HEALTH_CACHE_TTL_SECONDS:
        cached["health_cache_age_seconds"] = round(max(cache_age, 0.0), 3)
        cached["health_cache_stale"] = False
        return cached

    if not _dependency_health_refresh_lock.acquire(blocking=False):
        snapshot = cached or _dependency_health_placeholder(
            "dependency health refresh is already in progress"
        )
        snapshot["health_cache_age_seconds"] = round(max(cache_age, 0.0), 3)
        snapshot["health_cache_stale"] = True
        return snapshot

    try:
        snapshot = _probe_dependency_health()
        refreshed_at = time.monotonic()
        with _dependency_health_cache_lock:
            _dependency_health_cache = copy.deepcopy(snapshot)
            _dependency_health_cache_at = refreshed_at
        snapshot["health_cache_age_seconds"] = 0.0
        snapshot["health_cache_stale"] = False
        return snapshot
    finally:
        _dependency_health_refresh_lock.release()


def _build_health_payload() -> dict:
    """Build cached, bounded health and policy-specific readiness state."""
    core_services_initialized = _core_services_initialized()
    if core_services_initialized:
        payload = _cached_dependency_health()
    else:
        payload = _dependency_health_placeholder(
            "core services are not initialized"
        )
        payload["health_cache_age_seconds"] = 0.0
        payload["health_cache_stale"] = False

    if settings.READINESS_POLICY == "serving":
        ready = core_services_initialized
    else:
        ready = (
            core_services_initialized
            and payload["status"] == "healthy"
            and not payload.get("health_cache_stale", False)
        )
    return {
        **payload,
        "ready": ready,
        "readiness_policy": settings.READINESS_POLICY,
        "config_sync": "enabled",
    }


@app.get("/health", tags=["Health"])
def health():
    """Detailed dependency health. Returns JSON even when degraded."""
    return _build_health_payload()


@app.get("/ready", tags=["Health"])
def ready(response: Response):
    """Readiness probe governed by the configured serving or strict policy."""
    payload = _build_health_payload()
    if not payload["ready"]:
        response.status_code = 503
    return payload
