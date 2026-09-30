from pathlib import Path
from datetime import date
from app.storage import Database, Repository, MetricPoint
from app.reports import build_daily_report, format_daily

def setup_repo(tmp_path: Path):
    db=Database(tmp_path/'r.sqlite3'); db.initialize(); repo=Repository(db)
    s=repo.ensure_seller(1,'S'); shop=repo.ensure_shop(s.id,'Shop')
    wb=repo.ensure_connection(shop.id,'wildberries','WB')
    oz=repo.ensure_connection(shop.id,'ozon','Ozon')
    return repo, shop, wb, oz

def save(repo, conn, day, units, money, cancels=None, payload=None):
    pts=[MetricPoint(conn.id,day,'ordered_units',units,'units',True,'2026-09-29T08:00:00+00:00'),
         MetricPoint(conn.id,day,'ordered_revenue',money,'RUB',True,'2026-09-29T08:00:00+00:00')]
    if cancels is not None: pts.append(MetricPoint(conn.id,day,'cancellations_units',cancels,'units',True,'2026-09-29T08:00:00+00:00'))
    endpoint='analytics/orders' if conn.marketplace=='ozon' else 'statistics/orders'
    repo.record_success(conn.id,endpoint,day,payload or {'units':units,'money':money},pts)

def test_daily_report_sums_only_common_ordered_units(tmp_path):
    repo, shop, wb, oz = setup_repo(tmp_path)
    save(repo,wb,'2026-09-27',8,8000,1,{'x':'wb-prev'})
    save(repo,oz,'2026-09-27',12,12000,None,{'x':'oz-prev'})
    save(repo,wb,'2026-09-28',10,10000,2,{'x':'wb-now'})
    save(repo,oz,'2026-09-28',15,18000,None,{'x':'oz-now'})
    report=build_daily_report(repo,shop.id,date(2026,9,28))
    assert report.total_units == 25
    assert report.previous_total_units == 20
    text=format_daily(report)
    assert 'ИТОГО: 25 заказанных ед.' in text
    assert '25.0% к пред. дню' in text
    assert 'Суммы заказов по площадкам пока не складываются' in text

def test_report_keeps_last_success_and_shows_fresh_failure(tmp_path):
    repo, shop, wb, _ = setup_repo(tmp_path)
    save(repo,wb,'2026-09-28',10,10000,1,{'x':'success'})
    repo.record_failure(wb.id,'statistics/orders','2026-09-28','HTTP 500',http_status=500)
    text=format_daily(build_daily_report(repo,shop.id,date(2026,9,28)))
    assert '10 шт.' in text
    assert 'последнее обновление не удалось' in text
    assert 'HTTP 500' in text

def test_period_uses_only_days_complete_for_all_sources(tmp_path):
    from app.reports import build_period_report, format_period
    repo, shop, wb, oz = setup_repo(tmp_path)
    save(repo,wb,'2026-09-27',8,8000,1,{'x':'1'})
    save(repo,oz,'2026-09-27',12,12000,None,{'x':'2'})
    save(repo,wb,'2026-09-28',9,9000,1,{'x':'3'})  # Ozon missing -> incomplete day
    report=build_period_report(repo,shop.id,date(2026,9,28),2,'Два дня')
    assert report.complete_days==1
    assert report.total==20
    assert 'Полных дней: 1/2' in format_period(report)
