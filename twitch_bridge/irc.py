"""IRC lines as Twitch chat sends and accepts them (IRCv3 message tags).

Twitch chat is IRC carried over a WebSocket. A line is::

    [@tag=value;tag2=value2 ][:prefix ]COMMAND [param ...][ :trailing]

Tag values escape ``;``, space, backslash, CR and LF (IRCv3 message-tags).
Nothing in this module does I/O; :mod:`twitch_bridge.chat` owns the socket.
"""
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

#: Twitch drops a chat message longer than this many characters.
MAX_MESSAGE_LENGTH = 500
#: A Twitch login (user or channel name): lowercase letters, digits, underscore.
LOGIN_PATTERN = re.compile(r"[a-z0-9_]{1,25}")
#: An IRCv3 message-tag key: optional client prefix "+", optional vendor, name.
_TAG_KEY = re.compile(r"\+?(?:[A-Za-z0-9.-]+/)?[A-Za-z0-9-]+")

_TAG_UNESCAPES = {":": ";", "s": " ", "\\": "\\", "r": "\r", "n": "\n"}
_TAG_ESCAPES = {";": "\\:", " ": "\\s", "\\": "\\\\", "\r": "\\r", "\n": "\\n"}


@dataclass
class IrcMessage:
    """One parsed IRC line."""

    command: str
    params: List[str] = field(default_factory=list)
    tags: Dict[str, str] = field(default_factory=dict)
    prefix: Optional[str] = None

    @property
    def nick(self) -> Optional[str]:
        """The sender's nick from ``nick!user@host``, lowercased; None for the server."""
        if not self.prefix or "!" not in self.prefix:
            return None
        return self.prefix.split("!", 1)[0].lower()

    @property
    def trailing(self) -> str:
        """The last parameter (the message text of a PRIVMSG or NOTICE)."""
        return self.params[-1] if self.params else ""

    @property
    def channel(self) -> Optional[str]:
        """The channel login a channel command is about (``#name`` -> ``name``)."""
        for param in self.params[:1]:
            if param.startswith("#"):
                return param[1:].lower()
        return None


def unescape_tag_value(value: str) -> str:
    """Undo IRCv3 tag-value escaping (``\\s`` -> space, ``\\:`` -> ``;``...)."""
    out = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\":
            index += 1
            if index < len(value):
                out.append(_TAG_UNESCAPES.get(value[index], value[index]))
            # a lone trailing backslash is dropped, as the spec says
        else:
            out.append(char)
        index += 1
    return "".join(out)


def escape_tag_value(value: str) -> str:
    """IRCv3 tag-value escaping, the inverse of :func:`unescape_tag_value`."""
    return "".join(_TAG_ESCAPES.get(char, char) for char in value)


def parse_line(line: str) -> Optional[IrcMessage]:
    """Parse one IRC line (without its CRLF); None for an empty or malformed one."""
    line = line.rstrip("\r\n")
    if not line:
        return None
    tags: Dict[str, str] = {}
    prefix = None
    rest = line
    if rest.startswith("@"):
        raw_tags, _, rest = rest[1:].partition(" ")
        for item in raw_tags.split(";"):
            if not item:
                continue
            key, _, value = item.partition("=")
            tags[key] = unescape_tag_value(value)
        rest = rest.lstrip(" ")
    if rest.startswith(":"):
        prefix, _, rest = rest[1:].partition(" ")
        rest = rest.lstrip(" ")
    if not rest:
        return None
    if " :" in rest:
        head, _, trailing = rest.partition(" :")
        words = head.split()
        params = words[1:] + [trailing]
    elif rest.startswith(":"):
        return None
    else:
        words = rest.split()
        params = words[1:]
    command = words[0].upper() if words else ""
    if not command:
        return None
    return IrcMessage(command=command, params=params, tags=tags, prefix=prefix)


def format_line(command: str, *params: str, tags: Optional[Dict[str, str]] = None,
                trailing: bool = True) -> str:
    """Build one IRC line (without CRLF).

    With ``trailing`` the last parameter is sent after ``:`` (message text,
    which holds spaces); without it every parameter must be one word
    (``NICK login``, ``JOIN #channel``, as Twitch documents them).

    Raises ``ValueError`` if any part holds CR, LF or NUL: a line break inside
    a parameter would let the text that follows it run as a second command.
    A middle parameter holding a space, or starting with ``:``, is refused
    for the same reason.
    """
    parts = []
    if tags:
        for key, value in tags.items():
            if not isinstance(key, str) or not _TAG_KEY.fullmatch(key):
                raise ValueError("an IRC tag key is letters, digits and hyphens "
                                 "(with an optional vendor/ and +)")
            if not isinstance(value, str):
                raise ValueError("an IRC tag value must be text")
        parts.append("@" + ";".join(f"{key}={escape_tag_value(value)}"
                                    for key, value in tags.items()))
    for piece in (command, *params):
        if any(char in piece for char in "\r\n\0"):
            raise ValueError("IRC parameters cannot contain CR, LF or NUL")
    words = params[:-1] if trailing else params
    for word in (command, *words):
        if not word or " " in word or word.startswith(":"):
            raise ValueError("only the trailing IRC parameter can hold spaces")
    parts.append(command)
    if params:
        parts.extend(params[:-1])
        parts.append(":" + params[-1] if trailing else params[-1])
    return " ".join(parts)


def chat_length(text: str) -> int:
    """Length as Twitch's 500-character limit is safest counted: UTF-16 units.

    An emoji outside the Basic Multilingual Plane counts as two, so a message
    that fits by this count also fits by a code-point count.
    """
    return len(text.encode("utf-16-le")) // 2


def clean_text(text: str) -> str:
    """One line of chat text: control characters become spaces, runs of space collapse."""
    text = "".join(" " if (ord(char) < 32 or ord(char) == 127) else char for char in text)
    return " ".join(text.split())


def split_message(text: str, limit: int = MAX_MESSAGE_LENGTH, prefix: str = "") -> List[str]:
    """Split ``text`` into chat messages of at most ``limit`` characters each.

    Breaks between words where it can and inside a word only when one word is
    longer than a whole message. ``prefix`` (``"@alice "``) starts the first
    message and counts toward its length.
    """
    text = clean_text(text)
    if not text:
        return []
    chunks: List[str] = []
    current = prefix
    fresh = True  # ``current`` holds no word yet, only the prefix (if any)
    for word in text.split(" "):
        separator = "" if fresh else " "
        if chat_length(current + separator + word) <= limit:
            current += separator + word
            fresh = False
            continue
        if not fresh:
            chunks.append(current)
            current, fresh = "", True
            if chat_length(word) <= limit:
                current, fresh = word, False
                continue
        # a word longer than the room left in an empty message: cut it
        for char in word:
            if chat_length(current + char) > limit:
                chunks.append(current)
                current = ""
            current += char
        fresh = False
    if not fresh:
        chunks.append(current)
    return chunks
