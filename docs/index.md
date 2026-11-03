Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Thalovant fork: WebSocket chat client, token refresh, docs set.

# hivemind-twitch-bridge

A HiveMind bridge for Twitch chat: a satellite whose input and output are a Twitch bot account instead of a microphone. A message in an allowed channel that mentions the bot (or replies to it) becomes `recognizer_loop:utterance` on the hub; `speak` goes back to the channel as a Twitch reply to that message.

- How to run it, every setting and its default: [README](../readme.md)
- From nothing to a working bot: [setup.md](setup.md)
- Facts for lookup: [QUICK_FACTS.md](../QUICK_FACTS.md)
- Questions: [FAQ.md](../FAQ.md)
- Known issues: [AUDIT.md](../AUDIT.md)
- Proposed work: [SUGGESTIONS.md](../SUGGESTIONS.md)
- What changed and how it was verified: [MAINTENANCE_REPORT.md](../MAINTENANCE_REPORT.md)

## Code map

| File | What it holds |
| --- | --- |
| `twitch_bridge/__init__.py` | `HiveMindTwitchBridge`: lifecycle (`run`, `connect_hivemind`, `stop`), `addressed_text` (mention gating), `session_id`, `ready`, `handle_speak` and the reply path, exit statuses, `token_rejected` |
| `twitch_bridge/chat.py` | `TwitchChat`: the WebSocket connection to `wss://irc-ws.chat.twitch.tv:443`, login, JOIN, PING/PONG, reconnect backoff, the outgoing queue; `SendLimiter` and `SlidingWindow` (Twitch's limits) |
| `twitch_bridge/auth.py` | `TwitchAuth`: refresh (`/oauth2/token`), hourly validation (`/oauth2/validate`), static token mode |
| `twitch_bridge/irc.py` | `parse_line`, `format_line`, tag escaping, `split_message` (500 characters) |
| `twitch_bridge/errors.py` | `TokenRejected`, `ClientRejected` |
| `twitch_bridge/health.py` | `HealthServer`: `/healthz` and `/readyz` |
| `twitch_bridge/__main__.py` | `resolve_options`: flags, environment variables, `*_FILE` secrets; `main` |
| `tests/fake_twitch.py` | `FakeTwitch` (a WebSocket server speaking Twitch chat), `FakeTwitchId` (Twitch's token endpoints over HTTP), `FakeHub` |
| `Dockerfile` | the image: uid 10001, `HOME=/home/bridge`, entrypoint without arguments, read-only root friendly |
| `.github/workflows/ci.yml` | tests, lint, image smoke test, e2e through a real hub, signed publish to `ghcr.io/thalovant/hivemind-twitch-bridge` |

## Message flow

1. `TwitchChat` reads a WebSocket frame, splits it into IRC lines and parses each (`twitch_bridge.irc.parse_line`). A `PRIVMSG` in a configured channel from someone other than the bot becomes a `ChatMessage`.
2. `HiveMindTwitchBridge.handle_chat_message` keeps it only if it mentions `@botlogin` or replies to the bot (`reply-parent-user-login` tag), removes the mention, and emits `recognizer_loop:utterance` with `twitch_channel`, `twitch_message_id`, `user` and `session` in the context.
3. The hub answers with `speak`, keeping that context. `handle_speak` runs on the HiveMind bus thread and hands the reply to the chat loop with `call_soon_threadsafe`.
4. `TwitchChat.say` splits it into messages of at most 500 characters, the first starting with `@asker`, and queues them; the sender sends each with `@reply-parent-msg-id=<id>` as soon as `SendLimiter` allows.

Related: the HiveMind hub ([HiveMind-core](https://github.com/JarbasHiveMind/HiveMind-core)) and client library ([hivemind-bus-client](https://github.com/JarbasHiveMind/hivemind-bus-client), `HiveMessageBusClient`); the Telegram bridge built the same way ([thalovant/hivemind-telegram-bridge: docs/index.md](https://github.com/thalovant/hivemind-telegram-bridge/blob/main/docs/index.md)).
