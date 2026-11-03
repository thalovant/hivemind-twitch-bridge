"""In a Twitch channel only what is addressed to the bot reaches the hub."""
import pytest

from fake_twitch import FakeHub
from twitch_bridge import HiveMindTwitchBridge
from twitch_bridge.chat import ChatMessage


def make(**options):
    hub = FakeHub()
    hub.connect()
    bridge = HiveMindTwitchBridge("hivebot", ["alpha"], oauth_token="t", client=hub, **options)
    bridge.connect_hivemind()
    return bridge, hub


def said(text, login="alice", channel="alpha", reply_parent=None, user_id="1001"):
    return ChatMessage(channel=channel, user_login=login, user_id=user_id,
                       display_name=login, text=text, message_id="m-1",
                       reply_parent_login=reply_parent)


@pytest.mark.parametrize("text,forwarded", [
    ("@hivebot what time is it", "what time is it"),
    ("what time is it @HiveBot", "what time is it"),
    ("hey @hivebot, what time is it", "hey what time is it"),
    ("@hivebot: what time is it", "what time is it"),
    ("@hivebot", None),                      # nothing left to ask
    ("what time is it", None),               # not addressed
    ("@hivebotx what time is it", None),     # another account
    ("@hivebot_fan what time is it", None),  # another account
    ("mail me at me@hivebot today", None),   # not a mention
    ("hivebot what time is it", None),       # a mention needs the @
])
def test_only_a_mention_is_forwarded_and_it_is_removed(text, forwarded):
    bridge, hub = make()
    bridge.handle_chat_message(said(text))
    got = [m.data["utterances"][0] for m in hub.utterances()]
    assert got == ([forwarded] if forwarded else [])


def test_a_reply_to_the_bot_is_forwarded_without_the_leading_mention():
    bridge, hub = make()
    bridge.handle_chat_message(said("@hivebot and tomorrow?", reply_parent="hivebot"))
    bridge.handle_chat_message(said("and tomorrow?", reply_parent="hivebot"))
    bridge.handle_chat_message(said("@carol and tomorrow?", reply_parent="carol"))
    assert [m.data["utterances"][0] for m in hub.utterances()] == ["and tomorrow?",
                                                                    "and tomorrow?"]


def test_without_require_mention_everything_in_the_channel_is_forwarded():
    bridge, hub = make(require_mention=False)
    bridge.handle_chat_message(said("what time is it"))
    bridge.handle_chat_message(said("@hivebot and the date"))
    assert [m.data["utterances"][0] for m in hub.utterances()] == ["what time is it",
                                                                    "and the date"]


def test_the_bots_own_lines_and_other_channels_never_reach_the_hub():
    bridge, hub = make(require_mention=False)
    bridge.handle_chat_message(said("@hivebot hello", login="hivebot"))
    bridge.handle_chat_message(said("@hivebot hello", channel="elsewhere"))
    assert hub.utterances() == []


def test_two_bots_answering_each_others_replies_stop_after_a_bounded_chain(monkeypatch):
    """Another bridge answers the bot's reply with a Twitch reply that starts
    with "@hivebot": each answer is addressed to the other, without end."""
    import twitch_bridge as bridge_module

    clock = [1000.0]
    monkeypatch.setattr(bridge_module.time, "monotonic", lambda: clock[0])
    bridge, hub = make()
    for turn in range(bridge_module.LOOP_MAX_REPLIES + 5):
        bridge.handle_chat_message(said(f"@hivebot answer {turn}", login="otherbot",
                                        reply_parent="hivebot"))
        clock[0] += 2.0
    assert len(hub.utterances()) == bridge_module.LOOP_MAX_REPLIES
    # someone else, and a fresh question from the same account, still get answers
    bridge.handle_chat_message(said("@hivebot hi", login="carol", reply_parent="hivebot"))
    bridge.handle_chat_message(said("@hivebot a new question", login="otherbot"))
    assert len(hub.utterances()) == bridge_module.LOOP_MAX_REPLIES + 2
    # and once the window has passed, replies are answered again
    clock[0] += bridge_module.LOOP_WINDOW
    bridge.handle_chat_message(said("@hivebot later", login="otherbot", reply_parent="hivebot"))
    assert len(hub.utterances()) == bridge_module.LOOP_MAX_REPLIES + 3


def test_the_reply_chain_memory_is_bounded(monkeypatch):
    import twitch_bridge as bridge_module

    monkeypatch.setattr(bridge_module, "LOOP_MEMORY", 10)
    bridge, hub = make()
    for number in range(50):
        bridge.handle_chat_message(said("@hivebot hi", login=f"user{number}", reply_parent="hivebot"))
    assert len(hub.utterances()) == 50
    assert len(bridge._reply_chains) <= 10
