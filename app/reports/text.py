"""Conservative Telegram text limits, including emoji and HTML entities."""
from html import escape
from html.parser import HTMLParser
import re


def utf16_length(text: str) -> int:
    return len(text.encode('utf-16-le')) // 2


def page_slice(items, page: int = 0, size: int = 5):
    rows = list(items)
    pages = max(1, (len(rows) + size - 1) // size)
    page = max(0, min(int(page), pages - 1))
    return rows[page * size:(page + 1) * size], page, pages


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


class _Markup(HTMLParser):
    """Track tags so a page can close and reopen multiline formatting."""
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack = []

    def handle_starttag(self, tag, attrs):
        self.stack.append((tag, self.get_starttag_text()))

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()

    def prefix(self):
        return ''.join(raw for _, raw in self.stack)

    def suffix(self):
        return ''.join('</' + tag + '>' for tag, _ in reversed(self.stack))


class _LongLine(_Markup):
    def __init__(self, limit):
        super().__init__()
        self.limit = limit
        self.parts = []
        self.current = ''

    def append(self, value):
        if utf16_length(self.current + value + self.suffix()) > self.limit:
            self.parts.append(self.current + self.suffix())
            self.current = self.prefix()
        self.current += value

    def handle_starttag(self, tag, attrs):
        raw = self.get_starttag_text()
        # Reserve the closing tag before adding a new opening tag.
        if utf16_length(self.current + raw + '</' + tag + '>' + self.suffix()) > self.limit:
            self.parts.append(self.current + self.suffix())
            self.current = self.prefix()
        self.current += raw
        super().handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        self.current += '</' + tag + '>'

    def handle_data(self, data):
        for word in re.findall(r'\s+|\S+', data):
            value = escape(word, quote=False)
            if utf16_length(self.prefix() + value + self.suffix()) <= self.limit:
                self.append(value)
            else:
                for char in word:
                    self.append(escape(char, quote=False))

    def handle_entityref(self, name):
        self.append('&' + name + ';')

    def handle_charref(self, name):
        self.append('&#' + name + ';')


def paginate_report_html(text: str, *, limit: int = 1500, line_limit: int = 24, hard_limit: int = 3500) -> list[str]:
    """Compact pages with complete item blocks, HTML and unshortened names.

    The soft limit may be exceeded by one indivisible line (e.g. a name), up
    to a conservative hard limit. Long paragraphs retain their formatting.
    Code values that fit a line are never split or rewritten.
    """
    hard_limit = max(100, min(int(hard_limit), 3500))
    limit = max(100, min(int(limit), hard_limit))
    parser = _Markup()
    lines = []
    for raw in (text or '—').split('\n'):
        prefix = parser.prefix()
        parser.feed(raw)
        line = prefix + raw + parser.suffix()
        if utf16_length(line) > hard_limit:
            splitter = _LongLine(limit)
            splitter.feed(line)
            lines.extend(splitter.parts + [splitter.current + splitter.suffix()])
        else:
            lines.append(line)
    # Consecutive detail lines belong to the preceding product/record.
    blocks = []
    for line in lines:
        start = re.match(r'^\s*(?:\d+\.|[•🚨🧠🔴🟠🟡⚪🔵🟣])', line)
        if not blocks or not line.strip() or start:
            blocks.append([line])
        else:
            blocks[-1].append(line)
    pages = []
    current = []

    def full(candidate):
        return utf16_length('\n'.join(candidate)) > limit or len(candidate) > line_limit

    for block in blocks:
        if not full(block):
            if current and full(current + block):
                pages.append('\n'.join(current).strip())
                current = []
            current.extend(block)
        else:
            for line in block:
                if current and full(current + [line]):
                    pages.append('\n'.join(current).strip())
                    current = []
                current.append(line)
    if current:
        pages.append('\n'.join(current).strip())
    return [page for page in pages if page] or ['—']
