"""Wildberries API client. Raw source responses only."""
from __future__ import annotations
import base64
import json
from datetime import datetime, timezone

from .base import MarketplaceClient, FetchResult


WB_TOKEN_TYPES = {
    1: 'Базовый',
    2: 'Тестовый',
    3: 'Персональный',
    4: 'Сервисный',
}

WB_TOKEN_CATEGORY_BITS = {
    1: 'Контент',
    2: 'Аналитика',
    3: 'Цены и скидки',
    4: 'Маркетплейс',
    5: 'Статистика',
    6: 'Продвижение',
    7: 'Вопросы и отзывы',
    9: 'Чат с покупателями',
    10: 'Поставки',
    11: 'Возвраты',
    12: 'Документы',
    13: 'Финансы',
    16: 'Пользователи',
}

WB_BOT_REQUIRED_CATEGORIES = (
    'Статистика',
    'Аналитика',
    'Финансы',
    'Продвижение',
    'Поставки',
    'Цены и скидки',
)


def decode_wb_token(token: str) -> dict:
    """Decode public WB JWT claims locally without verifying the signature.

    This is diagnostics only: actual API calls remain the source of truth for
    whether a token is active and accepted. The token itself is never logged or
    returned.
    """
    result = {
        'ok': False,
        'type_code': None,
        'type': 'Неизвестный',
        'categories': tuple(),
        'read_only': None,
        'expires_at': None,
        'expired': None,
        'error': None,
    }
    try:
        parts=(token or '').split('.')
        if len(parts) < 2:
            raise ValueError('токен не похож на JWT')
        raw=parts[1]
        raw += '=' * (-len(raw) % 4)
        payload=json.loads(base64.urlsafe_b64decode(raw.encode('ascii')).decode('utf-8'))
        if not isinstance(payload,dict):
            raise ValueError('JWT payload не является объектом')
        type_code=int(payload.get('acc')) if payload.get('acc') is not None else None
        mask=int(payload.get('s') or 0)
        categories=tuple(
            label for bit,label in WB_TOKEN_CATEGORY_BITS.items()
            if mask & (1 << bit)
        )
        exp=payload.get('exp')
        expires_at=None; expired=None
        if exp is not None:
            dt=datetime.fromtimestamp(int(exp),tz=timezone.utc)
            expires_at=dt.isoformat(timespec='seconds')
            expired=dt <= datetime.now(timezone.utc)
        result.update({
            'ok': True,
            'type_code': type_code,
            'type': WB_TOKEN_TYPES.get(type_code,'Неизвестный'),
            'categories': categories,
            'read_only': bool(mask & (1 << 30)),
            'expires_at': expires_at,
            'expired': expired,
        })
    except (ValueError,TypeError,KeyError,json.JSONDecodeError,UnicodeDecodeError) as exc:
        result['error']=str(exc)
    except Exception:
        # Diagnostics must never break startup/readiness because of a malformed
        # token. Deliberately avoid including exception text that could contain
        # unexpected token material.
        result['error']='не удалось декодировать JWT'
    return result


class WildberriesClient(MarketplaceClient):
    def __init__(self, token: str, *, timeout: float = 60, min_interval: float = 1.0,
                 max_retries: int = 3, transport=None):
        super().__init__('wildberries', 'https://statistics-api.wildberries.ru', timeout=timeout,
                         min_interval=min_interval, max_retries=max_retries, transport=transport)
        self.token = token

    def _headers(self):
        return {'Authorization': self.token} if self.token else {}

    async def ping(self, base_url: str = 'https://common-api.wildberries.ru') -> FetchResult:
        return await self.request('GET', base_url.rstrip('/') + '/ping', headers=self._headers(),
                                  rate_key='ping:'+base_url, min_interval=10.0)

    async def seller_info(self) -> FetchResult:
        return await self.request('GET','https://common-api.wildberries.ru/api/v1/seller-info',
                                  headers=self._headers(),rate_key='seller_info',min_interval=60.0)

    async def orders(self, date_from: str, *, flag: int = 0) -> FetchResult:
        """Operational orders. flag=1 returns rows whose order date matches date_from."""
        result = await self.request('GET', '/api/v1/supplier/orders',
                                    params={'dateFrom': date_from, 'flag': flag}, headers=self._headers())
        if result.ok and not isinstance(result.data, list):
            return FetchResult.failure(self.source, 'WB orders вернул ответ не в формате списка', result.status_code, result.attempts)
        return result

    async def orders_since(self, date_from: str, *, row_threshold: int = 79000, max_pages: int = 100) -> FetchResult:
        """Fetch WB order delta with documented lastChangeDate pagination."""
        all_rows = []
        cursor = date_from
        total_attempts = 0
        seen = set()
        for _ in range(max_pages):
            result = await self.orders(cursor, flag=0)
            total_attempts += result.attempts
            if not result.ok:
                return FetchResult.failure(self.source, result.error or 'WB orders error', result.status_code, total_attempts)
            rows = result.data or []
            if not rows:
                return FetchResult.success(self.source, all_rows, 200, total_attempts)
            signature = (len(rows), str(rows[0].get('srid', '')), str(rows[-1].get('srid', '')), str(rows[-1].get('lastChangeDate', '')))
            if signature in seen:
                return FetchResult.failure(self.source, 'WB pagination repeated the same page', 200, total_attempts)
            seen.add(signature)
            all_rows.extend(rows)
            if len(rows) < row_threshold:
                return FetchResult.success(self.source, all_rows, 200, total_attempts)
            next_cursor = str(rows[-1].get('lastChangeDate') or '')
            if not next_cursor or next_cursor == cursor:
                return FetchResult.failure(self.source, 'WB pagination cursor did not advance', 200, total_attempts)
            cursor = next_cursor
        return FetchResult.failure(self.source, f'WB pagination safety limit reached ({max_pages})', 200, total_attempts)

    async def sales(self, date_from: str, *, flag: int = 0) -> FetchResult:
        result = await self.request('GET', '/api/v1/supplier/sales',
                                    params={'dateFrom': date_from, 'flag': flag}, headers=self._headers())
        if result.ok and not isinstance(result.data, list):
            return FetchResult.failure(self.source, 'WB вернул ответ не в формате списка', result.status_code, result.attempts)
        return result

    async def sales_since(self, date_from: str, *, row_threshold: int = 79000, max_pages: int = 100) -> FetchResult:
        """Fetch WB sale/return delta with lastChangeDate cursor pagination."""
        all_rows=[]; cursor=date_from; attempts=0; seen=set()
        for _ in range(max_pages):
            result=await self.sales(cursor,flag=0); attempts += result.attempts
            if not result.ok:
                return FetchResult.failure(self.source,result.error or 'WB sales error',result.status_code,attempts)
            rows=result.data or []
            if not rows:
                return FetchResult.success(self.source,all_rows,200,attempts)
            signature=(len(rows),str(rows[0].get('saleID','')),str(rows[-1].get('saleID','')),str(rows[-1].get('lastChangeDate','')))
            if signature in seen:
                return FetchResult.failure(self.source,'WB sales pagination repeated the same page',200,attempts)
            seen.add(signature); all_rows.extend(rows)
            if len(rows) < row_threshold:
                return FetchResult.success(self.source,all_rows,200,attempts)
            next_cursor=str(rows[-1].get('lastChangeDate') or '')
            if not next_cursor or next_cursor==cursor:
                return FetchResult.failure(self.source,'WB sales pagination cursor did not advance',200,attempts)
            cursor=next_cursor
        return FetchResult.failure(self.source,f'WB sales pagination safety limit reached ({max_pages})',200,attempts)

    async def stock_report_page(self, kind: str, *, nm_ids: list[int] | None = None,
                                chrt_ids: list[int] | None = None, limit: int = 250000,
                                offset: int = 0) -> FetchResult:
        """Current WB stocks from Seller Analytics.

        ``kind='wb'`` means WB warehouses (FBW), ``kind='seller'`` means seller
        warehouses (FBS).
        """
        if kind not in {'wb', 'seller'}:
            raise ValueError("kind must be 'wb' or 'seller'")
        endpoint = 'wb-warehouses' if kind == 'wb' else 'seller-warehouses'
        body: dict = {'limit': min(max(1, int(limit)), 250000), 'offset': max(0, int(offset))}
        if nm_ids:
            body['nmIds'] = [int(x) for x in nm_ids[:1000]]
        if chrt_ids:
            body['chrtIds'] = [int(x) for x in chrt_ids]
        return await self.request(
            'POST',
            f'https://seller-analytics-api.wildberries.ru/api/analytics/v1/stocks-report/{endpoint}',
            json=body,
            headers=self._headers(),
            rate_key='analytics_stocks',
            min_interval=20.0,
        )

    async def stock_report_all(self, kind: str, *, limit: int = 250000,
                               max_pages: int = 100) -> FetchResult:
        """Fetch all current WB stock rows with offset pagination."""
        offset = 0
        all_items: list[dict] = []
        total_attempts = 0
        page_limit = min(max(1, int(limit)), 250000)
        for _ in range(max_pages):
            result = await self.stock_report_page(kind, limit=page_limit, offset=offset)
            total_attempts += result.attempts
            if not result.ok:
                return FetchResult.failure(self.source, result.error or 'WB stocks error',
                                           result.status_code, total_attempts)
            if result.status_code == 204 or result.data is None:
                return FetchResult.success(self.source, {'data': {'items': all_items}},
                                           result.status_code or 204, total_attempts)
            body = result.data if isinstance(result.data, dict) else {}
            data = body.get('data') or {}
            items = (data.get('items') if isinstance(data, dict) else None) or body.get('items') or []
            if not isinstance(items, list):
                return FetchResult.failure(self.source, 'WB stocks items are not a list',
                                           result.status_code, total_attempts)
            all_items.extend(items)
            if len(items) < page_limit:
                return FetchResult.success(self.source, {'data': {'items': all_items}},
                                           result.status_code or 200, total_attempts)
            offset += len(items)
        return FetchResult.failure(self.source, f'WB stocks pagination safety limit reached ({max_pages})',
                                   200, total_attempts)


    async def finance_sales_reports_list(self, date_from: str, date_to: str, *,
                                         period: str = 'daily', limit: int = 1000,
                                         offset: int = 0) -> FetchResult:
        if period not in {'daily', 'weekly'}:
            raise ValueError("period must be 'daily' or 'weekly'")
        body = {'dateFrom': date_from, 'dateTo': date_to, 'limit': min(max(1, int(limit)), 1000),
                'offset': max(0, int(offset)), 'period': period}
        result = await self.request('POST',
            'https://finance-api.wildberries.ru/api/finance/v1/sales-reports/list',
            json=body, headers=self._headers(), rate_key='finance_list', min_interval=60.0)
        if result.ok and result.data is not None and not isinstance(result.data, list):
            return FetchResult.failure(self.source, 'WB finance reports list is not a list',
                                       result.status_code, result.attempts)
        return result

    async def finance_sales_reports_list_all(self, date_from: str, date_to: str, *,
                                             period: str = 'daily', limit: int = 1000,
                                             max_pages: int = 100) -> FetchResult:
        all_rows: list[dict] = []
        attempts = 0
        offset = 0
        page_limit = min(max(1, int(limit)), 1000)
        for _ in range(max_pages):
            result = await self.finance_sales_reports_list(date_from, date_to, period=period,
                                                           limit=page_limit, offset=offset)
            attempts += result.attempts
            if not result.ok:
                return FetchResult.failure(self.source, result.error or 'WB finance list error',
                                           result.status_code, attempts)
            if result.status_code == 204 or result.data is None:
                return FetchResult.success(self.source, all_rows, result.status_code or 204, attempts)
            rows = result.data or []
            all_rows.extend(rows)
            if len(rows) < page_limit:
                return FetchResult.success(self.source, all_rows, result.status_code or 200, attempts)
            offset += len(rows)
        return FetchResult.failure(self.source, f'WB finance list pagination safety limit reached ({max_pages})',
                                   200, attempts)

    async def finance_sales_report_detailed_all(self, date_from: str, date_to: str, *,
                                                period: str = 'daily', limit: int = 100000,
                                                max_pages: int = 100) -> FetchResult:
        """Current finance-api v1 detailed report, paginated by rrdId until 204."""
        if period not in {'daily', 'weekly'}:
            raise ValueError("period must be 'daily' or 'weekly'")
        rrd_id = 0
        attempts = 0
        rows: list[dict] = []
        page_limit = min(max(1, int(limit)), 100000)
        seen: set[int] = set()
        for _ in range(max_pages):
            body = {'dateFrom': date_from, 'dateTo': date_to, 'limit': page_limit,
                    'rrdId': rrd_id, 'period': period}
            result = await self.request('POST',
                'https://finance-api.wildberries.ru/api/finance/v1/sales-reports/detailed',
                json=body, headers=self._headers(), rate_key='finance_detailed', min_interval=60.0)
            attempts += result.attempts
            if not result.ok:
                return FetchResult.failure(self.source, result.error or 'WB finance detailed error',
                                           result.status_code, attempts)
            if result.status_code == 204 or result.data is None:
                return FetchResult.success(self.source, rows, result.status_code or 204, attempts)
            page = result.data
            if not isinstance(page, list):
                return FetchResult.failure(self.source, 'WB finance detailed response is not a list',
                                           result.status_code, attempts)
            if not page:
                return FetchResult.success(self.source, rows, result.status_code or 200, attempts)
            rows.extend(page)
            last = page[-1] if isinstance(page[-1], dict) else {}
            raw = last.get('rrdId')
            if raw is None:
                raw = last.get('rrd_id')
            try:
                next_rrd = int(raw)
            except (TypeError, ValueError):
                if len(page) < page_limit:
                    return FetchResult.success(self.source, rows, result.status_code or 200, attempts)
                return FetchResult.failure(self.source, 'WB finance pagination row has no rrdId',
                                           result.status_code, attempts)
            if next_rrd == rrd_id or next_rrd in seen:
                return FetchResult.failure(self.source, 'WB finance pagination cursor did not advance',
                                           result.status_code, attempts)
            seen.add(next_rrd)
            rrd_id = next_rrd
            if len(page) < page_limit:
                # The API documents 204 as the canonical end marker; a short page is also safe.
                return FetchResult.success(self.source, rows, result.status_code or 200, attempts)
        return FetchResult.failure(self.source, f'WB finance pagination safety limit reached ({max_pages})',
                                   200, attempts)

    async def promotion_campaigns(self) -> FetchResult:
        return await self.request('GET', 'https://advert-api.wildberries.ru/adv/v1/promotion/count',
                                  headers=self._headers(), rate_key='promotion', min_interval=0.2)

    async def promotion_fullstats(self, campaign_ids: list[int], begin_date: str,
                                  end_date: str) -> FetchResult:
        ids = [int(x) for x in campaign_ids[:50]]
        if not ids:
            return FetchResult.success(self.source, [], 200, 1)
        return await self.request('GET', 'https://advert-api.wildberries.ru/adv/v3/fullstats',
                                  params={'ids': ','.join(str(x) for x in ids),
                                          'beginDate': begin_date, 'endDate': end_date},
                                  headers=self._headers(), rate_key='promotion_stats', min_interval=20.0)

    async def promotion_fullstats_all(self, begin_date: str, end_date: str) -> FetchResult:
        campaigns = await self.promotion_campaigns()
        if not campaigns.ok:
            return campaigns
        body = campaigns.data if isinstance(campaigns.data, dict) else {}
        ids: list[int] = []
        # Current response groups IDs inside adverts[].advert_list, but keep traversal tolerant.
        def walk(value):
            if isinstance(value, dict):
                for k, v in value.items():
                    if k in {'advertId', 'advert_id', 'id'} and isinstance(v, (int, str)):
                        try: ids.append(int(v))
                        except (TypeError, ValueError): pass
                    else: walk(v)
            elif isinstance(value, list):
                for item in value: walk(item)
        walk(body.get('adverts') or body)
        ids = sorted(set(ids))
        if not ids:
            return FetchResult.success(self.source, [], campaigns.status_code or 200, campaigns.attempts)
        all_rows: list = []
        attempts = campaigns.attempts
        for i in range(0, len(ids), 50):
            result = await self.promotion_fullstats(ids[i:i+50], begin_date, end_date)
            attempts += result.attempts
            if not result.ok:
                return FetchResult.failure(self.source, result.error or 'WB promotion stats error',
                                           result.status_code, attempts)
            if isinstance(result.data, list):
                all_rows.extend(result.data)
            elif result.data is not None:
                return FetchResult.failure(self.source, 'WB promotion fullstats is not a list',
                                           result.status_code, attempts)
        return FetchResult.success(self.source, all_rows, 200, attempts)

    async def fbw_supplies_page(self, *, status_ids: list[int] | None = None,
                                limit: int = 1000, offset: int = 0) -> FetchResult:
        """Current FBW supplies list from Supplies API."""
        body = {'statusIDs': [int(x) for x in (status_ids or [1,2,3,4,6])]}
        return await self.request(
            'POST', 'https://supplies-api.wildberries.ru/api/v1/supplies',
            params={'limit': min(max(1,int(limit)),1000), 'offset': max(0,int(offset))},
            json=body, headers=self._headers(), rate_key='fbw_supplies', min_interval=2.0)

    async def fbw_supplies_all(self, *, status_ids: list[int] | None = None,
                               limit: int = 1000, max_pages: int = 20) -> FetchResult:
        all_rows=[]; attempts=0; offset=0; page_limit=min(max(1,int(limit)),1000)
        for _ in range(max_pages):
            result=await self.fbw_supplies_page(status_ids=status_ids,limit=page_limit,offset=offset)
            attempts += result.attempts
            if not result.ok:
                return FetchResult.failure(self.source,result.error or 'WB FBW supplies error',result.status_code,attempts)
            rows=result.data or []
            if not isinstance(rows,list):
                return FetchResult.failure(self.source,'WB FBW supplies response is not a list',result.status_code,attempts)
            all_rows.extend(rows)
            if len(rows)<page_limit:
                return FetchResult.success(self.source,all_rows,result.status_code or 200,attempts)
            offset += len(rows)
        return FetchResult.failure(self.source,f'WB FBW supplies pagination safety limit reached ({max_pages})',200,attempts)

    async def fbw_supply_goods_page(self, supply_id: int, *, is_preorder_id: bool=False,
                                    limit: int=1000, offset: int=0) -> FetchResult:
        return await self.request(
            'GET', f'https://supplies-api.wildberries.ru/api/v1/supplies/{int(supply_id)}/goods',
            params={'limit':min(max(1,int(limit)),1000),'offset':max(0,int(offset)),
                    'isPreorderID':str(bool(is_preorder_id)).lower()},
            headers=self._headers(), rate_key='fbw_supplies', min_interval=2.0)

    async def fbw_supply_goods_all(self, supply_id: int, *, is_preorder_id: bool=False,
                                   limit: int=1000, max_pages: int=50) -> FetchResult:
        rows=[]; attempts=0; offset=0; page_limit=min(max(1,int(limit)),1000)
        for _ in range(max_pages):
            result=await self.fbw_supply_goods_page(supply_id,is_preorder_id=is_preorder_id,
                                                     limit=page_limit,offset=offset)
            attempts += result.attempts
            if not result.ok:
                return FetchResult.failure(self.source,result.error or 'WB FBW supply goods error',result.status_code,attempts)
            page=result.data or []
            if not isinstance(page,list):
                return FetchResult.failure(self.source,'WB FBW supply goods response is not a list',result.status_code,attempts)
            rows.extend(page)
            if len(page)<page_limit:
                return FetchResult.success(self.source,rows,result.status_code or 200,attempts)
            offset += len(page)
        return FetchResult.failure(self.source,f'WB FBW goods pagination safety limit reached ({max_pages})',200,attempts)


    async def calendar_promotions_page(self, start_at: str, end_at: str, *, all_promo: bool=True,
                                       limit: int=1000, offset: int=0) -> FetchResult:
        return await self.request('GET','https://dp-calendar-api.wildberries.ru/api/v1/calendar/promotions',
            params={'startDateTime':start_at,'endDateTime':end_at,'allPromo':str(bool(all_promo)).lower(),
                    'limit':min(max(1,int(limit)),1000),'offset':max(0,int(offset))},
            headers=self._headers(),rate_key='promo_calendar',min_interval=0.6)

    async def calendar_promotions_all(self, start_at: str, end_at: str, *, all_promo: bool=True,
                                      limit: int=1000, max_pages: int=50) -> FetchResult:
        rows=[]; offset=0; attempts=0; page_limit=min(max(1,int(limit)),1000)
        for _ in range(max_pages):
            result=await self.calendar_promotions_page(start_at,end_at,all_promo=all_promo,limit=page_limit,offset=offset)
            attempts += result.attempts
            if not result.ok:
                return FetchResult.failure(self.source,result.error or 'WB promotion calendar error',result.status_code,attempts)
            body=result.data if isinstance(result.data,dict) else {}; data=body.get('data') or {}; page=data.get('promotions') or []
            if not isinstance(page,list):
                return FetchResult.failure(self.source,'WB promotion calendar promotions are not a list',result.status_code,attempts)
            rows.extend(page)
            if len(page)<page_limit:
                return FetchResult.success(self.source,{'data':{'promotions':rows}},result.status_code or 200,attempts)
            offset += len(page)
        return FetchResult.failure(self.source,f'WB promotion calendar pagination safety limit reached ({max_pages})',200,attempts)

    async def calendar_promotion_products_page(self, promotion_id: int, *, in_action: bool=True,
                                               limit: int=1000, offset: int=0) -> FetchResult:
        return await self.request('GET','https://dp-calendar-api.wildberries.ru/api/v1/calendar/promotions/nomenclatures',
            params={'promotionID':int(promotion_id),'inAction':str(bool(in_action)).lower(),
                    'limit':min(max(1,int(limit)),1000),'offset':max(0,int(offset))},
            headers=self._headers(),rate_key='promo_calendar',min_interval=0.6)

    async def calendar_promotion_products_all(self, promotion_id: int, *, in_action: bool=True,
                                              limit: int=1000, max_pages: int=50) -> FetchResult:
        rows=[]; offset=0; attempts=0; page_limit=min(max(1,int(limit)),1000)
        for _ in range(max_pages):
            result=await self.calendar_promotion_products_page(promotion_id,in_action=in_action,limit=page_limit,offset=offset)
            attempts += result.attempts
            if not result.ok:
                return FetchResult.failure(self.source,result.error or 'WB promotion nomenclatures error',result.status_code,attempts)
            body=result.data if isinstance(result.data,dict) else {}; data=body.get('data') or {}; page=data.get('nomenclatures') or []
            if not isinstance(page,list):
                return FetchResult.failure(self.source,'WB promotion nomenclatures are not a list',result.status_code,attempts)
            rows.extend(page)
            if len(page)<page_limit:
                return FetchResult.success(self.source,{'data':{'nomenclatures':rows}},result.status_code or 200,attempts)
            offset += len(page)
        return FetchResult.failure(self.source,f'WB promotion nomenclature pagination safety limit reached ({max_pages})',200,attempts)
