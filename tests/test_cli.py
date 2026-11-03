"""The CLI resolves its settings from flags, the environment and secret files.

A secret on the command line is readable by every process on the host
(``ps``, ``/proc/<pid>/cmdline``), so a container must be able to start the
bridge with no secret in its arguments at all.
"""
import pytest

from twitch_bridge.__main__ import ENVIRONMENT, main, resolve_options

BASE = {"TWITCH_BOT_LOGIN": "hivebot", "TWITCH_ALLOWED_CHANNELS": "alpha",
        "TWITCH_OAUTH_TOKEN": "tok"}


def test_everything_can_come_from_the_environment():
    options = resolve_options([], {
        "TWITCH_BOT_LOGIN": "HiveBot",
        "TWITCH_ALLOWED_CHANNELS": "alpha, #Beta gamma",
        "TWITCH_CLIENT_ID": "cid",
        "TWITCH_CLIENT_SECRET": "csecret",
        "TWITCH_REFRESH_TOKEN": "rtok",
        "TWITCH_REQUIRE_MENTION": "false",
        "TWITCH_SESSION_SCOPE": "chat",
        "BRIDGE_HEALTH_PORT": "8080",
        "HIVEMIND_ACCESS_KEY": "key",
        "HIVEMIND_PASSWORD": "pw",
        "HIVEMIND_HOST": "hub.local",
        "HIVEMIND_PORT": "6000",
        "HIVEMIND_SITE_ID": "stream",
        "HIVEMIND_SELF_SIGNED": "true",
        "HIVEMIND_LANG": "fr-fr",
    })
    assert options == {
        "bot_login": "hivebot",
        "channels": ["alpha", "beta", "gamma"],
        "oauth_token": None,
        "oauth_token_file": None,
        "client_id": "cid",
        "client_secret": "csecret",
        "refresh_token": "rtok",
        "key": "key",
        "password": "pw",
        "host": "ws://hub.local",
        "port": 6000,
        "self_signed": True,
        "lang": "fr-fr",
        "site_id": "stream",
        "require_mention": False,
        "session_scope": "chat",
        "health_port": 8080,
    }


def test_the_defaults():
    options = resolve_options([], dict(BASE))
    assert options["require_mention"] is True
    assert options["session_scope"] == "person"
    assert options["site_id"] == "twitch"
    assert options["lang"] == "en-us"
    assert options["port"] == 5678
    assert options["health_port"] is None
    assert options["self_signed"] is False
    assert options["oauth_token"] == "tok"


def test_secrets_can_come_from_files(tmp_path):
    files = {}
    for name, value in (("secret", "csecret-from-file\n"), ("refresh", "rtok-from-file"),
                        ("key", "key-from-file"), ("password", "pw-from-file")):
        path = tmp_path / name
        path.write_text(value, encoding="utf-8")
        files[name] = str(path)
    options = resolve_options([], {
        "TWITCH_BOT_LOGIN": "hivebot", "TWITCH_ALLOWED_CHANNELS": "alpha",
        "TWITCH_CLIENT_ID": "cid",
        "TWITCH_CLIENT_SECRET_FILE": files["secret"],
        "TWITCH_REFRESH_TOKEN_FILE": files["refresh"],
        "HIVEMIND_ACCESS_KEY_FILE": files["key"],
        "HIVEMIND_PASSWORD_FILE": files["password"],
    })
    assert options["client_secret"] == "csecret-from-file"
    assert options["refresh_token"] == "rtok-from-file"
    assert options["key"] == "key-from-file"
    assert options["password"] == "pw-from-file"


def test_an_oauth_token_file_is_passed_as_a_file_so_a_rotation_is_seen(tmp_path):
    path = tmp_path / "token"
    path.write_text("oauth:from-file\n", encoding="utf-8")
    options = resolve_options([], {"TWITCH_BOT_LOGIN": "hivebot",
                                   "TWITCH_ALLOWED_CHANNELS": "alpha",
                                   "TWITCH_OAUTH_TOKEN_FILE": str(path)})
    assert options["oauth_token"] is None
    assert options["oauth_token_file"] == str(path)


def test_a_flag_wins_over_the_environment_and_the_environment_over_a_file(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("from-file", encoding="utf-8")
    environ = {**BASE, "TWITCH_CLIENT_ID": "cid", "TWITCH_REFRESH_TOKEN": "r",
               "TWITCH_CLIENT_SECRET": "from-env", "TWITCH_CLIENT_SECRET_FILE": str(secret)}
    assert resolve_options(["--client-secret", "from-flag"], environ)["client_secret"] == "from-flag"
    assert resolve_options([], environ)["client_secret"] == "from-env"
    del environ["TWITCH_CLIENT_SECRET"]
    assert resolve_options([], environ)["client_secret"] == "from-file"
    assert resolve_options(["--allowed-channel", "zeta"], environ)["channels"] == ["zeta"]
    assert resolve_options(["--bot-login", "other"], environ)["bot_login"] == "other"
    assert resolve_options(["--no-require-mention"], environ)["require_mention"] is False


def test_the_bot_login_is_required_and_named(capsys):
    with pytest.raises(SystemExit):
        resolve_options([], {"TWITCH_ALLOWED_CHANNELS": "alpha", "TWITCH_OAUTH_TOKEN": "t"})
    assert "TWITCH_BOT_LOGIN" in capsys.readouterr().err


def test_channels_are_required_and_must_be_logins(capsys):
    with pytest.raises(SystemExit):
        resolve_options([], {"TWITCH_BOT_LOGIN": "hivebot", "TWITCH_OAUTH_TOKEN": "t"})
    assert "TWITCH_ALLOWED_CHANNELS" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        resolve_options([], {**BASE, "TWITCH_ALLOWED_CHANNELS": "alpha, bad-name"})
    assert "bad-name" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        resolve_options([], {**BASE, "TWITCH_ALLOWED_CHANNELS": " ".join(
            f"c{index}" for index in range(101))})


def test_some_token_is_required(capsys):
    with pytest.raises(SystemExit):
        resolve_options([], {"TWITCH_BOT_LOGIN": "hivebot", "TWITCH_ALLOWED_CHANNELS": "a"})
    err = capsys.readouterr().err
    assert "TWITCH_REFRESH_TOKEN" in err and "TWITCH_OAUTH_TOKEN" in err


def test_a_partial_refresh_setup_names_what_is_missing(capsys):
    with pytest.raises(SystemExit):
        resolve_options([], {**BASE, "TWITCH_CLIENT_ID": "cid", "TWITCH_REFRESH_TOKEN": "r"})
    assert "missing: TWITCH_CLIENT_SECRET" in capsys.readouterr().err


@pytest.mark.parametrize("variable,value", [
    ("TWITCH_SESSION_SCOPE", "room"),
    ("TWITCH_REQUIRE_MENTION", "sometimes"),
    ("HIVEMIND_PORT", "abc"),
    ("BRIDGE_HEALTH_PORT", "x"),
    ("TWITCH_BOT_LOGIN", "not a login"),
])
def test_malformed_values_are_refused(variable, value):
    with pytest.raises(SystemExit) as stopped:
        resolve_options([], {**BASE, variable: value})
    assert stopped.value.code == 2


def test_an_unreadable_secret_file_is_named_and_exits_2(tmp_path, capsys):
    with pytest.raises(SystemExit) as stopped:
        resolve_options([], {**BASE, "TWITCH_REFRESH_TOKEN_FILE": str(tmp_path / "missing")})
    assert stopped.value.code == 2
    assert "TWITCH_REFRESH_TOKEN_FILE" in capsys.readouterr().err


def test_no_hub_credentials_anywhere_exits_2(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # no identity file
    for name, value in BASE.items():
        monkeypatch.setenv(name, value)
    for name in ("HIVEMIND_ACCESS_KEY", "HIVEMIND_PASSWORD", "HIVEMIND_HOST"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit) as stopped:
        main([])
    assert stopped.value.code == 2
    assert "key and password" in capsys.readouterr().err


def test_every_secret_has_a_file_variant_and_the_names_are_the_contract():
    assert set(ENVIRONMENT.values()) == {
        "TWITCH_OAUTH_TOKEN", "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET",
        "TWITCH_REFRESH_TOKEN", "TWITCH_BOT_LOGIN", "TWITCH_ALLOWED_CHANNELS",
        "TWITCH_REQUIRE_MENTION", "TWITCH_SESSION_SCOPE", "BRIDGE_HEALTH_PORT",
        "HIVEMIND_ACCESS_KEY", "HIVEMIND_PASSWORD", "HIVEMIND_HOST", "HIVEMIND_PORT",
        "HIVEMIND_SITE_ID", "HIVEMIND_SELF_SIGNED", "HIVEMIND_LANG"}


def test_main_exits_with_the_bridges_status(monkeypatch):
    import twitch_bridge.__main__ as cli

    made = {}

    class Bridge:
        def __init__(self, **options):
            made.update(options)

        def run(self):
            return 3

    monkeypatch.setattr(cli, "HiveMindTwitchBridge", Bridge)
    for name, value in BASE.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit) as stopped:
        main([])
    assert stopped.value.code == 3
    assert made["bot_login"] == "hivebot"
