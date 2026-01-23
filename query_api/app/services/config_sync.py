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

    def __init__(self, redis_url: str, on_config_change: Callable[[], Awaitable[None]]):
        """
        Initialize the ConfigSyncService.

        Args:
            redis_url: Redis connection URL (e.g., 'redis://localhost:6379')
            on_config_change: Async function to call when config should be reloaded
        """
        self.redis_url = redis_url
        self.on_config_change = on_config_change
        self._redis_client: Optional[redis.Redis] = None
        self._pubsub: Optional[redis.client.PubSub] = None
        self._listener_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        """
        Start the configuration sync listener.

        Connects to Redis and subscribes to the config update channel.
        Spawns a background task to listen for messages.
        """
        try:
            self._redis_client = redis.from_url(self.redis_url, decode_responses=True)
            self._pubsub = self._redis_client.pubsub()
            await self._pubsub.subscribe(CONFIG_CHANNEL)

            self._running = True
            self._listener_task = asyncio.create_task(self._listen_for_updates())

            logger.info(f"ConfigSyncService started, subscribed to '{CONFIG_CHANNEL}'")
        except Exception as e:
            logger.error(f"Failed to start ConfigSyncService: {e}")
            # Non-fatal: service will work but without multi-instance sync

    async def stop(self) -> None:
        """Stop the configuration sync listener and cleanup resources."""
        self._running = False

        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
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

                    logger.info(
                        f"Config change notification received: {change_type} from {source}"
                    )

                    # Invoke the reload callback
                    await self.on_config_change()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in config sync listener: {e}")
                await asyncio.sleep(1)

    async def notify_config_change(self, change_type: str, source: str = "api") -> None:
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

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._sync_client: Optional[redis.Redis] = None

    def _get_client(self):
        """Get or create a synchronous Redis client."""
        if self._sync_client is None:
            import redis as sync_redis

            self._sync_client = sync_redis.from_url(
                self.redis_url, decode_responses=True
            )
        return self._sync_client

    def notify(self, change_type: str, source: str = "api") -> None:
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
                    "source": source,
                }
            )
            client.publish(CONFIG_CHANNEL, message)
            logger.debug(f"Published config change notification: {change_type}")
        except Exception as e:
            logger.error(f"Failed to publish config change: {e}")
