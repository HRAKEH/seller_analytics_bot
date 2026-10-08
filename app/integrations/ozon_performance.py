"""Ozon Performance API client (advertising), with its own OAuth2 credentials."""
from __future__ import annotations
import hashlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from .base import MarketplaceClient, FetchResult


class OzonPerformanceClient(MarketplaceClient):
    def __init__(self, client_id: str, client_secret: str, *, timeout: float = 60,
                 min_interval: float = 1.0, max_retries: int = 3, transport=None):
        fingerprint=f'{client_id}|{client_secret}'
        scope='ozon_performance:'+hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:24]
        super().__init__('ozon_performance', 'https://api-performance.ozon.ru', timeout=timeout,
                         min_interval=min_interval, max_retries=max_retries, transport=transport,
                         rate_scope=scope)
        self.client_id = client_id
        self.client_secret = client_secret
        self._access_token: str | None = None
        self._expires_at: datetime | None = None

    async def _token(self, force: bool = False, *, retry_on_429: bool = True,
                     fail_fast_rate_limit: bool = False) -> FetchResult:
        now = datetime.now(timezone.utc)
        if not force and self._access_token and self._expires_at and now < self._expires_at - timedelta(seconds=60):
            return FetchResult.success(self.source, {'access_token': self._access_token}, 200, 1)
        result = await self.request('POST', '/api/client/token', json={
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'grant_type': 'client_credentials',
        }, headers={'Content-Type': 'application/json', 'Accept': 'application/json'}, rate_key='token', min_interval=0,
            retry_on_429=retry_on_429,fail_fast_rate_limit=fail_fast_rate_limit)
        if not result.ok:
            return result
        body = result.data if isinstance(result.data, dict) else {}
        token = str(body.get('access_token') or '')
        if not token:
            return FetchResult.failure(self.source, 'Ozon Performance token response has no access_token', 200, result.attempts)
        try:
            expires = max(60, int(body.get('expires_in') or 1800))
        except (TypeError, ValueError):
            expires = 1800
        self._access_token = token
        self._expires_at = now + timedelta(seconds=expires)
        return result

    async def request_auth(self, method: str, path: str, *, params=None, json=None,
                           rate_key: str = 'performance') -> FetchResult:
        token = await self._token()
        if not token.ok:
            return token
        headers = {'Authorization': f'Bearer {self._access_token}', 'Content-Type': 'application/json', 'Accept': 'application/json'}
        result = await self.request(method, path, params=params, json=json, headers=headers, rate_key=rate_key)
        if result.status_code == 401:
            refreshed = await self._token(force=True)
            if not refreshed.ok:
                return refreshed
            headers['Authorization'] = f'Bearer {self._access_token}'
            result = await self.request(method, path, params=params, json=json, headers=headers, rate_key=rate_key)
        return result

    async def product_campaign_stats(self, date_from: str, date_to: str,
                                     campaign_ids: list[str] | None = None) -> FetchResult:
        params: list[tuple[str, str]] = [('dateFrom', date_from), ('dateTo', date_to)]
        for cid in campaign_ids or []:
            params.append(('campaignIds', str(cid)))
        # /json returns structured rows instead of CSV.
        return await self.request_auth('GET', '/api/client/statistics/campaign/product/json',
                                       params=params, rate_key='statistics')


    async def product_sku_stats(self, date_from: str, date_to: str,
                                campaign_ids: list[str] | None = None) -> FetchResult:
        ids = list(dict.fromkeys(str(x).strip() for x in campaign_ids or []))
        if not ids or any(not x.isascii() or not x.isdigit() or not 0 < int(x) < 2**64 for x in ids):
            return FetchResult.failure(self.source,
                'Реклама Ozon: для статистики товаров нужен список ID кампаний.', None, 0)
        payload = {'dateFrom': date_from, 'dateTo': date_to, 'campaignIds': ids}
        return await self.request_auth('POST', '/api/client/statistics/products/sku',
                                       json=payload, rate_key='statistics_products_sku')

    @staticmethod
    def sku_history_start():
        """The SKU method documents dateFrom no earlier than yesterday (Moscow)."""
        return datetime.now(ZoneInfo('Europe/Moscow')).date() - timedelta(days=1)
