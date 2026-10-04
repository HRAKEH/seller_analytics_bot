"""Regression checks for the approved 1–4 and 6 changes (roles stay unchanged)."""
from dataclasses import replace
from datetime import date, datetime, timezone
from html import escape
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import Bot, Dispatcher, types
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, EditMessageText, SendMessage
from aiogram.exceptions import TelegramBadRequest
import pytest

from app.bot import AppContext, register_handlers
from app.bot.keyboards import action_ref
from app.config import Settings
from app.integrations.base import FetchResult
from app.reports.alerts import format_active_alerts, format_alert_detail, sorted_alerts
from app.reports.ads import AdvertisingReport, AdRow, format_advertising
from app.reports.dates import readable_text
from app.reports.operations import format_action_center
from app.reports.products import ProductReport, StockRisk
from app.reports.text import utf16_length
from app.services.actions import ActionCenter, ActionItem
from app.services.collection import CollectionService
from app.services.promotions import wb_promotion_finished
from app.services.resilience import execute_retry_job
from app.storage import Database, Repository

DAY = date(2026, 10, 4)


@pytest.fixture
def data(tmp_path):
    db=Database(tmp_path/'approved-ux.sqlite3'); db.initialize()
    repo=Repository(db)
    seller=repo.ensure_seller(101,'Seller')
    shop=repo.ensure_shop(seller.id,'Shop'); repo.ensure_shop_preferences(shop.id)
    repo.grant_shop_access(101,shop.id,'owner')
    repo.grant_shop_access(102,shop.id,'viewer')
    wb=repo.ensure_connection(shop.id,'wildberries','WB')
    return repo, shop, wb


def test_dates_convert_instants_to_shop_zone_but_keep_api_examples():
    text='Проверено 2026-10-03T23:09:52.001+00:00 · период 2026-10-03.\n<code>/day 2026-10-03</code> https://example.test/2026-10-03'
    result=readable_text(text,tz='Asia/Yekaterinburg')
    assert '04.10.2026 04:09' in result and 'период 03.10.2026.' in result
    assert '<code>/day 2026-10-03</code>' in result
    assert readable_text('Пример: /finance 1 2026-10-03')=='Пример: /finance 1 2026-10-03'
    assert 'https://example.test/2026-10-03' in result
    assert readable_text('2026-10-03 23:09:00',tz='Europe/Kirov')=='04.10.2026 02:09'


def test_ad_report_articles_use_inline_code_and_keep_marketplace_labels():
    report=AdvertisingReport('2026-10-03','2026-10-03',1,(),(
        AdRow('wildberries','1001','Подходит для Ozon',10,20,1,2,3),
        AdRow('ozon','WB-1002 & 01.','Название для WB',10,20,1,2,3)))
    text=format_advertising(report)
    assert '🔵 WB · <b>Подходит для Ozon</b> · SKU <code>1001</code>' in text
    assert '🟣 Ozon · <b>Название для WB</b> · SKU <code>WB-1002 &amp; 01.</code>' in text


def test_alert_pages_prioritize_empty_stock_and_keep_details_separate(data):
    repo,shop,_=data
    risks=[]
    for index in range(12):
        sku=str(1000+index)
        quantity=0 if index==11 else index+1
        risks.append(StockRisk('wildberries','Название товара '+sku,sku,quantity,0,1,quantity,14,
            '2026-10-04T06:09:00+00:00'))
        repo.save_alert_state(shop.id,'low_stock','wildberries:'+sku,active=True,value=quantity,fingerprint=sku)
    report=ProductReport(DAY,DAY,1,stock_risks=risks)
    first=format_active_alerts(repo,shop.id,report)
    assert 'страница 1/3' in first and first.count('Остаток:')==5
    assert first.index('1011') < first.index('1000')
    assert 'Среднее:' not in first and 'Снимок API:' not in first
    last=format_active_alerts(repo,shop.id,report,page=999)
    assert 'страница 3/3' in last and last.count('Остаток:')==2
    detail=format_alert_detail(sorted_alerts(repo,shop.id,report)[0],report,timezone='Europe/Kirov')
    assert '04.10.2026 09:09' in detail and 'Расчёт:' in detail and '0 шт.' in detail


def test_action_list_warns_once_even_with_old_saved_disclaimers():
    warning='Это рекомендация по прогнозу, проверьте перед закупкой.'
    items=tuple(ActionItem(str(index),1,'supply','Пополнить товар '+str(index),'WB · '+warning) for index in range(11))
    text=format_action_center(ActionCenter(1,DAY,items),page=1)
    assert warning in text and text.count(warning)==1
    assert 'страница 2/3' in text and '6.' in text and '10.' in text and '11.' not in text
    long=tuple(ActionItem(str(index),1,'supply','😀<&'*1000,'🟠<&'*1000) for index in range(15))
    assert utf16_length(format_action_center(ActionCenter(1,DAY,long)))<3900


@pytest.fixture
def ui(data,monkeypatch):
    repo,shop,_=data
    settings=replace(Settings.from_env(),telegram_token='123456:LOCAL_TEST',owner_ids=(101,))
    ctx=AppContext(settings,repo,shop.id,CollectionService(repo))
    items=tuple(ActionItem('supply:'+str(index),1,'supply','Пополнить товар '+str(index),
        'WB · запас 2 дн.',evidence=('остаток 3 шт.','прогноз 1.50 шт./день'),
        articles=(('Внутренний','sku-'+str(index)),)) for index in range(12))
    def center(repository,shop_id,day,*,persist=False):
        if persist:
            repository.sync_action_center(shop_id,day.isoformat(),[{'action_key':x.action_key,'priority':x.priority,
                'category':x.category,'title':x.title,'detail':x.detail,'evidence':list(x.evidence)} for x in items])
        states={row['action_key']:row for row in repository.action_states(shop_id,include_resolved=False)}
        visible=tuple(replace(item,status=states.get(item.action_key,{}).get('status','open')) for item in items
                      if states.get(item.action_key,{}).get('status')!='snoozed')
        return ActionCenter(shop_id,day,visible,snoozed_count=len(items)-len(visible))
    monkeypatch.setattr('app.bot.operational_cards.build_action_center',center)
    calls=[]
    bot=Bot(settings.telegram_token)
    async def record(bot,method,**kwargs):
        calls.append(method)
        if isinstance(method,(AnswerCallbackQuery,DeleteMessage)):return True
        assert isinstance(method,(SendMessage,EditMessageText))
        return types.Message(message_id=getattr(method,'message_id',None) or 900,date=datetime.now(timezone.utc),
            chat=types.Chat(id=method.chat_id,type='private'),text=method.text)
    bot.session.make_request=AsyncMock(side_effect=record)
    dp=Dispatcher(); register_handlers(dp,ctx)
    return dp,bot,calls,ctx


async def feed(ui,text,*,user=101,callback=False):
    dp,bot,calls,ctx=ui
    message=types.Message(message_id=900 if callback else 1,date=datetime.now(timezone.utc),
        chat=types.Chat(id=user,type='private'),from_user=types.User(id=user,is_bot=False,first_name='Test'),text=text)
    if callback:
        message=message.model_copy(update={'from_user':types.User(id=bot.id,is_bot=True,first_name='Bot')})
        update=types.Update(update_id=len(calls)+1,callback_query=types.CallbackQuery(id='click-'+str(len(calls)),
            from_user=types.User(id=user,is_bot=False,first_name='Test'),chat_instance='test',message=message,data=text))
    else:
        update=types.Update(update_id=len(calls)+1,message=message)
    await dp.feed_update(bot,update)


@pytest.mark.asyncio
async def test_real_callbacks_edit_same_message_and_return_to_previous_page(ui):
    await feed(ui,'/actions')
    await feed(ui,f'ops:actions:list:{ui[3].shop_id}:1',callback=True)
    ref=action_ref('supply:3')  # Lexical priority ordering: 0,1,10,11,2 then 3.
    await feed(ui,f'ops:actions:view:{ui[3].shop_id}:1:{ref}',callback=True)
    await feed(ui,f'ops:actions:list:{ui[3].shop_id}:1',callback=True)
    sent=[method for method in ui[2] if isinstance(method,SendMessage)]
    edits=[method for method in ui[2] if isinstance(method,EditMessageText)]
    assert len(sent)==1 and len(edits)==3 and {method.message_id for method in edits}=={900}
    assert 'Почему и как посчитано:' in edits[1].text
    assert 'страница 2/3' in edits[2].text
    assert '<code>sku-3</code>' in edits[1].text
    assert not any(button.copy_text for row in edits[1].reply_markup.inline_keyboard for button in row)
    await ui[1].session.close()


@pytest.mark.asyncio
async def test_viewer_can_read_and_return_but_cannot_acknowledge(ui):
    ref=action_ref('supply:3'); shop=ui[3].shop_id
    await feed(ui,f'ops:actions:view:{shop}:1:{ref}',user=102,callback=True)
    detail=next(method for method in ui[2] if isinstance(method,EditMessageText))
    callbacks=[button.callback_data for row in detail.reply_markup.inline_keyboard for button in row if button.callback_data]
    assert callbacks==[f'ops:actions:list:{shop}:1']
    count=len([method for method in ui[2] if isinstance(method,EditMessageText)])
    await feed(ui,f'ops:actions:ack:{shop}:1:{ref}',user=102,callback=True)
    assert len([method for method in ui[2] if isinstance(method,EditMessageText)])==count
    assert ui[2][-1].show_alert and 'прав' in ui[2][-1].text
    await ui[1].session.close()


@pytest.mark.asyncio
async def test_accept_updates_detail_and_snooze_clamps_last_page(ui):
    await feed(ui,'/actions')
    shop=ui[3].shop_id; ref=action_ref('supply:3')
    await feed(ui,f'ops:actions:ack:{shop}:1:{ref}',callback=True)
    assert '✅ принято' in [method for method in ui[2] if isinstance(method,EditMessageText)][-1].text
    await feed(ui,f'ops:actions:snooze:{shop}:99:{ref}',callback=True)
    text=[method for method in ui[2] if isinstance(method,EditMessageText)][-1].text
    assert 'страница 3/3' in text and 'Отложено и скрыто: 1' in text
    await ui[1].session.close()


@pytest.mark.asyncio
async def test_old_shop_callback_and_revoked_access_do_not_read_new_shop(ui):
    shop=ui[3].shop_id
    await feed(ui,f'ops:actions:list:{shop}:0',user=103,callback=True)
    assert not any(isinstance(method,EditMessageText) for method in ui[2])
    ui[3].shop_id=shop+1
    await feed(ui,f'ops:actions:list:{shop}:0',callback=True)
    assert not any(isinstance(method,EditMessageText) for method in ui[2])
    assert 'Магазин недоступен' in ui[2][-1].text
    await ui[1].session.close()


@pytest.mark.parametrize('end,finished',[
    ('2026-10-03T23:59:59Z',True),('2026-10-04T00:00:00Z',False),('2026-10-04T21:00:00Z',False),
    ('2026-10-04T01:00:00+03:00',True),('2026-12-31',False),('unknown',False),(None,False)])
def test_promotion_end_boundaries(end,finished):
    assert wb_promotion_finished({'endDateTime':end},DAY) is finished


@pytest.mark.asyncio
async def test_finished_promo_history_and_products_survive_without_api_request(data):
    repo,shop,wb=data
    old={'id':2739,'name':'Завершённая','type':'regular','startDateTime':'2026-06-01','endDateTime':'2026-08-17T20:59:59Z'}
    current={'id':2766,'name':'Новинки','type':'regular','endDateTime':'2026-12-31T20:59:59Z'}
    run=repo.record_success(wb.id,'calendar/promotions','2026-08-01',{},[])
    repo.upsert_promotions(wb.id,run,'wildberries',[{'external_promotion_id':'2739','name':'Завершённая',
        'end_at':old['endDateTime'],'products_complete':True,'products':[{'marketplace_sku':'1001','in_action':True}]}])
    client=SimpleNamespace(calendar_promotions_all=AsyncMock(return_value=FetchResult.success('wildberries',
        {'data':{'promotions':[old,current]}},200,1)),calendar_promotion_products_all=AsyncMock(return_value=
        FetchResult.success('wildberries',{'data':{'nomenclatures':[]}},200,1)))
    result=await CollectionService(repo,wildberries=client).collect_promotions(shop_id=shop.id,as_of=DAY,wb_connection_id=wb.id)
    assert result[0].ok
    client.calendar_promotion_products_all.assert_awaited_once_with(2766,in_action=True)
    with repo.db.connect() as db:
        row=db.execute('SELECT name,active FROM promotions WHERE external_promotion_id=?',('2739',)).fetchone()
        assert row['name']=='Завершённая' and row['active']==1
        assert db.execute('SELECT COUNT(*) FROM promotion_products').fetchone()[0]==1


@pytest.mark.asyncio
async def test_current_promo_failures_reach_db_raw_payload_and_retry_message(data):
    repo,shop,wb=data
    error='HTTP 422: {"detail":"invalid promotion parameters"}'
    client=SimpleNamespace(calendar_promotions_all=AsyncMock(return_value=FetchResult.success('wildberries',
        {'data':{'promotions':[{'id':2766,'name':'Новинки','endDateTime':'2026-12-31T20:59:59Z'}]}},200,1)),
        calendar_promotion_products_all=AsyncMock(return_value=FetchResult.failure('wildberries',error,422,1)))
    outcomes=await CollectionService(repo,wildberries=client).collect_promotions(shop_id=shop.id,as_of=DAY,wb_connection_id=wb.id)
    assert not outcomes[0].ok and '2766' in outcomes[0].message and error in outcomes[0].message
    parent=repo.latest_run(wb.id,'calendar/promotions'); child=repo.latest_run(wb.id,'calendar/promotions/2766/products')
    assert parent.status=='partial' and error in parent.error
    assert child.http_status==422 and error in child.error
    assert 'invalid promotion parameters' in repo.raw_payload_for_run(parent.id)
    ctx=SimpleNamespace(shop_id=shop.id,collect_promotions=AsyncMock(return_value=outcomes))
    registry=SimpleNamespace(contexts=lambda:[ctx],get=lambda shop:ctx)
    with pytest.raises(RuntimeError,match='Акции обновлены не полностью.*WB.*2766.*422'):
        await execute_retry_job(None,registry,{'shop_id':shop.id,'job_type':'promotions','payload':{'day':DAY.isoformat()}})
