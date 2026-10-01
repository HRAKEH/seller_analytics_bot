"""Financial view. Keeps different settlement bases explicit."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date,timedelta
from app.storage import Repository

@dataclass(frozen=True)
class FinanceSource:
    marketplace: str
    metrics: dict[str,float]
    estimated_order_cogs: float
    cogs_coverage_pct: float | None

@dataclass(frozen=True)
class FinanceReport:
    start: str
    end: str
    days: int
    sources: tuple[FinanceSource,...]
    missing_cost_products: int
    source_coverage: tuple = ()

def build_finance_report(repo: Repository, shop_id: int, end: date, days: int=7) -> FinanceReport:
    start=end-timedelta(days=days-1)
    totals=repo.financial_metric_totals(shop_id,start.isoformat(),end.isoformat())
    cogs=repo.estimated_order_cogs(shop_id,start.isoformat(),end.isoformat())
    sources=[]
    for marketplace in ('ozon','wildberries'):
        m=totals.get(marketplace,{})
        c=cogs.get(marketplace,{})
        units=float(c.get('units',0)); covered=float(c.get('covered_units',0))
        coverage=(covered/units*100) if units>0 else None
        expected_units=float(m['ordered_units']) if 'ordered_units' in m else units
        if expected_units>0:
            coverage=min(covered,expected_units)/expected_units*100
        if m or c:
            sources.append(FinanceSource(marketplace,m,float(c.get('estimated_cost',0)),coverage))
    missing=len(repo.products_without_cost(shop_id,limit=10000))
    from .coverage import build_source_coverage
    coverage=build_source_coverage(repo,shop_id,start.isoformat(),end.isoformat()) if hasattr(repo,'db') else ()
    return FinanceReport(start.isoformat(),end.isoformat(),days,tuple(sources),missing,coverage)
