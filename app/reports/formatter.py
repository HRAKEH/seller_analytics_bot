"""Compact Telegram HTML formatter."""
from __future__ import annotations
from html import escape
from decimal import Decimal, ROUND_HALF_UP
from .daily import DailyReport, MarketplaceDaily

def money(value: float | None, *, precision: int = 0) -> str:
    if value is None: return '—'
    return f"{value:,.{precision}f}".replace(',', ' ') + ' ₽'

def num(value: float | None) -> str:
    if value is None: return '—'
    return str(int(value)) if float(value).is_integer() else f'{value:.1f}'

def delta(current: float | None, previous: float | None, comparison: str = 'к пред. дню') -> str:
    if current is None or previous is None: return 'нет базы сравнения'
    if previous == 0: return '↑ с 0' if current > 0 else 'без изменений'
    pct=(current-previous)/previous*100
    arrow='↑' if pct>0 else ('↓' if pct<0 else '→')
    return f'{arrow} {abs(pct):.1f}% {comparison}'

def source_name(source: str) -> str:
    return '🟣 Ozon' if source == 'ozon' else '🔵 Wildberries'


def _buyer_price_lines(prices) -> list[str]:
    if prices is None:
        return ['  по цене покупателя: ⏳ цены ещё не загружены · обновите все отчёты']
    def amount(value, currency):
        value = value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        return f'{value:,.2f}'.replace(',', ' ') + (' ₽' if currency == 'RUB' else f' {currency}')
    summary = ' + '.join(amount(t.amount, t.currency) for t in prices.totals)
    label = 'по цене покупателя' if prices.complete else 'по полученным ценам покупателя, неполно'
    lines = [f'  {label}: <b>{summary or "—"}</b>']
    expected = str(prices.expected_units) if prices.expected_units is not None else '—'
    lines.append(f'  цены получены для {prices.priced_units} ед. · заказов в аналитике {expected} ед.')
    if prices.complete and prices.foreign_currencies:
        if prices.rub_total is not None:
            lines.append(f'  оценка в рублях по ЦБ: <b>≈ {amount(prices.rub_total, "RUB")}</b>')
            quotes = []
            for currency in prices.foreign_currencies:
                rate = prices.rates.rate(currency)
                # Keep enough digits for non-unit nominals and small FX rates.
                value = format(rate, 'f').rstrip('0').rstrip('.') if '.' in format(rate, 'f') else format(rate, 'f')
                quotes.append(f'1 {currency} = {value} ₽')
            lines.append('  курс ЦБ: ' + ' · '.join(quotes) +
                         f' · действует с {prices.rates.effective_date}, для {prices.rates.requested_date}')
        else:
            lines.append('  оценка в рублях: ⏳ нет курса ЦБ для ' + ', '.join(prices.missing_rates))
    for warning in prices.warnings:
        lines.append('  ⚠️ ' + escape(warning))
    if prices.freshness:
        lines.append('  <i>цены от ' + escape(prices.freshness) + '</i>')
    return lines

def format_daily(report: DailyReport) -> str:
    lines=[f'📊 <b>Заказы за {report.day}</b>', '━━━━━━━━━━━━━━━━']
    total=report.total_units
    for s in report.sources:
        if s.units is None:
            lines.append(f'{source_name(s.marketplace)}: ⏳ данных нет')
            if s.warning: lines.append(f'  ⚠️ {escape(s.warning[:180])}')
            continue
        avg=(s.ordered_revenue/s.units) if s.ordered_revenue is not None and s.units else None
        if total is not None and total>0:
            share=s.units/total*100
            lines.append(f'{source_name(s.marketplace)}: <b>{num(s.units)} шт.</b> · доля {share:.1f}%')
        else:
            lines.append(f'{source_name(s.marketplace)}: <b>{num(s.units)} шт.</b>')
        if s.ordered_revenue is not None:
            label = 'сумма заказов по предельной цене (API)' if s.marketplace == 'ozon' else 'стоимость заказов до удержаний'
            lines.append(f'  {label} {money(s.ordered_revenue)} · на ед. {money(avg)}')
        if s.marketplace=='wildberries':
            label='Воронка продаж' if str(s.order_source or '').startswith('analytics/orders') else 'Статистика, резервный источник'
            lines.append(f'  источник: {label}')
        if s.marketplace=='ozon':
            lines.extend(_buyer_price_lines(s.buyer_prices))
            if s.marketplace_net is not None:
                lines.append(f'  начисления после удержаний по дате начисления: <b>{money(s.marketplace_net,precision=2)}</b>')
            else:
                lines.append(f'  начисления после удержаний: ⏳ ещё не загружены · /finance 1 {report.day}')
        if s.cancellations is not None:
            lines.append(f'  отмены {num(s.cancellations)} шт. · {delta(s.units, s.previous_units)}')
        if s.warning:
            lines.append(f'  ⚠️ последнее обновление не удалось: {escape(s.warning[:150])}')
        elif s.preliminary:
            fresh = f' · данные от {escape(s.freshness)}' if s.freshness else ''
            lines.append(f'  <i>предварительно{fresh}</i>')
    lines.append('━━━━━━━━━━━━━━━━')
    if total is not None:
        lines.append(f'🟡 <b>ИТОГО: {num(total)} заказанных ед.</b> · {delta(total, report.previous_total_units)}')
    else:
        partial=report.available_units
        if partial is not None:
            lines.append(f'🟡 ИТОГО: ⏳ неполные данные · по загруженным источникам {num(partial)} ед.')
        else:
            lines.append('🟡 ИТОГО: ⏳ нет сопоставимых данных')
    lines.append('ℹ️ Суммы заказов по площадкам пока не складываются: финансовая методика источников различается.')
    if any(s.marketplace=='wildberries' and s.units is not None and
           not str(s.order_source or '').startswith('analytics/orders') for s in report.sources):
        lines.append('ℹ️ WB: источник может не включать заказы с неподтверждённой оплатой.')
    if any(s.marketplace=='ozon' for s in report.sources):
        if any(s.marketplace=='ozon' and s.ordered_revenue is not None for s in report.sources):
            lines.append('ℹ️ Ozon: сумму заказов сверяйте с колонкой «по предельной цене». «По цене реализации» — отдельный показатель.')
        if any(s.marketplace=='ozon' and s.buyer_prices and s.buyer_prices.foreign_currencies for s in report.sources):
            lines.append('ℹ️ Пересчёт по ЦБ — оценка. Рублёвая сумма в кабинете Ozon может отличаться.')
        lines.append('ℹ️ Ozon: заказы сгруппированы по дате заказа, финансы — по дате начисления. Начисления могут включать заказы других дней.')
    return '\n'.join(lines)


def format_period(report) -> str:
    lines=[f'📅 <b>{escape(report.label)}</b> · {report.start} — {report.end}',
           f'✅ Полных дней: {report.complete_days}/{report.requested_days}', '━━━━━━━━━━━━━━━━']
    for s in report.sources:
        lines.append(f'{source_name(s.marketplace)}: <b>{num(s.units)} шт.</b> · {delta(s.units,s.previous_units,"к сопоставимым дням прошлого периода")}')
    lines.append('━━━━━━━━━━━━━━━━')
    if report.complete_days:
        lines.append(f'🟡 <b>ИТОГО: {num(report.total)} заказанных ед.</b> · {delta(report.total,report.previous_total,"к сопоставимым дням прошлого периода")}')
    else:
        lines.append('📭 Нет полностью сопоставимых дней. Откройте «📊 Отчёты» → «📥 Загрузить историю».')
    return '\n'.join(lines)


def _product_name(name: str, limit: int = 36) -> str:
    clean=' '.join(str(name).split())
    return escape(clean if len(clean)<=limit else clean[:limit-1]+'…')


def format_product_report(report) -> str:
    lines=[f'🏆 <b>Товары · {report.start} — {report.end}</b>','━━━━━━━━━━━━━━━━']
    labels={'ozon':'🟣 Ozon','wildberries':'🔵 Wildberries'}
    if not report.top:
        lines.append('📭 Товарной истории пока нет. Откройте «📊 Отчёты» → «📥 Загрузить историю».')
    for market in ('ozon','wildberries'):
        rows=report.top.get(market) or []
        if not rows: continue
        basis = ' (по предельной цене)' if market == 'ozon' else ''
        lines.append(f'\n{labels.get(market,market)} · <b>Top-{len(rows)} по сумме заказов{basis}*</b>')
        for i,row in enumerate(rows,1):
            lines.append(f'{i}. {_product_name(row.name)} · {row.units:g} шт. · {money(row.order_amount)}')

    if not getattr(report,'comparison_complete',True):
        lines.append('\n⚠️ Сравнение роста/просадки скрыто: текущий или предыдущий период загружен не полностью.')

    if report.decline:
        lines.append('\n📉 <b>Просадка по заказанным единицам</b>')
        for row in report.decline[:5]:
            pct=f'{row.change_pct:.0f}%' if row.change_pct is not None else '—'
            lines.append(f'• {_product_name(row.name)}: {row.previous_units:g} → {row.current_units:g} ({pct})')
    if report.growth:
        lines.append('\n📈 <b>Рост</b>')
        for row in report.growth[:5]:
            pct=f'+{row.change_pct:.0f}%' if row.change_pct is not None else 'новый спрос'
            lines.append(f'• {_product_name(row.name)}: {row.previous_units:g} → {row.current_units:g} ({pct})')

    if report.fulfillment_orders:
        lines.append('\n🚚 <b>Схемы по операционным заказам</b>')
        for row in report.fulfillment_orders:
            market=labels.get(str(row['marketplace']),str(row['marketplace']))
            lines.append(f"• {market} {escape(str(row['fulfillment_scheme']))}: {float(row['value']):g} ед.")
    if report.inventory_schemes:
        lines.append('\n📦 <b>Текущий остаток по схемам</b>')
        for row in report.inventory_schemes:
            market=labels.get(str(row['marketplace']),str(row['marketplace']))
            lines.append(f"• {market} {escape(str(row['fulfillment_scheme']))}: {float(row['available_units']):g} шт.")

    risks=[r for r in report.stock_risks if r.available_units<=0 or (r.days_left is not None and r.days_left<=report.risk_days)]
    if risks:
        lines.append('\n🚨 <b>Риск дефицита</b>')
        for row in risks[:7]:
            if row.available_units<=0:
                state='НЕТ ОСТАТКА'
            elif row.days_left is not None:
                state=f'≈ {row.days_left:.1f} дн.'
            else:
                state='нет оценки спроса'
            lines.append(f'• {_product_name(row.name)} · {row.available_units:g} шт. · {state}')
    elif report.stock_risks:
        lines.append(f'\n✅ По текущей оценке нет товаров с запасом менее {report.risk_days} дней.')

    lines.append('\n<i>* Это предварительная сумма операционных заказов, не выплата и не финансовая выручка.</i>')
    lines.append('<i>Прогноз запаса считается только по дням с успешно загруженными заказами.</i>')
    return '\n'.join(lines)


def format_stock_report(report) -> str:
    labels={'ozon':'🟣 Ozon','wildberries':'🔵 Wildberries'}
    lines=['📦 <b>Остатки и запас</b>','━━━━━━━━━━━━━━━━']
    if report.inventory_schemes:
        for row in report.inventory_schemes:
            market=labels.get(str(row['marketplace']),str(row['marketplace']))
            lines.append(f"{market} · {escape(str(row['fulfillment_scheme']))}: <b>{float(row['available_units']):g} шт.</b>")
    else:
        lines.append('📭 Остатки ещё не загружены.')
    if report.stock_risks:
        lines.append('\n<b>Минимальный запас</b>')
        for row in report.stock_risks[:10]:
            demand=f'{row.avg_daily_units:.1f}/день' if row.avg_daily_units is not None else 'спрос —'
            if row.available_units<=0: days='0 дн.'
            elif row.avg_daily_units is None: days='нет данных для оценки'
            elif row.avg_daily_units==0: days='нет спроса за загруженные дни'
            else: days=f'{row.days_left:.1f} дн.'
            lines.append(f'• {_product_name(row.name)} · {row.available_units:g} шт. · {demand} · {days}')
    lines.append('\n<i>Ozon: показывается present; reserved хранится отдельно и не вычитается без подтверждённой методики. WB: quantity.</i>')
    return '\n'.join(lines)

def format_finance(report) -> str:
    def fm(value):return money(value,precision=2)
    lines=[f'💰 <b>Финансы · {report.start} — {report.end}</b>','━━━━━━━━━━━━━━━━']
    if not report.sources:
        return '\n'.join(lines+['📭 Финансовые данные ещё не загружены. Откройте «💰 Деньги и реклама» → «💰 Финансы».'])
    for s in report.sources:
        m=s.metrics
        lines.append(f'\n{source_name(s.marketplace)}')
        if 'financial_sales' in m: lines.append(f'  продажи по фин. отчёту: <b>{fm(m["financial_sales"])}</b>')
        if 'goods_payable' in m: lines.append(f'  к перечислению за товар: {fm(m["goods_payable"])}')
        if 'marketplace_net' in m: lines.append(f'  начисления после удержаний: <b>{fm(m["marketplace_net"])}</b>')
        if 'bank_payment' in m: lines.append(f'  банковский платёж: <b>{fm(m["bank_payment"])}</b>')
        costs=[]
        for key,label in [('commission','комиссия'),('logistics','логистика'),('storage','хранение'),('acceptance','приёмка'),('acquiring','эквайринг'),('services','прочие услуги'),('penalties','штрафы')]:
            if m.get(key): costs.append(f'{label} {fm(m[key])}')
        if costs: lines.append('  расходы: '+ ' · '.join(costs))
        if 'finance_ad_spend' in m:
            lines.append(f'  из прочих услуг — реклама по начислениям: {fm(m["finance_ad_spend"])} (уже учтена в итоге)')
        if m.get('compensation'): lines.append(f'  компенсации/доплаты: {fm(m["compensation"])}')
        if m.get('ad_spend') is not None:
            label='реклама Performance, справочно' if s.marketplace=='ozon' else 'реклама'
            lines.append(f'  {label}: {fm(m.get("ad_spend",0))}')
            attributed=m.get('ad_attributed_sales',0)
            if attributed>0: lines.append(f'  ДРР по атрибутированным продажам: {m.get("ad_spend",0)/attributed*100:.1f}%')
        if s.estimated_order_cogs>0:
            cov=f'{s.cogs_coverage_pct:.0f}%' if s.cogs_coverage_pct is not None else '—'
            lines.append(f'  оценка себестоимости заказанных ед.: {money(s.estimated_order_cogs)} · покрытие {cov}')
    if report.missing_cost_products:
        lines.append(f'\n⚠️ Без себестоимости: {report.missing_cost_products} товаров. Откройте «📦 Товары» → «📥 Импорт себестоимости».')
    if getattr(report,'source_coverage',()):
        from .coverage import format_source_coverage
        lines.append('\n'+format_source_coverage(report.source_coverage))
    lines.append('\n<i>Финансовые данные приходят с задержкой. Продажи, начисления и банковская выплата — разные показатели.</i>')
    lines.append('<i>Себестоимость здесь относится к заказанным единицам и является оценкой, а не бухгалтерской прибылью.</i>')
    return '\n'.join(lines)
