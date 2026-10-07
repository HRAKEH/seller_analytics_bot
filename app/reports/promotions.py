from __future__ import annotations
from datetime import date, timedelta
from html import escape
from app.storage import Repository
from app.marketplaces import OZON_LABEL, WB_SHORT_LABEL
from .dates import readable_dates


@readable_dates
def format_promotions(repo: Repository, shop_id: int, as_of: date, *, future_days: int=60) -> str:
    end=as_of+timedelta(days=max(1,int(future_days)))
    promos=repo.promotions_for_shop(shop_id,as_of.isoformat(),end.isoformat())
    products=repo.promotion_products_for_shop(shop_id,as_of.isoformat(),end.isoformat())
    by_promo={}
    for row in products:
        key=(row['marketplace'],str(row['external_promotion_id']))
        by_promo.setdefault(key,[]).append(row)
    lines=[f'📅 <b>Календарь акций · {as_of.isoformat()} → {end.isoformat()}</b>','━━━━━━━━━━━━━━━━']
    for conn in repo.list_connections(shop_id):
        if not conn.enabled:continue
        endpoint='calendar/promotions' if conn.marketplace=='wildberries' else 'actions/promotions'
        run=repo.latest_run(conn.id, endpoint)
        if run and run.status != 'success':
            label='WB' if conn.marketplace=='wildberries' else 'Ozon'
            lines.append(f'⚠️ {label}: '+escape(str(run.error or 'Последняя загрузка акций не завершена.')[:650]))
    if not promos:
        lines += ['Акции пока не загружены или в выбранном горизонте их нет.',
                  'Используйте кнопку «🔄 Обновить акции».']
        return '\n'.join(lines)
    for promo in promos:
        market=WB_SHORT_LABEL if promo['marketplace']=='wildberries' else OZON_LABEL
        start=str(promo.get('start_at') or '') or '—'; finish=str(promo.get('end_at') or '') or '—'
        key=(promo['marketplace'],str(promo['external_promotion_id']))
        linked=[x for x in by_promo.get(key,[]) if x.get('product_id') is not None]
        unresolved=max(0,int(promo.get('participating_products') or 0)-len(linked))
        lines.append(f'\n{market} <b>{escape(str(promo.get("name") or promo["external_promotion_id"]))}</b>')
        if 'auto' in str(promo.get('promo_type') or '').casefold() and int(promo.get('product_rows') or 0)==0:
            lines.append(f'  {start} → {finish} · автоакция · SKU-детализация API недоступна')
        else:
            lines.append(f'  {start} → {finish} · участвует SKU: {int(promo.get("participating_products") or 0)} · связано: {len(linked)}')
        if linked:
            for row in linked:
                article=str(row.get('marketplace_sku') or row.get('internal_sku'))
                lines.append(f'  📦 {market} · артикул <code>{escape(article)}</code>')
        if unresolved:
            lines.append(f'  ⚠️ Не удалось связать с внутренним товаром: {unresolved}')
    lines += ['', 'ℹ️ Прогноз учитывает акцию только для точно участвующего SKU и только если по этому SKU накоплена достаточная история фактического promo uplift. Доступная, но не подключённая акция прогноз не меняет.']
    return '\n'.join(lines)
