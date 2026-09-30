"""Daily operational orders report built only from normalized comparable metrics."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, timedelta
from app.storage import Repository, MarketplaceConnection

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
        latest = repo.latest_order_run(conn.id, ds)
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
        ))
    return DailyReport(ds, tuple(sources))
