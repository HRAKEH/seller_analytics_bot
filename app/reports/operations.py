from __future__ import annotations
from html import escape
from app.storage import Repository
from app.marketplaces import OZON_ICON, WB_ICON
from app.services.actions import ActionCenter
from app.services.supply import ForecastQualityReport
from .text import escape_clip, page_slice
from .dates import readable_dates


@readable_dates
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
    for (market,sid),items in by_supply.items():
        first=items[0]; qty=sum(float(x.get('remaining_units') or 0) for x in items)
        icon=WB_ICON if market=='wildberries' else OZON_ICON
        label='WB' if market=='wildberries' else 'Ozon'
        eta=str(first.get('planned_at') or 'ETA неизвестна')
        wh=escape(str(first.get('warehouse_name') or 'склад не указан'))
        lines.append(f'\n{icon} {label} · <b>{escape(str(sid))}</b> · {escape(str(first.get("status") or ""))}\n  {qty:g} шт. · {eta} · {wh}')
        for x in items:
            name=escape(str(x.get('name') or ''))
            article=str(x.get('marketplace_sku') or x.get('internal_sku') or 'SKU')
            lines.append(f'  • {label} · поставка {escape(str(sid))} · {name} · артикул <code>{escape(article)}</code>: {float(x.get("remaining_units") or 0):g} шт.')
    unknown=sum(1 for r in rows if not r.get('planned_at'))
    if unknown:
        lines += ['',f'⚠️ У {unknown} товарных строк нет ETA. Они показаны здесь, но не уменьшают рекомендацию по закупке.']
    return '\n'.join(lines)


@readable_dates
def format_forecast_quality(report: ForecastQualityReport, limit: int | None=None) -> str:
    def pct(v): return '—' if v is None else f'{v:.1f}%'
    lines=[f'🎯 <b>Точность прогноза · {report.as_of.isoformat()}</b>','━━━━━━━━━━━━━━━━',
           f'Горизонт проверки: {report.horizon_days} дн. · модель {escape(report.method_version)}',
           f'Общий WAPE: <b>{pct(report.overall_wape_pct)}</b> · bias: {pct(report.overall_bias_pct)}','']
    if not report.items:
        lines.append('⏳ Пока недостаточно полной истории для backtest.')
        return '\n'.join(lines)
    lines.append('<b>SKU с наибольшей ошибкой</b>')
    for r in report.items[:limit]:
        lines.append(f'• Внутренний SKU <code>{escape(r.internal_sku)}</code> · WAPE {pct(r.wape_pct)} · bias {pct(r.bias_pct)} · samples {r.samples}')
    lines += ['','ℹ️ Это rolling backtest по прошлым полным дням. Ошибки API и неполные дни исключаются.']
    return '\n'.join(lines)


@readable_dates
def format_action_center(center: ActionCenter, *, page: int = 0) -> str:
    items = sorted(center.items, key=lambda item: (item.priority, item.category, item.title))
    visible, page, pages = page_slice(items, page)
    lines=['🎯 <b>Что делать сегодня</b>',f'Заказы учтены по {center.as_of.isoformat()}.',
        'Это рекомендация по прогнозу, проверьте перед закупкой.']
    if not center.items:
        lines.append('✅ По доступным данным срочных действий нет.')
        if center.snoozed_count:
            lines.append(f'⏰ Отложено действий: {center.snoozed_count}.')
        return '\n'.join(lines)
    lines.append(f'Всего: {len(items)} · страница {page + 1}/{pages}')
    groups={1:('🔴','Срочно'),2:('🟠','Сегодня'),3:('🟡','Планово'),4:('⚪️','Наблюдать')}
    for index, item in enumerate(visible, page * 5 + 1):
        icon, label = groups.get(item.priority, groups[4])
        state=' ✅ принято' if item.status=='acknowledged' else ''
        lines += ['', f'{index}. {icon} <b>{escape_clip(item.title, 160)}</b>{state}',
                  label + ' · ' + escape_clip(item.detail.replace('Это рекомендация по прогнозу, проверьте перед закупкой.', '').strip(), 170)]
        for label, sku in getattr(item, 'articles', ())[:1]:
            lines.append(f'{escape(label)} · артикул <code>{escape(sku)}</code>')
    if center.snoozed_count:
        lines += ['',f'⏰ Отложено и скрыто: {center.snoozed_count}.']
    lines += ['','Причины и расчёт — по кнопке «Подробнее».']
    return '\n'.join(lines)


@readable_dates
def format_action_detail(item) -> str:
    state = ' · ✅ принято' if item.status == 'acknowledged' else ''
    lines = [f'🎯 <b>{escape_clip(item.title, 1300)}</b>{state}', escape_clip(item.detail, 600)]
    for label, sku in getattr(item, 'articles', ()):
        lines.append(f'{escape(label)} · артикул <code>{escape(sku)}</code>')
    if item.evidence:
        lines += ['', '<b>Почему и как посчитано:</b>']
        lines += ['• ' + escape_clip(value, 120) for value in item.evidence[:12]]
    if item.hint:
        lines += ['', '<b>Где посмотреть:</b>', escape_clip(item.hint, 160)]
    return '\n'.join(lines)


@readable_dates
def format_action_history(rows: list[dict], *, days: int) -> str:
    lines=[f'📋 <b>История Action Center · {days} дн.</b>','━━━━━━━━━━━━━━━━']
    if not rows:
        return '\n'.join(lines+['📭 История действий пока пуста.'])
    for r in rows:
        p=int(r.get('priority') or 4); icon={1:'🔴',2:'🟠',3:'🟡',4:'⚪️'}.get(p,'⚪️')
        status=str(r.get('current_status') or 'resolved')
        mark={'open':'','acknowledged':' · ✅ принято','snoozed':' · ⏰ отложено','resolved':' · 🟢 решено'}.get(status,'')
        title=str(r.get('title') or '')
        old_prefix='🕒 Старый остаток · '
        if str(r.get('action_key') or '').startswith('stock:stale:') and title.startswith(old_prefix):
            title_html='<b>'+escape(old_prefix)+'</b><code>'+escape(title.removeprefix(old_prefix))+'</code>'
        else:
            title_html='<b>'+escape(title)+'</b>'
        lines.append(f'{icon} {escape(str(r.get("as_of_date") or ""))} · {title_html}{mark}')
        lines.append(f'  <code>{escape(str(r.get("action_key") or ""))}</code>')
    return '\n'.join(lines)
