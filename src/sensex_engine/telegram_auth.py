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
