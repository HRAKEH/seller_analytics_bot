"""Strict Yandex Market normalization; amounts are not net seller payouts."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation
from app.storage import MetricPoint
from .product_analytics import ProductDayObservation, StockObservation

MARKET = 'yandex_market'


def rows(payload, key):
    value = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
        raise ValueError('Неполный ответ Яндекс Маркета: ' + key)
    return value


def quantity(value):
    if type(value) is not int or value < 0:
        raise ValueError('Некорректное количество Яндекс Маркета')
    return value


def money(value):
    if not isinstance(value, dict) or value.get('currencyId') not in ('RUR', 'RUB'):
        raise ValueError('Нет рублёвой суммы Яндекс Маркета')
    try:
        amount = Decimal(str(value['value']))
        if not amount.is_finite() or amount < 0:
            raise ValueError
        return amount
    except (KeyError, InvalidOperation, ValueError):
        raise ValueError('Некорректная сумма Яндекс Маркета') from None


def normalize_orders(payload, connection_id, day, campaign_id):
    products = {}; seen = set()
    campaigns = {campaign_id} if isinstance(campaign_id, int) else set(campaign_id)
    for order in rows(payload, 'orders'):
        oid = order.get('orderId')
        order_key = (order.get('campaignId'), oid)
        if type(oid) is not int or oid <= 0 or order_key in seen:
            raise ValueError('Нет уникального ID заказа Яндекс Маркета')
        seen.add(order_key)
        if order.get('campaignId') not in campaigns or order.get('fake') is True:
            raise ValueError('В ответе заказ другого магазина или тестовый заказ')
        try:
            created = datetime.fromisoformat(order['creationDate'].replace('Z', '+00:00'))
            if created.tzinfo is None or created.astimezone(ZoneInfo('Europe/Moscow')).date().isoformat() != day:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ValueError('Дата заказа не соответствует запрошенному дню') from None
        scheme = order.get('programType')
        if scheme not in ('FBY', 'FBS', 'DBS', 'EXPRESS', 'LAAS'):
            raise ValueError('Неизвестная модель работы Яндекс Маркета')
        status = order.get('status')
        if not isinstance(status, str) or not status:
            raise ValueError('Нет статуса заказа')
        item_ids = set()
        items = rows(order, 'items')
        if not items:
            raise ValueError('Заказ без позиций')
        for item in items:
            sku = item.get('offerId'); iid = item.get('id')
            if not isinstance(sku, str) or not sku.strip() or type(iid) is not int or iid in item_ids:
                raise ValueError('Некорректная позиция заказа Яндекс Маркета')
            item_ids.add(iid)
            count = quantity(item.get('count'))
            prices = item.get('prices')
            if not isinstance(prices, dict):
                raise ValueError('Нет цены позиции')
            # Dashboard order turnover includes compensation of Market-funded
            # discounts. All three fields are line totals, not unit prices.
            # Delivery, fees and other payout deductions are not included.
            amount = money(prices.get('payment'))
            for component in ('cashback', 'subsidy'):
                if prices.get(component) is not None:
                    amount += money(prices[component])
            cancelled = count if status == 'CANCELLED' else 0
            if item.get('itemStatuses') is not None:
                states = rows(item, 'itemStatuses'); seen_status = set(); total = 0; cancelled = 0
                for state in states:
                    name = state.get('status')
                    if name not in {'CREATED','SHIPPED','CANCELLED','DELIVERED_TO_BUYER','LOST','REJECTED','RETURNED'} or name in seen_status:
                        raise ValueError('Неизвестный или повторный статус единиц товара')
                    seen_status.add(name); units = quantity(state.get('count')); total += units
                    if name == 'CANCELLED': cancelled += units
                if total != count:
                    raise ValueError('Количество единиц не совпадает со статусами')
            obs = products.setdefault(sku, ProductDayObservation(day, sku, item.get('offerName') or sku, sku))
            obs.ordered_units += count
            obs.ordered_revenue += float(amount)
            obs.cancellations_units += cancelled
            obs.fulfillment_units[scheme] = obs.fulfillment_units.get(scheme, 0) + count
    observations = list(products.values())
    points = [MetricPoint(connection_id, day, metric, sum(getattr(x, metric) for x in observations), unit, True)
              for metric, unit in [('ordered_units','units'),('ordered_revenue','RUB'),('cancellations_units','units')]]
    return points, observations


def normalize_stocks(payload, scheme, names=None):
    if 'campaignSnapshots' in payload:
        # One physical warehouse can serve several models/campaigns. Count it once.
        unique = {}
        for snapshot in rows(payload, 'campaignSnapshots'):
            data = snapshot['data']
            for obs in normalize_stocks(data, data['scheme'], names):
                key = (obs.warehouse_name, obs.marketplace_sku)
                previous = unique.get(key)
                if previous and (previous.available_units, previous.reserved_units) != (obs.available_units, obs.reserved_units):
                    raise ValueError('Общий склад вернул разные остатки; повторите обновление')
                unique.setdefault(key, obs)
        return list(unique.values())
    out = []; seen = set(); names = names or {}
    for warehouse in rows(payload, 'warehouses'):
        wid = warehouse.get('warehouseId')
        if type(wid) is not int or wid <= 0:
            raise ValueError('Нет ID склада Яндекс Маркета')
        for offer in rows(warehouse, 'offers'):
            sku = offer.get('offerId')
            if not isinstance(sku, str) or not sku or (wid, sku) in seen:
                raise ValueError('Нет уникального SKU на складе')
            seen.add((wid, sku)); counts = {}
            for stock in rows(offer, 'stocks'):
                kind = stock.get('type')
                if kind not in {'AVAILABLE','FIT','FREEZE','QUARANTINE','UTILIZATION','DEFECT','EXPIRED'} or kind in counts:
                    raise ValueError('Некорректный тип остатка')
                counts[kind] = quantity(stock.get('count'))
            if 'AVAILABLE' in counts:
                available = counts['AVAILABLE']
            elif 'FIT' in counts and 'FREEZE' in counts:
                available = counts['FIT'] - counts['FREEZE']
            else:
                raise ValueError('Нет доступного остатка: нужен AVAILABLE либо FIT и FREEZE')
            if available < 0:
                raise ValueError('Отрицательный доступный остаток')
            reserve = counts.get('FREEZE', max(0, counts.get('FIT', available)-available))
            out.append(StockObservation(sku, names.get(sku,sku), available, reserve, scheme,
                str(wid), sku, offer.get('updatedAt')))
    return out


def normalize_cards(payload):
    if 'campaignSnapshots' in payload:
        issues = {}; candidates = None; uncertain = set()
        for snapshot in rows(payload, 'campaignSnapshots'):
            data = snapshot['data']; cid = snapshot['campaignId']
            found, healthy = normalize_cards(data)
            present = {'yandex_market:' + offer['offerId'] for offer in rows(data, 'offers')}
            candidates = set(healthy) if candidates is None else candidates & healthy
            uncertain.update(present - healthy)
            for subject, (rule, message) in found.items():
                message = f'Магазин {cid} · ' + message
                if subject in issues:
                    previous_rule, previous_message = issues[subject]
                    rule = 'card_blocked' if 'card_blocked' in (rule, previous_rule) else rule
                    message = previous_message + '\n' + message
                issues[subject] = (rule, message)
        return issues, (candidates or set()) - uncertain
    catalogue = {}
    for mapping in rows(payload, 'offerMappings'):
        offer = mapping.get('offer')
        if not isinstance(offer, dict) or not isinstance(offer.get('offerId'), str):
            raise ValueError('Нет SKU в каталоге Яндекс Маркета')
        if offer['offerId'] in catalogue:
            raise ValueError('Повтор SKU в каталоге')
        catalogue[offer['offerId']] = offer
    issues, healthy, seen = {}, set(), set()
    for offer in rows(payload, 'offers'):
        sku = offer.get('offerId'); status = offer.get('status')
        if not isinstance(sku, str) or not sku or sku in seen or sku not in catalogue:
            raise ValueError('Неполная карточка Яндекс Маркета')
        seen.add(sku); card = catalogue[sku]; card_status = card.get('cardStatus')
        errors = offer.get('errors') or []
        if not isinstance(errors, list) or any(not isinstance(e, dict) for e in errors):
            raise ValueError('Некорректные ошибки карточки')
        reason = '; '.join(sorted(set(str(e.get('comment') or e.get('message') or 'Ошибка размещения') for e in errors)))
        failed = card_status in {'HAS_CARD_CAN_UPDATE_ERRORS','NO_CARD_ERRORS','NO_CARD_NEED_CONTENT'}
        subject = 'yandex_market:' + sku
        if status == 'REJECTED_BY_MARKET' or status in {'DISABLED_AUTOMATICALLY','NO_CARD'} or errors or failed:
            rule = 'card_blocked' if status == 'REJECTED_BY_MARKET' else 'card_errors'
            label = 'отклонён Маркетом' if rule == 'card_blocked' else 'нужна доработка'
            issues[subject] = (rule, '🟡 Яндекс Маркет · артикул ' + sku + ': ' + label + '\nТовар: ' + str(card.get('name') or sku) + '\nПричина: ' + (reason or str(card_status or status)))
        elif status == 'PUBLISHED' and card_status in {'HAS_CARD_CAN_UPDATE','HAS_CARD_CAN_NOT_UPDATE'}:
            healthy.add(subject)
        # Unknown / pending / hidden / no stock never implies recovery.
    return issues, healthy


def normalize_finance(payload, connection_id, day):
    """Costs of the cohort of orders created on day, not a payout-day ledger."""
    if 'campaignSnapshots' in payload:
        totals = {}
        for snapshot in rows(payload, 'campaignSnapshots'):
            for point in normalize_finance(snapshot['data'], connection_id, day):
                totals[point.metric_key] = totals.get(point.metric_key, 0) + point.value
        return [MetricPoint(connection_id, day, key, value, 'RUB', True) for key, value in totals.items()]
    from .numeric import finite_number
    totals = {'ym_order_services': 0.0, 'ym_order_boost': 0.0}
    seen = set()
    for order in rows(payload, 'orders'):
        oid = order.get('id')
        if type(oid) is not int or oid in seen or str(order.get('creationDate',''))[:10] != day:
            raise ValueError('Некорректная дата или ID финансового заказа')
        seen.add(oid)
        if order.get('currency') not in ('RUR','RUB'):
            raise ValueError('Финансовый заказ не в рублях')
        for commission in rows(order, 'commissions'):
            kind = commission.get('type')
            if not isinstance(kind, str) or not kind:
                raise ValueError('Нет типа услуги')
            amount = finite_number(commission.get('actual'))
            totals['ym_order_services'] += amount
            if kind == 'AUCTION_PROMOTION':
                totals['ym_order_boost'] += amount
    return [MetricPoint(connection_id,day,key,value,'RUB',True) for key,value in totals.items()]


def normalize_promotions(payload):
    if 'campaignSnapshots' in payload:
        merged = {}
        for snapshot in rows(payload, 'campaignSnapshots'):
            for promo in normalize_promotions(snapshot['data']):
                pid = promo['external_promotion_id']
                if pid not in merged:
                    merged[pid] = promo
                else:
                    products = {p['marketplace_sku']: p for p in merged[pid]['products']}
                    for product in promo['products']:
                        sku = product['marketplace_sku']
                        if sku not in products or product['in_action']:
                            products[sku] = product
                    merged[pid]['products'] = list(products.values())
        return list(merged.values())
    out=[]; seen=set(); campaign=payload['campaignId']
    for promo in rows(payload,'promos'):
        pid=promo.get('id');period=promo.get('period')
        if not isinstance(pid,str) or not pid or pid in seen or not isinstance(period,dict):
            raise ValueError('Некорректная акция')
        seen.add(pid)
        for key in ('dateTimeFrom','dateTimeTo'):
            datetime.fromisoformat(period[key].replace('Z','+00:00'))
        products=[]
        for item in rows({'items':payload['products'].get(pid)},'items'):
            sku=item.get('offerId');state=item.get('status')
            if not isinstance(sku,str) or not sku:
                raise ValueError('Нет SKU акции')
            if state not in {'AUTO','PARTIALLY_AUTO','MANUAL','NOT_PARTICIPATING','RENEWED','RENEW_FAILED','MINIMUM_FOR_PROMOS'}:
                raise ValueError('Неизвестное участие в акции')
            participates=state in {'AUTO','MANUAL','RENEWED','MINIMUM_FOR_PROMOS'}
            if state=='PARTIALLY_AUTO':
                ids=(item.get('autoParticipatingDetails') or {}).get('campaignIds')
                if not isinstance(ids,list):raise ValueError('Нет магазинов акции')
                participates=campaign in ids
            products.append({'marketplace_sku':sku,'in_action':participates,'metadata':item})
        out.append({'external_promotion_id':pid,'name':promo.get('name') or pid,
            'promo_type':str((promo.get('mechanicsInfo') or {}).get('type') or ''),
            'start_at':period['dateTimeFrom'],'end_at':period['dateTimeTo'],'metadata':promo,
            'products':products,'products_complete':True})
    return out


def normalize_inbound(payload):
    if 'campaignSnapshots' in payload:
        combined = []
        for snapshot in rows(payload, 'campaignSnapshots'):
            for shipment in normalize_inbound(snapshot['data']):
                shipment['external_supply_id'] = str(snapshot['campaignId']) + ':' + shipment['external_supply_id']
                combined.append(shipment)
        return combined
    out=[];seen=set()
    for request in rows(payload,'requests'):
        rid=str(request['id']['id'])
        if rid in seen or request.get('type')!='SUPPLY':raise ValueError('Некорректная поставка')
        seen.add(rid);items=[]
        status=request.get('status')
        if not isinstance(status,str) or not status:raise ValueError('Нет статуса поставки')
        for item in rows({'items':payload['items'].get(rid)},'items'):
            sku=item.get('offerId');counts=item.get('counters')
            if not isinstance(sku,str) or not sku or not isinstance(counts,dict):raise ValueError('Нет количества в поставке')
            planned=quantity(counts.get('planCount'));accepted=quantity(counts.get('factCount'))
            # Parent virtual/cross-dock requests must never duplicate child stock.
            remaining=max(0,planned-accepted) if not request.get('childrenLinks') and status not in {'FINISHED','CANCELLED','INVALID'} else 0
            items.append({'marketplace_sku':sku,'planned_units':planned,'accepted_units':accepted,'remaining_units':remaining})
        target=request.get('targetLocation') or {}
        out.append({'external_supply_id':rid,'status':status,'planned_at':target.get('requestedDate'),
            'arrival_at':None,'warehouse_name':target.get('name') or '', 'metadata':request,'items':items})
    return out
