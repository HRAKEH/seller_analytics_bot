"""Bounded replay protection and metadata-only Telegram input tracing."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import logging
from time import monotonic
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, types

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Delivery:
    expires_at: float


class TelegramUpdateMiddleware(BaseMiddleware):
    """Ignore replays of the same message/callback within this running process.

    Message identity, rather than text, distinguishes redelivery from another
    intentional click. Callback IDs distinguish different actions on one menu.
    A restart clears the bounded cache; the existing DB lease guards polling
    against another instance using the same database.
    """

    def __init__(self, instance_id: str, *, ttl: float = 3600, max_entries: int = 4096):
        if ttl <= 0 or max_entries < 1:
            raise ValueError('Telegram replay cache requires a positive TTL and capacity')
        self.instance_id = instance_id
        self.ttl = ttl
        self.max_entries = max_entries
        self._seen: OrderedDict[tuple[Any, ...], _Delivery] = OrderedDict()

    async def __call__(
        self,
        handler: Callable[[types.TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: types.TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, types.Update):
            return await handler(event, data)
        bot_id = getattr(data.get('bot'), 'id', None)
        message = event.message
        callback = event.callback_query
        origin = message or (callback.message if callback else None) or event.edited_message
        chat_id = origin.chat.id if origin else None
        message_id = origin.message_id if origin else None
        callback_id = callback.id if callback else None
        try:
            event_type = event.event_type
        except Exception:
            event_type = 'unknown'

        if message is not None:
            key = ('message', bot_id, chat_id, message_id)
        elif callback is not None:
            key = ('callback', bot_id, callback_id)
        else:
            # Edits and all other events have their own update identity: an edit
            # must not disappear because its original message was processed.
            key = ('update', bot_id, event.update_id)

        now = monotonic()
        while self._seen and next(iter(self._seen.values())).expires_at <= now:
            self._seen.popitem(last=False)
        if key in self._seen:
            log.warning(
                'Duplicate Telegram delivery ignored update=%s type=%s chat=%s '
                'message=%s callback=%s bot=%s instance=%s',
                event.update_id, event_type, chat_id, message_id, callback_id,
                bot_id, self.instance_id,
            )
            return None
        while len(self._seen) >= self.max_entries:
            self._seen.popitem(last=False)
        delivery = _Delivery(expires_at=now + self.ttl)
        # Claim before awaiting the handler so concurrent copies cannot reply
        # twice. No user text, names, credentials or callback payloads are logged.
        self._seen[key] = delivery
        log.info(
            'Telegram incoming update=%s type=%s chat=%s message=%s callback=%s '
            'bot=%s instance=%s',
            event.update_id, event_type, chat_id, message_id, callback_id,
            bot_id, self.instance_id,
        )
        try:
            return await handler(event, data)
        except BaseException:
            # A failed/cancelled handler can be retried. Do not remove a newer
            # delivery if this entry was evicted while the handler was running.
            if self._seen.get(key) is delivery:
                self._seen.pop(key, None)
            raise
