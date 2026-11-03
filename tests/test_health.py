"""Liveness and readiness, as a container platform asks for them."""
import urllib.error
import urllib.request

import pytest

from fake_twitch import FakeHub
from twitch_bridge import HiveMindTwitchBridge
from twitch_bridge.health import HealthServer


def get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as answer:
            return answer.status
    except urllib.error.HTTPError as error:
        return error.code


@pytest.fixture
def served():
    state = {"ready": False}
    server = HealthServer(0, lambda: state["ready"], host="127.0.0.1")
    server.start()
    yield server, state
    server.stop()


def test_liveness_answers_while_not_ready(served):
    server, _ = served
    assert get(server.port, "/healthz") == 200
    assert get(server.port, "/readyz") == 503


def test_readiness_follows_the_bridge(served):
    server, state = served
    state["ready"] = True
    assert get(server.port, "/readyz") == 200
    state["ready"] = False
    assert get(server.port, "/readyz") == 503


def test_a_failing_check_is_not_ready_not_a_crash():
    def broken():
        raise RuntimeError("boom")

    server = HealthServer(0, broken, host="127.0.0.1")
    server.start()
    try:
        assert get(server.port, "/readyz") == 503
        assert get(server.port, "/healthz") == 200
    finally:
        server.stop()


def test_anything_else_is_not_found(served):
    server, _ = served
    assert get(server.port, "/") == 404


def test_the_bridge_is_ready_only_with_a_live_hub_session_and_every_channel_joined():
    hub = FakeHub()
    bridge = HiveMindTwitchBridge("hivebot", ["alpha", "beta"], oauth_token="t", client=hub)
    assert not bridge.ready()
    bridge.connect_hivemind()
    assert not bridge.ready(), "not logged in to Twitch yet"
    bridge.chat._welcomed = True
    bridge.chat._joined.update({"alpha"})
    assert not bridge.ready(), "beta not joined"
    bridge.chat._joined.add("beta")
    assert bridge.ready()
    hub.handshake_event.clear()  # the hub connection dropped
    assert not bridge.ready()
    hub.handshake_event.set()
    bridge.chat._welcomed = False  # Twitch connection lost
    assert not bridge.ready()
