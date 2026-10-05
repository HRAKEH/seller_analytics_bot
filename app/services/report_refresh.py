"""One isolated refresh of report sources, with progress and partial results."""
from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from app.reports.dates import readable_dates, display_day

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RefreshStage:
    key: str
    label: str
    ok: bool
    skipped: bool = False


async def refresh_reports(ctx, start: date, end: date, progress=None, *, include_finance=True) -> tuple[RefreshStage,...]:
    if end < start or (end-start).days >= 31:
        raise ValueError('Обновление отчётов: от 1 до 31 дня')
    if ctx.demo_mode():
        return (RefreshStage('demo','Демо: сохранённые учебные данные',True,True),)
    stages=[]
    async def run(key, label, action):
        if progress is not None:
            try: await progress(label)
            except asyncio.CancelledError: raise
            except Exception: log.warning('Cannot update report refresh progress',exc_info=True)
        try:
            outcomes=await action()
            skipped=not outcomes
            ok=all(x.ok for x in outcomes)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Report refresh stage %s failed',key)
            ok=False; skipped=False
        stages.append(RefreshStage(key,label,ok,skipped))

    async with ctx.operation_lock('report_refresh'):
        for day_offset in range((end-start).days+1):
            day=start+timedelta(days=day_offset)
            if ctx.wb_connection_id is not None:
                await run('orders:wb:'+day.isoformat(),'Заказы WB · '+display_day(day),
                    lambda day=day: _one(ctx.collector.collect_wb_orders_day(ctx.wb_connection_id,day,shop_id=ctx.shop_id)))
            if ctx.ozon_connection_id is not None:
                await run('orders:ozon:'+day.isoformat(),'Заказы Ozon · '+display_day(day),
                    lambda day=day: _one(ctx.collector.collect_ozon_orders_day(ctx.ozon_connection_id,day,shop_id=ctx.shop_id)))
        for key,method,title in (('finance',ctx.collector.collect_finance,'Начисления и удержания'),
                                 ('advertising',ctx.collector.collect_advertising,'Рекламная статистика')):
            if key=='finance' and not include_finance:continue
            for market,wb,ozon in (('WB',ctx.wb_connection_id,None),('Ozon',None,ctx.ozon_connection_id)):
                if wb is None and ozon is None:continue
                kwargs={'start':start,'end':end,'wb_connection_id':wb,'ozon_connection_id':ozon}
                if key=='finance':kwargs['shop_id']=ctx.shop_id
                await run(key+':'+market,title+' · '+market,lambda method=method,kwargs=kwargs:method(**kwargs))
        if ctx.ozon_connection_id is not None:
            await run('fulfillment','Отправления Ozon · FBO/FBS',lambda:ctx.collector.collect_ozon_fulfillment_range(
                shop_id=ctx.shop_id,connection_id=ctx.ozon_connection_id,start=start,end=end))
            await run('buyouts','Цены выкупа Ozon · отдельный отчёт',lambda:ctx.collector.collect_ozon_buyout_prices_range(
                connection_id=ctx.ozon_connection_id,start=start,end=end))
        if ctx.wb_connection_id is not None:
            await run('sales','Продажи и возвраты WB',lambda:ctx.collector.collect_wb_sales_range(
                shop_id=ctx.shop_id,connection_id=ctx.wb_connection_id,start=start,end=end))
        events=getattr(ctx.collector,'collect_daily_events_range',None)
        if events is not None:
            await run('events','Выкупы, клиентские возвраты и даты отмен',lambda:events(
                start=start,end=end,wb_connection_id=ctx.wb_connection_id,
                ozon_connection_id=ctx.ozon_connection_id))
    return tuple(stages)


async def _one(awaitable):
    return [await awaitable]


@readable_dates
def format_refresh(stages, start: date, end: date) -> str:
    lines=[f'🔄 <b>Обновление · {start} — {end}</b>']
    for stage in stages:
        icon='—' if stage.skipped else ('✅' if stage.ok else '⚠️')
        suffix=' · источник не подключён/нет операций' if stage.skipped else ''
        lines.append(f'{icon} {stage.label}{suffix}')
    if any(not s.ok for s in stages):
        lines.append('⚠️ Часть источников не обновилась. Последние успешные данные сохранены; проверьте свежесть в отчёте.')
    return '\n'.join(lines)
