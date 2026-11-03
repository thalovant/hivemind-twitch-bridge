Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: CodeRabbit on PR #2: five comments applied (reconnect row, no-asker readme line, exact guard test, forwarded-only chain count, slow-mode cap).

# Maintenance report: hivemind-twitch-bridge


## 2026-11-03: CodeRabbit on pull request #2

- `PRRT_kwDOVg5F8c6zHPUS` QUICK_FACTS Reconnect row: the first planned reconnect after a short session stays immediate; later ones in a row back off.
- `PRRT_kwDOVg5F8c6zHPU3` readme: with no asker, only a part starting with `/` or `.` gets the invisible guard.
- `PRRT_kwDOVg5F8c6zHPVC` `test_an_answer_with_nobody_to_address_is_never_a_command` asserts the exact text, `"\u200b" + answer`.
- `PRRT_kwDOVg5F8c6zHPVP` `handle_chat_message` counts a reply to the bot only after `forward_to_hivemind` succeeded (`_record_reply`; `forward_to_hivemind` returns whether `emit` took it). Test: `tests/test_mentions.py::test_a_reply_that_never_reached_the_hub_does_not_count_toward_the_chain` (hub down, `emit` raising).
- `PRRT_kwDOVg5F8c6zHPVb` `_seconds` caps slow mode at 120 s (`MAX_SLOW_SECONDS`). Test: `tests/test_chat.py::test_slow_mode_is_read_up_to_twitchs_maximum`.
- **Verification:** the two code tests failed before their change (4 cases); `python -m pytest tests -q --ignore=tests/e2e` (ovos-config's file watcher off: no inotify watches left on this host): 167 passed; ruff clean.
- **Transparency report:** AI model Claude Opus 5.5 (`claude-opus-5-5`) in Claude Code; applied the automated review's five comments, code ones with tests; no human review yet.

## 2026-11-03: independent review of main

**Why:** `main` at `b400a990` had a CodeRabbit pass (eleven fixes, entry below) but no independent review. This one looked for the failure classes found in the Matrix and Mattermost forks the same night: hub-outage and emit errors, malformed input crash-loops, replies run as commands or pinging, bot loops, redirects and TLS in the token calls, secret leaks, queue bounds, reconnect storms and readiness. AUDIT.md has the findings (T1-T6) with lines and tests, and the open items (sections 12-17).

**Changes** (branch `fix/review-2026-11-03`, one commit each, oldest first)

1. No reply part can start with `/` or `.`, which Twitch runs as a command: every part starts with `@<asker> `, and with no asker a part starting so gets an invisible U+200B (`irc.defuse_command`).
2. Two bots answering each other's replies stop after 8 in 10 minutes per account and channel (`_chain_allows`, `LOOP_MAX_REPLIES`, `LOOP_WINDOW`).
3. The id-service calls never follow a redirect (`auth._NoRedirects`); urllib copied `Authorization: OAuth <token>` to the redirect's host.
4. A chat line whose handling raises is skipped, not fatal (`slow=²`, NUL in a PING); slow mode read from ASCII digits only; odd reply ids from the hub dropped; NUL refused in tag values.
5. The two catch-alls that could log text (`_deliver`, the `say` callback) log the exception type only.
6. A planned reconnect right after a login backs off instead of looping.
7. Docs: AUDIT (review table, sections 12-17), this entry, QUICK_FACTS, FAQ, readme, AGENTS, SUGGESTIONS.

**Verification**

- Each new test failed before its fix (for T5: both leaks shown in the captured log; for T6: four reconnects ~10 ms apart).
- Fuzz run of `parse_line` and `TwitchChat._handle` with 20 000 random and seeded lines: only the two T4 server lines raised.
- `python -m pytest tests -q --ignore=tests/e2e`: 160 passed (Python 3.12, local; 133 before).
- `ruff check --isolated --select F,E9 .`: clean.
- `python -m pytest tests/e2e -q` (the `e2e` extra; ovos-config's file watcher turned off, as this host has no inotify watches left): 2 passed through a real hivemind-core hub, 3 skipped (opt-in live Twitch).
- Not run locally: the image smoke steps, and a live Twitch check of the U+200B guard (AUDIT section 13).

**Transparency report**

- **AI model:** Claude Opus 5.5 (`claude-opus-5-5`), in Claude Code.
- **Actions taken:** reviewed the chat client, IRC parser, token handling, bridge and CLI; fuzzed the parser and line handler; reproduced the urllib redirect header leak; wrote the failing tests, the fixes and these documents.
- **Oversight:** requested by the session lead as an independent review; the branch is left for a human to push, review and merge.

## 2026-11-03: review round on pull request #1

**Changes** (commit `b50c559` for the code, then the docs commit): the eleven review threads, all valid. Code: a hub refusal before the event loop starts is kept and acted on; `BRIDGE_HEALTH_PORT` (0-65535) and `HIVEMIND_PORT` (1-65535) are range-checked (exit 2); `/validate` refusing a just-issued token raises `TokenRejected`; the bot's own PART rejoins at once; chat lines are delivered only from joined channels; `say()` coerces a reply id of another type and ignores a non-login mention, an unsendable answer is dropped and the send loop survives any error; `format_line` checks tag keys and values. Docs and build: `.env`, `.env.*` and `secrets` excluded from the build context and git; the README's `docker run` passes secrets as files; `docs/setup.md` has its install step back; QUICK_FACTS describes forwarding without `TWITCH_REQUIRE_MENTION`.

**Verification:** `python -m pytest tests -q --ignore=tests/e2e`: 133 passed on Python 3.12.12 and 3.14.8; each of the 8 code fixes reverted alone makes its new test fail; `ruff check --isolated --select F,E9 .` clean; `tests/e2e/test_twitch_hivemind_e2e.py`: 2 passed through a real hub; `TWITCH_LIVE=1 tests/e2e/test_twitch_live.py`: 2 passed, 1 skipped (needs a bot token).

**Transparency report**

- **AI model:** Claude Opus 5.5 (`claude-opus-5-5`), in Claude Code.
- **Actions taken:** read the eleven review threads, checked each against the code, fixed all eleven with tests for the code ones, replied on and resolved each thread.
- **Oversight:** requested by the session lead; findings from an automated reviewer; no human code review yet.

## 2026-11-03: Thalovant fork

**Changes** (each its own commit on `feat/twitch-websocket-chat`, oldest first)

1. `twitch_bridge/chat.py`, `twitch_bridge/irc.py`, `twitch_bridge/errors.py`: a new Twitch chat client, `TwitchChat`, over `wss://irc-ws.chat.twitch.tv:443` with `twitch.tv/tags` and `twitch.tv/commands`, PING/PONG both ways, reconnect with backoff, throttled and retried JOINs, and a rate-limited outgoing queue (18 per 30 s for the account, 1.1 s per channel, slow mode, 500-character split, threaded replies). `Login authentication failed` raises `TokenRejected`.
2. `twitch_bridge/auth.py`: `TwitchAuth` refreshes the bot's token (`TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `TWITCH_REFRESH_TOKEN`) on start and before expiry, validates hourly, keeps tokens in memory only; `TWITCH_OAUTH_TOKEN` stays for short tests.
3. `HiveMindTwitchBridge` replaces `JarbasTwitchBridge` and upstream's `twitch_bridge/twitch.py`: settings from the environment and `*_FILE` secrets, mention gating, per-person sessions, health endpoints, `token_rejected` and exit statuses, hub first with bounded retries, nothing sensitive logged; tests rewritten around local WebSocket and HTTP servers; e2e through a real hub.
4. Dockerfile (uid 10001, `HOME=/home/bridge`, read-only root), `.dockerignore`, `docker-compose.yml`, `.github/workflows/ci.yml` (tests, lint, smoke, e2e, signed publish from `main`); upstream's release, publish and registry workflows and `examples/echobot.py` removed; version 0.1.0.
5. Settings errors exit 2 consistently.
6. A token validation that fails for a network reason at start is tried again after `retry_cap` (5 min), not after an hour (`TwitchAuth._retry_validation_soon`; `tests/test_auth.py::test_a_validation_that_failed_at_start_is_tried_again_soon_not_in_an_hour` fails without it).
7. This docs set (README, `docs/index.md`, `docs/setup.md`, QUICK_FACTS, FAQ, AUDIT, SUGGESTIONS, this report, AGENTS.md fork section); upstream's `docs/configuration.md`, `docs/examples.md` and `docs/operator-setup.md` described the removed CLI and were folded into `readme.md` and `docs/setup.md`.

Repository: `main` created from upstream's `dev` (`dde4b89`) and made the default branch; the `dev` mirror deleted.

**Rationale:** run the bridge as a managed workload under the bridge contract: TCP 443 only, no secret in process arguments or logs, a token that outlives Twitch's few-hour access tokens, a machine-readable stop on refused credentials, and probes a platform can use. Upstream's client could not stay connected (see AUDIT.md, "Fixed").

**Verification**

- Upstream defects reproduced first with a local IRC server against `twitch_bridge/twitch.py` at `dde4b89`: colon truncation, a 1.76 CPU-s/2 s spin with no reconnect after the server closed, CRLF command injection, plaintext port 6667.
- `python -m pytest tests -q --ignore=tests/e2e`: 116 passed on Python 3.12.12 and 3.14.8 (local venvs). `ruff check --isolated --select F,E9 .`: clean.
- `python -m pytest tests/e2e/test_twitch_hivemind_e2e.py`: 2 passed through a real hivemind-core 5.3.10a1 hub (hivescope 0.8.8a1): the full round trip, and a wrong hub password exiting 1 before any Twitch request. Locally only, the machine's inotify watches were exhausted by another process, so watchdog was switched to its polling observer inside the throwaway e2e venv; nothing in the repository changed for it.
- `TWITCH_LIVE=1 python -m pytest tests/e2e/test_twitch_live.py`: 2 passed against the real `wss://irc-ws.chat.twitch.tv:443` (anonymous login joined two channels in 0.5 s and stayed up through keepalive PINGs; a made-up token got `Login authentication failed` and `TokenRejected`), 1 skipped (needs a bot token).
- Mutation check: 21 source mutations (rate limit, PONG, CAP request, CRLF guard, mention gating, sessions, readiness, RECONNECT, keepalive PING, refresh rules, rotation, scheduled refresh, reconnect on a new token, `token_rejected`, hub-first order, quiet websocket logger...), each made at least one test fail.
- Image (podman, `localhost/hivemind-twitch-bridge:check`): `--help` under `--read-only`; `id` is uid 10001 gid 10001; `HOME=/home/bridge`; no settings exits 2 naming `TWITCH_BOT_LOGIN`; no token exits 2 naming `TWITCH_REFRESH_TOKEN`. Then a full run of the image with `--read-only --tmpfs /tmp`, only `/home/bridge/.config/hivemind` mounted writable, secrets as read-only `*_FILE` mounts, a real hub on the host and the fake Twitch servers: `/readyz` 503 then 200, the reply came back threaded, SIGTERM exited 0, no message text or secret in the container log or its arguments, the identity directory held `_identity.json`, `unnamed-node.pem`, `unnamed-node_noise.key`, `unnamed-node_noise_psks.json`; a second run on the same directory with a revoked refresh token exited 3 with `token_rejected` in `/dev/termination-log`, never touched chat, and reused the same Noise key.
- Not verified: the GitHub CI run before this pull request, a real bot token, a real refresh against id.twitch.tv, a real message sent into a channel.

**Transparency report**

- **AI model:** Claude Opus 5.5 (`claude-opus-5-5`), in Claude Code.
- **Actions taken:** reproduced the upstream defects; wrote the chat client, the token upkeep, the bridge, the CLI, the tests and fake servers, the Dockerfile, the CI workflow and these documents; created `main`, set it as default and deleted `dev`; ran the unit, e2e, live and mutation checks and the local image checks.
- **Oversight:** directed by the session lead on the owner's request (Thalovant managed chat bridges, bridge contract, token refresh requirement); no human code review yet.
