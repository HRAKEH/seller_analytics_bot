"""Trace Ozon daily finance to the exact persisted API operations."""
from dataclasses import dataclass
from datetime import date
import csv
import json
from pathlib import Path

from app.services.finance import normalize_ozon_accruals
from app.services.money import money_sum
from .dates import readable_dates


@dataclass(frozen=True)
class AccrualLedger:
    start: str
    end: str
    loaded_dates: tuple[str,...]
    rows: tuple[dict,...]


def _fee_types(node):
    result=set()
    if isinstance(node,dict):
        if 'type_id' in node:result.add(str(node['type_id']))
        for value in node.values():result.update(_fee_types(value))
    elif isinstance(node,list):
        for value in node:result.update(_fee_types(value))
    return result


def build_accrual_ledger(repo, shop_id: int, start: str, end: str) -> AccrualLedger:
    with repo.db.connect() as c:
        payloads=c.execute('''WITH ranked AS (
            SELECT sr.id,sr.connection_id,sr.data_date,sr.finished_at,rp.payload_json,
              ROW_NUMBER() OVER(PARTITION BY sr.connection_id,sr.data_date
                ORDER BY sr.finished_at DESC,sr.id DESC) rn
            FROM source_runs sr JOIN raw_payloads rp ON rp.source_run_id=sr.id
            JOIN marketplace_connections mc ON mc.id=sr.connection_id
            WHERE mc.shop_id=? AND mc.marketplace='ozon' AND sr.endpoint='finance/accrual/by-day'
              AND sr.status='success' AND sr.data_date BETWEEN ? AND ?)
            SELECT * FROM ranked WHERE rn=1 ORDER BY data_date,connection_id''',(shop_id,start,end)).fetchall()
    rows=[]
    for source in payloads:
        payload=json.loads(source['payload_json'])
        for index,operation in enumerate(payload['accruals'],1):
            metrics={p.metric_key:p.value for p in normalize_ozon_accruals({'accruals':[operation]},source['connection_id'],source['data_date'])}
            posting=operation.get('posting') or {}
            products=posting.get('products') or posting.get('items') or []
            products=[p for p in products if isinstance(p,dict)] if isinstance(products,list) else []
            types=set()
            for key in ('item_fees','non_item_fee','container_fees'):types.update(_fee_types(operation.get(key)))
            explained=money_sum([metrics['financial_sales'],-metrics['commission'],-metrics['logistics'],-metrics['services']])
            rows.append({
                'date':source['data_date'],'source_run_id':source['id'],'operation_index':index,
                'operation_id':operation.get('id') or operation.get('accrual_id') or '',
                'unit_number':operation.get('unit_number') or '',
                'operation_date':operation.get('date') or '',
                'category':operation.get('accrued_category') or '',
                'posting_number':posting.get('posting_number') or posting.get('number') or '',
                'sku':', '.join(str(p.get('sku') or '') for p in products),
                'fee_type_ids':', '.join(sorted(types)),
                'financial_sales':metrics['financial_sales'],'commission':metrics['commission'],
                'logistics':metrics['logistics'],'services':metrics['services'],
                'ads_already_in_services':metrics['finance_ad_spend'],'marketplace_net':metrics['marketplace_net'],
                'other_components':money_sum([metrics['marketplace_net'],-explained]),
                'verified_at':source['finished_at'],
            })
    return AccrualLedger(start,end,tuple(sorted({p['data_date'] for p in payloads})),tuple(rows))


@readable_dates
def format_accrual_ledger(ledger: AccrualLedger) -> str:
    expected=(date.fromisoformat(ledger.end)-date.fromisoformat(ledger.start)).days+1
    lines=[f'🧮 <b>Начисления Ozon · {ledger.start} — {ledger.end}</b>',
           f'Сохранено дней: {len(ledger.loaded_dates)}/{expected} · операций: {len(ledger.rows)}']
    if not ledger.loaded_dates:return '\n'.join(lines+['Нет сохранённого ответа по начислениям. Сначала обновите выбранный период.'])
    labels={'financial_sales':'Продажи по финансовому источнику','commission':'Комиссия',
        'logistics':'Логистика','services':'Услуги и прочие удержания',
        'ads_already_in_services':'Из услуг: реклама, уже включена','other_components':'Другие составляющие итоговой суммы',
        'marketplace_net':'Итог начислений после удержаний'}
    for key,label in labels.items():
        value=money_sum(r[key] for r in ledger.rows)
        if key!='other_components' or value:lines.append(f'{label}: <b>{value:,.2f} ₽</b>'.replace(',',' '))
    if len(ledger.loaded_dates)<expected:lines.append('⚠️ Период загружен не полностью. Сумма относится только к сохранённым дням.')
    lines+=['Реклама из начислений повторно не вычитается. Количество новых заказов может отличаться от количества финансовых операций.',
        'Это начисления API за загруженные дни, не выплата на банк и не чистая прибыль. В CSV есть source_run_id, строка операции и типы удержаний для сверки.']
    return '\n'.join(lines)


def export_accrual_ledger(ledger: AccrualLedger, path: Path) -> Path:
    from app.services.exporting import _safe_row
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(ledger.rows[0]),delimiter=';')
        writer.writeheader()
        writer.writerows(_safe_row(row) for row in ledger.rows)
    return path
