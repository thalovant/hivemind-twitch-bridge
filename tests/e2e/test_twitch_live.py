"""Live Twitch chat: the real wss://irc-ws.chat.twitch.tv:443, with real credentials.

It needs a real bot account, so it runs only when these are set (it is
skipped otherwise, whatever is installed):

  TWITCH_LIVE_OAUTH_TOKEN  user access token with chat:read and chat:edit
  TWITCH_LIVE_BOT_LOGIN    the bot account's login
  TWITCH_LIVE_CHANNEL      a channel to join (the bot's own channel is fine)

Optional: TWITCH_LIVE_SEND=1 also posts one message into the channel, and
checks that Twitch delivered it: an anonymous read-only login in the same
channel (no extra credentials) must see it within 20 s.

``TWITCH_LIVE=1`` alone runs the credential-free checks below: an anonymous
read-only login (``justinfan<digits>``) that joins two channels and stays up
through keepalive PINGs, and a made-up token that Twitch must refuse with
``Login authentication failed``.

The names differ from the bridge's own settings on purpose, so a deployment
environment never runs this by accident.
"""
import asyncio
import os
import random
import time

import pytest

from twitch_bridge.chat import TwitchChat

TOKEN = os.environ.get("TWITCH_LIVE_OAUTH_TOKEN")
LOGIN = os.environ.get("TWITCH_LIVE_BOT_LOGIN")
CHANNEL = os.environ.get("TWITCH_LIVE_CHANNEL")

needs_credentials = pytest.mark.skipif(
    not (TOKEN and LOGIN and CHANNEL),
    reason="set TWITCH_LIVE_OAUTH_TOKEN, TWITCH_LIVE_BOT_LOGIN and TWITCH_LIVE_CHANNEL "
           "to run against real Twitch chat")
needs_live = pytest.mark.skipif(os.environ.get("TWITCH_LIVE") != "1",
                                reason="set TWITCH_LIVE=1 to talk to real Twitch chat")


@needs_live
def test_an_anonymous_login_joins_and_survives_keepalive_pings():
    async def scenario():
        chat = TwitchChat("justinfan48213", lambda: "anonymous", ["twitchdev", "twitch"],
                          lambda message: None, keepalive=5.0, pong_timeout=10.0)
        task = asyncio.create_task(chat.run())
        deadline = time.monotonic() + 30
        while not chat.ready() and time.monotonic() < deadline:
            if task.done():
                task.result()
            await asyncio.sleep(0.1)
        assert chat.joined == {"twitchdev", "twitch"}
        await asyncio.sleep(12)
        assert chat.ready() and chat.logins == 1
        chat.stop()
        await asyncio.wait_for(task, 10)

    asyncio.run(asyncio.wait_for(scenario(), 60))


@needs_live
def test_twitch_refuses_a_made_up_token_with_login_authentication_failed():
    from twitch_bridge.errors import TokenRejected

    chat = TwitchChat("hivemindbridgeprobe", lambda: "oauth:thisisnotavalidtoken000000000",
                      ["twitchdev"], lambda message: None)
    with pytest.raises(TokenRejected, match="Login authentication failed"):
        asyncio.run(asyncio.wait_for(chat.run(), 30))


@needs_credentials
def test_logs_in_joins_and_stays_connected_over_the_websocket():
    async def scenario():
        chat = TwitchChat(LOGIN, lambda: TOKEN, [CHANNEL], lambda message: None,
                          keepalive=5.0, pong_timeout=10.0)
        task = asyncio.create_task(chat.run())
        deadline = time.monotonic() + 30
        while not chat.ready() and time.monotonic() < deadline:
            if task.done():
                task.result()  # TokenRejected surfaces here
            await asyncio.sleep(0.1)
        assert chat.ready(), "not logged in and joined within 30 s"
        await asyncio.sleep(12)  # at least one keepalive PING answered
        assert chat.ready() and chat.logins == 1
        if os.environ.get("TWITCH_LIVE_SEND") == "1":
            await _send_and_see_it_delivered(chat)
        chat.stop()
        await asyncio.wait_for(task, 10)

    asyncio.run(asyncio.wait_for(scenario(), 150))


async def _send_and_see_it_delivered(chat: TwitchChat) -> None:
    """Post one message as the bot; another viewer must receive it.

    A queue that drained only shows the message left the bridge: Twitch can
    still drop it (``msg_duplicate``, a rate limit), which the bridge logs and
    does not record. The viewer is an anonymous read-only login, so the check
    needs no second account; it must not be the bot's login, whose own lines
    TwitchChat drops before ``on_message``.
    """
    seen = []
    observer = TwitchChat(f"justinfan{random.randint(10000, 99999)}", lambda: "anonymous",
                          [CHANNEL], seen.append)
    watcher = asyncio.create_task(observer.run())
    try:
        deadline = time.monotonic() + 30
        while not observer.ready() and time.monotonic() < deadline:
            if watcher.done():
                watcher.result()
            await asyncio.sleep(0.1)
        assert observer.ready(), "the anonymous observer did not join within 30 s"
        text = f"bridge live test {int(time.time())}"  # Twitch drops a repeat within 30 s
        assert chat.say(CHANNEL, text) == 1
        while chat.pending():
            await asyncio.sleep(0.1)

        def delivered():
            return any(message.user_login == LOGIN.lower() and message.text == text
                       for message in seen)

        deadline = time.monotonic() + 20
        while not delivered() and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        assert delivered(), "Twitch did not deliver the bot's message to another viewer in 20 s"
    finally:
        observer.stop()
        await asyncio.wait_for(watcher, 10)
