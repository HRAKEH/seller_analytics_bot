from __future__ import annotations
from html import escape
from app.storage import Repository
from app.services.actions import ActionCenter
from app.services.supply import ForecastQualityReport
from .text import escape_clip


def format_inbound(repo: Repository, shop_id: int) -> str:
    rows=repo.active_inbound_items(shop_id)
    lines=['📥 <b>Поставки в пути</b>','━━━━━━━━━━━━━━━━']
    for conn in repo.list_connections(shop_id):
        if not conn.enabled:continue
        market='WB' if conn.marketplace=='wildberries' else 'Ozon'
        endpoint='supplies/fbw/inbound' if conn.marketplace=='wildberries' else 'supply-order/inbound'
        run=repo.latest_run(conn.id,endpoint)
        count=sum(1 for r in rows if r['marketplace']==conn.marketplace)
        if run is None:lines.append(f'⚠️ {market}: поставки ещё не загружены. Нажмите «Обновить поставки в пути».')
        elif run.status!='success':lines.append(f'⚠️ {market}: последняя загрузка не завершена. Показаны ранее сохранённые поставки, если они есть.')
        else:lines.append(f'{market}: товарных позиций в пути {count} · проверено {run.finished_at}')
    if not rows:
        lines.append('📭 В сохранённых данных нет активных поставок с товарным составом.')
        return '\n'.join(lines)
    total=sum(float(r.get('remaining_units') or 0) for r in rows)
    lines.append(f'Активных товарных позиций: <b>{len(rows)}</b> · осталось в пути: <b>{total:g} шт.</b>')
    by_supply={}
    for r in rows: by_supply.setdefault((r['marketplace'],r['external_supply_id']),[]).append(r)
    for (market,sid),items in list(by_supply.items())[:15]:
        first=items[0]; qty=sum(float(x.get('remaining_units') or 0) for x in items)
        icon='🔵' if market=='wildberries' else '🟣'
        label='WB' if market=='wildberries' else 'Ozon'
        eta=str(first.get('planned_at') or 'ETA неизвестна')[:16]
        wh=escape(str(first.get('warehouse_name') or 'склад не указан'))
        lines.append(f'\n{icon} {label} · <b>{escape(str(sid))}</b> · {escape(str(first.get("status") or ""))}\n  {qty:g} шт. · {eta} · {wh}')
        for x in items[:4]:
            sku=escape(str(x.get('internal_sku') or x.get('marketplace_sku') or 'SKU'))
            name=escape(str(x.get('name') or ''))
            lines.append(f'  • {name} · артикул {escape(str(x.get("marketplace_sku") or sku))}: {float(x.get("remaining_units") or 0):g} шт.')
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


def format_action_center(center: ActionCenter) -> str:
    lines=['🎯 <b>Что делать сегодня</b>',f'Заказы учтены по {center.as_of.isoformat()}; остатки — из последних снимков API.','━━━━━━━━━━━━━━━━',
        'Это список подсказок: что пополнить, где проверить рекламу или обновить данные. Бот ничего не закупает и не меняет сам.']
    if not center.items:
        lines.append('✅ По доступным данным срочных действий нет.')
        if center.snoozed_count:
            lines.append(f'⏰ Отложено действий: {center.snoozed_count}.')
        return '\n'.join(lines)
    groups={1:('🔴','Срочно'),2:('🟠','Сегодня'),3:('🟡','Планово'),4:('⚪️','Наблюдать')}
    shown=0
    for priority in (1,2,3,4):
        rows=[x for x in center.items if x.priority==priority]
        if not rows: continue
        icon,label=groups[priority]; lines += ['',f'{icon} <b>{label}</b> · {len(rows)}']
        for item in rows:
            if shown>=8: break
            shown+=1
            state=' ✅ принято' if item.status=='acknowledged' else (' ⏰ отложено' if item.status=='snoozed' else '')
            lines.append(f'{shown}. <b>{escape_clip(item.title,130)}</b>{state}')
            lines.append(escape_clip(item.detail,170))
            if item.hint: lines.append(f'   → {escape_clip(item.hint,80)}')
        if shown>=8: break
    remaining=max(0,len(center.items)-shown)
    if remaining:
        lines += ['',f'Ещё действий: {remaining}. Ниже показаны самые приоритетные.']
    if center.snoozed_count:
        lines += ['',f'⏰ Отложено и скрыто: {center.snoozed_count}.']
    lines += ['','Нажмите на нужное действие ниже — откроются детали и кнопки «Принято» / «Отложить».']
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
