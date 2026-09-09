from __future__ import annotations

import json
from pathlib import Path

import yaml


COMPOSE_FILE = Path(__file__).resolve().parents[1] / "docker-compose.yml"
LOCAL_DIRECTORY_CLIENTS = {
    "orderbook-watcher",
    "jmwalletd",
    "jam-playwright",
    "maker",
    "migration-maker",
    "taker",
    "taker-reference",
    "maker1",
    "maker2",
    "maker3",
    "maker4",
    "maker5",
    "maker-neutrino",
    "taker-neutrino",
}


def test_local_directory_clients_explicitly_allow_development_clearnet() -> None:
    compose = yaml.safe_load(COMPOSE_FILE.read_text())

    for service_name in LOCAL_DIRECTORY_CLIENTS:
        environment = compose["services"][service_name]["environment"]
        assert "NETWORK_CONFIG__ALLOW_CLEARNET_CONNECTIONS=true" in environment


def test_orderbook_watcher_covers_every_maker_startup_directory() -> None:
    """A maker can announce on whichever configured directory connects first."""
    services = yaml.safe_load(COMPOSE_FILE.read_text())["services"]
    watcher = dict(
        item.split("=", 1) for item in services["orderbook-watcher"]["environment"]
    )
    watcher_nodes = set(watcher["DIRECTORY_NODES"].split(","))
    watcher_ids = json.loads(watcher["NETWORK_CONFIG__NICK_AUTH_DIRECTORY_IDS"])

    for name in ("maker1", "maker2", "maker3", "maker4", "maker5"):
        maker = dict(item.split("=", 1) for item in services[name]["environment"])
        maker_nodes = set(maker["DIRECTORY_SERVERS"].split(","))
        maker_ids = json.loads(maker["NETWORK_CONFIG__NICK_AUTH_DIRECTORY_IDS"])
        assert maker_nodes <= watcher_nodes, name
        for node in maker_nodes:
            assert watcher_ids[node] == maker_ids[node], (name, node)
