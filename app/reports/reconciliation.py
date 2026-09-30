"""Operational-to-finance reconciliation report.

Differences are presented as timing/reconciliation gaps, not as accounting errors:
operational and financial sources settle on different dates.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, timedelta
from html import escape
from app.storage import Repository


@dataclass(frozen=True)
class ReconciliationRow:
    marketplace: str
    marketplace_sku: str
    internal_sku: str
    name: str
    ordered_units: float
    cancelled_units: float
    posting_units: float
    sale_units: float
    return_units: float
    finance_gross: float


@dataclass(frozen=True)
class ReconciliationReport:
    start: str
    end: str
    rows: tuple[ReconciliationRow,...]
    match_stats: dict[str,dict[str,float]]


def build_reconciliation_report(repo: Repository, shop_id: int, end: date, days: int=14) -> ReconciliationReport:
    start=end-timedelta(days=days-1)
    summary={(str(r.get('marketplace')),str(r.get('marketplace_sku') or '')):r
             for r in repo.reconciliation_summary(shop_id,start.isoformat(),end.isoformat())
             if r.get('marketplace_sku') is not None}
    economics={(str(r.get('marketplace')),str(r.get('marketplace_sku') or '')):r
               for r in repo.sku_economics(shop_id,start.isoformat(),end.isoformat())}
    rows=[]
    for key in sorted(set(summary)|set(economics)):
        rec=summary.get(key,{}) ; eco=economics.get(key,{})
        rows.append(ReconciliationRow(
            key[0],key[1],str(eco.get('internal_sku') or rec.get('internal_sku') or ''),
            str(eco.get('name') or rec.get('name') or key[1]),float(eco.get('units') or 0),
            float(rec.get('cancelled_units') or 0),float(rec.get('posting_units') or 0),
            float(rec.get('sale_units') or 0),float(rec.get('return_units') or 0),
            float(rec.get('finance_gross') or 0)))
    rows.sort(key=lambda r:(r.marketplace,-max(r.ordered_units,r.sale_units,r.posting_units)))
    return ReconciliationReport(start.isoformat(),end.isoformat(),tuple(rows),
                                repo.reconciliation_match_stats(shop_id,start.isoformat(),end.isoformat()))


def _money(v: float) -> str:
    return f'{v:,.0f}'.replace(',',' ')+' ₽'


def format_reconciliation(report: ReconciliationReport, limit: int=12) -> str:
    lines=[f'🔎 <b>Сверка · {report.start} — {report.end}</b>','━━━━━━━━━━━━━━━━',
           'Разница между этапами может быть нормальной задержкой: заказы, продажи/возвраты и финансы обновляются в разные даты.']
    for market,label in (('wildberries','🔵 Wildberries'),('ozon','🟣 Ozon')):
        stat=report.match_stats.get(market,{})
        total=stat.get('total',0); matched=stat.get('matched',0); pct=stat.get('coverage_pct',0)
        if market=='wildberries':
            note='точная связь order → sale/return/finance по srid'
        else:
            note='точная связь posting → finance по posting_number; Analytics-заказ не имеет posting ID'
        lines += ['',f'<b>{label}</b>',f'ID-сверка: {matched:g}/{total:g} · {pct:.0f}% · <i>{note}</i>']
        market_rows=[r for r in report.rows if r.marketplace==market]
        if not market_rows:
            lines.append('  📭 Нет событий для сверки.')
            continue
        for row in market_rows[:limit]:
            if market=='wildberries':
                net_orders=max(0.0,row.ordered_units-row.cancelled_units)
                net_sales=row.sale_units-row.return_units
                gap=net_orders-net_sales
                detail=f'заказ {row.ordered_units:g} · отмена {row.cancelled_units:g} · продажа {row.sale_units:g} · возврат {row.return_units:g} · gap {gap:+g}'
            else:
                gap=row.ordered_units-row.posting_units
                detail=f'заказано {row.ordered_units:g} · posting {row.posting_units:g} · gap {gap:+g}'
            lines += [f'• <b>{escape(row.name)}</b> · SKU {escape(row.marketplace_sku)}',
                      f'  {detail}',f'  фин. продажи источника: {_money(row.finance_gross)}']
    lines += ['', 'ℹ️ Gap — сигнал для проверки, а не автоматически ошибка. Для свежих дат финансовое покрытие обычно ниже из-за задержки расчётов.']
    return '\n'.join(lines)
