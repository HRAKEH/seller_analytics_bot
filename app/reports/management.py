"""Management contribution view with transparent basis and data coverage."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date,timedelta
from html import escape
from app.storage import Repository

EXPENSE_KEYS=('commission','logistics','storage','acceptance','acquiring','services','penalties')

@dataclass(frozen=True)
class ManagementSource:
    marketplace: str
    ordered_revenue: float
    estimated_cogs: float
    cogs_coverage_pct: float | None
    marketplace_expenses: float
    ad_spend: float
    compensation: float
    estimated_result: float | None
    financial_sales: float | None
    marketplace_net: float | None
    goods_payable: float | None
    bank_payment: float | None

@dataclass(frozen=True)
class ManagementReport:
    start: str
    end: str
    days: int
    sources: tuple[ManagementSource,...]


def build_management_report(repo: Repository, shop_id: int, end: date, days: int=7) -> ManagementReport:
    start=end-timedelta(days=days-1)
    totals=repo.financial_metric_totals(shop_id,start.isoformat(),end.isoformat())
    cogs=repo.estimated_order_cogs(shop_id,start.isoformat(),end.isoformat())
    rows=[]
    for marketplace in ('ozon','wildberries'):
        m=totals.get(marketplace,{})
        c=cogs.get(marketplace,{})
        if not m and not c: continue
        units=float(c.get('units',0)); covered=float(c.get('covered_units',0))
        coverage=(covered/units*100) if units>0 else None
        ordered_revenue=float(m.get('ordered_revenue',0))
        estimated_cogs=float(c.get('estimated_cost',0))
        marketplace_expenses=sum(float(m.get(k,0)) for k in EXPENSE_KEYS)
        ad_spend=float(m.get('ad_spend',0)); compensation=float(m.get('compensation',0))
        result=None
        # A zero-unit row returned by estimated_order_cogs is a valid zero-COST
        # basis (for example, an expense-only day). But if there is no COGS row
        # at all while order revenue exists, treating missing product coverage as
        # zero cost would overstate the result.
        has_cogs_basis=marketplace in cogs
        if has_cogs_basis and (units<=0 or covered+1e-9>=units):
            result=ordered_revenue-estimated_cogs-marketplace_expenses-ad_spend+compensation
        rows.append(ManagementSource(marketplace,ordered_revenue,estimated_cogs,coverage,
            marketplace_expenses,ad_spend,compensation,result,
            float(m['financial_sales']) if 'financial_sales' in m else None,
            float(m['marketplace_net']) if 'marketplace_net' in m else None,
            float(m['goods_payable']) if 'goods_payable' in m else None,
            float(m['bank_payment']) if 'bank_payment' in m else None))
    return ManagementReport(start.isoformat(),end.isoformat(),days,tuple(rows))


def _money(v: float | None) -> str:
    return '—' if v is None else f'{v:,.0f}'.replace(',',' ')+' ₽'


def format_management(report: ManagementReport) -> str:
    lines=[f'📈 <b>Управленческий результат · {report.start} — {report.end}</b>','━━━━━━━━━━━━━━━━',
           '⚠️ Это <b>операционная оценка</b>, не бухгалтерская чистая прибыль: заказы, финансовые удержания и реклама могут иметь разные лаги.']
    if not report.sources:
        return '\n'.join(lines+['','📭 Данных для расчёта пока нет.'])
    total=0.0; complete=True
    for row in report.sources:
        icon='🟣' if row.marketplace=='ozon' else '🔵'
        cov='—' if row.cogs_coverage_pct is None else f'{row.cogs_coverage_pct:.0f}%'
        lines += ['',f'{icon} <b>{escape(row.marketplace.title())}</b>',
                  f'Заказы в деньгах: {_money(row.ordered_revenue)}',
                  f'Оценочная себестоимость: {_money(row.estimated_cogs)} · покрытие {cov}',
                  f'Известные расходы маркетплейса: {_money(row.marketplace_expenses)}',
                  f'Реклама: {_money(row.ad_spend)}',
                  f'Компенсации/доплаты: {_money(row.compensation)}',
                  f'<b>Оценочный результат: {_money(row.estimated_result)}</b>']
        if row.estimated_result is None: complete=False
        else: total += row.estimated_result
        facts=[]
        if row.financial_sales is not None: facts.append('фин. продажи '+_money(row.financial_sales))
        if row.marketplace_net is not None: facts.append('нетто '+_money(row.marketplace_net))
        if row.goods_payable is not None: facts.append('к перечислению '+_money(row.goods_payable))
        if row.bank_payment is not None: facts.append('банк '+_money(row.bank_payment))
        if facts: lines.append('Финансовый источник: '+ ' · '.join(facts))
    lines += ['','━━━━━━━━━━━━━━━━']
    lines.append(f'Суммарная оценка: <b>{_money(total)}</b>' if complete else 'Суммарная оценка: — (неполная себестоимость/данные)')
    return '\n'.join(lines)
