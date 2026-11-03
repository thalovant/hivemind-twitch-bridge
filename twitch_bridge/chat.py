"""Twitch chat over its WebSocket endpoint, kept connected and within limits.

:class:`TwitchChat` logs in to ``wss://irc-ws.chat.twitch.tv:443`` (the only
port the bridge needs: TCP 443), requests the IRCv3 ``twitch.tv/tags`` and
``twitch.tv/commands`` capabilities, joins exactly the configured channels
and then:

- answers the server's ``PING`` with ``PONG``, and sends its own ``PING``
  after a minute of silence so a dead connection is noticed;
- reconnects with exponential backoff and jitter when the connection drops,
  at once when Twitch sends ``RECONNECT``, and rejoins every channel;
- queues outgoing messages and sends them under Twitch's limits for an
  account that is not a moderator: fewer than 20 messages per 30 seconds for
  the whole account (Twitch counts this limit per user, so it also holds per
  channel) and at most one message per second in a channel, longer in slow
  mode; replies are split to Twitch's 500-character limit;
- raises :class:`~twitch_bridge.errors.TokenRejected` on ``Login
  authentication failed``: no retry can fix a refused token.

It never logs what anyone wrote, what the bridge answered, or the token.
"""
import asyncio
import logging
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Iterable, Optional, Set

from ovos_utils.log import LOG
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from twitch_bridge.errors import TokenRejected
from twitch_bridge.irc import (
    LOGIN_PATTERN,
    IrcMessage,
    chat_length,
    format_line,
    parse_line,
    split_message,
)

#: Twitch chat over TLS WebSocket.
TWITCH_CHAT_URL = "wss://irc-ws.chat.twitch.tv:443"
#: ``tags`` gives message ids, user ids and reply parents; ``commands`` gives
#: NOTICE ids, ROOMSTATE and RECONNECT (Twitch requires both for tags).
CAPABILITIES = "twitch.tv/tags twitch.tv/commands"
#: The NOTICE texts Twitch sends before it closes a refused login.
AUTH_FAILURES = ("Login authentication failed", "Improperly formatted auth")

#: Twitch: 20 messages per 30 seconds per user for an account that is not the
#: broadcaster, a moderator or a VIP. Two short of it for clock skew between
#: us and Twitch; going over gets the bot ignored for an hour.
MESSAGES_PER_WINDOW = 18
MESSAGE_WINDOW = 30.0
#: Twitch: at most one message per second per channel for the same accounts.
CHANNEL_GAP = 1.1
#: Twitch: 20 join attempts per 10 seconds per user.
JOINS_PER_WINDOW = 15
JOIN_WINDOW = 10.0
#: How often channels that did not confirm the join are joined again.
JOIN_RETRY = 60.0
#: Twitch's limit on channels joined at the same time.
MAX_CHANNELS = 100

#: Silence (seconds) after which the bridge sends its own PING.
KEEPALIVE = 60.0
#: How long the server has to answer that PING (any line counts).
PONG_TIMEOUT = 15.0
#: How long Twitch has to welcome the login (numeric 001).
LOGIN_TIMEOUT = 20.0
#: Reconnect backoff: base * 2**attempt, capped, times a random 0.5-1.0.
BACKOFF_BASE = 1.0
BACKOFF_CAP = 60.0
#: A session that stayed logged in this long resets the backoff.
STABLE_AFTER = 60.0
#: Outgoing messages waiting per channel; more are dropped.
MAX_PENDING = 50
#: An answer that waited this long (seconds) is no longer worth sending.
MAX_AGE = 120.0

# websockets logs every frame at debug level, and a frame holds the token
# (PASS) and what people wrote. Keep its logger above debug whatever the
# root logger's level is.
_QUIET_WEBSOCKET_LOG = logging.getLogger("twitch_bridge.websocket")
_QUIET_WEBSOCKET_LOG.setLevel(logging.WARNING)


def normalize_token(token: str) -> str:
    """``PASS`` wants ``oauth:<token>``; accept the token with or without the prefix.

    Raises ``TokenRejected`` for an empty token or one holding spaces or
    control characters: it cannot be valid, and a line break in it would end
    the ``PASS`` line early.
    """
    token = (token or "").strip()
    if token.lower().startswith("oauth:"):
        token = token[len("oauth:"):]
    if not token or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in token):
        raise TokenRejected("the Twitch token is empty or not well formed")
    return f"oauth:{token}"


@dataclass
class ChatMessage:
    """A chat message from a channel the bot is in."""

    channel: str
    user_login: str
    user_id: Optional[str]
    display_name: str
    text: str
    message_id: Optional[str]
    reply_parent_login: Optional[str]


@dataclass
class _Outgoing:
    channel: str
    text: str
    reply_to: Optional[str]
    queued_at: float


class SlidingWindow:
    """At most ``limit`` events in any ``window`` seconds."""

    def __init__(self, limit: int, window: float):
        self.limit = limit
        self.window = window
        self._times: Deque[float] = deque()

    def wait(self, now: float) -> float:
        """Seconds until one more event fits (0 if it fits now)."""
        while self._times and now - self._times[0] >= self.window:
            self._times.popleft()
        if len(self._times) < self.limit:
            return 0.0
        return self._times[0] + self.window - now

    def record(self, now: float) -> None:
        self._times.append(now)


class SendLimiter:
    """When the next message may go to a channel without breaking Twitch's limits."""

    def __init__(self, limit: int = MESSAGES_PER_WINDOW, window: float = MESSAGE_WINDOW,
                 channel_gap: float = CHANNEL_GAP):
        self.account = SlidingWindow(limit, window)
        self.channel_gap = channel_gap
        self.window = window
        self._last: Dict[str, float] = {}
        self._slow: Dict[str, float] = {}
        self._paused_until: Dict[str, float] = {}

    def wait(self, channel: str, now: float) -> float:
        """Seconds until a message to ``channel`` may be sent."""
        gap = max(self.channel_gap, self._slow.get(channel, 0.0))
        last = self._last.get(channel)
        channel_wait = 0.0 if last is None else last + gap - now
        paused = self._paused_until.get(channel, 0.0) - now
        return max(0.0, self.account.wait(now), channel_wait, paused)

    def record(self, channel: str, now: float) -> None:
        self.account.record(now)
        self._last[channel] = now

    def set_slow_mode(self, channel: str, seconds: float) -> None:
        """Slow mode: a non-moderator waits this long between messages."""
        self._slow[channel] = max(0.0, seconds)

    def pause(self, channel: str, seconds: float, now: float) -> None:
        """Send nothing to ``channel`` for ``seconds`` (Twitch said: too fast)."""
        self._paused_until[channel] = max(self._paused_until.get(channel, 0.0), now + seconds)


class _ConnectionLost(Exception):
    """The session ended for a reason a reconnect can fix."""


class TwitchChat:
    """One Twitch chat connection for one bot account and its channels."""

    def __init__(self, login: str, token_provider: Callable[[], str],
                 channels: Iterable[str], on_message: Callable[[ChatMessage], None],
                 url: str = TWITCH_CHAT_URL, *,
                 limiter: Optional[SendLimiter] = None,
                 join_limiter: Optional[SlidingWindow] = None,
                 join_retry: float = JOIN_RETRY,
                 keepalive: float = KEEPALIVE,
                 pong_timeout: float = PONG_TIMEOUT,
                 login_timeout: float = LOGIN_TIMEOUT,
                 backoff_base: float = BACKOFF_BASE,
                 backoff_cap: float = BACKOFF_CAP,
                 max_age: float = MAX_AGE):
        """
        Parameters
        ----------
        login: the bot account's login name (lowercase); it is the NICK.
        token_provider: returns the current user access token. It is called
            on every connection attempt, so a refreshed token (or a secret
            file the platform rotated) is used by the next login.
        channels: channel logins to join, exactly these.
        on_message: called on the event loop for every PRIVMSG in a joined
            channel that someone other than the bot sent.
        url: the chat endpoint (tests point it at a local server).
        """
        self.login = login.lower()
        self.channels = tuple(dict.fromkeys(channel.lower().lstrip("#") for channel in channels))
        if not self.channels:
            raise ValueError("at least one channel is required")
        if len(self.channels) > MAX_CHANNELS:
            raise ValueError(f"Twitch allows at most {MAX_CHANNELS} channels per connection")
        self._channel_set = frozenset(self.channels)
        self._token_provider = token_provider
        self._on_message = on_message
        self.url = url
        self.limiter = limiter or SendLimiter()
        self.join_limiter = join_limiter or SlidingWindow(JOINS_PER_WINDOW, JOIN_WINDOW)
        self.join_retry = join_retry
        self.keepalive = keepalive
        self.pong_timeout = pong_timeout
        self.login_timeout = login_timeout
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.max_age = max_age

        #: how many times Twitch welcomed a login (tests and the bridge read it)
        self.logins = 0
        self._welcomed = False
        self._welcomed_at = 0.0
        self._joined: Set[str] = set()
        self._ws = None
        self._pending: Dict[str, Deque[_Outgoing]] = {}
        self._planned_reconnect = False
        self._stop: Optional[asyncio.Event] = None
        self._wake: Optional[asyncio.Event] = None
        self._send_lock: Optional[asyncio.Lock] = None
        self._joiner: Optional[asyncio.Task] = None
        self._rejoin: Optional[asyncio.Event] = None
        self._reconnector: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------
    @property
    def joined(self) -> Set[str]:
        """Channels Twitch confirmed the bot is in."""
        return set(self._joined)

    def ready(self) -> bool:
        """Logged in and in every configured channel."""
        return self._welcomed and self._channel_set <= set(self._joined)

    def pending(self) -> int:
        """Messages waiting to be sent."""
        return sum(len(queue) for queue in self._pending.values())

    # ------------------------------------------------------------------
    # lifecycle (all of this runs on the event loop)
    # ------------------------------------------------------------------
    async def run(self) -> None:
        """Stay connected until :meth:`stop`. Raises ``TokenRejected``."""
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._rejoin = asyncio.Event()
        self._send_lock = asyncio.Lock()
        sender = asyncio.create_task(self._send_loop(), name="twitch-send")
        attempt = 0
        try:
            while not self._stop.is_set():
                self._planned_reconnect = False
                try:
                    await self._session()
                except TokenRejected:
                    raise
                except (OSError, ConnectionClosed, WebSocketException,
                        asyncio.TimeoutError, _ConnectionLost) as error:
                    if not self._stop.is_set() and not self._planned_reconnect:
                        LOG.warning(f"Twitch chat connection lost "
                                    f"({type(error).__name__}: {error}); reconnecting")
                finally:
                    stable = self._welcomed and time.monotonic() - self._welcomed_at >= STABLE_AFTER
                    self._end_session()
                if self._stop.is_set():
                    break
                if self._planned_reconnect:
                    attempt, delay = 0, 0.0
                else:
                    if stable:
                        attempt = 0
                    delay = self._backoff(attempt)
                    attempt += 1
                    LOG.info(f"reconnecting to Twitch chat in {delay:.1f} s")
                if delay:
                    try:
                        await asyncio.wait_for(self._stop.wait(), delay)
                    except asyncio.TimeoutError:
                        pass
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            self._end_session()

    def stop(self) -> None:
        """Disconnect and make :meth:`run` return. Call on the event loop."""
        if self._stop is not None:
            self._stop.set()
        self._close_socket()

    def request_reconnect(self, drain_timeout: float = 30.0) -> None:
        """Log in again (with a new token) once the outgoing queue is empty.

        Call on the event loop. Does nothing while not connected: the next
        login reads the token anyway.
        """
        if self._ws is None or (self._reconnector and not self._reconnector.done()):
            return
        self._reconnector = asyncio.create_task(self._reconnect_when_idle(drain_timeout))

    async def _reconnect_when_idle(self, drain_timeout: float) -> None:
        deadline = time.monotonic() + drain_timeout
        while self.pending() and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        LOG.info("logging in to Twitch chat again with the new token")
        self._planned_reconnect = True
        self._close_socket()

    def _close_socket(self) -> None:
        ws = self._ws
        if ws is not None:
            asyncio.ensure_future(ws.close())

    def _backoff(self, attempt: int) -> float:
        delay = min(self.backoff_cap, self.backoff_base * (2 ** min(attempt, 16)))
        return delay * random.uniform(0.5, 1.0)

    def _end_session(self) -> None:
        for task in (self._joiner, self._reconnector):
            if task is not None and not task.done():
                task.cancel()
        self._joiner = None
        self._reconnector = None
        self._welcomed = False
        self._joined.clear()
        self._ws = None

    async def _session(self) -> None:
        token = normalize_token(self._token_provider())
        async with websocket_connect(self.url, open_timeout=15, close_timeout=5,
                                     ping_interval=None, logger=_QUIET_WEBSOCKET_LOG) as ws:
            self._ws = ws
            if self._stop.is_set():
                return
            await self._send(f"CAP REQ :{CAPABILITIES}")
            await self._send(f"PASS {token}")
            await self._send(format_line("NICK", self.login, trailing=False))
            login_deadline = time.monotonic() + self.login_timeout
            awaiting_pong = False
            while True:
                if not self._welcomed:
                    timeout = max(0.0, login_deadline - time.monotonic())
                else:
                    timeout = self.pong_timeout if awaiting_pong else self.keepalive
                try:
                    frame = await asyncio.wait_for(ws.recv(), timeout)
                except asyncio.TimeoutError:
                    if not self._welcomed:
                        raise _ConnectionLost("Twitch did not answer the login") from None
                    if awaiting_pong:
                        raise _ConnectionLost("Twitch did not answer a PING") from None
                    await self._send("PING :tmi.twitch.tv")
                    awaiting_pong = True
                    continue
                awaiting_pong = False
                if isinstance(frame, bytes):
                    frame = frame.decode("utf-8", "replace")
                # one frame can carry several lines
                for line in frame.split("\n"):
                    message = parse_line(line)
                    if message is not None and await self._handle(message):
                        self._planned_reconnect = True
                        return

    async def _send(self, line: str) -> None:
        ws = self._ws
        if ws is None:
            raise _ConnectionLost("not connected")
        async with self._send_lock:
            await ws.send(line + "\r\n")

    # ------------------------------------------------------------------
    # what Twitch says
    # ------------------------------------------------------------------
    async def _handle(self, message: IrcMessage) -> bool:
        """Act on one line; True when Twitch asked for a reconnect."""
        command = message.command
        if command == "PING":
            await self._send(format_line("PONG", message.trailing or "tmi.twitch.tv"))
        elif command == "001":
            self._welcomed = True
            self._welcomed_at = time.monotonic()
            self.logins += 1
            LOG.info(f"== logged in to Twitch chat as {self.login}")
            self._joiner = asyncio.create_task(self._join_loop(), name="twitch-join")
            self._wake.set()
        elif command == "CAP":
            if len(message.params) >= 2 and message.params[1].upper() == "NAK":
                LOG.warning("Twitch refused the tags/commands capabilities: replies "
                            "and notices will be missing")
        elif command == "NOTICE":
            self._handle_notice(message)
        elif command == "JOIN" and message.nick == self.login:
            self._mark_joined(message.channel)
        elif command == "366" and len(message.params) >= 2:
            self._mark_joined(message.params[1].lstrip("#").lower())
        elif command == "ROOMSTATE":
            channel = message.channel
            self._mark_joined(channel)
            slow = message.tags.get("slow")
            if channel and slow is not None and slow.isdigit():
                self.limiter.set_slow_mode(channel, float(slow))
        elif command == "PART" and message.nick == self.login:
            channel = message.channel
            if channel in self._joined:
                self._joined.discard(channel)
                LOG.warning(f"left #{channel}; joining it again")
                self._rejoin.set()  # now, not after the next JOIN_RETRY
        elif command == "RECONNECT":
            LOG.info("Twitch asked for a reconnect")
            return True
        elif command == "PRIVMSG":
            self._deliver(message)
        elif command == "WHISPER":
            # Whispers are not answered: the bridge talks in its channels only.
            LOG.debug("ignoring a whisper")
        return False

    def _handle_notice(self, message: IrcMessage) -> None:
        text = message.trailing.strip()
        if message.channel is None and any(text.startswith(failure) for failure in AUTH_FAILURES):
            raise TokenRejected(text)
        msg_id = message.tags.get("msg-id") or "no id"
        channel = message.channel
        where = f" in #{channel}" if channel else ""
        # the notice id, never its text: some notices quote what was sent
        if msg_id == "msg_ratelimit" and channel:
            self.limiter.pause(channel, self.limiter.window, time.monotonic())
            LOG.warning(f"Twitch says the bot sends too fast{where}; pausing that channel")
        elif msg_id == "msg_slowmode" and channel:
            self.limiter.pause(channel, 5.0, time.monotonic())
            LOG.warning(f"Twitch notice {msg_id}{where}")
        else:
            LOG.warning(f"Twitch notice {msg_id}{where}")

    def _mark_joined(self, channel: Optional[str]) -> None:
        if channel in self._channel_set and channel not in self._joined:
            self._joined.add(channel)
            LOG.info(f"== joined #{channel}")
            self._wake.set()

    def _deliver(self, message: IrcMessage) -> None:
        channel = message.channel
        # configured AND joined: Twitch does not guarantee order, so a line can
        # arrive after the bot's own PART
        if channel not in self._channel_set or channel not in self._joined:
            return
        sender = message.nick or message.tags.get("login", "").lower()
        if not sender or sender == self.login:
            return
        text = message.trailing
        if text.startswith("\x01ACTION ") and text.endswith("\x01"):
            text = text[len("\x01ACTION "):-1]
        chat_message = ChatMessage(
            channel=channel,
            user_login=sender,
            user_id=message.tags.get("user-id") or None,
            display_name=message.tags.get("display-name") or sender,
            text=text,
            message_id=message.tags.get("id") or None,
            reply_parent_login=(message.tags.get("reply-parent-user-login") or "").lower() or None,
        )
        try:
            self._on_message(chat_message)
        except Exception:  # one bad message must not end the session
            LOG.exception("the chat message handler failed")

    async def _join_loop(self) -> None:
        try:
            await self._join_until_cancelled()
        except (ConnectionClosed, OSError, _ConnectionLost):
            return  # the session is ending; the next login joins again

    async def _join_until_cancelled(self) -> None:
        while True:
            missing = [channel for channel in self.channels if channel not in self._joined]
            for channel in missing:
                wait = self.join_limiter.wait(time.monotonic())
                if wait > 0:
                    await asyncio.sleep(wait)
                if channel in self._joined:
                    continue
                self.join_limiter.record(time.monotonic())
                await self._send(format_line("JOIN", f"#{channel}", trailing=False))
            self._rejoin.clear()
            try:
                await asyncio.wait_for(self._rejoin.wait(), self.join_retry)
            except asyncio.TimeoutError:
                pass
            still = [f"#{channel}" for channel in self.channels if channel not in self._joined]
            if still:
                LOG.warning(f"Twitch has not confirmed {', '.join(still)}; joining again")

    # ------------------------------------------------------------------
    # what the bridge says
    # ------------------------------------------------------------------
    def say(self, channel: str, text: str, reply_to: Optional[str] = None,
            mention: Optional[str] = None) -> int:
        """Queue ``text`` for ``channel``; returns how many messages it became.

        Call on the event loop. ``reply_to`` is the id of the message being
        answered (sent as ``reply-parent-msg-id``, so Twitch shows a reply);
        ``mention`` is the login to address, ``@login`` starting the first
        message.
        """
        channel = (channel or "").lower().lstrip("#")
        if channel not in self._channel_set:
            LOG.warning("not sending to a channel that is not configured")
            return 0
        # the hub echoes these back from the question's context; a skill can
        # hand back other types, which must not reach the wire as they are
        reply_to = str(reply_to) if reply_to not in (None, "") else None
        if not (isinstance(mention, str) and LOGIN_PATTERN.fullmatch(mention)):
            mention = None
        prefix = f"@{mention} " if mention else ""
        text = text if isinstance(text, str) else str(text)
        chunks = split_message(text, prefix=prefix)
        queue = self._pending.setdefault(channel, deque())
        now = time.monotonic()
        queued = 0
        for chunk in chunks:
            if len(queue) >= MAX_PENDING:
                LOG.warning(f"dropping {len(chunks) - queued} outgoing message(s) for "
                            f"#{channel}: {MAX_PENDING} already waiting")
                break
            queue.append(_Outgoing(channel, chunk, reply_to, now))
            queued += 1
        if queued and self._wake is not None:
            self._wake.set()
        return queued

    async def _send_loop(self) -> None:
        while True:
            self._wake.clear()
            try:
                delay = await self._send_next()
            except Exception as error:  # the sender must outlive any one message
                LOG.error(f"sending to Twitch chat failed ({type(error).__name__}); "
                          f"trying again in 1 s")
                delay = 1.0
            if delay == 0:
                continue
            try:
                await asyncio.wait_for(self._wake.wait(), delay)
            except asyncio.TimeoutError:
                pass

    async def _send_next(self) -> Optional[float]:
        """Send one message if one may go now: 0 if sent, else seconds to wait (None: until woken)."""
        now = time.monotonic()
        best = None
        for channel, queue in self._pending.items():
            while queue and now - queue[0].queued_at > self.max_age:
                queue.popleft()
                LOG.warning(f"dropped an answer for #{channel} that waited over "
                            f"{self.max_age:.0f} s")
            if not queue or not self._welcomed or channel not in self._joined:
                continue
            wait = self.limiter.wait(channel, now)
            if best is None or wait < best[0]:
                best = (wait, channel)
        if best is None:
            return None
        wait, channel = best
        if wait > 0:
            return wait
        item = self._pending[channel][0]
        tags = {"reply-parent-msg-id": item.reply_to} if item.reply_to else None
        try:
            line = format_line("PRIVMSG", f"#{channel}", item.text, tags=tags)
        except Exception:  # one unsendable answer must not hold up the queue
            self._pending[channel].popleft()
            LOG.warning(f"dropped an answer for #{channel} that cannot be sent")
            return 0
        try:
            await self._send(line)
        except (ConnectionClosed, OSError, _ConnectionLost):
            return None  # stays queued; the next login wakes the sender
        self._pending[channel].popleft()
        self.limiter.record(channel, time.monotonic())
        LOG.debug(f"sent {chat_length(item.text)} characters to #{channel}")
        return 0
