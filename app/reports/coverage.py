"""Per-period completeness and last verification for each report component."""
from dataclasses import dataclass
from datetime import date, timedelta
from html import escape
from .dates import readable_dates


@dataclass(frozen=True)
class SourceCoverage:
    marketplace: str
    component: str
    available_days: int
    expected_days: int
    verified_at: str | None
    missing_dates: tuple[str,...] = ()
    failed_dates: tuple[str,...] = ()
    partial_dates: tuple[str,...] = ()


def build_source_coverage(repo, shop_id: int, start: str, end: str) -> tuple[SourceCoverage,...]:
    first=date.fromisoformat(start); last=date.fromisoformat(end)
    count=(last-first).days+1
    if not 1<=count<=365:raise ValueError('Период источников: от 1 до 365 дней')
    dates=tuple((first+timedelta(days=i)).isoformat() for i in range(count))
    result=[]
    with repo.db.connect() as c:
        for conn in repo.list_connections(shop_id):
            if not conn.enabled:continue
            rows=c.execute('''WITH ranked AS (
                SELECT mv.data_date,mv.metric_key,sr.finished_at,sr.endpoint,sr.status,
                  ROW_NUMBER() OVER(PARTITION BY mv.data_date,mv.metric_key ORDER BY mv.fetched_at DESC,mv.id DESC) rn
                FROM metric_values mv JOIN source_runs sr ON sr.id=mv.source_run_id
                WHERE mv.connection_id=? AND mv.data_date BETWEEN ? AND ? AND sr.status IN ('success','partial'))
                SELECT * FROM ranked WHERE rn=1''',(conn.id,start,end)).fetchall()
            attempts=c.execute('''SELECT data_date,endpoint,status,finished_at FROM source_runs
                WHERE connection_id=? AND data_date BETWEEN ? AND ?
                ORDER BY COALESCE(finished_at,started_at) DESC,id DESC''',(conn.id,start,end)).fetchall()
            components={'Заказы':{'ordered_units'},
                'Финансы':{'marketplace_net'} if conn.marketplace=='ozon' else {'bank_payment','goods_payable','financial_sales'},
                'Рекламная статистика':{'ad_spend'}}
            for label,keys in components.items():
                selected=[r for r in rows if r['metric_key'] in keys]
                present={r['data_date'] for r in selected}
                checked=[]
                for d in present:
                    checks=[r['finished_at'] for r in selected if r['data_date']==d and r['finished_at']]
                    if checks:checked.append(max(checks))
                latest={}
                for r in attempts:
                    ep=r['endpoint']
                    family=('Заказы' if ep.startswith(('statistics/orders','analytics/orders'))
                            else 'Финансы' if ep.startswith('finance/') and ep!='finance/products/buyout'
                            else 'Рекламная статистика' if ep.startswith(('promotion/','ads/','performance/')) else None)
                    if family==label:
                        primary_funnel=label=='Заказы' and conn.marketplace=='wildberries' and any(
                            x['data_date']==r['data_date'] and x['endpoint'].startswith('analytics/orders') for x in selected)
                        if primary_funnel and ep.startswith('statistics/orders'):continue
                        latest.setdefault((r['data_date'],ep),r)
                failed=tuple(d for d in dates if any(k[0]==d and r['status']=='failed' for k,r in latest.items()))
                partial=tuple(sorted({r['data_date'] for r in selected if r['status']=='partial'}))
                result.append(SourceCoverage(conn.marketplace,label,len(present),len(dates),min(checked) if checked else None,
                    tuple(d for d in dates if d not in present),failed,partial))
    return tuple(result)


@readable_dates
def format_source_coverage(rows) -> str:
    lines=['📡 <b>Источники и полнота периода</b>']
    for row in rows:
        complete=row.available_days==row.expected_days and not row.failed_dates and not row.partial_dates
        icon='✅' if complete else '⚠️'
        market='WB' if row.marketplace=='wildberries' else 'Ozon'
        lines.append(f'{icon} {market} · {row.component}: {row.available_days}/{row.expected_days} дней')
        if row.verified_at:lines.append('  самая ранняя проверка API в периоде: '+escape(row.verified_at))
        if row.missing_dates:
            dates=', '.join(row.missing_dates[:4])+('…' if len(row.missing_dates)>4 else '')
            lines.append('  нет данных: '+dates)
        if row.failed_dates:lines.append('  есть неуспешные повторные запросы; сохранён предыдущий успешный ответ')
        if row.partial_dates:lines.append('  часть ответов API неполная; итог предварительный')
    lines.append('Время проверки показывает загрузку API; начисления площадки могут корректироваться позднее.')
    return '\n'.join(lines)
