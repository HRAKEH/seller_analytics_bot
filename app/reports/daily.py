"""Daily operational orders report built only from normalized comparable metrics."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, timedelta
from app.storage import Repository, MarketplaceConnection
from app.services.buyer_prices import BuyerPriceTotals, build_buyer_prices
from app.services.currency import CurrencyRateError, ensure_cbr_rates
from app.services.ozon_buyouts import BuyoutCheck, build_buyout_check
from app.services.daily_events import DailyEvents, build_daily_events

@dataclass(frozen=True)
class MarketplaceDaily:
    marketplace: str
    connection_id: int
    units: float | None
    ordered_revenue: float | None
    cancellations: float | None
    previous_units: float | None
    preliminary: bool
    freshness: str | None
    warning: str | None
    order_source: str | None = None
    marketplace_net: float | None = None
    finance_freshness: str | None = None
    buyer_prices: BuyerPriceTotals | None = None
    buyout_check: BuyoutCheck | None = None
    events: DailyEvents | None = None

@dataclass(frozen=True)
class DailyReport:
    day: str
    sources: tuple[MarketplaceDaily, ...]

    @property
    def complete(self) -> bool:
        return bool(self.sources) and all(s.units is not None for s in self.sources)

    @property
    def total_units(self) -> float | None:
        # Never present a cross-marketplace "total" when one enabled source is
        # missing; a partial sum looks authoritative but is not comparable.
        if not self.complete: return None
        return sum(float(s.units or 0) for s in self.sources)

    @property
    def available_units(self) -> float | None:
        values=[float(s.units) for s in self.sources if s.units is not None]
        return sum(values) if values else None

    @property
    def previous_total_units(self) -> float | None:
        if not self.sources or any(s.previous_units is None for s in self.sources):
            return None
        return sum(float(s.previous_units or 0) for s in self.sources)


def _metric(repo: Repository, connection_id: int, day: str, key: str):
    return repo.latest_metric(connection_id, day, key)


def build_daily_report(repo: Repository, shop_id: int, day: date) -> DailyReport:
    ds = day.isoformat(); prev = (day - timedelta(days=1)).isoformat()
    sources = []
    for conn in repo.list_connections(shop_id):
        if not conn.enabled: continue
        units = _metric(repo, conn.id, ds, 'ordered_units')
        money = _metric(repo, conn.id, ds, 'ordered_revenue')
        cancels = _metric(repo, conn.id, ds, 'cancellations_units')
        prev_units = _metric(repo, conn.id, prev, 'ordered_units')
        source=dict(units).get('endpoint') if units else None
        if conn.marketplace=='wildberries' and units and prev_units:
            previous_source=dict(prev_units).get('endpoint') or ''
            if str(source or '').startswith('analytics/orders')!=previous_source.startswith('analytics/orders'):
                prev_units=None
        finance=_metric(repo,conn.id,ds,'marketplace_net') if conn.marketplace=='ozon' else None
        latest = repo.latest_order_run(conn.id, ds,prefer_wb_funnel=(
            conn.marketplace=='wildberries' and str(source or '').startswith('analytics/orders')))
        warning = latest.error if latest and latest.status == 'failed' else None
        sources.append(MarketplaceDaily(
            conn.marketplace, conn.id,
            float(units['value']) if units else None,
            float(money['value']) if money else None,
            float(cancels['value']) if cancels else None,
            float(prev_units['value']) if prev_units else None,
            bool(units['is_preliminary']) if units else True,
            units['as_of'] if units else None,
            warning,
            source,float(finance['value']) if finance else None,
            finance['as_of'] if finance else None,
            build_buyer_prices(repo, conn.id, day, units['value'] if units else None)
            if conn.marketplace == 'ozon' else None,
            build_buyout_check(repo,conn.id,day) if conn.marketplace=='ozon' else None,
            build_daily_events(repo, conn.id, conn.marketplace, day),
        ))
    return DailyReport(ds, tuple(sources))


async def build_daily_report_with_currency(repo: Repository, shop_id: int, day: date,
                                          *, transport=None) -> DailyReport:
    """Get missing reference rates automatically before publishing the report."""
    report = build_daily_report(repo, shop_id, day)
    needs_rates = any(s.buyer_prices and s.buyer_prices.complete and
                      s.buyer_prices.foreign_currencies for s in report.sources)
    if needs_rates:
        try:
            await ensure_cbr_rates(repo, day, transport=transport)
        except CurrencyRateError:
            # Original currencies remain usable; no invented rate or zero.
            return report
        report = build_daily_report(repo, shop_id, day)
    return report
