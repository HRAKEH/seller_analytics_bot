"""Action Center 2.0: transparent, deduplicated, workflow-aware seller priorities."""
from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any
from app.storage import Repository
from .supply import build_supply_plan, evaluate_forecast_quality

EXPENSE_KEYS=('commission','logistics','storage','acceptance','acquiring','services','penalties')

@dataclass(frozen=True)
class ActionItem:
    action_key: str
    priority: int
    category: str
    title: str
    detail: str
    hint: str = ''
    evidence: tuple[str,...] = ()
    status: str = 'open'
    snoozed_until: str | None = None

@dataclass(frozen=True)
class ActionCenter:
    shop_id: int
    as_of: date
    items: tuple[ActionItem,...]
    total_generated: int = 0
    snoozed_count: int = 0


def _state_map(repo: Repository, shop_id: int) -> dict[str,dict[str,Any]]:
    return {str(r['action_key']):r for r in repo.action_states(shop_id,include_resolved=False,limit=500)}


def _merge_action(target: dict[str,ActionItem], item: ActionItem) -> None:
    old=target.get(item.action_key)
    if old is None:
        target[item.action_key]=item; return
    evidence=tuple(dict.fromkeys((*old.evidence,*item.evidence)))
    priority=min(old.priority,item.priority)
    detail=old.detail
    if item.detail and item.detail not in detail: detail += ' · '+item.detail
    hint=old.hint or item.hint
    target[item.action_key]=replace(old,priority=priority,detail=detail,hint=hint,evidence=evidence)


def _management_actions(repo: Repository, shop_id: int, as_of: date, days: int) -> list[ActionItem]:
    start=as_of-timedelta(days=max(1,days)-1)
    totals=repo.financial_metric_totals(shop_id,start.isoformat(),as_of.isoformat())
    cogs=repo.estimated_order_cogs(shop_id,start.isoformat(),as_of.isoformat())
    out=[]
    for market in ('ozon','wildberries'):
        m=totals.get(market,{})
        c=cogs.get(market,{})
        units=float(c.get('units',0)); covered=float(c.get('covered_units',0))
        revenue=float(m.get('ordered_revenue',0)); estimated_cogs=float(c.get('estimated_cost',0))
        expenses=sum(float(m.get(k,0)) for k in EXPENSE_KEYS)
        ads=float(m.get('ad_spend',0)); compensation=float(m.get('compensation',0))
        complete=units<=0 or covered+1e-9>=units
        if revenue>0 and complete:
            result=revenue-estimated_cogs-expenses-ads+compensation
            if result<0:
                out.append(ActionItem(
                    f'management:negative:{market}',2,'finance',
                    f'📉 Отрицательный управленческий результат · {market.title()}',
                    f'Оценка за {days} дн.: {result:,.0f} ₽ при полном покрытии себестоимости.'.replace(',',' '),
                    '💰 Деньги и реклама → 📈 Управленческий результат',
                    (f'заказы {revenue:,.0f} ₽'.replace(',',' '),f'расходы МП {expenses:,.0f} ₽'.replace(',',' '),f'реклама {ads:,.0f} ₽'.replace(',',' ')),
                ))
    return out


def _advertising_actions(repo: Repository, shop_id: int, as_of: date, threshold: float) -> list[ActionItem]:
    start=as_of-timedelta(days=6); out=[]
    for r in repo.ad_campaign_totals(shop_id,start.isoformat(),as_of.isoformat()):
        spend=float(r.get('spend') or 0); sales=float(r.get('attributed_sales') or 0)
        if spend<=0 or sales<=0: continue
        drr=spend/sales*100
        if drr < threshold: continue
        market=str(r.get('marketplace') or '')
        cid=str(r.get('campaign_id') or '')
        name=str(r.get('campaign_name') or cid)
        out.append(ActionItem(
            f'ads:drr:{market}:{cid}',2,'ads',f'📣 Высокий ДРР · {name}',
            f'{market.title()}: ДРР {drr:.1f}% за 7 дней, порог {threshold:.1f}%.',
            '💰 Деньги и реклама → 📣 Реклама',
            (f'расход {spend:,.0f} ₽'.replace(',',' '),f'атрибутированные продажи {sales:,.0f} ₽'.replace(',',' ')),
        ))
    return out[:10]


def build_action_center(repo: Repository, shop_id: int, as_of: date, *, persist: bool=False,
                        include_snoozed: bool=False) -> ActionCenter:
    actions: dict[str,ActionItem]={}
    pref=repo.get_shop_preferences(shop_id)
    drr_threshold=float(pref.alert_drr_pct if pref else 25)
    finance_days=int(pref.finance_lookback_days if pref else 14)

    # System/API alerts. Stock and DRR are rebuilt below with richer evidence to avoid duplicates.
    for a in repo.active_alert_states(shop_id):
        rule=str(a.get('rule_key') or 'alert'); subject=str(a.get('subject_key') or '')
        if rule in {'low_stock','high_drr'}: continue
        labels={'order_drop':'Падение заказов','api_stale':'API'}
        p=1 if rule=='api_stale' else 2
        _merge_action(actions,ActionItem(
            f'alert:{rule}:{subject}',p,'system' if rule=='api_stale' else 'sales',
            f'🚨 {labels.get(rule,rule)} · {subject}',
            f'Активный алерт. Последнее значение: {a.get("last_value") if a.get("last_value") is not None else "—"}',
            '🚨 Контроль → 🚨 Алерты',('источник: alert engine',)
        ))

    dead=[j for j in repo.recent_retry_jobs(limit=50,shop_id=shop_id) if str(j.get('status'))=='dead']
    if dead:
        _merge_action(actions,ActionItem('system:dead-retries',1,'system',f'🧰 Dead retry-задачи: {len(dead)}',
            'Есть задачи, исчерпавшие автоматические попытки.','🚨 Контроль → 🧰 Retry-очередь',
            tuple(f'job #{j.get("id")} · {j.get("job_type")}' for j in dead[:5])))

    # Promotions indexed by physical product so stock+supply+promo collapses into one action.
    promo_rows=repo.promotion_products_for_shop(shop_id,as_of.isoformat(),(as_of+timedelta(days=14)).isoformat())
    promos_by_product: dict[int,list[dict[str,Any]]]={}
    unmerged_promos: dict[tuple[str,str],dict[str,Any]]={}
    for row in promo_rows:
        if not row.get('in_action'): continue
        pid=row.get('product_id')
        if pid is not None: promos_by_product.setdefault(int(pid),[]).append(row)
        key=(str(row.get('marketplace')),str(row.get('external_promotion_id')))
        x=unmerged_promos.setdefault(key,{'name':str(row.get('promotion_name') or row.get('external_promotion_id')),'count':0,'start':str(row.get('start_at') or '')[:10]})
        if pid is not None: x['count']+=1

    plan=build_supply_plan(repo,shop_id,as_of,persist=False)
    merged_promo_keys=set()
    for r in plan.rows:
        product_promos=promos_by_product.get(r.product_id,[])
        promo_evidence=[]
        for p in product_promos:
            merged_promo_keys.add((str(p.get('marketplace')),str(p.get('external_promotion_id'))))
            promo_evidence.append(f'акция {p.get("promotion_name") or p.get("external_promotion_id")} с {str(p.get("start_at") or "")[:10]}')
        if r.available_units is None:
            _merge_action(actions,ActionItem(f'stock:unknown:{r.product_id}',2,'stock',f'❓ Нет остатка · {r.internal_sku}',
                'Нет успешного снимка остатков — безопасная рекомендация поставки невозможна.',
                '📦 Товары и SKU → 📦 Остатки',tuple(['остаток неизвестен',*promo_evidence])))
            continue
        if r.needs_order:
            critical=(r.available_units<=0 or (r.days_cover is not None and r.days_cover<=max(1,r.effective_lead_time_days)))
            p=1 if critical else (2 if r.abc_class=='A' or product_promos else 3)
            inbound=f' · в пути {r.inbound_units:g}' if r.inbound_units>0 else ''
            cover='нет оценки покрытия' if r.days_cover is None else f'запас {r.days_cover:.1f} дн.'
            evidence=[f'{r.abc_class}{r.xyz_class}',f'остаток {r.available_units:g}',f'effective lead {r.effective_lead_time_days} дн.']
            if r.inventory_stale: evidence.append(f'остаток устарел на {r.inventory_age_days} дн.')
            evidence += promo_evidence
            _merge_action(actions,ActionItem(f'supply:{r.product_id}',p,'supply',f'🚚 Заказать {r.internal_sku}: {r.recommended_order_units:g} шт.',
                f'{cover}{inbound}.','🚚 Поставки → 🚚 План поставок',tuple(evidence)))
        elif r.inventory_stale:
            _merge_action(actions,ActionItem(f'stock:stale:{r.product_id}',2,'stock',f'🕒 Старый остаток · {r.internal_sku}',
                f'Снимок остатков старше {r.inventory_age_days} дн.','📦 Товары и SKU → 📦 Остатки',tuple(promo_evidence)))

    no_cost=repo.products_without_cost(shop_id,limit=50)
    if no_cost:
        _merge_action(actions,ActionItem('finance:missing-cost',3,'finance',f'💲 Нет себестоимости: {len(no_cost)} SKU',
            'Управленческий результат по этим товарам неполный.','📦 Товары и SKU → 📥 Импорт себестоимости',
            tuple(str(x.get('internal_sku') or x.get('marketplace_sku')) for x in no_cost[:8])))

    for key,item in unmerged_promos.items():
        if key in merged_promo_keys or item['count']<=0: continue
        _merge_action(actions,ActionItem(f'promo:{key[0]}:{key[1]}',4,'promo',f'📅 Акция скоро · {item["name"]}',
            f'Связанных участвующих SKU: {item["count"]} · старт {item["start"] or "—"}.',
            '🚚 Поставки → 📅 Акции',(f'{item["count"]} SKU участвуют',)))

    quality=evaluate_forecast_quality(repo,shop_id,as_of,persist=False)
    if quality.overall_wape_pct is not None and quality.overall_wape_pct>50:
        _merge_action(actions,ActionItem('forecast:quality',3,'forecast','🎯 Низкая точность прогноза',
            f'WAPE ≈ {quality.overall_wape_pct:.1f}% на горизонте {quality.horizon_days} дн.',
            '🚚 Поставки → 🎯 Точность прогноза',(f'samples {sum(x.samples for x in quality.items)}',)))

    for item in _advertising_actions(repo,shop_id,as_of,drr_threshold): _merge_action(actions,item)
    for item in _management_actions(repo,shop_id,as_of,finance_days): _merge_action(actions,item)

    generated=sorted(actions.values(),key=lambda x:(x.priority,x.category,x.title))[:50]
    if persist:
        repo.sync_action_center(shop_id,as_of.isoformat(),[
            {'action_key':x.action_key,'priority':x.priority,'category':x.category,'title':x.title,'detail':x.detail,
             'evidence':list(x.evidence)} for x in generated])
    states=_state_map(repo,shop_id)
    now=datetime.now(timezone.utc); visible=[]; snoozed=0
    for item in generated:
        state=states.get(item.action_key,{})
        status=str(state.get('status') or 'open'); until=state.get('snoozed_until')
        active_snooze=False
        if status=='snoozed' and until:
            try: active_snooze=datetime.fromisoformat(str(until))>now
            except ValueError: active_snooze=False
        if active_snooze and not include_snoozed:
            snoozed+=1; continue
        visible.append(replace(item,status=status if active_snooze or status=='acknowledged' else 'open',snoozed_until=str(until) if until else None))
    return ActionCenter(shop_id,as_of,tuple(visible[:30]),len(generated),snoozed)
