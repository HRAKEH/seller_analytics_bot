from datetime import date
import pytest
from app.services.finance import normalize_ozon_accruals
from app.reports.management import build_management_report,format_management
from app.reports.finance import FinanceReport,FinanceSource
from app.reports.formatter import format_finance
from app.storage import Database,Repository,MetricPoint


def test_billed_advertising_is_subset_of_services_including_refunds():
    payload={'accruals':[{'total_amount':-122,'non_item_fee':{'type_id':41,'accrued':-20},
                        'item_fees':[{'type_id':54,'accrued':-7},{'type_id':41,'accrued':5}],
                        'container_fees':[{'type_id':99,'accrued':-100}]}]}
    metrics={p.metric_key:p.value for p in normalize_ozon_accruals(payload,1,'2026-09-30')}
    assert metrics['services']==122
    assert metrics['finance_ad_spend']==22
    assert metrics['marketplace_net']==-122


@pytest.mark.parametrize('separate_billing',[True,False])
def test_management_never_deducts_performance_again_when_accruals_include_ads(separate_billing):
    class Repo:
        def financial_metric_totals(self,*args):
            metrics={'ordered_units':1,'ordered_revenue':1000,'services':120,
                     'marketplace_net':880,'ad_spend':40}
            if separate_billing:metrics['finance_ad_spend']=20
            return {'ozon':metrics}
        def estimated_order_cogs(self,*args):return {'ozon':{'units':1,'covered_units':1,'estimated_cost':100}}
    report=build_management_report(Repo(),1,date(2026,9,30),1)
    row=report.sources[0]
    assert row.estimated_result==780
    assert row.marketplace_expenses+row.ad_spend==120
    assert row.performance_ad_spend==40
    assert 'повторно не вычитается' in format_management(report)


def test_finance_shows_cents_and_billed_ads_as_already_included():
    report=FinanceReport('2026-09-30','2026-09-30',1,
        (FinanceSource('ozon',{'marketplace_net':13570.60,'services':3757.18,
                             'finance_ad_spend':2482.42,'ad_spend':2600},0,None),),0)
    text=format_finance(report)
    assert '13 570.60 ₽' in text and '2 482.42 ₽' in text
    assert 'уже учтена в итоге' in text and 'Performance, справочно' in text


def test_unchanged_legacy_payload_adds_billed_ads_without_duplicate_source_run(tmp_path):
    db=Database(tmp_path/'legacy.sqlite3');db.initialize();repo=Repository(db)
    shop=repo.ensure_shop(repo.ensure_seller(1).id)
    conn=repo.ensure_connection(shop.id,'ozon','Ozon')
    day='2026-09-30'
    payload={'accruals':[{'total_amount':-20,'non_item_fee':{'type_id':41,'accrued':-20}}]}
    new=normalize_ozon_accruals(payload,conn.id,day)
    old=[p for p in new if p.metric_key!='finance_ad_spend']
    original=repo.record_success(conn.id,'finance/accrual/by-day',day,payload,old)
    assert repo.latest_metric(conn.id,day,'finance_ad_spend') is None
    assert repo.record_success(conn.id,'finance/accrual/by-day',day,payload,new)==original
    assert repo.latest_metric(conn.id,day,'finance_ad_spend')['value']==20
    assert repo.count('source_runs')==1 and repo.count('metric_values')==len(new)


def test_management_uses_performance_only_on_days_without_finance(tmp_path):
    db=Database(tmp_path/'partial.sqlite3');db.initialize();repo=Repository(db)
    shop=repo.ensure_shop(repo.ensure_seller(1).id)
    conn=repo.ensure_connection(shop.id,'ozon','Ozon')
    for day,spend in [('2026-09-29',40),('2026-09-30',70)]:
        repo.record_success(conn.id,'ads/stats',day,{'spend':spend},
            [MetricPoint(conn.id,day,'ad_spend',spend,'RUB')])
    metrics={'marketplace_net':880,'services':120,'finance_ad_spend':20}
    repo.record_success(conn.id,'finance/accrual/by-day','2026-09-29',metrics,
        [MetricPoint(conn.id,'2026-09-29',k,v,'RUB') for k,v in metrics.items()])
    report=build_management_report(repo,shop.id,date(2026,9,30),2)
    row=report.sources[0]
    assert row.marketplace_expenses==100 and row.ad_spend==90
    assert row.performance_ad_spend==110 and row.unbilled_performance_ad_spend==70
    assert row.marketplace_expenses+row.ad_spend==190
    assert 'За дни без финансовых начислений' in format_management(report)
