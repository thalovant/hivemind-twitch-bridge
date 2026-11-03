"""The whole bridge: fake hub client, real WebSocket chat server, real OAuth server.

``HiveMindTwitchBridge.run()`` runs exactly as the entry point runs it
(``asyncio.run`` on its own thread here); Twitch chat is ``FakeTwitch`` over a
real WebSocket and the token endpoints are ``FakeTwitchId`` over real HTTP.
Only the HiveMind client is a stand-in (``FakeHub``); tests/e2e drives a real
hub.
"""
import threading
import urllib.error
import urllib.request

import pytest

import twitch_bridge
from fake_twitch import FakeHub, FakeTwitchId, ThreadedFakeTwitch
from twitch_bridge import (
    DEFAULT_HANDSHAKE_MAX_RETRIES,
    EXIT_HUB_FAILED,
    EXIT_OK,
    EXIT_TOKEN_REJECTED,
    HiveMindTwitchBridge,
)

FAST_CHAT = {"backoff_base": 0.05, "backoff_cap": 0.2}
FAST_AUTH = {"retry_base": 0.05, "retry_cap": 0.2}


def make_bridge(chat_server, id_server, hub=None, termination=None, **options):
    options.setdefault("channels", ["alpha", "beta"])
    if "oauth_token" not in options:
        options.setdefault("client_id", "cid")
        options.setdefault("client_secret", "secret")
        options.setdefault("refresh_token", "refresh-0")
    options.setdefault("chat_options", dict(FAST_CHAT))
    options.setdefault("auth_options", dict(FAST_AUTH))
    return HiveMindTwitchBridge(
        "hivebot", client=hub or FakeHub(), chat_url=chat_server.url,
        auth_url=id_server.url if id_server else "http://127.0.0.1:9/oauth2",
        termination_log=str(termination) if termination else "/nonexistent/termination-log",
        **options)


class Running:
    """``bridge.run()`` on a thread, with its exit status."""

    def __init__(self, bridge):
        self.bridge = bridge
        self.code = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        self.code = self.bridge.run()

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.bridge.stop()
        self._thread.join(10)
        assert not self._thread.is_alive(), "the bridge did not stop"

    def wait(self, timeout=10):
        self._thread.join(timeout)
        assert not self._thread.is_alive(), "the bridge is still running"
        return self.code


def accept_access_tokens(password):
    return password.startswith("oauth:access-")


# ---------------------------------------------------------------------------
# the round trip
# ---------------------------------------------------------------------------
def test_a_question_reaches_the_hub_and_the_answer_comes_back_as_a_reply():
    hub = FakeHub()
    with ThreadedFakeTwitch(accept=accept_access_tokens) as chat, FakeTwitchId() as ids:
        bridge = make_bridge(chat, ids, hub)
        with Running(bridge):
            chat.wait_for(bridge.ready, what="ready")
            chat.call(chat.fake.chat("alpha", "alice", "@HiveBot what time is it",
                                     msg_id="m-1", user_id="1001"))
            chat.wait_for(lambda: hub.utterances(), what="the utterance")
            question = hub.utterances()[0]
            assert question.data == {"utterances": ["what time is it"], "lang": "en-us"}
            assert question.context["twitch_channel"] == "alpha"
            assert question.context["twitch_message_id"] == "m-1"
            assert question.context["user"] == {"twitch_username": "alice",
                                                "twitch_user_id": "1001"}
            assert question.context["session"] == {"session_id": "twitch-alpha-1001"}

            hub.answer(question, "it is noon")
            chat.wait_for(lambda: chat.fake.privmsgs, what="the reply")
            assert chat.fake.privmsgs[0][1] == (
                "@reply-parent-msg-id=m-1 PRIVMSG #alpha :@alice it is noon")
    assert hub.connect_kwargs == {"site_id": "twitch",
                                  "handshake_max_retries": DEFAULT_HANDSHAKE_MAX_RETRIES}
    assert hub.closed


def test_ready_needs_the_hub_session_and_every_channel_and_is_served_on_readyz():
    hub = FakeHub()
    with ThreadedFakeTwitch(accept=accept_access_tokens, silent_channels={"beta"}) as chat, \
            FakeTwitchId() as ids:
        bridge = make_bridge(chat, ids, hub, health_port=0,
                             chat_options={**FAST_CHAT, "join_retry": 0.2})
        with Running(bridge):
            chat.wait_for(lambda: bridge._health is not None, what="health server")
            port = bridge._health.port
            chat.wait_for(lambda: "alpha" in bridge.chat.joined, what="alpha joined")
            assert not bridge.ready()
            assert readyz(port) == 503
            assert readyz(port, "/healthz") == 200
            chat.fake.silent_channels.clear()
            chat.wait_for(bridge.ready, what="ready")
            assert readyz(port) == 200
            hub.handshake_event.clear()  # the hub connection dropped
            assert not bridge.ready()
            assert readyz(port) == 503


def readyz(port, path="/readyz"):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as answer:
            return answer.status
    except urllib.error.HTTPError as error:
        return error.code


def test_an_unknown_answer_gets_the_fixed_line_in_the_right_channel():
    hub = FakeHub()
    with ThreadedFakeTwitch(accept=accept_access_tokens) as chat, FakeTwitchId() as ids:
        bridge = make_bridge(chat, ids, hub)
        with Running(bridge):
            chat.wait_for(bridge.ready, what="ready")
            chat.call(chat.fake.chat("beta", "bob", "@hivebot sing", msg_id="m-9"))
            chat.wait_for(lambda: hub.utterances(), what="the utterance")
            hub.answer(hub.utterances()[0], None, "hive.complete_intent_failure")
            chat.wait_for(lambda: chat.fake.privmsgs, what="the reply")
            assert chat.fake.privmsgs[0][1] == (
                "@reply-parent-msg-id=m-9 PRIVMSG #beta :@bob I don't know how to answer that")


def test_answers_for_other_bridges_or_channels_are_not_posted():
    from ovos_bus_client.message import Message
    hub = FakeHub()
    with ThreadedFakeTwitch(accept=accept_access_tokens) as chat, FakeTwitchId() as ids:
        bridge = make_bridge(chat, ids, hub)
        with Running(bridge):
            chat.wait_for(bridge.ready, what="ready")
            for context in ({}, {"twitch_channel": "gamma"}, {"chat_id": 42}):
                hub.handlers["speak"](Message("speak", {"utterance": "x"}, context))
            hub.handlers["speak"](Message("speak", {"utterance": ""}, {"twitch_channel": "alpha"}))
            import time
            time.sleep(0.3)
            assert chat.fake.privmsgs == []


def test_whispers_are_never_forwarded():
    hub = FakeHub()
    with ThreadedFakeTwitch(accept=accept_access_tokens) as chat, FakeTwitchId() as ids:
        bridge = make_bridge(chat, ids, hub)
        with Running(bridge):
            chat.wait_for(bridge.ready, what="ready")
            chat.call(chat.fake.push(
                "@badges=;display-name=Carol;user-id=5 :carol!carol@carol.tmi.twitch.tv "
                "WHISPER hivebot :@hivebot what time is it"))
            chat.call(chat.fake.chat("alpha", "dave", "@hivebot ping"))
            chat.wait_for(lambda: hub.utterances(), what="dave's message")
            assert [m.data["utterances"] for m in hub.utterances()] == [["ping"]]


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------
def test_a_chat_login_twitch_refuses_writes_token_rejected_and_exits_3(tmp_path):
    termination = tmp_path / "termination-log"
    with ThreadedFakeTwitch(token="what-twitch-accepts") as chat, FakeTwitchId() as ids:
        ids.access["static-1"] = "hivebot"  # validates, but chat refuses it
        bridge = make_bridge(chat, ids, termination=termination, oauth_token="static-1")
        assert Running(bridge).__enter__().wait() == EXIT_TOKEN_REJECTED
        assert "NOTICE" not in termination.read_text()
        assert termination.read_text() == "token_rejected"
        assert chat.fake.connections == 1


def test_an_invalid_refresh_grant_writes_token_rejected_before_chat_is_touched(tmp_path):
    termination = tmp_path / "termination-log"
    with ThreadedFakeTwitch() as chat, FakeTwitchId(refresh_token="another") as ids:
        bridge = make_bridge(chat, ids, termination=termination)
        assert Running(bridge).__enter__().wait() == EXIT_TOKEN_REJECTED
        assert termination.read_text() == "token_rejected"
        assert chat.fake.connections == 0


@pytest.mark.parametrize(
    "refused",
    [
        {"client_secret": "other"},  # Twitch answers 403 "invalid client secret"
        {"client_id": "other"},  # Twitch answers 400 "invalid client"
    ],
)
def test_a_refused_client_id_or_secret_writes_token_rejected_and_exits_3(
    tmp_path, monkeypatch, refused
):
    # The owner supplies the client id and secret like the token: a refusal
    # is credentials to replace, so it reads token_rejected like a refused
    # token, with a log line of its own.
    errors = []
    monkeypatch.setattr(twitch_bridge.LOG, "error", lambda message, *args: errors.append(message))
    termination = tmp_path / "termination-log"
    with ThreadedFakeTwitch() as chat, FakeTwitchId(**refused) as ids:
        bridge = make_bridge(chat, ids, termination=termination)
        # the literal status: the contract the operator and the docs name
        assert Running(bridge).__enter__().wait() == 3
        assert termination.read_text() == "token_rejected"
        assert chat.fake.connections == 0
    assert any("client id or secret" in message for message in errors), errors
    assert not any("bot's credentials" in message for message in errors), errors


def test_an_unwritable_termination_log_still_exits_3():
    with ThreadedFakeTwitch() as chat, FakeTwitchId(refresh_token="another") as ids:
        bridge = make_bridge(chat, ids)  # /nonexistent/termination-log
        assert Running(bridge).__enter__().wait() == EXIT_TOKEN_REJECTED


def test_chat_refusing_a_refreshed_token_gets_one_more_refresh():
    accepted = {"oauth:access-2"}
    with ThreadedFakeTwitch(accept=lambda password: password in accepted) as chat, \
            FakeTwitchId() as ids:
        bridge = make_bridge(chat, ids)
        with Running(bridge):
            chat.wait_for(bridge.ready, what="ready with the second token")
            assert len(ids.posts()) == 2
            assert "PASS oauth:access-2" in chat.fake.lines(2)


def test_a_refresh_before_expiry_logs_chat_in_again_with_the_new_token():
    with ThreadedFakeTwitch(accept=accept_access_tokens) as chat, \
            FakeTwitchId(expires_in=2) as ids:
        bridge = make_bridge(chat, ids, auth_options={**FAST_AUTH, "refresh_margin": 1.0})
        with Running(bridge):
            chat.wait_for(bridge.ready, what="ready")
            chat.wait_for(lambda: chat.fake.connections >= 2 and bridge.ready(), timeout=8,
                          what="a second login after the refresh")
            assert "PASS oauth:access-1" in chat.fake.lines(1)
            assert "PASS oauth:access-2" in chat.fake.lines(2)
            assert [form["refresh_token"] for form in ids.posts()][:2] == ["refresh-0",
                                                                          "refresh-1"]


# ---------------------------------------------------------------------------
# the hub
# ---------------------------------------------------------------------------
def test_a_hub_that_never_answers_exits_1_before_any_twitch_request():
    hub = FakeHub(fail_connect=RuntimeError("timed out waiting for handshake"))
    with ThreadedFakeTwitch() as chat, FakeTwitchId() as ids:
        bridge = make_bridge(chat, ids, hub)
        assert Running(bridge).__enter__().wait() == EXIT_HUB_FAILED
        assert ids.requests == [], "a token refresh was spent with the hub down"
        assert chat.fake.connections == 0


def test_a_hub_that_refuses_the_credentials_exits_1():
    hub = FakeHub(fail_connect=ConnectionRefusedError("HiveMind refused this identity"))
    with ThreadedFakeTwitch() as chat, FakeTwitchId() as ids:
        assert Running(make_bridge(chat, ids, hub)).__enter__().wait() == EXIT_HUB_FAILED


def test_a_hub_refusal_while_running_stops_the_bridge_with_1():
    hub = FakeHub()
    with ThreadedFakeTwitch(accept=accept_access_tokens) as chat, FakeTwitchId() as ids:
        running = Running(make_bridge(chat, ids, hub)).__enter__()
        chat.wait_for(running.bridge.ready, what="ready")
        hub.emitter.emit("auth_rejected", "invalid authorization")
        assert running.wait() == EXIT_HUB_FAILED


def test_stop_exits_0_and_messages_wait_for_the_hub():
    hub = FakeHub()
    with ThreadedFakeTwitch(accept=accept_access_tokens) as chat, FakeTwitchId() as ids:
        running = Running(make_bridge(chat, ids, hub)).__enter__()
        chat.wait_for(running.bridge.ready, what="ready")
        hub.handshake_event.clear()
        chat.call(chat.fake.chat("alpha", "alice", "@hivebot hello"))
        import time
        time.sleep(0.3)
        assert hub.utterances() == [], "forwarded while the hub session was down"
        running.bridge.stop()
        assert running.wait() == EXIT_OK


# ---------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("options", [
    {"channels": []},
    {"channels": ["not a channel"]},
    {"channels": [f"c{index}" for index in range(101)]},
    {"session_scope": "room"},
])
def test_bad_settings_are_refused(options):
    with pytest.raises(ValueError):
        HiveMindTwitchBridge("hivebot", **{"channels": ["alpha"], "oauth_token": "t",
                                           "client": FakeHub(), **options})


def test_the_bot_login_must_be_a_login():
    with pytest.raises(ValueError):
        HiveMindTwitchBridge("hive bot", ["alpha"], oauth_token="t", client=FakeHub())


def test_channels_are_lowercased_and_deduplicated():
    bridge = HiveMindTwitchBridge("HiveBot", ["#Alpha", "alpha", "beta"], oauth_token="t",
                                  client=FakeHub())
    assert bridge.channels == ("alpha", "beta")
    assert bridge.bot_login == "hivebot"


def test_a_hub_refusal_before_the_loop_starts_still_stops_the_bridge():
    hub = FakeHub()
    with ThreadedFakeTwitch() as chat, FakeTwitchId() as ids:
        bridge = make_bridge(chat, ids, hub)
        connect = bridge.connect_hivemind

        def connect_then_refused():
            connect()  # the auth_rejected listener is registered now
            hub.emitter.emit("auth_rejected", "invalid authorization")

        bridge.connect_hivemind = connect_then_refused
        assert Running(bridge).__enter__().wait() == EXIT_HUB_FAILED
        assert ids.requests == [], "a token refresh was spent after the hub refused"
        assert chat.fake.connections == 0


def test_a_reply_id_of_another_type_from_the_hub_is_still_sent():
    hub = FakeHub()
    with ThreadedFakeTwitch(accept=accept_access_tokens) as chat, FakeTwitchId() as ids:
        bridge = make_bridge(chat, ids, hub)
        with Running(bridge):
            chat.wait_for(bridge.ready, what="ready")
            from ovos_bus_client.message import Message
            context = {"twitch_channel": "alpha", "twitch_message_id": 777,
                       "user": {"twitch_username": "alice"}}
            hub.handlers["speak"](Message("speak", {"utterance": "one"}, context))
            hub.handlers["speak"](Message("speak", {"utterance": "two"}, context))
            chat.wait_for(lambda: len(chat.fake.privmsgs) == 2, what="both replies")
            assert chat.fake.privmsgs[1][1] == "@reply-parent-msg-id=777 PRIVMSG #alpha :@alice two"
