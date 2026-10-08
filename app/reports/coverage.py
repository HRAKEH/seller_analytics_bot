"""Per-period completeness and last verification for each report component."""
from dataclasses import dataclass
from datetime import date, timedelta
from html import escape
import json
from .dates import readable_dates
from app.services.daily_events import (
    WB_SALES, WB_CANCELS, OZON_RETURNS, OZON_REALIZATION, DailyEventError,
    normalize_wb_sales_day, normalize_wb_cancellations_day,
    normalize_ozon_returns_day, normalize_ozon_realization_day,
)


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
    note: str | None = None


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
                  EXISTS(SELECT 1 FROM raw_payloads rp WHERE rp.source_run_id=sr.id) raw_saved,
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
                unconfirmed_ads=(conn.marketplace=='ozon' and label=='Рекламная статистика'
                                 and any(not r['raw_saved'] for r in selected))
                if conn.marketplace=='ozon' and label=='Рекламная статистика':
                    selected=[r for r in selected if r['raw_saved']]
                present={r['data_date'] for r in selected}
                checked=[]
                for d in present:
                    checks=[r['finished_at'] for r in selected if r['data_date']==d and r['finished_at']]
                    if checks:checked.append(max(checks))
                latest={}
                for r in attempts:
                    ep=r['endpoint']
                    family=('Заказы' if ep.startswith(('statistics/orders','analytics/orders'))
                            else 'Финансы' if ep.startswith('finance/') and ep not in {'finance/products/buyout',OZON_REALIZATION}
                            else 'Рекламная статистика' if ep.startswith(('promotion/','ads/','performance/')) else None)
                    if family==label:
                        primary_funnel=label=='Заказы' and conn.marketplace=='wildberries' and any(
                            x['data_date']==r['data_date'] and x['endpoint'].startswith('analytics/orders') for x in selected)
                        if primary_funnel and ep.startswith('statistics/orders'):continue
                        latest.setdefault((r['data_date'],ep),r)
                failed=tuple(d for d in dates if any(k[0]==d and r['status']=='failed' for k,r in latest.items()))
                partial=tuple(sorted({r['data_date'] for r in selected if r['status']=='partial'} |
                                    {key[0] for key,r in latest.items() if r['status']=='partial'}))
                result.append(SourceCoverage(conn.marketplace,label,len(present),len(dates),min(checked) if checked else None,
                    tuple(d for d in dates if d not in present),failed,partial,
                    'Исторические суммы Ozon без исходного ответа не подтверждены; обновите рекламу.' if unconfirmed_ads else None))
            snapshots=c.execute('''WITH ranked AS (
                SELECT sr.*,rp.payload_json,
                  ROW_NUMBER() OVER(PARTITION BY sr.endpoint,sr.data_date ORDER BY sr.finished_at DESC,sr.id DESC) rn
                FROM source_runs sr JOIN raw_payloads rp ON rp.source_run_id=sr.id
                WHERE sr.connection_id=? AND sr.data_date BETWEEN ? AND ?
                  AND sr.status IN ('success','partial') AND sr.endpoint IN (?,?,?,?))
                SELECT * FROM ranked WHERE rn=1''',
                (conn.id,start,end,WB_SALES,WB_CANCELS,OZON_RETURNS,OZON_REALIZATION)).fetchall()
            definitions=(
                (('Выкупы по дате события',WB_SALES,0),('Клиентские возвраты',WB_SALES,1),('Отмены по дате события',WB_CANCELS,None))
                if conn.marketplace=='wildberries' else
                (('Выкупы: реализация за день',OZON_REALIZATION,0),('Клиентские возвраты',OZON_RETURNS,None)))
            for label,endpoint,index in definitions:
                present=set();partial=set();checked=[]
                for row in snapshots:
                    if row['endpoint']!=endpoint:continue
                    ds=row['data_date']
                    try:
                        payload=json.loads(row['payload_json'])
                        if endpoint==WB_SALES:reading=normalize_wb_sales_day(payload,ds)[index]
                        elif endpoint==WB_CANCELS:reading=normalize_wb_cancellations_day(payload,ds)
                        elif endpoint==OZON_RETURNS:reading=normalize_ozon_returns_day(payload,ds)
                        else:reading=normalize_ozon_realization_day(payload)[index]
                        if row['status']=='success' and reading.units is not None:
                            present.add(ds);checked.append(row['finished_at'])
                        else:partial.add(ds)
                    except (DailyEventError,ValueError,TypeError,KeyError):partial.add(ds)
                latest={}
                for row in attempts:
                    if row['endpoint']==endpoint:latest.setdefault(row['data_date'],row)
                failed=tuple(ds for ds in dates if ds in latest and latest[ds]['status']=='failed')
                note='Дневная реализация требует доступа Premium Plus/Pro; отсутствие доступа не означает ноль выкупов.' if endpoint==OZON_REALIZATION else None
                result.append(SourceCoverage(conn.marketplace,label,len(present),len(dates),min(checked) if checked else None,
                    tuple(ds for ds in dates if ds not in present),failed,tuple(sorted(partial)),note))
            if conn.marketplace=='ozon':
                result.append(SourceCoverage('ozon','Отмены по дате события',0,len(dates),None,dates,
                    note='Точная дата всех отмен пока не подтверждена источниками; статусы заказов другого дня не подставляются.'))
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
        if row.note:lines.append('  '+escape(row.note))
    lines.append('Полнота событий показывает количество. Для суммы дополнительно нужны подтверждённые цены и валюта.')
    lines.append('Время проверки показывает загрузку API; начисления площадки могут корректироваться позднее.')
    return '\n'.join(lines)
