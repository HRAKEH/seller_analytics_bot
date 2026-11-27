"""Market cancellation notifications; durable inbox and separate API verification."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hmac
import json
import logging
import re
from zoneinfo import ZoneInfo
from aiohttp import web
from app.services.ozon_push_payload import canonical, digest, positive_int, stamp, PushError

log=logging.getLogger(__name__)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def validate_secret(secret):
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}',secret):
        raise ValueError('YANDEX_PUSH_SECRET: required 32–128 URL-safe random characters')


def scope(creds):
    return digest(canonical([int(creds.yandex_market_business_id),sorted(creds.yandex_market_campaign_ids)]))


def sync_scope(db,connection_id,creds):
    with db.session() as c:
        if creds is None:
            c.execute('DELETE FROM ym_push_scope WHERE connection_id=?',(connection_id,))
        else:
            c.execute("""INSERT INTO ym_push_scope VALUES(?,?) ON CONFLICT(connection_id)
                DO UPDATE SET account_hash=excluded.account_hash""",(connection_id,scope(creds)))


def parse_cancel(payload):
    if not isinstance(payload,dict) or payload.get('notificationType')!='ORDER_CANCELLED':
        raise PushError('Не уведомление об отмене заказа.')
    cid=positive_int(payload.get('campaignId'),'campaignId')
    oid=positive_int(payload.get('orderId'),'orderId')
    when=stamp(payload.get('cancelledAt'),'cancelledAt')
    if datetime.fromisoformat(when)>datetime.now(timezone.utc)+timedelta(minutes=5):
        raise PushError('Дата отмены в будущем.')
    items=payload.get('items')
    if not isinstance(items,list) or not 1<=len(items)<=1000:
        raise PushError('Нет состава заказа.')
    counts={}
    for item in items:
        if not isinstance(item,dict):
            raise PushError('Некорректная позиция.')
        sku=item.get('offerId')
        if not isinstance(sku,str) or not sku.strip() or len(sku)>512:
            raise PushError('Некорректный offerId.')
        count=positive_int(item.get('count'),'count')
        counts[sku]=counts.get(sku,0)+count
    units=sum(counts.values())
    if units>=2**63:
        raise PushError('Некорректное количество.')
    return cid,oid,when,datetime.fromisoformat(when).astimezone(ZoneInfo('Europe/Moscow')).date().isoformat(),canonical(counts),units


def save_cancel(db,connection_id,account,event):
    cid,oid,when,day,items,units=event
    with db.session() as c:
        c.execute('PRAGMA busy_timeout=1000')
        c.execute('BEGIN IMMEDIATE')
        active=c.execute("""SELECT 1 FROM ym_push_scope a JOIN marketplace_connections mc ON mc.id=a.connection_id
            JOIN shops s ON s.id=mc.shop_id WHERE a.connection_id=? AND a.account_hash=?
            AND mc.marketplace='yandex_market' AND mc.enabled=1 AND s.active=1""",(connection_id,account)).fetchone()
        if not active: raise PushError('Кабинет изменился.',404)
        key=(connection_id,account,cid,oid)
        old=c.execute("""SELECT cancelled_at,items_json FROM ym_push_cancellations
            WHERE connection_id=? AND account_hash=? AND campaign_id=? AND order_id=?""",key).fetchone()
        if old:
            if old['cancelled_at']!=when or old['items_json']!=items:
                c.execute("""UPDATE ym_push_cancellations SET status='conflict'
                    WHERE connection_id=? AND account_hash=? AND campaign_id=? AND order_id=?""",key)
            return
        c.execute("""INSERT INTO ym_push_cancellations(connection_id,account_hash,campaign_id,order_id,
            event_day,cancelled_at,items_json,units,received_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (*key,day,when,items,units,utc_now()))


def cancellation_day(db,connection_id,day):
    with db.session() as c:
        r=c.execute("""SELECT COUNT(*) n,COALESCE(SUM(CASE WHEN p.status='confirmed' THEN p.units ELSE 0 END),0) units,
            SUM(CASE WHEN p.status IN ('pending','conflict') THEN 1 ELSE 0 END) issues,
            MAX(p.verified_at) freshness FROM ym_push_cancellations p
            JOIN ym_push_scope a ON a.connection_id=p.connection_id AND a.account_hash=p.account_hash
            JOIN marketplace_connections mc ON mc.id=a.connection_id JOIN shops s ON s.id=mc.shop_id
            WHERE p.connection_id=? AND p.event_day=? AND s.active=1 AND mc.enabled=1""",(connection_id,day)).fetchone()
        return dict(r) if r and r['n'] else None


def current_contexts(registry):
    for ctx in registry.contexts():
        connection_id=ctx.yandex_market_connection_id
        client=ctx.collector.yandex_market
        if not connection_id or client is None:
            continue
        shop=registry.repository.get_shop(ctx.shop_id)
        if not shop or not shop.active:
            continue
        creds=registry.settings.credentials_for_profile(shop.credential_profile)
        if creds.has_yandex_market:
            yield ctx,client,creds


def register_route(app,registry):
    from app.services.ozon_push_http import storage_call

    def error(message,status=400):
        return web.json_response({'error':{'type':'WRONG_EVENT_FORMAT' if status==400 else 'UNKNOWN','message':message}},status=status)

    async def receive(request):
        started=utc_now()
        token=request.match_info['token']
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}',token) or not hmac.compare_digest(token,registry.settings.yandex_push_secret):
            return error('Not found',404)
        try:
            raw=await asyncio.wait_for(request.read(),timeout=2)
            def unique(pairs):
                result={}
                for k,v in pairs:
                    if k in result: raise ValueError('duplicate JSON key')
                    result[k]=v
                return result
            def bad(v): raise ValueError('non-finite JSON')
            payload=json.loads(raw,object_pairs_hook=unique,parse_constant=bad)
            if not isinstance(payload,dict): raise ValueError('JSON object required')
            kind=payload.get('notificationType')
            if kind=='PING':
                stamp(payload.get('time'),'time')
            elif kind=='ORDER_CANCELLED':
                event=parse_cancel(payload)
                if registry.maintenance_lock.locked(): return error('Maintenance; retry',500)
                async with registry.maintenance_lock:
                    matches=[(ctx,creds) for ctx,client,creds in current_contexts(registry)
                             if event[0] in creds.yandex_market_campaign_ids]
                    if len(matches)>1: return error('Campaign configured more than once',500)
                    if matches:
                        ctx,creds=matches[0]
                        await storage_call(save_cancel,registry.repository.db,ctx.yandex_market_connection_id,scope(creds),event)
                    # Cabinet-wide notifications may include campaigns not connected to this bot.
            else:
                return error('Unsupported notification type')
            return web.json_response({'version':'1.0.0','name':'seller_analytics_bot','time':started})
        except (ValueError,UnicodeDecodeError,RecursionError,web.HTTPRequestEntityTooLarge):
            return error('Invalid notification')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('Yandex notification failed: %s',type(exc).__name__)
            return error('Temporary error; retry',500)

    app.router.add_post('/yandex/push/{token}/notification',receive)


def pending(db,connection_id,account):
    with db.session() as c:
        return [dict(r) for r in c.execute("""SELECT * FROM ym_push_cancellations
            WHERE connection_id=? AND account_hash=? AND status='pending' AND retry_after<=?
            ORDER BY retry_after,received_at LIMIT 50""",(connection_id,account,utc_now()))]


def verify_order(order,row):
    if order.get('orderId')!=row['order_id'] or order.get('campaignId')!=row['campaign_id']:
        return False
    if order.get('fake') is not False or order.get('status')!='CANCELLED':
        return False
    counts={}
    try:
        for item in order['items']:
            sku=item['offerId']; count=positive_int(item['count'],'count')
            counts[sku]=counts.get(sku,0)+count
        created=stamp(order.get('creationDate'),'creationDate')
        if created>row['cancelled_at']: return False
    except (ValueError,KeyError,TypeError): return False
    return canonical(counts)==row['items_json']


def finish(db,row,confirmed):
    delay=min(3600,60*2**min(row['attempts'],6))
    retry=(datetime.now(timezone.utc)+timedelta(seconds=delay)).isoformat(timespec='microseconds')
    with db.session() as c:
        c.execute("""UPDATE ym_push_cancellations SET status=?,attempts=attempts+1,retry_after=?,verified_at=?
            WHERE connection_id=? AND account_hash=? AND campaign_id=? AND order_id=? AND status='pending'
            AND cancelled_at=? AND items_json=? AND EXISTS(SELECT 1 FROM ym_push_scope a
            WHERE a.connection_id=ym_push_cancellations.connection_id AND a.account_hash=ym_push_cancellations.account_hash)""",
            ('confirmed' if confirmed else 'pending',retry,utc_now() if confirmed else None,
             row['connection_id'],row['account_hash'],row['campaign_id'],row['order_id'],row['cancelled_at'],row['items_json']))


async def verification_once(registry):
    from app.services.ozon_push_http import storage_call
    db=registry.repository.db
    for ctx,client,creds in list(current_contexts(registry)):
        async with registry.maintenance_lock:
            rows=await storage_call(pending,db,ctx.yandex_market_connection_id,scope(creds))
        clients=getattr(client,'clients',[client])
        for sub in clients:
            batch=[r for r in rows if r['campaign_id']==sub.campaign_id]
            if not batch: continue
            result=await sub._pages(f'/v1/businesses/{sub.business_id}/orders','orders',nested=False,
                body={'campaignIds':[sub.campaign_id],'orderIds':[r['order_id'] for r in batch],'fake':False})
            orders=result.data['orders'] if result.ok else []
            for row in batch:
                matched=[o for o in orders if o.get('orderId')==row['order_id']]
                confirmed=len(matched)==1 and verify_order(matched[0],row)
                async with registry.maintenance_lock:
                    await storage_call(finish,db,row,confirmed)


async def verification_loop(registry):
    while True:
        try:
            await verification_once(registry)
        except asyncio.CancelledError: raise
        except Exception as exc: log.warning('Yandex cancellation verification: %s',type(exc).__name__)
        await asyncio.sleep(60)
