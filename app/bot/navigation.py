"""Remove only explicitly transient navigation in private conversations."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import logging
from weakref import WeakValueDictionary

from aiogram import BaseMiddleware, types
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.exceptions import TelegramAPIError

log=logging.getLogger(__name__)


@dataclass
class _Visit:
    key: tuple[int, int, int]
    message_id: int | None
    shown: bool = False


class NavigationMessages:
    def __init__(self, repository):
        # A factory also supports ContextProxy: resolve after shop middleware.
        self.repository=repository
        self._visit: ContextVar[_Visit | None]=ContextVar('navigation_visit',default=None)
        self._locks: WeakValueDictionary=WeakValueDictionary()

    def _key(self, message, actor_user=None):
        if message.chat.type != 'private':
            return None
        prefix=(message.bot.id,message.chat.id)
        visit=self._visit.get()
        if actor_user is None and visit and visit.key[:2]==prefix:
            return visit.key
        user=actor_user or message.from_user
        if user is None or user.is_bot:
            return None
        return (*prefix,user.id)

    def _lock(self, key):
        return self._locks.setdefault(key,asyncio.Lock())

    @contextmanager
    def visit(self, event):
        message=event.message if isinstance(event,types.CallbackQuery) else event
        actor=event.from_user
        key=self._key(message,actor) if isinstance(message,types.Message) else None
        message_id=(message.message_id if isinstance(event,types.CallbackQuery)
                    else self.repository().navigation_message(*key)) if key else None
        visit=_Visit(key,message_id) if key else None
        token=self._visit.set(visit)
        try:
            yield visit
        finally:
            self._visit.reset(token)

    async def remove(self, message, message_id: int):
        """Best effort: unavailable/old messages must not break navigation."""
        if message.chat.type != 'private':
            return
        try:
            await message.bot.delete_message(message.chat.id,message_id)
        except (TelegramAPIError,OSError,asyncio.TimeoutError) as exc:
            # Telegram exceptions can contain request data; log metadata only.
            log.info('Navigation deletion skipped chat=%s message=%s error=%s',
                     message.chat.id,message_id,type(exc).__name__)

    async def show(self, message, text: str, *, actor_user=None, **kwargs):
        key=self._key(message,actor_user)
        if key is None:
            return await message.answer(text,**kwargs)
        async with self._lock(key):
            repo=self.repository()
            previous=repo.navigation_message(*key)
            # Reply keyboards cannot be attached to editMessageText. Send the
            # working replacement first, then retire its predecessor.
            sent=await message.answer(text,**kwargs)
            repo.save_navigation_message(*key,sent.message_id)
            visit=self._visit.get()
            if visit and visit.key==key:
                visit.message_id=sent.message_id
                visit.shown=True
            if previous is not None and previous!=sent.message_id:
                await self.remove(message,previous)
            return sent

    async def dismiss(self, message):
        """Retire this request's prompt after a report/file was delivered."""
        key=self._key(message)
        if key is None:
            return
        visit=self._visit.get()
        # The snapshot at handler entry protects menus created by concurrent
        # requests while this report was collecting data.
        expected=visit.message_id if visit and visit.key==key else self.repository().navigation_message(*key)
        if expected is None:
            return
        async with self._lock(key):
            repo=self.repository()
            if repo.navigation_message(*key)!=expected:
                return
            await self.remove(message,expected)
            repo.clear_navigation_message(*key,expected)
            if visit and visit.key==key:
                visit.message_id=None

    async def retain(self, message):
        """An edited selector became a business result: keep it in history."""
        key=self._key(message)
        if key is None:
            return
        async with self._lock(key):
            self.repository().clear_navigation_message(*key,message.message_id)


class MenuCleanupMiddleware(BaseMiddleware):
    def __init__(self, navigation: NavigationMessages, button_texts, allowed):
        self.navigation=navigation
        self.button_texts=button_texts
        self.allowed=allowed

    async def __call__(self, handler, event, data):
        button=(isinstance(event,types.Message) and event.chat.type=='private'
                and event.text in self.button_texts and self.allowed(event))
        with self.navigation.visit(event) as visit:
            result=await handler(event,data)
            if button and result is not UNHANDLED:
                if visit and not visit.shown:
                    await self.navigation.dismiss(event)
                await self.navigation.remove(event,event.message_id)
            return result
