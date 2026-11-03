"""CLI entry point for the HiveMind <-> Twitch chat bridge.

Every option can come from the environment, so a container never has to
put a secret on its command line, where any process on the host can read
it. Secrets also accept a ``<VARIABLE>_FILE`` variable naming a file that
holds the value, the convention for secrets mounted into a container. A
flag wins over its environment variable, and the variable over its file.

HiveMind identity (key/password/host/port) falls back to the values stored
by ``hivemind-client set-identity`` when none is given.
"""
import argparse
import os
import sys
from typing import Mapping, Optional, Sequence

from ovos_utils.log import LOG

from twitch_bridge import HiveMindTwitchBridge
from twitch_bridge.chat import MAX_CHANNELS
from twitch_bridge.irc import LOGIN_PATTERN

#: option -> environment variable it falls back to.
ENVIRONMENT = {
    "oauth_token": "TWITCH_OAUTH_TOKEN",
    "client_id": "TWITCH_CLIENT_ID",
    "client_secret": "TWITCH_CLIENT_SECRET",
    "refresh_token": "TWITCH_REFRESH_TOKEN",
    "bot_login": "TWITCH_BOT_LOGIN",
    "channels": "TWITCH_ALLOWED_CHANNELS",
    "require_mention": "TWITCH_REQUIRE_MENTION",
    "session_scope": "TWITCH_SESSION_SCOPE",
    "health_port": "BRIDGE_HEALTH_PORT",
    "key": "HIVEMIND_ACCESS_KEY",
    "password": "HIVEMIND_PASSWORD",
    "host": "HIVEMIND_HOST",
    "port": "HIVEMIND_PORT",
    "site_id": "HIVEMIND_SITE_ID",
    "self_signed": "HIVEMIND_SELF_SIGNED",
    "lang": "HIVEMIND_LANG",
}
#: options whose value may be read from the file named by ``<VARIABLE>_FILE``.
SECRETS = ("oauth_token", "client_secret", "refresh_token", "key", "password")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hivemind-twitch-bridge",
        description="Bridge a Twitch bot's chat to a HiveMind hub",
        epilog="Every option falls back to an environment variable: "
               + ", ".join(ENVIRONMENT.values())
               + ". TWITCH_OAUTH_TOKEN, TWITCH_CLIENT_SECRET, TWITCH_REFRESH_TOKEN, "
                 "HIVEMIND_ACCESS_KEY and HIVEMIND_PASSWORD also accept "
                 "<VARIABLE>_FILE naming a file that holds the value.")
    # Twitch
    parser.add_argument("--bot-login", default=None,
                        help="the bot account's Twitch login [TWITCH_BOT_LOGIN]")
    parser.add_argument("--allowed-channel", dest="channels", action="append", default=None,
                        help="channel login to join and answer in, repeatable; the bot "
                             "joins exactly these [TWITCH_ALLOWED_CHANNELS]")
    parser.add_argument("--client-id", default=None,
                        help="Twitch application client id, to refresh the token "
                             "[TWITCH_CLIENT_ID]")
    parser.add_argument("--client-secret", default=None,
                        help="Twitch application client secret [TWITCH_CLIENT_SECRET]")
    parser.add_argument("--refresh-token", default=None,
                        help="the bot's refresh token [TWITCH_REFRESH_TOKEN]")
    parser.add_argument("--oauth-token", default=None,
                        help="a user access token with chat:read and chat:edit, for short "
                             "tests; not refreshed [TWITCH_OAUTH_TOKEN]")
    parser.add_argument("--no-require-mention", dest="require_mention",
                        action="store_false", default=None,
                        help="forward every message in the channels, not only those that "
                             "mention the bot or reply to it [TWITCH_REQUIRE_MENTION]")
    parser.add_argument("--session-scope", choices=("person", "chat"), default=None,
                        help="one hub conversation per person in each channel (default) or "
                             "one per channel [TWITCH_SESSION_SCOPE]")
    parser.add_argument("--health-port", type=int, default=None,
                        help="serve /healthz and /readyz on this port (default: off) "
                             "[BRIDGE_HEALTH_PORT]")
    # HiveMind
    parser.add_argument("--access-key", dest="key", default=None,
                        help="HiveMind access key (default: from identity file) "
                             "[HIVEMIND_ACCESS_KEY]")
    parser.add_argument("--password", default=None,
                        help="HiveMind password (default: from identity file) "
                             "[HIVEMIND_PASSWORD]")
    parser.add_argument("--host", default=None,
                        help="HiveMind host, e.g. ws://127.0.0.1 (default: from identity "
                             "file) [HIVEMIND_HOST]")
    parser.add_argument("--port", type=int, default=None,
                        help="HiveMind port (default: 5678) [HIVEMIND_PORT]")
    parser.add_argument("--site-id", default=None,
                        help="this bridge's HiveMind site id (default: twitch) "
                             "[HIVEMIND_SITE_ID]")
    parser.add_argument("--self-signed", action="store_true", default=None,
                        help="accept self-signed SSL certificates [HIVEMIND_SELF_SIGNED]")
    parser.add_argument("--lang", default=None,
                        help="utterance language (default: en-us) [HIVEMIND_LANG]")
    return parser


def _read_secret_file(path: str, variable: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError as error:
        raise SystemExit(f"{variable}: cannot read {path}: {error.strerror}") from None


def _from_environment(option: str, environ: Mapping[str, str]) -> Optional[str]:
    variable = ENVIRONMENT[option]
    value = environ.get(variable)
    if value:
        return value
    if option in SECRETS:
        path = environ.get(f"{variable}_FILE")
        if path:
            return _read_secret_file(path, f"{variable}_FILE")
    return None


def resolve_options(argv: Optional[Sequence[str]] = None,
                    environ: Optional[Mapping[str, str]] = None) -> dict:
    """The bridge's settings: flags first, then the environment, then defaults.

    Raises ``SystemExit`` with a usage message when a required setting is
    missing or a value is malformed.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ

    def pick(option):
        given = getattr(args, option)
        return given if given is not None else _from_environment(option, environ)

    bot_login = (pick("bot_login") or "").strip().lower()
    if not bot_login:
        parser.error("the bot's login is required: --bot-login or TWITCH_BOT_LOGIN")
    if not LOGIN_PATTERN.fullmatch(bot_login):
        parser.error("TWITCH_BOT_LOGIN must be a Twitch login (letters, digits, underscore)")

    listed = args.channels
    if listed is None:
        listed = [_from_environment("channels", environ) or ""]
    channels = []
    for item in " ".join(listed).replace(",", " ").split():
        login = item.lstrip("#").lower()
        if not LOGIN_PATTERN.fullmatch(login):
            parser.error(f"TWITCH_ALLOWED_CHANNELS: {item!r} is not a channel login")
        if login not in channels:
            channels.append(login)
    if not channels:
        parser.error("at least one channel is required: --allowed-channel or "
                     "TWITCH_ALLOWED_CHANNELS")
    if len(channels) > MAX_CHANNELS:
        parser.error(f"TWITCH_ALLOWED_CHANNELS lists {len(channels)} channels; Twitch "
                     f"allows {MAX_CHANNELS}")

    client_id, client_secret, refresh_token = (pick("client_id"), pick("client_secret"),
                                               pick("refresh_token"))
    refresh = {"TWITCH_CLIENT_ID": client_id, "TWITCH_CLIENT_SECRET": client_secret,
               "TWITCH_REFRESH_TOKEN": refresh_token}
    if any(refresh.values()) and not all(refresh.values()):
        missing = ", ".join(name for name, value in refresh.items() if not value)
        parser.error(f"refreshing the Twitch token needs TWITCH_CLIENT_ID, "
                     f"TWITCH_CLIENT_SECRET and TWITCH_REFRESH_TOKEN; missing: {missing}")

    # A token from a file is passed as the file, so a rotated secret is read
    # again at the next login.
    oauth_token = args.oauth_token or environ.get(ENVIRONMENT["oauth_token"]) or None
    oauth_token_file = None
    if not oauth_token and environ.get("TWITCH_OAUTH_TOKEN_FILE"):
        oauth_token_file = environ["TWITCH_OAUTH_TOKEN_FILE"]
        if not _read_secret_file(oauth_token_file, "TWITCH_OAUTH_TOKEN_FILE"):
            parser.error("TWITCH_OAUTH_TOKEN_FILE names an empty file")
    if not all(refresh.values()) and not (oauth_token or oauth_token_file):
        parser.error("a Twitch token is required: TWITCH_REFRESH_TOKEN with TWITCH_CLIENT_ID "
                     "and TWITCH_CLIENT_SECRET (refreshed), or TWITCH_OAUTH_TOKEN "
                     "(each also as <VARIABLE>_FILE)")

    port = pick("port")
    try:
        port = int(port) if port is not None else 5678
    except ValueError:
        parser.error("HIVEMIND_PORT must be a number")

    self_signed = args.self_signed
    if self_signed is None:
        self_signed = str(environ.get(ENVIRONMENT["self_signed"], "")).lower() in _TRUE

    require_mention = args.require_mention
    if require_mention is None:
        setting = str(environ.get(ENVIRONMENT["require_mention"], "")).strip().lower()
        if setting and setting not in _TRUE | _FALSE:
            parser.error("TWITCH_REQUIRE_MENTION must be true or false")
        require_mention = setting not in _FALSE

    health_port = pick("health_port")
    try:
        health_port = int(health_port) if health_port is not None else None
    except ValueError:
        parser.error("BRIDGE_HEALTH_PORT must be a number")

    session_scope = pick("session_scope") or "person"
    if session_scope not in ("person", "chat"):
        parser.error("TWITCH_SESSION_SCOPE must be 'person' or 'chat'")

    host = pick("host")
    if host and not host.startswith("ws://") and not host.startswith("wss://"):
        host = "ws://" + host

    return {
        "bot_login": bot_login,
        "channels": channels,
        "oauth_token": oauth_token,
        "oauth_token_file": oauth_token_file,
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "key": pick("key"),
        "password": pick("password"),
        "host": host,
        "port": port,
        "self_signed": self_signed,
        "lang": pick("lang") or "en-us",
        "site_id": pick("site_id") or "twitch",
        "require_mention": require_mention,
        "session_scope": session_scope,
        "health_port": health_port,
    }


def main(argv: Optional[Sequence[str]] = None) -> None:
    options = resolve_options(argv)
    LOG.info(f"bridge starting for {options['bot_login']} in "
             f"{', '.join('#' + channel for channel in options['channels'])}")
    try:
        bridge = HiveMindTwitchBridge(**options)
    except (ValueError, RuntimeError) as error:
        raise SystemExit(f"hivemind-twitch-bridge: {error}") from None
    sys.exit(bridge.run())


if __name__ == '__main__':
    main()
