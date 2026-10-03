from __future__ import annotations

import ast
import asyncio
import logging
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.services.backups import BackupService
from app.services.exporting import _safe_spreadsheet_value
from app.services.health import public_health_report
from app.services.observability import JsonFormatter, SecretRedactionFilter
from app.storage import Database, Repository, LATEST_SCHEMA_VERSION
import app.storage.database as dbmod

ROOT=Path(__file__).resolve().parents[1]


def make_repo(tmp_path):
    db=Database(tmp_path/'phase20.sqlite3'); assert db.initialize()==LATEST_SCHEMA_VERSION
    repo=Repository(db); seller=repo.ensure_seller(200,'Seller'); shop=repo.ensure_shop(seller.id,'Shop')
    return db,repo,seller,shop


def _load_context_module():
    import importlib.util
    name='phase20_context'
    spec=importlib.util.spec_from_file_location(name,ROOT/'app/bot/context.py')
    mod=importlib.util.module_from_spec(spec); assert spec and spec.loader
    sys.modules[name]=mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_operation_is_cancelled_when_distributed_lease_is_lost():
    AppContext=_load_context_module().AppContext
    class Repo:
        released=False
        def acquire_lease(self,*a,**k): return True
        def renew_lease(self,*a,**k): return False
        def release_lease(self,*a,**k): self.released=True; return True
    class Settings:
        distributed_lock_ttl_seconds=1
        instance_id='instance-a'
    repo=Repo()
    ctx=AppContext(Settings(),repo,1,object())
    with pytest.raises(RuntimeError,match='Потеряна межпроцессная блокировка'):
        async with ctx.operation_lock('test'):
            await asyncio.sleep(2)
    assert repo.released


def test_retry_jobs_are_shop_scoped_and_owner_guarded(tmp_path):
    db,repo,seller,shop1=make_repo(tmp_path)
    shop2=repo.ensure_shop(seller.id,'Shop 2')
    j1=repo.enqueue_retry_job(shop1.id,'daily','a',{'day':'2026-09-01'})
    j2=repo.enqueue_retry_job(shop2.id,'daily','b',{'day':'2026-09-02'})
    assert repo.retry_job_counts(shop1.id)=={'pending':1}
    assert [r['id'] for r in repo.recent_retry_jobs(10,shop_id=shop1.id)]==[j1]
    assert not repo.requeue_retry_job(j2,shop_id=shop1.id)

    claimed=repo.claim_retry_job('worker-a',lease_seconds=30)
    assert claimed and claimed['id']==j1
    assert not repo.renew_retry_job(j1,'worker-b',30)
    assert repo.renew_retry_job(j1,'worker-a',30)
    assert not repo.complete_retry_job(j1,owner_id='worker-b')
    assert repo.complete_retry_job(j1,owner_id='worker-a')


def test_fresh_database_is_removed_after_failed_first_migration(tmp_path,monkeypatch):
    path=tmp_path/'fresh.sqlite3'
    original=dbmod.MIGRATIONS[LATEST_SCHEMA_VERSION]
    def broken(conn):
        original(conn)
        conn.execute('CREATE TABLE phase20_partial(x INTEGER)')
        raise RuntimeError('phase20 boom')
    monkeypatch.setitem(dbmod.MIGRATIONS,LATEST_SCHEMA_VERSION,broken)
    db=Database(path)
    with pytest.raises(RuntimeError,match='phase20 boom'):
        db.initialize_safely(tmp_path/'backups')
    assert not path.exists()
    assert not Path(str(path)+'-wal').exists()
    assert not Path(str(path)+'-shm').exists()


def test_backup_names_are_unique_and_restore_is_atomic(tmp_path):
    db,repo,_,shop=make_repo(tmp_path)
    service=BackupService(db,repo,tmp_path/'backups')
    first=service.create(kind='manual'); second=service.create(kind='manual')
    assert first.path != second.path
    assert first.path.exists() and second.path.exists()
    repo.rename_shop(shop.id,'Changed')
    service.restore(first.path)
    assert repo.get_shop(shop.id).name=='Shop'
    assert db.quick_check()


def test_exception_traceback_is_secret_redacted():
    secret='phase20-super-secret'
    try:
        raise RuntimeError('credential='+secret)
    except RuntimeError:
        exc_info=sys.exc_info()
    record=logging.LogRecord('x',logging.ERROR,__file__,1,'request failed '+secret,(),exc_info)
    filt=SecretRedactionFilter([secret]); assert filt.filter(record)
    rendered=JsonFormatter().format(record)
    assert secret not in rendered
    assert 'REDACTED' in rendered


@pytest.mark.parametrize('value',["=HYPERLINK(\"x\")",'+SUM(A1:A2)','-1+2','@cmd','  =1+1','\t@evil'])
def test_spreadsheet_formula_injection_is_neutralized(value):
    safe=_safe_spreadsheet_value(value)
    assert isinstance(safe,str) and safe.startswith("'")
    assert _safe_spreadsheet_value(123.0)==123.0
    assert _safe_spreadsheet_value('normal sku')=='normal sku'


def test_public_health_payload_does_not_expose_runtime_identity():
    report={
        'status':'ok','ready':True,'timestamp':'2026-09-30T00:00:00+00:00',
        'checks':{'database':'ok','schema_version':14,'schema_current':True,'shops_runtime':2,
                  'maintenance_mode':False,'instance_id':'secret-host','poller_lease':{'owner_id':'worker'},
                  'heartbeats':[{'instance_id':'worker'}],'retry_jobs':{'dead':4}},
    }
    public=public_health_report(report)
    text=str(public)
    assert 'secret-host' not in text and 'poller_lease' not in text and 'heartbeats' not in text
    assert public['ready'] is True


def _load_keyboard_module_with_stub(monkeypatch):
    class Builder:
        def __init__(self): self.items=[]
        def button(self,*,text,**kwargs):
            callback_data=kwargs.get('callback_data')
            self.items.append((text,callback_data) if callback_data is not None else text)
        def adjust(self,*args): return None
        def as_markup(self,**kwargs): return tuple(self.items)
    aiogram=ModuleType('aiogram'); utils=ModuleType('aiogram.utils'); keyboard=ModuleType('aiogram.utils.keyboard')
    keyboard.ReplyKeyboardBuilder=Builder
    keyboard.InlineKeyboardBuilder=Builder
    monkeypatch.setitem(sys.modules,'aiogram',aiogram)
    monkeypatch.setitem(sys.modules,'aiogram.utils',utils)
    monkeypatch.setitem(sys.modules,'aiogram.utils.keyboard',keyboard)
    import importlib.util
    spec=importlib.util.spec_from_file_location('phase20_keyboards',ROOT/'app/bot/keyboards.py')
    mod=importlib.util.module_from_spec(spec); assert spec and spec.loader; spec.loader.exec_module(mod)
    return mod


def test_global_database_and_credential_buttons_are_system_owner_only(monkeypatch):
    kb=_load_keyboard_module_with_stub(monkeypatch)
    delegated_shop_owner=(
        set(kb.service_keyboard('owner',system_owner=False))
        | set(kb.technical_keyboard('owner',system_owner=False))
        | set(kb.shop_keyboard('owner',system_owner=False))
    )
    system_owner=(
        set(kb.service_keyboard('owner',system_owner=True))
        | set(kb.technical_keyboard('owner',system_owner=True))
        | set(kb.shop_keyboard('owner',system_owner=True))
    )
    global_buttons={kb.COMMAND_BUTTONS[x] for x in ('backup','backups','restore','shop_add','shop_profile','shop_archive','shop_archived','shop_restore','shop_delete','profiles')}
    assert delegated_shop_owner.isdisjoint(global_buttons)
    assert global_buttons <= system_owner


def test_global_slash_handlers_enforce_system_owner():
    tree=ast.parse((ROOT/'app/bot/handlers.py').read_text(encoding='utf-8'))
    functions={n.name:n for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
    for name in ('cmd_backup','cmd_backups','cmd_restore','restore_file','cmd_shop_add','cmd_shop_profile','cmd_shop_archive','cmd_shop_archived','cmd_shop_restore','cmd_shop_delete','cmd_profiles'):
        node=functions[name]
        calls=[n for n in ast.walk(node) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)]
        assert any(c.func.id=='is_system_owner' for c in calls), name


def test_scheduler_outer_retry_uses_previous_completed_day():
    src=(ROOT/'app/services/scheduler.py').read_text(encoding='utf-8')
    assert 'report_day=_scheduled_report_day(now)' in src
    assert "{'day':report_day,'notify':True}" in src
    assert "'daily',report_day" in src


def test_runtime_validates_credential_profile_before_persisting_it():
    src=(ROOT/'app/bot/runtime.py').read_text(encoding='utf-8')
    create=src[src.index('    async def create_shop'):src.index('    async def set_profile')]
    setp=src[src.index('    async def set_profile'):src.index('    async def close')]
    assert create.index('credentials_for_profile') < create.index('ensure_shop')
    assert setp.index('credentials_for_profile') < setp.index('set_shop_credential_profile')


def test_future_day_is_rejected_before_collection():
    src=(ROOT/'app/bot/handlers.py').read_text(encoding='utf-8')
    start=src.index("    @dp.message(Command('day'))")
    end=src.index("    @dp.message(Command('backfill'))",start)
    block=src[start:end]
    assert 'Нельзя загружать отчёт за будущую дату' in block
    assert block.index('Нельзя загружать отчёт за будущую дату') < block.index('collect_and_report')


def test_action_center_retry_failures_are_shop_scoped():
    src=(ROOT/'app/services/actions.py').read_text(encoding='utf-8')
    assert 'recent_retry_jobs(limit=50,shop_id=shop_id)' in src


def test_startup_reuses_existing_active_shop_instead_of_recreating_default():
    src=(ROOT/'main.py').read_text(encoding='utf-8')
    assert 'active_shops=repo.list_shops(seller.id)' in src
    assert "shop=active_shops[0] if active_shops else repo.ensure_shop(" in src


def test_simplified_user_menus_hide_manual_and_technical_actions(monkeypatch):
    kb=_load_keyboard_module_with_stub(monkeypatch)
    reports=set(kb.reports_keyboard('owner'))
    products=set(kb.products_keyboard('owner'))
    control=set(kb.control_keyboard('owner'))
    supply=set(kb.supply_keyboard('owner'))
    assert kb.COMMAND_BUTTONS['day'] not in reports
    assert kb.COMMAND_BUTTONS['cost'] not in products
    assert kb.COMMAND_BUTTONS['link'] not in products
    assert kb.COMMAND_BUTTONS['action_ack'] not in control
    assert kb.COMMAND_BUTTONS['action_snooze'] not in control
    assert kb.COMMAND_BUTTONS['health'] not in control
    assert kb.COMMAND_BUTTONS['diagnostics'] not in control
    assert kb.COMMAND_BUTTONS['supply_set'] not in supply
    assert kb.COMMAND_BUTTONS['supply_defaults'] not in supply


def test_common_report_export_and_retry_flows_are_button_driven(monkeypatch):
    kb=_load_keyboard_module_with_stub(monkeypatch)
    reports=set(kb.reports_keyboard('owner'))
    assert {'🗓 Другая дата','📥 Догрузить данные','📜 Что уже загружено'} <= reports
    assert ('Вчера','report_date:1') in kb.report_date_keyboard()
    assert ('30 дней','export:period:30') in kb.export_period_keyboard()
    assert ('📊 Excel','export:run:30:xlsx') in kb.export_format_keyboard(30)
    users=kb.users_admin_keyboard()
    assert ('➕ Дать доступ','users:add') in users
    retry=kb.retry_jobs_keyboard([{'id':7,'status':'dead'},{'id':8,'status':'success'}])
    assert ('🔁 Повторить #7','retry:run:7') in retry
    assert all(not (isinstance(x,tuple) and x[1]=='retry:run:8') for x in retry)


def test_report_date_button_reads_database_without_forcing_api_refresh():
    src=(ROOT/'app/bot/handlers.py').read_text(encoding='utf-8')
    assert "F.data.startswith('report_date:')" in src
    assert any(isinstance(node,ast.Call) and isinstance(node.func,ast.Name)
               and node.func.id=='start_menu_input'
               and any(isinstance(arg,ast.Constant) and arg.value=='day_view' for arg in node.args)
               for node in ast.walk(ast.parse(src)))
    start=src.index("if action=='day_view':")
    end=src.index('pair=input_actions.get(action)',start)
    block=src[start:end]
    assert 'collect_and_report(message,target,force=False)' in block
    assert 'collect_day' not in block


def test_action_center_has_inline_button_flow(monkeypatch):
    kb=_load_keyboard_module_with_stub(monkeypatch)
    class Item:
        action_key='alert:api_stale:wildberries'
        title='Wildberries: данные давно не обновлялись'
    markup=kb.action_center_keyboard([Item()])
    assert any(x[1].startswith('action:view:') for x in markup if isinstance(x,tuple))
    ref=kb.action_ref(Item.action_key)
    detail=kb.action_item_keyboard(ref)
    assert ('✅ Принято',f'action:ack:{ref}') in detail
    assert ('⏰ Отложить на 24 ч',f'action:snooze:{ref}') in detail


def test_long_message_guard_and_human_readable_api_alerts_are_present():
    src=(ROOT/'app/bot/handlers.py').read_text(encoding='utf-8')
    from app.reports.text import split_report_html, utf16_length
    chunks=split_report_html('\n'.join(['<b>Полное название товара 😀</b>']*500))
    assert len(chunks)>1 and all(utf16_length(chunk)<=3900 for chunk in chunks)
    from types import SimpleNamespace
    from app.reports.alerts import format_active_alerts
    repo=SimpleNamespace(active_alert_states=lambda shop: [
        {'rule_key':'api_stale','subject_key':'wildberries','last_value':27}])
    alert=format_active_alerts(repo,1,SimpleNamespace(stock_risks=[]))
    assert 'WB' in alert and 'Данные заказов давно не обновлялись' in alert and '27.0 ч.' in alert
    assert 'Wildberries' in src
    assert "F.data.startswith('action:view:')" in src


def test_main_singleton_lease_has_fail_fast_cleanup():
    src=(ROOT/'main.py').read_text(encoding='utf-8')
    assert "poller_lease='singleton:telegram-poller'" in src
    assert 'fatal_errors.append(exc)' in src
    assert 'create_task(dp.stop_polling())' in src
    assert "repo.release_lease(poller_lease,settings.instance_id)" in src
    # Cleanup must live in the finally block that wraps post-lease startup.
    finally_pos=src.rindex('    finally:')
    release_pos=src.rindex("repo.release_lease(poller_lease,settings.instance_id)")
    assert release_pos > finally_pos


@pytest.mark.asyncio
async def test_database_integrity_failure_is_fatal():
    import importlib
    resilience=importlib.import_module('app.services.resilience')
    class DB:
        def checkpoint(self,*a): return None
        def quick_check(self): return False
    class Repo: db=DB()
    class Registry:
        repository=Repo()
        class Lock:
            def locked(self): return False
        maintenance_lock=Lock()
    with pytest.raises(resilience.DatabaseIntegrityError):
        await asyncio.wait_for(resilience.database_maintenance_loop(Registry()),timeout=1)


def test_shop_picker_keyboard_uses_one_tap_callbacks(monkeypatch):
    kb=_load_keyboard_module_with_stub(monkeypatch)
    class Shop:
        def __init__(self,shop_id,name): self.id=shop_id; self.name=name
    markup=kb.shop_picker_keyboard([Shop(7,'Первый'),Shop(9,'Второй')],'select',current_shop_id=9)
    assert ('Первый','shop:select:7') in markup
    assert ('✅ Второй','shop:select:9') in markup
    assert ('❌ Отмена','shop:cancel') in markup


def test_backfill_picker_uses_marketplace_and_period_callbacks(monkeypatch):
    kb=_load_keyboard_module_with_stub(monkeypatch)
    sources=kb.backfill_source_keyboard(has_ozon=True,has_wb=True)
    assert ('🟣 Ozon','backfill:source:ozon') in sources
    assert ('🔵 Wildberries','backfill:source:wildberries') in sources
    assert ('🟣🔵 Оба маркетплейса','backfill:source:all') in sources
    period=kb.backfill_period_keyboard('wildberries')
    assert ('7 дней','backfill:period:wildberries:7') in period
    assert ('30 дней','backfill:period:wildberries:30') in period
    assert ('📅 Свой период','backfill:custom:wildberries') in period


    running=kb.backfill_running_keyboard()
    assert ('🛑 Остановить загрузку','backfill:stop') in running


@pytest.mark.asyncio
async def test_context_can_cancel_active_backfill_task():
    AppContext=_load_context_module().AppContext
    class Settings: pass
    ctx=AppContext(Settings(),object(),1,object())
    task=asyncio.create_task(asyncio.sleep(60))
    ctx.active_backfill_task=task
    assert ctx.backfill_running()
    assert ctx.cancel_backfill() is True
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not ctx.backfill_running()
    assert ctx.cancel_backfill() is False


def test_backfill_handler_catches_cancellation_and_exposes_stop_callback():
    src=(ROOT/'app/bot/handlers.py').read_text(encoding='utf-8')
    assert "F.data == 'backfill:stop'" in src
    assert 'ctx.cancel_backfill()' in src
    assert 'except asyncio.CancelledError:' in src
    assert 'backfill_running_keyboard()' in src


def test_context_backfill_routes_selected_marketplace():
    src=(ROOT/'app/bot/context.py').read_text(encoding='utf-8')
    start=src.index('    async def backfill_orders')
    end=src.index('    async def collect_inventory',start)
    block=src[start:end]
    assert "source in {'all','wildberries','wb'}" in block
    assert "source in {'all','ozon'}" in block
    assert "one_source(wb_id,'wildberries')" in block
    assert "one_source(ozon_id,'ozon')" in block
    assert 'orders already loaded' in block


def test_normal_report_buttons_are_cached_and_refresh_is_explicit(monkeypatch):
    kb=_load_keyboard_module_with_stub(monkeypatch)
    products=set(kb.products_keyboard('owner'))
    money=set(kb.money_keyboard('owner'))
    assert '🔄 Обновить остатки' in products
    assert '🔄 Обновить финансы' in money
    assert '🔄 Обновить рекламу' in money

    src=(ROOT/'app/bot/handlers.py').read_text(encoding='utf-8')
    finance=src[src.index("@dp.message(F.text == COMMAND_BUTTONS['finance'])"):src.index("@dp.message(F.text == COMMAND_BUTTONS['ads'])")]
    ads=src[src.index("@dp.message(F.text == COMMAND_BUTTONS['ads'])"):src.index("@dp.message(F.text == COMMAND_BUTTONS['management'])")]
    stocks=src[src.index("@dp.message(F.text == '📦 Остатки')"):src.index("@dp.message(F.text == '🔄 Обновить остатки')")]
    assert 'collect_finance' not in finance
    assert 'collect_advertising' not in ads
    assert 'collect_inventory' not in stocks
    assert "@dp.message(F.text == '🔄 Обновить финансы')" in src
    assert "@dp.message(F.text == '🔄 Обновить рекламу')" in src
    assert "@dp.message(F.text == '🔄 Обновить остатки')" in src


def test_finance_refresh_does_not_refresh_advertising():
    src=(ROOT/'app/bot/handlers.py').read_text(encoding='utf-8')
    block=src[src.index("    @dp.message(Command('finance'))"):src.index("    @dp.message(Command('ads'))")]
    assert 'collect_finance' in block
    assert 'collect_advertising' not in block
    assert 'Финансы обновились не полностью' in block


def test_wb_connection_check_fails_fast_and_stops_extra_probes_after_429():
    wb=(ROOT/'app/integrations/wildberries.py').read_text(encoding='utf-8')
    probe=wb[wb.index('    async def ping'):wb.index('    async def orders')]
    assert probe.count('retry_on_429: bool = False') >= 2
    assert probe.count('fail_fast_rate_limit: bool = True') >= 2

    readiness=(ROOT/'app/services/readiness.py').read_text(encoding='utf-8')
    assert "rate_limited=(not info.ok and info.status_code==429)" in readiness
    assert 'WB временно ограничил запросы; повторите проверку позже' in readiness


def test_rare_service_controls_are_grouped_under_technical_menu(monkeypatch):
    kb=_load_keyboard_module_with_stub(monkeypatch)
    service=set(kb.service_keyboard('owner',system_owner=True))
    assert kb.MENU_TECH in service
    for key in ('health','diagnostics','jobs','backup','backups','restore','profiles'):
        assert kb.COMMAND_BUTTONS[key] not in service
    technical=set(kb.technical_keyboard('owner',system_owner=True))
    assert kb.COMMAND_BUTTONS['health'] in technical
    assert kb.COMMAND_BUTTONS['jobs'] in technical
    assert kb.COMMAND_BUTTONS['backup'] in technical
