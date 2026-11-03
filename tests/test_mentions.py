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
