from __future__ import annotations
import sqlite3
from datetime import date
from pathlib import Path
import httpx
import pytest

from app.integrations import OzonClient, WildberriesClient
from app.storage import Database, Repository, LATEST_SCHEMA_VERSION
from app.services.demo import enable_demo, disable_demo
from app.services.readiness import build_readiness
import app.storage.database as dbmod


def make_repo(tmp_path: Path):
    db=Database(tmp_path/'phase19.sqlite3'); assert db.initialize()==LATEST_SCHEMA_VERSION
    repo=Repository(db); seller=repo.ensure_seller(190,'Seller'); shop=repo.ensure_shop(seller.id,'Shop')
    repo.ensure_shop_preferences(shop.id)
    return db,repo,shop


def test_schema_v14_onboarding_fields_and_readiness_table(tmp_path):
    db,repo,shop=make_repo(tmp_path)
    assert db.schema_version()==14
    pref=repo.get_shop_preferences(shop.id)
    assert pref and pref.demo_mode is False and pref.onboarding_version==''
    with db.connect() as c:
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'shop_readiness_snapshots' in tables


def test_demo_seed_makes_local_readiness_ready_and_disables_network(tmp_path):
    db,repo,shop=make_repo(tmp_path)
    result=enable_demo(repo,shop.id,today=date(2026,9,30))
    assert result['products']==3
    pref=repo.get_shop_preferences(shop.id); assert pref and pref.demo_mode and pref.setup_completed
    class Ctx:
        repository=repo; shop_id=shop.id
        collector=type('Collector',(),{'wb':None,'ozon':None,'ozon_performance':None})()
        def preferences(self): return repo.get_shop_preferences(shop.id)
        def configured_sources(self): return 2 if self.preferences().demo_mode else 0
    import asyncio
    readiness=asyncio.run(build_readiness(Ctx(),live=False,persist=True))
    assert readiness.status=='ready'
    assert repo.latest_readiness_snapshot(shop.id)['status']=='ready'
    disable_demo(repo,shop.id); assert not repo.get_shop_preferences(shop.id).demo_mode
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM products WHERE shop_id=? AND internal_sku LIKE 'DEMO-%'",(shop.id,)).fetchone()[0]==0
        assert c.execute("SELECT COUNT(*) FROM marketplace_connections WHERE shop_id=? AND display_name LIKE '% Demo'",(shop.id,)).fetchone()[0]==0

@pytest.mark.asyncio
async def test_live_probe_transport_endpoints():
    seen=[]
    async def handler(request: httpx.Request):
        seen.append((request.method,request.url.path,request.url.host))
        if request.url.path=='/v1/seller/info': return httpx.Response(200,json={'company':{'name':'Demo Ozon'}})
        if request.url.path=='/v1/roles': return httpx.Response(200,json={'roles':[{'methods':['/v1/analytics/data']}],'expires_at':'2027-01-01T00:00:00Z'})
        if request.url.path=='/api/v1/seller-info': return httpx.Response(200,json={'name':'Demo WB','sid':'s'})
        if request.url.path=='/ping': return httpx.Response(200,json={'Status':'OK'})
        return httpx.Response(404,json={})
    transport=httpx.MockTransport(handler)
    oz=OzonClient('1','key',min_interval=0,transport=transport)
    wb=WildberriesClient('token',min_interval=0,transport=transport)
    assert (await oz.seller_info()).ok
    assert (await oz.api_roles()).ok
    assert (await wb.seller_info()).ok
    assert (await wb.ping('https://finance-api.wildberries.ru')).ok
    await oz.close(); await wb.close()
    assert ('POST','/v1/roles','api-seller.ozon.ru') in seen
    assert ('GET','/ping','finance-api.wildberries.ru') in seen


def test_v13_migrates_to_v14(tmp_path):
    path=tmp_path/'v13.sqlite3'; c=sqlite3.connect(path)
    c.execute('CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
    for version in range(1,14):
        dbmod.MIGRATIONS[version](c); c.execute("INSERT INTO schema_migrations VALUES(?,datetime('now'))",(version,))
    c.execute("INSERT INTO sellers(telegram_user_id,name,timezone,active,created_at) VALUES(1,'S','Europe/Moscow',1,datetime('now'))")
    sid=c.execute('SELECT id FROM sellers').fetchone()[0]
    c.execute("INSERT INTO shops(seller_id,name,currency,active,created_at) VALUES(?,'Shop','RUB',1,datetime('now'))",(sid,))
    shop_id=c.execute('SELECT id FROM shops').fetchone()[0]
    c.execute("""INSERT INTO shop_preferences(shop_id,timezone,report_time,product_report_days,stock_velocity_days,stock_risk_days,
      finance_lookback_days,alerts_enabled,alerts_interval_minutes,alert_order_drop_pct,alert_order_lookback_days,alert_api_stale_hours,
      alert_drr_pct,alert_cooldown_minutes,setup_completed,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
      (shop_id,'Europe/Moscow','09:00',7,14,14,14,1,60,35,7,26,25,1440,1))
    c.commit(); c.close()
    db=Database(path); assert db.initialize()==LATEST_SCHEMA_VERSION; assert db.quick_check()
    repo=Repository(db); pref=repo.get_shop_preferences(shop_id)
    assert pref.setup_completed and not pref.demo_mode and pref.onboarding_version==''


def test_demo_refuses_to_mix_with_real_shop(tmp_path):
    db,repo,shop=make_repo(tmp_path)
    repo.ensure_product(shop.id,'REAL-1','Real product')
    with pytest.raises(ValueError,match='пустом магазине'):
        enable_demo(repo,shop.id,today=date(2026,9,30))
