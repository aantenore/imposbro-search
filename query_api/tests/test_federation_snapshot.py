"""Concurrency regression tests for atomic federation runtime replacement."""

import sys
import threading
from pathlib import Path

import pytest


_app_dir = Path(__file__).resolve().parent.parent / "app"
if str(_app_dir) not in sys.path:
    sys.path.insert(0, str(_app_dir))

from services.federation import FederationService


def test_reload_builds_next_runtime_before_one_atomic_swap(monkeypatch):
    federation = FederationService()
    federation.clusters_config = {"old": {"name": "old"}}
    federation.clients = {"old": object()}
    federation.routing_rules = {"products": {"default_cluster": "old", "rules": []}}
    federation.collection_schemas = {"products": {"name": "products", "fields": []}}
    federation.mark_applied_revision(7)
    old_snapshot = federation.runtime_snapshot()

    client_build_started = threading.Event()
    allow_client_build = threading.Event()

    def slow_create_client(config, **_kwargs):
        client_build_started.set()
        assert allow_client_build.wait(timeout=2)
        return {"client_for": config["name"]}

    monkeypatch.setattr(FederationService, "create_client", staticmethod(slow_create_client))

    thread = threading.Thread(
        target=federation.reload_from_state,
        kwargs={
            "clusters_config": {
                "new": {
                    "name": "new",
                    "host": "typesense-new",
                    "port": 8108,
                    "api_key": "secret",
                }
            },
            "routing_rules": {
                "products": {"default_cluster": "new", "rules": []}
            },
            "collection_schemas": {
                "products": {"name": "products", "fields": []}
            },
            "revision": 8,
        },
    )
    thread.start()
    assert client_build_started.wait(timeout=2)

    # While the next snapshot is building, readers retain the complete old view.
    during_reload = federation.runtime_snapshot()
    assert during_reload is old_snapshot
    assert set(during_reload.clients) == {"old"}
    assert during_reload.revision == 7

    allow_client_build.set()
    thread.join(timeout=2)
    assert not thread.is_alive()

    current = federation.runtime_snapshot()
    assert current is not old_snapshot
    assert current.revision == 8
    assert set(current.clients) == {"new"}
    assert current.routing_rules["products"]["default_cluster"] == "new"
    assert set(old_snapshot.clients) == {"old"}


def test_applied_revision_cannot_regress():
    federation = FederationService()
    federation.mark_applied_revision(4)

    with pytest.raises(ValueError, match="cannot move backwards"):
        federation.mark_applied_revision(3)
