from __future__ import annotations
from html import escape
from app.storage import Repository
from app.services.actions import ActionCenter
from app.services.supply import ForecastQualityReport


def format_inbound(repo: Repository, shop_id: int) -> str:
    rows=repo.active_inbound_items(shop_id)
    lines=['📥 <b>Поставки в пути</b>','━━━━━━━━━━━━━━━━']
    if not rows:
        lines.append('✅ Активных поставок с товарным составом сейчас нет.')
        return '\n'.join(lines)
    total=sum(float(r.get('remaining_units') or 0) for r in rows)
    lines.append(f'Активных товарных позиций: <b>{len(rows)}</b> · осталось в пути: <b>{total:g} шт.</b>')
    by_supply={}
    for r in rows: by_supply.setdefault((r['marketplace'],r['external_supply_id']),[]).append(r)
    for (market,sid),items in list(by_supply.items())[:15]:
        first=items[0]; qty=sum(float(x.get('remaining_units') or 0) for x in items)
        icon='🔵' if market=='wildberries' else '🟣'
        eta=str(first.get('planned_at') or 'ETA неизвестна')[:16]
        wh=escape(str(first.get('warehouse_name') or 'склад не указан'))
        lines.append(f'\n{icon} <b>{escape(str(sid))}</b> · {escape(str(first.get("status") or ""))}\n  {qty:g} шт. · {eta} · {wh}')
        for x in items[:4]:
            sku=escape(str(x.get('internal_sku') or x.get('marketplace_sku') or 'SKU'))
            lines.append(f'  • {sku}: {float(x.get("remaining_units") or 0):g} шт.')
        if len(items)>4: lines.append(f'  … ещё {len(items)-4} SKU')
    unknown=sum(1 for r in rows if not r.get('planned_at'))
    if unknown:
        lines += ['',f'⚠️ У {unknown} товарных строк нет ETA. Они показаны здесь, но не уменьшают рекомендацию по закупке.']
    return '\n'.join(lines)


def format_forecast_quality(report: ForecastQualityReport, limit: int=12) -> str:
    def pct(v): return '—' if v is None else f'{v:.1f}%'
    lines=[f'🎯 <b>Точность прогноза · {report.as_of.isoformat()}</b>','━━━━━━━━━━━━━━━━',
           f'Горизонт проверки: {report.horizon_days} дн. · модель {escape(report.method_version)}',
           f'Общий WAPE: <b>{pct(report.overall_wape_pct)}</b> · bias: {pct(report.overall_bias_pct)}','']
    if not report.items:
        lines.append('⏳ Пока недостаточно полной истории для backtest.')
        return '\n'.join(lines)
    lines.append('<b>SKU с наибольшей ошибкой</b>')
    for r in report.items[:limit]:
        lines.append(f'• <b>{escape(r.internal_sku)}</b> · WAPE {pct(r.wape_pct)} · bias {pct(r.bias_pct)} · samples {r.samples}')
    lines += ['','ℹ️ Это rolling backtest по прошлым полным дням. Ошибки API и неполные дни исключаются.']
    return '\n'.join(lines)


def format_action_center(center: ActionCenter, *, page: int=0, per_page: int=5) -> str:
    rows=list(center.items)
    pages=max(1,(len(rows)+per_page-1)//per_page)
    page=max(0,min(page,pages-1))
    start=page*per_page
    visible=rows[start:start+per_page]
    lines=[f'🎯 <b>Что делать · {center.as_of.isoformat()}</b>','━━━━━━━━━━━━━━━━']
    if not rows:
        lines.append('✅ По доступным данным сейчас нет действий, требующих внимания.')
        if center.snoozed_count:
            lines.append(f'⏰ Отложено: {center.snoozed_count}.')
        return '\n'.join(lines)
    lines += [
        f'Показываю {start+1}–{start+len(visible)} из {len(rows)} · страница {page+1}/{pages}.',
        'Нажмите ✅ у пункта, если взяли его в работу, или ⏰ чтобы скрыть на 24 часа.'
    ]
    groups={1:('🔴','Срочно'),2:('🟠','Сегодня'),3:('🟡','Планово'),4:('⚪️','Наблюдать')}
    last_priority=None
    for item in visible:
        if item.priority!=last_priority:
            icon,label=groups.get(item.priority,('⚪️','Наблюдать'))
            lines += ['',f'{icon} <b>{label}</b>']
            last_priority=item.priority
        state=' · ✅ принято' if item.status=='acknowledged' else (' · ⏰ отложено' if item.status=='snoozed' else '')
        title=item.title if len(item.title)<=120 else item.title[:117]+'…'
        detail=item.detail if len(item.detail)<=350 else item.detail[:347]+'…'
        lines.append(f'• <b>{escape(title)}</b>{state}')
        lines.append(escape(detail))
        if item.evidence:
            evidence=' · '.join(str(x)[:120] for x in item.evidence[:3])
            lines.append('  ↳ '+escape(evidence[:380]))
        if item.hint:
            lines.append(f'  → {escape(item.hint[:180])}')
    if center.snoozed_count:
        lines += ['',f'⏰ Скрыто отложенных действий: {center.snoozed_count}.']
    return '\n'.join(lines)


def format_action_history(rows: list[dict], *, days: int) -> str:
    lines=[f'📋 <b>История Action Center · {days} дн.</b>','━━━━━━━━━━━━━━━━']
    if not rows:
        return '\n'.join(lines+['📭 История действий пока пуста.'])
    for r in rows[:40]:
        p=int(r.get('priority') or 4); icon={1:'🔴',2:'🟠',3:'🟡',4:'⚪️'}.get(p,'⚪️')
        status=str(r.get('current_status') or 'resolved')
        mark={'open':'','acknowledged':' · ✅ принято','snoozed':' · ⏰ отложено','resolved':' · 🟢 решено'}.get(status,'')
        lines.append(f'{icon} {escape(str(r.get("as_of_date") or ""))} · <b>{escape(str(r.get("title") or ""))}</b>{mark}')
        lines.append(f'  <code>{escape(str(r.get("action_key") or ""))}</code>')
    return '\n'.join(lines)
