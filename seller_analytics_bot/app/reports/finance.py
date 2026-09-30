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
        if m or c:
            sources.append(FinanceSource(marketplace,m,float(c.get('estimated_cost',0)),coverage))
    missing=len(repo.products_without_cost(shop_id,limit=10000))
    return FinanceReport(start.isoformat(),end.isoformat(),days,tuple(sources),missing)
