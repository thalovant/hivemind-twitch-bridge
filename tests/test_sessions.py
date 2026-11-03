"""Who shares a conversation on the hub."""
from fake_twitch import FakeHub
from twitch_bridge import HiveMindTwitchBridge
from twitch_bridge.chat import ChatMessage


def make(**options):
    hub = FakeHub()
    bridge = HiveMindTwitchBridge("hivebot", ["alpha", "beta"], oauth_token="t", client=hub,
                                  **options)
    bridge.connect_hivemind()
    return bridge, hub


def said(login, user_id, channel="alpha"):
    return ChatMessage(channel=channel, user_login=login, user_id=user_id, display_name=login,
                       text="@hivebot hi", message_id=None, reply_parent_login=None)


def sessions(hub):
    return [m.context["session"]["session_id"] for m in hub.utterances()]


def test_by_default_each_person_has_their_own_session_in_each_channel():
    bridge, hub = make()
    assert bridge.session_scope == "person"
    bridge.handle_chat_message(said("alice", "1001"))
    bridge.handle_chat_message(said("bob", "1002"))
    bridge.handle_chat_message(said("alice", "1001", channel="beta"))
    assert sessions(hub) == ["twitch-alpha-1001", "twitch-alpha-1002", "twitch-beta-1001"]


def test_the_session_follows_the_user_id_and_falls_back_to_the_login():
    bridge, hub = make()
    bridge.handle_chat_message(said("alice", None))
    assert sessions(hub) == ["twitch-alpha-alice"]


def test_chat_scope_shares_one_session_per_channel():
    bridge, hub = make(session_scope="chat")
    bridge.handle_chat_message(said("alice", "1001"))
    bridge.handle_chat_message(said("bob", "1002"))
    bridge.handle_chat_message(said("bob", "1002", channel="beta"))
    assert sessions(hub) == ["twitch-alpha", "twitch-alpha", "twitch-beta"]
