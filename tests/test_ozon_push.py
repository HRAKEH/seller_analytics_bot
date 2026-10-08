"""Ozon Push contract, exact event dates, durable deduplication and owner flows."""
import asyncio
from dataclasses import replace
from datetime import date
import json
from types import SimpleNamespace
from uuid import uuid4

from aiohttp.test_utils import TestClient, TestServer
import pytest
import pytest_asyncio

from app.config import Settings
from app.services.daily_events import build_daily_events
from app.services.ozon_push_http import create_push_app
from app.services.ozon_push_payload import MAX_BODY_BYTES, PushError, base_url, digest, parse_notification
from app.storage import Database, Repository, LATEST_SCHEMA_VERSION
from app.storage.database import MIGRATIONS
from app.storage.ozon_push import OzonPushStore
from test_navigation import ui, press, callback, active, inline_data


DAY = date(2026,10,8)


def cancellation(*, fbo=True, number='12345-0001-1', quantity=3, **fields):
    result = {'message_type':'TYPE_FBO_POSTING_CANCELLED' if fbo else 'TYPE_POSTING_CANCELLED',
              'posting_number':number,'products':[{'sku':147451959,'quantity':quantity}],
              'old_state':'posting_transferred_to_courier_service','new_state':'posting_canceled',
              'reason':{'id':537,'message':'Не вручен'},'warehouse_id':18044249781000,'seller_id':7376,
              'cancel_date' if fbo else 'changed_state_date':'2026-10-07T21:00:00.123Z'}
    if fbo:
        result['uuid']='bf354adc-e404-480c-a037-cf865464f1d9'
    return {**result,**fields}


@pytest.fixture
def system(tmp_path):
    db = Database(tmp_path/'push.sqlite3')
    db.initialize()
    repo = Repository(db)
    seller = repo.ensure_seller(101)
    shop = repo.ensure_shop(seller.id,'Shop')
    repo.ensure_shop_preferences(shop.id)
    repo.grant_shop_access(101,shop.id,'owner')
    connection = repo.ensure_connection(shop.id,'ozon','Ozon')
    settings = replace(Settings.from_env(),owner_ids=(101,),ozon_client_id='123',ozon_api_key='LOCAL_TEST',
                       ozon_push_enabled=True,ozon_push_base_url='https://bot.example.org')
    store = OzonPushStore(db)
    token = store.issue(connection.id,shop.id,'123','DEFAULT',actor_id=101,system_owners=(101,))
    context = SimpleNamespace(shop_id=shop.id,ozon_connection_id=connection.id)
    registry = SimpleNamespace(settings=settings,repository=repo,maintenance_lock=asyncio.Lock(),contexts=lambda:[context])
    return SimpleNamespace(db=db,repo=repo,shop=shop,connection=connection,settings=settings,store=store,token=token,registry=registry)


def accept(system,payload,**fields):
    return system.store.receive(system.connection.id,system.token,'123','DEFAULT',payload,parse_notification(payload),**fields)


@pytest_asyncio.fixture
async def http(system):
    client = TestClient(TestServer(create_push_app(system.registry)))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


def path(system,token=None):
    return f'/ozon/push/{system.connection.id}/{token or system.token}'


@pytest.mark.parametrize('fbo',[False,True])
def test_documented_cancel_uses_event_time_moscow_and_sums_product_quantities(fbo):
    payload = cancellation(fbo=fbo,products=[{'sku':2,'quantity':3},{'sku':1,'quantity':2}])
    event = parse_notification(payload)
    assert event['units']==5 and event['event_day']=='2026-10-08'
    assert event['scheme']==('FBO' if fbo else 'FBS')
    assert event['cancelled_at']=='2026-10-07T21:00:00.123000+00:00'
    assert json.loads(event['products_json'])==[{'sku':1,'quantity':2},{'sku':2,'quantity':3}]


@pytest.mark.parametrize('fields',[
    {'cancel_date':None}, {'cancel_date':'2026-10-08'}, {'cancel_date':'2026-10-08T09:00:00'},
    {'cancel_date':'bad','created_at':'2026-10-08T10:00:00Z'}, {'cancel_date':'0001-01-01T00:00:00Z'},
    {'products':[]}, {'products':[{'sku':True,'quantity':1}]}, {'products':[{'sku':1,'quantity':False}]},
    {'products':[{'sku':1,'quantity':0}]}, {'products':[{'sku':1,'quantity':'3'}]},
    {'products':[{'sku':1,'quantity':1},{'sku':1,'quantity':1}]}, {'seller_id':0},
    {'seller_id':True}, {'uuid':None}, {'uuid':'not-a-uuid'}, {'new_state':'posting_delivered'},
    {'posting_number':'<script>'}, {'products':[{'sku':1,'quantity':2**63}]},
])
def test_bad_cancel_fields_never_infer_date_quantity_or_false_zero(fields):
    with pytest.raises(PushError):
        parse_notification(cancellation(**fields))


@pytest.mark.parametrize('value',['http://bot.example.org','https://u:p@bot.example.org',
    'https://bot.example.org/secret','https://bot.example.org/?token=abc','https://bot.example.org/#abc',
    'https://bot.example.org:8080','https://','https://bot.example.org:abc'])
def test_origin_config_rejects_secrets_paths_and_non_https(value):
    with pytest.raises(PushError):base_url(value)


def test_per_cabinet_opaque_token_survives_rotation_restart_and_never_is_stored_plain(system):
    payload = cancellation()
    assert accept(system,payload)=='accepted'
    assert accept(system,payload)=='accepted'  # Same raw delivery is already acknowledged.
    same_event = cancellation(uuid=str(uuid4()),reason={'id':537,'message':'updated text'})
    assert accept(system,same_event)=='duplicate'
    assert system.store.day(system.connection.id,DAY.isoformat())['units']==3
    old_hash = system.store.status(system.connection.id)['token_hash']
    new = system.store.issue(system.connection.id,system.shop.id,'123','DEFAULT',actor_id=101,
                             system_owners=(101,),replace_hash=old_hash)
    assert new != system.token
    with pytest.raises(PushError):accept(system,cancellation(number='old-address',uuid=str(uuid4())))
    system.token = new
    system.store = OzonPushStore(Database(system.db.path))
    assert accept(system,same_event)=='duplicate'
    assert system.store.day(system.connection.id,DAY.isoformat())['units']==3
    with system.db.connect() as c:
        dump = '\n'.join(c.iterdump())
    assert new not in dump and system.token not in dump


def test_concurrent_redelivery_cannot_multiply_count(system):
    from concurrent.futures import ThreadPoolExecutor
    payload = cancellation()
    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _:accept(system,payload),range(5)))
    assert results==['accepted']*5
    assert system.repo.count('ozon_push_inbox')==1
    assert system.repo.count('ozon_push_cancellations')==1
    assert system.store.status(system.connection.id)['repeated_deliveries']==4
    assert system.store.day(system.connection.id,DAY.isoformat())['units']==3


def test_changed_quantity_or_reused_uuid_is_conflict_not_a_second_cancel(system):
    assert accept(system,cancellation())=='accepted'
    assert accept(system,cancellation(quantity=2))=='conflict'
    assert system.store.day(system.connection.id,DAY.isoformat())['units']==0
    assert accept(system,cancellation(number='different-posting',quantity=9))=='conflict'
    reading = build_daily_events(system.repo,system.connection.id,'ozon',DAY).cancellations
    assert reading.units is None and reading.available_units==0
    assert 'спорные' in reading.warning and '0 шт' not in reading.unavailable_note


def test_order_cancel_without_products_is_preserved_but_not_counted(system):
    accept(system,cancellation())
    payload = {'message_type':'TYPE_ORDER_CANCELLED','order_number':'12345-0001','order_id':35452597966,
               'uuid':str(uuid4()),'cancelled_at':'2026-10-07T21:00:00Z','seller_id':7376}
    assert accept(system,payload)=='ignored'
    assert system.repo.count('ozon_push_inbox')==2
    assert system.store.day(system.connection.id,DAY.isoformat())['units']==3


def test_two_shops_and_changed_client_id_never_mix_cancellations(system):
    accept(system,cancellation())
    other = system.repo.ensure_shop(system.repo.ensure_seller(101).id,'Other')
    system.repo.grant_shop_access(101,other.id,'owner')
    conn = system.repo.ensure_connection(other.id,'ozon','Ozon')
    other_token = system.store.issue(conn.id,other.id,'456','DEFAULT',actor_id=101,system_owners=(101,))
    payload = cancellation(quantity=7,seller_id=9000)
    system.store.receive(conn.id,other_token,'456','DEFAULT',payload,parse_notification(payload))
    assert system.store.day(conn.id,DAY.isoformat())['units']==7
    assert system.store.day(system.connection.id,DAY.isoformat())['units']==3
    system.store.observe_account(system.connection.id,'NEW_CLIENT','DEFAULT')
    assert system.store.day(system.connection.id,DAY.isoformat()) is None
    with pytest.raises(PushError):accept(system,cancellation())
    old = system.store.status(system.connection.id)
    new_token = system.store.issue(system.connection.id,system.shop.id,'NEW_CLIENT','DEFAULT',actor_id=101,
                                   system_owners=(101,),replace_hash=old['token_hash'])
    assert system.store.status(system.connection.id)['seller_id'] is None
    assert system.store.day(system.connection.id,DAY.isoformat())['units']==0
    new_payload = cancellation(quantity=2,seller_id=9999)
    system.store.receive(system.connection.id,new_token,'NEW_CLIENT','DEFAULT',new_payload,parse_notification(new_payload))
    assert system.store.day(system.connection.id,DAY.isoformat())['units']==2


def test_shop_profile_change_requires_new_binding_and_revoked_owner_cannot_rotate(system):
    system.repo.set_shop_credential_profile(system.shop.id,'OTHER')
    assert system.store.day(system.connection.id,DAY.isoformat()) is None
    with pytest.raises(PushError):accept(system,cancellation())
    system.repo.set_shop_credential_profile(system.shop.id,'DEFAULT')
    system.repo.grant_shop_access(101,system.shop.id,'accountant')
    with pytest.raises(PushError):
        system.store.issue(system.connection.id,system.shop.id,'123','DEFAULT',actor_id=101,
                          system_owners=(101,),replace_hash=system.store.status(system.connection.id)['token_hash'])


def test_daily_card_reads_positive_confirmed_part_but_does_not_claim_complete_zero(system):
    empty = build_daily_events(system.repo,system.connection.id,'ozon',DAY).cancellations
    assert empty.units is None and '0 шт' not in empty.unavailable_note
    accept(system,cancellation())
    accept(system,cancellation(fbo=False,number='FBS-1',quantity=2))
    event = build_daily_events(system.repo,system.connection.id,'ozon',DAY).cancellations
    assert event.units is None and event.amount is None and event.available_units==5
    assert event.unavailable_note=='≥ 5 шт. · по уведомлениям'
    previous = build_daily_events(system.repo,system.connection.id,'ozon',date(2026,10,7)).cancellations
    assert previous.units is None and previous.available_units==0


def test_migration_from_21_preserves_existing_shop_and_backup_keeps_receiver_state(tmp_path):
    db = Database(tmp_path/'v21.sqlite3')
    with db.connect() as c:
        c.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)')
        for version in range(1,22):
            MIGRATIONS[version](c)
            c.execute('INSERT INTO schema_migrations VALUES(?,?)',(version,'old'))
    repo = Repository(db)
    shop = repo.ensure_shop(repo.ensure_seller(101).id,'Existing shop')
    assert db.initialize_safely(tmp_path/'backups')==LATEST_SCHEMA_VERSION==22
    assert repo.get_shop(shop.id).name=='Existing shop'
    repo.grant_shop_access(101,shop.id,'owner')
    conn = repo.ensure_connection(shop.id,'ozon','Ozon')
    store = OzonPushStore(db)
    token = store.issue(conn.id,shop.id,'123','DEFAULT',actor_id=101,system_owners=(101,))
    body = cancellation()
    store.receive(conn.id,token,'123','DEFAULT',body,parse_notification(body))
    copy = Database(tmp_path/'restored.sqlite3')
    with db.connect() as source,copy.connect() as target:
        source.backup(target)
    restored = OzonPushStore(copy)
    assert restored.receive(conn.id,token,'123','DEFAULT',body,parse_notification(body))=='accepted'
    assert restored.day(conn.id,DAY.isoformat())['units']==3
    assert copy.quick_check()


@pytest.mark.asyncio
async def test_http_ping_ack_matches_ozon_contract_and_never_counts_an_event(http,system):
    response = await http.post(path(system),json={'message_type':'TYPE_PING','time':'2026-10-08T09:00:00Z'})
    assert response.status==200
    body = await response.json()
    assert set(body)=={'version','name','time'} and body['name']=='seller_analytics_bot'
    assert system.store.status(system.connection.id)['last_ping_at']
    assert system.repo.count('ozon_push_inbox')==0
    response = await http.get('/health')
    health = await response.json()
    assert 'instance_id' not in health['checks'] and system.token not in json.dumps(health)


@pytest.mark.asyncio
async def test_http_authentication_pin_and_redelivery_are_durable(http,system):
    wrong = await http.post(path(system,'x'*43),json=cancellation())
    assert wrong.status==404 and system.repo.count('ozon_push_inbox')==0
    for _ in range(2):
        result = await http.post(path(system),json=cancellation())
        assert result.status==200
    assert system.store.status(system.connection.id)['seller_id']==7376  # Not Client-Id 123.
    bad_seller = await http.post(path(system),json=cancellation(seller_id=9999))
    assert bad_seller.status==409
    assert system.repo.count('ozon_push_inbox')==1
    assert system.store.day(system.connection.id,DAY.isoformat())['units']==3


@pytest.mark.asyncio
async def test_original_json_body_is_preserved_without_headers_and_price_is_not_inferred(http,system):
    from app.reports.cards import _event_line
    payload = cancellation(price=750,financial_data={'price':'345.00'})
    raw = json.dumps(payload,ensure_ascii=False,indent=4)
    response = await http.post(path(system),data=raw,headers={'X-Private-Test':'do-not-save-this-header'})
    assert response.status==200
    with system.db.connect() as c:
        stored = c.execute('SELECT payload_json FROM ozon_push_inbox').fetchone()[0]
        dump = '\n'.join(c.iterdump())
    assert stored==raw and 'do-not-save-this-header' not in dump and system.token not in dump
    event = build_daily_events(system.repo,system.connection.id,'ozon',DAY).cancellations
    assert _event_line('❌ Отменено',event)=='❌ Отменено: ≥ 3 шт. · по уведомлениям'
    assert event.amount is None


@pytest.mark.asyncio
async def test_http_invalid_payload_is_archived_without_count_and_oversized_json_is_rejected(http,system):
    bad = cancellation(cancel_date=None)
    for _ in range(2):
        response = await http.post(path(system),json=bad)
        assert response.status==400
    assert system.repo.count('ozon_push_inbox')==1 and system.repo.count('ozon_push_cancellations')==0
    for raw in ['{"message_type":"TYPE_PING","time":NaN}',
                '{"message_type":"TYPE_PING","message_type":"TYPE_ORDER_CANCELLED"}', '[]','{']:
        response = await http.post(path(system),data=raw)
        assert response.status==400
    oversized = await http.post(path(system),data=b'x'*(MAX_BODY_BYTES+1))
    assert oversized.status==413
    assert system.repo.count('ozon_push_inbox')==1


@pytest.mark.asyncio
async def test_http_during_database_restore_retries_and_archived_shop_stops_receiving(http,system):
    async with system.registry.maintenance_lock:
        response = await http.post(path(system),json=cancellation())
        assert response.status==503
    assert system.repo.count('ozon_push_inbox')==0
    other = system.repo.ensure_shop(system.repo.ensure_seller(101).id,'Other')
    system.repo.grant_shop_access(101,other.id,'owner')
    accept(system,cancellation())
    system.repo.archive_shop(101,system.shop.id)
    response = await http.post(path(system),json=cancellation())
    assert response.status==404
    system.repo.delete_archived_shop(101,system.shop.id)
    for table in ('ozon_push_bindings','ozon_push_inbox','ozon_push_cancellations'):
        assert system.repo.count(table)==0
    with system.db.connect() as c:
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]


@pytest.mark.asyncio
async def test_old_url_cannot_receive_for_a_shop_outside_the_running_edition(http,system):
    system.registry.contexts = lambda:[]
    response = await http.post(path(system),json=cancellation())
    assert response.status==404 and system.repo.count('ozon_push_inbox')==0


def enable_ui(ui):
    ui.ctx.settings = replace(ui.ctx.settings,ozon_push_enabled=True,ozon_push_base_url='https://bot.example.org',
                             ozon_client_id='123',ozon_api_key='LOCAL_TEST')
    ui.ctx.ozon_connection_id = ui.repo.ensure_connection(ui.shop.id,'ozon','Ozon').id


@pytest.mark.asyncio
async def test_owner_creates_private_url_without_pager_storage_or_repeat_rotation(ui):
    enable_ui(ui)
    await press(ui,'/ozon_push')
    menu = active(ui)
    data = inline_data(ui,menu,'🔗 Создать адрес')
    ui.telegram.fail_deletes.add(menu)
    await callback(ui,menu,data)
    result = list(ui.telegram.messages.values())[-1].text
    assert 'https://bot.example.org/ozon/push/' in result
    token = result.split('/ozon/push/')[1].split('</code>')[0].split('/')[-1]
    binding = OzonPushStore(ui.repo.db).status(ui.ctx.ozon_connection_id)
    assert binding['token_hash']==digest(token)
    await callback(ui,menu,data)
    assert OzonPushStore(ui.repo.db).status(ui.ctx.ozon_connection_id)['token_hash']==binding['token_hash']
    with ui.repo.db.connect() as c:
        assert token not in '\n'.join(c.iterdump())
    await press(ui,'/ozon_push')
    menu = active(ui)
    await callback(ui,menu,inline_data(ui,menu,'🔄 Заменить адрес'))
    assert 'Старый адрес перестанет работать' in ui.telegram.messages[(101,menu)].text
    assert OzonPushStore(ui.repo.db).status(ui.ctx.ozon_connection_id)['token_hash']==binding['token_hash']
    await callback(ui,menu,inline_data(ui,menu,'✅ Заменить адрес'))
    assert OzonPushStore(ui.repo.db).status(ui.ctx.ozon_connection_id)['token_hash']!=binding['token_hash']


@pytest.mark.asyncio
async def test_accountant_manager_group_and_stale_shop_cannot_create_url(ui):
    enable_ui(ui)
    for uid,role in ((103,'accountant'),(104,'manager'),(105,'owner')):
        ui.repo.grant_shop_access(uid,ui.shop.id,role)
        await press(ui,'/ozon_push',user=uid)
    assert ui.repo.count('ozon_push_bindings')==0
    await press(ui,'/ozon_push',chat=-123)
    assert ui.repo.count('ozon_push_bindings')==0
    await press(ui,'/ozon_push')
    menu = active(ui)
    data = inline_data(ui,menu,'🔗 Создать адрес')
    ui.ctx.shop_id = ui.repo.ensure_shop(ui.repo.ensure_seller(101).id,'Other').id
    await callback(ui,menu,data)
    assert ui.repo.count('ozon_push_bindings')==0
