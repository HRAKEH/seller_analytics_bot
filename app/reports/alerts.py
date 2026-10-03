"""One bounded Telegram digest for a shop's alert evaluation cycle."""
from collections import Counter
from html import escape


def format_active_alerts(repo, shop_id: int, report) -> str:
    """Render persisted states with current quantity and the stored value's unit."""
    from app.services.alerts import marketplace_label
    risks={f'{r.marketplace}:{r.sku}':r for r in report.stock_risks}
    states=repo.active_alert_states(shop_id)
    lines=['🚨 <b>Активные проблемы</b>']
    if not states:return '\n'.join(lines+['🟢 Активных проблем нет.'])
    for state in states[:30]:
        rule=str(state.get('rule_key') or ''); subject=str(state.get('subject_key') or '')
        value=state.get('last_value'); market,_,sku=subject.partition(':')
        if rule=='low_stock':
            row=risks.get(subject)
            label=f'{marketplace_label(market)} · артикул {sku}'
            if row:
                label+=f' · {row.name}'
                lines.append('• <b>'+escape(label)+'</b>')
                cover='не рассчитан' if value is None else f'≈ {float(value):.1f} дн.'
                lines.append(f'  Остаток: <b>{row.available_units:g} шт.</b> · запас при проверке: {cover}')
                if row.scheme_units:lines.append('  '+' · '.join(f'{escape(s)}: {v:g} шт.' for s,v in row.scheme_units))
                if row.avg_daily_units is not None:
                    lines.append(f'  Среднее: {row.avg_daily_units:.2f} шт./день за {row.coverage_days} загруженных дней')
                if row.captured_at:lines.append('  Снимок API: '+escape(row.captured_at))
            else:
                lines.append('• '+escape(label)+': нет актуального товарного снимка. Обновите остатки.')
        else:
            labels={'api_stale':'Данные заказов давно не обновлялись','order_drop':'Падение заказов','high_drr':'Высокий ДРР'}
            suffix='' if value is None else (f' · {float(value):.1f} ч.' if rule=='api_stale' else f' · {float(value):.1f}%')
            lines.append('• '+escape(marketplace_label(subject))+': '+escape(labels.get(rule,rule))+suffix)
    if len(states)>30:lines.append(f'Ещё проблем: {len(states)-30}.')
    lines.append('\nДни запаса = остаток в штуках ÷ среднее число заказанных единиц за загруженные дни. Это прогноз, а не количество товара.')
    for warning in getattr(report,'inventory_warnings',()):lines.append('⚠️ '+escape(warning))
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
