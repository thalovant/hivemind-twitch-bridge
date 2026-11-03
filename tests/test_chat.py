"""TwitchChat against a real local WebSocket server that behaves like Twitch chat.

Every test here opens real WebSocket connections to ``FakeTwitch``
(``tests/fake_twitch.py``): login, capabilities, JOIN, PING/PONG, drops,
RECONNECT, the outgoing queue and its limits all go over the wire.
"""
import asyncio
import time

import pytest

from fake_twitch import FakeTwitch
from twitch_bridge.chat import (
    CAPABILITIES,
    SendLimiter,
    SlidingWindow,
    TwitchChat,
    normalize_token,
)
from twitch_bridge.errors import TokenRejected
from twitch_bridge.irc import MAX_MESSAGE_LENGTH, chat_length, parse_line

LOGIN = "hivebot"


def make_chat(server, channels=("alpha", "beta"), token="good-token", received=None, **options):
    options.setdefault("backoff_base", 0.05)
    options.setdefault("backoff_cap", 0.2)
    tokens = token if callable(token) else (lambda: token)
    sink = received if received is not None else []
    return TwitchChat(LOGIN, tokens, channels, sink.append, url=server.url, **options)


def run(scenario, timeout=15):
    asyncio.run(asyncio.wait_for(scenario(), timeout))


async def started(chat):
    task = asyncio.create_task(chat.run())
    return task


async def stop(chat, task):
    chat.stop()
    await asyncio.wait_for(task, 5)


async def until(predicate, timeout=5.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


# ---------------------------------------------------------------------------
# login and channels
# ---------------------------------------------------------------------------
def test_logs_in_with_tags_and_commands_and_joins_exactly_the_channels():
    async def scenario():
        async with FakeTwitch() as server:
            chat = make_chat(server)
            task = await started(chat)
            await until(chat.ready, what="ready")
            lines = server.lines(1)
            assert lines[0] == f"CAP REQ :{CAPABILITIES}"
            assert CAPABILITIES == "twitch.tv/tags twitch.tv/commands"
            assert lines[1] == "PASS oauth:good-token"
            assert lines[2] == f"NICK {LOGIN}"
            joins = [line for line in lines if line.startswith("JOIN")]
            assert joins == ["JOIN #alpha", "JOIN #beta"]
            assert chat.joined == {"alpha", "beta"}
            await stop(chat, task)
            assert not chat.ready()
    run(scenario)


def test_the_oauth_prefix_is_optional_and_never_doubled():
    assert normalize_token("abc") == "oauth:abc"
    assert normalize_token("oauth:abc") == "oauth:abc"
    assert normalize_token(" OAuth:abc\n") == "oauth:abc"
    for broken in ("", "oauth:", "two words", "line\nbreak"):
        with pytest.raises(TokenRejected):
            normalize_token(broken)

    async def scenario():
        async with FakeTwitch() as server:
            chat = make_chat(server, token="oauth:good-token")
            task = await started(chat)
            await until(chat.ready, what="ready")
            assert "PASS oauth:good-token" in server.lines(1)
            await stop(chat, task)
    run(scenario)


def test_a_refused_login_raises_token_rejected_and_is_not_retried():
    async def scenario():
        async with FakeTwitch() as server:
            chat = make_chat(server, token="wrong-token")
            with pytest.raises(TokenRejected, match="Login authentication failed"):
                await asyncio.wait_for(chat.run(), 5)
            await asyncio.sleep(0.3)  # a retry would have connected by now
            assert server.connections == 1
            assert not chat.ready()
    run(scenario)


def test_not_ready_until_every_channel_is_joined_and_missing_joins_are_retried():
    async def scenario():
        async with FakeTwitch(silent_channels={"beta"}) as server:
            chat = make_chat(server, join_retry=0.2)
            task = await started(chat)
            await until(lambda: "alpha" in chat.joined, what="alpha joined")
            await asyncio.sleep(0.5)
            assert not chat.ready(), "beta never confirmed the join"
            assert server.lines(1).count("JOIN #beta") >= 2, "the join was not retried"
            server.silent_channels.clear()
            await until(chat.ready, what="ready once beta answers")
            await stop(chat, task)
    run(scenario)


def test_leaving_a_channel_makes_the_bridge_not_ready_and_it_joins_again_at_once():
    async def scenario():
        async with FakeTwitch() as server:
            # a long retry timer: the rejoin after a PART must not wait for it
            chat = make_chat(server, join_retry=30.0)
            task = await started(chat)
            await until(chat.ready, what="ready")
            server.silent_channels.add("beta")  # hold the rejoin's echo back
            await server.push(f":{LOGIN}!{LOGIN}@{LOGIN}.tmi.twitch.tv PART #beta")
            await until(lambda: server.lines(1).count("JOIN #beta") == 2, timeout=2,
                        what="an immediate JOIN after the PART")
            assert not chat.ready()
            # Twitch's (late) echo of that JOIN
            await server.push(f":{LOGIN}!{LOGIN}@{LOGIN}.tmi.twitch.tv JOIN #beta")
            await until(chat.ready, timeout=2, what="ready again")
            assert server.lines(1).count("JOIN #beta") == 2
            await stop(chat, task)
    run(scenario)


def test_a_login_twitch_never_answers_times_out_and_reconnects():
    async def scenario():
        async with FakeTwitch(welcome=False) as server:
            chat = make_chat(server, login_timeout=0.2)
            task = await started(chat)
            await until(lambda: server.connections >= 2, what="a second attempt")
            assert not chat.ready()
            await stop(chat, task)
    run(scenario)


# ---------------------------------------------------------------------------
# keeping the connection alive
# ---------------------------------------------------------------------------
def test_the_servers_ping_gets_a_pong_with_the_same_text():
    async def scenario():
        async with FakeTwitch() as server:
            chat = make_chat(server)
            task = await started(chat)
            await until(chat.ready, what="ready")
            await server.push("PING :tmi.twitch.tv")
            await until(lambda: "PONG :tmi.twitch.tv" in server.lines(1), what="PONG")
            await stop(chat, task)
    run(scenario)


def test_silence_gets_a_ping_and_an_unanswered_ping_a_new_connection():
    async def scenario():
        async with FakeTwitch(answer_ping=False) as server:
            chat = make_chat(server, keepalive=0.2, pong_timeout=0.2)
            task = await started(chat)
            await until(chat.ready, what="ready")
            await until(lambda: "PING :tmi.twitch.tv" in server.lines(1), what="client PING")
            await until(lambda: server.connections >= 2, what="reconnect after a dead PING")
            await until(chat.ready, what="ready again")
            await stop(chat, task)
    run(scenario)


def test_an_answered_ping_keeps_the_connection():
    async def scenario():
        async with FakeTwitch(answer_ping=True) as server:
            chat = make_chat(server, keepalive=0.1, pong_timeout=0.3)
            task = await started(chat)
            await until(chat.ready, what="ready")
            await asyncio.sleep(0.8)
            assert server.lines(1).count("PING :tmi.twitch.tv") >= 2
            assert server.connections == 1
            await stop(chat, task)
    run(scenario)


def test_a_dropped_connection_comes_back_and_joins_every_channel_again():
    async def scenario():
        async with FakeTwitch() as server:
            chat = make_chat(server)
            task = await started(chat)
            await until(chat.ready, what="ready")
            await server.drop()
            await until(lambda: not chat.ready(), what="not ready after the drop")
            await until(lambda: server.connections == 2 and chat.ready(), what="back")
            assert [line for line in server.lines(2) if line.startswith("JOIN")] == [
                "JOIN #alpha", "JOIN #beta"]
            assert chat.logins == 2
            await stop(chat, task)
    run(scenario)


def test_reconnect_backoff_grows_while_the_server_is_away():
    async def scenario():
        chat = TwitchChat(LOGIN, lambda: "good-token", ["alpha"], lambda m: None,
                          url="ws://127.0.0.1:9", backoff_base=1.0, backoff_cap=60.0)
        delays = [chat._backoff(attempt) for attempt in range(8)]
        for attempt, delay in enumerate(delays):
            ceiling = min(60.0, 2 ** attempt)
            assert ceiling * 0.5 <= delay <= ceiling
        assert delays[-1] >= 30.0
    run(scenario)


def test_twitchs_reconnect_command_reconnects_at_once():
    async def scenario():
        async with FakeTwitch() as server:
            # a backoff of at least 2.5 s would show
            chat = make_chat(server, backoff_base=5.0, backoff_cap=10.0)
            task = await started(chat)
            await until(chat.ready, what="ready")
            began = time.monotonic()
            await server.push(":tmi.twitch.tv RECONNECT")
            await until(lambda: server.connections == 2 and chat.ready(), what="back")
            assert time.monotonic() - began < 2.0
            await stop(chat, task)
    run(scenario)


def test_a_planned_reconnect_logs_in_with_the_new_token():
    async def scenario():
        async with FakeTwitch(token="first") as server:
            current = {"token": "first"}
            chat = make_chat(server, token=lambda: current["token"], backoff_base=5.0)
            task = await started(chat)
            await until(chat.ready, what="ready")
            current["token"] = "second"
            server.token = "second"
            chat.request_reconnect()
            await until(lambda: server.connections == 2 and chat.ready(), what="second login")
            assert "PASS oauth:second" in server.lines(2)
            await stop(chat, task)
    run(scenario)


# ---------------------------------------------------------------------------
# incoming chat
# ---------------------------------------------------------------------------
def test_chat_messages_arrive_with_their_tags_and_the_bots_own_do_not():
    async def scenario():
        received = []
        async with FakeTwitch() as server:
            chat = make_chat(server, received=received)
            task = await started(chat)
            await until(chat.ready, what="ready")
            await server.chat("alpha", "alice", "@hivebot what time is it", msg_id="m-7",
                              user_id="1001", reply_parent_user_login="HiveBot")
            await server.chat("alpha", LOGIN, "my own line")
            await server.chat("elsewhere", "bob", "not our channel")
            await server.push("@badges=;user-id=5 :carol!carol@carol.tmi.twitch.tv "
                              f"WHISPER {LOGIN} :psst")
            await server.chat("beta", "dave", "\x01ACTION waves\x01")
            await until(lambda: len(received) >= 2, what="two messages")
            await asyncio.sleep(0.1)
            assert [(m.channel, m.user_login, m.text) for m in received] == [
                ("alpha", "alice", "@hivebot what time is it"),
                ("beta", "dave", "waves"),
            ]
            first = received[0]
            assert (first.message_id, first.user_id, first.reply_parent_login) == (
                "m-7", "1001", "hivebot")
            await stop(chat, task)
    run(scenario)


def test_one_frame_with_several_lines_is_read_line_by_line():
    async def scenario():
        received = []
        async with FakeTwitch() as server:
            chat = make_chat(server, received=received)
            task = await started(chat)
            await until(chat.ready, what="ready")
            await server.push(
                "@id=a;user-id=1 :ann!ann@ann.tmi.twitch.tv PRIVMSG #alpha :one",
                "PING :tmi.twitch.tv",
                "@id=b;user-id=2 :ben!ben@ben.tmi.twitch.tv PRIVMSG #beta :two")
            await until(lambda: len(received) == 2, what="both messages")
            await until(lambda: "PONG :tmi.twitch.tv" in server.lines(1), what="PONG")
            await stop(chat, task)
    run(scenario)


# ---------------------------------------------------------------------------
# outgoing chat
# ---------------------------------------------------------------------------
def test_a_reply_threads_under_the_question_and_addresses_the_person():
    async def scenario():
        async with FakeTwitch() as server:
            chat = make_chat(server)
            task = await started(chat)
            await until(chat.ready, what="ready")
            assert chat.say("alpha", "it is noon", reply_to="m-7", mention="alice") == 1
            await until(lambda: server.privmsgs, what="the reply")
            line = server.privmsgs[0][1]
            assert line == "@reply-parent-msg-id=m-7 PRIVMSG #alpha :@alice it is noon"
            await stop(chat, task)
    run(scenario)


def test_a_long_reply_is_split_under_500_characters_in_order():
    async def scenario():
        async with FakeTwitch() as server:
            chat = make_chat(server, limiter=SendLimiter(limit=10, window=1.0, channel_gap=0.0))
            task = await started(chat)
            await until(chat.ready, what="ready")
            words = [f"w{index:03d}" for index in range(250)]
            count = chat.say("alpha", " ".join(words), reply_to="m-1", mention="alice")
            assert count == 3
            await until(lambda: len(server.privmsgs) == 3, what="three messages")
            texts = [parse_line(line).trailing for _, line in server.privmsgs]
            assert all(chat_length(text) <= MAX_MESSAGE_LENGTH for text in texts)
            assert texts[0].startswith("@alice w000")
            assert all(text.startswith("@alice ") for text in texts)
            assert " ".join(text[len("@alice "):] for text in texts).split(" ") == words
            assert all(parse_line(line).tags == {"reply-parent-msg-id": "m-1"}
                       for _, line in server.privmsgs)
            await stop(chat, task)
    run(scenario)


def test_a_reply_cannot_inject_a_second_irc_command():
    async def scenario():
        async with FakeTwitch() as server:
            chat = make_chat(server)
            task = await started(chat)
            await until(chat.ready, what="ready")
            chat.say("alpha", "hi\r\nPRIVMSG #beta :spam", mention="bob\r\nJOIN #x")
            await until(lambda: server.privmsgs, what="the reply")
            await asyncio.sleep(0.2)
            assert [line for _, line in server.privmsgs] == ["PRIVMSG #alpha :hi PRIVMSG #beta :spam"]
            assert not any(line.startswith("JOIN #x") for line in server.lines())
            await stop(chat, task)
    run(scenario)


def test_nothing_is_sent_to_a_channel_that_is_not_configured():
    async def scenario():
        async with FakeTwitch() as server:
            chat = make_chat(server)
            task = await started(chat)
            await until(chat.ready, what="ready")
            assert chat.say("gamma", "hello") == 0
            await asyncio.sleep(0.2)
            assert server.privmsgs == []
            await stop(chat, task)
    run(scenario)


def test_replies_queued_while_disconnected_go_out_after_the_login():
    async def scenario():
        async with FakeTwitch() as server:
            chat = make_chat(server, backoff_base=0.3, backoff_cap=0.3)
            task = await started(chat)
            await until(chat.ready, what="ready")
            await server.drop()
            await until(lambda: not chat.ready(), what="disconnected")
            chat.say("alpha", "late answer")
            await until(lambda: server.privmsgs, what="the queued reply")
            assert server.connections == 2
            await stop(chat, task)
    run(scenario)


def test_the_wire_never_carries_more_than_the_limit_in_any_window():
    """A small limit (3 per second, 0.2 s between messages in a channel) so the
    test runs fast; the default numbers are checked without a server below."""
    async def scenario():
        async with FakeTwitch() as server:
            limiter = SendLimiter(limit=3, window=1.0, channel_gap=0.2)
            chat = make_chat(server, limiter=limiter)
            task = await started(chat)
            await until(chat.ready, what="ready")
            for index in range(5):
                chat.say("alpha", f"a{index}")
                chat.say("beta", f"b{index}")
            await until(lambda: len(server.privmsgs) == 10, timeout=10, what="all ten")
            times = [at for at, _ in server.privmsgs]
            for start in times:
                in_window = [at for at in times if start <= at < start + 0.95]
                assert len(in_window) <= 3, "more than 3 messages inside one window"
            for channel in ("#alpha", "#beta"):
                sent = [at for at, line in server.privmsgs if f"PRIVMSG {channel} " in line]
                assert all(b - a >= 0.18 for a, b in zip(sent, sent[1:]))
            # the queue kept each channel's order
            alpha = [parse_line(line).trailing for _, line in server.privmsgs
                     if "PRIVMSG #alpha " in line]
            assert alpha == [f"a{index}" for index in range(5)]
            await stop(chat, task)
    run(scenario)


def test_the_default_limits_stay_under_twitchs_20_per_30_seconds():
    limiter = SendLimiter()
    now = 1000.0
    sent = []
    # three channels, as fast as the limiter allows, for two minutes
    while now < 1120.0:
        for channel in ("a", "b", "c"):
            if limiter.wait(channel, now) == 0:
                limiter.record(channel, now)
                sent.append((now, channel))
        now += 0.05
    for start, _ in sent:
        window = [entry for entry in sent if start <= entry[0] < start + 30.0]
        assert len(window) < 20
    for channel in ("a", "b", "c"):
        times = [at for at, name in sent if name == channel]
        assert all(b - a >= 1.0 for a, b in zip(times, times[1:]))
    assert len(sent) >= 18 * 4 - 3, "the limiter is far stricter than it needs to be"


def test_slow_mode_and_a_rate_limit_notice_hold_a_channel_back():
    limiter = SendLimiter()
    limiter.set_slow_mode("a", 30)
    limiter.record("a", 0.0)
    assert limiter.wait("a", 10.0) == pytest.approx(20.0)
    assert limiter.wait("b", 10.0) == 0.0
    limiter.pause("b", 30.0, now=10.0)
    assert limiter.wait("b", 15.0) == pytest.approx(25.0)


def test_a_sliding_window_frees_a_slot_when_the_oldest_event_leaves_it():
    window = SlidingWindow(2, 10.0)
    window.record(0.0)
    window.record(1.0)
    assert window.wait(5.0) == pytest.approx(5.0)
    assert window.wait(10.0) == 0.0


def test_a_slow_mode_roomstate_is_respected_on_the_wire():
    async def scenario():
        async with FakeTwitch() as server:
            chat = make_chat(server, limiter=SendLimiter(limit=10, window=1.0, channel_gap=0.0))
            task = await started(chat)
            await until(chat.ready, what="ready")
            await server.push("@room-id=42;slow=1 :tmi.twitch.tv ROOMSTATE #alpha")
            await asyncio.sleep(0.05)
            chat.say("alpha", "one")
            chat.say("alpha", "two")
            await until(lambda: len(server.privmsgs) == 2, what="both")
            assert server.privmsgs[1][0] - server.privmsgs[0][0] >= 0.95
            await stop(chat, task)
    run(scenario)


def test_nothing_is_delivered_from_a_channel_the_bot_is_not_in():
    """Twitch does not guarantee order: a line can come before the JOIN is
    confirmed or after the bot's own PART."""
    async def scenario():
        received = []
        async with FakeTwitch(silent_channels={"beta"}) as server:
            chat = make_chat(server, received=received, join_retry=30.0)
            task = await started(chat)
            await until(lambda: "alpha" in chat.joined, what="alpha joined")
            await server.chat("beta", "bob", "@hivebot before the join")
            server.silent_channels.add("alpha")  # alpha's rejoin will not be echoed
            await server.push(f":{LOGIN}!{LOGIN}@{LOGIN}.tmi.twitch.tv PART #alpha")
            await until(lambda: "alpha" not in chat.joined, what="alpha left")
            await server.chat("alpha", "carol", "@hivebot after the part")
            server.silent_channels.clear()
            await server.push(f":{LOGIN}!{LOGIN}@{LOGIN}.tmi.twitch.tv JOIN #alpha")
            await until(lambda: "alpha" in chat.joined, what="alpha back")
            await server.chat("alpha", "dave", "@hivebot once joined")
            await until(lambda: received, what="dave's message")
            await asyncio.sleep(0.1)
            assert [message.user_login for message in received] == ["dave"]
            await stop(chat, task)
    run(scenario)


def test_a_reply_id_or_mention_of_another_type_cannot_stop_the_sender():
    async def scenario():
        async with FakeTwitch() as server:
            chat = make_chat(server)
            task = await started(chat)
            await until(chat.ready, what="ready")
            assert chat.say("alpha", "first", reply_to=12345, mention=678) == 1
            assert chat.say("alpha", 42, mention=["alice"]) == 1
            assert chat.say("alpha", "third", mention="alice") == 1
            await until(lambda: len(server.privmsgs) == 3, what="all three")
            assert [line for _, line in server.privmsgs] == [
                "@reply-parent-msg-id=12345 PRIVMSG #alpha :first",
                "PRIVMSG #alpha :42",
                "PRIVMSG #alpha :@alice third"]
            await stop(chat, task)
    run(scenario)


def test_the_sender_survives_an_item_it_cannot_send():
    async def scenario():
        async with FakeTwitch() as server:
            chat = make_chat(server)
            task = await started(chat)
            await until(chat.ready, what="ready")
            from twitch_bridge.chat import _Outgoing
            # an item put on the queue directly, around say()'s checks
            chat._pending.setdefault("alpha", __import__("collections").deque()).append(
                _Outgoing("alpha", object(), None, time.monotonic()))
            chat.say("alpha", "still sending")
            await until(lambda: server.privmsgs, what="the next message")
            assert [line for _, line in server.privmsgs] == ["PRIVMSG #alpha :still sending"]
            await stop(chat, task)
    run(scenario)


# ---------------------------------------------------------------------------
# Twitch runs a chat message that starts with "/" or "." as a command
# ---------------------------------------------------------------------------
COMMANDS = ("/ban alice", ".timeout alice 600", "/me dances", ". ban", "/announce hi")


@pytest.mark.parametrize("answer", COMMANDS)
def test_an_answer_with_nobody_to_address_is_never_a_command(answer):
    async def scenario():
        async with FakeTwitch() as server:
            chat = make_chat(server)
            task = await started(chat)
            await until(chat.ready, what="ready")
            assert chat.say("alpha", answer) == 1
            await until(lambda: server.privmsgs, what="the reply")
            text = parse_line(server.privmsgs[0][1]).trailing
            assert not text.startswith(("/", "."))
            assert text.endswith(answer)
            await stop(chat, task)
    run(scenario)


@pytest.mark.parametrize("mention", ["alice", None])
def test_no_part_of_a_long_answer_starts_like_a_command(mention):
    """A crafted answer can put ``/ban`` or ``.timeout`` exactly where a part begins."""
    async def scenario():
        async with FakeTwitch() as server:
            chat = make_chat(server, limiter=SendLimiter(limit=20, window=1.0, channel_gap=0.0))
            task = await started(chat)
            await until(chat.ready, what="ready")
            # every word is a command, so whatever a part starts with, it is one
            answer = " ".join(["/ban", ".timeout"] * 120)
            count = chat.say("alpha", answer, mention=mention)
            assert count >= 2
            await until(lambda: len(server.privmsgs) == count, what="every part")
            texts = [parse_line(line).trailing for _, line in server.privmsgs]
            assert all(chat_length(text) <= MAX_MESSAGE_LENGTH for text in texts)
            assert not any(text.startswith(("/", ".")) for text in texts)
            if mention:
                assert all(text.startswith("@alice ") for text in texts)
            await stop(chat, task)
    run(scenario)


def test_a_line_the_bridge_cannot_act_on_is_skipped_not_fatal():
    """``"²".isdigit()`` is True and ``float("²")`` raises; a NUL makes the PONG
    unsendable. Either used to end the chat task, and with it the process."""
    async def scenario():
        async with FakeTwitch() as server:
            received = []
            chat = make_chat(server, channels=("alpha",), received=received)
            task = await started(chat)
            await until(chat.ready, what="ready")
            await server.push("@room-id=42;slow=² :tmi.twitch.tv ROOMSTATE #alpha",
                              "PING :tmi\x00.twitch.tv")
            await server.chat("alpha", "alice", "@hivebot still there?")
            await until(lambda: received, what="the next message")
            assert not task.done()
            assert server.connections == 1
            await stop(chat, task)
    run(scenario)


@pytest.mark.parametrize("reply_to", ["m\x001", "m 1", "m\r\n1", "x" * 200])
def test_an_odd_reply_id_from_the_hub_still_sends_the_answer_without_it(reply_to):
    async def scenario():
        async with FakeTwitch() as server:
            chat = make_chat(server)
            task = await started(chat)
            await until(chat.ready, what="ready")
            assert chat.say("alpha", "it is noon", reply_to=reply_to, mention="alice") == 1
            await until(lambda: server.privmsgs, what="the reply")
            line = server.privmsgs[0][1]
            assert line == "PRIVMSG #alpha :@alice it is noon"
            assert "\x00" not in "".join(server.lines())
            await stop(chat, task)
    run(scenario)
