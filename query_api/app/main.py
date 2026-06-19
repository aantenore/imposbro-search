"""
IMPOSBRO Federated Search & Admin API

A production-ready, enterprise-grade federated search system built on Typesense.
Provides document-level routing, resilient scatter-gather search, and
comprehensive management capabilities.

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

import logging
import os
import socket
import time
from contextlib import asynccontextmanager
from typing import Optional
from unittest.mock import MagicMock

import typesense
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from constants import APP_NAME, VERSION
from settings import settings
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

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Global service instances
federation_service: Optional[FederationService] = None
state_manager: Optional[StateManager] = None
kafka_service: Optional[KafkaService] = None
config_sync_service: Optional[ConfigSyncService] = None
config_notifier: Optional[SyncConfigNotifier] = None
rate_limiter_service: Optional[FixedWindowRateLimiter] = None


def create_state_client() -> typesense.Client:
    """
    Create and return a Typesense client for the internal state cluster.
    """
    nodes = [
        {"host": h.strip(), "port": "8108", "protocol": "http"}
        for h in settings.INTERNAL_STATE_NODES.split(",")
    ]

    return typesense.Client(
        {
            "nodes": nodes,
            "api_key": settings.INTERNAL_STATE_API_KEY,
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
            node_statuses = FederationService.node_statuses(
                settings.INTERNAL_STATE_NODES,
                "8108",
                settings.INTERNAL_STATE_API_KEY,
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
        )
        logger.info("Configuration reloaded successfully")
    else:
        logger.warning("Failed to reload configuration: no state found")


@asynccontextmanager
async def lifespan_test(app: FastAPI):
    """Minimal lifespan for testing: injects mocks so no external services are required."""
    global federation_service, state_manager, kafka_service, config_sync_service, config_notifier, rate_limiter_service

    mock_federation = MagicMock()
    mock_federation.clients = {"default-data-cluster": MagicMock()}
    mock_federation.clusters_config = {}  # GET /admin/federation/clusters iterates this
    mock_federation.routing_rules = {}
    mock_federation.collection_schemas = {}
    mock_federation.collection_aliases = {}
    mock_federation.get_client_for_document = MagicMock(
        return_value=(MagicMock(), "default-data-cluster")
    )
    mock_federation.get_named_clients_for_search = MagicMock(return_value=[])
    mock_federation.get_clients_for_search = MagicMock(return_value=[])

    federation_service = mock_federation
    state_manager = MagicMock()
    kafka_service = MagicMock()
    config_sync_service = MagicMock()
    config_notifier = MagicMock()
    rate_limiter_service = FixedWindowRateLimiter(settings.REDIS_URL)

    app.state.federation_service = federation_service
    app.state.state_manager = state_manager
    app.state.kafka_service = kafka_service
    app.state.config_notifier = config_notifier
    app.state.rate_limiter = rate_limiter_service

    yield


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan handler.

    Initializes all services on startup and cleans up on shutdown.
    Includes Redis Pub/Sub for multi-instance configuration sync.
    """
    global federation_service, state_manager, kafka_service, config_sync_service, config_notifier, rate_limiter_service

    logger.info("=" * 60)
    logger.info("%s %s Starting...", APP_NAME, VERSION)
    logger.info("=" * 60)

    # Initialize Typesense client and state manager
    state_client = wait_for_typesense()
    state_manager = StateManager(state_client)

    # Initialize federation service
    federation_service = FederationService()

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
        )
    else:
        # Bootstrap with default data cluster
        logger.info("No existing state found. Bootstrapping default configuration...")

        config = FederationService.create_client_config(
            "default-data-cluster",
            settings.DEFAULT_DATA_CLUSTER_NODES,
            settings.DEFAULT_DATA_CLUSTER_API_KEY,
        )
        federation_service.clusters_config["default-data-cluster"] = config
        federation_service.clients["default-data-cluster"] = (
            FederationService.create_client(config)
        )

        # Add second default cluster if configured
        if settings.DEFAULT_DATA2_CLUSTER_NODES:
            config2 = FederationService.create_client_config(
                "default-data-cluster-2",
                settings.DEFAULT_DATA2_CLUSTER_NODES,
                settings.DEFAULT_DATA2_CLUSTER_API_KEY,
            )
            federation_service.clusters_config["default-data-cluster-2"] = config2
            federation_service.clients["default-data-cluster-2"] = (
                FederationService.create_client(config2)
            )

        if not state_manager.save_state(
            federation_service.clusters_config,
            federation_service.routing_rules,
            federation_service.collection_schemas,
            federation_service.collection_aliases,
        ):
            raise RuntimeError("Failed to persist default federation configuration.")
        logger.info("Default data clusters bootstrapped.")

    # Initialize Kafka service
    kafka_service = KafkaService(
        broker_url=settings.KAFKA_BROKER_URL, topic_prefix=settings.KAFKA_TOPIC_PREFIX
    )
    _ = kafka_service.producer

    config_sync_source_id = (
        settings.CONFIG_SYNC_SOURCE_ID
        or f"{socket.gethostname()}:{os.getpid()}"
    )

    # Initialize Redis Pub/Sub for config sync across instances
    config_sync_service = ConfigSyncService(
        redis_url=settings.REDIS_URL,
        on_config_change=reload_configuration,
        source_id=config_sync_source_id,
    )
    await config_sync_service.start()

    # Create synchronous notifier for use in route handlers
    config_notifier = SyncConfigNotifier(
        settings.REDIS_URL,
        source_id=config_sync_source_id,
    )
    rate_limiter_service = FixedWindowRateLimiter(settings.REDIS_URL)

    app.state.federation_service = federation_service
    app.state.state_manager = state_manager
    app.state.kafka_service = kafka_service
    app.state.config_notifier = config_notifier
    app.state.rate_limiter = rate_limiter_service

    logger.info("=" * 60)
    logger.info("IMPOSBRO Search API Ready!")
    logger.info(f"  Clusters: {len(federation_service.clients)}")
    logger.info(f"  Config sync: Redis Pub/Sub enabled")
    logger.info("=" * 60)

    yield

    # Cleanup on shutdown
    logger.info("Shutting down IMPOSBRO Search API...")
    if config_sync_service:
        await config_sync_service.stop()
    if config_notifier:
        config_notifier.close()
    if rate_limiter_service:
        rate_limiter_service.close()
    if kafka_service:
        kafka_service.close()
    logger.info("Shutdown complete.")


def _get_lifespan():
    """Use test lifespan when TESTING=1 to avoid external service dependencies."""
    if os.environ.get("TESTING") == "1":
        return lifespan_test
    return lifespan


# Create FastAPI application
app = FastAPI(
    title="IMPOSBRO Federated Search & Admin API",
    description="""
Enterprise-grade federated search system built on Typesense.

## Features
- **Document-level sharding** with configurable routing rules
- **Federated search** across multiple clusters with scatter-gather
- **Deep pagination** with correct result merging
- **Asynchronous indexing** via Kafka for high throughput
- **Config synchronization** via Redis Pub/Sub for multi-instance consistency
- **High availability** state management with Typesense HA cluster
    """,
    version=VERSION,
    lifespan=_get_lifespan(),
)

if settings.CORS_ORIGINS.strip():
    origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Include routers
app.include_router(admin_router)
app.include_router(search_router)

# Add Prometheus metrics
Instrumentator().instrument(app).expose(app)


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
        r = redis.from_url(settings.REDIS_URL, decode_responses=True)
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


def _check_data_cluster_nodes() -> dict:
    """Return per-node readiness grouped by data cluster without raising."""
    node_statuses = {}
    if not federation_service or not hasattr(federation_service, "clients"):
        return node_statuses

    clients = getattr(federation_service, "clients", {})
    clusters_config = getattr(federation_service, "clusters_config", {}) or {}
    cluster_node_statuses = getattr(
        federation_service, "cluster_node_statuses", None
    )

    for name, client in clients.items():
        if callable(cluster_node_statuses) and name in clusters_config:
            try:
                node_statuses[name] = cluster_node_statuses(name)
            except Exception as exc:
                node_statuses[name] = [
                    {
                        "host": name,
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                ]
        else:
            node_statuses[name] = _single_client_node_status(name, client)
    return node_statuses


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


def _build_health_payload() -> dict:
    """Build dependency health payload for /health and /ready."""
    try:
        redis_ok = _check_redis_ok()
    except Exception:
        redis_ok = False
    try:
        kafka_ok = _check_kafka_ok()
    except Exception:
        kafka_ok = False
    try:
        clusters = (
            len(federation_service.clients)
            if federation_service and hasattr(federation_service, "clients")
            else 0
        )
        collections = (
            len(federation_service.routing_rules)
            if federation_service and hasattr(federation_service, "routing_rules")
            else 0
        )
    except Exception:
        clusters = 0
        collections = 0
    try:
        data_cluster_nodes = _check_data_cluster_nodes()
        data_clusters = _check_data_clusters(data_cluster_nodes)
    except Exception:
        data_cluster_nodes = {}
        data_clusters = {}

    data_clusters_ready = bool(data_clusters) and all(
        status == "ok" for status in data_clusters.values()
    )
    status = (
        "healthy"
        if clusters > 0 and redis_ok and kafka_ok and data_clusters_ready
        else "degraded"
    )
    return {
        "status": status,
        "clusters": clusters,
        "collections": collections,
        "config_sync": "enabled",
        "redis": "ok" if redis_ok else "error",
        "kafka": "ok" if kafka_ok else "error",
        "data_clusters": data_clusters,
        "data_cluster_nodes": data_cluster_nodes,
    }


@app.get("/health", tags=["Health"])
def health():
    """Detailed dependency health. Returns JSON even when degraded."""
    return _build_health_payload()


@app.get("/ready", tags=["Health"])
def ready(response: Response):
    """Readiness probe: HTTP 503 until all required dependencies are ready."""
    payload = _build_health_payload()
    if payload["status"] != "healthy":
        response.status_code = 503
    return payload
