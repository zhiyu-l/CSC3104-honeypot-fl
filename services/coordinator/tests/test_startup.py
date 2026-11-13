"""Coordinator start-up helpers: host-name override and SuperNode re-registration."""

import socket

import pytest

from fl_coordinator import flwr_cli
from fl_coordinator.register_nodes import already_registered, parse_keys
from fl_coordinator.trigger import json_result

KEY = "ecdsa-sha2-nistp384 AAAAE2VjZHNhLXNoYTItbmlzdHAzODQAAAAIbmlzdHAzODQ"


def test_parse_overrides():
    assert flwr_cli.parse_overrides("") == {}
    assert flwr_cli.parse_overrides(" Coordinator.FL.internal=127.0.0.1 , b=nlb.example ") == {
        "coordinator.fl.internal": "127.0.0.1",
        "b": "nlb.example",
    }
    with pytest.raises(ValueError):
        flwr_cli.parse_overrides("no-equals-sign")


def test_override_resolves_name_and_leaves_others(monkeypatch):
    calls = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: calls.append(host) or [])
    flwr_cli.install({"coordinator.fl.internal": "127.0.0.1"})
    socket.getaddrinfo("COORDINATOR.fl.internal", 8000)
    socket.getaddrinfo(b"coordinator.fl.internal", 8000)
    socket.getaddrinfo("example.org", 443)
    assert calls == ["127.0.0.1", "127.0.0.1", "example.org"]


def test_parse_keys_names_from_comment():
    text = f"# org keys\n{KEY} org-a\n\n{KEY}\n"
    assert parse_keys(text) == [("org-a", f"{KEY} org-a"), ("node-4", KEY)]
    with pytest.raises(ValueError):
        parse_keys("-----BEGIN PUBLIC KEY-----")


def test_json_result_needs_explicit_success():
    # flwr --format json can exit 0 and still report a failure
    assert json_result('{"success": true, "node-id": 7}') == {"success": True, "node-id": 7}
    assert json_result('{"success": false, "error-message": "Connection refused"}') is None
    assert json_result("not json") is None
    assert json_result("[]") is None


def test_already_registered():
    assert already_registered("Error: SuperNode with this public key is Already registered")
    assert not already_registered("connection refused")
