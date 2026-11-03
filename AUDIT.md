Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Section 12 restated for the managed deployment: the owner supplies the client id and secret, and the operator maps only token_rejected.

# Audit: hivemind-twitch-bridge

Evidence: `python -m pytest tests -q --ignore=tests/e2e` (133 passed, Python 3.12 and 3.14), `python -m pytest tests/e2e/test_twitch_hivemind_e2e.py` (2 passed through a real hivemind-core 5.3.10a1 hub), `TWITCH_LIVE=1 python -m pytest tests/e2e/test_twitch_live.py` (2 passed against real Twitch chat, 1 skipped: it needs a bot token), a mutation pass (21 source mutations, each failed a test) and a revert check for each review fix (8, each failed its test), the image run in MAINTENANCE_REPORT.md, and reading the code at commit `b50c55945e3e` (line numbers below refer to it). Upstream findings were reproduced against upstream's `twitch_bridge/twitch.py` at `dde4b89` with a local IRC server.

## Fixed: upstream's IRC client could not hold a session

- **Where (upstream, removed):** `twitch_bridge/twitch.py` at `dde4b89`: `Twitch.listen` splits each line on `":"`; `recv(1024)` loop; `send_message` concatenates the text into the line; `port=6667`.
- **Finding:** `"@bot meet at 10:30"` arrived as `"@bot meet at 1"`; after the server closed the connection the loop spun on `recv()` returning `b""` (1.76 CPU-seconds per 2 s) and never reconnected; `"hi\r\nPRIVMSG #other :spam"` put a second command on the wire; the token went over plaintext TCP 6667.
- **Now:** `twitch_bridge/chat.py` (`TwitchChat`) over `wss://irc-ws.chat.twitch.tv:443`; `twitch_bridge/irc.py:format_line` refuses CR/LF; tests `tests/test_chat.py::test_a_reply_cannot_inject_a_second_irc_command`, `test_a_dropped_connection_comes_back_and_joins_every_channel_again`, `tests/test_irc.py::test_a_tagged_privmsg_parses`.

## Fixed: a fixed trigger tag matched anywhere

- **Where (upstream):** `JarbasTwitchBridge.on_twitch_message` at `dde4b89` (`tag.lower() in utterance` with default `@bot`).
- **Finding:** `@botany` or any text containing `@bot` was forwarded, and the bot's real login was not a trigger.
- **Now:** `HiveMindTwitchBridge.addressed_text` (`twitch_bridge/__init__.py:326`) matches `@<bot login>` as a whole word or a reply to the bot; `tests/test_mentions.py`.

## Fixed after review (pull request #1)

- **Hub refusal before the event loop:** `_hub_refused` used to drop a stop asked for before `_run` set the loop, leaving the bridge up without a hub. Now kept in `_pending_stop` under `_loop_lock` (`twitch_bridge/__init__.py`). Test: `tests/test_bridge.py::test_a_hub_refusal_before_the_loop_starts_still_stops_the_bridge`.
- **Port ranges:** `BRIDGE_HEALTH_PORT` and `HIVEMIND_PORT` outside their range exit 2 (`resolve_options`). Test: `tests/test_cli.py::test_malformed_values_are_refused`.
- **A just-issued token refused by `/validate`:** raises `TokenRejected` (`TwitchAuth._validate_fresh`). Test: `tests/test_auth.py::test_a_freshly_issued_token_that_validate_refuses_is_token_rejected`.
- **PART:** wakes the join loop (`TwitchChat._rejoin`), so the channel is joined again at once. Test: `tests/test_chat.py::test_leaving_a_channel_makes_the_bridge_not_ready_and_it_joins_again_at_once`.
- **Delivery:** only from joined channels (`TwitchChat._deliver`). Test: `tests/test_chat.py::test_nothing_is_delivered_from_a_channel_the_bot_is_not_in`.
- **Sender:** a reply id of another type is turned into text, a mention that is not a login is ignored, an unsendable answer is dropped and the send loop survives any error (`TwitchChat.say`, `_send_next`, `_send_loop`). Tests: `tests/test_chat.py::test_a_reply_id_or_mention_of_another_type_cannot_stop_the_sender`, `test_the_sender_survives_an_item_it_cannot_send`, `tests/test_bridge.py::test_a_reply_id_of_another_type_from_the_hub_is_still_sent`.
- **Tag keys:** `format_line` refuses a key that is not IRCv3 key syntax and a value that is not text. Test: `tests/test_irc.py::test_a_tag_key_cannot_smuggle_anything`.
- **Secrets:** `.dockerignore` and `.gitignore` exclude `.env`, `.env.*` and `secrets`, so `COPY . /app` cannot bake a local secret into the image; the README's `docker run` passes secrets as `*_FILE` mounts instead of `-e` arguments.

## 1. A channel Twitch never confirms keeps the bridge not ready

- **Where:** `TwitchChat.ready` and `_join_until_cancelled` (`twitch_bridge/chat.py:496`, warning at `:514`).
- **Finding:** Twitch silently drops a JOIN to a channel that does not exist. By contract `/readyz` needs every channel, so one misspelt or renamed channel makes the whole bridge not ready, while it does answer in the others. Test: `tests/test_chat.py::test_not_ready_until_every_channel_is_joined_and_missing_joins_are_retried`.
- **Impact:** an operator sees a not-ready pod and must read the log line naming the channel.

## 2. No bridge-side limit on incoming questions

- **Where:** `HiveMindTwitchBridge.handle_chat_message` / `forward_to_hivemind` (`twitch_bridge/__init__.py:314`, `:349`).
- **Finding:** every addressed message is forwarded at once. Anyone in an allowed channel can mention the bot repeatedly; only the hub's quotas slow them down. Outgoing answers are limited (`SendLimiter`), so a flood ends in dropped answers (`MAX_PENDING`, `chat.py:546`; `MAX_AGE`, `:577`) rather than a Twitch lockout.

## 3. Every start mints a new access token

- **Where:** `TwitchAuth.start` (`twitch_bridge/auth.py:138`).
- **Finding:** in refresh mode each start refreshes. Twitch allows at most 50 valid access tokens per refresh token and refuses the 51st. A container restarting more than 50 times within one access token's lifetime (about 4 h) would be refused. Mitigations in the code: the hub is connected first, so a hub outage exits before any refresh (`tests/test_bridge.py::test_a_hub_that_never_answers_exits_1_before_any_twitch_request`); an unrecognised 400 is retried, not treated as `token_rejected` (`auth.py:277`, `tests/test_auth.py::test_twitch_errors_and_unknown_answers_are_retried_not_fatal`).

## 4. A rotated refresh token lives in memory only

- **Where:** `TwitchAuth._refresh_once` (`twitch_bridge/auth.py:248`, log line `:261`).
- **Finding:** by design nothing is written to disk. If Twitch ever invalidated the previous refresh token on rotation, the next restart would fail with `token_rejected`. For a confidential client the stored refresh token stays valid unless revoked, which is what the design relies on.

## 5. A refused login is retried once in refresh mode, never in static mode

- **Where:** `HiveMindTwitchBridge._run_chat` (`twitch_bridge/__init__.py:241`).
- **Finding:** when chat refuses a freshly refreshed token the bridge refreshes once more before writing `token_rejected` (`tests/test_bridge.py::test_chat_refusing_a_refreshed_token_gets_one_more_refresh`). A `TWITCH_OAUTH_TOKEN` gets no second chance (`test_a_chat_login_twitch_refuses_writes_token_rejected_and_exits_3`).

## 6. Answers depend on the hub keeping the question's context

- **Where:** `HiveMindTwitchBridge._reply` (`twitch_bridge/__init__.py:383`).
- **Finding:** a `speak` is posted only if its context still carries `twitch_channel` (and, for a threaded reply, `twitch_message_id`). A skill that emits `speak` with a fresh context is dropped silently. Test: `tests/test_bridge.py::test_answers_for_other_bridges_or_channels_are_not_posted`; the real hub keeps the context (`tests/e2e/test_twitch_hivemind_e2e.py`).

## 7. Twitch drops a repeated identical message

- **Where:** Twitch's duplicate rule (`msg_duplicate` notice, logged by id in `TwitchChat._handle_notice`).
- **Finding:** an account that is not a moderator cannot send the same text twice within 30 s in a channel. The `@asker` prefix makes most answers differ, but the same answer to the same person twice in 30 s (for example the fixed fallback line) is dropped by Twitch.

## 8. The fallback reply is English only

- **Where:** `twitch_bridge/__init__.py:381`.
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

## Independent review 2026-11-03 (fixed on `fix/review-2026-11-03`)

Evidence: an adversarial read of `main` at `b400a990` (treated as unreviewed although it had a CodeRabbit pass), a fuzz run of `parse_line` and `TwitchChat._handle` (20 000 random and seeded lines), a two-server reproduction of the token redirect, one failing test written per finding before its fix, then `python -m pytest tests -q --ignore=tests/e2e` 160 passed (133 before; Python 3.12) and `ruff check --isolated --select F,E9 .` clean. Line numbers refer to `499334abe3bd`.

| # | Severity | Where | Finding | Fix and test |
| --- | --- | --- | --- | --- |
| T1 | Medium | `TwitchChat.say` (`twitch_bridge/chat.py:579`, `:603`, `:605`), `irc.defuse_command` (`twitch_bridge/irc.py:173`) | Twitch runs a chat message whose first character is `/` or `.` as a command (`/me`; `/ban`, `.timeout` and the like where they are still taken). Only the first part of a reply started with `@<asker> `: later parts of a long reply started with whatever word fell at the cut, and a reply with no asker (a `speak` whose context lost the user) went out as it came. | Every part starts with `@<asker> ` (`split_message(..., repeat_prefix=True)`, `irc.py:180`); with no asker, a part starting with `/` or `.` gets `COMMAND_GUARD` U+200B (`irc.py:23`). `tests/test_chat.py::test_an_answer_with_nobody_to_address_is_never_a_command`, `::test_no_part_of_a_long_answer_starts_like_a_command`, `tests/test_irc.py::test_defuse_command_guards_only_a_leading_slash_or_dot` |
| T2 | Medium | `handle_chat_message` (`twitch_bridge/__init__.py:329`, `:336`), `_chain_allows` (`:343`) | A reply to the bot is answered, and the bot's answers are Twitch replies addressed to the asker. Two bots doing that (a second HiveMind bridge in the channel, or any bot with `TWITCH_REQUIRE_MENTION=false`) answered each other until a hub quota ran out. Twitch has no bot flag to filter on. | At most `LOOP_MAX_REPLIES = 8` (`:65`) replies to the bot per account and channel in `LOOP_WINDOW = 600` s; logged once (login only); bounded memory. `tests/test_mentions.py::test_two_bots_answering_each_others_replies_stop_after_a_bounded_chain`, `::test_the_reply_chain_memory_is_bounded` |
| T3 | Low | `auth._http` (`twitch_bridge/auth.py:327`) | `urllib.request.urlopen` follows a redirect and copies every header to the new location: `Authorization: OAuth <access token>` went to whatever host a redirect from the id service named (reproduced with two local servers). Needs Twitch or a TLS interception point to redirect; the token must not depend on it. | One opener that never follows a redirect (`_NoRedirects`, `:314`; `_OPENER`, `:324`); the 3xx is retried as an unexpected answer. `tests/test_auth.py::test_a_redirect_never_carries_the_token_to_another_host` (301-308, GET and POST), `::test_a_redirected_refresh_is_retried_not_fatal` |
| T4 | Low | `TwitchChat._session` (`chat.py:394`, `:430`), `_seconds` (`:186`), `_message_id` (`:197`), `format_line` (`irc.py:141`) | Any exception from handling one line ended `chat.run()` and the process: `ROOMSTATE` with `slow=²` (`"²".isdigit()` is True, `float("²")` raises) or a `PING` whose text holds a NUL (found by the fuzz run). Both come from the server, not from chat users. A NUL in a reply id from the hub reached the wire unescaped. | Each line handled on its own, failures logged by command and exception type; slow mode read only from ASCII digits; odd reply ids dropped (the answer is still sent); NUL refused in tag values. `tests/test_chat.py::test_a_line_the_bridge_cannot_act_on_is_skipped_not_fatal`, `::test_an_odd_reply_id_from_the_hub_still_sends_the_answer_without_it`, `tests/test_irc.py::test_a_tag_value_cannot_carry_a_nul` |
| T5 | Low | `TwitchChat._deliver` (`chat.py:522`, `:545`), `HiveMindTwitchBridge.speak`/`_say` (`__init__.py:437`, `:451`) | Two catch-alls would log text: `LOG.exception` prints the exception's text (which can quote the message), and asyncio logs a raising `call_soon_threadsafe` callback with its arguments (the answer). Nothing raises there today. | Exception type only on both paths. `tests/test_logs.py::test_a_failure_while_handling_text_never_puts_the_text_in_a_log` (both leaks reproduced before) |
| T6 | Low | `TwitchChat.run` (`chat.py:298`, `:308`, `:329`) | Every planned reconnect (`RECONNECT`, a new token) was immediate and reset the backoff: a server sending `RECONNECT` after each login got a reconnect-and-login every ~10 ms. | The first is immediate; another after a session shorter than `STABLE_AFTER` waits the normal backoff, growing. `tests/test_chat.py::test_reconnect_requests_right_after_each_login_back_off` |

CodeRabbit follow-ups on PR #2: T2 now counts a reply only once it reached the hub (`_record_reply`; a reply dropped while the hub is down, or refused by `emit`, used to count and could block that account for up to 600 s), test `tests/test_mentions.py::test_a_reply_that_never_reached_the_hub_does_not_count_toward_the_chain`; T4's slow-mode value is capped at Twitch's 120 s (`MAX_SLOW_SECONDS`), as a longer digit string, or one that overflows to inf, held the channel's answers until they aged out, test `tests/test_chat.py::test_slow_mode_is_read_up_to_twitchs_maximum`; the T1 no-asker test now pins the exact guarded text.

CodeRabbit review-body follow-ups (seen after PRs #1 and #2 merged): `split_message` refuses a prefix that leaves no room for a two-unit character (a 499-unit prefix and an emoji gave a prefix-only part and a 501-unit one; `say` never passes such a prefix, a login is at most 25 characters), tests `tests/test_irc.py::test_a_prefix_must_leave_room_for_a_two_unit_character`, `::test_the_longest_allowed_prefix_still_fits_every_part`; T5's test now reads the printed output too (`capfd`): the bridge's logger does not propagate to `caplog`, so the question leak through `LOG.exception` was not asserted before, only the answer leak through asyncio's logger (checked: restoring `LOG.exception` now fails the test); the send-window test measures the limiter's own send times against its exact window; the repeated-prefix split test checks the rebuilt text; the optional live send check (`TWITCH_LIVE_SEND=1`) waits for an anonymous read-only viewer to receive the message (no new credentials; not run here, it needs the bot token).

Checked and fine: the hub outage path (`hub_live` before forwarding, `forward_to_hivemind` catches what `emit` raises and logs the type only); `parse_line` never raised in the fuzz run (malformed tags, missing params, lone backslashes, empty commands); CR/LF/NUL cannot reach the wire from reply text or mentions; whispers are never forwarded; the bot's own lines and other channels are dropped; Twitch messages are at most 500 characters, so inbound size needs no cap; the outgoing queue is bounded (`MAX_PENDING` per configured channel, `MAX_AGE`); the limiter keeps to 18 per 30 s and 1.1 s per channel; TLS is verified for chat (`wss://`) and the id service (urllib's default context), and neither endpoint can be set from the environment; the refresh token and client secret travel only in the POST body and appear in no log line, exception text, argv default or termination message (only `token_rejected`); `/readyz` needs the live hub session and every channel joined; session ids are `twitch-<channel>-<user id>`.

## 12. A refused client id or secret writes no termination message

- **Where:** `HiveMindTwitchBridge._run` (`twitch_bridge/__init__.py`, `except ClientRejected`).
- **Finding:** exit status 4 and nothing in `/dev/termination-log`, by design (the bot's consent is not the problem; FAQ.md). The operator can only tell it from the exit status, and the other bridges' contract names `token_rejected` alone. In the managed deployment the owner supplies the client id and secret themselves (the Twitch entry of the platform's connector catalog, in review in thalovant-api#467), and the operator reports `TokenRejected` only from the termination message (hivemind-k8s-operator#121). A refused client id or secret therefore shows as a bridge that stopped answering, not as credentials to replace; SUGGESTIONS.md has the fix.

## 13. The command guard is not checked against live Twitch

- **Where:** `COMMAND_GUARD` (`twitch_bridge/irc.py:23`).
- **Finding:** the no-asker case relies on Twitch looking at the first character after trimming whitespace, which U+200B is not. Replies with an asker (nearly all) start with a visible `@login`, which needs no such assumption. `TWITCH_LIVE=1` runs could add a check with a bot token.

## 14. A bot that answers with plain mentions is bounded only by the hub

- **Where:** `_chain_allows` (`twitch_bridge/__init__.py:343`).
- **Finding:** T2 counts replies to the bot. A bot that answers with a plain `@hivebot ...` message, not a Twitch reply, is not counted; only the hub's per-person limit slows such a loop. A configurable ignore list of accounts (SUGGESTIONS.md) would cover it.

## 15. Replies queued at shutdown are dropped

- **Where:** `_until_stopped` / `TwitchChat.stop`.
- **Finding:** SIGTERM stops chat at once; answers still in the queue are not sent. Bounded by `MAX_PENDING` and usually empty.

## 16. A token refresh is not rate limited by the bridge

- **Where:** `TwitchAuth._refresh_due_at` (`twitch_bridge/auth.py`).
- **Finding:** the refresh time follows `expires_in` from Twitch. A token issued with a lifetime of seconds would be refreshed (and chat logged in again) about that often; T6 bounds the reconnects, not the refreshes. Twitch issues tokens for about four hours.

## 17. The image keeps the source tree

- **Where:** `Dockerfile` (`COPY . /app`, `pip install .`).
- **Finding:** `/app` (tests and docs included, local secrets excluded by `.dockerignore`) stays in the image, owned by root and read-only to the bridge. Not a secret, only weight; the Matrix fork installs from a copy and removes it.
