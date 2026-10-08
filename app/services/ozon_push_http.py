"""Optional HTTPS-proxy receiver in the bot process; Telegram stays on polling."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3

from aiohttp import web

from app.services.health import build_health, public_health_report
from app.services.ozon_push_payload import MAX_BODY_BYTES, PushError, base_url, parse_notification
from app.storage.ozon_push import OzonPushStore

log = logging.getLogger(__name__)


def error_response(message: str, status: int):
    return web.json_response({'error':{'code':'ERROR_PARAMETER_VALUE_MISSED' if status==400 else 'ERROR_UNKNOWN',
                                     'message':message,'details':None}}, status=status)


def create_push_app(registry) -> web.Application:
    if not registry.settings.ozon_push_enabled:
        raise ValueError('OZON_PUSH_ENABLED is false')
    base_url(registry.settings.ozon_push_base_url)
    store = OzonPushStore(registry.repository.db)
    version_path = Path(__file__).resolve().parents[2] / 'VERSION'
    version = version_path.read_text().strip() if version_path.exists() else 'unknown'

    async def health(request):
        report = build_health(registry, include_runtime_details=False)
        return web.json_response(public_health_report(report),
            status=503 if request.path=='/ready' and not report['ready'] else 200)

    async def receive(request):
        started = datetime.now(timezone.utc).isoformat(timespec='microseconds')
        try:
            connection_id = int(request.match_info['connection_id'])
            if not 0 < connection_id < 2**63:
                raise ValueError
        except ValueError:
            return error_response('Адрес подключения не найден.',404)
        try:
            raw = await asyncio.wait_for(request.read(),timeout=2)
            # Reject non-standard NaN/Infinity and duplicate JSON keys: either
            # can otherwise make the archived body disagree with the count.
            def unique_object(pairs):
                result = {}
                for key,value in pairs:
                    if key in result:
                        raise ValueError('duplicate key')
                    result[key] = value
                return result
            def bad_constant(value):
                raise ValueError('non-finite JSON')
            payload_text = raw.decode('utf-8')
            payload = json.loads(payload_text,object_pairs_hook=unique_object,parse_constant=bad_constant)
            if not isinstance(payload,dict):
                raise ValueError
        except web.HTTPRequestEntityTooLarge:
            return error_response('Уведомление превышает допустимый размер.',413)
        except (ValueError,UnicodeDecodeError,RecursionError):
            return error_response('Неверный JSON уведомления.',400)
        except asyncio.TimeoutError:
            return error_response('Не удалось прочитать уведомление вовремя.',408)
        if registry.maintenance_lock.locked():
            return error_response('База временно обслуживается; повторите уведомление.',503)
        try:
            async with registry.maintenance_lock:
                connection = await asyncio.to_thread(store.connection,connection_id)
                if not connection or not connection['active'] or not connection['enabled'] or connection['marketplace']!='ozon':
                    raise PushError('Адрес подключения не найден.',404)
                if not any(ctx.shop_id==connection['shop_id'] and ctx.ozon_connection_id==connection_id
                           for ctx in registry.contexts()):
                    # A lower edition may retain other shops in the database;
                    # their old URLs must not bypass its runtime restrictions.
                    raise PushError('Магазин не запущен в этой сборке.',404)
                creds = registry.settings.credentials_for_profile(connection['credential_profile'])
                if not creds.has_ozon:
                    raise PushError('Кабинет Ozon не подключён.',404)
                parsed = None
                validation_error = None
                try:
                    parsed = parse_notification(payload)
                except PushError as exc:
                    validation_error = str(exc)
                status = await asyncio.to_thread(store.receive,connection_id,request.match_info['token'],
                    creds.ozon_client_id,creds.profile,payload,parsed,validation_error=validation_error,payload_text=payload_text)
            if status == 'invalid':
                return error_response(validation_error or 'Неверные поля уведомления.',400)
            # A duplicate/conflict is already durably saved; retries cannot
            # resolve a semantic conflict. Ack without counting it again.
            return web.json_response({'version':version,'name':'seller_analytics_bot','time':started})
        except PushError as exc:
            return error_response(str(exc),exc.status)
        except (sqlite3.Error,OSError):
            # Never log the request URL, body, token or API credentials.
            log.warning('Ozon push storage temporarily unavailable connection=%s',connection_id)
            return error_response('Не удалось сохранить уведомление; повторите запрос.',503)
        except Exception as exc:
            log.error('Ozon push processing failed connection=%s error=%s',connection_id,type(exc).__name__)
            return error_response('Не удалось обработать уведомление.',500)

    app = web.Application(client_max_size=MAX_BODY_BYTES)
    app.router.add_get('/health',health)
    app.router.add_get('/ready',health)
    app.router.add_post('/ozon/push/{connection_id}/{token}',receive)
    return app


async def ozon_push_http_server(registry):
    runner = web.AppRunner(create_push_app(registry),access_log=None)
    try:
        await runner.setup()
        site = web.TCPSite(runner,registry.settings.health_host,registry.settings.health_port)
        await site.start()
        log.info('Ozon push HTTP server listening on %s:%s',registry.settings.health_host,registry.settings.health_port)
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
