"""
IMPOSBRO Federated Search & Admin API

A production-ready, enterprise-grade federated search system built on Typesense.
Provides document-level sharding, resilient scatter-gather search, and
comprehensive management capabilities.

Key Features:
- Document-level routing with configurable rules
- Fan-out routing to multiple clusters
- Asynchronous indexing via Kafka
- High availability state management
- Full admin UI support

Author: IMPOSBRO Team
Version: 3.5.0
"""

import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

import typesense
from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from settings import settings
from services import StateManager, FederationService, KafkaService
from routers import admin_router, search_router
from routers.admin import get_federation_service as admin_get_federation
from routers.admin import get_state_manager as admin_get_state_manager
from routers.search import get_federation_service as search_get_federation
from routers.search import get_kafka_service as search_get_kafka

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Global service instances
federation_service: Optional[FederationService] = None
state_manager: Optional[StateManager] = None
kafka_service: Optional[KafkaService] = None


def create_state_client() -> typesense.Client:
    """
    Create and return a Typesense client for the internal state cluster.

    Waits for the cluster to be ready before returning.
    """
    nodes = [
        {"host": h.strip(), "port": "8108", "protocol": "http"}
        for h in settings.INTERNAL_STATE_NODES.split(",")
    ]

    client = typesense.Client(
        {
            "nodes": nodes,
            "api_key": settings.INTERNAL_STATE_API_KEY,
            "connection_timeout_seconds": 5,
        }
    )

    return client


def wait_for_typesense() -> typesense.Client:
    """
    Wait for Typesense cluster to be ready and return the client.
    """
    while True:
        try:
            logger.info("Checking Typesense cluster readiness...")
            client = create_state_client()
            # Try to access the cluster to verify it's ready
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan handler.

    Initializes all services on startup and cleans up on shutdown.
    This replaces the deprecated @app.on_event decorators.
    """
    global federation_service, state_manager, kafka_service

    logger.info("=" * 60)
    logger.info("IMPOSBRO Search API Starting...")
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
    # Trigger connection
    _ = kafka_service.producer

    # Override dependency injection functions
    def get_federation():
        return federation_service

    def get_state():
        return state_manager

    def get_kafka():
        return kafka_service

    # Inject dependencies into routers
    import routers.admin as admin_module
    import routers.search as search_module

    admin_module.get_federation_service = get_federation
    admin_module.get_state_manager = get_state
    search_module.get_federation_service = get_federation
    search_module.get_kafka_service = get_kafka

    logger.info("=" * 60)
    logger.info("IMPOSBRO Search API Ready!")
    logger.info("=" * 60)

    yield

    # Cleanup on shutdown
    logger.info("Shutting down IMPOSBRO Search API...")
    if kafka_service:
        kafka_service.close()
    logger.info("Shutdown complete.")


# Create FastAPI application
app = FastAPI(
    title="IMPOSBRO Federated Search & Admin API",
    description="""
Enterprise-grade federated search system built on Typesense.

## Features
- **Document-level sharding** with configurable routing rules
- **Federated search** across multiple clusters with scatter-gather
- **Asynchronous indexing** via Kafka for high throughput
- **High availability** state management with Typesense HA cluster

## Getting Started
1. Register external clusters at `/admin/federation/clusters`
2. Create collections at `/admin/collections`
3. Configure routing rules at `/admin/routing-rules`
4. Ingest documents at `/ingest/{collection}`
5. Search with `/search/{collection}`
    """,
    version="3.5.0",
    lifespan=lifespan,
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
        "service": "IMPOSBRO Federated Search API",
        "version": "3.5.0",
        "status": "healthy",
    }


@app.get("/health", tags=["Health"])
def health():
    """Detailed health check endpoint."""
    return {
        "status": "healthy",
        "clusters": len(federation_service.clients) if federation_service else 0,
        "collections": (
            len(federation_service.routing_rules) if federation_service else 0
        ),
    }
