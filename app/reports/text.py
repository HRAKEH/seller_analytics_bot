"""Conservative Telegram text limits, including emoji and HTML entities."""
from html import escape
from html.parser import HTMLParser


def utf16_length(text: str) -> int:
    return len(text.encode('utf-16-le')) // 2


def escape_clip(text: str, limit: int) -> str:
    """Clip plain text before escaping, without cutting an HTML entity."""
    escaped = escape(str(text))
    if utf16_length(escaped) <= limit:
        return escaped
    result = []
    size = 0
    for char in str(text):
        value = escape(char)
        used = utf16_length(value)
        if size + used > limit - 1:
            break
        result.append(value)
        size += used
    return ''.join(result) + '…'


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def split_report_html(text: str, limit: int = 3900) -> list[str]:
    """Keep formatter lines intact; preserve all text in an oversized line.

    Report markup is balanced within each line. A pathological long line is
    rendered as plain escaped text, avoiding broken tags or entities at a cut.
    """
    chunks = []
    current = ''
    for line in (text or '').split('\n'):
        if utf16_length(line) > limit:
            if current:
                chunks.append(current)
                current = ''
            parser = _PlainText()
            parser.feed(line)
            part = ''
            for char in ''.join(parser.parts):
                value = escape(char)
                if utf16_length(part + value) > limit:
                    chunks.append(part)
                    part = ''
                part += value
            current = part
            continue
        candidate = line if not current else current + '\n' + line
        if utf16_length(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current or not chunks:
        chunks.append(current or '—')
    return chunks
