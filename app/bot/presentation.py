"""Shop-local display dates; articles are inline code in the report itself."""
from __future__ import annotations

from contextvars import ContextVar

from aiogram import BaseMiddleware
from aiogram.methods import EditMessageText, SendMessage, SendDocument

from app.reports.dates import display_timezone, readable_text

_presentation = ContextVar('message_presentation', default=None)
_TECH_COMMANDS = {'status', 'health', 'jobs', 'job_retry', 'diagnostics', 'backup', 'backups', 'restore', 'profiles', 'connect_check', 'ozon_push'}
_TECH_BUTTONS = {'📡 Состояние данных', '❤️ Проверка бота', '🔁 Ошибки и повторы', '🔁 Повторить retry-задачу',
                 '🧪 Техническая диагностика', '💾 Создать backup', '🗂 История backup', '♻️ Восстановить backup',
                 '🗝 Профили окружения', '🔌 Проверить API', '🧰 Техническое', '🔵 Подключить отмены Ozon'}


def message_text(text: str) -> str:
    options = _presentation.get()
    return readable_text(text, tz=options[0]) if options and options[1] else text


async def present_request(make_request, bot, method):
    options = _presentation.get()
    if options and options[1] and isinstance(method, (SendMessage, EditMessageText, SendDocument)):
        field = 'caption' if isinstance(method, SendDocument) else 'text'
        original = getattr(method, field)
        if original:
            text = message_text(original)
            method = method.model_copy(update={field: text})
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
        technical = technical or callback.startswith(('retry_job:', 'job:', 'restore:', 'report_page:', 'ozpush:'))
        pref = self.context.repository.get_shop_preferences(self.context.shop_id)
        tz = pref.timezone if pref else 'Europe/Moscow'
        token = _presentation.set((tz, not technical))
        date_token = display_timezone.set(tz)
        try:
            return await handler(event, data)
        finally:
            _presentation.reset(token)
            display_timezone.reset(date_token)
