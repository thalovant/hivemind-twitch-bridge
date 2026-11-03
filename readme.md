Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: CodeRabbit on PR #2: the no-asker case of the command guard.

# HiveMind Twitch Bridge

This bridges a Twitch bot account's chat to a [HiveMind](https://github.com/JarbasHiveMind/HiveMind-core) hub. A HiveMind bridge is a satellite whose input and output are a chat platform instead of a microphone: a chat message that mentions the bot becomes a HiveMind utterance, and the hub's spoken reply is posted back into the same channel as a Twitch reply to that message.

```
Twitch chat (wss://irc-ws.chat.twitch.tv:443)  ⇄  hivemind-twitch-bridge  ⇄  HiveMind hub  ⇄  OVOS skills
```

![](./twitch.png)
![](./bridge.png)

New here? [docs/setup.md](docs/setup.md) walks through it from scratch: the Twitch application, the bot's token, registering the bridge on the hub, and checking a message round-trips.

## What it does

- Connects to Twitch chat over its WebSocket endpoint, `wss://irc-ws.chat.twitch.tv:443` (TCP 443 is the only port it needs), with the IRCv3 `twitch.tv/tags` and `twitch.tv/commands` capabilities.
- Joins exactly the channels you list, and only answers there. Whispers are never answered.
- Forwards only messages that mention the bot (`@botlogin what time is it`) or reply to one of its messages, with the mention removed, so the hub is asked `what time is it`.
- Posts the hub's `speak` as a Twitch reply (threaded under the question), split into messages of at most 500 characters, each starting with `@asker` when the asker is known. Without an asker, a part that starts with `/` or `.` begins with an invisible guard (U+200B) instead, so no part can run as a Twitch command (`/ban`, `.timeout`).
- Stops answering an account's replies after 8 in 10 minutes in a channel, so two bots cannot answer each other forever.
- Stays under Twitch's chat limits for an account that is not a moderator: at most 18 messages in any 30 seconds for the whole account (Twitch's limit is 20, counted per user, so it also holds per channel), at least 1.1 s between messages in one channel, longer in slow mode. Outgoing messages wait in a queue; an answer that waited more than 2 minutes is dropped.
- Answers Twitch's PING, sends its own after a minute of silence, and reconnects with exponential backoff (at once when Twitch asks with RECONNECT), joining every channel again.
- Refreshes the bot's user access token before it expires, and validates it hourly, as Twitch asks.
- Stops on a credential Twitch refuses, writing `token_rejected` to `/dev/termination-log` (exit status 3).
- Never logs what anyone wrote, what the bridge answered, or a token.

## Getting a token

The bridge logs in as a Twitch account (the bot). It needs that account's **user access token** with the `chat:read` and `chat:edit` scopes.

For a bridge that runs for days, register an application at <https://dev.twitch.tv/console/apps> (client type **Confidential**), get the bot's consent with the authorization code flow for `chat:read chat:edit`, and keep the **refresh token** Twitch returns. Give the bridge the application's client id and secret and that refresh token: it trades the refresh token for access tokens on its own. A confidential client's refresh token does not expire; it stops working only if the bot's password changes or the bot disconnects the application.

For a short test, a plain access token (`TWITCH_OAUTH_TOKEN`, with or without the `oauth:` prefix) also works. Nothing refreshes it: once Twitch says it is invalid, the bridge stops.

Treat all of these like passwords.

## Registering the bridge on the hub

```bash
hivemind-core add-client
hivemind-core allow-msg recognizer_loop:utterance <client_id>
hivemind-core allow-msg speak <client_id>
```

`add-client` prints the access key and password for the bridge. A freshly added client is denied every message type until it is allowed; skipping `allow-msg` is the most common reason a bridge "connects fine" but nothing happens.

## Running the bridge

```bash
pip install .
export TWITCH_BOT_LOGIN=mybot
export TWITCH_ALLOWED_CHANNELS="mychannel, otherchannel"
export TWITCH_CLIENT_ID=... TWITCH_CLIENT_SECRET=... TWITCH_REFRESH_TOKEN=...
export HIVEMIND_ACCESS_KEY=... HIVEMIND_PASSWORD=... HIVEMIND_HOST=ws://127.0.0.1
hivemind-twitch-bridge
```

Every setting comes from the environment; each also has a flag (`hivemind-twitch-bridge --help`). A flag wins over its variable. Secrets also accept `<VARIABLE>_FILE`, the path of a file holding the value (how container platforms mount secrets); the variable wins over its file.

| Variable | Default | Meaning |
| --- | --- | --- |
| `TWITCH_BOT_LOGIN` | required | the bot account's login name |
| `TWITCH_ALLOWED_CHANNELS` | required | channel logins, comma or space separated (`#` and case are ignored); the bot joins exactly these, at most 100 |
| `TWITCH_CLIENT_ID` | none | the Twitch application's client id |
| `TWITCH_CLIENT_SECRET` (`_FILE`) | none | the application's client secret |
| `TWITCH_REFRESH_TOKEN` (`_FILE`) | none | the bot's refresh token; with the two above, the bridge refreshes its own access token |
| `TWITCH_OAUTH_TOKEN` (`_FILE`) | none | a plain access token for short tests, used when no refresh token is set; a `_FILE` is read again at every login |
| `TWITCH_REQUIRE_MENTION` | `true` | `false` forwards every message in the channels |
| `TWITCH_SESSION_SCOPE` | `person` | `person`: one hub session per person per channel (`twitch-<channel>-<user id>`); `chat`: one per channel (`twitch-<channel>`) |
| `BRIDGE_HEALTH_PORT` | off | serve `/healthz` and `/readyz` on this port (0-65535) |
| `HIVEMIND_HOST` | identity file | hub address, `ws://` or `wss://` (`ws://` is added when missing) |
| `HIVEMIND_PORT` | `5678` | hub port (1-65535) |
| `HIVEMIND_ACCESS_KEY` (`_FILE`) | identity file | the bridge's access key on the hub |
| `HIVEMIND_PASSWORD` (`_FILE`) | identity file | its password |
| `HIVEMIND_SITE_ID` | `twitch` | this bridge's HiveMind site id |
| `HIVEMIND_SELF_SIGNED` | `false` | accept a self-signed TLS certificate on a `wss://` hub |
| `HIVEMIND_LANG` | `en-us` | language tag on forwarded utterances |

`/healthz` answers 200 while the process runs. `/readyz` answers 200 only while the HiveMind session is live **and** the bot is logged in to Twitch and has joined every listed channel (503 otherwise). A channel Twitch never confirms (misspelt, renamed or suspended) keeps the bridge not ready; the log names it every minute.

Exit statuses: `0` stopped (SIGTERM or SIGINT), `1` the hub is unreachable or refused the bridge, `2` a setting is missing or malformed, `3` Twitch refused the bot's token (`token_rejected` written to `/dev/termination-log`), `4` Twitch refused the client id or secret.

## Docker

Put each secret in its own file under `./secrets`, readable by the container's user and nobody else (`chown 10001 secrets/* && chmod 0400 secrets/*`); `.gitignore` and `.dockerignore` keep the directory out of git and out of the image. Then:

```bash
docker build -t hivemind-twitch-bridge .
docker run --rm --read-only --tmpfs /tmp \
  -v twitch-identity:/home/bridge/.config/hivemind \
  -v "$PWD/secrets:/run/secrets:ro" \
  -e TWITCH_BOT_LOGIN=mybot -e TWITCH_ALLOWED_CHANNELS=mychannel \
  -e TWITCH_CLIENT_ID=... \
  -e TWITCH_CLIENT_SECRET_FILE=/run/secrets/twitch_client_secret \
  -e TWITCH_REFRESH_TOKEN_FILE=/run/secrets/twitch_refresh_token \
  -e HIVEMIND_ACCESS_KEY_FILE=/run/secrets/hivemind_access_key \
  -e HIVEMIND_PASSWORD_FILE=/run/secrets/hivemind_password \
  -e HIVEMIND_HOST=ws://hivemind-core \
  hivemind-twitch-bridge
```

A secret given as `-e NAME=value` would sit in the `docker run` command line, which any local user can read for as long as the container runs. `docker-compose.yml` is the other way to run it.

The image runs as `bridge` (uid 10001) with `HOME=/home/bridge` and no default arguments. It writes only to `/home/bridge/.config/hivemind` (the HiveMind identity and the Noise static key, `unnamed-node_noise.key`) and `/tmp`, so the root filesystem can be read-only. Keep the identity directory on a volume if the hub pins the bridge's key.

Signed images are published as `ghcr.io/thalovant/hivemind-twitch-bridge:sha-<commit>` and `:latest` (cosign keyless, verified against `.github/workflows/ci.yml@refs/heads/main`).

## Testing

```bash
pip install -e .[test]
pytest tests --ignore=tests/e2e        # Twitch chat and the token endpoints are local servers
pip install -e .[e2e]
pytest tests/e2e                       # through a real hivemind-core hub (hivescope)
TWITCH_LIVE=1 pytest tests/e2e/test_twitch_live.py   # real Twitch chat, no credentials needed
```

`tests/fake_twitch.py` is a real WebSocket server that speaks Twitch's chat protocol and a real HTTP server with Twitch's token endpoints, so the tests go over sockets. `TWITCH_LIVE=1` logs in anonymously to the real endpoint and checks that a made-up token is refused; with `TWITCH_LIVE_OAUTH_TOKEN`, `TWITCH_LIVE_BOT_LOGIN` and `TWITCH_LIVE_CHANNEL` it also logs in as a real bot.

## Documentation

- [docs/index.md](docs/index.md): overview and code map
- [docs/setup.md](docs/setup.md): from nothing to a working bot
- [QUICK_FACTS.md](QUICK_FACTS.md), [FAQ.md](FAQ.md), [AUDIT.md](AUDIT.md), [SUGGESTIONS.md](SUGGESTIONS.md), [MAINTENANCE_REPORT.md](MAINTENANCE_REPORT.md)

## License

Apache-2.0, see [LICENSE](LICENSE).
