"""One bounded Telegram digest for a shop's alert evaluation cycle."""
from collections import Counter
from html import escape


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
