"""Tests for Redis config synchronization helpers."""

import asyncio
import json

from control_plane import AuditEvent, InMemoryControlPlaneStore
from main import dispatch_control_plane_outbox_once
from services.config_sync import ConfigSyncService, SyncConfigNotifier
from services.state_manager import StateManager


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

    notifier.notify("routing_updated:products", revision=7)

    assert fake.messages == [
        (
            "imposbro:config_updates",
            {
                "type": "routing_updated:products",
                "source": "query-api-1",
                "revision": 7,
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


def test_revision_reconciler_catches_up_without_pubsub_notification():
    calls = []

    async def reload_state():
        calls.append("reload")
        local["revision"] = 2

    local = {"revision": 1}
    service = ConfigSyncService(
        "redis://unavailable",
        on_config_change=reload_state,
        local_revision=lambda: local["revision"],
        authoritative_revision=lambda: 2,
        reconcile_interval_seconds=0.01,
    )

    async def scenario():
        service._running = True
        task = asyncio.create_task(service._reconcile_revisions())
        await asyncio.sleep(0.04)
        service._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert calls == ["reload"]


def test_durable_outbox_dispatch_continues_after_one_delivery_failure():
    store = InMemoryControlPlaneStore()
    for revision in range(2):
        store.commit(
            expected_revision=revision,
            state={"value": revision + 1},
            audit=AuditEvent(
                actor="test",
                action="changed",
                resource_type="state",
                resource_id="current",
            ),
            event_type=f"state.changed.{revision + 1}",
        )
    manager = StateManager(store=store)

    class PartiallyFailingNotifier:
        def __init__(self):
            self.published = []

        def publish_durable(self, *, event_type, revision, event_id):
            if revision == 1:
                raise RuntimeError("redis unavailable")
            self.published.append((event_type, revision, event_id))

    notifier = PartiallyFailingNotifier()
    delivered, failed = dispatch_control_plane_outbox_once(
        manager,
        notifier,
        limit=100,
    )

    assert (delivered, failed) == (1, 1)
    pending = store.list_unpublished_outbox()
    assert [record.revision for record in pending] == [1]
    assert pending[0].publish_attempts == 1
    assert pending[0].last_error == "redis unavailable"
    assert notifier.published[0][0:2] == ("state.changed.2", 2)
