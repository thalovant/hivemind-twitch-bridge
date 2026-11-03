"""HiveMind <-> Twitch chat bridge.

A HiveMind bridge is a satellite whose input and output are a chat platform
instead of a microphone. This one connects to a HiveMind hub with
:class:`~hivemind_bus_client.HiveMessageBusClient` and to Twitch chat with
:class:`~twitch_bridge.chat.TwitchChat` (``wss://irc-ws.chat.twitch.tv:443``).
A chat message in an allowed channel that mentions the bot (``@botlogin``) or
replies to it becomes a ``recognizer_loop:utterance``, mention removed; the
hub's ``speak`` goes back to the same channel as a reply to that message,
addressed to the person who asked. Whispers are never answered.

Connection lifecycle:

- ``HiveMessageBusClient.connect()`` starts and owns the hub's reconnect
  worker thread and blocks until the handshake completes (or fails). It is
  called exactly once, from :meth:`HiveMindTwitchBridge.run`, with a bounded
  ``handshake_max_retries``, before anything touches Twitch: a hub that is
  down stops the process before it spends a Twitch token refresh.
- Twitch chat runs on an asyncio loop on the main thread. The hub's ``speak``
  arrives on the bus thread and is handed to that loop with
  ``call_soon_threadsafe``; it never blocks the bus thread.
- A Twitch credential Twitch refuses ends the process: ``token_rejected`` is
  written to ``/dev/termination-log`` and the exit status is 3.
- a freshly registered HiveMind client is denied every message type until a
  hub admin runs ``hivemind-core allow-msg recognizer_loop:utterance
  <client_id>`` (and usually ``speak`` too); this bridge cannot do that step
  itself.
"""
import asyncio
import re
import signal
import threading
import time
from collections import OrderedDict, deque
from typing import Deque, Iterable, Optional, Tuple

from hivemind_bus_client import (
    HiveMessage,
    HiveMessageBusClient,
    HiveMessageType,
)
from ovos_bus_client.message import Message
from ovos_utils.log import LOG

from twitch_bridge.auth import TWITCH_ID_URL, TwitchAuth
from twitch_bridge.chat import MAX_CHANNELS, TWITCH_CHAT_URL, ChatMessage, TwitchChat
from twitch_bridge.errors import ClientRejected, TokenRejected
from twitch_bridge.health import HealthServer
from twitch_bridge.irc import LOGIN_PATTERN, chat_length

platform = "HiveMindTwitchBridgeV0.1"

# hivemind-bus-client's own default (None) retries the handshake forever.
# Bound it so a stalled or unreachable hub (down, wrong password) fails fast.
DEFAULT_HANDSHAKE_MAX_RETRIES = 10

#: ``person``: one hub session per person in each channel. ``chat``: one per
#: channel, shared by everyone in it.
SESSION_SCOPES = ("person", "chat")

#: Replies to the bot's own messages answered, per account and channel, within
#: ``LOOP_WINDOW`` seconds; more are not answered until the window has passed.
#: Another bot (another HiveMind bridge, say) answers the bot's reply with a
#: Twitch reply addressed to it, and the two would answer each other forever.
LOOP_MAX_REPLIES = 8
LOOP_WINDOW = 600.0
#: Accounts whose reply chains are remembered (least recently seen dropped).
LOOP_MEMORY = 1024

#: Where Kubernetes reads a container's termination message.
TERMINATION_LOG = "/dev/termination-log"
#: Exit statuses.
EXIT_OK = 0
EXIT_HUB_FAILED = 1
EXIT_TOKEN_REJECTED = 3
EXIT_CLIENT_REJECTED = 4


class HiveMindTwitchBridge:
    """Bridge one Twitch bot account, in a fixed set of channels, to a HiveMind hub."""

    def __init__(self,
                 bot_login: str,
                 channels: Iterable[str],
                 oauth_token: Optional[str] = None,
                 oauth_token_file: Optional[str] = None,
                 client_id: Optional[str] = None,
                 client_secret: Optional[str] = None,
                 refresh_token: Optional[str] = None,
                 key: Optional[str] = None,
                 password: Optional[str] = None,
                 host: Optional[str] = None,
                 port: int = 5678,
                 self_signed: bool = False,
                 lang: str = "en-us",
                 site_id: str = "twitch",
                 require_mention: bool = True,
                 session_scope: str = "person",
                 health_port: Optional[int] = None,
                 handshake_max_retries: int = DEFAULT_HANDSHAKE_MAX_RETRIES,
                 *,
                 client: Optional[HiveMessageBusClient] = None,
                 chat_url: str = TWITCH_CHAT_URL,
                 auth_url: str = TWITCH_ID_URL,
                 termination_log: str = TERMINATION_LOG,
                 chat_options: Optional[dict] = None,
                 auth_options: Optional[dict] = None):
        """
        Parameters
        ----------
        bot_login: the bot account's Twitch login (its NICK).
        channels: channel logins to join and answer in; exactly these.
        oauth_token, oauth_token_file: a user access token (``chat:read`` and
            ``chat:edit``), with or without ``oauth:``; for short tests.
        client_id, client_secret, refresh_token: refresh the access token
            (wins over ``oauth_token``); see :mod:`twitch_bridge.auth`.
        key, password, host, port, self_signed: HiveMind hub connection.
        lang: utterance language tag. site_id: this bridge's HiveMind site id.
        require_mention: forward only messages that mention the bot or
            reply to it (Twitch channels are always shared rooms).
        session_scope: ``"person"`` or ``"chat"`` (see ``SESSION_SCOPES``).
        health_port: serve ``/healthz`` and ``/readyz`` on this port.
        handshake_max_retries: bound on the hub handshake retries.
        client: pre-built HiveMessageBusClient (tests). Not connected here.
        chat_url, auth_url, chat_options, auth_options: Twitch endpoints and
            client tuning (tests point them at local servers).
        termination_log: where ``token_rejected`` is written.
        """
        self.bot_login = (bot_login or "").strip().lower()
        if not LOGIN_PATTERN.fullmatch(self.bot_login):
            raise ValueError("bot_login must be a Twitch login (letters, digits, underscore)")
        self.channels = tuple(dict.fromkeys(_channel_login(channel) for channel in channels))
        if not self.channels:
            raise ValueError("at least one channel is required")
        if len(self.channels) > MAX_CHANNELS:
            raise ValueError(f"Twitch allows at most {MAX_CHANNELS} channels")
        if session_scope not in SESSION_SCOPES:
            raise ValueError(f"session_scope must be one of {SESSION_SCOPES}, "
                             f"not {session_scope!r}")
        self.session_scope = session_scope
        self.require_mention = require_mention
        self.lang = lang
        self.site_id = site_id
        self.handshake_max_retries = handshake_max_retries
        self.termination_log = termination_log
        # "@botlogin", and the comma or colon that often follows it
        self._mention = re.compile(rf"(?<![\w@])@{re.escape(self.bot_login)}\b[,:]?",
                                   re.IGNORECASE)

        self.auth = TwitchAuth(self.bot_login, access_token=oauth_token,
                               access_token_file=oauth_token_file, client_id=client_id,
                               client_secret=client_secret, refresh_token=refresh_token,
                               base_url=auth_url, on_new_token=self._token_renewed,
                               **(auth_options or {}))
        self.chat = TwitchChat(self.bot_login, self.auth.token, self.channels,
                               self.handle_chat_message, url=chat_url, **(chat_options or {}))

        self.client = client or HiveMessageBusClient(
            key=key, password=password, host=host, port=port,
            useragent=platform, self_signed=self_signed)

        self.health_port = health_port
        self._health: Optional[HealthServer] = None
        self._connected = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stopping: Optional[asyncio.Event] = None
        self._exit_code = EXIT_OK
        # a stop asked for (from another thread) before the loop exists, e.g.
        # the hub refusing the bridge right after the handshake
        self._loop_lock = threading.Lock()
        self._pending_stop: Optional[int] = None
        # (channel, login) -> when the bot answered that account's replies to it
        # (event loop only)
        self._reply_chains: "OrderedDict[Tuple[str, str], Deque[float]]" = OrderedDict()
        self._loop_warned: set = set()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def connect_hivemind(self) -> None:
        """Connect to the hub once and wait for the handshake (bounded)."""
        self.client.connect(site_id=self.site_id,
                            handshake_max_retries=self.handshake_max_retries)
        self.client.on_mycroft("speak", self.handle_speak)
        self.client.on_mycroft("hive.complete_intent_failure", self.handle_intent_failure)
        emitter = getattr(self.client, "emitter", None)
        if emitter is not None and hasattr(emitter, "on"):
            # the client stops reconnecting once the hub refuses the identity
            emitter.on("auth_rejected", self._hub_refused)
        self._connected.set()
        LOG.info("== connected to HiveMind")

    def run(self) -> int:
        """Connect to the hub, then run Twitch chat until stopped. Returns the exit status."""
        if self.health_port is not None:
            self._health = HealthServer(self.health_port, self.ready)
            self._health.start()
        try:
            try:
                self.connect_hivemind()
            except (ConnectionRefusedError, RuntimeError, OSError) as error:
                LOG.error(f"cannot use the HiveMind hub: {error}")
                return EXIT_HUB_FAILED
            LOG.warning("a freshly registered HiveMind client is denied every message type "
                        "until an admin runs `hivemind-core allow-msg "
                        "recognizer_loop:utterance <client_id>` on the hub (and usually "
                        "`speak` too). If messages seem to vanish silently, check that first.")
            return asyncio.run(self._run())
        finally:
            self._shutdown()

    async def _run(self) -> int:
        self._stopping = asyncio.Event()
        with self._loop_lock:
            self._loop = asyncio.get_running_loop()
            pending, self._pending_stop = self._pending_stop, None
        if pending is not None:
            self._request_stop(pending)
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                self._loop.add_signal_handler(signum, self._request_stop, EXIT_OK)
            except (NotImplementedError, RuntimeError, ValueError):
                pass  # not the main thread (tests), or no signals on this platform
        try:
            # a token first (a refresh can retry for a while), then chat and the
            # token's upkeep side by side; a stop request ends either stage
            if not self._stopping.is_set():
                await self._until_stopped(self.auth.start())
            if not self._stopping.is_set():
                await self._until_stopped(self._run_chat(), self.auth.run())
            return self._exit_code
        except TokenRejected as error:
            LOG.error(f"Twitch refused the bot's credentials: {error}. Not retrying.")
            self._write_termination_message("token_rejected")
            return EXIT_TOKEN_REJECTED
        except ClientRejected as error:
            LOG.error(f"{error}. Not retrying.")
            return EXIT_CLIENT_REJECTED
        finally:
            self.chat.stop()

    async def _until_stopped(self, *coroutines) -> None:
        """Run ``coroutines`` until one ends or a stop is requested; re-raise its error."""
        tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
        stopper = asyncio.create_task(self._stopping.wait())
        done, pending = await asyncio.wait([*tasks, stopper],
                                           return_when=asyncio.FIRST_COMPLETED)
        self.chat.stop()
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if task is not stopper:
                task.result()

    async def _run_chat(self) -> None:
        """Run chat; when Twitch refuses the login, refresh once before giving up."""
        refreshed_for_login = None
        while True:
            try:
                await self.chat.run()
                return
            except TokenRejected:
                if not self.auth.can_refresh or refreshed_for_login == self.chat.logins:
                    raise
                refreshed_for_login = self.chat.logins
                LOG.warning("Twitch chat refused the access token; refreshing it once")
                await self.auth.refresh()

    def stop(self) -> None:
        """Stop from any thread: leave chat, then close the hub connection."""
        self._request_stop_threadsafe(EXIT_OK)

    def _request_stop_threadsafe(self, code: int) -> None:
        with self._loop_lock:
            loop = self._loop
            if loop is None:
                self._pending_stop = code  # _run acts on it once the loop exists
                return
        if not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._request_stop, code)
            except RuntimeError:
                pass

    def _request_stop(self, code: int) -> None:
        if self._exit_code == EXIT_OK:
            self._exit_code = code
        if self._stopping is not None:
            self._stopping.set()

    def _hub_refused(self, *args) -> None:
        LOG.error("the HiveMind hub refused this bridge's credentials; stopping")
        self._request_stop_threadsafe(EXIT_HUB_FAILED)

    def _token_renewed(self) -> None:
        """The access token changed: log in to chat again with it (on the loop)."""
        self.chat.request_reconnect()

    def _shutdown(self) -> None:
        try:
            self.client.close()
        except Exception:
            LOG.exception("error closing the HiveMind client")
        self._connected.clear()
        if self._health is not None:
            self._health.stop()
            self._health = None

    def _write_termination_message(self, reason: str) -> None:
        try:
            with open(self.termination_log, "w", encoding="utf-8") as handle:
                handle.write(reason)
        except OSError as error:
            LOG.warning(f"cannot write {self.termination_log}: {error.strerror}")

    def hub_live(self) -> bool:
        """The HiveMind session is up now (the client clears its handshake on a drop)."""
        handshake = getattr(self.client, "handshake_event", None)
        return self._connected.is_set() and (handshake is None or handshake.is_set())

    def ready(self) -> bool:
        """A message can go end to end: hub session live AND every channel joined."""
        return bool(self.hub_live() and self.chat.ready())

    # ------------------------------------------------------------------
    # Twitch -> HiveMind (on the event loop)
    # ------------------------------------------------------------------
    def handle_chat_message(self, message: ChatMessage) -> None:
        """Forward a chat message addressed to the bot to the hub."""
        if message.channel not in self.channels or message.user_login == self.bot_login:
            return
        text = self.addressed_text(message)
        if text is None:
            return
        replies_to_bot = message.reply_parent_login == self.bot_login
        if replies_to_bot and not self._chain_allows(message):
            return
        if not self.hub_live():
            LOG.warning("dropping a Twitch message: the HiveMind session is down")
            return
        if self.forward_to_hivemind(text, message) and replies_to_bot:
            self._record_reply(message)

    def _chain_allows(self, message: ChatMessage) -> bool:
        """Whether one more reply to the bot from this account may be answered.

        At most ``LOOP_MAX_REPLIES`` in ``LOOP_WINDOW`` seconds per account and
        channel: what two bots answering each other's replies look like, and
        far more than a person asks in a row. A new question (not a reply) is
        not counted, and other accounts are not affected. Only a reply that
        reached the hub counts (:meth:`_record_reply`).
        """
        key = (message.channel, message.user_login)
        now = time.monotonic()
        answered = self._reply_chains.pop(key, None) or deque()
        self._reply_chains[key] = answered  # now the most recently seen
        while answered and now - answered[0] >= LOOP_WINDOW:
            answered.popleft()
        while len(self._reply_chains) > LOOP_MEMORY:
            dropped, _ = self._reply_chains.popitem(last=False)
            self._loop_warned.discard(dropped)
        if len(answered) >= LOOP_MAX_REPLIES:
            if key not in self._loop_warned:
                self._loop_warned.add(key)
                LOG.warning(f"not answering more replies from {message.user_login} in "
                            f"#{message.channel}: {len(answered)} within {LOOP_WINDOW:.0f} s "
                            "look like two bots answering each other")
            return False
        self._loop_warned.discard(key)
        return True

    def _record_reply(self, message: ChatMessage) -> None:
        """Count a reply to the bot once it reached the hub.

        A reply dropped because the hub is down, or one ``emit`` refused, is
        not counted: it was never answered.
        """
        key = (message.channel, message.user_login)
        answered = self._reply_chains.pop(key, None) or deque()
        answered.append(time.monotonic())
        self._reply_chains[key] = answered
        while len(self._reply_chains) > LOOP_MEMORY:
            dropped, _ = self._reply_chains.popitem(last=False)
            self._loop_warned.discard(dropped)

    def addressed_text(self, message: ChatMessage) -> Optional[str]:
        """The text to forward, mention removed; None when it is not for the bot.

        With ``require_mention`` a message must mention ``@botlogin`` or reply
        to one of the bot's messages. Twitch starts a reply with ``@<parent>``,
        so a reply to the bot carries the mention too, and it is removed the
        same way.
        """
        text, count = self._mention.subn(" ", message.text)
        text = " ".join(text.split())
        if not text:
            return None
        replied = message.reply_parent_login == self.bot_login
        if self.require_mention and not (count or replied):
            return None
        return text

    def session_id(self, message: ChatMessage) -> str:
        """The hub session a message belongs to: ``twitch-<channel>[-<user id>]``."""
        if self.session_scope == "person":
            return f"twitch-{message.channel}-{message.user_id or message.user_login}"
        return f"twitch-{message.channel}"

    def forward_to_hivemind(self, text: str, message: ChatMessage) -> bool:
        """Emit ``text`` as an utterance; whether the hub client took it."""
        utterance = Message(
            "recognizer_loop:utterance",
            {"utterances": [text], "lang": self.lang},
            {
                "source": platform,
                "destination": "HiveMind",
                "platform": platform,
                "twitch_channel": message.channel,
                "twitch_message_id": message.message_id,
                "user": {"twitch_username": message.user_login,
                         "twitch_user_id": message.user_id},
                "session": {"session_id": self.session_id(message)},
            },
        )
        try:
            self.client.emit(HiveMessage(HiveMessageType.BUS, utterance))
        except Exception as error:
            LOG.warning(f"could not forward a Twitch message to the hub "
                        f"({type(error).__name__})")
            return False
        return True

    # ------------------------------------------------------------------
    # HiveMind -> Twitch (on the bus thread)
    # ------------------------------------------------------------------
    def handle_speak(self, message: Message) -> None:
        utterance = message.data.get("utterance")
        if utterance:
            self._reply(message, utterance)

    def handle_intent_failure(self, message: Message) -> None:
        if message.context.get("twitch_channel") in self.channels:
            LOG.error("complete intent failure")
            self._reply(message, "I don't know how to answer that")

    def _reply(self, message: Message, text: str) -> None:
        channel = message.context.get("twitch_channel")
        if channel not in self.channels:
            return
        user = message.context.get("user") or {}
        login = user.get("twitch_username") if isinstance(user, dict) else None
        self.speak(text, channel, reply_to=message.context.get("twitch_message_id"),
                   mention=login)

    def speak(self, text: str, channel: str, reply_to: Optional[str] = None,
              mention: Optional[str] = None) -> None:
        """Queue ``text`` for ``channel`` from any thread; never blocks the caller."""
        loop = self._loop
        if loop is None or loop.is_closed():
            LOG.warning("Twitch chat is not running; dropping a reply")
            return
        # the length, never the text
        LOG.debug(f"reply for #{channel}: {chat_length(text)} characters")
        try:
            loop.call_soon_threadsafe(self._say, channel, text, reply_to, mention)
        except RuntimeError:
            LOG.warning("Twitch chat stopped; dropping a reply")

    def _say(self, channel: str, text: str, reply_to: Optional[str],
             mention: Optional[str]) -> None:
        """``chat.say`` on the loop. asyncio logs a callback that raises together
        with its arguments, the answer included: log the type only."""
        try:
            self.chat.say(channel, text, reply_to, mention)
        except Exception as error:
            LOG.error(f"could not queue a reply for #{channel} ({type(error).__name__})")


def _channel_login(channel: str) -> str:
    login = (channel or "").strip().lstrip("#").lower()
    if not LOGIN_PATTERN.fullmatch(login):
        raise ValueError(f"not a Twitch channel login: {channel!r}")
    return login


__all__ = ["HiveMindTwitchBridge", "platform", "DEFAULT_HANDSHAKE_MAX_RETRIES",
           "LOOP_MAX_REPLIES", "LOOP_WINDOW",
           "SESSION_SCOPES", "EXIT_OK", "EXIT_HUB_FAILED", "EXIT_TOKEN_REJECTED",
           "EXIT_CLIENT_REJECTED"]
