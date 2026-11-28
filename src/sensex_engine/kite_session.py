"""Kite access-token lifecycle for a process that shares a Kite app with another bot.

Zerodha issues one daily access token per login. Logging in again from this
process would invalidate the token the other container is using, so this
engine never performs the login flow itself. It reads the token file that the
primary bot (sxpro-bot) writes after its daily Kite login, mounted read-only,
and falls back to ``KITE_ACCESS_TOKEN``. When the file changes, the shared
KiteConnect client is re-pointed at the new token. Tokens are never logged.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .indicators import IST

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenInfo:
    token: str
    source: str  # "file" | "env" | "none"
    saved_at: datetime | None

    @property
    def is_today(self) -> bool:
        if self.saved_at is None:
            return self.source == "env"
        # Kite tokens expire around 06:00 IST the next day; a token saved on an
        # earlier IST date is treated as expired.
        return self.saved_at.astimezone(IST).date() == datetime.now(IST).date()


class AccessTokenProvider:
    def __init__(self, *, token_file: str = "", fallback_token: str = "") -> None:
        self._file = token_file.strip()
        self._fallback = fallback_token.strip()
        self._mtime: float | None = None
        self._cached: TokenInfo | None = None

    def current(self) -> TokenInfo:
        if self._file:
            try:
                mtime = os.path.getmtime(self._file)
                if self._cached is not None and mtime == self._mtime:
                    return self._cached
                with open(self._file, encoding="utf-8") as fh:
                    data = json.load(fh)
                token = str(data.get("access_token") or "").strip()
                saved_raw = data.get("saved_at")
                saved = datetime.fromisoformat(saved_raw) if saved_raw else None
                if saved is not None and saved.tzinfo is None:
                    saved = saved.replace(tzinfo=IST)
                if token:
                    self._mtime = mtime
                    self._cached = TokenInfo(token, "file", saved)
                    return self._cached
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError) as exc:
                logger.warning("Shared Kite token file unreadable: %s", type(exc).__name__)
        if self._fallback:
            return TokenInfo(self._fallback, "env", None)
        return TokenInfo("", "none", None)


class SharedKiteClient:
    """Keeps one KiteConnect client pointed at the latest shared token."""

    def __init__(
        self, api_key: str, provider: AccessTokenProvider, client: Any | None = None
    ) -> None:
        if not api_key.strip():
            raise ValueError("Kite API key is required")
        if client is None:
            from kiteconnect import KiteConnect

            client = KiteConnect(api_key=api_key)
        self.client = client
        self._provider = provider
        self._token = ""

    def refresh(self) -> TokenInfo:
        info = self._provider.current()
        if info.token and info.token != self._token:
            self.client.set_access_token(info.token)
            self._token = info.token
            logger.info("Kite access token loaded from %s (saved %s)", info.source, info.saved_at)
        return info

    @property
    def has_token(self) -> bool:
        return bool(self._token)
