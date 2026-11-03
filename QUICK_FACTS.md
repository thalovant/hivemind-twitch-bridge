Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: RSA identity path; exit statuses, and what the operator reads from them.

# Quick facts: hivemind-twitch-bridge

| Key | Value |
| --- | --- |
| Package (distribution) | `HiveMind-twitch-bridge` |
| Import name | `twitch_bridge` |
| Version | `0.1.0` (`twitch_bridge/version.py`) |
| Console entry point | `hivemind-twitch-bridge` -> `twitch_bridge.__main__:main` |
| Main class | `twitch_bridge.HiveMindTwitchBridge` |
| Chat client | `twitch_bridge.chat.TwitchChat` (`wss://irc-ws.chat.twitch.tv:443`, caps `twitch.tv/tags twitch.tv/commands`) |
| Token upkeep | `twitch_bridge.auth.TwitchAuth` (`https://id.twitch.tv/oauth2/token`, `/oauth2/validate`) |
| Health server | `twitch_bridge.health.HealthServer` (`/healthz`, `/readyz`) |
| Network egress | TCP 443 to `irc-ws.chat.twitch.tv` and `id.twitch.tv`; the hub's host and port |
| Image | `ghcr.io/thalovant/hivemind-twitch-bridge` (`:sha-<commit>` and `:latest`, signed by digest with cosign keyless) |
| Image user | `bridge`, uid 10001, `HOME=/home/bridge`; writable: `/home/bridge/.config/hivemind`, `/tmp` |
| Noise key | `/home/bridge/.config/hivemind/unnamed-node_noise.key` (hivemind-bus-client default for an unnamed node) |
| RSA identity | `/home/bridge/.config/hivemind/unnamed-node.pem`; where the operator runs the bridge, both keys come read-only from its credentials Secret (hivemind-k8s-operator#122) |
| Exit statuses | 3 Twitch refused the bot's token (`token_rejected` in `/dev/termination-log`), 4 Twitch refused the client id or secret (no termination message), 1 hub unreachable or refused; the operator reports `TokenRejected` from the message only (hivemind-k8s-operator#121) |
| Default branch | `main` (upstream: `JarbasHiveMind/HiveMind-twitch-bridge`, branch `dev`) |
| CI | `.github/workflows/ci.yml`: tests on Python 3.12 and 3.14, ruff `F,E9`, image smoke test, e2e through a real hub, publish on `main` |
| Tests | `tests/`, 170 tests (`python -m pytest tests -q --ignore=tests/e2e`); `tests/e2e`, 5 tests (2 through a real hub, 3 opt-in against real Twitch) |
| HiveMind message types | sends `recognizer_loop:utterance`; handles `speak`, `hive.complete_intent_failure` |
| License | Apache-2.0 |

## Settings

| Flag | Environment variable | Default |
| --- | --- | --- |
| `--bot-login` | `TWITCH_BOT_LOGIN` | required |
| `--allowed-channel` (repeatable) | `TWITCH_ALLOWED_CHANNELS` (comma/space separated) | required, at most 100 |
| `--client-id` | `TWITCH_CLIENT_ID` | none |
| `--client-secret` | `TWITCH_CLIENT_SECRET` (or `_FILE`) | none |
| `--refresh-token` | `TWITCH_REFRESH_TOKEN` (or `_FILE`) | none |
| `--oauth-token` | `TWITCH_OAUTH_TOKEN` (or `_FILE`, read at every login) | none; used only without a refresh token |
| `--no-require-mention` | `TWITCH_REQUIRE_MENTION` | `true` |
| `--session-scope` | `TWITCH_SESSION_SCOPE` (`person` or `chat`) | `person` |
| `--health-port` | `BRIDGE_HEALTH_PORT` (0-65535) | off |
| `--access-key` | `HIVEMIND_ACCESS_KEY` (or `_FILE`) | identity file |
| `--password` | `HIVEMIND_PASSWORD` (or `_FILE`) | identity file |
| `--host`, `--port` | `HIVEMIND_HOST`, `HIVEMIND_PORT` (1-65535) | identity file, `5678` |
| `--site-id`, `--lang`, `--self-signed` | `HIVEMIND_SITE_ID`, `HIVEMIND_LANG`, `HIVEMIND_SELF_SIGNED` | `twitch`, `en-us`, `false` |

Precedence: flag > variable > `<VARIABLE>_FILE`. A refresh token needs `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET` too; with all three, `TWITCH_OAUTH_TOKEN` is ignored.

## Behaviour

| Item | Value |
| --- | --- |
| Session id | `person`: `twitch-<channel>-<user id>` (login when no id); `chat`: `twitch-<channel>` |
| Forwarded | `PRIVMSG` in a configured channel the bot has joined that mentions `@botlogin` or replies to the bot, mention removed; with `TWITCH_REQUIRE_MENTION=false`, every such `PRIVMSG`, the mention removed when present |
| Never forwarded | whispers, the bot's own lines, other channels, a channel before its JOIN is confirmed or after the bot's PART, messages while the hub session is down |
| Reply | `@reply-parent-msg-id=<id> PRIVMSG #<channel> :@<asker> <text>`, split at 500 characters (UTF-16 units), every part starting with `@<asker> `; with no asker, a part starting with `/` or `.` gets U+200B first (`COMMAND_GUARD`), so no part is a Twitch command |
| Bot loops | at most 8 replies to the bot answered per account and channel in 10 min (`LOOP_MAX_REPLIES`, `LOOP_WINDOW`); only replies that reached the hub count |
| Send limits | 18 per 30 s for the account (`MESSAGES_PER_WINDOW`), 1.1 s per channel (`CHANNEL_GAP`), slow mode honoured (capped at 120 s, `MAX_SLOW_SECONDS`), 30 s pause on `msg_ratelimit` |
| Queue | 50 messages per channel (`MAX_PENDING`); older than 120 s dropped (`MAX_AGE`) |
| Joins | 15 per 10 s (`JOINS_PER_WINDOW`); unconfirmed channels joined again every 60 s (`JOIN_RETRY`), at once after the bot's own PART |
| Keepalive | own `PING` after 60 s of silence (`KEEPALIVE`), reconnect if unanswered in 15 s (`PONG_TIMEOUT`); login must be welcomed in 20 s |
| Reconnect | backoff `min(60, 2**attempt)` s times 0.5-1.0; immediate on `RECONNECT` or a token change; after sessions under 60 s (`STABLE_AFTER`), only the first such reconnect is immediate, later ones in a row back off |
| Token calls | `https://id.twitch.tv` only, certificates verified, redirects never followed |
| Token | refreshed at start and 10 min before expiry (`REFRESH_MARGIN`), validated hourly (`VALIDATE_EVERY`), one extra refresh if chat refuses a fresh token |
| Ready | hub handshake live AND logged in AND every configured channel joined |

## Exit statuses

| Status | Meaning |
| --- | --- |
| `0` | stopped (SIGTERM, SIGINT) |
| `1` | hub unreachable after 10 handshake attempts, or refused the bridge |
| `2` | missing or malformed setting |
| `3` | Twitch refused the bot's token; `token_rejected` written to `/dev/termination-log` |
| `4` | Twitch refused the client id or secret |
