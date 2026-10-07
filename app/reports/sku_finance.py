"""Conservative SKU economics based on orders and historical cost.

This is deliberately not labelled profit: marketplace fees, logistics and ads are
not allocated to a SKU unless the source provides a trustworthy SKU allocation.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, timedelta
from html import escape

from app.storage import Repository
from app.marketplaces import OZON_ICON, WB_ICON
from .management import EXPENSE_KEYS
from app.services.money import money_sum
from .dates import readable_dates

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
    performance_expense: float = 0.0
    performance_reference: float = 0.0
    warnings: tuple[str,...] = ()
    data_complete: bool = True

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
    ad_accounting=(repo.ozon_sku_ad_accounting(shop_id,start.isoformat(),end.isoformat())
                   if hasattr(repo,'ozon_sku_ad_accounting') else None)
    rows=[]
    for raw in repo.sku_economics(shop_id,start.isoformat(),end.isoformat()):
        marketplace=str(raw['marketplace']); sku=str(raw['marketplace_sku'])
        units=float(raw.get('units') or 0); covered=float(raw.get('covered_units') or 0)
        revenue=float(raw.get('order_revenue') or 0); cost=float(raw.get('estimated_cost') or 0)
        coverage=(covered/units*100) if units>0 else None
        contribution=money_sum([revenue,-cost]) if raw.get('data_complete',True) and units>0 and covered+1e-9>=units else None
        fm=actual.get((marketplace,sku),{}); am=ads.get((marketplace,sku),{})
        known_marketplace=money_sum(fm.get(k,0) for k in EXPENSE_KEYS)
        ad_spend=float(am.get('ad_spend',0))
        reference=0.0; warnings=[]
        if marketplace=='ozon':
            if ad_accounting is not None:
                accounting=ad_accounting.get(sku,{})
                ad_spend=float(accounting.get('expense',0)); reference=float(accounting.get('reference',0))
                if accounting.get('unbilled_days'):
                    warnings.append('За дни без начислений расход рекламы взят из Performance; оценка предварительная.')
            elif 'finance_ad_spend' in fm:
                reference=ad_spend; ad_spend=0.0
            warnings.append('Расходы Ozon без привязки к товару не распределены; это результат после известных SKU-расходов.')
        else:
            warnings.append('Комиссия WB показывается только при явной детализации. Сумма «к перечислению за товар» уже учитывает удержания по товару.')
        if not raw.get('data_complete',True):warnings.append('Заказы и суммы загружены не полностью: результат не рассчитывается.')
        after=money_sum([contribution,-known_marketplace,-ad_spend,fm.get('compensation',0)]) if contribution is not None else None
        rows.append(SkuEconomicsRow(marketplace,str(raw['internal_sku']),sku,str(raw['name']),units,revenue,cost,coverage,contribution,after,fm,am,ad_spend,reference,tuple(warnings),bool(raw.get('data_complete',True))))
    rows.sort(key=lambda x:x.order_revenue,reverse=True)
    return SkuEconomicsReport(start.isoformat(),end.isoformat(),days,tuple(rows))


def _money(v: float) -> str:
    return f'{v:,.2f}'.replace(',',' ')+' ₽'


@readable_dates
def format_sku_economics(report: SkuEconomicsReport, limit: int | None = None) -> str:
    lines=[f'🧾 <b>SKU-экономика · {report.start} — {report.end}</b>',
           '━━━━━━━━━━━━━━━━',
           'Это оценка по заказам, <b>не чистая прибыль</b>. Комиссии, логистика и реклама не распределяются по SKU без подтверждённой детализации источника.']
    if not report.rows:
        return '\n'.join(lines+['','📭 Товарных данных за период нет.'])
    selected=[]
    for market in ('wildberries','ozon'):
        rows=[r for r in report.rows if r.marketplace==market and (r.units or r.order_revenue or r.financial_metrics or r.advertising_metrics)]
        label='WB' if market=='wildberries' else 'Ozon'
        shown=len(rows) if limit is None else min(limit,len(rows))
        lines.append(f'\n{label}: товаров с данными — {len(rows)}. Показано: {shown} · по сумме заказов.')
        selected.extend(rows[:limit])
    for i,row in enumerate(selected,start=1):
        icon=OZON_ICON if row.marketplace=='ozon' else WB_ICON
        cov='—' if row.coverage_pct is None else f'{row.coverage_pct:.0f}%'
        market='Ozon' if row.marketplace=='ozon' else 'WB'
        lines += ['',f'{i}. {icon} {market} · <b>{escape(row.name)}</b>',
                  f'   SKU <code>{escape(row.marketplace_sku)}</code> · {row.units:g} ед. · {_money(row.order_revenue)}',
                  f'   себестоимость заполненных товаров: {_money(row.estimated_cost) if row.coverage_pct else "не задана"} · покрытие {cov}']
        if row.contribution_before_marketplace is not None:
            lines.append(f'   до расходов маркетплейса: <b>{_money(row.contribution_before_marketplace)}</b>')
        else:
            reason='данные заказов неполные' if not row.data_complete else 'себестоимость заполнена не полностью'
            lines.append(f'   до расходов маркетплейса: — ({reason})')
        if row.financial_metrics:
            fm=row.financial_metrics
            if 'financial_sales' in fm:
                lines.append(f'   фин. продажи источника: {_money(fm["financial_sales"])}')
            expense_keys=EXPENSE_KEYS
            known=sum(float(fm.get(k,0)) for k in expense_keys)
            if known:
                lines.append(f'   известные расходы маркетплейса по SKU: {_money(known)}')
            if 'goods_payable' in fm:
                lines.append(f'   к перечислению за товар: {_money(fm["goods_payable"])}')
            if 'finance_ad_spend' in fm:
                lines.append(f'   из SKU-расходов — начисленная реклама: {_money(fm["finance_ad_spend"])} (уже учтена)')
        if row.advertising_metrics:
            am=row.advertising_metrics; spend=float(am.get('ad_spend',0)); sales=float(am.get('ad_attributed_sales',0))
            drr=(spend/sales*100) if sales>0 else None
            label='Performance по SKU' if row.marketplace=='ozon' else 'реклама по SKU'
            lines.append(f'   {label}: {_money(spend)} · атриб. продажи {_money(sales)} · ДРР {"—" if drr is None else f"{drr:.1f}%"}')
            if row.performance_reference:
                lines.append(f'   Performance справочно: {_money(row.performance_reference)} · повторно не вычитается')
        if row.contribution_after_known_expenses is not None:
            lines.append(f'   <b>после известных SKU-расходов: {_money(row.contribution_after_known_expenses)}</b>')
        for warning in row.warnings:lines.append('   ℹ️ '+warning)
    lines.append('\nℹ️ Заказы и финансовые начисления могут относиться к разным продажам. Эта оценка не заменяет расчёт прибыли по выкупленным товарам.')
    return '\n'.join(lines)
