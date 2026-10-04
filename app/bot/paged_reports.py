"""Compact report snapshots: one message, persistent pages, original access."""
from __future__ import annotations

import asyncio
import json
from functools import partial
from weakref import WeakValueDictionary

from aiogram import types
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest

from app.reports.text import paginate_report_html, utf16_length
from .presentation import message_text


def report_page_text(pages, page):
    page = max(0, min(int(page), len(pages) - 1))
    text = pages[page]
    # Repeat a compact report title so later pages remain identifiable.
    title = pages[0].split('\n', 1)[0]
    if page and utf16_length(title) <= 180 and not text.startswith(title):
        text = title + '\n\n' + text
    return text + f'\n\n<i>Страница {page + 1}/{len(pages)}</i>'


def report_page_keyboard(pages, page, markup=None):
    rows = [[types.InlineKeyboardButton(text='◀️', callback_data=f'report_page:{max(0,page-1)}'),
             types.InlineKeyboardButton(text=f'{page+1}/{len(pages)}', callback_data=f'report_page:{page}'),
             types.InlineKeyboardButton(text='▶️', callback_data=f'report_page:{min(len(pages)-1,page+1)}')]]
    if markup:
        rows.extend(markup.inline_keyboard)
    rows.append([types.InlineKeyboardButton(text='🏠 Главное меню', callback_data='report_page:home')])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_report_pages(send, context, *, bot_id, chat_id, user_id, pages,
                             reply_markup=None, permission='view', system_owner_only=False):
    if len(pages) == 1:
        return await send(pages[0] or '—', parse_mode='HTML', reply_markup=reply_markup)
    base = reply_markup if isinstance(reply_markup, types.InlineKeyboardMarkup) else None
    sent = await send(report_page_text(pages, 0), parse_mode='HTML',
                      reply_markup=report_page_keyboard(pages, 0, base))
    context.repository.save_paged_report(bot_id, chat_id, sent.message_id,
        shop_id=context.shop_id, user_id=user_id, permission=permission,
        system_owner_only=system_owner_only, pages=pages,
        markup=base.model_dump(exclude_none=True) if base else None)
    return sent


async def send_report_pages(bot, context, chat_id, pages, *, user_id,
                            reply_markup=None, permission='view', system_owner_only=False):
    """Outbound reports use the same durable page callbacks as manual reports."""
    return await _send_report_pages(partial(bot.send_message, chat_id), context,
        bot_id=bot.id, chat_id=chat_id, user_id=user_id, pages=pages,
        reply_markup=reply_markup, permission=permission, system_owner_only=system_owner_only)


class PagedReportController:
    def __init__(self, context):
        self.context = context
        self._locks = WeakValueDictionary()

    async def show(self, message, text, *, reply_markup=None, permission='view', system_owner_only=False):
        if (not message.from_user or not self.context.repository.can_user(message.from_user.id,self.context.shop_id,permission)
                or system_owner_only and message.from_user.id not in self.context.settings.owner_ids):
            return await message.answer('⛔ Нет доступа к этому отчёту.')
        text = message_text(text)
        pages = paginate_report_html(text)
        return await _send_report_pages(message.answer, self.context,
            bot_id=message.bot.id, chat_id=message.chat.id, user_id=message.from_user.id,
            pages=pages if len(pages) > 1 else [text], reply_markup=reply_markup,
            permission=permission, system_owner_only=system_owner_only)

    async def handle(self, callback, *, on_home=None):
        if not isinstance(callback.message, types.Message):
            return await callback.answer('Сообщение недоступно.', show_alert=True)
        message = callback.message
        key = (message.bot.id, message.chat.id, message.message_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            repo = self.context.repository
            saved = repo.paged_report(*key)
            if saved is None:
                return await callback.answer('Откройте раздел заново.', show_alert=True)
            shop = repo.get_shop(saved['shop_id'])
            user = callback.from_user.id
            capability = saved['capability']
            if (shop is None or not shop.active or user != saved['user_id'] or
                not repo.can_user(user, saved['shop_id'], saved['permission']) or
                (capability == 'legacy' and user not in self.context.settings.owner_ids) or
                (capability != 'legacy' and not repo.can_user(user, saved['shop_id'], capability)) or
                saved['system_owner_only'] and user not in self.context.settings.owner_ids):
                return await callback.answer('Нет доступа к этому отчёту.', show_alert=True)
            action = str(callback.data or '').removeprefix('report_page:')
            if action == 'home' and on_home:
                await callback.answer()
                return await on_home(callback)
            try:
                pages = json.loads(saved['pages_json'])
                page = max(0, min(int(action), len(pages) - 1))
                base = types.InlineKeyboardMarkup.model_validate(json.loads(saved['markup_json'])) if saved['markup_json'] else None
            except (ValueError, TypeError, IndexError):
                return await callback.answer('Некорректная страница.', show_alert=True)
            await callback.answer()
            try:
                await message.edit_text(report_page_text(pages, page), parse_mode='HTML',
                                        reply_markup=report_page_keyboard(pages, page, base))
            except TelegramBadRequest as exc:
                if 'message is not modified' not in str(exc).lower():
                    return await callback.answer('Сообщение недоступно. Откройте раздел заново.', show_alert=True)
            except TelegramAPIError:
                return await callback.answer('Не удалось перелистнуть. Повторите нажатие.', show_alert=True)
            repo.set_report_page(*key, page)
