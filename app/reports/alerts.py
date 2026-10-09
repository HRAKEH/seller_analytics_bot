"""Shop alert lists and complete, compact pages of automatic notifications."""
from collections import Counter
from html import escape
from .dates import readable_dates, readable_text
from .text import escape_clip, page_slice, paginate_report_html, utf16_length
from .products import stock_data_warning


def sorted_alerts(repo, shop_id: int, report):
    risks = {f'{r.marketplace}:{r.sku}': r for r in report.stock_risks}
    def priority(state):
        rule = str(state.get('rule_key') or '')
        row = risks.get(str(state.get('subject_key') or ''))
        if rule == 'api_stale':
            return (0, 0, str(state.get('subject_key')))
        if row and row.inventory_confirmed and (row.available_units <= 0 or row.out_of_stock_schemes):
            return (0, 1, row.sku)
        if rule == 'low_stock':
            return (1, row.days_left if row and row.days_left is not None else float('inf'), str(state.get('subject_key')))
        return (2, 0, str(state.get('subject_key')))
    return sorted(repo.active_alert_states(shop_id), key=priority)


def alert_key(state) -> str:
    return str(state.get('rule_key') or '') + ':' + str(state.get('subject_key') or '')


def _cover(row):
    if row.available_units <= 0:
        return '0 дн.'
    if row.days_left is None:
        return 'нет оценки'
    return f'≈ {row.days_left:.1f} дн.'


@readable_dates
def format_active_alerts(repo, shop_id: int, report, *, page: int = 0) -> str:
    """Five short problems per page; quantities and forecast days are distinct."""
    from app.services.alerts import marketplace_label
    risks={f'{r.marketplace}:{r.sku}':r for r in report.stock_risks}
    states=sorted_alerts(repo, shop_id, report)
    visible, page, pages = page_slice(states, page)
    lines=['🚨 <b>Активные проблемы</b>']
    if not states:return '\n'.join(lines+['🟢 Активных проблем нет.'])
    lines.append(f'Всего: {len(states)} · страница {page + 1}/{pages}')
    for index, state in enumerate(visible, page * 5 + 1):
        rule=str(state.get('rule_key') or ''); subject=str(state.get('subject_key') or '')
        value=state.get('last_value'); market,_,sku=subject.partition(':')
        if rule in {'low_stock','stock_unknown'}:
            row=risks.get(subject)
            label=f'{marketplace_label(market)} · артикул <code>{escape(sku)}</code>'
            if row:
                icon = '🔴' if row.inventory_confirmed and (row.available_units <= 0 or row.out_of_stock_schemes) else '🟠'
                quantity_label='Остаток' if row.inventory_confirmed else 'Получено по API (неполно)'
                lines += [f'\n{index}. {icon} <b>{escape_clip(row.name, 150)}</b>', label,
                          f'{quantity_label}: <b>{row.available_units:g} шт.</b> · запас: {_cover(row) if row.inventory_confirmed else "не подтверждён"}']
                if row.scheme_units:
                    lines.append(' · '.join(f'{escape(s)} {q:g} шт.' for s,q in row.scheme_units))
                if not row.inventory_confirmed:lines.append(escape(stock_data_warning(row)))
                if row.out_of_stock_schemes:lines.append('Нет остатка: '+escape(', '.join(row.out_of_stock_schemes)))
            else:
                lines += [f'\n{index}. 🟠 {label}', 'Нет актуального остатка. Обновите остатки.']
        else:
            labels={'api_stale':'Данные заказов давно не обновлялись','order_drop':'Падение заказов','high_drr':'Высокий ДРР'}
            suffix='' if value is None else (f' · {float(value):.1f} ч.' if rule=='api_stale' else f' · {float(value):.1f}%')
            lines.append(f'\n{index}. '+escape(marketplace_label(subject))+': '+escape(labels.get(rule,rule))+suffix)
    lines.append('\nШтуки — остаток товара. Дни — прогноз запаса. Причины и расчёт — по кнопке «Подробнее».')
    return '\n'.join(lines)


@readable_dates
def format_alert_detail(state, report) -> str:
    from app.services.alerts import marketplace_label
    rule = str(state.get('rule_key') or '')
    subject = str(state.get('subject_key') or '')
    market, _, sku = subject.partition(':')
    value = state.get('last_value')
    if rule not in {'low_stock','stock_unknown'}:
        labels = {'api_stale': 'Данные заказов давно не обновлялись', 'order_drop': 'Падение заказов', 'high_drr': 'Высокий ДРР'}
        unit = 'ч.' if rule == 'api_stale' else '%'
        lines = ['🚨 <b>' + escape(labels.get(rule, rule)) + '</b>', escape(marketplace_label(subject)),
                 'Значение при проверке: ' + ('нет данных' if value is None else f'{float(value):.1f} {unit}')]
        explanations = {'api_stale': 'Время с последней успешной загрузки заказов. Обновите отчёты и проверьте ошибки API.',
                        'order_drop': 'Снижение заказов относительно среднего за предыдущие полные дни. Проверьте спрос и остатки.',
                        'high_drr': 'Расходы на рекламу в процентах от атрибутированных продаж. Проверьте рекламную статистику.'}
        lines.append(explanations.get(rule, 'Сохранённая проверка предупреждений.'))
    else:
        row = next((r for r in report.stock_risks if f'{r.marketplace}:{r.sku}' == subject), None)
        title='Проверить остатки' if rule=='stock_unknown' else 'Мало остатка'
        lines = [f'🚨 <b>{title}</b>', f'{escape(marketplace_label(market))} · артикул <code>{escape(sku)}</code>']
        if row is None:
            return '\n'.join(lines + ['Нет актуального снимка. Сначала обновите остатки.'])
        quantity_label='Остаток' if row.inventory_confirmed else 'Получено по API (неполно)'
        lines += ['<b>' + escape_clip(row.name, 1700) + '</b>', f'{quantity_label}: <b>{row.available_units:g} шт.</b>',
                  f'Резерв: {row.reserved_units:g} шт.', f'Текущий запас: {_cover(row) if row.inventory_confirmed else "не подтверждён"}']
        for scheme, quantity in row.scheme_units:
            lines.append(f'{escape(scheme)}: {quantity:g} шт.')
        if not row.inventory_confirmed:lines.append(escape(stock_data_warning(row)))
        if row.out_of_stock_schemes:lines.append('Нет остатка: '+escape(', '.join(row.out_of_stock_schemes)))
        if row.avg_daily_units is not None:
            lines += [f'Среднее: {row.avg_daily_units:.2f} шт./день за {row.coverage_days} загруженных дней.',
                      'Расчёт: остаток ÷ среднее количество заказанных единиц в день.']
        else:
            lines.append('Спрос неизвестен: недостаточно загруженных дней заказов.')
        if value is not None:
            lines.append(f'Запас при проверке предупреждения: {float(value):.1f} дн.')
        if row.captured_at:
            lines.append('Снимок API: ' + escape(row.captured_at))
        lines.append('Дни — прогноз, а не количество товара. Перед закупкой проверьте остаток.')
    if state.get('last_triggered_at'):
        lines.append('Предупреждение: ' + escape(str(state['last_triggered_at'])))
    for warning in getattr(report, 'inventory_warnings', ())[:2]:
        lines.append('⚠️ ' + escape_clip(warning, 220))
    return '\n'.join(lines)


_DIGEST_FOOTER = '\n\nТекущие проблемы: «🚨 Проблемы» → «🚨 Активные проблемы».'


def _digest_parts(notifications, shop_name):
    notes = list(notifications)
    if not notes:
        return None

    def severity(note):
        return note.severity if note.severity in {'critical', 'resolved'} else 'warning'

    counts = Counter(severity(note) for note in notes)
    lines = []
    if shop_name:
        lines.append(f'🏪 <b>{escape_clip(str(shop_name), 180)}</b>')
    lines += ['🚨 <b>Сводка оповещений</b>',
              f'Критичных: {counts["critical"]} · предупреждений: {counts["warning"]} · снято предупреждений: {counts["resolved"]}']
    priority = {'critical': 0, 'warning': 1, 'resolved': 2}
    icons = {'critical': '🔴', 'warning': '🟠', 'resolved': '✅'}
    events = []
    for note in sorted(notes, key=lambda item: priority[severity(item)]):
        message = ' '.join(str(note.message).split())
        if severity(note)=='resolved':message=message.removeprefix('✅ ')
        display = escape(message)
        _, _, sku = str(note.subject_key).partition(':')
        if sku:
            article = escape(sku)
            display = display.replace('артикул ' + article, 'артикул <code>' + article + '</code>', 1)
        events.append(f'{icons[severity(note)]} {display}')
    return '\n'.join(lines), '\n'.join(events)


def format_alert_digest(notifications, *, shop_name: str | None = None) -> str | None:
    """Full digest text; delivery must use pages for long notification lists."""
    parts = _digest_parts(notifications, shop_name)
    if parts is None:
        return None
    header, body = parts
    return header + '\n\n' + body + _DIGEST_FOOTER


def format_alert_digest_pages(notifications, *, shop_name: str | None = None,
                              timezone: str | None = None) -> list[str]:
    """Keep every event, repeat shop/counts, and reserve room for navigation.

    Dates are rendered in the originating shop's timezone before the snapshot
    is split and saved. HTML, emoji and long names retain all their content.
    """
    parts = _digest_parts(notifications, shop_name)
    if parts is None:
        return []
    header, body = (readable_text(part, tz=timezone) for part in parts)
    reserved = utf16_length(header + '\n\n' + _DIGEST_FOOTER)
    bodies = paginate_report_html(body, limit=1500-reserved, hard_limit=3500-reserved)
    return [header + '\n\n' + content + (_DIGEST_FOOTER if index == len(bodies)-1 else '')
            for index, content in enumerate(bodies)]
