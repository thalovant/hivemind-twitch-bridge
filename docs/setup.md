Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: A refused client id or secret writes token_rejected and exits 3, like a refused token (AUDIT section 12 fixed).

# Setup: from nothing to a working Twitch bot

This takes a Twitch account for the bot, a HiveMind hub and a machine (or container) that can reach both over TCP 443 (Twitch) and the hub's port.

## 1. The bot's Twitch account

Create (or pick) the Twitch account the bot will chat as. Its **login** (the lowercase name in `twitch.tv/<login>`) is `TWITCH_BOT_LOGIN`. Some channels only let accounts with a verified phone number chat; Twitch then answers with the notice `msg_requires_verified_phone_number`, which the bridge logs by its id.

## 2. A Twitch application

At <https://dev.twitch.tv/console/apps>, register an application:

- **OAuth Redirect URL**: where your consent flow lands, for example `http://localhost:3000` for a one-off manual flow;
- **Client Type**: **Confidential**. A confidential client's refresh token does not expire; a public client's expires after 30 days.

Note the **Client ID** and create a **Client Secret**: they are `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET`.

## 3. The bot's consent (refresh token)

Signed in to Twitch **as the bot account**, open:

```
https://id.twitch.tv/oauth2/authorize?response_type=code&client_id=<CLIENT_ID>&redirect_uri=<REDIRECT_URL>&scope=chat%3Aread+chat%3Aedit
```

Approve, and copy the `code` from the address you are sent to. Exchange it:

```bash
curl -X POST https://id.twitch.tv/oauth2/token \
  -d client_id=<CLIENT_ID> -d client_secret=<CLIENT_SECRET> \
  -d code=<CODE> -d grant_type=authorization_code -d redirect_uri=<REDIRECT_URL>
```

The answer holds `access_token` and `refresh_token`. Keep the **refresh token** (`TWITCH_REFRESH_TOKEN`); the bridge gets its own access tokens from it on every start and before each one expires. The bridge keeps the access token, and any new refresh token Twitch hands out, in memory only: a restart starts again from the refresh token you stored.

For a quick test you can skip the application and pass any user access token with `chat:read` and `chat:edit` as `TWITCH_OAUTH_TOKEN`. It is not refreshed.

## 4. Register the bridge on the hub

```bash
hivemind-core add-client
hivemind-core allow-msg recognizer_loop:utterance <client_id>
hivemind-core allow-msg speak <client_id>
```

The access key and password are `HIVEMIND_ACCESS_KEY` and `HIVEMIND_PASSWORD`. Use a long random password: the hub's password handshake can refuse one that is easy to guess (`WeakPasswordError` in the hub's log), and the bridge then sees only closed connections until its handshake retries run out.

## 5. Run it

Install the package once, from a clone of the repository (or use the image, see the README):

```bash
git clone https://github.com/thalovant/hivemind-twitch-bridge.git
cd hivemind-twitch-bridge
pip install .
```

Then:

```bash
export TWITCH_BOT_LOGIN=mybot
export TWITCH_ALLOWED_CHANNELS=mychannel
export TWITCH_CLIENT_ID=... TWITCH_CLIENT_SECRET=... TWITCH_REFRESH_TOKEN=...
export HIVEMIND_ACCESS_KEY=... HIVEMIND_PASSWORD=... HIVEMIND_HOST=ws://hub.local
export BRIDGE_HEALTH_PORT=8080
hivemind-twitch-bridge
```

The log shows, in order: the hub handshake (`== connected to HiveMind`), the token (`refreshed the Twitch access token`, `Twitch token valid for mybot`), the chat login (`== logged in to Twitch chat as mybot`) and one `== joined #channel` per channel. `curl localhost:8080/readyz` answers `ready` from then on.

## 6. Check the round trip

In one of the channels, write `@mybot what time is it`. The hub's logs show the `recognizer_loop:utterance`; the bot answers as a reply to your message, starting with `@you`.

## Troubleshooting

- **`/readyz` stays 503 and the log repeats "Twitch has not confirmed #name"**: the channel login is wrong, renamed or suspended (Twitch silently drops a JOIN to a channel that does not exist).
- **Exit status 3, `token_rejected` in `/dev/termination-log`**: Twitch refused a credential. The log says which. "Twitch refused the bot's credentials": the refresh token was revoked (password change, application disconnected), the token belongs to another account than `TWITCH_BOT_LOGIN`, or it lacks `chat:read`/`chat:edit`; get a new consent (step 3). "Twitch refused the client id or secret": check both against the application's page in the Twitch developer console.
- **Exit status 1 right away**: the hub is unreachable or refused the access key and password; the bridge gives up after `DEFAULT_HANDSHAKE_MAX_RETRIES` (10) handshake attempts.
- **The bot is connected but never answers**: the message must mention `@mybot` (or reply to the bot); whispers are never answered; check `allow-msg` on the hub.
- **"HiveMind refused this identity" after the hub was reinstalled**: the bridge pins the hub's Noise key; reset the pin (`hivemind-client reset-noise-pin`) or start with a fresh `/home/bridge/.config/hivemind`.
