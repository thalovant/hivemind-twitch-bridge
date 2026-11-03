"""The bot's token, refreshed and validated against a local Twitch ID server.

``FakeTwitchId`` (``tests/fake_twitch.py``) is a real HTTP server with
Twitch's ``/oauth2/token`` and ``/oauth2/validate`` behaviour, so every
request here goes over a socket the way it does against id.twitch.tv.
"""
import asyncio
import time
from unittest.mock import MagicMock

import pytest

import twitch_bridge.auth as auth_module
from fake_twitch import FakeTwitchId
from twitch_bridge.auth import TwitchAuth
from twitch_bridge.errors import ClientRejected, TokenRejected


def refreshing(server, **options):
    options.setdefault("retry_base", 0.05)
    options.setdefault("retry_cap", 0.2)
    return TwitchAuth("hivebot", client_id="cid", client_secret="secret",
                      refresh_token="refresh-0", base_url=server.url, **options)


def run(coroutine, timeout=10):
    return asyncio.run(asyncio.wait_for(coroutine, timeout))


def test_start_trades_the_refresh_token_for_an_access_token_and_validates_it():
    with FakeTwitchId() as server:
        auth = refreshing(server)
        run(auth.start())
        assert auth.token() == "access-1"
        assert server.posts() == [{"client_id": "cid", "client_secret": "secret",
                                   "grant_type": "refresh_token", "refresh_token": "refresh-0"}]
        assert server.validations() == ["OAuth access-1"]
        assert server.requests[0][1] == "/oauth2/token"
        assert server.requests[1][1] == "/oauth2/validate"


def test_the_rotated_refresh_token_is_used_next_and_only_kept_in_memory(monkeypatch, tmp_path):
    lines = []
    log = MagicMock()
    for level in ("debug", "info", "warning", "error", "exception"):
        getattr(log, level).side_effect = lambda *a, **k: lines.append(" ".join(map(str, a)))
    monkeypatch.setattr(auth_module, "LOG", log)
    monkeypatch.chdir(tmp_path)
    with FakeTwitchId() as server:
        auth = refreshing(server)
        run(auth.start())
        run(auth.refresh())
        assert [form["refresh_token"] for form in server.posts()] == ["refresh-0", "refresh-1"]
        assert auth.token() == "access-2"
    said = "\n".join(lines)
    assert "kept in memory only" in said and "restart uses the stored refresh token" in said
    for secret in ("refresh-0", "refresh-1", "refresh-2", "access-1", "access-2", "secret"):
        assert secret not in said
    assert list(tmp_path.iterdir()) == [], "a token was written to disk"


@pytest.mark.parametrize("status,body", [
    (400, {"error": "Bad Request", "status": 400, "message": "Invalid refresh token"}),
    (401, {"status": 401, "message": "invalid refresh token"}),
])
def test_an_invalid_grant_is_token_rejected(status, body):
    with FakeTwitchId() as server:
        server.token_answers.append((status, body))
        auth = refreshing(server)
        with pytest.raises(TokenRejected, match="refused the refresh token"):
            run(auth.start())
        assert len(server.posts()) == 1, "an invalid grant must not be retried"


def test_a_refused_client_secret_is_client_rejected_not_token_rejected():
    with FakeTwitchId(client_secret="the-real-one") as server:
        with pytest.raises(ClientRejected):
            run(refreshing(server).start())


def test_twitch_errors_and_unknown_answers_are_retried_not_fatal():
    with FakeTwitchId() as server:
        server.token_answers += [(503, {"message": "unavailable"}), (429, {}),
                                 (400, {"message": "something else"})]
        auth = refreshing(server)
        run(auth.start())
        assert auth.token() == "access-1"
        assert len(server.posts()) == 4


def test_network_trouble_is_retried():
    auth = TwitchAuth("hivebot", client_id="cid", client_secret="secret",
                      refresh_token="refresh-0", base_url="http://127.0.0.1:9/oauth2",
                      retry_base=0.05, retry_cap=0.1)

    async def attempt():
        await asyncio.wait_for(auth.start(), 0.5)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(attempt())


def test_the_token_is_refreshed_before_it_expires_and_chat_is_told():
    with FakeTwitchId(expires_in=2) as server:
        told = []
        auth = refreshing(server, refresh_margin=1.0, on_new_token=lambda: told.append(time.monotonic()))

        async def scenario():
            began = time.monotonic()
            await auth.start()
            keeper = asyncio.create_task(auth.run())
            while not told:
                await asyncio.sleep(0.02)
            keeper.cancel()
            return began

        began = run(scenario())
        assert auth.token() == "access-2"
        assert len(told) == 1
        # expires after 2 s: refreshed at half its life, well before expiry
        assert 0.8 <= told[0] - began <= 1.9
        assert [form["refresh_token"] for form in server.posts()] == ["refresh-0", "refresh-1"]


def test_validation_is_repeated_and_a_401_triggers_a_refresh():
    with FakeTwitchId() as server:
        told = []
        auth = refreshing(server, validate_every=0.2, on_new_token=lambda: told.append(1))

        async def scenario():
            await auth.start()
            keeper = asyncio.create_task(auth.run())
            while len(server.validations()) < 3:
                await asyncio.sleep(0.02)
            server.access.clear()  # Twitch revoked every access token
            while not told:
                await asyncio.sleep(0.02)
            keeper.cancel()

        run(scenario())
        assert told == [1]
        assert auth.token() == "access-2"


def test_a_token_for_another_account_is_rejected():
    with FakeTwitchId(login="someoneelse") as server:
        with pytest.raises(TokenRejected, match="belongs to someoneelse"):
            run(refreshing(server).start())


def test_a_token_without_chat_edit_is_rejected():
    with FakeTwitchId(scopes=("chat:read",)) as server:
        with pytest.raises(TokenRejected, match="chat:edit"):
            run(refreshing(server).start())


# ---------------------------------------------------------------------------
# a static token (short tests)
# ---------------------------------------------------------------------------
def test_a_static_token_is_validated_on_start():
    with FakeTwitchId() as server:
        server.access["static-1"] = "hivebot"
        auth = TwitchAuth("hivebot", access_token="oauth:static-1", base_url=server.url)
        run(auth.start())
        assert auth.token() == "static-1"
        assert server.validations() == ["OAuth static-1"]
        assert server.posts() == []
        assert not auth.can_refresh


def test_an_invalid_static_token_is_token_rejected():
    with FakeTwitchId() as server:
        auth = TwitchAuth("hivebot", access_token="expired", base_url=server.url)
        with pytest.raises(TokenRejected, match="invalid or expired"):
            run(auth.start())


def test_a_static_token_expiring_while_running_is_token_rejected():
    with FakeTwitchId() as server:
        server.access["static-1"] = "hivebot"
        auth = TwitchAuth("hivebot", access_token="static-1", base_url=server.url,
                          validate_every=0.1)

        async def scenario():
            await auth.start()
            server.access.clear()
            await auth.run()

        with pytest.raises(TokenRejected):
            run(scenario())


def test_a_static_token_file_is_read_at_every_login(tmp_path):
    path = tmp_path / "token"
    path.write_text("oauth:first\n", encoding="utf-8")
    auth = TwitchAuth("hivebot", access_token_file=str(path), base_url="http://127.0.0.1:9/oauth2")
    assert auth.token() == "first"
    path.write_text("second", encoding="utf-8")
    assert auth.token() == "second"


def test_validation_that_cannot_reach_twitch_does_not_stop_a_static_token():
    auth = TwitchAuth("hivebot", access_token="static-1", base_url="http://127.0.0.1:9/oauth2")
    run(auth.start())
    assert auth.token() == "static-1"


def test_a_refresh_token_wins_over_a_static_token():
    with FakeTwitchId() as server:
        auth = TwitchAuth("hivebot", access_token="static-1", client_id="cid",
                          client_secret="secret", refresh_token="refresh-0", base_url=server.url)
        run(auth.start())
        assert auth.token() == "access-1"


def test_some_token_is_required():
    with pytest.raises(ValueError):
        TwitchAuth("hivebot", client_id="cid", client_secret="secret")


def test_a_validation_that_failed_at_start_is_tried_again_soon_not_in_an_hour():
    with FakeTwitchId() as server:
        server.access["static-1"] = "hivebot"
        server.validate_answers.append((503, {"message": "unavailable"}))
        auth = TwitchAuth("hivebot", access_token="static-1", base_url=server.url,
                          retry_cap=0.2)

        async def scenario():
            await auth.start()
            keeper = asyncio.create_task(auth.run())
            while len(server.validations()) < 2:
                await asyncio.sleep(0.02)
            keeper.cancel()

        run(scenario(), timeout=5)
        assert len(server.validations()) == 2
