"""Shop maintenance is global for owners; report access stays per shop."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

from aiogram import Dispatcher
from aiogram.methods import AnswerCallbackQuery, SendMessage
import pytest

from app.bot import register_handlers
from app.bot.keyboards import COMMAND_BUTTONS, HOME, MENU_SHOP
from app.bot.runtime import RuntimeRegistry, ContextProxy, ShopContextMiddleware
from app.storage import Database, Repository
from test_navigation import ui, press, callback, active, inline_data


@pytest.fixture
def shops(tmp_path):
    db=Database(tmp_path/'shops.sqlite3');db.initialize();repo=Repository(db)
    first=repo.ensure_shop(repo.ensure_seller(101,'First').id,'Same name')
    second=repo.ensure_shop(repo.ensure_seller(202,'Second').id,'Same name')
    repo.grant_shop_access(101,first.id,'owner')
    repo.grant_shop_access(202,second.id,'owner')
    for shop in (first,second):
        conn=repo.ensure_connection(shop.id,'ozon','Ozon')
        repo.record_success(conn.id,'analytics/orders','2026-10-07',{'shop':shop.id},[])
    return SimpleNamespace(repo=repo,first=first,second=second)


@pytest.mark.parametrize('actor,target',[(101,'second'),(202,'first')])
def test_owner_can_manage_shop_of_another_seller_and_preserve_survivor(shops,actor,target):
    repo=shops.repo;shop=getattr(shops,target)
    keep=shops.first if target=='second' else shops.second
    assert not repo.can_user(actor,shop.id,'view')
    repo.archive_shop(actor,shop.id)
    assert [s.id for s in repo.archived_shops()]==[shop.id]
    repo.restore_shop(actor,shop.id)
    repo.archive_shop(actor,shop.id)
    repo.delete_archived_shop(actor,shop.id)
    assert repo.get_shop(shop.id) is None
    assert repo.get_shop(keep.id)==keep
    assert repo.count('source_runs')==1
    assert repo.count('raw_payloads')==1


def test_last_active_guard_counts_the_whole_database(shops):
    repo=shops.repo
    repo.archive_shop(101,shops.second.id)
    with pytest.raises(ValueError,match='последний активный'):
        repo.archive_shop(202,shops.first.id)
    assert [s.id for s in repo.list_shops()]==[shops.first.id]


@pytest.mark.parametrize('role',['accountant','manager','viewer',None,'inactive'])
@pytest.mark.parametrize('operation',['archive_shop','restore_shop','delete_archived_shop'])
def test_backend_rechecks_owner_before_any_shop_mutation(shops,role,operation):
    repo=shops.repo
    if role:
        repo.grant_shop_access(303,shops.first.id,'owner' if role=='inactive' else role)
    if role=='inactive':
        with repo.db.connect() as c:c.execute('UPDATE bot_users SET active=0 WHERE telegram_user_id=303')
    if operation!='archive_shop':repo.archive_shop(101,shops.second.id)
    before=repo.get_shop(shops.second.id)
    with pytest.raises(PermissionError,match='Владелец'):
        getattr(repo,operation)(303,shops.second.id)
    assert repo.get_shop(shops.second.id)==before


def test_archived_role_allows_owner_to_finish_deletion_of_their_shop(shops):
    repo=shops.repo
    repo.archive_shop(202,shops.second.id)
    assert repo.role_for_user(202,shops.second.id) is None
    assert repo.is_shop_owner(202)
    repo.delete_archived_shop(202,shops.second.id)
    assert not repo.is_shop_owner(202)


def test_two_simultaneous_archive_requests_leave_one_active_shop(shops):
    def archive(shop):
        try:shops.repo.archive_shop(101,shop.id);return 'ok'
        except ValueError as exc:return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(archive,[shops.first,shops.second]))
    assert results.count('ok')==1
    assert any('последний активный' in result for result in results)
    assert len(shops.repo.list_shops())==1


def test_permanent_delete_requires_archival_and_an_active_survivor(shops):
    repo=shops.repo
    with pytest.raises(ValueError,match='Сначала архивируйте'):
        repo.delete_archived_shop(101,shops.second.id)
    with repo.db.connect() as c:c.execute('UPDATE shops SET active=0')
    with pytest.raises(ValueError,match='должен остаться активный'):
        repo.delete_archived_shop(101,shops.second.id)
    with pytest.raises(ValueError,match='нет активных'):
        repo.shops_for_startup(shops.second.seller_id)
    assert repo.count('shops')==2


def test_startup_reuses_other_sellers_shop_after_owner_order_changes(shops):
    repo=shops.repo
    repo.archive_shop(101,shops.second.id)
    repo.delete_archived_shop(101,shops.second.id)
    assert [s.id for s in repo.shops_for_startup(shops.second.seller_id)]==[shops.first.id]
    new_seller=repo.ensure_seller(999,'New configured owner')
    assert [s.id for s in repo.shops_for_startup(new_seller.id)]==[shops.first.id]
    assert repo.count('shops')==1


async def attach_registry(ui,second,*,owner_ids=(101,),default=None):
    settings=replace(ui.ctx.settings,owner_ids=owner_ids,wb_api_token='',ozon_client_id='',ozon_api_key='',
                     ozon_perf_client_id='',ozon_perf_client_secret='')
    registry=RuntimeRegistry(settings,ui.repo,second.seller_id,default or ui.shop.id)
    await registry.initialize()
    dp=Dispatcher();middleware=ShopContextMiddleware(registry)
    dp.message.middleware(middleware);dp.callback_query.middleware(middleware)
    register_handlers(dp,ContextProxy(),registry);ui.dp=dp
    return registry


def another_shop(ui):
    seller=ui.repo.ensure_seller(202,'Other owner')
    second=ui.repo.ensure_shop(seller.id,ui.shop.name)
    ui.repo.grant_shop_access(202,second.id,'owner')
    return second


@pytest.mark.asyncio
async def test_owner_outside_env_uses_menu_to_delete_foreign_shop(ui):
    second=another_shop(ui);ui.repo.grant_shop_access(303,ui.shop.id,'owner')
    registry=await attach_registry(ui,second)
    try:
        await press(ui,MENU_SHOP,user=303);menu=active(ui,303)
        await callback(ui,menu,inline_data(ui,menu,COMMAND_BUTTONS['shop_archive'],user=303),user=303)
        await callback(ui,menu,inline_data(ui,menu,'▶️ Открыть',user=303),user=303)
        picker=active(ui,303)
        data=[b.callback_data for row in ui.telegram.messages[(303,picker)].reply_markup.inline_keyboard for b in row]
        assert f'shop:archive:{second.id}' in data
        await callback(ui,picker,f'shop:archive:{second.id}',user=303)
        await callback(ui,picker,f'shop:archive_confirm:{second.id}',user=303)
        assert not ui.repo.get_shop(second.id).active
        await press(ui,COMMAND_BUTTONS['shop_delete'],user=303);picker=active(ui,303)
        await callback(ui,picker,f'shop:delete:{second.id}',user=303)
        assert ui.repo.get_shop(second.id) is not None
        await callback(ui,picker,f'shop:delete_confirm:{second.id}',user=303)
        assert ui.repo.get_shop(second.id) is None
        assert ui.repo.get_shop(ui.shop.id).active
    finally:await registry.close()


@pytest.mark.asyncio
async def test_owner_can_finish_deleting_own_shop_without_report_access_to_survivor(ui):
    second=another_shop(ui);ui.repo.grant_shop_access(303,second.id,'owner')
    registry=await attach_registry(ui,second,default=second.id)
    try:
        await press(ui,f'/shop_archive {second.id}',user=303)
        assert not ui.repo.get_shop(second.id).active
        assert registry.default_shop_id==ui.shop.id
        assert not ui.repo.can_user(303,ui.shop.id,'view')
        await press(ui,HOME,user=303);menu=active(ui,303)
        await callback(ui,menu,inline_data(ui,menu,MENU_SHOP,user=303),user=303)
        assert inline_data(ui,menu,COMMAND_BUTTONS['shop_delete'],user=303)
        await press(ui,'/day 2026-10-07',user=303)
        assert 'Недостаточно прав' in next(m.text for m in reversed(ui.telegram.methods)
            if isinstance(m,SendMessage) and m.chat_id==303)
        await press(ui,f'/shop_delete {second.id} DELETE',user=303)
        assert ui.repo.get_shop(second.id) is None
    finally:await registry.close()


@pytest.mark.asyncio
async def test_reload_does_not_recreate_deleted_first_owners_shop(ui):
    second=another_shop(ui)
    registry=await attach_registry(ui,second,owner_ids=(202,101),default=second.id)
    try:
        await registry.archive_shop(second.id,actor_id=202)
        await registry.delete_archived_shop(second.id,actor_id=202)
        await registry.reload()
        assert registry.default_shop_id==ui.shop.id
        assert [ctx.shop_id for ctx in registry.contexts()]==[ui.shop.id]
        assert ui.repo.count('shops')==1
        assert (await registry.context_for_user(202)).shop_id==ui.shop.id
    finally:await registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('role',['accountant','manager','viewer'])
@pytest.mark.parametrize('data',['archive','archive_confirm','restore','delete','delete_confirm'])
async def test_non_owner_old_buttons_cannot_change_any_shop(ui,role,data):
    second=another_shop(ui);ui.repo.grant_shop_access(303,ui.shop.id,role)
    registry=await attach_registry(ui,second)
    try:
        await press(ui,HOME,user=303);menu=active(ui,303)
        await callback(ui,menu,f'shop:{data}:{second.id}',user=303)
        assert ui.repo.get_shop(second.id).active
        assert 'владелец' in next(m.text for m in reversed(ui.telegram.methods) if isinstance(m,AnswerCallbackQuery))
    finally:await registry.close()


@pytest.mark.asyncio
async def test_revoked_owner_cannot_use_confirmation_created_before_revocation(ui):
    second=another_shop(ui);ui.repo.grant_shop_access(303,ui.shop.id,'owner')
    registry=await attach_registry(ui,second)
    try:
        await press(ui,COMMAND_BUTTONS['shop_archive'],user=303);picker=active(ui,303)
        await callback(ui,picker,f'shop:archive:{second.id}',user=303)
        ui.repo.grant_shop_access(303,ui.shop.id,'manager')
        await callback(ui,picker,f'shop:archive_confirm:{second.id}',user=303)
        assert ui.repo.get_shop(second.id).active
    finally:await registry.close()


@pytest.mark.asyncio
async def test_global_archive_pages_check_owner_again_after_revocation(ui):
    second=another_shop(ui)
    ui.repo.grant_shop_access(303,ui.shop.id,'manager')
    ui.repo.grant_shop_access(303,second.id,'owner')
    registry=await attach_registry(ui,second)
    try:
        await registry.archive_shop(second.id,actor_id=101)
        for index in range(40):
            archived=ui.repo.ensure_shop(second.seller_id,f'{index} '+'Магазин с длинным названием '*5)
            ui.repo.archive_shop(101,archived.id)
        await press(ui,MENU_SHOP,user=303);menu=active(ui,303)
        assert inline_data(ui,menu,COMMAND_BUTTONS['shop_archived'],user=303)
        await press(ui,'/shop_archived',user=303)
        card=next(m.message_id for (chat,_),m in reversed(ui.telegram.messages.items())
                  if chat==303 and m.text and 'Архив магазинов' in m.text)
        saved=ui.repo.paged_report(ui.bot.id,303,card)
        assert saved['capability']=='shop_lifecycle' and not saved['system_owner_only']
        await callback(ui,card,'report_page:1',user=303)
        assert ui.repo.paged_report(ui.bot.id,303,card)['current_page']==1
        ui.repo.revoke_shop_access(303,second.id)
        await callback(ui,card,'report_page:0',user=303)
        assert ui.repo.paged_report(ui.bot.id,303,card)['current_page']==1
        assert 'Нет доступа' in next(m.text for m in reversed(ui.telegram.methods) if isinstance(m,AnswerCallbackQuery))
    finally:await registry.close()


@pytest.mark.asyncio
async def test_running_foreign_shop_is_not_archived(ui):
    second=another_shop(ui);registry=await attach_registry(ui,second)
    try:
        current=registry.get(second.id)
        async with current.job_lock:
            with pytest.raises(RuntimeError,match='во время загрузки'):
                await registry.archive_shop(second.id,actor_id=101)
        assert ui.repo.get_shop(second.id).active
        assert registry.get(second.id) is current
    finally:await registry.close()
