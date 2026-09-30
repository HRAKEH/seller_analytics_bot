"""Shop onboarding/readiness checks.

Critical checks are deliberately separate from optional capabilities.  A shop is
"ready" only when basic configuration and at least one marketplace connection
can be used.  Advertising/cost coverage/history improve usefulness but do not
pretend to be hard blockers for the bot itself.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from html import escape
from typing import Any

from app.storage import Repository
from app.integrations.wildberries import decode_wb_token, WB_BOT_REQUIRED_CATEGORIES


@dataclass(frozen=True)
class ReadinessItem:
    key: str
    label: str
    ok: bool
    critical: bool = False
    detail: str = ''


@dataclass(frozen=True)
class ReadinessReport:
    shop_id: int
    live: bool
    status: str
    items: tuple[ReadinessItem, ...]

    @property
    def critical_ok(self) -> int: return sum(1 for x in self.items if x.critical and x.ok)
    @property
    def critical_total(self) -> int: return sum(1 for x in self.items if x.critical)
    @property
    def optional_ok(self) -> int: return sum(1 for x in self.items if not x.critical and x.ok)
    @property
    def optional_total(self) -> int: return sum(1 for x in self.items if not x.critical)


def _ozon_method_strings(value: Any) -> set[str]:
    out:set[str]=set()
    if isinstance(value, dict):
        for k,v in value.items():
            if isinstance(v,str) and ('/' in v or v.upper() in {'GET','POST','PUT','DELETE','PATCH'}): out.add(v)
            out |= _ozon_method_strings(v)
    elif isinstance(value,list):
        for x in value: out |= _ozon_method_strings(x)
    elif isinstance(value,str) and '/' in value:
        out.add(value)
    return out


def _expiry_hint(body: Any) -> str:
    if not isinstance(body,dict): return ''
    raw=body.get('expires_at') or body.get('expire_at') or body.get('expiration_date')
    if not raw: return ''
    return f' · срок ключа: {raw}'


async def build_readiness(ctx, *, live: bool = False, persist: bool = True) -> ReadinessReport:
    repo: Repository=ctx.repository
    p=ctx.preferences(); shop=repo.get_shop(ctx.shop_id)
    items:list[ReadinessItem]=[]
    items.append(ReadinessItem('setup','Первичная настройка',bool(p.setup_completed),True,
                               'завершена' if p.setup_completed else 'запустите «🧩 Мастер настройки»'))
    items.append(ReadinessItem('timezone','Часовой пояс и расписание',bool(p.timezone and p.report_time),True,
                               f'{p.timezone} · {p.report_time}'))

    if p.demo_mode:
        items.append(ReadinessItem('demo','Демо-режим',True,True,'синтетические данные; внешние API не вызываются'))
        items.append(ReadinessItem('sources','Источники данных',True,True,'демо WB + Ozon'))
    else:
        configured=ctx.configured_sources()
        items.append(ReadinessItem('sources','Хотя бы один маркетплейс',configured>0,True,
                                   f'подключено: {configured}' if configured else 'задайте профиль API-ключей в окружении'))

    # Existing data/history check is useful even without live network probes.
    with repo.db.connect() as c:
        products=int(c.execute('SELECT COUNT(*) FROM products WHERE shop_id=? AND active=1',(ctx.shop_id,)).fetchone()[0])
        days=int(c.execute("""SELECT COUNT(DISTINCT mv.data_date) FROM metric_values mv
            JOIN marketplace_connections mc ON mc.id=mv.connection_id
            WHERE mc.shop_id=? AND mv.metric_key='ordered_units'""",(ctx.shop_id,)).fetchone()[0])
    missing_cost=len(repo.products_without_cost(ctx.shop_id,limit=10000))
    items.append(ReadinessItem('history','История заказов',days>=7,False,f'{days} дней; желательно ≥7'))
    items.append(ReadinessItem('products','Товарная база',products>0,False,f'{products} активных товаров'))
    items.append(ReadinessItem('costs','Себестоимость',products>0 and missing_cost==0,False,
                               'покрыта' if products and missing_cost==0 else f'без себестоимости: {missing_cost}'))

    if live and not p.demo_mode:
        if ctx.collector.wb is not None:
            wb=ctx.collector.wb
            info=await wb.seller_info()
            name=''
            if info.ok and isinstance(info.data,dict): name=str(info.data.get('name') or info.data.get('tradeMark') or '')
            items.append(ReadinessItem('wb_auth','WB · токен',info.ok,True,
                                       ('кабинет: '+name) if info.ok and name else (info.error or 'OK')))

            token_meta=decode_wb_token(wb.token)
            if token_meta['ok']:
                type_name=str(token_meta['type'])
                type_ok=token_meta['type_code'] in {3,4} and token_meta['expired'] is not True
                access='RO' if token_meta['read_only'] else 'RW'
                expiry=''
                if token_meta['expires_at']:
                    expiry=' · истёк' if token_meta['expired'] else f" · до {str(token_meta['expires_at'])[:10]}"
                type_detail=f'{type_name} · {access}{expiry}'
                if token_meta['type_code']==1:
                    type_detail += ' · для полного набора функций лучше Personal/Service'
                elif token_meta['type_code']==2:
                    type_detail += ' · тестовый токен не работает с боевыми данными'
                items.append(ReadinessItem('wb_token_type','WB · тип токена',type_ok,False,type_detail))

                categories=set(token_meta['categories'])
                missing=[x for x in WB_BOT_REQUIRED_CATEGORIES if x not in categories]
                items.append(ReadinessItem(
                    'wb_categories','WB · категории для функций бота',not missing and token_meta['expired'] is not True,False,
                    'все 6 категорий доступны' if not missing else 'не хватает: '+', '.join(missing)))
            else:
                categories=None
                items.append(ReadinessItem(
                    'wb_token_type','WB · тип токена',False,False,
                    'JWT не распознан; фактический доступ проверяется API'))

            domains=[
                ('wb_statistics','WB · Статистика','https://statistics-api.wildberries.ru','Статистика'),
                ('wb_analytics','WB · Аналитика','https://seller-analytics-api.wildberries.ru','Аналитика'),
                ('wb_finance','WB · Финансы','https://finance-api.wildberries.ru','Финансы'),
                ('wb_ads','WB · Продвижение','https://advert-api.wildberries.ru','Продвижение'),
                ('wb_supplies','WB · Поставки','https://supplies-api.wildberries.ru','Поставки'),
                ('wb_prices','WB · Цены и скидки','https://discounts-prices-api.wildberries.ru','Цены и скидки'),
            ]
            auth_blocked=(not info.ok and info.status_code in {401,403})
            rate_limited=(not info.ok and info.status_code==429)
            for key,label,url,category in domains:
                if auth_blocked:
                    items.append(ReadinessItem(key,label,False,False,'WB-токен не авторизован'))
                    continue
                if rate_limited:
                    items.append(ReadinessItem(key,label,False,False,'WB временно ограничил запросы; повторите проверку позже'))
                    continue
                if token_meta['ok'] and token_meta['expired'] is True:
                    items.append(ReadinessItem(key,label,False,False,'срок WB-токена истёк'))
                    continue
                if categories is not None and category not in categories:
                    items.append(ReadinessItem(key,label,False,False,'категория отсутствует в токене'))
                    continue
                r=await wb.ping(url)
                items.append(ReadinessItem(key,label,r.ok,False,'доступ есть' if r.ok else (r.error or f'HTTP {r.status_code}')))

            if token_meta['ok']:
                stock_type_ok=token_meta['type_code'] in {3,4} and token_meta['expired'] is not True
                analytics_ok='Аналитика' in set(token_meta['categories'])
                items.append(ReadinessItem(
                    'wb_stock_capability','WB · текущие остатки FBW/FBS',
                    stock_type_ok and analytics_ok,False,
                    'тип токена и категория подходят' if stock_type_ok and analytics_ok
                    else 'нужен Personal/Service + категория Аналитика'))
        if ctx.collector.ozon is not None:
            oz=ctx.collector.ozon
            info=await oz.seller_info(); company=''
            if info.ok and isinstance(info.data,dict):
                company=str((info.data.get('company') or {}).get('name') or (info.data.get('company') or {}).get('legal_name') or '')
            items.append(ReadinessItem('ozon_auth','Ozon · кабинет',info.ok,True,
                                       ('кабинет: '+company) if info.ok and company else (info.error or 'OK')))
            roles=await oz.api_roles(); methods=_ozon_method_strings(roles.data) if roles.ok else set()
            detail=(f'{len(methods)} методов доступны'+_expiry_hint(roles.data)) if roles.ok else (roles.error or 'ошибка ролей')
            items.append(ReadinessItem('ozon_roles','Ozon · права API-ключа',roles.ok,True,detail))
        if ctx.collector.ozon_performance is not None:
            token=await ctx.collector.ozon_performance._token()
            items.append(ReadinessItem('ozon_ads','Ozon Performance',token.ok,False,
                                       'авторизация успешна' if token.ok else (token.error or 'ошибка')))

    critical=[x for x in items if x.critical]
    status='ready' if critical and all(x.ok for x in critical) else ('partial' if any(x.ok for x in critical) else 'blocked')
    report=ReadinessReport(ctx.shop_id,live,status,tuple(items))
    if persist:
        repo.save_readiness_snapshot(ctx.shop_id,status,report.critical_ok,report.critical_total,
                                     report.optional_ok,report.optional_total,
                                     [{'key':x.key,'label':x.label,'ok':x.ok,'critical':x.critical,'detail':x.detail} for x in items])
    return report


def format_readiness(report: ReadinessReport) -> str:
    icon={'ready':'✅','partial':'⚠️','blocked':'⛔'}[report.status]
    lines=[f'{icon} <b>Готовность магазина</b>','━━━━━━━━━━━━━━━━',
           f'Критичные: <b>{report.critical_ok}/{report.critical_total}</b> · Дополнительно: {report.optional_ok}/{report.optional_total}',
           f'Проверка API: {"живая" if report.live else "локальная"}','']
    for item in report.items:
        mark='✅' if item.ok else ('❌' if item.critical else '▫️')
        suffix=f' — {escape(item.detail)}' if item.detail else ''
        lines.append(f'{mark} <b>{escape(item.label)}</b>{suffix}')
    if report.status!='ready':
        lines += ['','💡 Исправьте пункты с ❌ и повторите «🔌 Проверить подключения».']
    return '\n'.join(lines)


def format_onboarding_help(profile: str='DEFAULT') -> str:
    profile=(profile or 'DEFAULT').upper()
    prefix='' if profile=='DEFAULT' else f'SELLERBOT_{profile}_'
    def env(name): return name if profile=='DEFAULT' else prefix+name
    return '\n'.join([
        '🧭 <b>Подключение нового магазина</b>','━━━━━━━━━━━━━━━━',
        '1️⃣ Пройдите «🧩 Мастер настройки».',
        '2️⃣ В Bothost/VPS задайте нужные секреты окружения:',
        f'• <code>{env("WB_API_TOKEN")}</code> — для полного WB: Personal, только чтение',
        '  категории WB: Статистика, Аналитика, Финансы, Продвижение, Поставки, Цены и скидки',
        f'• <code>{env("OZON_CLIENT_ID")}</code>',
        f'• <code>{env("OZON_API_KEY")}</code>',
        f'• <code>{env("OZON_PERF_CLIENT_ID")}</code> — реклама Ozon, необязательно',
        f'• <code>{env("OZON_PERF_CLIENT_SECRET")}</code> — реклама Ozon, необязательно',
        '3️⃣ Перезапустите приложение после изменения окружения.',
        '4️⃣ Нажмите «🔌 Проверить подключения».',
        '5️⃣ Нажмите «📥 Загрузить историю».',
        '6️⃣ Импортируйте себестоимость для управленческой экономики.',
        '',
        '🔐 Никогда не отправляйте API-токены сообщением в Telegram. Бот их не запрашивает и не хранит в SQLite.'
    ])
