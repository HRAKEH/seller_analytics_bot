"""One bounded Telegram digest for a shop's alert evaluation cycle."""
from collections import Counter
from html import escape
from .dates import readable_dates
from .text import escape_clip, page_slice


def sorted_alerts(repo, shop_id: int, report):
    risks = {f'{r.marketplace}:{r.sku}': r for r in report.stock_risks}
    def priority(state):
        rule = str(state.get('rule_key') or '')
        row = risks.get(str(state.get('subject_key') or ''))
        if rule == 'api_stale':
            return (0, 0, str(state.get('subject_key')))
        if row and row.available_units <= 0:
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
        if rule=='low_stock':
            row=risks.get(subject)
            label=f'{marketplace_label(market)} · артикул <code>{escape_clip(sku, 180)}</code>'
            if row:
                icon = '🔴' if row.available_units <= 0 else '🟠'
                lines += [f'\n{index}. {icon} <b>{escape_clip(row.name, 150)}</b>', label,
                          f'Остаток: <b>{row.available_units:g} шт.</b> · запас: {_cover(row)}']
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
    if rule != 'low_stock':
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
        lines = ['🚨 <b>Мало остатка</b>', f'{escape(marketplace_label(market))} · артикул <code>{escape(sku)}</code>']
        if row is None:
            return '\n'.join(lines + ['Нет актуального снимка. Сначала обновите остатки.'])
        lines += ['<b>' + escape_clip(row.name, 1700) + '</b>', f'Остаток: <b>{row.available_units:g} шт.</b>',
                  f'Резерв: {row.reserved_units:g} шт.', f'Текущий запас: {_cover(row)}']
        for scheme, quantity in row.scheme_units:
            lines.append(f'{escape(scheme)}: {quantity:g} шт.')
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


def _utf16_length(text: str) -> int:
    return len(text.encode('utf-16-le')) // 2


def _clip(text: str, limit: int) -> str:
    if _utf16_length(text) <= limit:
        return text
    result = []
    used = 0
    for char in text:
        size = _utf16_length(char)
        if used + size > limit - 1:
            break
        result.append(char)
        used += size
    return ''.join(result) + '…'


def format_alert_digest(notifications, *, shop_name: str | None = None) -> str | None:
    """Prioritize critical events and announce omitted detail without splitting.

    Count escaped HTML conservatively, including markup and UTF-16 units. The
    result fits one Telegram sendMessage even with emoji or HTML in a product
    name. Full active problems remain available through the existing menu.
    """
    notes = list(notifications)
    if not notes:
        return None

    def severity(note):
        return note.severity if note.severity in {'critical', 'resolved'} else 'warning'

    counts = Counter(severity(note) for note in notes)
    lines = []
    if shop_name:
        lines.append(f'🏪 <b>{escape(_clip(str(shop_name), 120))}</b>')
    lines += ['🚨 <b>Сводка оповещений</b>',
              f'Критичных: {counts["critical"]} · предупреждений: {counts["warning"]} · восстановлено: {counts["resolved"]}']
    priority = {'critical': 0, 'warning': 1, 'resolved': 2}
    icons = {'critical': '🔴', 'warning': '🟠', 'resolved': '✅'}
    footer = '\n\nТекущие проблемы: «🚨 Проблемы» → «🚨 Активные проблемы».'
    shown = 0
    for note in sorted(notes, key=lambda item: priority[severity(item)]):
        message = ' '.join(str(note.message).split())
        line = f'{icons[severity(note)]} {escape(_clip(message, 450))}'
        omitted = len(notes) - shown - 1
        suffix = f'\n\nЕщё событий: {omitted}. Подробности сокращены.' if omitted else ''
        if _utf16_length('\n'.join([*lines, line]) + suffix + footer) > 3900:
            break
        lines.append(line)
        shown += 1
    if shown < len(notes):
        lines.append(f'\nЕщё событий: {len(notes) - shown}. Подробности сокращены.')
    return '\n'.join(lines) + footer
