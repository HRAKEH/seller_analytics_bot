import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.storage import Database, Repository
from app.services.product_status import apply_snapshot, wb_blocked, wb_errors, ozon_cards, check_product_status
from app.integrations.base import FetchResult
from app.integrations.ozon import OzonClient
from app.integrations.wildberries import WildberriesClient

@pytest.fixture
def setup(tmp_path):
    db=Database(tmp_path/'cards.db');db.initialize();repo=Repository(db)
    seller=repo.ensure_seller(1);shop=repo.ensure_shop(seller.id)
    conn=repo.ensure_connection(shop.id,'wildberries')
    return repo,shop,conn


def test_changes_persist_and_do_not_mix_shops(setup):
    repo,shop,conn=setup
    msg={'wildberries:1':'blocked <b> reason'}
    assert len(apply_snapshot(repo,shop.id,'card_blocked',msg))==1
    assert not apply_snapshot(Repository(repo.db),shop.id,'card_blocked',msg)
    other=repo.ensure_shop(shop.seller_id,'other')
    assert len(apply_snapshot(repo,other.id,'card_blocked',msg))==1
    assert len(apply_snapshot(repo,shop.id,'card_blocked',{'wildberries:1':'new reason'}))==1
    assert not apply_snapshot(repo,shop.id,'card_blocked',{},resolved_subjects=set())
    notes=apply_snapshot(repo,shop.id,'card_blocked',{})
    assert len(notes)==1 and notes[0].severity=='resolved'
    assert repo.active_alert_states(other.id)
    assert not apply_snapshot(repo,shop.id,'card_blocked',{})


def test_wb_strict_parsing_and_separate_drafts():
    for bad in ({}, {'report':None}, {'error':True,'report':[]}, {'report':[{}]}):
        with pytest.raises(ValueError):wb_blocked(bad)
    assert wb_blocked({'report':[]})=={}
    msg=wb_errors({'items':[{'errors':{'vendor':['z','a']}},{'errors':{'vendor':['a']}}]})
    assert list(msg)==['wildberries:vendor:vendor']
    assert 'a; z' in next(iter(msg.values()))


def product(**status):
    return {'id':1,'offer_id':'offer','name':'name','errors':[], 'statuses':status}


def test_ozon_unknown_pending_hidden_not_recovery():
    for row in [product(),product(moderate_status='pending',is_failed=False),
                dict(product(),visible=False,is_archived=True)]:
        assert ozon_cards([row])==({},set())
    issues,healthy=ozon_cards([product(is_failed=False,moderate_status='approved',validation_status='success')])
    assert healthy=={'ozon:1'} and not issues
    assert ozon_cards([product(is_failed=True)])[0]['ozon:1'][0]=='card_errors'
    assert ozon_cards([product(state='blocked')])[0]['ozon:1'][0]=='card_blocked'
    assert ozon_cards([product(state='not_blocked')])==({},set())
    with pytest.raises(ValueError):ozon_cards([{'id':1}])


def success(data):return FetchResult.success('test',data,200,1)

@asynccontextmanager
async def lock(_):yield

@pytest.mark.asyncio
async def test_failed_source_keeps_problem_other_source_works_and_throttles(setup):
    repo,shop,conn=setup
    apply_snapshot(repo,shop.id,'card_blocked',{'wildberries:1':'old'})
    wb=SimpleNamespace(blocked_cards=AsyncMock(return_value=success({'wrong':[]})),
        card_errors=AsyncMock(return_value=success({'items':[{'errors':{'a':['fix']}}]})))
    ctx=SimpleNamespace(repository=repo,shop_id=shop.id,demo_mode=lambda:False,operation_lock=lock,
        wb_connection_id=conn.id,ozon_connection_id=None,collector=SimpleNamespace(wb=wb))
    notes=await check_product_status(ctx)
    assert any(n.rule_key=='card_source' for n in notes)
    assert any(n.rule_key=='card_errors' for n in notes)
    assert repo.get_alert_state(shop.id,'card_blocked','wildberries:1')['active']
    assert not await check_product_status(ctx)
    assert wb.blocked_cards.await_count==1
    ctx.demo_mode=lambda:True
    assert not await check_product_status(ctx)

@pytest.mark.asyncio
async def test_ozon_full_catalogue_and_missing_batch():
    client=object.__new__(OzonClient);client.source='ozon';client._headers=lambda:{}
    client.request=AsyncMock(side_effect=[success({'result':{'items':[{'product_id':2}],'total':2,'last_id':'next'}}),
        success({'result':{'items':[{'product_id':3}],'total':2,'last_id':'end'}}),
        success({'items':[{'id':2}]})])
    result=await client.card_status_snapshot()
    assert not result.ok
    assert client.request.await_args_list[1].kwargs['json']['last_id']=='next'
    assert client.request.await_args_list[0].kwargs['json']['filter']=={'visibility':'ALL'}

@pytest.mark.asyncio
async def test_wb_errors_cursor_requires_explicit_completion():
    client=object.__new__(WildberriesClient);client.source='wildberries';client._headers=lambda:{}
    client.request=AsyncMock(side_effect=[success({'data':{'items':[{'errors':{'a':['x']}}],
        'cursor':{'next':True,'updatedAt':'date','batchUUID':'batch'}}}),
        success({'data':{'items':[],'cursor':{'next':False}}})])
    result=await client.card_errors()
    assert result.ok and len(result.data['items'])==1
    client.request=AsyncMock(return_value=success({'data':{'items':[]}}))
    assert not (await client.card_errors()).ok


def test_card_reports_escape_and_include_reason(setup):
    from app.reports.alerts import format_active_alerts, format_alert_detail
    repo,shop,_=setup
    apply_snapshot(repo,shop.id,'card_blocked',{'wildberries:1':'<b>Danger & reason'})
    report=SimpleNamespace(stock_risks=[],inventory_warnings=[])
    state=repo.active_alert_states(shop.id)[0]
    assert '&lt;b&gt;Danger &amp; reason' in format_active_alerts(repo,shop.id,report)
    assert '&lt;b&gt;Danger &amp; reason' in format_alert_detail(state,report)


def test_card_button_filters_other_alerts_and_keeps_detail_navigation(setup):
    from app.bot.keyboards import COMMAND_BUTTONS, control_keyboard
    from app.bot.operational_cards import OperationalCards
    from app.reports.alerts import format_active_alerts
    repo,shop,_=setup
    apply_snapshot(repo,shop.id,'card_blocked',{'wildberries:1':'WB · blocked'})
    apply_snapshot(repo,shop.id,'low_stock',{'wildberries:2':'stock'})
    ctx=SimpleNamespace(repository=repo,shop_id=shop.id,
        preferences=lambda:SimpleNamespace(timezone='UTC',stock_velocity_days=7,stock_risk_days=7))
    keyboard=control_keyboard('owner')
    assert COMMAND_BUTTONS['card_alerts'] in [button.text for row in keyboard.keyboard for button in row]
    controller=OperationalCards(ctx)
    # The filtered list is also the list used for callbacks and pagination.
    from unittest.mock import patch
    report=SimpleNamespace(stock_risks=[],inventory_warnings=[])
    with patch('app.bot.operational_cards.build_product_report',return_value=report):
        _,items=controller._data(ctx,'card_alerts',1)
    assert len(items)==1 and items[0]['rule_key']=='card_blocked'
    text,markup=controller._list(ctx,'card_alerts',report,items,0)
    assert 'Проблемы с карточками' in text and 'blocked' in text and 'stock' not in text
    assert 'ops:card_alerts:view:' in markup.inline_keyboard[0][0].callback_data
