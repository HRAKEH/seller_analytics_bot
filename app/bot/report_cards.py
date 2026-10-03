"""Persistent daily cards shared by manual reports, scheduler, and retries."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import date
import logging
from weakref import WeakValueDictionary

from aiogram import types
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest

from app.reports.cards import DailyCardText, format_daily_card, render_daily_card
from app.reports.daily import build_daily_report_with_currency
from .keyboards import daily_card_keyboard

log=logging.getLogger(__name__)


async def prepare_daily_card(ctx, day: date) -> DailyCardText:
    report=await build_daily_report_with_currency(ctx.repository,ctx.shop_id,day)
    return format_daily_card(ctx.repository,ctx.shop_id,report)


async def send_daily_card(bot, ctx, chat_id: int, day: date, *, user_id: int,
                          text: DailyCardText | None = None, status_note: str = ''):
    text=text or await prepare_daily_card(ctx,day)
    card={'shop_id':ctx.shop_id,'report_day':day.isoformat(),**asdict(text),
          'section':'summary','status_note':status_note}
    can_refresh=ctx.repository.can_user(user_id,ctx.shop_id,'operate')
    message=await bot.send_message(chat_id,render_daily_card(card),parse_mode='HTML',
                                  reply_markup=daily_card_keyboard(can_refresh=can_refresh))
    ctx.repository.save_report_card(bot.id,chat_id,message.message_id,**card)
    return message


async def send_daily_cards(bot, ctx, day: date, *, status_note: str = ''):
    # Resolve FX once, before any recipient sees this snapshot.
    text=await prepare_daily_card(ctx,day)
    for row in ctx.repository.users_for_shop(ctx.shop_id):
        user_id=int(row['telegram_user_id'])
        try:
            await send_daily_card(bot,ctx,user_id,day,user_id=user_id,text=text,status_note=status_note)
        except asyncio.CancelledError:raise
        except Exception:log.exception('Cannot send daily card to %s',user_id)


class DailyCardController:
    def __init__(self, context, registry=None):
        self.context=context
        self.registry=registry
        self._locks=WeakValueDictionary()
        self._refreshing=set()
        self._refreshing_shops=set()

    def _lock(self, key):
        lock=self._locks.get(key)
        if lock is None:
            lock=asyncio.Lock()
            self._locks[key]=lock
        return lock

    async def _answer(self, callback, text: str = '', *, alert: bool = False):
        if callback is None:return
        try:await callback.answer(text,show_alert=alert)
        except TelegramAPIError:log.debug('Daily card callback acknowledgement expired',exc_info=True)

    async def _edit(self, bot, key, card, user_id: int) -> bool:
        repo=self.context.repository
        try:
            await bot.edit_message_text(render_daily_card(card),chat_id=key[1],message_id=key[2],
                parse_mode='HTML',reply_markup=daily_card_keyboard(card['section'],
                    can_refresh=repo.can_user(user_id,card['shop_id'],'operate')))
        except TelegramBadRequest as exc:
            if 'message is not modified' not in str(exc).lower():
                log.warning('Cannot edit daily card %s',key,exc_info=True)
                return False
        except TelegramAPIError:
            log.warning('Cannot edit daily card %s',key,exc_info=True)
            return False
        # Persist only the state Telegram actually accepted, so failed edits can
        # be retried without a new message or an inconsistent collapse button.
        repo.save_report_card(*key,**{field:card[field] for field in (
            'shop_id','report_day','summary_html','details_html','accruals_html','section','status_note')})
        return True

    async def handle(self, callback: types.CallbackQuery):
        if not isinstance(callback.message,types.Message):
            return await self._answer(callback,'Сообщение недоступно.',alert=True)
        action=(callback.data or '').removeprefix('daily_card:')
        if action not in {'details','accruals','collapse','refresh'}:
            return await self._answer(callback,'Неизвестная кнопка отчёта.',alert=True)
        message=callback.message
        key=(message.bot.id,message.chat.id,message.message_id)
        if action=='refresh':
            return await self.refresh(message,callback.from_user.id,callback=callback)
        async with self._lock(key):
            card=self.context.repository.report_card(*key)
            if card is None:
                return await self._answer(callback,'Откройте новый отчёт через «Вчера» или выбор даты.',alert=True)
            if not self.context.repository.can_user(callback.from_user.id,card['shop_id'],'view'):
                return await self._answer(callback,'Нет доступа к магазину этого отчёта.',alert=True)
            if action!='collapse' and card['section']!='summary':
                return await self._answer(callback,'Сначала сверните открытый блок.')
            card['section']='summary' if action=='collapse' else action
            if await self._edit(message.bot,key,card,callback.from_user.id):
                await self._answer(callback)
            else:
                await self._answer(callback,'Не удалось изменить сообщение. Повторите нажатие или откройте новый отчёт.',alert=True)

    async def refresh(self, message: types.Message, user_id: int, *, callback=None):
        key=(message.bot.id,message.chat.id,message.message_id)
        repo=self.context.repository
        async with self._lock(key):
            card=repo.report_card(*key)
            if card is None:
                return await self._answer(callback,'Откройте новый отчёт через «Вчера» или выбор даты.',alert=True)
            if not repo.can_user(user_id,card['shop_id'],'operate'):
                return await self._answer(callback,'Обновление доступно владельцу или аналитику этого магазина.',alert=True)
            if key in self._refreshing:
                return await self._answer(callback,'Этот отчёт уже обновляется.')
            if card['section']!='summary':
                return await self._answer(callback,'Сначала сверните открытый блок.')
            try:
                ctx=self.registry.get(card['shop_id']) if self.registry else self.context
                if ctx.shop_id!=card['shop_id']:raise ValueError('shop runtime unavailable')
            except ValueError:
                return await self._answer(callback,'Магазин недоступен. Откройте новый отчёт.',alert=True)
            if ctx.job_lock.locked() or ctx.shop_id in self._refreshing_shops:
                if callback is None:
                    card['status_note']='⏳ Уже выполняется другая загрузка этого магазина. Пока показаны сохранённые данные.'
                    await self._edit(message.bot,key,card,user_id)
                return await self._answer(callback,'Уже выполняется другая загрузка этого магазина. Повторите позже.',alert=True)
            self._refreshing.add(key)
            self._refreshing_shops.add(ctx.shop_id)
            day=date.fromisoformat(card['report_day'])
            try:
                await self._answer(callback,'Обновляю данные за '+day.strftime('%d.%m.%Y')+'…')
                card['status_note']='⏳ Обновляю данные за '+day.strftime('%d.%m.%Y')+'…'
                edited=await self._edit(message.bot,key,card,user_id)
            except BaseException:
                self._refreshing.discard(key)
                self._refreshing_shops.discard(ctx.shop_id)
                raise
            if not edited:
                self._refreshing.discard(key)
                self._refreshing_shops.discard(ctx.shop_id)
                await self._answer(callback,'Сообщение не удалось изменить. Откройте новый отчёт; загрузка не запускалась.',alert=True)
                return  # A deleted/uneditable card must not trigger an expensive API job.
        # The edit lock is deliberately free during API rate-limit waits.
        # Details/collapse can still work; completion uses the latest section.
        try:
            # A scheduler/backfill can acquire the shop lock while Telegram is
            # accepting the progress edit. Do not silently queue behind it.
            if ctx.job_lock.locked():
                await self._finish(message,key,user_id,None,
                    '⏳ Началась другая загрузка этого магазина. Повторите позже; пока показаны сохранённые данные.')
                return
            stages=await ctx.refresh_reports(day,day)
            active=[stage for stage in stages if not stage.skipped]
            if active:
                text=await prepare_daily_card(ctx,day)
                note='✅ Подключённые источники обновлены.' if all(stage.ok for stage in active) else (
                    '⚠️ Часть источников не обновилась. Показаны последние сохранённые данные; проверьте время в блоках.')
            else:
                text=None
                note='ℹ️ Учебные данные: запросы к API не выполнялись.' if any(stage.key=='demo' for stage in stages) else (
                    'ℹ️ Источники не подключены или не вернули операции. Сохранённые данные оставлены.')
            await self._finish(message,key,user_id,text,note)
        except asyncio.CancelledError:
            await self._finish(message,key,user_id,None,'⚠️ Обновление прервано. Можно повторить; сохранённые данные оставлены.')
            raise
        except Exception:
            log.exception('Daily card refresh failed for shop %s',card['shop_id'])
            await self._finish(message,key,user_id,None,'⚠️ Обновление не завершилось. Сохранённые данные оставлены; попробуйте позже.')
        finally:
            self._refreshing.discard(key)
            self._refreshing_shops.discard(ctx.shop_id)

    async def _finish(self, message, key, user_id, text, note):
        async with self._lock(key):
            card=self.context.repository.report_card(*key)
            if card is None:return
            if text is not None:card.update(asdict(text))
            card['status_note']=note
            await self._edit(message.bot,key,card,user_id)
