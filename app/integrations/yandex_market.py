"""Read-only Yandex Market Partner API client."""
from __future__ import annotations
import hashlib
from .base import MarketplaceClient, FetchResult


class YandexMarketClient(MarketplaceClient):
    def __init__(self, api_key: str, *, timeout: float = 60, min_interval: float = 1.0, transport=None):
        if not api_key:
            raise ValueError('Yandex Market API key is required')
        super().__init__('yandex_market', 'https://api.partner.market.yandex.ru',
                         timeout=timeout, min_interval=min_interval, transport=transport,
                         rate_scope='yandex:' + hashlib.sha256(api_key.encode()).hexdigest())
        self._api_key = api_key

    def _headers(self):
        return {'Api-Key': self._api_key}

    async def campaigns(self, *, max_pages: int = 100) -> FetchResult:
        """Discover business and campaign IDs available to this key."""
        rows, token, seen, attempts = [], None, set(), 0
        for _ in range(max_pages):
            params = {'limit': 100}
            if token:
                params['pageToken'] = token
            response = await self.request('GET', '/v2/campaigns', params=params,
                                          headers=self._headers(), rate_key='campaigns')
            attempts += response.attempts
            if not response.ok:
                return response
            payload = response.data
            if not isinstance(payload, dict) or payload.get('status') == 'ERROR' or not isinstance(payload.get('campaigns'), list):
                return FetchResult.failure(self.source, 'Некорректный список магазинов Яндекс Маркета', response.status_code, attempts)
            page = payload['campaigns']
            if any(not isinstance(item, dict) or not isinstance(item.get('id'), int) for item in page):
                return FetchResult.failure(self.source, 'Некорректный идентификатор магазина Яндекс Маркета', response.status_code, attempts)
            rows.extend(page)
            paging = payload.get('paging') or {}
            if not isinstance(paging, dict):
                return FetchResult.failure(self.source, 'Некорректная пагинация Яндекс Маркета', response.status_code, attempts)
            next_token = paging.get('nextPageToken')
            if not next_token:
                return FetchResult.success(self.source, {'campaigns': rows}, 200, attempts)
            if not isinstance(next_token, str) or next_token in seen:
                return FetchResult.failure(self.source, 'Повтор страницы Яндекс Маркета', response.status_code, attempts)
            seen.add(next_token)
            token = next_token
        return FetchResult.failure(self.source, 'Превышен лимит страниц Яндекс Маркета', None, attempts)
