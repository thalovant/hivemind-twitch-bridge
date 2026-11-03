"""The bot's Twitch user access token: validated hourly, refreshed before it expires.

Two ways to give the bridge a token:

- **refresh** (``client_id`` + ``client_secret`` + ``refresh_token``): on
  start the bridge trades the refresh token for an access token at
  ``https://id.twitch.tv/oauth2/token`` and refreshes again before that one
  expires (``expires_in`` from ``/oauth2/validate``) or as soon as Twitch
  calls it invalid. Both the access token and a rotated refresh token are
  kept in memory only; a restart starts again from the refresh token it was
  given, which stays valid for a confidential client unless it is revoked.
- **static** (``access_token``, or ``access_token_file`` read on every
  login): for short tests. Nothing refreshes it; once Twitch calls it
  invalid the bridge stops.

Twitch asks every app to validate its token on start and hourly. A token
that Twitch refuses, that belongs to another account than the bot, or that
lacks ``chat:read``/``chat:edit`` raises
:class:`~twitch_bridge.errors.TokenRejected`; a refused client id or secret
raises :class:`~twitch_bridge.errors.ClientRejected`. Network trouble and
Twitch errors (5xx, 429) are retried with backoff and never stop the bridge.
No token or secret is ever logged.
"""
import asyncio
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from ovos_utils.log import LOG

from twitch_bridge.errors import ClientRejected, TokenRejected

TWITCH_ID_URL = "https://id.twitch.tv/oauth2"
#: What a chat bot needs: read chat, and write to it.
REQUIRED_SCOPES = ("chat:read", "chat:edit")
#: Twitch: validate on start and hourly.
VALIDATE_EVERY = 3600.0
#: Refresh this long before the access token expires (or at half its life
#: when it is shorter than twice this).
REFRESH_MARGIN = 600.0
#: Retry backoff for network trouble and Twitch errors.
RETRY_BASE = 5.0
RETRY_CAP = 300.0
HTTP_TIMEOUT = 10.0


class _Transient(Exception):
    """A failure worth retrying: network, timeout, 5xx, 429."""


@dataclass
class Validation:
    """What ``/oauth2/validate`` says about a token."""

    login: Optional[str]
    user_id: Optional[str]
    client_id: Optional[str]
    scopes: List[str] = field(default_factory=list)
    expires_in: Optional[int] = None


def _read_token_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError as error:
        raise TokenRejected(f"cannot read the token file {path}: {error.strerror}") from None


def _strip_oauth_prefix(token: str) -> str:
    token = (token or "").strip()
    return token[len("oauth:"):] if token.lower().startswith("oauth:") else token


class TwitchAuth:
    """Holds the bot's access token and keeps it valid."""

    def __init__(self, bot_login: str,
                 access_token: Optional[str] = None,
                 access_token_file: Optional[str] = None,
                 client_id: Optional[str] = None,
                 client_secret: Optional[str] = None,
                 refresh_token: Optional[str] = None,
                 base_url: str = TWITCH_ID_URL,
                 *,
                 validate_every: float = VALIDATE_EVERY,
                 refresh_margin: float = REFRESH_MARGIN,
                 retry_base: float = RETRY_BASE,
                 retry_cap: float = RETRY_CAP,
                 on_new_token: Optional[Callable[[], None]] = None):
        self.bot_login = bot_login.lower()
        self.client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token = _strip_oauth_prefix(access_token) if access_token else None
        self._access_token_file = access_token_file
        self.base_url = base_url.rstrip("/")
        self.validate_every = validate_every
        self.refresh_margin = refresh_margin
        self.retry_base = retry_base
        self.retry_cap = retry_cap
        self.on_new_token = on_new_token
        self.refreshes = 0
        self._expires_at: Optional[float] = None
        self._validated_at: Optional[float] = None
        self._told_about_rotation = False
        if self.can_refresh:
            if access_token or access_token_file:
                LOG.warning("a refresh token is configured, so TWITCH_OAUTH_TOKEN is not used")
            self._access_token = None
            self._access_token_file = None
        elif not (self._access_token or access_token_file):
            raise ValueError("a Twitch token is required: a refresh token with the "
                             "client id and secret, or an access token")

    @property
    def can_refresh(self) -> bool:
        return bool(self.client_id and self._client_secret and self._refresh_token)

    def token(self) -> str:
        """The access token to log in with (a static token file is read every time)."""
        if self._access_token_file:
            return _strip_oauth_prefix(_read_token_file(self._access_token_file))
        if not self._access_token:
            raise TokenRejected("no Twitch access token yet")
        return self._access_token

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Have a usable access token before chat logs in."""
        if self.can_refresh:
            await self._refresh_with_retry()
            return
        try:
            await self._validate_current()
        except _Transient as error:
            LOG.warning(f"could not validate the Twitch token ({error}); "
                        f"trying chat with it anyway")
            self._retry_validation_soon()

    async def run(self) -> None:
        """Validate hourly and refresh before expiry, until cancelled."""
        while True:
            now = time.monotonic()
            next_validate = (self._validated_at or now) + self.validate_every
            due = next_validate
            refresh_due = self._refresh_due_at()
            if refresh_due is not None:
                due = min(due, refresh_due)
            await asyncio.sleep(max(0.0, due - now))
            refresh_due = self._refresh_due_at()
            if refresh_due is not None and time.monotonic() >= refresh_due:
                await self._refresh_with_retry()
                self._announce_new_token()
                continue
            try:
                await self._validate_current()
            except _Transient as error:
                LOG.warning(f"could not validate the Twitch token ({error}); "
                            f"trying again in {self.retry_cap:.0f} s")
                self._retry_validation_soon()

    def _retry_validation_soon(self) -> None:
        """Schedule the next validation ``retry_cap`` seconds from now, not an hour."""
        self._validated_at = time.monotonic() - self.validate_every + self.retry_cap

    async def refresh(self) -> None:
        """Refresh now (chat refused the token). Raises TokenRejected without a refresh token."""
        if not self.can_refresh:
            raise TokenRejected("Twitch refused the access token and no refresh token is set")
        await self._refresh_with_retry()

    def _announce_new_token(self) -> None:
        if self.on_new_token is not None:
            try:
                self.on_new_token()
            except Exception:
                LOG.exception("new-token callback failed")

    def _refresh_due_at(self) -> Optional[float]:
        if not self.can_refresh or self._expires_at is None:
            return None
        lifetime_left = self._expires_at - (self._validated_at or time.monotonic())
        margin = min(self.refresh_margin, max(lifetime_left, 0.0) / 2)
        return self._expires_at - margin

    # ------------------------------------------------------------------
    # validate
    # ------------------------------------------------------------------
    async def _validate_current(self) -> None:
        """Validate the token in use; refresh (or stop) when Twitch refuses it."""
        status, body = await asyncio.to_thread(
            _http, "GET", f"{self.base_url}/validate",
            headers={"Authorization": f"OAuth {self.token()}"})
        if status == 401:
            if not self.can_refresh:
                raise TokenRejected("Twitch says the access token is invalid or expired")
            LOG.warning("Twitch says the access token is no longer valid; refreshing it")
            await self._refresh_with_retry()
            self._announce_new_token()
            return
        if status != 200:
            raise _Transient(f"validate answered HTTP {status}")
        self._accept_validation(_validation(body))

    def _accept_validation(self, validation: Validation) -> None:
        if (validation.login or "").lower() != self.bot_login:
            raise TokenRejected(f"the token belongs to {validation.login or 'no user'}, "
                                f"not to the bot account {self.bot_login}")
        missing = [scope for scope in REQUIRED_SCOPES if scope not in validation.scopes]
        if missing:
            raise TokenRejected(f"the token lacks the {' and '.join(missing)} scope")
        now = time.monotonic()
        self._validated_at = now
        if validation.expires_in:
            self._expires_at = now + validation.expires_in
            if not self.can_refresh and validation.expires_in < 2 * self.validate_every:
                LOG.warning(f"the Twitch token expires in {validation.expires_in} s and no "
                            f"refresh token is configured")
        else:
            self._expires_at = None
        LOG.info(f"Twitch token valid for {validation.login}"
                 + (f", expires in {validation.expires_in} s" if validation.expires_in else ""))

    # ------------------------------------------------------------------
    # refresh
    # ------------------------------------------------------------------
    async def _refresh_with_retry(self) -> None:
        attempt = 0
        while True:
            try:
                await self._refresh_once()
                return
            except _Transient as error:
                delay = min(self.retry_cap, self.retry_base * (2 ** min(attempt, 16)))
                delay *= random.uniform(0.5, 1.0)
                attempt += 1
                LOG.warning(f"Twitch token refresh failed ({error}); retrying in {delay:.0f} s")
                await asyncio.sleep(delay)

    async def _refresh_once(self) -> None:
        form = {"client_id": self.client_id, "client_secret": self._client_secret,
                "grant_type": "refresh_token", "refresh_token": self._refresh_token}
        status, body = await asyncio.to_thread(
            _http, "POST", f"{self.base_url}/token", form=form)
        message = str(body.get("message") or body.get("error") or "").strip()
        if status == 200 and body.get("access_token"):
            self._access_token = _strip_oauth_prefix(str(body["access_token"]))
            rotated = body.get("refresh_token")
            if rotated and rotated != self._refresh_token:
                self._refresh_token = str(rotated)
                if not self._told_about_rotation:
                    self._told_about_rotation = True
                    LOG.info("Twitch issued a new refresh token; it is kept in memory only. "
                             "A restart uses the stored refresh token again, which stays "
                             "valid for a confidential client unless it is revoked.")
            self.refreshes += 1
            expires_in = _int(body.get("expires_in"))
            self._validated_at = time.monotonic()
            self._expires_at = self._validated_at + expires_in if expires_in else None
            LOG.info("refreshed the Twitch access token"
                     + (f"; it expires in {expires_in} s" if expires_in else ""))
            await self._validate_fresh()
            return
        lowered = message.lower()
        if status == 401 or (status == 400 and "refresh token" in lowered):
            raise TokenRejected(f"Twitch refused the refresh token (HTTP {status}: {message or 'no reason'})")
        if status in (400, 403) and "client" in lowered:
            raise ClientRejected(f"Twitch refused the client id or secret (HTTP {status}: {message})")
        raise _Transient(f"HTTP {status}{': ' + message if message else ''}")

    async def _validate_fresh(self) -> None:
        """Check a just-refreshed token belongs to the bot and can chat."""
        try:
            status, body = await asyncio.to_thread(
                _http, "GET", f"{self.base_url}/validate",
                headers={"Authorization": f"OAuth {self._access_token}"})
        except _Transient as error:
            LOG.warning(f"could not validate the refreshed token ({error})")
            return
        if status == 200:
            self._accept_validation(_validation(body))
        elif status == 401:
            raise TokenRejected("Twitch says the access token it just issued is invalid")
        else:
            LOG.warning(f"validating the refreshed token answered HTTP {status}")


# ----------------------------------------------------------------------
# HTTP (stdlib, run in a worker thread)
# ----------------------------------------------------------------------
def _int(value) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _validation(body: dict) -> Validation:
    scopes = body.get("scopes") or []
    return Validation(login=body.get("login"), user_id=body.get("user_id"),
                      client_id=body.get("client_id"),
                      scopes=[str(scope) for scope in scopes] if isinstance(scopes, list) else [],
                      expires_in=_int(body.get("expires_in")))


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Hand a 3xx back as it is. urllib would follow it and copy every header,
    ``Authorization: OAuth <token>`` included, to whatever host it names."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises HTTPError with the 3xx status


#: The opener for every call to Twitch's id service: certificates verified
#: (urllib's default HTTPS context), redirects never followed.
_OPENER = urllib.request.build_opener(_NoRedirects)


def _http(method: str, url: str, headers: Optional[dict] = None,
          form: Optional[dict] = None) -> Tuple[int, dict]:
    """One request; ``(status, json body or {})``. Raises ``_Transient`` for
    network trouble and for answers worth retrying (5xx, 429). A redirect is
    not followed: its 3xx status comes back like any other, and is retried
    by the caller as an unexpected answer."""
    data = urllib.parse.urlencode(form).encode("ascii") if form is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
    if data is not None:
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with _OPENER.open(request, timeout=HTTP_TIMEOUT) as answer:
            status, raw = answer.status, answer.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        reason = getattr(error, "reason", error)
        raise _Transient(f"{type(error).__name__}: {reason}") from None
    if status >= 500 or status == 429:
        raise _Transient(f"HTTP {status}")
    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError):
        body = {}
    return status, body if isinstance(body, dict) else {}
