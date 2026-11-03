"""A local stand-in for ``wss://irc-ws.chat.twitch.tv``: a real WebSocket server.

It speaks the part of Twitch chat the bridge relies on: CAP ACK, PASS/NICK
login (welcome or ``Login authentication failed``), JOIN echo with
353/366/ROOMSTATE, PING/PONG both ways, RECONNECT, tagged PRIVMSG and
WHISPER, and it records every line the client sent with the time it arrived.
Several lines go out in one frame, as Twitch sends them.
"""
import asyncio
import json
import time
from typing import Dict, List, Optional, Tuple

from websockets.asyncio.server import serve

from twitch_bridge.irc import escape_tag_value, parse_line


class FakeTwitch:
    def __init__(self, token: str = "good-token", *, silent_channels=(),
                 answer_ping: bool = True, welcome: bool = True):
        self.token = token
        self.silent_channels = set(silent_channels)
        self.answer_ping = answer_ping
        self.welcome = welcome
        #: every line any client sent, in order: (connection number, line)
        self.received: List[Tuple[int, str]] = []
        #: PRIVMSG lines with their arrival time (time.monotonic())
        self.privmsgs: List[Tuple[float, str]] = []
        self.connections = 0
        self.url: Optional[str] = None
        self._current = None
        self._nick: Dict[int, str] = {}
        self._server = None

    async def __aenter__(self):
        self._server = await serve(self._handler, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    # ------------------------------------------------------------------
    def lines(self, connection: Optional[int] = None) -> List[str]:
        return [line for number, line in self.received
                if connection is None or number == connection]

    async def wait_for(self, predicate, timeout: float = 5.0, what: str = "condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"timed out waiting for {what}")

    async def push(self, *lines: str) -> None:
        """Send lines to the current client, in one frame."""
        await self._current.send("".join(f"{line}\r\n" for line in lines))

    async def chat(self, channel: str, login: str, text: str, msg_id: str = "m-1",
                   user_id: str = "1001", **extra_tags: str) -> None:
        """A tagged PRIVMSG from ``login`` in ``#channel``."""
        tags = {"badge-info": "", "badges": "", "display-name": login.capitalize(),
                "id": msg_id, "mod": "0", "room-id": "42", "user-id": user_id,
                "tmi-sent-ts": "1700000000000", "user-type": ""}
        tags.update({key.replace("_", "-"): value for key, value in extra_tags.items()})
        raw_tags = ";".join(f"{key}={escape_tag_value(value)}" for key, value in tags.items())
        await self.push(f"@{raw_tags} :{login}!{login}@{login}.tmi.twitch.tv "
                        f"PRIVMSG #{channel} :{text}")

    async def drop(self) -> None:
        """Close the current connection, as a network failure would."""
        await self._current.close(code=1011, reason="gone")

    # ------------------------------------------------------------------
    async def _handler(self, websocket):
        self.connections += 1
        number = self.connections
        self._current = websocket
        password = None
        try:
            async for frame in websocket:
                for line in frame.split("\r\n"):
                    if not line:
                        continue
                    self.received.append((number, line))
                    message = parse_line(line)
                    if message is None:
                        continue
                    command = message.command
                    if command == "CAP" and message.params[:1] == ["REQ"]:
                        await websocket.send(f":tmi.twitch.tv CAP * ACK :{message.trailing}\r\n")
                    elif command == "PASS":
                        password = message.params[0] if message.params else ""
                    elif command == "NICK":
                        nick = message.params[0]
                        self._nick[number] = nick
                        if password != f"oauth:{self.token}":
                            await websocket.send(
                                ":tmi.twitch.tv NOTICE * :Login authentication failed\r\n")
                            await websocket.close()
                            return
                        if self.welcome:
                            await websocket.send(
                                f":tmi.twitch.tv 001 {nick} :Welcome, GLHF!\r\n"
                                f":tmi.twitch.tv 002 {nick} :Your host is tmi.twitch.tv\r\n"
                                f":tmi.twitch.tv 376 {nick} :>\r\n"
                                f"@badge-info=;badges=;color=;display-name={nick};"
                                f"user-id=999;user-type= :tmi.twitch.tv GLOBALUSERSTATE\r\n")
                    elif command == "JOIN":
                        nick = self._nick.get(number, "unknown")
                        for target in message.params[0].split(","):
                            channel = target.lstrip("#")
                            if channel in self.silent_channels:
                                continue  # Twitch drops a JOIN to a channel that does not exist
                            await websocket.send(
                                f":{nick}!{nick}@{nick}.tmi.twitch.tv JOIN #{channel}\r\n"
                                f":{nick}.tmi.twitch.tv 353 {nick} = #{channel} :{nick}\r\n"
                                f":{nick}.tmi.twitch.tv 366 {nick} #{channel} :End of /NAMES list\r\n"
                                f"@badge-info=;badges=;mod=0 :tmi.twitch.tv USERSTATE #{channel}\r\n"
                                f"@emote-only=0;followers-only=-1;r9k=0;room-id=42;slow=0;"
                                f"subs-only=0 :tmi.twitch.tv ROOMSTATE #{channel}\r\n")
                    elif command == "PING":
                        if self.answer_ping:
                            await websocket.send(
                                f":tmi.twitch.tv PONG tmi.twitch.tv :{message.trailing}\r\n")
                    elif command == "PRIVMSG":
                        self.privmsgs.append((time.monotonic(), line))
        except Exception:
            return


class FakeTwitchId:
    """A local stand-in for ``https://id.twitch.tv/oauth2`` (``/token`` and ``/validate``).

    Refreshing a known refresh token issues ``access-<n>`` and rotates the
    refresh token to ``refresh-<n>``. ``token_answers`` / ``validate_answers``
    queue canned (status, body) answers that are used before the normal logic.
    """

    def __init__(self, refresh_token: str = "refresh-0", client_id: str = "cid",
                 client_secret: str = "secret", login: str = "hivebot",
                 scopes=("chat:read", "chat:edit"), expires_in: int = 14400):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import parse_qs

        self.valid_refresh = {refresh_token}
        self.client_id = client_id
        self.client_secret = client_secret
        self.login = login
        self.scopes = list(scopes)
        self.expires_in = expires_in
        #: access token -> login it belongs to
        self.access = {}
        self.token_answers = []
        self.validate_answers = []
        #: (method, path, form or None, Authorization header or None)
        self.requests = []
        self._count = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _answer(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                form = {key: values[0] for key, values in
                        parse_qs(self.rfile.read(length).decode()).items()}
                fake.requests.append(("POST", self.path, form, None))
                if fake.token_answers:
                    return self._answer(*fake.token_answers.pop(0))
                if form.get("client_id") != fake.client_id:
                    return self._answer(400, {"status": 400, "message": "invalid client"})
                if form.get("client_secret") != fake.client_secret:
                    return self._answer(403, {"status": 403, "message": "invalid client secret"})
                if (form.get("grant_type") != "refresh_token"
                        or form.get("refresh_token") not in fake.valid_refresh):
                    return self._answer(400, {"error": "Bad Request", "status": 400,
                                              "message": "Invalid refresh token"})
                fake._count += 1
                access, rotated = f"access-{fake._count}", f"refresh-{fake._count}"
                fake.valid_refresh = {rotated}
                fake.access[access] = fake.login
                return self._answer(200, {"access_token": access, "refresh_token": rotated,
                                          "expires_in": fake.expires_in,
                                          "scope": fake.scopes, "token_type": "bearer"})

            def do_GET(self):
                header = self.headers.get("Authorization")
                fake.requests.append(("GET", self.path, None, header))
                if fake.validate_answers:
                    return self._answer(*fake.validate_answers.pop(0))
                token = (header or "").partition(" ")[2]
                if token not in fake.access:
                    return self._answer(401, {"status": 401, "message": "invalid access token"})
                return self._answer(200, {"client_id": fake.client_id,
                                          "login": fake.access[token], "scopes": fake.scopes,
                                          "user_id": "999", "expires_in": fake.expires_in})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/oauth2"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()

    def posts(self):
        return [form for method, _, form, _ in self.requests if method == "POST"]

    def validations(self):
        return [header for method, _, _, header in self.requests if method == "GET"]
