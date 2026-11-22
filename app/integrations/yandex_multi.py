"""All-or-nothing snapshots for several campaigns in one Market business."""
from .base import FetchResult
from .yandex_market import YandexMarketClient


class YandexMarketMultiClient:
    source = 'yandex_market'

    def __init__(self, api_key, business_id, campaign_ids, **kwargs):
        self.business_id = business_id
        self.campaign_id = tuple(dict.fromkeys(campaign_ids))
        if not self.campaign_id or any(type(cid) is not int or cid <= 0 for cid in self.campaign_id):
            raise ValueError('Нужны положительные campaignId')
        self.clients = []
        for cid in self.campaign_id:
            client = YandexMarketClient(api_key, **kwargs)
            client.business_id = business_id
            client.campaign_id = cid
            self.clients.append(client)

    async def close(self):
        for client in self.clients:
            await client.close()

    async def configured_campaign(self):
        result = await self.clients[0].campaigns()
        if not result.ok:
            return result
        for client in self.clients:
            matches = [c for c in result.data['campaigns'] if c['id'] == client.campaign_id]
            if (len(matches) != 1 or not isinstance(matches[0].get('business'), dict)
                    or matches[0]['business'].get('id') != self.business_id):
                return FetchResult.failure(self.source,
                    f'Магазин {client.campaign_id} недоступен ключу или не принадлежит businessId',
                    None, result.attempts)
        # Cache only after every campaign has been validated.
        for client in self.clients:
            client._campaign = next(c for c in result.data['campaigns'] if c['id'] == client.campaign_id)
        return FetchResult.success(self.source, result.data, 200, result.attempts)

    async def _all(self, method, *args):
        valid = await self.configured_campaign()
        if not valid.ok:
            return valid
        snapshots = []; attempts = valid.attempts
        for client in self.clients:
            if method == 'orders_day':
                result = await client.orders_day(self.business_id, client.campaign_id, *args)
            else:
                result = await getattr(client, method)(*args)
            attempts += result.attempts
            if not result.ok:
                return FetchResult.failure(self.source,
                    f'Магазин {client.campaign_id}: {result.error}', result.status_code, attempts)
            snapshots.append({'campaignId': client.campaign_id, 'data': result.data})
        return FetchResult.success(self.source, {'campaignSnapshots': snapshots, 'scheme': 'MULTI'}, 200, attempts)

    async def orders_day(self, business_id, campaign_id, day):
        if business_id != self.business_id or campaign_id != self.campaign_id:
            raise ValueError('Запрошен другой набор магазинов')
        result = await self._all('orders_day', day)
        if result.ok:
            orders = []
            for snapshot in result.data['campaignSnapshots']:
                for order in snapshot['data']['orders']:
                    if order.get('campaignId') != snapshot['campaignId']:
                        return FetchResult.failure(self.source, 'В ответе заказ другого магазина', None, result.attempts)
                    orders.append(order)
            return FetchResult.success(self.source, {'orders': orders}, 200, result.attempts)
        return result

    async def inventory_snapshot(self):
        return await self._all('inventory_snapshot')

    async def card_status_snapshot(self):
        return await self._all('card_status_snapshot')

    async def order_finance(self, day):
        return await self._all('order_finance', day)

    async def promotions_snapshot(self):
        return await self._all('promotions_snapshot')

    async def inbound_snapshot(self):
        return await self._all('inbound_snapshot')
