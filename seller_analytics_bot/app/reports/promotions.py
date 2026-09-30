from __future__ import annotations
from datetime import date, timedelta
from html import escape
from app.storage import Repository


def format_promotions(repo: Repository, shop_id: int, as_of: date, *, future_days: int=60) -> str:
    end=as_of+timedelta(days=max(1,int(future_days)))
    promos=repo.promotions_for_shop(shop_id,as_of.isoformat(),end.isoformat())
    products=repo.promotion_products_for_shop(shop_id,as_of.isoformat(),end.isoformat())
    by_promo={}
    for row in products:
        key=(row['marketplace'],str(row['external_promotion_id']))
        by_promo.setdefault(key,[]).append(row)
    lines=[f'📅 <b>Календарь акций · {as_of.isoformat()} → {end.isoformat()}</b>','━━━━━━━━━━━━━━━━']
    if not promos:
        lines += ['Акции пока не загружены или в выбранном горизонте их нет.',
                  'Используйте кнопку «🔄 Обновить акции».']
        return '\n'.join(lines)
    for promo in promos[:20]:
        market='🔵 WB' if promo['marketplace']=='wildberries' else '🟣 Ozon'
        start=str(promo.get('start_at') or '')[:10] or '—'; finish=str(promo.get('end_at') or '')[:10] or '—'
        key=(promo['marketplace'],str(promo['external_promotion_id']))
        linked=[x for x in by_promo.get(key,[]) if x.get('product_id') is not None]
        unresolved=max(0,int(promo.get('participating_products') or 0)-len(linked))
        lines.append(f'\n{market} <b>{escape(str(promo.get("name") or promo["external_promotion_id"]))}</b>')
        if 'auto' in str(promo.get('promo_type') or '').casefold() and int(promo.get('product_rows') or 0)==0:
            lines.append(f'  {start} → {finish} · автоакция · SKU-детализация API недоступна')
        else:
            lines.append(f'  {start} → {finish} · участвует SKU: {int(promo.get("participating_products") or 0)} · связано: {len(linked)}')
        if linked:
            preview=', '.join(escape(str(x.get('internal_sku') or x.get('marketplace_sku'))) for x in linked[:5])
            lines.append(f'  📦 {preview}' + (' …' if len(linked)>5 else ''))
        if unresolved:
            lines.append(f'  ⚠️ Не удалось связать с внутренним товаром: {unresolved}')
    if len(promos)>20: lines.append(f'\n… ещё акций: {len(promos)-20}')
    lines += ['', 'ℹ️ Прогноз учитывает акцию только для точно участвующего SKU и только если по этому SKU накоплена достаточная история фактического promo uplift. Доступная, но не подключённая акция прогноз не меняет.']
    return '\n'.join(lines)
