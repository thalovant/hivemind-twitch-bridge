Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: first audit of the Thalovant fork: upstream defects (fixed) and the known limits of the new client.

# Audit: hivemind-twitch-bridge

Evidence: `python -m pytest tests -q --ignore=tests/e2e` (116 passed, Python 3.12 and 3.14), `python -m pytest tests/e2e/test_twitch_hivemind_e2e.py` (2 passed through a real hivemind-core 5.3.10a1 hub), `TWITCH_LIVE=1 python -m pytest tests/e2e/test_twitch_live.py` (2 passed against real Twitch chat, 1 skipped: it needs a bot token), a mutation pass (21 source mutations, each failed a test), the image run in MAINTENANCE_REPORT.md, and reading the code at commit `21a8cd2b3aab` (line numbers below refer to it). Upstream findings were reproduced against upstream's `twitch_bridge/twitch.py` at `dde4b89` with a local IRC server.

## Fixed: upstream's IRC client could not hold a session

- **Where (upstream, removed):** `twitch_bridge/twitch.py` at `dde4b89`: `Twitch.listen` splits each line on `":"`; `recv(1024)` loop; `send_message` concatenates the text into the line; `port=6667`.
- **Finding:** `"@bot meet at 10:30"` arrived as `"@bot meet at 1"`; after the server closed the connection the loop spun on `recv()` returning `b""` (1.76 CPU-seconds per 2 s) and never reconnected; `"hi\r\nPRIVMSG #other :spam"` put a second command on the wire; the token went over plaintext TCP 6667.
- **Now:** `twitch_bridge/chat.py` (`TwitchChat`) over `wss://irc-ws.chat.twitch.tv:443`; `twitch_bridge/irc.py:format_line` refuses CR/LF; tests `tests/test_chat.py::test_a_reply_cannot_inject_a_second_irc_command`, `test_a_dropped_connection_comes_back_and_joins_every_channel_again`, `tests/test_irc.py::test_a_tagged_privmsg_parses`.

## Fixed: a fixed trigger tag matched anywhere

- **Where (upstream):** `JarbasTwitchBridge.on_twitch_message` at `dde4b89` (`tag.lower() in utterance` with default `@bot`).
- **Finding:** `@botany` or any text containing `@bot` was forwarded, and the bot's real login was not a trigger.
- **Now:** `HiveMindTwitchBridge.addressed_text` (`twitch_bridge/__init__.py:313`) matches `@<bot login>` as a whole word or a reply to the bot; `tests/test_mentions.py`.

## 1. A channel Twitch never confirms keeps the bridge not ready

- **Where:** `TwitchChat.ready` and `_join_until_cancelled` (`twitch_bridge/chat.py:491`, warning at `:505`).
- **Finding:** Twitch silently drops a JOIN to a channel that does not exist. By contract `/readyz` needs every channel, so one misspelt or renamed channel makes the whole bridge not ready, while it does answer in the others. Test: `tests/test_chat.py::test_not_ready_until_every_channel_is_joined_and_missing_joins_are_retried`.
- **Impact:** an operator sees a not-ready pod and must read the log line naming the channel.

## 2. No bridge-side limit on incoming questions

- **Where:** `HiveMindTwitchBridge.handle_chat_message` / `forward_to_hivemind` (`twitch_bridge/__init__.py:301`, `:336`).
- **Finding:** every addressed message is forwarded at once. Anyone in an allowed channel can mention the bot repeatedly; only the hub's quotas slow them down. Outgoing answers are limited (`SendLimiter`), so a flood ends in dropped answers (`MAX_PENDING`, `chat.py:531`; `MAX_AGE`, `:557`) rather than a Twitch lockout.

## 3. Every start mints a new access token

- **Where:** `TwitchAuth.start` (`twitch_bridge/auth.py:138`).
- **Finding:** in refresh mode each start refreshes. Twitch allows at most 50 valid access tokens per refresh token and refuses the 51st. A container restarting more than 50 times within one access token's lifetime (about 4 h) would be refused. Mitigations in the code: the hub is connected first, so a hub outage exits before any refresh (`tests/test_bridge.py::test_a_hub_that_never_answers_exits_1_before_any_twitch_request`); an unrecognised 400 is retried, not treated as `token_rejected` (`auth.py:277`, `tests/test_auth.py::test_twitch_errors_and_unknown_answers_are_retried_not_fatal`).

## 4. A rotated refresh token lives in memory only

- **Where:** `TwitchAuth._refresh_once` (`twitch_bridge/auth.py:248`, log line `:261`).
- **Finding:** by design nothing is written to disk. If Twitch ever invalidated the previous refresh token on rotation, the next restart would fail with `token_rejected`. For a confidential client the stored refresh token stays valid unless revoked, which is what the design relies on.

## 5. A refused login is retried once in refresh mode, never in static mode

- **Where:** `HiveMindTwitchBridge._run_chat` (`twitch_bridge/__init__.py:232`).
- **Finding:** when chat refuses a freshly refreshed token the bridge refreshes once more before writing `token_rejected` (`tests/test_bridge.py::test_chat_refusing_a_refreshed_token_gets_one_more_refresh`). A `TWITCH_OAUTH_TOKEN` gets no second chance (`test_a_chat_login_twitch_refuses_writes_token_rejected_and_exits_3`).

## 6. Answers depend on the hub keeping the question's context

- **Where:** `HiveMindTwitchBridge._reply` (`twitch_bridge/__init__.py:370`).
- **Finding:** a `speak` is posted only if its context still carries `twitch_channel` (and, for a threaded reply, `twitch_message_id`). A skill that emits `speak` with a fresh context is dropped silently. Test: `tests/test_bridge.py::test_answers_for_other_bridges_or_channels_are_not_posted`; the real hub keeps the context (`tests/e2e/test_twitch_hivemind_e2e.py`).

## 7. Twitch drops a repeated identical message

- **Where:** Twitch's duplicate rule (`msg_duplicate` notice, logged by id in `TwitchChat._handle_notice`).
- **Finding:** an account that is not a moderator cannot send the same text twice within 30 s in a channel. The `@asker` prefix makes most answers differ, but the same answer to the same person twice in 30 s (for example the fixed fallback line) is dropped by Twitch.

## 8. The fallback reply is English only

- **Where:** `twitch_bridge/__init__.py:368`.
- **Finding:** `hive.complete_intent_failure` always answers "I don't know how to answer that", whatever `HIVEMIND_LANG` is.

## 9. The HiveMind client uses the shared identity

- **Where:** `twitch_bridge/__init__.py:147` (`HiveMessageBusClient` built without `identity=`).
- **Finding:** by contract the Noise key is the unnamed-node default (`~/.config/hivemind/unnamed-node_noise.key`), so hivemind-bus-client logs a warning on every start that the shared identity is used. Harmless in a container that runs one bridge; two bridges sharing one home would share one key.

## 10. Live coverage

- **Where:** `tests/e2e/test_twitch_live.py`.
- **Finding:** the real Twitch endpoint was exercised anonymously (login, two JOINs, keepalive PINGs) and with a made-up token (`Login authentication failed`), on 2026-11-03. Nothing has been tested with a real bot token, a real refresh against id.twitch.tv, or a real send into a channel.

## 11. Prerelease client, unpinned base image

- **Where:** `requirements.txt:1` (`hivemind-bus-client>=1.0.16a1`), `Dockerfile:1` (`FROM python:3.14-slim`).
- **Finding:** each image build can resolve a different alpha and a different base; builds are not reproducible.
