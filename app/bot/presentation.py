"""Shop-local display dates and Telegram's native SKU copy buttons."""
from __future__ import annotations

from contextvars import ContextVar
from html import unescape
import re

from aiogram import BaseMiddleware
from aiogram.methods import EditMessageText, SendMessage, SendDocument
from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from app.reports.dates import display_timezone, readable_text

_presentation = ContextVar('message_presentation', default=None)
_SKU = re.compile(r'артикул\s*:?\s*(?:<code>(.*?)</code>|([^\s<>·]+))|SKU\s*:?\s*<code>(.*?)</code>', re.I)
_INTERNAL = re.compile(r'\bвнутренн(?:ий|его)\b', re.I)
_TECH_COMMANDS = {'status', 'health', 'jobs', 'job_retry', 'diagnostics', 'backup', 'backups', 'restore', 'profiles', 'connect_check'}
_TECH_BUTTONS = {'📡 Состояние данных', '❤️ Проверка бота', '🔁 Ошибки и повторы', '🔁 Повторить retry-задачу',
                 '🧪 Техническая диагностика', '💾 Создать backup', '🗂 История backup', '♻️ Восстановить backup',
                 '🗝 Профили окружения', '🔌 Проверить API', '🧰 Техническое'}


def sku_copy_buttons(text: str) -> list[list[InlineKeyboardButton]]:
    """Only explicitly labelled SKUs are copied; IDs and amounts aren't SKUs."""
    result = []
    seen = set()
    market = 'Артикул'
    for line in text.splitlines():
        # Marketplace labels precede names/SKUs. A product named "for Ozon"
        # must not relabel a WB article, nor may a SKU containing "WB" do so.
        prefix = re.split(r'<b>|артикул|SKU', line, maxsplit=1, flags=re.I)[0]
        markets = re.findall(r'\b(?:WB|Wildberries|wildberries|Ozon|ozon)\b', prefix)
        labels = {'WB' if x.lower() in {'wb', 'wildberries'} else 'Ozon' for x in markets}
        if len(labels) == 1:
            market = labels.pop()
        elif len(labels) > 1:
            market = 'Артикул'
        for match in _SKU.finditer(line):
            coded = match.group(1) if match.group(1) is not None else match.group(3)
            value = unescape(coded if coded is not None else match.group(2).rstrip('.,;'))
            if not value or value == '—' or not 1 <= len(value) <= 256:
                continue
            label = 'Внутренний' if _INTERNAL.search(line) else market
            key = (label, value)
            if key in seen:
                continue
            seen.add(key)
            caption = f'📋 {label}: {value}'
            if len(caption) > 58:
                caption = caption[:55] + '…'
            result.append([InlineKeyboardButton(text=caption, copy_text=CopyTextButton(text=value))])
    return result[:60]


def with_sku_copy(text: str, markup=None):
    copies = sku_copy_buttons(text)
    if not copies:
        return markup
    rows = list(markup.inline_keyboard) if isinstance(markup, InlineKeyboardMarkup) else []
    existing = {(button.copy_text.text) for row in rows for button in row if button.copy_text}
    rows += [row for row in copies if row[0].copy_text.text not in existing]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def present_request(make_request, bot, method):
    options = _presentation.get()
    if options and options[1] and isinstance(method, (SendMessage, EditMessageText, SendDocument)):
        field = 'caption' if isinstance(method, SendDocument) else 'text'
        original = getattr(method, field)
        if original:
            text = readable_text(original, tz=options[0])
            method = method.model_copy(update={field: text, 'reply_markup': with_sku_copy(text, method.reply_markup)})
    return await make_request(bot, method)


class PresentationMiddleware(BaseMiddleware):
    def __init__(self, context):
        self.context = context
        self._sessions = set()

    async def __call__(self, handler, event, data):
        bot = data.get('bot')
        if bot is not None and id(bot.session) not in self._sessions:
            bot.session.middleware.register(present_request)
            self._sessions.add(id(bot.session))
        raw = str(getattr(event, 'text', '') or '')
        command = raw.split(maxsplit=1)[0].split('@')[0].lstrip('/') if raw else ''
        technical = command in _TECH_COMMANDS or raw in _TECH_BUTTONS
        callback = str(getattr(event, 'data', '') or '')
        technical = technical or callback.startswith(('retry_job:', 'job:', 'restore:'))
        pref = self.context.repository.get_shop_preferences(self.context.shop_id)
        tz = pref.timezone if pref else 'Europe/Moscow'
        token = _presentation.set((tz, not technical))
        date_token = display_timezone.set(tz)
        try:
            return await handler(event, data)
        finally:
            _presentation.reset(token)
            display_timezone.reset(date_token)
