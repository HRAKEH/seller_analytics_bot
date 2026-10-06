"""Daily cards: order totals use displayed prices; accruals stay separate."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from html import escape
import json

from .daily import DailyReport
from .formatter import num, source_name
from app.services.ozon_buyouts import format_buyout_check
from .text import paginate_report_html, utf16_length
from .dates import display_time
from app.services.daily_events import DayEvent, build_daily_events


@dataclass(frozen=True)
class DailyCardText:
    summary_html: str
    details_html: str
    accruals_html: str


def rubles(value, *, precision: int = 2) -> str:
    amount=Decimal(str(value)).quantize(Decimal(1).scaleb(-precision),rounding=ROUND_HALF_UP)
    return f'{amount:,.{precision}f}'.replace(',', ' ').replace('.', ',')+' ₽'


def _safe(value, limit: int = 150) -> str:
    text=str(value)
    return escape(text[:limit]+('…' if len(text)>limit else ''))


def _day(value: str) -> str:
    return date.fromisoformat(value).strftime('%d.%m.%Y')


def _event_line(label: str, event: DayEvent) -> str:
    if event.units is None:
        if event.unavailable_note:
            return label+': '+_safe(event.unavailable_note,120)
        status = ('⏳ неполные данные' if event.available_units else
                  '⏳ источник недоступен' if event.warning and 'доступ' in event.warning else
                  '⏳ нет подтверждённых данных' if event.warning else '⏳ данные не загружены')
        return label + ': ' + status
    text = label + f': <b>{event.units} шт.'
    if event.amount is not None:
        text += ' · ' + rubles(event.amount)
    elif not label.startswith('❌'):
        text += ' · ⏳ сумма не подтверждена'
    text += '</b>'
    if event.load_failed:
        text += ' · ⚠️ сохранённые данные'
    return text


def _event_details(label: str, event: DayEvent, *, tz: str) -> list[str]:
    lines = [label + ': ' + _safe(event.source, 160) + '.']
    if event.units is None and event.available_units:
        lines.append(f'Подтверждено в доступной части: {event.available_units} шт.; это неполный итог.')
    if event.price_note:
        lines.append(_safe(event.price_note, 240))
    if event.freshness:
        lines.append('Проверено: ' + display_time(event.freshness, tz=tz))
    if event.warning:
        lines.append('⚠️ ' + _safe(event.warning, 450))
    return lines


def _orders_total(report: DailyReport) -> tuple[Decimal | None, bool]:
    """Sum the visible marketplace amounts, never substitute net/max prices."""
    if not report.complete:return None,False
    total=Decimal(0);approximate=False
    for source in report.sources:
        if source.marketplace=='ozon':
            prices=source.buyer_prices
            amount=prices.rub_total if prices else None
            if amount is None:return None,False
            approximate=approximate or bool(prices.foreign_currencies)
        elif source.marketplace=='wildberries' and source.ordered_revenue is not None:
            amount=Decimal(str(source.ordered_revenue))
            if not amount.is_finite():return None,False
            amount=amount.quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)
        else:return None,False
        total+=amount
    return total,approximate


def _wb_period(repo, metric) -> str:
    """A report's actual period can differ from its storage/end date."""
    raw=repo.raw_payload_for_run(metric['source_run_id'])
    try:
        payload=json.loads(raw) if raw else {}
        rows=payload.get('reports',[]) if isinstance(payload,dict) else []
        periods=set()
        for row in rows:
            if not isinstance(row,dict):continue
            start=date.fromisoformat(str(row.get('dateFrom',''))[:10])
            end=date.fromisoformat(str(row.get('dateTo',''))[:10])
            if end<start:raise ValueError('invalid report period')
            periods.add((_day(start.isoformat()),_day(end.isoformat())))
        if periods:
            return 'Период фин. отчёта: '+', '.join(
                start if start==end else start+' — '+end for start,end in sorted(periods))
    except (ValueError,TypeError,KeyError):
        pass
    return 'Период фин. отчёта не указан в сохранённых данных; дата записи: '+_day(metric['data_date'])+'.'


def format_daily_card(repo, shop_id: int, report: DailyReport) -> DailyCardText:
    shop=repo.get_shop(shop_id)
    pref=repo.get_shop_preferences(shop_id)
    summary=[f'📊 <b>Отчёт за {_day(report.day)}</b>']
    if shop:summary.append('🏪 '+_safe(shop.name,80))
    if pref and pref.demo_mode:summary.append('🧪 <b>Учебные данные · DEMO</b>')
    summary.append('')
    total,approximate=_orders_total(report)
    if report.total_units is None:
        summary.append('Всего заказано: ⏳ неполные данные.')
    elif total is None:
        summary.append(f'Всего заказано: <b>{num(report.total_units)} шт.</b> · ⏳ сумма неполная.')
    else:
        prefix='≈ ' if approximate else ''
        summary.append(f'Всего заказано: <b>{num(report.total_units)} шт. · {prefix}{rubles(total)}</b>')
    details=['🔎 <b>Подробнее</b>']
    accruals=['💰 <b>Начисления</b>',
              'Финансовые операции и заказы относятся к разным датам.']
    sources=sorted(report.sources,key=lambda s: (s.marketplace!='ozon',s.connection_id))
    event_freshness=[]
    event_load_failed=False
    tz=pref.timezone if pref else 'Europe/Moscow'
    for s in sources:
        label=source_name(s.marketplace)
        summary.extend(['',label])
        details.extend(['','<b>'+label+'</b>'])
        accruals.extend(['','<b>'+label+'</b>'])
        units=num(s.units)+' шт.' if s.units is not None else '⏳ количество не загружено'
        if s.marketplace=='ozon':
            prices=s.buyer_prices
            converted=prices.rub_total if prices else None
            if s.units is not None and converted is not None:
                approximate='≈ ' if prices.foreign_currencies else ''
                summary.append(f'🛒 Заказано: <b>{units} · {approximate}{rubles(converted)}</b>')
                currencies=', '.join(prices.foreign_currencies)
                details.append(f'Заказы по цене покупателя, с пересчётом {currencies} по ЦБ.' if currencies
                               else 'Заказы по цене покупателя.')
            else:
                summary.append('🛒 Заказано: <b>'+units+'</b> · ⏳ сумма неполная.')
                details.append('По цене покупателя: ⏳ цены пока неполные.' if not prices or not prices.complete
                               else 'По цене покупателя: ⏳ нет полного итога в рублях.')
            details.append('Источник заказов: аналитика Ozon.')
            if s.ordered_revenue is not None:
                details.append('По предельной цене API: '+rubles(s.ordered_revenue)+'.')
                details.append('Эта сумма отличается от цены покупателя.')
            if prices:
                for total in prices.totals:
                    amount=rubles(total.amount) if total.currency=='RUB' else (
                        f'{total.amount:,.2f}'.replace(',', ' ').replace('.', ',')+' '+_safe(total.currency))
                    details.append('Цена покупателя: '+amount+' · '+str(total.units)+' шт.')
                expected=prices.expected_units if prices.expected_units is not None else '—'
                details.append(f'Цены получены: {prices.priced_units} шт.; в аналитике: {expected} шт.')
                if not prices.complete:details.append('⚠️ Покрытие цен неполное; рублёвый итог не рассчитан.')
                if prices.complete and prices.foreign_currencies:
                    if converted is None:
                        details.append('⏳ Нет курса ЦБ для '+_safe(', '.join(prices.missing_rates))+'.')
                    else:
                        for currency in prices.foreign_currencies:
                            rate=prices.rates.rate(currency)
                            rate_text=format(rate,'f').rstrip('0').rstrip('.') if '.' in format(rate,'f') else format(rate,'f')
                            details.append('Курс ЦБ: 1 '+currency+' = '+rate_text.replace('.', ',')+' ₽.')
                        details.append('Курс для '+_day(prices.rates.requested_date)+
                                       ', действует с '+_day(prices.rates.effective_date)+'.')
                        details.append('Пересчёт по ЦБ — оценка; курс Ozon может отличаться.')
                for warning in prices.warnings:
                    details.append('⚠️ '+_safe(warning,200))
                if prices.freshness:details.append('Цены обновлены: '+_safe(prices.freshness,50))
            else:details.append('⏳ Цены покупателей ещё не загружены.')
            details.extend(format_buyout_check(s.buyout_check))
            finance=repo.latest_metric(s.connection_id,report.day,'marketplace_net')
            if finance:
                accruals.append('После удержаний Ozon: <b>'+rubles(finance['value'])+'</b>')
                accruals.append('По дате начисления: '+_day(report.day)+'.')
            else:accruals.append('После удержаний Ozon: ⏳ начисления ещё не загружены.')
        else:
            if s.units is not None and s.ordered_revenue is not None:
                precision=0 if float(s.ordered_revenue).is_integer() else 2
                summary.append(f'🛒 Заказано: <b>{units} · {rubles(s.ordered_revenue,precision=precision)}</b>')
            else:
                summary.append('🛒 Заказано: <b>'+units+'</b> · ⏳ сумма неполная.')
            details.append('Стоимость заказов до удержаний.')
            source='Воронка продаж WB' if str(s.order_source or '').startswith('analytics/orders') else 'Статистика WB, резервный источник'
            details.append('Источник заказов: '+source+'.')
            finance=repo.latest_metric(s.connection_id,report.day,'bank_payment')
            if finance:
                accruals.append('Итог к оплате по фин. отчёту: <b>'+rubles(finance['value'])+'</b>')
                accruals.append(_wb_period(repo,finance))
                accruals.append('По финансовому отчёту WB, с удержаниями и корректировками.')
                accruals.append('Это не сумма всех заказов, оформленных в этот день, и не подтверждение банковского перевода.')
            else:accruals.append('Итог к оплате по фин. отчёту WB: ⏳ ещё не загружен.')
            goods=repo.latest_metric(s.connection_id,report.day,'goods_payable')
            if goods:
                accruals.append('К перечислению за товар: '+rubles(goods['value'])+'.')
                accruals.append('После комиссии и эквайринга, до остальных расходов; это отдельный показатель.')
                if not finance:accruals.append(_wb_period(repo,goods))
        events=s.events or build_daily_events(repo,s.connection_id,s.marketplace,date.fromisoformat(report.day))
        for label,event in (('✅ Выкуплено',events.buyouts),('↩️ Возвращено',events.returns),
                            ('❌ Отменено',events.cancellations)):
            event_load_failed = event_load_failed or (event.load_failed and
                not (event.units is None and event.unavailable_note))
            summary.append(_event_line(label,event))
            details.extend(_event_details(label,event,tz=tz))
            if event.freshness:event_freshness.append(event.freshness)
        if s.marketplace=='ozon':
            details.append('Продажи покупателям: дневная реализация Ozon. Возвраты: '
                           'после вручения, по дате возврата покупателем; отказы при вручении сюда не входят.')
        if s.cancellations is not None:
            details.append('Отмены среди заказов, созданных в этот день: '+num(s.cancellations)+
                           ' шт. Это другой показатель; он не подставляется в строку «Отменено».')
        if s.freshness:details.append('Заказы обновлены: '+_safe(s.freshness,50))
        if s.warning:details.append('⚠️ Последняя загрузка заказов не удалась; показаны сохранённые данные.')
        if finance:
            accruals.append('Обновлено: '+_safe(finance['as_of'],50))
            if finance['status']=='partial':accruals.append('⚠️ Финансовый источник вернул неполные данные.')
    if not sources:
        summary.append('\n⏳ Подключите маркетплейсы и загрузите данные.')
        details.append('Нет подключённых источников для этого магазина.')
        accruals.append('Нет подключённых финансовых источников.')
    summary.extend(['','ℹ️ Каждый показатель относится к дате своего события. '
                    'Начисления после удержаний открываются отдельно.','Данные предварительные.'])
    if event_freshness:
        summary.append('🕘 События проверены: '+display_time(min(event_freshness),tz=tz))
    if event_load_failed or any(s.warning for s in sources):
        summary.append('⚠️ Есть сбой загрузки — см. «Подробнее».')
    details.extend(['','Общий итог — сумма показанных цен: WB до удержаний, Ozon по цене покупателя. '
        'При пересчёте валют это оценка по ЦБ. Начисления после удержаний смотрите отдельно.'])
    accruals.extend(['','Это не чистая прибыль: себестоимость и налоги здесь не вычитаются.'])
    return DailyCardText('\n'.join(summary),'\n'.join(details),'\n'.join(accruals))


def daily_card_pages(card: dict) -> list[str]:
    """The upper summary stays fixed; long lower blocks are fully pageable."""
    limit=3900
    text=card['summary_html']
    note=card.get('status_note','')
    if note:text+='\n\n'+_safe(note,250)
    if utf16_length(text)>limit:raise ValueError('Daily summary exceeds Telegram limit')
    if card.get('section','summary')=='summary':return [text]
    lower=card[card['section']+'_html']
    budget=limit-utf16_length(text)-80
    if budget<100:raise ValueError('Daily summary leaves no room for details')
    pages=paginate_report_html(lower,limit=min(1500,budget),hard_limit=budget)
    result=[]
    for page,part in enumerate(pages):
        suffix=f'\n\n<i>Страница {page+1}/{len(pages)}</i>' if len(pages)>1 else ''
        result.append(text+'\n\n'+part+suffix)
    return result


def render_daily_card(card: dict) -> str:
    pages=daily_card_pages(card)
    return pages[max(0,min(int(card.get('page',0)),len(pages)-1))]
