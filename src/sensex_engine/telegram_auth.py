"""Fail-closed Telegram identity checks; handlers are added in the next increment."""
from dataclasses import dataclass


@dataclass(frozen=True)
class TelegramAllowlist:
    user_ids: frozenset[int]
    chat_ids: frozenset[int]

    def allows(self, user_id: int | None, chat_id: int | None) -> bool:
        if user_id is None or chat_id is None:
            return False
        return user_id in self.user_ids and chat_id in self.chat_ids


def parse_id_allowlist(raw: str) -> frozenset[int]:
    """Parse comma-separated Telegram numeric IDs; reject malformed configuration."""
    if not raw.strip():
        return frozenset()
    values: set[int] = set()
    for item in raw.split(","):
        value = item.strip()
        if not value or not value.lstrip("-").isdigit():
            raise ValueError("Telegram allowlist must contain comma-separated integer IDs")
        values.add(int(value))
    return frozenset(values)
