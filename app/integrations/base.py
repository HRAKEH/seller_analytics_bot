"""Shared async HTTP primitives for marketplace integrations."""
from __future__ import annotations
import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
import httpx

log=logging.getLogger(__name__)

@dataclass(frozen=True)
class FetchResult:
    source: str
    ok: bool
    data: Any = None
    error: str | None = None
    status_code: int | None = None
    received_at: datetime | None = None
    attempts: int = 1

    @classmethod
    def success(cls, source: str, data: Any, status_code: int, attempts: int):
        return cls(source, True, data, None, status_code, datetime.now(timezone.utc), attempts)

    @classmethod
    def failure(cls, source: str, error: str, status_code: int | None, attempts: int):
        return cls(source, False, None, error, status_code, None, attempts)

def _server_retry_delay(response: httpx.Response) -> float | None:
    """Return the server-requested retry delay in seconds.

    WB documents X-Ratelimit-Retry as the safe retry delay for 429 responses.
    Retry-After is supported as seconds or an HTTP date for other APIs.
    X-Ratelimit-Reset is a conservative fallback when Retry is absent.
    """
    for name in ('X-Ratelimit-Retry','Retry-After','X-Ratelimit-Reset'):
        raw=response.headers.get(name)
        if not raw:
            continue
        try:
            return max(1.0,float(raw))
        except (TypeError,ValueError):
            if name!='Retry-After':
                continue
            try:
                target=parsedate_to_datetime(raw)
                if target.tzinfo is None:
                    target=target.replace(tzinfo=timezone.utc)
                return max(1.0,(target-datetime.now(timezone.utc)).total_seconds())
            except (TypeError,ValueError,OverflowError):
                continue
    return None


class MarketplaceClient:
    def __init__(self, source: str, base_url: str, *, timeout: float = 60,
                 min_interval: float = 1.0, max_retries: int = 3,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.source, self.base_url = source, base_url.rstrip('/')
        self.timeout, self.min_interval, self.max_retries = timeout, min_interval, max_retries
        self._rate_locks: dict[str, asyncio.Lock] = {}
        self._rate_next: dict[str, float] = {}
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout), transport=transport)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def close(self):
        await self._client.aclose()

    async def request(self, method: str, path: str, *, params=None, json=None, headers=None,
                      rate_key: str = 'default', min_interval: float | None = None,
                      retry_on_429: bool = True) -> FetchResult:
        url = path if path.startswith('https://') else f'{self.base_url}/{path.lstrip("/")}'
        interval = self.min_interval if min_interval is None else max(0.0, float(min_interval))
        lock = self._rate_locks.setdefault(rate_key, asyncio.Lock())
        for attempt in range(1, self.max_retries + 2):
            retry_wait = 0.0
            async with lock:
                loop = asyncio.get_running_loop()
                due_wait = max(0.0, self._rate_next.get(rate_key, 0.0) - loop.time())
                if due_wait:
                    await asyncio.sleep(due_wait)
                try:
                    response = await self._client.request(method, url, params=params, json=json, headers=headers)
                except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                    self._rate_next[rate_key] = loop.time() + interval
                    if attempt > self.max_retries:
                        return FetchResult.failure(self.source, f'Сетевая ошибка: {exc}', None, attempt)
                    retry_wait = min(120.0, max(interval, 1.0) * attempt) + random.uniform(0, .25)
                else:
                    self._rate_next[rate_key] = loop.time() + interval
                    if response.status_code == 204:
                        return FetchResult.success(self.source, None, 204, attempt)
                    if response.status_code == 200:
                        try:
                            return FetchResult.success(self.source, response.json(), 200, attempt)
                        except ValueError:
                            return FetchResult.failure(self.source, 'Некорректный JSON', 200, attempt)
                    if response.status_code == 429:
                        requested=_server_retry_delay(response)
                        if not retry_on_429:
                            hint=f' · повторить примерно через {requested:.0f} сек.' if requested is not None else ''
                            return FetchResult.failure(self.source, f'HTTP 429: лимит запросов{hint}', 429, attempt)
                        if attempt > self.max_retries:
                            return FetchResult.failure(self.source, f'HTTP 429: {response.text[:300]}', 429, attempt)
                        retry_wait=requested if requested is not None else min(120.0, 2 ** attempt * 2)
                        # Never retry earlier than the marketplace explicitly asks.
                        self._rate_next[rate_key] = max(self._rate_next.get(rate_key, 0.0), loop.time() + retry_wait)
                        log.warning(
                            'Rate limited by %s; retrying in %.1f seconds (attempt %s/%s, rate_key=%s)',
                            self.source,retry_wait,attempt,self.max_retries+1,rate_key)
                    elif 500 <= response.status_code < 600:
                        if attempt > self.max_retries:
                            return FetchResult.failure(self.source, f'HTTP {response.status_code}: {response.text[:300]}', response.status_code, attempt)
                        retry_wait=min(120.0, 2 ** attempt * 2)
                        self._rate_next[rate_key] = max(self._rate_next.get(rate_key, 0.0), loop.time() + retry_wait)
                    else:
                        return FetchResult.failure(self.source, f'HTTP {response.status_code}: {response.text[:300]}', response.status_code, attempt)
            if retry_wait:
                await asyncio.sleep(retry_wait)
        return FetchResult.failure(self.source, 'Превышено число попыток', None, self.max_retries + 1)
