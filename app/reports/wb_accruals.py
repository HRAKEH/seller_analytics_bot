"""WB accrual totals traced to saved financial report headers, not orders."""
from dataclasses import dataclass
from datetime import date
from html import escape
import json
from pathlib import Path

from app.services.finance import normalize_wb_finance_report
from app.services.money import money_sum
from .accruals import export_accrual_ledger
from .dates import readable_dates


@dataclass(frozen=True)
class WbAccrualLedger:
    start: str
    end: str
    loaded_dates: tuple[str,...]
    rows: tuple[dict,...]
    source_run_ids: tuple[int,...] = ()


def build_wb_accrual_ledger(repo, shop_id: int, start: str, end: str, *, source_run_ids=None) -> WbAccrualLedger:
    pinned = source_run_ids is not None
    ids = tuple(dict.fromkeys(int(value) for value in (source_run_ids or ())))
    selection = ' AND sr.id IN (' + ','.join('?' for _ in ids) + ')' if ids else (' AND 0' if pinned else '')
    with repo.db.connect() as c:
        payloads=c.execute('''WITH ranked AS (
            SELECT sr.id,sr.connection_id,sr.data_date,sr.finished_at,rp.payload_json,
              ROW_NUMBER() OVER(PARTITION BY sr.connection_id,sr.data_date
                ORDER BY sr.finished_at DESC,sr.id DESC) rn
            FROM source_runs sr JOIN raw_payloads rp ON rp.source_run_id=sr.id
            JOIN marketplace_connections mc ON mc.id=sr.connection_id
            WHERE mc.shop_id=? AND mc.marketplace='wildberries'
              AND sr.endpoint='finance/sales-reports/list' AND sr.status='success'
              AND sr.data_date BETWEEN ? AND ?''' + selection + ''')
            SELECT * FROM ranked WHERE rn=1 ORDER BY finished_at DESC,id DESC''',(shop_id,start,end,*ids)).fetchall()
    if pinned and {row['id'] for row in payloads} != set(ids):
        raise ValueError('Сохранённый источник недоступен. Откройте начисления заново.')
    if pinned:
        order={value:index for index,value in enumerate(ids)}
        payloads=sorted(payloads,key=lambda row:order[row['id']])
    rows=[]; seen=set()
    keys=('financial_sales','goods_payable','bank_payment','logistics','storage','acceptance','services','penalties','compensation')
    for source in payloads:
        payload=json.loads(source['payload_json'])
        reports=payload.get('reports') if isinstance(payload,dict) else payload
        if not isinstance(reports,list):continue
        for index,report in enumerate(reports,1):
            if not isinstance(report,dict):continue
            report_id=report.get('reportId')
            identity=(source['connection_id'],str(report_id)) if report_id is not None else (source['id'],index)
            if identity in seen:continue
            seen.add(identity)
            _,points=normalize_wb_finance_report(report,source['connection_id'])
            metrics={p.metric_key:p.value for p in points}
            rows.append({'date':source['data_date'],'source_run_id':source['id'],'report_id':report_id or '',
                'report_type':report.get('reportType') if report.get('reportType') is not None else '',
                'report_from':str(report.get('dateFrom') or '')[:10],
                'report_to':str(report.get('dateTo') or '')[:10],
                **{key:metrics.get(key) for key in keys},'verified_at':source['finished_at']})
    rows.sort(key=lambda r:(r['date'],str(r['report_id'])))
    return WbAccrualLedger(start,end,tuple(sorted({r['date'] for r in rows})),tuple(rows),tuple(p['id'] for p in payloads))


@readable_dates
def format_wb_accrual_ledger(ledger: WbAccrualLedger) -> str:
    count=(date.fromisoformat(ledger.end)-date.fromisoformat(ledger.start)).days+1
    lines=[f'🧮 <b>Начисления WB · {ledger.start} — {ledger.end}</b>',
        f'Сохранено дней с отчётами: {len(ledger.loaded_dates)}/{count} · финансовых отчётов: {len(ledger.rows)}']
    if not ledger.rows:return '\n'.join(lines+['📭 Нет сохранённых финансовых отчётов WB за этот период. Нажмите «Обновить финансы». WB может публиковать их с задержкой.'])
    labels={'financial_sales':'Продажи по финансовому отчёту','goods_payable':'К перечислению за товар',
        'logistics':'Логистика','storage':'Хранение','acceptance':'Приёмка','services':'Прочие удержания',
        'penalties':'Штрафы','compensation':'Компенсации и доплаты','bank_payment':'Итог к оплате по финансовому отчёту'}
    for key,label in labels.items():
        values=[r[key] for r in ledger.rows if r[key] is not None]
        text=f'{money_sum(values):,.2f} ₽'.replace(',',' ') if values else 'нет значения в API'
        lines.append(f'{label}: <b>{text}</b>')
        if values and len(values)<len(ledger.rows):lines.append('  ⚠️ Поле есть не во всех отчётах; показана доступная часть.')
    periods=sorted({(r['report_from'],r['report_to']) for r in ledger.rows})
    lines.append('Периоды самих отчётов WB: '+escape('; '.join(f'{a or "—"} — {b or "—"}' for a,b in periods)))
    if any(a!=b for a,b in periods):lines.append('⚠️ Среди отчётов есть период больше одного дня. Его сумма целиком относится к этому периоду, а не к дате загрузки.')
    if len(ledger.loaded_dates)<count:lines.append('⚠️ Не все дни имеют опубликованный и загруженный финансовый отчёт.')
    lines+=['Это начисления по финансовым отчётам WB, не новые заказы и не чистая прибыль.',
        'Удержания из суммы «к перечислению за товар» повторно не вычитаются. Итог к оплате не подтверждает поступление денег в банк.',
        'CSV содержит строки итогов отчётов, их номера, периоды и ссылки на сохранённые ответы API.']
    return '\n'.join(lines)


def export_wb_accrual_ledger(ledger: WbAccrualLedger, path: Path, *, timezone='Europe/Moscow', shop_name='') -> Path:
    return export_accrual_ledger(ledger,path,timezone=timezone,shop_name=shop_name,marketplace='wb')
