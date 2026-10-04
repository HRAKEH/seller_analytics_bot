from __future__ import annotations
from html import escape
from app.services.supply import SupplyPlan, SupplyRow, SupplyCalibrationReport
from .dates import readable_dates


def _num(v: float | None) -> str:
    if v is None: return "—"
    if abs(v-round(v))<1e-9: return str(int(round(v)))
    return f'{v:.1f}'


def _cover(v: float | None) -> str:
    if v is None: return '—'
    if v>999: return '>999 дн.'
    return f'{v:.1f} дн.'


@readable_dates
def format_supply_plan(plan: SupplyPlan, *, limit: int=15) -> str:
    counts={x:sum(1 for r in plan.rows if r.abc_class==x) for x in 'ABC'}
    xyz={x:sum(1 for r in plan.rows if r.xyz_class==x) for x in ('X','Y','Z','?')}
    lines=[f'🚚 <b>План поставок · {plan.as_of.isoformat()}</b>','━━━━━━━━━━━━━━━━',
           f'Полных дней истории: <b>{plan.complete_days}/{plan.lookback_days}</b>',
           f'ABC по заказанным единицам: A {counts["A"]} · B {counts["B"]} · C {counts["C"]}',
           f'XYZ: X {xyz["X"]} · Y {xyz["Y"]} · Z {xyz["Z"]} · ? {xyz["?"]}',
           f'К заказу сейчас: <b>{plan.order_now_count}</b> SKU','']
    urgent=[r for r in plan.rows if r.needs_order]
    for market,label in (('wildberries','WB'),('ozon','Ozon')):
        market_rows=[r for r in plan.rows if market in r.marketplaces]
        if market_rows:
            missing=sum(1 for r in market_rows if market in r.missing_inventory_marketplaces)
            lines.append(f'{label}: товаров в плане {len(market_rows)} · без снимка остатков {missing}')
    if urgent:
        lines.append('<b>Приоритет поставки</b>')
        for r in urgent[:limit]:
            trend='' if r.trend_pct is None else f' · тренд {r.trend_pct:+.0f}%'
            stock_note=' ⚠️' if r.inventory_stale else ''
            markets=', '.join('WB' if m=='wildberries' else 'Ozon' for m in r.marketplaces)
            confidence={'low':'мало данных','medium':'средняя','high':'высокая'}.get(r.confidence,r.confidence)
            lines.append(
                f'🚨 <b>{escape(r.name)}</b> · {markets} · Внутренний SKU <code>{escape(r.internal_sku)}</code> · {r.abc_class}{r.xyz_class}\n'
                f'  остаток {_num(r.available_units)}{stock_note} · в пути {_num(r.inbound_units)} · запас {_cover(r.days_cover)}\n'
                f'  прогноз {_num(r.forecast_daily_units)}/день · 7д {_num(r.forecast_next_7_units)}{trend}\n'
                f'  корректировка bias ×{r.bias_correction:.2f} · promo ×{r.promo_factor:.2f} ({r.promo_days} дн.)\n'
                f'  рекомендовано <b>{_num(r.recommended_order_units)} шт.</b> · доставка {r.effective_lead_time_days} дн. · запас на задержки {r.effective_safety_stock_days} дн. · уверенность: {confidence}')
    else:
        lines.append('✅ По текущей модели товаров для немедленной поставки нет.')
    missing_stock=[r for r in plan.rows if r.available_units is None]
    if missing_stock:
        lines += ['',f'⚠️ У {len(missing_stock)} SKU нет успешного снимка остатков: рекомендации на закупку для них не выдаются.']
    stale=[r for r in plan.rows if r.inventory_stale]
    if stale:
        lines += ['',f'⚠️ У {len(stale)} SKU снимок остатков старше 2 дней; перед заказом нажмите «📦 Остатки» или «🔄 Обновить план».']
    low=[r for r in plan.rows if r.confidence=='low']
    if low:
        lines += ['',f'⚠️ Низкая уверенность у {len(low)} SKU: мало полной истории или недостаточно недель для XYZ.']
    lines += ['',
              'ℹ️ Прогноз использует полные дни заказов, последний успешный остаток, известные поставки с ETA и ограниченную недельную сезонность. '
              'Поставки без ETA и внешние ограничения поставщика автоматически не засчитываются. Акции учитываются только при точном участии SKU и подтверждённом историческом uplift.',
              'ABC здесь — по заказанным единицам, а не по бухгалтерской выручке.']
    return '\n'.join(lines)


@readable_dates
def format_supply_product(row: SupplyRow) -> str:
    trend='—' if row.trend_pct is None else f'{row.trend_pct:+.1f}%'
    return '\n'.join([
        f'🚚 <b>{escape(row.name)}</b>',f'Внутренний SKU: <code>{escape(row.internal_sku)}</code>',
        'Площадки: '+', '.join('WB' if m=='wildberries' else 'Ozon' for m in row.marketplaces),
        f'Класс: <b>{row.abc_class}{row.xyz_class}</b> · confidence {row.confidence}',
        f'История: {row.history_days} полных дней · {row.first_history_date or "—"} → {row.last_history_date or "—"}',
        f'Среднее: {_num(row.avg_daily_units)} шт./день',
        f'Прогноз: <b>{_num(row.forecast_daily_units)} шт./день</b> · 7 дней {_num(row.forecast_next_7_units)} · тренд {trend}',
        f'Коррекция: bias ×{row.bias_correction:.2f} · promo ×{row.promo_factor:.2f} · promo-дней в горизонте {row.promo_days}',
        f'Остаток: {_num(row.available_units)} · в пути {_num(row.inbound_units)} · эффективный {_num(row.effective_units)} · покрытие {_cover(row.days_cover)}',
        f'Reorder point: {_num(row.reorder_point_units)}',
        f'Target stock: {_num(row.target_units)}',
        f'Рекомендация: <b>{_num(row.recommended_order_units)} шт.</b>',
        f'Lead: {row.lead_time_days} + обученный буфер {row.lead_buffer_days} = {row.effective_lead_time_days} дней',
        f'Safety: {row.safety_stock_days} + обученный буфер {row.safety_buffer_days} = {row.effective_safety_stock_days} дней',
        f'Target: {row.target_stock_days} дней · калибровка {row.calibration_confidence}',
        f'Упаковка / min order: {_num(row.pack_size)} / {_num(row.min_order_qty)}',
        f'Остаток снимок: {row.inventory_as_of or "—"}' + (f' · ⚠️ {row.inventory_age_days} дн. назад' if row.inventory_stale else ''),
    ])


@readable_dates
def format_supply_calibration(report: SupplyCalibrationReport, *, limit: int=20) -> str:
    lines=[f'🧠 <b>Самокалибровка поставок · {report.as_of.isoformat()}</b>','━━━━━━━━━━━━━━━━',
           f'Автоприменение буферов: <b>{"включено" if report.auto_apply else "выключено"}</b>',
           'Базовые lead/safety настройки не переписываются. Модель может только добавить ограниченный риск-буфер.','']
    changed=[r for r in report.rows if r.lead_buffer_days or r.safety_buffer_days]
    if not changed:
        lines.append('✅ Подтверждённых оснований увеличивать буферы пока нет.')
    else:
        lines.append('<b>SKU с обученными буферами</b>')
        for r in changed[:limit]:
            wape='—' if r.forecast_wape_pct is None else f'{r.forecast_wape_pct:.0f}%'
            zero='—' if r.zero_stock_rate_pct is None else f'{r.zero_stock_rate_pct:.0f}%'
            delay='—' if r.p75_inbound_delay_days is None else f'{r.p75_inbound_delay_days:.1f} дн.'
            lines.append(
                f'🧠 Внутренний SKU <code>{escape(r.internal_sku)}</code> · {r.confidence}\n'
                f'  safety +{r.safety_buffer_days} дн. · lead +{r.lead_buffer_days} дн.\n'
                f'  WAPE {wape} ({r.forecast_samples} выборок) · нулевой остаток {zero} ({r.inventory_samples} снимков)\n'
                f'  P75 задержки {delay} ({r.inbound_delay_samples} поставок)\n'
                f'  причина: {escape("; ".join(r.reasons))}')
    low=sum(1 for r in report.rows if r.confidence=='low')
    if low:
        lines += ['',f'ℹ️ У {low} SKU низкая уверенность: модель не будет делать сильные выводы из единичных наблюдений.']
    lines += ['',
        'ℹ️ Lead-буфер обучается только по фактической задержке приёмки, где источник даёт реальную fact-date. '
        'Safety-буфер использует ошибки уже завершённых backtest и наблюдения нулевого остатка.']
    return '\n'.join(lines)
