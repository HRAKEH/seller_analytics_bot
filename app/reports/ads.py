"""Advertising analytics by campaign and SKU."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, timedelta
from html import escape
from app.storage import Repository
from app.marketplaces import OZON_ICON, WB_ICON
from .dates import readable_dates

@dataclass(frozen=True)
class AdRow:
    marketplace: str
    key: str
    name: str
    spend: float
    attributed_sales: float
    orders: float
    clicks: float
    impressions: float

    @property
    def drr(self) -> float | None:
        return self.spend / self.attributed_sales * 100 if self.attributed_sales > 0 else None

    @property
    def roas(self) -> float | None:
        return self.attributed_sales / self.spend if self.spend > 0 else None

@dataclass(frozen=True)
class AdvertisingReport:
    start: str
    end: str
    days: int
    campaigns: tuple[AdRow,...]
    products: tuple[AdRow,...]
    source_coverage: tuple = ()
    warnings: tuple[str,...] = ()


def build_advertising_report(repo: Repository, shop_id: int, end: date, days: int = 7) -> AdvertisingReport:
    start=end-timedelta(days=days-1)
    campaigns=[]
    for r in repo.ad_campaign_totals(shop_id,start.isoformat(),end.isoformat()):
        campaigns.append(AdRow(str(r['marketplace']),str(r['campaign_id']),str(r.get('campaign_name') or r['campaign_id']),
            float(r.get('spend') or 0),float(r.get('attributed_sales') or 0),float(r.get('orders') or 0),
            float(r.get('clicks') or 0),float(r.get('impressions') or 0)))
    products=[]
    for r in repo.ad_product_totals(shop_id,start.isoformat(),end.isoformat()):
        products.append(AdRow(str(r['marketplace']),str(r['marketplace_sku']),str(r.get('name') or r['marketplace_sku']),
            float(r.get('spend') or 0),float(r.get('attributed_sales') or 0),float(r.get('orders') or 0),
            float(r.get('clicks') or 0),float(r.get('impressions') or 0)))
    campaigns.sort(key=lambda x:x.spend,reverse=True); products.sort(key=lambda x:x.spend,reverse=True)
    from .coverage import build_source_coverage
    coverage=tuple(r for r in build_source_coverage(repo,shop_id,start.isoformat(),end.isoformat()) if r.component=='Рекламная статистика')
    warnings=[]
    for conn in repo.list_connections(shop_id):
        if conn.enabled and conn.marketplace=='ozon':
            run=repo.latest_run(conn.id,'performance/product-stats')
            if run and run.status=='failed' and 'OZON_PERF_CLIENT_ID' in (run.error or ''):
                warnings.append('Реклама Ozon не подключена. Владельцу нужны отдельные Client ID и Client Secret из Ozon Performance. После подключения нажмите «Обновить рекламу».')
            elif not any(r.marketplace=='ozon' and r.available_days for r in coverage):
                warnings.append('Ozon: нет загруженной рекламной статистики за период. Проверьте отдельное подключение Ozon Performance через «Проверить API», затем нажмите «Обновить рекламу».')
    return AdvertisingReport(start.isoformat(),end.isoformat(),days,tuple(campaigns),tuple(products),coverage,tuple(warnings))


def _money(v: float) -> str:
    return f'{v:,.0f}'.replace(',',' ')+' ₽'

def _pct(v: float | None) -> str:
    return '—' if v is None else f'{v:.1f}%'


@readable_dates
def format_advertising(report: AdvertisingReport, limit: int | None = None) -> str:
    lines=[f'📣 <b>Реклама · {report.start} — {report.end}</b>','━━━━━━━━━━━━━━━━',
           'ДРР здесь считается только из рекламно-атрибутированной выручки конкретного источника.',
           'ℹ️ Рекламная атрибуция не равна финансовому признанию продажи или выплате маркетплейса.']
    for warning in report.warnings:lines.append('⚠️ '+escape(warning))
    for c in report.source_coverage:
        label='WB' if c.marketplace=='wildberries' else 'Ozon'
        lines.append(f'{label}: загружено {c.available_days}/{c.expected_days} дней' + (' · есть ошибка обновления' if c.failed_dates else ''))
    if not report.campaigns and not report.products:
        return '\n'.join(lines+['','📭 Детальной рекламной статистики за период нет.'])
    if report.campaigns:
        lines += ['','<b>Кампании по расходу</b>']
        for i,r in enumerate(report.campaigns[:limit],1):
            icon=OZON_ICON if r.marketplace=='ozon' else WB_ICON
            lines.append(f'{i}. {icon} <b>{escape(r.name)}</b> · {_money(r.spend)} · ДРР {_pct(r.drr)}')
            lines.append(f'   продажи рекламы {_money(r.attributed_sales)} · заказы {r.orders:g} · клики {r.clicks:g}')
    if report.products:
        lines += ['','<b>SKU по рекламному расходу</b>']
        for i,r in enumerate(report.products[:limit],1):
            icon=OZON_ICON if r.marketplace=='ozon' else WB_ICON
            market='Ozon' if r.marketplace=='ozon' else 'WB'
            lines.append(f'{i}. {icon} {market} · <b>{escape(r.name)}</b> · SKU <code>{escape(r.key)}</code>')
            lines.append(f'   расход {_money(r.spend)} · продажи {_money(r.attributed_sales)} · ДРР {_pct(r.drr)} · заказы {r.orders:g}')
    return '\n'.join(lines)
