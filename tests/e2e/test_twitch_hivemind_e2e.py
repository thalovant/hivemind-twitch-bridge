"""End to end: a real hivemind-core hub, the real bridge, a WebSocket Twitch.

The hub is booted in-process by hivescope with a real WebSocket server
(``add_master(..., use_loopback=True)``). The bridge is built exactly as the
entry point builds it: it makes its own ``HiveMessageBusClient`` from key,
password, host and port and runs ``HiveMindTwitchBridge.run()``. Twitch chat
is ``FakeTwitch`` (``tests/fake_twitch.py``), a real WebSocket server that
speaks Twitch's IRC, and the token is validated against ``FakeTwitchId``.

Round trip proven:

    "@hivebot what is the weather" in #mychannel (WebSocket frame)
      -> TwitchChat -> HiveMindTwitchBridge.handle_chat_message
      -> recognizer_loop:utterance over the real hub connection (Noise)
      -> hub agent bus: a responder answers with speak, context kept
      -> hub routes speak back to the bridge's peer
      -> HiveMindTwitchBridge.handle_speak (bus thread)
      -> TwitchChat queue -> PRIVMSG on the WebSocket, threaded under the
         question and addressed to the person.

hivescope redirects the XDG directories for the test session, so the hub's
database and the bridge's identity and Noise key never touch the real home.
"""
import time

from ovos_bus_client.message import Message
from hivescope.topology import TopologyBuilder

from fake_twitch import FakeTwitchId, ThreadedFakeTwitch
from twitch_bridge import HiveMindTwitchBridge

KEY, PASSWORD = "twitch-e2e-key", "twitch-e2e-pwd"


def _host_port(url):
    host, port = url.replace("ws://", "").rstrip("/").split(":")
    return f"ws://{host}", int(port)


def _answer_every_utterance(master, answer):
    bus = master.agent_protocol.bus

    def respond(message: Message):
        context = dict(message.context)
        peer = context.get("peer") or context.get("source")
        context.update({"destination": [peer], "source": "skills"})
        bus.emit(Message("speak", {"utterance": answer}, context))

    bus.on("recognizer_loop:utterance", respond)


def _wait(predicate, timeout=15.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def test_twitch_round_trip_through_a_real_hub():
    builder = TopologyBuilder()
    master = builder.add_master("M0", use_loopback=True)
    master.register_satellite(KEY, password=PASSWORD,
                              allowed_types=["recognizer_loop:utterance"])
    builder.start_all()
    host, port = _host_port(master.network_protocol.url)
    try:
        with ThreadedFakeTwitch(token="static-1") as chat, FakeTwitchId() as ids:
            ids.access["static-1"] = "hivebot"
            bridge = HiveMindTwitchBridge(
                "hivebot", ["mychannel"], oauth_token="oauth:static-1",
                key=KEY, password=PASSWORD, host=host, port=port, site_id="twitch-e2e",
                chat_url=chat.url, auth_url=ids.url)
            _answer_every_utterance(master, "it is sunny")

            import threading
            outcome = {}
            runner = threading.Thread(target=lambda: outcome.update(code=bridge.run()),
                                      daemon=True)
            runner.start()
            try:
                _wait(bridge.ready, what="hub session and #mychannel")
                assert len(master.connected_peers()) == 1

                # not addressed to the bot: never reaches the hub
                chat.call(chat.fake.chat("mychannel", "bob", "just chatting", msg_id="m-0"))
                chat.call(chat.fake.chat("mychannel", "alice", "@hivebot what is the weather",
                                         msg_id="m-1", user_id="1001"))
                _wait(lambda: chat.fake.privmsgs, what="the reply on the Twitch wire")
                time.sleep(0.3)  # let a duplicate show up, if any

                assert [line for _, line in chat.fake.privmsgs] == [
                    "@reply-parent-msg-id=m-1 PRIVMSG #mychannel :@alice it is sunny"]
                master.agent_protocol.assert_injected("recognizer_loop:utterance", count=1)
                question = master.agent_protocol.last_injected("recognizer_loop:utterance")
                assert question.data["utterances"] == ["what is the weather"]
                # the hub keeps the bridge's session id, scoped to this client
                session_id = question.context["session"]["session_id"]
                assert session_id.split(":")[-1] == "twitch-mychannel-1001"
            finally:
                bridge.stop()
                runner.join(15)
            assert outcome.get("code") == 0
    finally:
        builder.stop_all()


def test_a_wrong_hub_password_fails_fast_before_twitch_is_touched():
    builder = TopologyBuilder()
    master = builder.add_master("M0", use_loopback=True)
    master.register_satellite(KEY, password=PASSWORD,
                              allowed_types=["recognizer_loop:utterance"])
    builder.start_all()
    host, port = _host_port(master.network_protocol.url)
    try:
        with ThreadedFakeTwitch(token="static-1") as chat, FakeTwitchId() as ids:
            bridge = HiveMindTwitchBridge(
                "hivebot", ["mychannel"], oauth_token="static-1",
                key=KEY, password="not-the-password", host=host, port=port,
                chat_url=chat.url, auth_url=ids.url, handshake_max_retries=2)
            began = time.monotonic()
            assert bridge.run() == 1
            assert time.monotonic() - began < 60
            assert chat.fake.connections == 0
            assert ids.requests == []
    finally:
        builder.stop_all()
