FROM python:3.14-slim

WORKDIR /app
COPY . /app

# force the current hivemind-bus-client alpha rather than whatever a stale
# base layer might already have cached: the bridge relies on its bounded
# handshake and its refusal of rejected credentials
RUN pip install --no-cache-dir --upgrade "hivemind-bus-client>=1.0.16a1" \
    && pip install --no-cache-dir .

# The bridge reads its settings from the environment (TWITCH_BOT_LOGIN,
# TWITCH_ALLOWED_CHANNELS, TWITCH_CLIENT_ID, TWITCH_REFRESH_TOKEN,
# HIVEMIND_ACCESS_KEY, HIVEMIND_PASSWORD, HIVEMIND_HOST, ...), or a secret
# from the file named by <VARIABLE>_FILE, so no secret is ever passed on the
# command line where other processes can read it. Nothing secret is baked
# into the image.
ENV HOME=/home/bridge \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HIVEMIND_HOST=ws://127.0.0.1 \
    HIVEMIND_PORT=5678 \
    HIVEMIND_SITE_ID=twitch \
    HIVEMIND_LANG=en-us

# An unprivileged user. ~/.config/hivemind holds the HiveMind identity and
# the Noise static key (unnamed-node_noise.key); it is the only path the
# bridge writes besides /tmp, so the root filesystem can be read-only with an
# emptyDir (or a volume, to keep the identity across pod restarts) mounted there.
RUN useradd --create-home --uid 10001 bridge \
    && mkdir -p /home/bridge/.config/hivemind \
    && chown -R bridge:bridge /home/bridge
USER bridge

ENTRYPOINT ["hivemind-twitch-bridge"]
