"""Failures that no retry can fix: the process has to stop and say why."""


class TokenRejected(Exception):
    """Twitch refused the bot's credentials (chat login, refresh or validation).

    The message is the bridge's own short reason, never a token.
    """


class ClientRejected(Exception):
    """Twitch refused the application's client id or client secret."""
