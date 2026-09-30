"""Completed-day period reports for the common ordered_units metric."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, timedelta
from app.storage import Repository

@dataclass(frozen=True)
class SourcePeriod:
    marketplace: str
    units: float
    previous_units: float | None

@dataclass(frozen=True)
class PeriodReport:
    label: str
    start: str
    end: str
    complete_days: int
    requested_days: int
    sources: tuple[SourcePeriod,...]
    previous_total: float | None

    @property
    def total(self): return sum(x.units for x in self.sources)


def build_period_report(repo: Repository, shop_id: int, end: date, days: int, label: str) -> PeriodReport:
    start=end-timedelta(days=days-1)
    prev_end=start-timedelta(days=1)
    prev_start=prev_end-timedelta(days=days-1)
    conns=[c for c in repo.list_connections(shop_id) if c.enabled]
    current={c.id:repo.metric_series(c.id,start.isoformat(),end.isoformat(),'ordered_units') for c in conns}
    previous={c.id:repo.metric_series(c.id,prev_start.isoformat(),prev_end.isoformat(),'ordered_units') for c in conns}
    complete=[]
    d=start
    while d<=end:
        ds=d.isoformat()
        if conns and all(ds in current[c.id] for c in conns): complete.append(ds)
        d += timedelta(days=1)
    prev_complete=[]
    d=prev_start
    while d<=prev_end:
        ds=d.isoformat()
        if conns and all(ds in previous[c.id] for c in conns): prev_complete.append(ds)
        d += timedelta(days=1)
    sources=[]
    for c in conns:
        units=sum(current[c.id][d] for d in complete)
        prev=sum(previous[c.id][d] for d in prev_complete) if prev_complete else None
        sources.append(SourcePeriod(c.marketplace,units,prev))
    prev_total=sum(x.previous_units for x in sources if x.previous_units is not None) if prev_complete else None
    return PeriodReport(label,start.isoformat(),end.isoformat(),len(complete),days,tuple(sources),prev_total)
