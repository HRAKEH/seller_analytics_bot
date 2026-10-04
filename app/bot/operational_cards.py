"""Paged operational reports, bound to their shop, edited in one message."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from weakref import WeakValueDictionary
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.reports.alerts import alert_key, format_active_alerts, format_alert_detail, sorted_alerts
from app.reports.operations import format_action_center, format_action_detail
from app.reports.products import build_product_report
from app.reports.text import page_slice
from app.services.actions import build_action_center
from .keyboards import action_ref


def _button(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def page_keyboard(kind, shop_id, page, pages, items):
    rows = []
    for index, item in enumerate(items, page * 5 + 1):
        key = alert_key(item) if kind == 'alerts' else item.action_key
        rows.append([_button(f'{index}. Подробнее', f'ops:{kind}:view:{shop_id}:{page}:{action_ref(key)}')])
    if pages > 1:
        rows.append([_button('◀️', f'ops:{kind}:list:{shop_id}:{max(0, page - 1)}'),
                     _button(f'{page + 1}/{pages}', f'ops:{kind}:list:{shop_id}:{page}'),
                     _button('▶️', f'ops:{kind}:list:{shop_id}:{min(pages - 1, page + 1)}')])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def detail_keyboard(kind, shop_id, page, ref, *, can_operate=False):
    rows = []
    if kind == 'actions' and can_operate:
        rows.append([_button('✅ Принято', f'ops:actions:ack:{shop_id}:{page}:{ref}'),
                     _button('⏰ Отложить на 24 ч', f'ops:actions:snooze:{shop_id}:{page}:{ref}')])
    rows.append([_button('⬅️ К списку', f'ops:{kind}:list:{shop_id}:{page}')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


class OperationalCards:
    def __init__(self, context, registry=None):
        self.context = context
        self.registry = registry
        self._locks = WeakValueDictionary()

    def _lock(self, key):
        return self._locks.setdefault(key, asyncio.Lock())

    def _context(self, shop_id):
        ctx = self.registry.get(shop_id) if self.registry else self.context
        if ctx.shop_id != shop_id:
            raise ValueError('shop changed')
        shop = ctx.repository.get_shop(shop_id)
        if shop is None or not shop.active:
            raise ValueError('shop unavailable')
        return ctx

    def _data(self, ctx, kind, *, persist=False):
        pref = ctx.preferences()
        day = datetime.now(ZoneInfo(pref.timezone)).date() - timedelta(days=1)
        if kind == 'actions':
            report = build_action_center(ctx.repository, ctx.shop_id, day, persist=persist)
            items = sorted(report.items, key=lambda item: (item.priority, item.category, item.title))
        else:
            report = build_product_report(ctx.repository, ctx.shop_id, day,
                stock_lookback_days=pref.stock_velocity_days, stock_risk_days=pref.stock_risk_days)
            items = sorted_alerts(ctx.repository, ctx.shop_id, report)
        return report, items

    def _list(self, ctx, kind, report, items, page):
        visible, page, pages = page_slice(items, page)
        tz = ctx.preferences().timezone
        text = (format_active_alerts(ctx.repository, ctx.shop_id, report, page=page, timezone=tz)
                if kind == 'alerts' else format_action_center(report, page=page, timezone=tz))
        return text, page_keyboard(kind, ctx.shop_id, page, pages, visible)

    async def show(self, message, kind):
        ctx = self.context
        if not message.from_user or not ctx.repository.can_user(message.from_user.id, ctx.shop_id, 'view'):
            return await message.answer('⛔ Недостаточно прав.')
        report, items = self._data(ctx, kind, persist=kind == 'actions' and
                                   ctx.repository.can_user(message.from_user.id, ctx.shop_id, 'operate'))
        text, markup = self._list(ctx, kind, report, items, 0)
        return await message.answer(text, parse_mode='HTML', reply_markup=markup)

    async def _edit(self, callback, text, markup):
        try:
            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=markup)
        except TelegramBadRequest as exc:
            if 'message is not modified' not in str(exc).lower():
                await callback.answer('Сообщение недоступно. Откройте раздел снова.', show_alert=True)
        except TelegramAPIError:
            await callback.answer('Не удалось изменить сообщение. Повторите нажатие.', show_alert=True)

    async def handle(self, callback):
        parts = str(callback.data or '').split(':')
        if len(parts) not in {5, 6} or parts[1] not in {'alerts', 'actions'}:
            return await callback.answer('Неизвестная кнопка.', show_alert=True)
        _, kind, action, raw_shop, raw_page, *tail = parts
        if action not in {'list', 'view', 'ack', 'snooze'} or (action != 'list' and not tail):
            return await callback.answer('Неизвестная кнопка.', show_alert=True)
        if kind == 'alerts' and action in {'ack', 'snooze'}:
            return await callback.answer('Неизвестная кнопка.', show_alert=True)
        try:
            ctx = self._context(int(raw_shop))
            page = int(raw_page)
        except (ValueError, TypeError, KeyError):
            return await callback.answer('Магазин недоступен. Откройте раздел снова.', show_alert=True)
        permission = 'operate' if action in {'ack', 'snooze'} else 'view'
        if not callback.from_user or not ctx.repository.can_user(callback.from_user.id, ctx.shop_id, permission):
            return await callback.answer('Недостаточно прав.', show_alert=True)
        if callback.message is None or not hasattr(callback.message, 'edit_text'):
            return await callback.answer('Сообщение недоступно.', show_alert=True)
        key = (callback.message.chat.id, callback.message.message_id)
        async with self._lock(key):
            report, items = self._data(ctx, kind)
            if action == 'list':
                text, markup = self._list(ctx, kind, report, items, page)
                await callback.answer()
                return await self._edit(callback, text, markup)
            ref = tail[0]
            item = next((item for item in items if action_ref(alert_key(item) if kind == 'alerts' else item.action_key) == ref), None)
            if item is None:
                return await callback.answer('Пункт уже изменился. Вернитесь к списку.', show_alert=True)
            if action in {'ack', 'snooze'}:
                status = 'acknowledged' if action == 'ack' else 'snoozed'
                ok = ctx.repository.set_action_status(ctx.shop_id, item.action_key, status,
                    telegram_user_id=callback.from_user.id, snooze_hours=24)
                if not ok:
                    return await callback.answer('Не удалось обновить действие.', show_alert=True)
                report, items = self._data(ctx, kind)
                await callback.answer('Принято' if action == 'ack' else 'Отложено на 24 часа')
                if action == 'snooze':
                    text, markup = self._list(ctx, kind, report, items, page)
                    return await self._edit(callback, text, markup)
                item = next((item for item in items if action_ref(item.action_key) == ref), item)
            else:
                await callback.answer()
            tz = ctx.preferences().timezone
            text = format_alert_detail(item, report, timezone=tz) if kind == 'alerts' else format_action_detail(item, timezone=tz)
            markup = detail_keyboard(kind, ctx.shop_id, page, ref,
                can_operate=ctx.repository.can_user(callback.from_user.id, ctx.shop_id, 'operate'))
            await self._edit(callback, text, markup)
