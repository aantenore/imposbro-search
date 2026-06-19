"""Tests for Redis config synchronization helpers."""

import json

from services.config_sync import ConfigSyncService, SyncConfigNotifier


async def noop_reload():
    return None


class FakeRedis:
    def __init__(self):
        self.messages = []

    def publish(self, channel, message):
        self.messages.append((channel, json.loads(message)))


def test_notifier_uses_configured_source_id():
    """Notifications include the instance source id so writers can ignore themselves."""
    fake = FakeRedis()
    notifier = SyncConfigNotifier("redis://localhost:6379", source_id="query-api-1")
    notifier._sync_client = fake

    notifier.notify("routing_updated:products")

    assert fake.messages == [
        (
            "imposbro:config_updates",
            {
                "type": "routing_updated:products",
                "source": "query-api-1",
            },
        )
    ]


def test_config_sync_service_identifies_self_notifications():
    """A process should not reload state for its own just-published change."""
    service = ConfigSyncService(
        "redis://localhost:6379",
        on_config_change=noop_reload,
        source_id="query-api-1",
    )

    assert service._is_self_notification("query-api-1") is True
    assert service._is_self_notification("query-api-2") is False
