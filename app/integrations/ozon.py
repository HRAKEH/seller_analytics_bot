"""Ozon Seller API client.

This module only transports raw source data. Business meanings are normalized in
``app.services`` so API plumbing cannot silently redefine a metric.
"""
from __future__ import annotations
from .base import MarketplaceClient, FetchResult


class OzonClient(MarketplaceClient):
    def __init__(self, client_id: str, api_key: str, *, timeout: float = 60,
                 min_interval: float = 1.0, max_retries: int = 3, transport=None):
        super().__init__('ozon', 'https://api-seller.ozon.ru', timeout=timeout,
                         min_interval=min_interval, max_retries=max_retries, transport=transport)
        self.client_id, self.api_key = client_id, api_key

    def _headers(self):
        return {'Content-Type': 'application/json', 'Client-Id': self.client_id, 'Api-Key': self.api_key}

    async def seller_info(self) -> FetchResult:
        return await self.request('POST','/v1/seller/info',json={},headers=self._headers(),rate_key='seller_info',min_interval=1.0)

    async def api_roles(self) -> FetchResult:
        return await self.request('POST','/v1/roles',json={},headers=self._headers(),rate_key='roles',min_interval=1.0)

    async def analytics(self, payload: dict) -> FetchResult:
        return await self.request('POST', '/v1/analytics/data', json=payload, headers=self._headers(), rate_key='analytics')

    async def analytics_all(self, payload: dict, *, max_pages: int = 100) -> FetchResult:
        """Fetch all Ozon analytics rows using ``offset`` pagination.

        The caller controls dimensions/metrics. ``result.data`` from all pages is
        merged into a single response. This is important for shops where
        SKU×day rows exceed one page.
        """
        request_payload = dict(payload)
        limit = int(request_payload.get('limit') or 1000)
        limit = max(1, min(limit, 1000))
        offset = int(request_payload.get('offset') or 0)
        rows: list[dict] = []
        total_attempts = 0
        last_result: FetchResult | None = None
        for _ in range(max_pages):
            request_payload['limit'] = limit
            request_payload['offset'] = offset
            result = await self.analytics(request_payload)
            last_result = result
            total_attempts += result.attempts
            if not result.ok:
                return FetchResult.failure(self.source, result.error or 'Ozon analytics error',
                                           result.status_code, total_attempts)
            body = result.data if isinstance(result.data, dict) else {}
            inner = body.get('result') or {}
            page = inner.get('data') or inner.get('rows') or []
            if not isinstance(page, list):
                return FetchResult.failure(self.source, 'Ozon analytics rows are not a list',
                                           result.status_code, total_attempts)
            rows.extend(page)
            if len(page) < limit:
                merged = dict(body)
                merged_inner = dict(inner)
                merged_inner['data'] = rows
                # Always aggregate product rows ourselves after pagination. A page-level
                # totals field must never make a multi-page shop look like one page.
                if rows:
                    merged_inner.pop('totals', None)
                merged['result'] = merged_inner
                return FetchResult.success(self.source, merged, result.status_code or 200, total_attempts)
            offset += len(page)
        code = last_result.status_code if last_result else 200
        return FetchResult.failure(self.source, f'Ozon analytics pagination safety limit reached ({max_pages})',
                                   code, total_attempts)

    async def fbs_postings(self, payload: dict) -> FetchResult:
        return await self.request('POST', '/v4/posting/fbs/list', json=payload, headers=self._headers(), rate_key='postings', min_interval=1.0)

    async def fbo_postings(self, payload: dict) -> FetchResult:
        return await self.request('POST', '/v3/posting/fbo/list', json=payload, headers=self._headers(), rate_key='postings', min_interval=1.0)

    async def postings_all(self, scheme: str, since: str, to: str, *, limit: int = 1000,
                           max_pages: int = 100) -> FetchResult:
        """Fetch current-version FBO/FBS postings with cursor pagination."""
        scheme=scheme.upper()
        if scheme not in {'FBO','FBS'}:
            raise ValueError("scheme must be 'FBO' or 'FBS'")
        cursor=''; seen:set[str]=set(); all_postings:list[dict]=[]; attempts=0
        for _ in range(max_pages):
            payload={'cursor':cursor,'filter':{'since':since,'to':to},
                     'limit':min(max(1,int(limit)),1000),'sort_dir':'asc'}
            result=await (self.fbo_postings(payload) if scheme=='FBO' else self.fbs_postings(payload))
            attempts += result.attempts
            if not result.ok:
                return FetchResult.failure(self.source,result.error or f'Ozon {scheme} postings error',result.status_code,attempts)
            body=result.data if isinstance(result.data,dict) else {}
            postings=body.get('postings') or (body.get('result') or {}).get('postings') or []
            if not isinstance(postings,list):
                return FetchResult.failure(self.source,f'Ozon {scheme} postings are not a list',result.status_code,attempts)
            all_postings.extend(postings)
            has_next=bool(body.get('has_next') if 'has_next' in body else (body.get('result') or {}).get('has_next'))
            next_cursor=str(body.get('cursor') or (body.get('result') or {}).get('cursor') or '')
            if not has_next:
                return FetchResult.success(self.source,{'postings':all_postings,'has_next':False,'cursor':next_cursor},result.status_code or 200,attempts)
            if not next_cursor or next_cursor==cursor or next_cursor in seen:
                return FetchResult.failure(self.source,f'Ozon {scheme} postings cursor did not advance',result.status_code,attempts)
            seen.add(next_cursor); cursor=next_cursor
        return FetchResult.failure(self.source,f'Ozon {scheme} postings pagination safety limit reached ({max_pages})',200,attempts)

    async def product_stocks(self, payload: dict) -> FetchResult:
        return await self.request('POST', '/v4/product/info/stocks', json=payload, headers=self._headers(), rate_key='stocks', min_interval=1.0)

    async def product_stocks_all(self, *, limit: int = 1000, max_pages: int = 100) -> FetchResult:
        """Return all current product stock rows using Ozon cursor pagination."""
        cursor = ''
        seen: set[str] = set()
        all_items: list[dict] = []
        total_attempts = 0
        for _ in range(max_pages):
            payload = {'cursor': cursor, 'filter': {'visibility': 'ALL', 'with_quant': {'created': True, 'exists': True}}, 'limit': min(max(1, limit), 1000)}
            result = await self.product_stocks(payload)
            total_attempts += result.attempts
            if not result.ok:
                return FetchResult.failure(self.source, result.error or 'Ozon stocks error',
                                           result.status_code, total_attempts)
            body = result.data if isinstance(result.data, dict) else {}
            items = body.get('items') or []
            if not isinstance(items, list):
                return FetchResult.failure(self.source, 'Ozon stocks items are not a list',
                                           result.status_code, total_attempts)
            all_items.extend(items)
            next_cursor = str(body.get('cursor') or '')
            total = body.get('total')
            if not items or not next_cursor or (isinstance(total, int) and len(all_items) >= total):
                return FetchResult.success(self.source, {'items': all_items, 'total': total},
                                           result.status_code or 200, total_attempts)
            if next_cursor == cursor or next_cursor in seen:
                return FetchResult.failure(self.source, 'Ozon stocks cursor did not advance',
                                           result.status_code, total_attempts)
            seen.add(next_cursor)
            cursor = next_cursor
        return FetchResult.failure(self.source, f'Ozon stocks pagination safety limit reached ({max_pages})',
                                   200, total_attempts)

    async def finance_accrual_types(self) -> FetchResult:
        """Current Ozon Seller Finance dictionary of accrual types."""
        return await self.request('POST', '/v1/finance/accrual/types', json={},
                                  headers=self._headers(), rate_key='finance', min_interval=1.0)

    async def finance_accrual_by_day(self, day: str, last_id: str = '') -> FetchResult:
        """Current Ozon Seller Finance daily accrual page."""
        return await self.request('POST', '/v1/finance/accrual/by-day',
                                  json={'date': day, 'last_id': last_id},
                                  headers=self._headers(), rate_key='finance', min_interval=1.0)

    async def finance_accrual_by_day_all(self, day: str, *, max_pages: int = 200) -> FetchResult:
        """Collect all daily accrual pages using last_id cursor with loop protection."""
        last_id = ''
        seen: set[str] = set()
        accruals: list[dict] = []
        attempts = 0
        for _ in range(max_pages):
            result = await self.finance_accrual_by_day(day, last_id)
            attempts += result.attempts
            if not result.ok:
                return FetchResult.failure(self.source, result.error or 'Ozon finance error',
                                           result.status_code, attempts)
            body = result.data if isinstance(result.data, dict) else {}
            rows = body.get('accruals') or []
            if not isinstance(rows, list):
                return FetchResult.failure(self.source, 'Ozon finance accruals are not a list',
                                           result.status_code, attempts)
            accruals.extend(rows)
            next_id = str(body.get('last_id') or '')
            if not next_id:
                return FetchResult.success(self.source, {'accruals': accruals, 'last_id': ''},
                                           result.status_code or 200, attempts)
            if next_id == last_id or next_id in seen:
                return FetchResult.failure(self.source, 'Ozon finance last_id did not advance',
                                           result.status_code, attempts)
            seen.add(next_id)
            last_id = next_id
        return FetchResult.failure(self.source,
                                   f'Ozon finance pagination safety limit reached ({max_pages})',
                                   200, attempts)

    async def supply_orders_page(self, *, states: list[str] | None=None, last_id: str='',
                                 limit: int=100) -> FetchResult:
        active=states or [
            'ORDER_STATE_DATA_FILLING','ORDER_STATE_READY_TO_SUPPLY','ORDER_STATE_ACCEPTED_AT_SUPPLY_WAREHOUSE',
            'ORDER_STATE_IN_TRANSIT','ORDER_STATE_ACCEPTANCE_AT_STORAGE_WAREHOUSE',
            'ORDER_STATE_REPORTS_CONFIRMATION_AWAITING','ORDER_STATE_REPORT_REJECTED',
        ]
        payload={'filter':{'states':active},'last_id':last_id,'limit':min(max(1,int(limit)),100),
                 'sort_by':'ORDER_STATE_UPDATED_AT','sort_dir':'DESC'}
        return await self.request('POST','/v3/supply-order/list',json=payload,headers=self._headers(),
                                  rate_key='supply_orders',min_interval=1.0)

    async def supply_orders_all(self, *, states: list[str] | None=None, limit: int=100,
                                max_pages: int=100) -> FetchResult:
        order_ids=[]; last_id=''; seen=set(); attempts=0
        for _ in range(max_pages):
            result=await self.supply_orders_page(states=states,last_id=last_id,limit=limit)
            attempts += result.attempts
            if not result.ok:
                return FetchResult.failure(self.source,result.error or 'Ozon supply list error',result.status_code,attempts)
            body=result.data if isinstance(result.data,dict) else {}
            page=body.get('order_ids') or body.get('supply_order_id') or []
            if not isinstance(page,list):
                return FetchResult.failure(self.source,'Ozon supply order_ids are not a list',result.status_code,attempts)
            order_ids.extend(str(x) for x in page)
            next_id=str(body.get('last_id') or body.get('last_supply_order_id') or '')
            if not page or not next_id:
                return FetchResult.success(self.source,{'order_ids':order_ids},result.status_code or 200,attempts)
            if next_id==last_id or next_id in seen:
                return FetchResult.failure(self.source,'Ozon supply list cursor did not advance',result.status_code,attempts)
            seen.add(next_id); last_id=next_id
        return FetchResult.failure(self.source,f'Ozon supply list pagination safety limit reached ({max_pages})',200,attempts)

    async def supply_orders_get(self, order_ids: list[str]) -> FetchResult:
        return await self.request('POST','/v3/supply-order/get',json={'order_ids':[str(x) for x in order_ids[:100]]},
                                  headers=self._headers(),rate_key='supply_orders',min_interval=1.0)

    async def supply_bundle_page(self, bundle_id: str, *, last_id: str='', limit: int=100) -> FetchResult:
        payload={'bundle_ids':[str(bundle_id)],'last_id':last_id,'limit':min(max(1,int(limit)),100),
                 'is_asc':True,'sort_field':'SKU'}
        return await self.request('POST','/v1/supply-order/bundle',json=payload,headers=self._headers(),
                                  rate_key='supply_orders',min_interval=1.0)

    async def supply_bundle_all(self, bundle_id: str, *, max_pages: int=100) -> FetchResult:
        items=[]; last_id=''; seen=set(); attempts=0
        for _ in range(max_pages):
            result=await self.supply_bundle_page(bundle_id,last_id=last_id)
            attempts += result.attempts
            if not result.ok:
                return FetchResult.failure(self.source,result.error or 'Ozon supply bundle error',result.status_code,attempts)
            body=result.data if isinstance(result.data,dict) else {}
            page=body.get('items') or []
            if not isinstance(page,list):
                return FetchResult.failure(self.source,'Ozon supply bundle items are not a list',result.status_code,attempts)
            items.extend(page)
            if not body.get('has_next'):
                return FetchResult.success(self.source,{'items':items},result.status_code or 200,attempts)
            next_id=str(body.get('last_id') or '')
            if not next_id or next_id==last_id or next_id in seen:
                return FetchResult.failure(self.source,'Ozon supply bundle cursor did not advance',result.status_code,attempts)
            seen.add(next_id); last_id=next_id
        return FetchResult.failure(self.source,f'Ozon supply bundle pagination safety limit reached ({max_pages})',200,attempts)


    async def promotions_list(self) -> FetchResult:
        """Available/current Ozon promotions."""
        return await self.request('GET','/v1/actions',headers=self._headers(),rate_key='promotions',min_interval=1.0)

    async def promotion_products_page(self, action_id: int, *, last_id: str='', limit: int=100) -> FetchResult:
        payload={'action_id':int(action_id),'limit':min(max(1,int(limit)),1000)}
        if last_id: payload['last_id']=last_id
        return await self.request('POST','/v1/actions/products',json=payload,headers=self._headers(),rate_key='promotions',min_interval=1.0)

    async def promotion_products_all(self, action_id: int, *, max_pages: int=100) -> FetchResult:
        rows=[]; last_id=''; seen=set(); attempts=0
        for _ in range(max_pages):
            result=await self.promotion_products_page(action_id,last_id=last_id)
            attempts += result.attempts
            if not result.ok:
                return FetchResult.failure(self.source,result.error or 'Ozon promotion products error',result.status_code,attempts)
            body=result.data if isinstance(result.data,dict) else {}; inner=body.get('result') or {}
            page=inner.get('products') or []
            if not isinstance(page,list):
                return FetchResult.failure(self.source,'Ozon promotion products are not a list',result.status_code,attempts)
            rows.extend(page); nxt=str(inner.get('last_id') or '')
            total=inner.get('total')
            if len(page)==0 or not nxt or (isinstance(total,(int,float)) and len(rows)>=int(total)):
                return FetchResult.success(self.source,{'result':{'products':rows,'total':total,'last_id':''}},result.status_code or 200,attempts)
            if nxt==last_id or nxt in seen:
                return FetchResult.failure(self.source,'Ozon promotion cursor did not advance',result.status_code,attempts)
            seen.add(nxt); last_id=nxt
        return FetchResult.failure(self.source,f'Ozon promotion pagination safety limit reached ({max_pages})',200,attempts)

    async def product_info_list(self, *, product_ids: list[int] | None=None,
                                offer_ids: list[str] | None=None) -> FetchResult:
        payload={}
        if product_ids: payload['product_id']=[int(x) for x in product_ids[:1000]]
        if offer_ids: payload['offer_id']=[str(x) for x in offer_ids[:1000]]
        return await self.request('POST','/v3/product/info/list',json=payload,headers=self._headers(),rate_key='product_info',min_interval=1.0)
