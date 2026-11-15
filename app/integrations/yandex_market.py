"""Read-only Yandex Market Partner API client."""
from __future__ import annotations
import hashlib
from datetime import date
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

    async def _pages(self, path, key, *, body, nested=True, limit=50, max_pages=1000):
        rows, token, seen, attempts = [], None, set(), 0
        for _ in range(max_pages):
            params = {'limit': limit}
            if token:
                params['pageToken'] = token
            response = await self.request('POST', path, json=body, params=params,
                                          headers=self._headers(), rate_key=path)
            attempts += response.attempts
            if not response.ok:
                return response
            payload = response.data
            if not isinstance(payload, dict) or payload.get('status') == 'ERROR':
                return FetchResult.failure(self.source, 'Некорректный ответ Яндекс Маркета', response.status_code, attempts)
            data = payload.get('result') if nested else payload
            if not isinstance(data, dict) or not isinstance(data.get(key), list):
                return FetchResult.failure(self.source, 'Неполный ответ Яндекс Маркета: ' + key, response.status_code, attempts)
            if any(not isinstance(row, dict) for row in data[key]):
                return FetchResult.failure(self.source, 'Некорректные строки Яндекс Маркета', response.status_code, attempts)
            rows.extend(data[key])
            paging = data.get('paging', {})
            if not isinstance(paging, dict):
                return FetchResult.failure(self.source, 'Некорректная пагинация Яндекс Маркета', response.status_code, attempts)
            token = paging.get('nextPageToken')
            if not token:
                return FetchResult.success(self.source, {key: rows}, 200, attempts)
            if not isinstance(token, str) or token in seen or not data[key]:
                return FetchResult.failure(self.source, 'Повтор или незавершённая страница Яндекс Маркета', response.status_code, attempts)
            seen.add(token)
        return FetchResult.failure(self.source, 'Превышен лимит страниц Яндекс Маркета', None, attempts)

    async def orders_day(self, business_id: int, campaign_id: int, day: date):
        if business_id <= 0 or campaign_id <= 0:
            raise ValueError('Идентификаторы Яндекс Маркета должны быть положительными')
        return await self._pages(f'/v1/businesses/{business_id}/orders', 'orders', nested=False,
            body={'campaignIds': [campaign_id], 'fake': False,
                  'dates': {'creationDateFrom': day.isoformat(), 'creationDateTo': day.isoformat()}})

    async def catalogue(self, business_id: int):
        if type(business_id) is not int or business_id <= 0:
            raise ValueError('Некорректный businessId')
        return await self._pages(f'/v2/businesses/{business_id}/offer-mappings',
                                 'offerMappings', body={}, limit=100)

    async def campaign_stocks(self, campaign_id: int):
        """FBY/LaaS and shops with warehouse groups."""
        if type(campaign_id) is not int or campaign_id <= 0:
            raise ValueError('Некорректный campaignId')
        return await self._pages(f'/v2/campaigns/{campaign_id}/offers/stocks',
                                 'warehouses', body={}, limit=100)

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

    async def configured_campaign(self):
        """Verify that the configured campaign belongs to the configured business."""
        cached = getattr(self, '_campaign', None)
        if cached is not None:
            return FetchResult.success(self.source, cached, 200, 0)
        result = await self.campaigns()
        if not result.ok:
            return result
        matches = [x for x in result.data['campaigns'] if x['id'] == self.campaign_id]
        if len(matches) != 1 or not isinstance(matches[0].get('business'), dict) or matches[0]['business'].get('id') != self.business_id:
            return FetchResult.failure(self.source, 'campaignId не принадлежит businessId или недоступен ключу', None, result.attempts)
        self._campaign = matches[0]
        return FetchResult.success(self.source, self._campaign, 200, result.attempts)

    async def campaign_offers(self):
        return await self._pages(f'/v2/campaigns/{self.campaign_id}/offers', 'offers', body={}, limit=100)

    async def card_status_snapshot(self):
        valid = await self.configured_campaign()
        if not valid.ok:
            return valid
        offers = await self.campaign_offers()
        if not offers.ok:
            return offers
        catalogue = await self.catalogue(self.business_id)
        if not catalogue.ok:
            return catalogue
        return FetchResult.success(self.source, {**offers.data, **catalogue.data}, 200,
                                   valid.attempts + offers.attempts + catalogue.attempts)

    async def inventory_snapshot(self):
        valid = await self.configured_campaign()
        if not valid.ok:
            return valid
        scheme = valid.data.get('placementType')
        if scheme not in ('FBY','FBS','DBS','EXPRESS','LAAS'):
            return FetchResult.failure(self.source, 'Неизвестная модель работы магазина', None, valid.attempts)
        if scheme not in ('FBY','LAAS'):
            warehouses = await self._pages(f'/v3/businesses/{self.business_id}/warehouses', 'warehouses', body={}, limit=100)
            if warehouses.ok:
                selected = []
                for warehouse in warehouses.data['warehouses']:
                    models = warehouse.get('models')
                    if not isinstance(models, list) or any(not isinstance(m, dict) for m in models):
                        return FetchResult.failure(self.source, 'Нет привязки складов к магазину', None, warehouses.attempts)
                    if any(m.get('campaignId') == self.campaign_id for m in models):
                        selected.append(warehouse)
                if selected:
                    result = []; attempts = warehouses.attempts
                    for warehouse in selected:
                        wid = warehouse.get('id')
                        if type(wid) is not int or wid <= 0:
                            return FetchResult.failure(self.source, 'Нет ID партнёрского склада', None, attempts)
                        stocks = await self._pages(f'/v3/businesses/{self.business_id}/offers/stocks', 'offers',
                                                  body={'partnerWarehouseId': wid}, limit=100)
                        attempts += stocks.attempts
                        if not stocks.ok:
                            return stocks
                        result.append({'warehouseId': wid, 'offers': stocks.data['offers']})
                    return FetchResult.success(self.source, {'warehouses': result, 'scheme': scheme}, 200, attempts)
            elif warehouses.status_code != 400:
                return warehouses
            # Grouped warehouses are served only by the campaign endpoint.
        result = await self.campaign_stocks(self.campaign_id)
        if result.ok:
            return FetchResult.success(self.source, {**result.data, 'scheme': scheme}, 200, result.attempts)
        return result
    async def order_finance(self, day):
        valid = await self.configured_campaign()
        if not valid.ok:
            return valid
        return await self._pages(f'/v2/campaigns/{self.campaign_id}/stats/orders', 'orders',
                                 body={'dateFrom':day.isoformat(),'dateTo':day.isoformat()}, limit=200)

    async def promotions_snapshot(self):
        valid = await self.configured_campaign()
        if not valid.ok:
            return valid
        response = await self.request('POST', f'/v2/businesses/{self.business_id}/promos',
                                      json={}, headers=self._headers(),rate_key='promos')
        if not response.ok:
            return response
        payload=response.data
        data=payload.get('result') if isinstance(payload,dict) and payload.get('status')!='ERROR' else None
        if not isinstance(data,dict) or not isinstance(data.get('promos'),list):
            return FetchResult.failure(self.source,'Нет списка акций',response.status_code,response.attempts)
        details={}; attempts=response.attempts
        for promo in data['promos']:
            if not isinstance(promo,dict) or not isinstance(promo.get('id'),str) or not promo['id']:
                return FetchResult.failure(self.source,'Некорректная акция',None,attempts)
            offers=await self._pages(f'/v2/businesses/{self.business_id}/promos/offers','offers',
                                    body={'promoId':promo['id']},limit=100)
            attempts+=offers.attempts
            if not offers.ok:
                return offers
            details[promo['id']]=offers.data['offers']
        return FetchResult.success(self.source,{'promos':data['promos'],'products':details,'campaignId':self.campaign_id},200,attempts)

    async def inbound_snapshot(self):
        valid=await self.configured_campaign()
        if not valid.ok:
            return valid
        if valid.data.get('placementType') not in ('FBY','LAAS'):
            return FetchResult.success(self.source,{'requests':[],'items':{},'not_applicable':True},200,valid.attempts)
        result=await self._pages(f'/v2/campaigns/{self.campaign_id}/supply-requests','requests',
                                body={'requestTypes':['SUPPLY']},limit=100)
        if not result.ok:
            return result
        items={}; attempts=result.attempts
        for row in result.data['requests']:
            rid=(row.get('id') or {}).get('id') if isinstance(row.get('id'),dict) else None
            if type(rid) is not int or rid<=0:
                return FetchResult.failure(self.source,'Нет ID заявки на поставку',None,attempts)
            detail=await self._pages(f'/v2/campaigns/{self.campaign_id}/supply-requests/items','items',body={'requestId':rid},limit=100)
            attempts+=detail.attempts
            if not detail.ok:return detail
            items[str(rid)]=detail.data['items']
        return FetchResult.success(self.source,{'requests':result.data['requests'],'items':items},200,attempts)
