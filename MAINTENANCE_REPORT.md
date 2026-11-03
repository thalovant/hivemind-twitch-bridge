Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Thalovant fork set up: WebSocket chat client, token refresh, managed-bridge contract, CI and signed images, docs set.

# Maintenance report: hivemind-twitch-bridge

## 2026-11-03: Thalovant fork

**Changes** (each its own commit on `feat/twitch-websocket-chat`, oldest first)

1. `twitch_bridge/chat.py`, `twitch_bridge/irc.py`, `twitch_bridge/errors.py`: a new Twitch chat client, `TwitchChat`, over `wss://irc-ws.chat.twitch.tv:443` with `twitch.tv/tags` and `twitch.tv/commands`, PING/PONG both ways, reconnect with backoff, throttled and retried JOINs, and a rate-limited outgoing queue (18 per 30 s for the account, 1.1 s per channel, slow mode, 500-character split, threaded replies). `Login authentication failed` raises `TokenRejected`.
2. `twitch_bridge/auth.py`: `TwitchAuth` refreshes the bot's token (`TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `TWITCH_REFRESH_TOKEN`) on start and before expiry, validates hourly, keeps tokens in memory only; `TWITCH_OAUTH_TOKEN` stays for short tests.
3. `HiveMindTwitchBridge` replaces `JarbasTwitchBridge` and upstream's `twitch_bridge/twitch.py`: settings from the environment and `*_FILE` secrets, mention gating, per-person sessions, health endpoints, `token_rejected` and exit statuses, hub first with bounded retries, nothing sensitive logged; tests rewritten around local WebSocket and HTTP servers; e2e through a real hub.
4. Dockerfile (uid 10001, `HOME=/home/bridge`, read-only root), `.dockerignore`, `docker-compose.yml`, `.github/workflows/ci.yml` (tests, lint, smoke, e2e, signed publish from `main`); upstream's release, publish and registry workflows and `examples/echobot.py` removed; version 0.1.0.
5. Settings errors exit 2 consistently.
6. This docs set (README, `docs/index.md`, `docs/setup.md`, QUICK_FACTS, FAQ, AUDIT, SUGGESTIONS, this report, AGENTS.md fork section); upstream's `docs/configuration.md`, `docs/examples.md` and `docs/operator-setup.md` described the removed CLI and were folded into `readme.md` and `docs/setup.md`.

Repository: `main` created from upstream's `dev` (`dde4b89`) and made the default branch; the `dev` mirror deleted.

**Rationale:** run the bridge as a managed workload under the bridge contract: TCP 443 only, no secret in process arguments or logs, a token that outlives Twitch's few-hour access tokens, a machine-readable stop on refused credentials, and probes a platform can use. Upstream's client could not stay connected (see AUDIT.md, "Fixed").

**Verification**

- Upstream defects reproduced first with a local IRC server against `twitch_bridge/twitch.py` at `dde4b89`: colon truncation, a 1.76 CPU-s/2 s spin with no reconnect after the server closed, CRLF command injection, plaintext port 6667.
- `python -m pytest tests -q --ignore=tests/e2e`: 115 passed on Python 3.12.12 and 3.14.8 (local venvs). `ruff check --isolated --select F,E9 .`: clean.
- `python -m pytest tests/e2e/test_twitch_hivemind_e2e.py`: 2 passed through a real hivemind-core 5.3.10a1 hub (hivescope 0.8.8a1): the full round trip, and a wrong hub password exiting 1 before any Twitch request. Locally only, the machine's inotify watches were exhausted by another process, so watchdog was switched to its polling observer inside the throwaway e2e venv; nothing in the repository changed for it.
- `TWITCH_LIVE=1 python -m pytest tests/e2e/test_twitch_live.py`: 2 passed against the real `wss://irc-ws.chat.twitch.tv:443` (anonymous login joined two channels in 0.5 s and stayed up through keepalive PINGs; a made-up token got `Login authentication failed` and `TokenRejected`), 1 skipped (needs a bot token).
- Mutation check: 21 source mutations (rate limit, PONG, CAP request, CRLF guard, mention gating, sessions, readiness, RECONNECT, keepalive PING, refresh rules, rotation, scheduled refresh, reconnect on a new token, `token_rejected`, hub-first order, quiet websocket logger...), each made at least one test fail.
- Image (podman, `localhost/hivemind-twitch-bridge:check`): `--help` under `--read-only`; `id` is uid 10001 gid 10001; `HOME=/home/bridge`; no settings exits 2 naming `TWITCH_BOT_LOGIN`; no token exits 2 naming `TWITCH_REFRESH_TOKEN`. Then a full run of the image with `--read-only --tmpfs /tmp`, only `/home/bridge/.config/hivemind` mounted writable, secrets as read-only `*_FILE` mounts, a real hub on the host and the fake Twitch servers: `/readyz` 503 then 200, the reply came back threaded, SIGTERM exited 0, no message text or secret in the container log or its arguments, the identity directory held `_identity.json`, `unnamed-node.pem`, `unnamed-node_noise.key`, `unnamed-node_noise_psks.json`; a second run on the same directory with a revoked refresh token exited 3 with `token_rejected` in `/dev/termination-log`, never touched chat, and reused the same Noise key.
- Not verified: the GitHub CI run before this pull request, a real bot token, a real refresh against id.twitch.tv, a real message sent into a channel.

**Transparency report**

- **AI model:** Claude Opus 5.5 (`claude-opus-5-5`), in Claude Code.
- **Actions taken:** reproduced the upstream defects; wrote the chat client, the token upkeep, the bridge, the CLI, the tests and fake servers, the Dockerfile, the CI workflow and these documents; created `main`, set it as default and deleted `dev`; ran the unit, e2e, live and mutation checks and the local image checks.
- **Oversight:** directed by the session lead on the owner's request (Thalovant managed chat bridges, bridge contract, token refresh requirement); no human code review yet.
