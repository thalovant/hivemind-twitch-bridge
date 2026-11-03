"""Nothing anyone wrote or was told, and no token, ends up in a log, at any level.

A debug log is shipped and read far more widely than the chat it came from.
This drives a full session (login, refresh, question, answer) with every
logger at DEBUG, the websockets frame logger included.
"""
import logging
from unittest.mock import MagicMock

import twitch_bridge as bridge_module
import twitch_bridge.auth as auth_module
import twitch_bridge.chat as chat_module
from fake_twitch import FakeHub, FakeTwitchId, ThreadedFakeTwitch
from test_bridge import Running, make_bridge

SECRET_QUESTION = "is my biopsy result back"
SECRET_ANSWER = "Your appointment is on Tuesday"
SECRETS = ("refresh-0", "refresh-1", "access-1", "secret", "hivemind-pw", "hivemind-key")


def test_neither_text_nor_tokens_are_logged(monkeypatch, caplog):
    lines = []
    log = MagicMock()
    for level in ("debug", "info", "warning", "error", "exception"):
        getattr(log, level).side_effect = lambda *a, **k: lines.append(" ".join(map(str, a)))
    for module in (bridge_module, auth_module, chat_module):
        monkeypatch.setattr(module, "LOG", log)
    caplog.set_level(logging.DEBUG)
    logging.getLogger().setLevel(logging.DEBUG)

    hub = FakeHub()
    with ThreadedFakeTwitch(accept=lambda p: p.startswith("oauth:access-")) as chat, \
            FakeTwitchId() as ids:
        bridge = make_bridge(chat, ids, hub, key="hivemind-key", password="hivemind-pw")
        with Running(bridge):
            chat.wait_for(bridge.ready, what="ready")
            chat.call(chat.fake.chat("alpha", "alice", f"@hivebot {SECRET_QUESTION}"))
            chat.wait_for(lambda: hub.utterances(), what="the utterance")
            hub.answer(hub.utterances()[0], SECRET_ANSWER)
            chat.wait_for(lambda: chat.fake.privmsgs, what="the reply")

    assert lines, "the bridge logged nothing; the test would prove nothing"
    # the fake Twitch server's own frame log (websockets.server) is the test's,
    # not the bridge's; everything else, the bridge's websocket client included,
    # must be clean
    records = [f"{record.name}: {record.getMessage()}" for record in caplog.records
               if not record.name.startswith("websockets.server")]
    assert not any(record.startswith("websockets.client") for record in records)
    everything = "\n".join(lines) + "\n" + "\n".join(records)
    assert SECRET_QUESTION not in everything
    assert SECRET_ANSWER not in everything
    for secret in SECRETS:
        assert secret not in everything, secret


def test_a_failure_while_handling_text_never_puts_the_text_in_a_log(caplog):
    """A traceback carries the exception's text, and asyncio logs a failed
    callback with its arguments: the question or the answer, both times."""
    import asyncio
    import threading

    from twitch_bridge.chat import TwitchChat
    from twitch_bridge.irc import parse_line

    caplog.set_level(logging.DEBUG)

    def handler(message):
        raise ValueError(f"cannot handle {message.text}")

    chat = TwitchChat("hivebot", lambda: "t", ["alpha"], handler)
    chat._joined.add("alpha")
    chat._deliver(parse_line(f":alice!alice@alice.tmi.twitch.tv PRIVMSG #alpha :{SECRET_QUESTION}"))

    hub = FakeHub()
    hub.connect()
    bridge = bridge_module.HiveMindTwitchBridge("hivebot", ["alpha"], oauth_token="t", client=hub)
    loop = asyncio.new_event_loop()
    runner = threading.Thread(target=loop.run_forever, daemon=True)
    runner.start()
    try:
        bridge._loop = loop

        def say(channel, text, *args):
            raise RuntimeError(f"cannot send {text}")

        bridge.chat.say = say
        bridge.speak(SECRET_ANSWER, "alpha", reply_to="m-1", mention="alice")
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0.05), loop).result(5)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        runner.join(5)
        loop.close()

    assert caplog.records, "nothing was logged; the test would prove nothing"
    assert SECRET_QUESTION not in caplog.text
    assert SECRET_ANSWER not in caplog.text
