Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Thalovant fork: WebSocket chat client, token refresh, settings from the environment.

# FAQ: hivemind-twitch-bridge

## Why does the bot ignore most chat messages?

`HiveMindTwitchBridge.addressed_text` forwards only messages that mention `@botlogin` or reply to one of the bot's messages (Twitch's `reply-parent-user-login` tag), and removes the mention first. `TWITCH_REQUIRE_MENTION=false` forwards every message in the configured channels.

## Does the bot answer whispers?

No. `TwitchChat._handle` ignores `WHISPER`, whatever the settings. The bridge only talks in the channels in `TWITCH_ALLOWED_CHANNELS`, so there is no direct-message policy to set.

## Which channels does it join?

Exactly those in `TWITCH_ALLOWED_CHANNELS` (case and a leading `#` do not matter, at most 100). It answers only there, and `HiveMindTwitchBridge._reply` refuses to post anywhere else even if the hub names another channel.

## Why is `/readyz` 503 although the bot is in chat?

`/readyz` needs the hub session live and every listed channel joined (`HiveMindTwitchBridge.ready`). Twitch silently drops a JOIN to a channel that does not exist, so a misspelt or renamed channel keeps the bridge not ready; the log says `Twitch has not confirmed #name; joining again` every minute.

## How does the bridge get a token, and what happens when it expires?

With `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` and `TWITCH_REFRESH_TOKEN`, `TwitchAuth` trades the refresh token for an access token on start, validates it hourly and refreshes it 10 minutes before it expires, then logs chat in again with the new token. A new refresh token Twitch hands out is kept in memory only; a restart starts again from the stored one, which stays valid for a confidential client unless revoked. `TWITCH_OAUTH_TOKEN` is for short tests: it is never refreshed.

## What does `token_rejected` mean?

Twitch refused the bot's credentials and no retry can fix it: `Login authentication failed` from chat (after one extra refresh when a refresh token is set), an invalid refresh grant, a token for another account than `TWITCH_BOT_LOGIN`, or a token without `chat:read` and `chat:edit`. The bridge writes `token_rejected` to `/dev/termination-log` and exits 3. A refused client id or secret exits 4 and writes nothing: the bot's consent is not the problem.

## Why does the bridge need only TCP 443?

Chat goes over `wss://irc-ws.chat.twitch.tv:443` and tokens over `https://id.twitch.tv`. Upstream's client used plaintext IRC on TCP 6667, which sent the token in the clear.

## How fast can the bot answer?

At most 18 messages in any 30 seconds for the whole account and one message per 1.1 s in a channel (`SendLimiter`), longer in slow mode. Twitch's own limit for an account that is not a moderator is 20 per 30 s per user; going over gets the bot ignored for an hour. Replies longer than 500 characters are split (`twitch_bridge.irc.split_message`).

## Do people in one channel share a conversation?

Not by default: `TWITCH_SESSION_SCOPE=person` gives each person their own hub session in each channel (`twitch-<channel>-<user id>`). `chat` shares one per channel. The hub keeps the id scoped to the bridge's client (`<client>:twitch-...`).

## Why does a reply start with `@name`?

The reply is threaded under the question (`reply-parent-msg-id`), and starts with `@asker` as a Twitch reply written by a person does, so it is clear on stream overlays that do not show threads. The rest is the hub's text, with control characters turned into spaces.

## Where is the Noise key?

`/home/bridge/.config/hivemind/unnamed-node_noise.key`, the hivemind-bus-client default for an unnamed node; the identity (`_identity.json`) sits next to it. It is the only directory the image writes besides `/tmp`.

## How do I test without a Twitch account?

`python -m pytest tests -q --ignore=tests/e2e` runs everything against local servers (`tests/fake_twitch.py`). `TWITCH_LIVE=1 pytest tests/e2e/test_twitch_live.py` logs in anonymously to the real Twitch chat and checks a made-up token is refused.

## Why is the default branch `main` and not `dev`?

This is the Thalovant fork. Upstream's release workflows were removed, so nothing here publishes to PyPI or to upstream's registry; images go to `ghcr.io/thalovant/hivemind-twitch-bridge`.
