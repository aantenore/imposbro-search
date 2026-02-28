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
import time
from contextlib import asynccontextmanager
from typing import Optional
from unittest.mock import MagicMock

import typesense
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from constants import APP_NAME, VERSION
from settings import settings
from services import (
    StateManager,
    FederationService,
    KafkaService,
    ConfigSyncService,
    SyncConfigNotifier,
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
    """
    while True:
        try:
            logger.info("Checking Typesense cluster readiness...")
            client = create_state_client()
            client.operations.perform("health", {})
            logger.info("Typesense cluster is ready.")
            return client
        except (
            typesense.exceptions.ServiceUnavailable,
            typesense.exceptions.ConnectionError,
            typesense.exceptions.ConnectionTimeout,
        ) as e:
            logger.warning(
                f"Typesense cluster not ready ({type(e).__name__}). Retrying in 5s..."
            )
            time.sleep(5)
        except Exception as e:
            logger.error(f"Unexpected error during health check: {e}. Retrying...")
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
    clusters_config, routing_rules = state_manager.load_state()

    if clusters_config is not None and routing_rules is not None:
        federation_service.reload_from_state(clusters_config, routing_rules)
        logger.info("Configuration reloaded successfully")
    else:
        logger.warning("Failed to reload configuration: no state found")


@asynccontextmanager
async def lifespan_test(app: FastAPI):
    """Minimal lifespan for testing: injects mocks so no external services are required."""
    global federation_service, state_manager, kafka_service, config_sync_service, config_notifier

    mock_federation = MagicMock()
    mock_federation.clients = {"default-data-cluster": MagicMock()}
    mock_federation.clusters_config = {}  # GET /admin/federation/clusters iterates this
    mock_federation.routing_rules = {}
    mock_federation.get_client_for_document = MagicMock(
        return_value=(MagicMock(), "default-data-cluster")
    )
    mock_federation.get_clients_for_search = MagicMock(return_value=[])

    federation_service = mock_federation
    state_manager = MagicMock()
    kafka_service = MagicMock()
    config_sync_service = MagicMock()
    config_notifier = MagicMock()

    app.state.federation_service = federation_service
    app.state.state_manager = state_manager
    app.state.kafka_service = kafka_service
    app.state.config_notifier = config_notifier

    yield


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan handler.

    Initializes all services on startup and cleans up on shutdown.
    Includes Redis Pub/Sub for multi-instance configuration sync.
    """
    global federation_service, state_manager, kafka_service, config_sync_service, config_notifier

    logger.info("=" * 60)
    logger.info("%s %s Starting...", APP_NAME, VERSION)
    logger.info("=" * 60)

    # Initialize Typesense client and state manager
    state_client = wait_for_typesense()
    state_manager = StateManager(state_client)

    # Initialize federation service
    federation_service = FederationService()

    # Try to load existing state
    clusters_config, routing_rules = state_manager.load_state()

    if clusters_config and routing_rules:
        federation_service.load_from_state(clusters_config, routing_rules)
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

        state_manager.save_state(
            federation_service.clusters_config, federation_service.routing_rules
        )
        logger.info("Default data clusters bootstrapped.")

    # Initialize Kafka service
    kafka_service = KafkaService(
        broker_url=settings.KAFKA_BROKER_URL, topic_prefix=settings.KAFKA_TOPIC_PREFIX
    )
    _ = kafka_service.producer

    # Initialize Redis Pub/Sub for config sync across instances
    config_sync_service = ConfigSyncService(
        redis_url=settings.REDIS_URL, on_config_change=reload_configuration
    )
    await config_sync_service.start()

    # Create synchronous notifier for use in route handlers
    config_notifier = SyncConfigNotifier(settings.REDIS_URL)

    app.state.federation_service = federation_service
    app.state.state_manager = state_manager
    app.state.kafka_service = kafka_service
    app.state.config_notifier = config_notifier

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


@app.get("/health", tags=["Health"])
def health():
    """Detailed health check endpoint with optional dependency status."""
    redis_ok = _check_redis_ok()
    kafka_ok = _check_kafka_ok()
    clusters = len(federation_service.clients) if federation_service else 0
    collections = (
        len(federation_service.routing_rules) if federation_service else 0
    )
    status = "healthy" if (clusters > 0 and redis_ok) else "degraded"
    return {
        "status": status,
        "clusters": clusters,
        "collections": collections,
        "config_sync": "enabled",
        "redis": "ok" if redis_ok else "error",
        "kafka": "ok" if kafka_ok else "error",
    }
