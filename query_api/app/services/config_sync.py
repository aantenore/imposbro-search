"""
Configuration Synchronization Service for IMPOSBRO Search.

This module provides real-time configuration synchronization across multiple
API instances using Redis Pub/Sub. When any instance updates the configuration,
all other instances are notified and reload their state.

This solves the "split-brain" problem where different API replicas could have
different views of the cluster configuration.
"""

import asyncio
import logging
import json
from typing import Optional, Callable, Awaitable
import redis.asyncio as redis

logger = logging.getLogger(__name__)

# Redis channel for configuration updates
CONFIG_CHANNEL = "imposbro:config_updates"


class ConfigSyncService:
    """
    Manages real-time configuration synchronization across API instances.

    Uses Redis Pub/Sub to broadcast configuration change notifications.
    When a notification is received, the configured callback is invoked
    to reload the application state.

    Attributes:
        redis_url: Redis connection URL
        on_config_change: Async callback to invoke when config changes
    """

    def __init__(
        self,
        redis_url: str,
        on_config_change: Callable[[], Awaitable[None]],
        source_id: str = "api",
        *,
        local_revision: Optional[Callable[[], int]] = None,
        authoritative_revision: Optional[Callable[[], int]] = None,
        reconcile_interval_seconds: float = 5.0,
    ):
        """
        Initialize the ConfigSyncService.

        Args:
            redis_url: Redis connection URL (e.g., 'redis://localhost:6379')
            on_config_change: Async function to call when config should be reloaded
        """
        self.redis_url = redis_url
        self.on_config_change = on_config_change
        self.source_id = source_id
        self.local_revision = local_revision
        self.authoritative_revision = authoritative_revision
        self.reconcile_interval_seconds = reconcile_interval_seconds
        self._redis_client: Optional[redis.Redis] = None
        self._pubsub: Optional[redis.client.PubSub] = None
        self._listener_task: Optional[asyncio.Task] = None
        self._reconcile_task: Optional[asyncio.Task] = None
        self._running = False

    def _is_self_notification(self, source: str) -> bool:
        """Return whether a config notification originated from this process."""
        return source == self.source_id

    async def start(self) -> None:
        """
        Start the configuration sync listener.

        Connects to Redis and subscribes to the config update channel.
        Spawns a background task to listen for messages.
        """
        self._running = True
        if self.local_revision and self.authoritative_revision:
            self._reconcile_task = asyncio.create_task(self._reconcile_revisions())
        try:
            self._redis_client = redis.from_url(self.redis_url, decode_responses=True)
            self._pubsub = self._redis_client.pubsub()
            await self._pubsub.subscribe(CONFIG_CHANNEL)

            self._listener_task = asyncio.create_task(self._listen_for_updates())

            logger.info(f"ConfigSyncService started, subscribed to '{CONFIG_CHANNEL}'")
        except Exception as e:
            logger.error(f"Failed to start ConfigSyncService: {e}")
            # Pub/Sub is only a wake-up accelerator. The durable revision poller
            # continues when Redis subscription is unavailable.

    async def stop(self) -> None:
        """Stop the configuration sync listener and cleanup resources."""
        self._running = False

        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass

        if self._reconcile_task:
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
            except asyncio.CancelledError:
                pass

        if self._pubsub:
            await self._pubsub.unsubscribe(CONFIG_CHANNEL)
            await self._pubsub.close()

        if self._redis_client:
            await self._redis_client.close()

        logger.info("ConfigSyncService stopped")

    async def _listen_for_updates(self) -> None:
        """
        Background task that listens for configuration update messages.

        When a message is received, it triggers a configuration reload
        via the on_config_change callback.
        """
        logger.info("ConfigSyncService listener started")

        while self._running:
            try:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )

                if message and message["type"] == "message":
                    data = json.loads(message["data"]) if message["data"] else {}
                    source = data.get("source", "unknown")
                    change_type = data.get("type", "unknown")
                    revision = int(data.get("revision") or 0)

                    if self._is_self_notification(source):
                        logger.debug(
                            "Ignoring self-originated config notification: %s",
                            change_type,
                        )
                        continue

                    logger.info(
                        f"Config change notification received: {change_type} from {source}"
                    )

                    if self.local_revision and revision:
                        local_revision = int(self.local_revision())
                        if revision <= local_revision:
                            logger.debug(
                                "Ignoring non-advancing config revision %s (local=%s)",
                                revision,
                                local_revision,
                            )
                            continue

                    # Invoke the reload callback. State is always re-read from the
                    # durable source; Pub/Sub payloads are never applied directly.
                    await self.on_config_change()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in config sync listener: {e}")
                await asyncio.sleep(1)

    async def _reconcile_revisions(self) -> None:
        """Poll the authoritative head so missed Pub/Sub cannot leave stale replicas."""
        while self._running:
            try:
                await asyncio.sleep(self.reconcile_interval_seconds)
                local_revision = int(self.local_revision())
                remote_revision = int(
                    await asyncio.to_thread(self.authoritative_revision)
                )
                if remote_revision > local_revision:
                    logger.info(
                        "Control-plane revision reconciliation: local=%s authoritative=%s",
                        local_revision,
                        remote_revision,
                    )
                    await self.on_config_change()
                elif remote_revision < local_revision:
                    logger.error(
                        "Authoritative control-plane revision regressed: local=%s remote=%s",
                        local_revision,
                        remote_revision,
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Control-plane revision reconciliation failed: %s", exc)

    async def notify_config_change(
        self,
        change_type: str,
        source: str = "api",
        revision: Optional[int] = None,
    ) -> None:
        """
        Broadcast a configuration change notification to all instances.

        Args:
            change_type: Type of change (e.g., 'cluster_added', 'rule_updated')
            source: Identifier of the source instance
        """
        if not self._redis_client:
            logger.warning("ConfigSyncService not connected, skipping notification")
            return

        try:
            message = json.dumps(
                {
                    "type": change_type,
                    "source": source,
                    "revision": revision,
                }
            )
            await self._redis_client.publish(CONFIG_CHANNEL, message)
            logger.debug(f"Published config change notification: {change_type}")
        except Exception as e:
            logger.error(f"Failed to publish config change: {e}")


# Synchronous wrapper for use in sync contexts
class SyncConfigNotifier:
    """
    Synchronous wrapper for publishing config change notifications.

    Used in synchronous route handlers that need to notify other instances
    of configuration changes.
    """

    def __init__(self, redis_url: str, source_id: str = "api"):
        self.redis_url = redis_url
        self.source_id = source_id
        self._sync_client: Optional[redis.Redis] = None

    def _get_client(self):
        """Get or create a synchronous Redis client."""
        if self._sync_client is None:
            import redis as sync_redis

            self._sync_client = sync_redis.from_url(
                self.redis_url, decode_responses=True
            )
        return self._sync_client

    def notify(
        self,
        change_type: str,
        source: Optional[str] = None,
        revision: Optional[int] = None,
    ) -> None:
        """
        Publish a configuration change notification synchronously.

        Args:
            change_type: Type of change
            source: Source identifier
        """
        try:
            client = self._get_client()
            message = json.dumps(
                {
                    "type": change_type,
                    "source": source or self.source_id,
                    "revision": revision,
                }
            )
            client.publish(CONFIG_CHANNEL, message)
            logger.debug(f"Published config change notification: {change_type}")
        except Exception as e:
            logger.error(f"Failed to publish config change: {e}")

    def publish_durable(
        self,
        *,
        event_type: str,
        revision: int,
        event_id: str,
    ) -> None:
        """Publish one persisted outbox record and surface delivery failure."""
        client = self._get_client()
        message = json.dumps(
            {
                "type": event_type,
                "source": self.source_id,
                "revision": revision,
                "event_id": event_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        client.publish(CONFIG_CHANNEL, message)

    def close(self) -> None:
        """Close the Redis connection. Call on application shutdown."""
        if self._sync_client:
            try:
                self._sync_client.close()
            except Exception as e:
                logger.debug(f"Error closing SyncConfigNotifier Redis client: {e}")
            self._sync_client = None
