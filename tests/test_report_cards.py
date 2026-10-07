"""Exercise report cards with the real dispatcher and a local Telegram double."""
import asyncio
from dataclasses import asdict, replace
from datetime import date
from decimal import Decimal
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import Bot, Dispatcher, types
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendMessage, EditMessageText, AnswerCallbackQuery
import pytest

from app.bot import AppContext, register_handlers
from app.bot.keyboards import HOME, MENU_REPORTS
from app.bot.report_cards import DailyCardController, send_daily_card, send_daily_cards
from app.bot.runtime import ContextProxy, ShopContextMiddleware
from app.reports.cards import format_daily_card, render_daily_card, daily_card_pages
from app.reports.daily import DailyReport, MarketplaceDaily, build_daily_report
from app.services.buyer_prices import BuyerPriceTotals, CurrencyTotal
from app.services.collection import CollectionService
from app.services.currency import CurrencyRates, CurrencyQuote
from app.services.report_refresh import RefreshStage
from app.storage import Database, Repository, MetricPoint, LATEST_SCHEMA_VERSION

from test_navigation import ui, press, callback, active

DAY=date(2026,10,1)


def orders(ui, units=130, revenue=89503, *, day=DAY, revision=1):
    conn=ui.repo.ensure_connection(ui.shop.id,'wildberries','WB')
    ui.repo.record_success(conn.id,'analytics/orders',day.isoformat(),{'revision':revision},[
        MetricPoint(conn.id,day.isoformat(),'ordered_units',units,'units',True,'2026-10-02T06:00:00Z'),
        MetricPoint(conn.id,day.isoformat(),'ordered_revenue',revenue,'RUB',True,'2026-10-02T06:00:00Z')])
    return conn


async def card(ui, *, day=DAY, user=101):
    return await send_daily_card(ui.bot,ui.ctx,user,day,user_id=user)


def labels(message):
    return [b.text for row in message.reply_markup.inline_keyboard for b in row]


def stored(ui, message):
    return ui.repo.report_card(ui.bot.id,message.chat.id,message.message_id)


async def tap(ui, message, action, *, user=101):
    ui.update_id+=1
    current=ui.telegram.messages[(message.chat.id,message.message_id)]
    await ui.dp.feed_update(ui.bot,types.Update(update_id=ui.update_id,callback_query=types.CallbackQuery(
        id=str(ui.update_id),from_user=types.User(id=user,is_bot=False,first_name='User'),
        chat_instance='chat',message=current,data='daily_card:'+action)))


def preview(ui, prices):
    wb=orders(ui)
    oz=ui.repo.ensure_connection(ui.shop.id,'ozon','Ozon')
    report=DailyReport(DAY.isoformat(),(
        MarketplaceDaily('wildberries',wb.id,130,89503,None,None,True,'time',None,'analytics/orders'),
        MarketplaceDaily('ozon',oz.id,29,33649,None,None,True,'time',None,'analytics/orders',buyer_prices=prices)))
    return format_daily_card(ui.repo,ui.shop.id,report)


def buyer_prices():
    rates=CurrencyRates(DAY.isoformat(),DAY.isoformat(),(CurrencyQuote('BYN',1,Decimal('27.6621')),))
    return BuyerPriceTotals((CurrencyTotal('RUB',Decimal('14663.15'),28),CurrencyTotal('BYN',Decimal('14.98'),1)),
                           29,29,29,True,'time',rates=rates)


def test_summary_uses_agreed_money_bases_and_original_currencies_in_details(ui):
    text=preview(ui,buyer_prices())
    assert 'Всего заказано: <b>159 шт. · ≈ 104 580,53 ₽</b>' in text.summary_html
    assert '29 шт. · ≈ 15 077,53 ₽' in text.summary_html
    assert '130 шт. · 89 503 ₽' in text.summary_html
    assert text.summary_html.index('🔵 Ozon')<text.summary_html.index('🟣 Wildberries')
    assert '33649' not in text.summary_html and '33 649' not in text.summary_html
    assert '33 649,00 ₽' in text.details_html and '14,98 BYN' in text.details_html
    assert '1 BYN = 27,6621 ₽' in text.details_html
    assert '4,2%' not in text.summary_html


@pytest.mark.parametrize('change',[{'complete':False},{'rates':None}])
def test_missing_prices_or_rates_never_become_full_ruble_total(ui,change):
    text=preview(ui,replace(buyer_prices(),**change))
    ozon=text.summary_html.split('🟣 Wildberries')[0]
    assert '15 077' not in ozon and '33 649' not in ozon
    assert '⏳' in ozon and '14,98 BYN' in text.details_html
    assert 'Всего заказано: <b>159 шт.</b> · ⏳ сумма неполная.' in text.summary_html


def test_complete_rub_prices_and_real_zero_are_not_approximate_or_missing(ui):
    prices=BuyerPriceTotals((CurrencyTotal('RUB',Decimal(0),29),),29,29,29,True,None)
    text=preview(ui,prices)
    assert '29 шт. · 0,00 ₽' in text.summary_html and '≈' not in text.summary_html
    assert 'Всего заказано: <b>159 шт. · 89 503,00 ₽</b>' in text.summary_html


def test_order_total_uses_buyer_prices_without_predelnaya_or_net_substitution(ui):
    prices=BuyerPriceTotals((CurrencyTotal('RUB',Decimal('14663.15'),29),),29,29,29,True,None)
    text=preview(ui,prices)
    assert 'Всего заказано: <b>159 шт. · 104 166,15 ₽</b>' in text.summary_html
    assert '123 152' not in text.summary_html  # WB + Ozon's maximum API price is a different basis.
    assert 'Общий итог — сумма показанных цен' in text.details_html


@pytest.mark.parametrize('wb_amount,ozon_amount,total',[(0,0,'0,00'),(.1,'0.2','0,30'),(.005,'0.005','0,02')])
def test_order_total_matches_displayed_kopecks_including_zero(ui,wb_amount,ozon_amount,total):
    wb=orders(ui,units=1,revenue=wb_amount);oz=ui.repo.ensure_connection(ui.shop.id,'ozon','Ozon')
    prices=BuyerPriceTotals((CurrencyTotal('RUB',Decimal(str(ozon_amount)),1),),1,1,1,True,None)
    report=DailyReport(DAY.isoformat(),(
        MarketplaceDaily('wildberries',wb.id,1,wb_amount,None,None,True,None,None),
        MarketplaceDaily('ozon',oz.id,1,999,None,None,True,None,None,buyer_prices=prices)))
    text=format_daily_card(ui.repo,ui.shop.id,report)
    assert f'Всего заказано: <b>2 шт. · {total} ₽</b>' in text.summary_html


def test_no_total_when_wb_amount_missing_even_with_complete_units(ui):
    wb=orders(ui);oz=ui.repo.ensure_connection(ui.shop.id,'ozon','Ozon')
    prices=BuyerPriceTotals((CurrencyTotal('RUB',Decimal(100),1),),1,1,1,True,None)
    report=DailyReport(DAY.isoformat(),(
        MarketplaceDaily('wildberries',wb.id,1,None,None,None,True,None,None),
        MarketplaceDaily('ozon',oz.id,1,999,None,None,True,None,None,buyer_prices=prices)))
    text=format_daily_card(ui.repo,ui.shop.id,report)
    assert 'Всего заказано: <b>2 шт.</b> · ⏳ сумма неполная.' in text.summary_html


def test_order_total_for_a_single_connected_marketplace(ui):
    orders(ui,units=3,revenue=100.49)
    text=format_daily_card(ui.repo,ui.shop.id,build_daily_report(ui.repo,ui.shop.id,DAY))
    assert 'Всего заказано: <b>3 шт. · 100,49 ₽</b>' in text.summary_html


def test_unconnected_shop_has_no_invented_zero_total(ui):
    text=format_daily_card(ui.repo,ui.shop.id,DailyReport(DAY.isoformat(),()))
    assert 'Всего заказано: ⏳ неполные данные.' in text.summary_html
    assert '0,00 ₽' not in text.summary_html


def test_missing_enabled_market_does_not_turn_available_units_into_total(ui):
    orders(ui)
    ui.repo.ensure_connection(ui.shop.id,'ozon','Ozon')
    text=format_daily_card(ui.repo,ui.shop.id,build_daily_report(ui.repo,ui.shop.id,DAY))
    assert 'Всего заказано: ⏳ неполные данные' in text.summary_html
    assert 'Всего заказано: <b>130' not in text.summary_html
    assert '⏳ количество не загружено' in text.summary_html
    assert '0,00 ₽' not in text.accruals_html


def test_wb_kopecks_are_preserved_when_the_source_has_a_fraction(ui):
    orders(ui,revenue=89503.49)
    text=format_daily_card(ui.repo,ui.shop.id,build_daily_report(ui.repo,ui.shop.id,DAY))
    assert '89 503,49 ₽' in text.summary_html


@pytest.mark.parametrize('section',['details','accruals'])
@pytest.mark.asyncio
async def test_expand_and_collapse_edit_one_message_keep_snapshot_and_only_collapse_button(ui,section):
    orders(ui); message=await card(ui); summary=message.text
    # A background collection must not silently change an opened card.
    orders(ui,units=999,revenue=999999,revision=2)
    ui.telegram.methods.clear()
    await tap(ui,message,section)
    expanded=ui.telegram.messages[(101,message.message_id)]
    assert expanded.text.startswith(summary+'\n\n') and '999 шт.' not in expanded.text
    assert labels(expanded)==['Свернуть','🗓 Выбрать дату',HOME] and stored(ui,message)['section']==section
    await tap(ui,message,'collapse')
    collapsed=ui.telegram.messages[(101,message.message_id)]
    assert collapsed.text==summary
    assert labels(collapsed)==['Подробнее','Начисления','Обновить','🗓 Выбрать дату',HOME]
    assert sum(isinstance(m,EditMessageText) for m in ui.telegram.methods)==2
    assert not any(isinstance(m,SendMessage) for m in ui.telegram.methods)


@pytest.mark.asyncio
async def test_open_block_cannot_switch_or_refresh_until_collapsed(ui):
    message=await card(ui)
    await tap(ui,message,'details')
    ui.ctx.refresh_reports=AsyncMock()
    await tap(ui,message,'accruals'); await tap(ui,message,'refresh')
    assert stored(ui,message)['section']=='details'
    ui.ctx.refresh_reports.assert_not_awaited()


def test_finance_keeps_net_accruals_and_goods_payable_and_wb_period_distinct(ui):
    wb=orders(ui); oz=ui.repo.ensure_connection(ui.shop.id,'ozon','Ozon')
    ui.repo.record_success(oz.id,'finance/accrual/by-day',DAY.isoformat(),{'net':15313.65},[
        MetricPoint(oz.id,DAY.isoformat(),'marketplace_net',15313.65,'RUB')])
    ui.repo.record_success(wb.id,'finance/sales-reports/list',DAY.isoformat(),{
        'reports':[{'dateFrom':'2026-09-25','dateTo':DAY.isoformat(),'bankPaymentSum':70000}]},[
        MetricPoint(wb.id,DAY.isoformat(),'bank_payment',70000,'RUB'),
        MetricPoint(wb.id,DAY.isoformat(),'goods_payable',80000,'RUB')])
    text=preview(ui,buyer_prices())
    assert '15 313,65 ₽' in text.accruals_html and '15 313' not in text.summary_html
    assert 'Итог к оплате по фин. отчёту: <b>70 000,00 ₽</b>' in text.accruals_html
    assert 'К перечислению за товар: 80 000,00 ₽' in text.accruals_html
    assert '25.09.2026 — 01.10.2026' in text.accruals_html
    assert 'не подтверждение банковского перевода' in text.accruals_html
    assert 'себестоимость и налоги' in text.accruals_html


def test_wb_goods_payable_alone_is_not_final_amount_or_known_period(ui):
    wb=orders(ui)
    ui.repo.record_success(wb.id,'finance/sales-reports/list',DAY.isoformat(),{},[
        MetricPoint(wb.id,DAY.isoformat(),'goods_payable',500,'RUB')])
    text=format_daily_card(ui.repo,ui.shop.id,build_daily_report(ui.repo,ui.shop.id,DAY))
    assert 'Итог к оплате по фин. отчёту WB: ⏳' in text.accruals_html
    assert 'К перечислению за товар: 500,00 ₽' in text.accruals_html
    assert 'Период фин. отчёта не указан' in text.accruals_html


@pytest.mark.asyncio
async def test_refresh_uses_card_date_edits_values_without_new_messages_or_changing_buttons(ui):
    orders(ui); message=await card(ui)
    async def refresh(start,end):
        assert start==end==DAY
        during=ui.telegram.messages[(101,message.message_id)]
        assert labels(during)==['Подробнее','Начисления','Обновить','🗓 Выбрать дату',HOME] and 'Обновляю' in during.text
        orders(ui,units=131,revenue=90000,revision=2)
        return (RefreshStage('orders','Заказы WB',True),)
    ui.ctx.refresh_reports=AsyncMock(side_effect=refresh)
    ui.telegram.methods.clear()
    await tap(ui,message,'refresh')
    result=ui.telegram.messages[(101,message.message_id)]
    assert '131 шт. · 90 000 ₽' in result.text and '01.10.2026' in result.text
    assert labels(result)==['Подробнее','Начисления','Обновить','🗓 Выбрать дату',HOME]
    assert not any(isinstance(m,SendMessage) for m in ui.telegram.methods)
    ui.ctx.refresh_reports.assert_awaited_once_with(DAY,DAY)


@pytest.mark.parametrize('collapse',[True,False])
@pytest.mark.asyncio
async def test_duplicate_refresh_does_not_collect_twice_and_view_changes_work_during_wait(ui,collapse):
    orders(ui); message=await card(ui)
    entered=asyncio.Event(); finish=asyncio.Event()
    async def refresh(*args):
        entered.set(); await finish.wait()
        orders(ui,units=131,revision=2)
        return (RefreshStage('orders','Заказы WB',True),)
    ui.ctx.refresh_reports=AsyncMock(side_effect=refresh)
    task=asyncio.create_task(tap(ui,message,'refresh'))
    try:
        await asyncio.wait_for(entered.wait(),2)
        await tap(ui,message,'refresh')
        await tap(ui,message,'details')
        if collapse:await tap(ui,message,'collapse')
        finish.set(); await asyncio.wait_for(task,2)
    finally:
        finish.set()
        if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
    ui.ctx.refresh_reports.assert_awaited_once()
    result=ui.telegram.messages[(101,message.message_id)]
    assert stored(ui,message)['section']==('summary' if collapse else 'details')
    assert labels(result)==(['Подробнее','Начисления','Обновить','🗓 Выбрать дату',HOME] if collapse else ['Свернуть','🗓 Выбрать дату',HOME])
    assert '131 шт.' in result.text


@pytest.mark.asyncio
async def test_viewer_can_expand_but_cannot_refresh_even_with_forged_callback(ui):
    orders(ui); message=await card(ui,user=102)
    assert labels(message)==['Подробнее','Начисления','🗓 Выбрать дату',HOME]
    ui.ctx.refresh_reports=AsyncMock()
    await tap(ui,message,'details',user=102); await tap(ui,message,'collapse',user=102)
    await tap(ui,message,'refresh',user=102)
    ui.ctx.refresh_reports.assert_not_awaited()
    assert labels(ui.telegram.messages[(102,message.message_id)])==['Подробнее','Начисления','🗓 Выбрать дату',HOME]
    assert any(isinstance(m,AnswerCallbackQuery) and m.show_alert for m in ui.telegram.methods)


@pytest.mark.asyncio
async def test_card_keeps_shop_identity_after_switch_and_checks_access_to_that_shop(ui):
    orders(ui); message=await card(ui)
    other=ui.repo.ensure_shop(ui.shop.seller_id,'Other'); ui.repo.ensure_shop_preferences(other.id)
    ui.repo.grant_shop_access(101,other.id,'owner'); ui.repo.grant_shop_access(103,other.id,'owner')
    other_ctx=AppContext(ui.ctx.settings,ui.repo,other.id,CollectionService(ui.repo))
    contexts={ui.shop.id:ui.ctx,other.id:other_ctx}
    async def context_for_user(uid):
        return contexts[ui.repo.selected_authorized_shop_for_user(uid,ui.shop.id)]
    registry=SimpleNamespace(maintenance_lock=asyncio.Lock(),context_for_user=context_for_user,get=contexts.__getitem__)
    dp=Dispatcher(); middleware=ShopContextMiddleware(registry)
    dp.message.middleware(middleware);dp.callback_query.middleware(middleware)
    register_handlers(dp,ContextProxy(),registry);ui.dp=dp
    ui.repo.select_authorized_shop_for_user(101,other.id)
    ui.ctx.refresh_reports=AsyncMock(return_value=(RefreshStage('orders','WB',True),))
    other_ctx.refresh_reports=AsyncMock()
    await tap(ui,message,'refresh')
    ui.ctx.refresh_reports.assert_awaited_once_with(DAY,DAY)
    other_ctx.refresh_reports.assert_not_awaited()
    assert stored(ui,message)['shop_id']==ui.shop.id and 'Shop' in stored(ui,message)['summary_html']
    before=ui.telegram.messages[(101,message.message_id)].text
    await tap(ui,message,'details',user=103);await tap(ui,message,'refresh',user=103)
    assert ui.telegram.messages[(101,message.message_id)].text==before
    ui.ctx.refresh_reports.assert_awaited_once()


@pytest.mark.asyncio
async def test_restart_preserves_expanded_card_and_menu_cleanup_never_removes_report(ui):
    orders(ui); message=await card(ui)
    await tap(ui,message,'accruals')
    ui.dp=ui.new_dispatcher()
    await tap(ui,message,'collapse')
    await press(ui,HOME); await press(ui,MENU_REPORTS)
    assert (101,message.message_id) in ui.telegram.messages
    menu=active(ui)
    await tap(ui,message,'details')
    assert active(ui)==menu and (101,menu) in ui.telegram.messages


@pytest.mark.parametrize('error',['network','deleted','not_modified'])
@pytest.mark.asyncio
async def test_edit_failure_leaves_card_retryable_and_does_not_send_duplicate(ui,error):
    orders(ui); message=await card(ui); original=ui.telegram.request
    async def fail(bot,method,**kwargs):
        if isinstance(method,EditMessageText):
            if error=='network':raise TelegramNetworkError(method,'offline')
            raise TelegramBadRequest(method,'message is not modified' if error=='not_modified' else 'message to edit not found')
        return await original(bot,method,**kwargs)
    ui.bot.session.make_request=AsyncMock(side_effect=fail);ui.telegram.methods.clear()
    await tap(ui,message,'details')
    assert stored(ui,message)['section']==('details' if error=='not_modified' else 'summary')
    assert not any(isinstance(m,SendMessage) for m in ui.telegram.methods)
    ui.bot.session.make_request=AsyncMock(side_effect=original)
    await tap(ui,message,'collapse' if error=='not_modified' else 'details')


@pytest.mark.asyncio
async def test_deleted_card_does_not_start_api_job(ui):
    message=await card(ui);original=ui.telegram.request;ui.ctx.refresh_reports=AsyncMock()
    async def fail(bot,method,**kwargs):
        if isinstance(method,EditMessageText):raise TelegramBadRequest(method,'message to edit not found')
        return await original(bot,method,**kwargs)
    ui.bot.session.make_request=AsyncMock(side_effect=fail)
    await tap(ui,message,'refresh')
    ui.ctx.refresh_reports.assert_not_awaited()


@pytest.mark.parametrize('mode',['partial','exception','empty','demo'])
@pytest.mark.asyncio
async def test_failed_or_skipped_refresh_keeps_values_and_does_not_claim_complete_success(ui,mode):
    orders(ui); message=await card(ui)
    results={'partial':(RefreshStage('orders','WB',False),),'empty':(),
             'demo':(RefreshStage('demo','Демо',True,True),)}
    ui.ctx.refresh_reports=AsyncMock(side_effect=RuntimeError('secret=do-not-display')) if mode=='exception' else (
        AsyncMock(return_value=results[mode]))
    await tap(ui,message,'refresh')
    result=ui.telegram.messages[(101,message.message_id)]
    assert '130 шт. · 89 503 ₽' in result.text
    assert '✅ Подключённые источники обновлены.' not in result.text and 'do-not-display' not in result.text
    # The in-process guard is released after every result.
    await tap(ui,message,'refresh')
    assert ui.ctx.refresh_reports.await_count==2


@pytest.mark.asyncio
async def test_busy_shop_does_not_queue_refresh_or_edit_report(ui):
    message=await card(ui);ui.ctx.refresh_reports=AsyncMock();ui.telegram.methods.clear()
    async with ui.ctx.job_lock:await tap(ui,message,'refresh')
    ui.ctx.refresh_reports.assert_not_awaited()
    assert not any(isinstance(m,EditMessageText) for m in ui.telegram.methods)


@pytest.mark.asyncio
async def test_manual_forced_day_is_one_card_and_refreshes_its_explicit_date(ui):
    orders(ui)
    ui.ctx.refresh_reports=AsyncMock(return_value=(RefreshStage('orders','WB',True),))
    ui.telegram.methods.clear()
    await press(ui,'/day 2026-10-01')
    sends=[m for m in ui.telegram.methods if isinstance(m,SendMessage)]
    assert len(sends)==1 and '01.10.2026' in sends[0].text
    ui.ctx.refresh_reports.assert_awaited_once_with(DAY,DAY)
    assert sum(isinstance(m,EditMessageText) for m in ui.telegram.methods)==2


@pytest.mark.asyncio
async def test_date_picker_reads_saved_orders_without_marketplace_api_calls(ui):
    orders(ui)
    ui.ctx.collect_day=AsyncMock();ui.ctx.collect_buyer_prices=AsyncMock();ui.ctx.refresh_reports=AsyncMock()
    await press(ui,'🗓 Другая дата')
    await callback(ui,active(ui),'report_date:custom')
    await press(ui,DAY.isoformat())
    result=ui.telegram.messages[(101,ui.telegram.sequence)]
    assert '130 шт. · 89 503 ₽' in result.text
    ui.ctx.collect_day.assert_not_awaited()
    ui.ctx.collect_buyer_prices.assert_not_awaited()
    ui.ctx.refresh_reports.assert_not_awaited()


@pytest.mark.asyncio
async def test_interrupted_refresh_can_be_retried_and_keeps_last_data(ui):
    orders(ui);message=await card(ui);entered=asyncio.Event()
    async def wait(*args):
        entered.set();await asyncio.Event().wait()
    ui.ctx.refresh_reports=AsyncMock(side_effect=wait)
    task=asyncio.create_task(tap(ui,message,'refresh'))
    await asyncio.wait_for(entered.wait(),2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert 'прервано' in stored(ui,message)['status_note']
    assert '130 шт.' in stored(ui,message)['summary_html']
    ui.ctx.refresh_reports=AsyncMock(return_value=(RefreshStage('orders','WB',True),))
    await tap(ui,message,'refresh')
    ui.ctx.refresh_reports.assert_awaited_once()


@pytest.mark.asyncio
async def test_two_cards_cannot_start_two_shop_refreshes_during_telegram_wait(ui):
    orders(ui);first=await card(ui);second=await card(ui)
    original=ui.telegram.request;entered=asyncio.Event();finish=asyncio.Event()
    async def delayed(bot,method,**kwargs):
        if isinstance(method,EditMessageText) and method.message_id==first.message_id and 'Обновляю' in method.text:
            entered.set();await finish.wait()
        return await original(bot,method,**kwargs)
    ui.bot.session.make_request=AsyncMock(side_effect=delayed)
    ui.ctx.refresh_reports=AsyncMock(return_value=(RefreshStage('orders','WB',True),))
    task=asyncio.create_task(tap(ui,first,'refresh'))
    try:
        await asyncio.wait_for(entered.wait(),2)
        await tap(ui,second,'refresh')
        ui.ctx.refresh_reports.assert_not_awaited()
        assert stored(ui,second)['status_note']==''
        finish.set();await asyncio.wait_for(task,2)
    finally:
        finish.set()
        if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
    ui.ctx.refresh_reports.assert_awaited_once()


@pytest.mark.asyncio
async def test_job_that_starts_during_progress_edit_is_not_followed_by_queued_refresh(ui):
    message=await card(ui);original=ui.telegram.request
    async def start_job(bot,method,**kwargs):
        if isinstance(method,EditMessageText) and 'Обновляю' in method.text:
            await ui.ctx.job_lock.acquire()
        return await original(bot,method,**kwargs)
    ui.bot.session.make_request=AsyncMock(side_effect=start_job)
    ui.ctx.refresh_reports=AsyncMock()
    try:await tap(ui,message,'refresh')
    finally:ui.ctx.job_lock.release()
    ui.ctx.refresh_reports.assert_not_awaited()
    assert 'другая загрузка' in stored(ui,message)['status_note']


@pytest.mark.asyncio
async def test_card_callbacks_are_scoped_to_bot_chat_message_and_unknown_cards_are_safe(ui):
    message=await card(ui);controller=DailyCardController(ui.ctx)
    other_bot=Bot('654321:LOCAL_TEST');other_bot.session.make_request=ui.bot.session.make_request
    for bot,chat,message_id in ((other_bot,101,message.message_id),(ui.bot,999,message.message_id),
                                (ui.bot,101,99999)):
        msg=message.model_copy(update={'chat':types.Chat(id=chat,type='private'),'message_id':message_id}).as_(bot)
        cb=types.CallbackQuery(id='scope',from_user=types.User(id=101,is_bot=False,first_name='User'),
                              chat_instance='chat',message=msg,data='daily_card:details').as_(bot)
        await controller.handle(cb)
    assert stored(ui,message)['section']=='summary'
    await tap(ui,message,'unknown')
    assert stored(ui,message)['section']=='summary'


def test_long_lower_block_is_one_valid_html_message_and_main_is_unchanged(ui):
    text=preview(ui,buyer_prices());data={**asdict(text),'section':'details','status_note':''}
    data['details_html']='🔎 <b>Подробнее</b>\n'+('\n'.join('<b>Товар 😀 &amp; склад</b>' for _ in range(2000)))
    output=render_daily_card(data)
    assert len(output.encode('utf-16-le'))//2<=3900
    assert output.startswith(text.summary_html+'\n\n') and 'Страница 1/' in output
    pages=daily_card_pages(data)
    assert len(pages)>1 and all(len(page.encode('utf-16-le'))//2<=3900 for page in pages)
    assert sum(page.count('<b>Товар 😀 &amp; склад</b>') for page in pages)==2000
    assert all(page.startswith(text.summary_html+'\n\n') for page in pages)
    class Tags(HTMLParser):
        def __init__(self):super().__init__();self.stack=[]
        def handle_starttag(self,tag,attrs):self.stack.append(tag)
        def handle_endtag(self,tag):assert self.stack.pop()==tag
    parser=Tags();parser.feed(output);assert parser.stack==[]


@pytest.mark.asyncio
async def test_scheduler_sender_prepares_one_snapshot_and_sends_one_card_to_each_role(ui,monkeypatch):
    import app.bot.report_cards as module
    orders(ui);prepare=AsyncMock(wraps=module.prepare_daily_card)
    monkeypatch.setattr(module,'prepare_daily_card',prepare)
    ui.telegram.methods.clear()
    await send_daily_cards(ui.bot,ui.ctx,DAY)
    sends=[m for m in ui.telegram.methods if isinstance(m,SendMessage)]
    assert len(sends)==2 and {m.chat_id for m in sends}=={101,102}
    prepare.assert_awaited_once()
    for method in sends:
        assert [b.text for row in method.reply_markup.inline_keyboard for b in row]==(
            ['Подробнее','Начисления','Обновить','🗓 Выбрать дату',HOME] if method.chat_id==101 else ['Подробнее','Начисления','🗓 Выбрать дату',HOME])


def test_schema17_upgrade_keeps_navigation_and_creates_persistent_report_snapshots(tmp_path):
    from app.storage.database import MIGRATIONS
    db=Database(tmp_path/'schema17.sqlite3')
    with db.connect() as connection:
        connection.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)')
        for version,migration in MIGRATIONS.items():
            if version>17:continue
            migration(connection)
            connection.execute("INSERT INTO schema_migrations VALUES(?,datetime('now'))",(version,))
    repo=Repository(db);shop=repo.ensure_shop(repo.ensure_seller(101).id)
    repo.save_navigation_message(123,101,101,10)
    assert db.initialize_safely(tmp_path/'backups')==LATEST_SCHEMA_VERSION
    assert list((tmp_path/'backups').glob('pre_migration_v17_*.sqlite3'))
    assert repo.navigation_message(123,101,101)==10
    repo.save_report_card(123,101,20,shop_id=shop.id,report_day=DAY.isoformat(),
                          summary_html='main',details_html='details',accruals_html='finance')
    assert Repository(Database(db.path)).report_card(123,101,20)['summary_html']=='main'
