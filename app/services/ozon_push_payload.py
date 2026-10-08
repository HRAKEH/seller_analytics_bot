"""Documented Ozon notifications; cancellation time never comes from polling."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

MAX_BODY_BYTES = 256 * 1024
CANCELLATION_TYPES = {'TYPE_POSTING_CANCELLED': ('FBS', 'changed_state_date'),
                      'TYPE_FBO_POSTING_CANCELLED': ('FBO', 'cancel_date')}


class PushError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def base_url(value: str) -> str:
    """An HTTPS origin, not a secret-bearing URL or an arbitrary route."""
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        raise PushError('OZON_PUSH_BASE_URL: укажите HTTPS-адрес домена.') from None
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or
            parsed.path not in ('', '/') or parsed.query or parsed.fragment or
            port not in (None, 443) or not re.fullmatch(r'[A-Za-z0-9.-]+', parsed.hostname)):
        raise PushError('OZON_PUSH_BASE_URL: нужен HTTPS-домен без пути и параметров.')
    return 'https://' + parsed.netloc.rstrip('/')


def positive_int(value, name: str) -> int:
    if type(value) is not int or not 0 < value < 2**63:
        raise PushError(f'{name}: требуется положительное целое число.')
    return value


def stamp(value, name: str) -> str:
    if not isinstance(value, str) or len(value) > 80 or 'T' not in value:
        raise PushError(f'{name}: дата события не указана.')
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None or dt.utcoffset() is None or dt.year < 2000:
            raise ValueError
        return dt.astimezone(timezone.utc).isoformat(timespec='microseconds')
    except (ValueError, OverflowError):
        raise PushError(f'{name}: требуется дата и время с часовым поясом.') from None


def parse_notification(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise PushError('Ожидается JSON-объект.')
    kind = payload.get('message_type')
    if not isinstance(kind, str) or not re.fullmatch(r'TYPE_[A-Z_]{1,72}', kind):
        raise PushError('message_type: неизвестный формат уведомления.')
    if kind == 'TYPE_PING':
        stamp(payload.get('time'), 'time')
        return {'message_type': kind}
    seller = positive_int(payload.get('seller_id'), 'seller_id')
    parsed = {'message_type': kind, 'seller_id': seller}
    if kind not in CANCELLATION_TYPES:
        # An order cancellation has no product quantities. In particular it
        # must not be added on top of a cancellation of its postings.
        return parsed
    scheme, date_field = CANCELLATION_TYPES[kind]
    number = payload.get('posting_number')
    if not isinstance(number, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', number):
        raise PushError('posting_number: не указан номер отправления.')
    if payload.get('new_state') != 'posting_canceled':
        raise PushError('new_state: отмена отправления не подтверждена.')
    cancelled = stamp(payload.get(date_field), date_field)
    event_uuid = payload.get('uuid')
    if event_uuid is not None:
        try:
            if not isinstance(event_uuid, str):
                raise ValueError
            event_uuid = str(UUID(event_uuid))
        except (ValueError, AttributeError):
            raise PushError('uuid: неверный идентификатор события.') from None
    if scheme == 'FBO' and event_uuid is None:
        raise PushError('uuid: идентификатор события FBO не указан.')
    rows = payload.get('products')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
        raise PushError('products: товары отменённого отправления не указаны.')
    products = {}
    for row in rows:
        if not isinstance(row, dict):
            raise PushError('products: неверная строка товара.')
        sku = positive_int(row.get('sku'), 'sku')
        quantity = positive_int(row.get('quantity'), 'quantity')
        if sku in products:
            # Do not invent a rule for repeated SKU rows in a single event.
            raise PushError('products: SKU повторяется внутри уведомления.')
        products[sku] = quantity
    ordered = [{'sku': sku, 'quantity': qty} for sku, qty in sorted(products.items())]
    units = sum(products.values())
    if units >= 2**63:
        raise PushError('quantity: количество превышает допустимый размер.')
    parsed.update(scheme=scheme, posting_number=number, event_uuid=event_uuid,
                  cancelled_at=cancelled,
                  event_day=datetime.fromisoformat(cancelled).astimezone(ZoneInfo('Europe/Moscow')).date().isoformat(),
                  units=units, products_json=canonical(ordered))
    semantic = {key: parsed[key] for key in ('seller_id', 'scheme', 'posting_number', 'cancelled_at', 'products_json')}
    parsed['semantic_hash'] = digest(canonical(semantic))
    return parsed
