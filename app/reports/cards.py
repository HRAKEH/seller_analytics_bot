"""Snapshots for one-message daily reports; monetary bases stay separate."""
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
    summary=[f'📊 <b>Заказы · {_day(report.day)}</b>']
    if shop:summary.append('🏪 '+_safe(shop.name,80))
    if pref and pref.demo_mode:summary.append('🧪 <b>Учебные данные · DEMO</b>')
    summary.append('')
    summary.append(f'Всего заказано: <b>{num(report.total_units)} шт.</b>' if report.total_units is not None
                   else 'Всего заказано: ⏳ неполные данные.')
    details=['🔎 <b>Подробнее</b>']
    accruals=['💰 <b>Начисления</b>',
              'Финансовые операции и заказы относятся к разным датам.']
    sources=sorted(report.sources,key=lambda s: (s.marketplace!='ozon',s.connection_id))
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
                summary.append(f'<b>{units} · {approximate}{rubles(converted)}</b>')
                currencies=', '.join(prices.foreign_currencies)
                summary.append(f'По цене покупателя, с пересчётом {currencies} по ЦБ.' if currencies
                               else 'По цене покупателя.')
            else:
                summary.append('<b>'+units+'</b>')
                summary.append('По цене покупателя: ⏳ цены пока неполные.' if not prices or not prices.complete
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
                summary.append(f'<b>{units} · {rubles(s.ordered_revenue,precision=precision)}</b>')
            else:
                summary.append('<b>'+units+'</b>')
                if s.ordered_revenue is None:summary.append('⏳ Сумма заказов не загружена.')
            summary.append('Стоимость заказов до удержаний.')
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
        if s.cancellations is not None:details.append('Отмены: '+num(s.cancellations)+' шт.')
        if s.freshness:details.append('Заказы обновлены: '+_safe(s.freshness,50))
        if s.warning:details.append('⚠️ Последняя загрузка заказов не удалась; показаны сохранённые данные.')
        if finance:
            accruals.append('Обновлено: '+_safe(finance['as_of'],50))
            if finance['status']=='partial':accruals.append('⚠️ Финансовый источник вернул неполные данные.')
    if not sources:
        summary.append('\n⏳ Подключите маркетплейсы и загрузите данные.')
        details.append('Нет подключённых источников для этого магазина.')
        accruals.append('Нет подключённых финансовых источников.')
    summary.extend(['','Данные предварительные.'])
    if any(s.warning for s in sources):summary.append('⚠️ Есть сбой загрузки — см. «Подробнее».')
    details.extend(['','Суммы площадок не складываются: основания расчёта различаются.'])
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
