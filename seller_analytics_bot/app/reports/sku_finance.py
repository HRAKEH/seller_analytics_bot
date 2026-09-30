"""Conservative SKU economics based on orders and historical cost.

This is deliberately not labelled profit: marketplace fees, logistics and ads are
not allocated to a SKU unless the source provides a trustworthy SKU allocation.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, timedelta
from html import escape

from app.storage import Repository

@dataclass(frozen=True)
class SkuEconomicsRow:
    marketplace: str
    internal_sku: str
    marketplace_sku: str
    name: str
    units: float
    order_revenue: float
    estimated_cost: float
    coverage_pct: float | None
    contribution_before_marketplace: float | None
    contribution_after_known_expenses: float | None
    financial_metrics: dict[str,float]
    advertising_metrics: dict[str,float]

@dataclass(frozen=True)
class SkuEconomicsReport:
    start: str
    end: str
    days: int
    rows: tuple[SkuEconomicsRow, ...]


def build_sku_economics(repo: Repository, shop_id: int, end: date, days: int = 7) -> SkuEconomicsReport:
    start=end-timedelta(days=days-1)
    actual=repo.sku_financial_totals(shop_id,start.isoformat(),end.isoformat())
    ads=repo.ad_product_totals_map(shop_id,start.isoformat(),end.isoformat())
    rows=[]
    for raw in repo.sku_economics(shop_id,start.isoformat(),end.isoformat()):
        marketplace=str(raw['marketplace']); sku=str(raw['marketplace_sku'])
        units=float(raw.get('units') or 0); covered=float(raw.get('covered_units') or 0)
        revenue=float(raw.get('order_revenue') or 0); cost=float(raw.get('estimated_cost') or 0)
        coverage=(covered/units*100) if units>0 else None
        contribution=(revenue-cost) if units>0 and covered+1e-9>=units else None
        fm=actual.get((marketplace,sku),{}); am=ads.get((marketplace,sku),{})
        known_marketplace=sum(float(fm.get(k,0)) for k in ('commission','logistics','storage','acceptance','services','penalties'))
        ad_spend=float(am.get('ad_spend',0))
        after=(contribution-known_marketplace-ad_spend+float(fm.get('compensation',0))) if contribution is not None else None
        rows.append(SkuEconomicsRow(marketplace,str(raw['internal_sku']),sku,str(raw['name']),units,revenue,cost,coverage,contribution,after,fm,am))
    rows.sort(key=lambda x:x.order_revenue,reverse=True)
    return SkuEconomicsReport(start.isoformat(),end.isoformat(),days,tuple(rows))


def _money(v: float) -> str:
    return f'{v:,.0f}'.replace(',',' ')+' ₽'


def format_sku_economics(report: SkuEconomicsReport, limit: int = 10) -> str:
    lines=[f'🧾 <b>SKU-экономика · {report.start} — {report.end}</b>',
           '━━━━━━━━━━━━━━━━',
           'Это оценка по заказам, <b>не чистая прибыль</b>. Комиссии, логистика и реклама не распределяются по SKU без подтверждённой детализации источника.']
    if not report.rows:
        return '\n'.join(lines+['','📭 Товарных данных за период нет.'])
    for i,row in enumerate(report.rows[:limit],start=1):
        icon='🟣' if row.marketplace=='ozon' else '🔵'
        cov='—' if row.coverage_pct is None else f'{row.coverage_pct:.0f}%'
        lines += ['',f'{i}. {icon} <b>{escape(row.name)}</b>',
                  f'   SKU {escape(row.marketplace_sku)} · {row.units:g} ед. · {_money(row.order_revenue)}',
                  f'   оценка себестоимости: {_money(row.estimated_cost)} · покрытие {cov}']
        if row.contribution_before_marketplace is not None:
            lines.append(f'   до расходов маркетплейса: <b>{_money(row.contribution_before_marketplace)}</b>')
        else:
            lines.append('   до расходов маркетплейса: — (себестоимость заполнена не полностью)')
        if row.financial_metrics:
            fm=row.financial_metrics
            if 'financial_sales' in fm:
                lines.append(f'   фин. продажи источника: {_money(fm["financial_sales"])}')
            expense_keys=('commission','logistics','storage','acceptance','services','penalties')
            known=sum(float(fm.get(k,0)) for k in expense_keys)
            if known:
                lines.append(f'   известные расходы маркетплейса по SKU: {_money(known)}')
            if 'goods_payable' in fm:
                lines.append(f'   к перечислению за товар: {_money(fm["goods_payable"])}')
        if row.advertising_metrics:
            am=row.advertising_metrics; spend=float(am.get('ad_spend',0)); sales=float(am.get('ad_attributed_sales',0))
            drr=(spend/sales*100) if sales>0 else None
            lines.append(f'   реклама по SKU: {_money(spend)} · атриб. продажи {_money(sales)} · ДРР {"—" if drr is None else f"{drr:.1f}%"}')
        if row.contribution_after_known_expenses is not None:
            lines.append(f'   <b>после известных SKU-расходов: {_money(row.contribution_after_known_expenses)}</b>')
    return '\n'.join(lines)
