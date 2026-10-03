"""Alert engine with persistence/cooldown. Alerts only on sufficiently grounded data."""
from __future__ import annotations
import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from app.storage import Repository
from app.reports.products import build_product_report

def marketplace_label(value: str) -> str:
    return {'wildberries':'WB','wb':'WB','ozon':'Ozon'}.get(value,value)


def stock_alert_text(row) -> str:
    state='нет остатка' if row.available_units<=0 else (
        f'хватит примерно на {row.days_left:.1f} дн.' if row.days_left is not None else 'спрос неизвестен')
    return f'📦 {marketplace_label(row.marketplace)} · артикул {row.sku}: остаток {row.available_units:g} шт.; {state}\nТовар: {row.name}'

@dataclass(frozen=True)
class AlertNotification:
    rule_key: str
    subject_key: str
    severity: str
    message: str
    value: float | None = None

class AlertEngine:
    def __init__(self, repo: Repository, *, cooldown_minutes: int = 1440):
        self.repo=repo; self.cooldown=timedelta(minutes=cooldown_minutes)

    @staticmethod
    def _fingerprint(rule: str, subject: str, message: str) -> str:
        return hashlib.sha256(f'{rule}|{subject}|{message}'.encode()).hexdigest()[:24]

    def _emit(self, shop_id: int, n: AlertNotification) -> AlertNotification | None:
        now=datetime.now(timezone.utc)
        state=self.repo.get_alert_state(shop_id,n.rule_key,n.subject_key)
        fp=self._fingerprint(n.rule_key,n.subject_key,n.message)
        if state and state.get('active'):
            last=state.get('last_notified_at')
            if last:
                try: last_dt=datetime.fromisoformat(str(last))
                except ValueError: last_dt=None
                if last_dt and now-last_dt < self.cooldown:
                    # Cooldown is tied to the rule+subject, not the exact message.
                    # Otherwise a slowly changing stock/runway value would create a new
                    # fingerprint and spam the owner on every evaluation cycle.
                    self.repo.save_alert_state(shop_id,n.rule_key,n.subject_key,active=True,value=n.value,fingerprint=fp)
                    return None
        ts=now.isoformat(timespec='seconds')
        self.repo.save_alert_state(shop_id,n.rule_key,n.subject_key,active=True,value=n.value,fingerprint=fp,notified_at=ts)
        self.repo.record_alert_event(shop_id,n.rule_key,n.subject_key,n.severity,n.message,n.value,fp)
        return n

    def _resolve_missing(self, shop_id: int, active_keys: set[tuple[str,str]], rules: set[str], *,
                         subjects: dict[str,set[str]] | None = None) -> list[AlertNotification]:
        # Resolve only rules that were actually evaluated this cycle. Emit one recovery event
        # on the active -> resolved transition so owners know the situation normalised.
        now=datetime.now(timezone.utc).isoformat(timespec='seconds')
        resolved: list[AlertNotification] = []
        with self.repo.db.connect() as c:
            rows=c.execute("SELECT rule_key,subject_key FROM alert_state WHERE shop_id=? AND active=1",(shop_id,)).fetchall()
        labels={'low_stock':'остаток','order_drop':'падение заказов','api_stale':'доступность API','high_drr':'ДРР'}
        for r in rows:
            key=(str(r['rule_key']),str(r['subject_key']))
            if subjects is not None and key[0] in subjects and key[1] not in subjects[key[0]]:
                continue
            if key[0] in rules and key not in active_keys:
                market,_,sku=key[1].partition(':')
                subject=marketplace_label(market)+(f' · артикул {sku}' if sku else '')
                if sku:
                    conn=next((x for x in self.repo.list_connections(shop_id) if x.marketplace==market),None)
                    listing=self.repo.listing_by_marketplace_sku(conn.id,sku) if conn else None
                    if listing:
                        with self.repo.db.connect() as c:
                            product=c.execute('SELECT name FROM products WHERE id=?',(listing.product_id,)).fetchone()
                        if product:subject+=f' · {product["name"]}'
                msg=f'✅ Восстановлено: {labels.get(key[0], key[0])} · {subject}'
                fp=self._fingerprint(key[0],key[1],msg)
                self.repo.save_alert_state(shop_id,key[0],key[1],active=False,value=None,fingerprint=None,resolved_at=now)
                self.repo.record_alert_event(shop_id,key[0],key[1],'resolved',msg,None,fp)
                resolved.append(AlertNotification(key[0],key[1],'resolved',msg,None))
        return resolved

    def evaluate(self, shop_id: int, *, today: date, order_drop_pct: float, order_lookback_days: int,
                 api_stale_hours: float, drr_pct: float, stock_risk_days: int,
                 stock_velocity_days: int) -> list[AlertNotification]:
        candidates: list[AlertNotification]=[]; active:set[tuple[str,str]]=set(); evaluated:set[str]=set()
        subjects={'low_stock':set(),'high_drr':set()}

        # 1) Low stock. Uses only successful operational-order days for velocity.
        end=today-timedelta(days=1)
        report=build_product_report(self.repo,shop_id,end,days=min(7,stock_velocity_days),
                                    stock_lookback_days=stock_velocity_days,stock_risk_days=stock_risk_days)
        evaluated.add('low_stock')
        for r in report.stock_risks:
            if r.captured_at:
                try:
                    if (today-datetime.fromisoformat(r.captured_at.replace('Z','+00:00')).date()).days>2:continue
                except ValueError:continue
            if r.available_units<=0 or r.avg_daily_units is not None:
                subjects['low_stock'].add(f'{r.marketplace}:{r.sku}')
            risky=r.available_units<=0 or (r.days_left is not None and r.days_left<=stock_risk_days)
            if not risky: continue
            subject=f'{r.marketplace}:{r.sku}'; active.add(('low_stock',subject))
            candidates.append(AlertNotification('low_stock',subject,'critical' if r.available_units<=0 else 'warning',
                stock_alert_text(r),0.0 if r.available_units<=0 else r.days_left))

        # 2) Order drop only when yesterday is complete for all enabled marketplaces.
        start=today-timedelta(days=order_lookback_days+1)
        y=(today-timedelta(days=1)).isoformat()
        complete=set(self.repo.complete_order_dates(shop_id,start.isoformat(),y))
        if y in complete:
            series=self.repo.daily_shop_metric(shop_id,start.isoformat(),y,'ordered_units')
            baselines=[]
            for i in range(2,order_lookback_days+2):
                d=(today-timedelta(days=i)).isoformat()
                if d in complete and d in series: baselines.append(series[d])
            if baselines and y in series:
                evaluated.add('order_drop')
                avg=sum(baselines)/len(baselines); cur=series.get(y,0.0)
                drop=((avg-cur)/avg*100) if avg>0 else 0
                if avg>0 and drop>=order_drop_pct:
                    active.add(('order_drop','shop'))
                    candidates.append(AlertNotification('order_drop','shop','warning',
                        f'📉 Заказы за вчера {cur:g} ед., на {drop:.1f}% ниже среднего за {len(baselines)} предыдущих полных дней ({avg:.1f}).',drop))

        # 3) Primary order-data freshness. A successful backfill is a real
        # refresh too, so it must clear api_stale instead of waiting for the next
        # scheduled one-day endpoint call.
        evaluated.add('api_stale')
        for conn in self.repo.list_connections(shop_id):
            if not conn.enabled: continue
            run=self.repo.last_successful_order_run(conn.id)
            stale=True; age=None
            if run and run.finished_at:
                try:
                    dt=datetime.fromisoformat(run.finished_at); age=(datetime.now(timezone.utc)-dt).total_seconds()/3600; stale=age>api_stale_hours
                except ValueError: pass
            if stale:
                subject=conn.marketplace; active.add(('api_stale',subject))
                msg=f'🔌 {conn.display_name}: нет свежей успешной загрузки заказов'
                if age is not None: msg+=f' уже {age:.1f} ч.'
                candidates.append(AlertNotification('api_stale',subject,'critical',msg,age))

        # 4) DRR on recent advertising attribution. No attributed sales -> no percentage alert.
        evaluated.add('high_drr')
        ad_start=today-timedelta(days=7)
        fin=self.repo.financial_metric_totals(shop_id,ad_start.isoformat(),y)
        for market,m in fin.items():
            spend=m.get('ad_spend',0.0); sales=m.get('ad_attributed_sales',0.0)
            if 'ad_spend' in m and 'ad_attributed_sales' in m and (sales>0 or spend==0):
                subjects['high_drr'].add(market)
            if spend>0 and sales>0:
                drr=spend/sales*100
                if drr>=drr_pct:
                    active.add(('high_drr',market))
                    candidates.append(AlertNotification('high_drr',market,'warning',
                        f'📣 {marketplace_label(market)}: ДРР {drr:.1f}% за последние 7 дней, порог {drr_pct:.1f}%.',drr))

        resolved=self._resolve_missing(shop_id,active,evaluated,subjects=subjects)
        result=list(resolved)
        for n in candidates:
            sent=self._emit(shop_id,n)
            if sent: result.append(sent)
        return result
