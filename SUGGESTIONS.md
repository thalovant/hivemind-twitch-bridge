Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: proposals after the Thalovant fork set-up.

# Suggestions: hivemind-twitch-bridge

## Store a rotated refresh token back

- **Problem:** AUDIT.md section 4: a refresh token Twitch rotates is kept in memory only.
- **Proposal:** let the platform pass a write-back target (a Secret the pod may patch, or a callback endpoint) and have `TwitchAuth._refresh_once` hand the new refresh token to it.
- **Impact:** medium; removes the dependency on Twitch keeping old refresh tokens valid.

## Fewer refreshes across restarts

- **Problem:** AUDIT.md section 3: every start mints an access token, against Twitch's 50-per-refresh-token cap.
- **Proposal:** accept the current access token alongside the refresh token and refresh at start only when `/oauth2/validate` says it has less than the margin left; or cache it in the identity emptyDir, which survives container restarts within a pod.
- **Impact:** medium for crash-looping pods; small change in `TwitchAuth.start`.

## Per-person limit on incoming questions

- **Problem:** AUDIT.md section 2.
- **Proposal:** a token bucket per (channel, user id) in `HiveMindTwitchBridge.handle_chat_message`, silent when exceeded (an answer would itself cost a message).
- **Impact:** high for busy channels; small change.

## Moderator-aware send limits

- **Opportunity:** Twitch allows 100 messages per 30 s in channels where the bot is the broadcaster, a moderator or a VIP, and no per-channel 1 s rule there.
- **Proposal:** read `USERSTATE` badges per channel in `TwitchChat._handle` and give `SendLimiter` a per-channel allowance; keep the account-wide 18 per 30 s for the rest.
- **Impact:** low to medium; only matters for chatty channels.

## A not-ready reason on `/readyz`

- **Problem:** AUDIT.md section 1: the operator has to read logs to know which channel is missing.
- **Proposal:** answer `/readyz` with a short machine-readable reason (`hub_down`, `not_logged_in`, `not_joined: name`) in the body; keep the status codes.
- **Impact:** medium for support; no secrets or chat text involved.

## EventSub chat instead of IRC

- **Opportunity:** Twitch's rate-limit documentation says its IRC limits do not apply to a bot that reads chat through the EventSub `channel.chat.message` subscription with an app access token (and sends with the Send Chat Message API).
- **Proposal:** a second `TwitchChat` implementation behind the same interface (`ready`, `say`, `run`, `on_message`).
- **Impact:** high in the long run; large change, needs a WebSocket EventSub session and Helix calls (both on TCP 443).

## Localised fallback line

- **Problem:** AUDIT.md section 8.
- **Proposal:** a small per-language table keyed by `HIVEMIND_LANG`, or let the hub supply the line.
- **Impact:** low.

## Reproducible images and a required e2e job

- **Problem:** AUDIT.md section 11; the `e2e` CI job does not gate publishing.
- **Proposal:** pin the base image by digest and build from a constraints file generated in CI (Renovate updates both); once the e2e job has proven stable, add it to the `image` job's `needs`.
- **Impact:** medium; makes a rollback mean something and keeps hub interop from regressing silently.
