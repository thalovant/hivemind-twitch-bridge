"""IRC lines as Twitch sends them, and replies cut to Twitch's 500 characters."""
import pytest

from twitch_bridge.irc import (
    MAX_MESSAGE_LENGTH,
    chat_length,
    clean_text,
    escape_tag_value,
    format_line,
    parse_line,
    split_message,
    unescape_tag_value,
)


def test_a_tagged_privmsg_parses():
    line = ("@badge-info=;display-name=Alice;id=abc-123;reply-parent-user-login=hivebot;"
            "user-id=1001 :alice!alice@alice.tmi.twitch.tv PRIVMSG #mychannel :@hivebot what time is it")
    message = parse_line(line)
    assert message.command == "PRIVMSG"
    assert message.nick == "alice"
    assert message.channel == "mychannel"
    assert message.trailing == "@hivebot what time is it"
    assert message.tags["id"] == "abc-123"
    assert message.tags["reply-parent-user-login"] == "hivebot"
    assert message.tags["badge-info"] == ""


def test_server_lines_without_tags_parse():
    assert parse_line("PING :tmi.twitch.tv").trailing == "tmi.twitch.tv"
    welcome = parse_line(":tmi.twitch.tv 001 hivebot :Welcome, GLHF!")
    assert (welcome.command, welcome.params) == ("001", ["hivebot", "Welcome, GLHF!"])
    assert welcome.nick is None
    notice = parse_line(":tmi.twitch.tv NOTICE * :Login authentication failed")
    assert notice.channel is None and notice.trailing == "Login authentication failed"
    assert parse_line(":tmi.twitch.tv RECONNECT").command == "RECONNECT"
    assert parse_line("") is None
    assert parse_line("@a=b") is None


def test_tag_values_round_trip_through_escaping():
    value = "semi;colon space back\\slash\r\nend"
    assert escape_tag_value(value) == "semi\\:colon\\sspace\\sback\\\\slash\\r\\nend"
    assert unescape_tag_value(escape_tag_value(value)) == value
    assert unescape_tag_value("trailing\\") == "trailing"


def test_format_line_puts_the_text_last_and_escapes_tags():
    line = format_line("PRIVMSG", "#chan", "hello there", tags={"reply-parent-msg-id": "a b"})
    assert line == "@reply-parent-msg-id=a\\sb PRIVMSG #chan :hello there"


def test_a_line_break_cannot_smuggle_a_second_command():
    with pytest.raises(ValueError):
        format_line("PRIVMSG", "#chan", "hi\r\nPRIVMSG #other :spam")
    with pytest.raises(ValueError):
        format_line("NICK", "bot\nJOIN #x", trailing=False)
    with pytest.raises(ValueError):
        format_line("JOIN", "#a :b", trailing=False)


def test_one_word_commands_go_without_a_colon():
    assert format_line("NICK", "hivebot", trailing=False) == "NICK hivebot"
    assert format_line("JOIN", "#alpha", trailing=False) == "JOIN #alpha"
    assert format_line("PONG", "tmi.twitch.tv") == "PONG :tmi.twitch.tv"


def test_clean_text_turns_control_characters_into_spaces():
    assert clean_text("one\r\ntwo\tthree\x00four  five") == "one two three four five"


def test_a_short_answer_is_one_message_with_its_prefix():
    assert split_message("it is noon", prefix="@alice ") == ["@alice it is noon"]
    assert split_message("   ") == []


def test_a_long_answer_is_cut_between_words_under_500_characters():
    words = [f"word{index}" for index in range(400)]
    text = " ".join(words)
    chunks = split_message(text, prefix="@alice ")
    assert len(chunks) > 1
    assert all(chat_length(chunk) <= MAX_MESSAGE_LENGTH for chunk in chunks)
    assert chunks[0].startswith("@alice word0 ")
    assert not any(chunk.startswith("@alice") for chunk in chunks[1:])
    # nothing lost, nothing cut inside a word
    rejoined = " ".join(chunks)[len("@alice "):]
    assert rejoined.split(" ") == words


def test_a_word_longer_than_a_message_is_cut_by_characters():
    chunks = split_message("x" * 1200, limit=500)
    assert [len(chunk) for chunk in chunks] == [500, 500, 200]


def test_emoji_count_as_two_so_the_message_still_fits():
    text = "\U0001F600" * 300  # 300 code points, 600 UTF-16 units
    chunks = split_message(text)
    assert all(chat_length(chunk) <= MAX_MESSAGE_LENGTH for chunk in chunks)
    assert "".join(chunks) == text
    assert len(chunks) == 2


@pytest.mark.parametrize("key", ["reply\r\nPRIVMSG #x :spam", "two words", "a=b", "a;b", "", "\x00"])
def test_a_tag_key_cannot_smuggle_anything(key):
    with pytest.raises(ValueError):
        format_line("PRIVMSG", "#chan", "hi", tags={key: "v"})


def test_valid_tag_keys_and_a_non_text_value():
    assert format_line("PRIVMSG", "#c", "hi", tags={"+example.com/x-y": "1"}).startswith(
        "@+example.com/x-y=1 ")
    with pytest.raises(ValueError):
        format_line("PRIVMSG", "#c", "hi", tags={"reply-parent-msg-id": 123})


def test_a_repeated_prefix_starts_every_part_and_fits():
    from twitch_bridge.irc import split_message
    text = "word " * 300 + "x" * 1200
    parts = split_message(text, prefix="@alice ", repeat_prefix=True)
    assert len(parts) > 3
    assert all(part.startswith("@alice ") and chat_length(part) <= MAX_MESSAGE_LENGTH
               for part in parts)
    bodies = "".join(part[len("@alice "):] for part in parts)
    assert bodies.replace(" ", "") == text.replace(" ", "")


@pytest.mark.parametrize("repeat_prefix", [True, False])
def test_a_prefix_must_leave_room_for_a_two_unit_character(repeat_prefix):
    """A prefix of 499 units and an emoji (two UTF-16 units) cannot share a
    500-unit message: a prefix-only part, then a 501-unit one, came out."""
    from twitch_bridge.irc import split_message
    with pytest.raises(ValueError):
        split_message("\U0001F600" * 10, prefix="p" * 499, repeat_prefix=repeat_prefix)


def test_the_longest_allowed_prefix_still_fits_every_part():
    from twitch_bridge.irc import split_message
    prefix = "p" * 498
    parts = split_message("\U0001F600" * 10, prefix=prefix, repeat_prefix=True)
    assert len(parts) == 10
    assert all(part == prefix + "\U0001F600" and chat_length(part) == MAX_MESSAGE_LENGTH
               for part in parts)


@pytest.mark.parametrize("text,guarded", [
    ("/ban alice", True), (".timeout x 1", True), ("  /me hi", True),
    ("@alice /ban", False), ("hello /ban", False), ("", False), ("1.5 is a number", False),
])
def test_defuse_command_guards_only_a_leading_slash_or_dot(text, guarded):
    from twitch_bridge.irc import COMMAND_GUARD, defuse_command
    assert defuse_command(text) == (COMMAND_GUARD + text if guarded else text)


def test_a_tag_value_cannot_carry_a_nul():
    from twitch_bridge.irc import format_line
    with pytest.raises(ValueError):
        format_line("PRIVMSG", "#alpha", "hi", tags={"reply-parent-msg-id": "a\x00b"})
