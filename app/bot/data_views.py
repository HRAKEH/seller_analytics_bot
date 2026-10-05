"""Durable finance cards and export pickers with user/shop-bound callbacks."""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from html import escape
import logging
from pathlib import Path
import tempfile
from weakref import WeakValueDictionary

from aiogram import types
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest

from app.reports.accruals import build_accrual_ledger, export_accrual_ledger
from app.reports.wb_accruals import build_wb_accrual_ledger, export_wb_accrual_ledger
from app.reports.accrual_cards import build_accrual_card
from app.reports.dates import display_day
from app.services.exporting import export_xlsx, export_csv_zip
from app.services.spreadsheet_format import SHEETS, export_filename

log = logging.getLogger(__name__)


def button(text, action):
    return types.InlineKeyboardButton(text=text, callback_data='data_view:'+action)


def export_sections(finance):
    hidden=set() if finance else {'Finance','Costs','Management','Reconciliation'}
    return [(key, label, detail) for key,(label,detail) in SHEETS.items()
            if key not in hidden and key not in {'AccrualSummary','OzonOperations','WbReports'}]


def render_view(view, *, finance=True):
    p=view['payload'];kind=view['kind'];rows=[]
    if kind=='export':
        text='📤 <b>Скачать данные магазина</b>\n🏪 '+escape(p['shop_name'])
        if p.get('days'):
            start=date.fromisoformat(p['end'])-timedelta(days=p['days']-1)
            text+='\n\nПериод: <b>'+display_day(start)+' — '+display_day(p['end'])+'</b>'
            text+='\n\nВыберите формат:'
            text+='\n\n📊 <b>Excel (.xlsx)</b> — один файл с листами по разделам.'
            text+='\n🗂 <b>Архив CSV (.zip)</b> — отдельные таблицы и расшифровка полей.'
            text+='\n\n<i>Данные берутся из базы. Для свежих цифр сначала обновите отчёты.</i>'
            rows += [[button('📊 Скачать Excel (.xlsx)','file:xlsx')],
                     [button('🗂 Скачать архив CSV (.zip)','file:csv')]]
        else:
            text+='\n\nВыберите период. В файл попадут сохранённые данные, доступные вашей роли.'
            rows += [[button(f'{days} дней',f'days:{days}') for days in (7,30,90)]]
        items=export_sections(finance)
        pages=[items[i:i+5] for i in range(0,len(items),5)]
        if p.get('expanded'):
            page=max(0,min(p.get('page',0),len(pages)-1))
            text+='\n\n<b>Что будет в файле</b>\n'+'\n\n'.join('<b>'+label+'</b>\n'+detail for _,label,detail in pages[page])
    else:
        text=p['summary'];pages=p['pages']
        if p.get('expanded'):
            page=max(0,min(p.get('page',0),len(pages)-1));text+='\n\n'+pages[page]
        if p.get('available'):
            rows += [[button('📄 Скачать CSV','file:csv'),button('📊 Excel (.xlsx)','file:xlsx')]]
    if p.get('expanded') and len(pages)>1:
        page=max(0,min(p.get('page',0),len(pages)-1))
        text+=f'\n\n<i>Страница {page+1}/{len(pages)}</i>'
        rows.append([button('◀️','prev'),button(f'{page+1}/{len(pages)}','noop'),button('▶️','next')])
    label=('Что будет в файле' if kind=='export' else 'Подробнее') if not p.get('expanded') else 'Свернуть'
    rows.append([button(label,'toggle')])
    if kind=='export' and p.get('days'):rows.append([button('⬅️ Другой период','period')])
    rows.append([button('🏠 Главное меню','home')])
    return text,types.InlineKeyboardMarkup(inline_keyboard=rows)


class DataViewController:
    def __init__(self, context, navigation):
        self.context=context;self.navigation=navigation
        self._locks=WeakValueDictionary()

    def _allowed(self, view, user_id):
        repo=self.context.repository
        shop=repo.get_shop(view['shop_id'])
        required='view' if view['kind']=='export' else 'finance'
        return bool(shop and shop.active and user_id==view['user_id'] and repo.can_user(user_id,shop.id,required))

    def _save(self, key, view):
        self.context.repository.save_data_view(*key,**{k:view[k] for k in ('shop_id','user_id','kind','payload')})

    async def _show(self, message, kind, payload, *, transient=False):
        user=message.from_user
        view={'shop_id':self.context.shop_id,'user_id':user.id if user else 0,'kind':kind,'payload':payload}
        if not self._allowed(view,view['user_id']):return await message.answer('⛔ Нет доступа к этому разделу.')
        text,markup=render_view(view,finance=self.context.repository.can_user(view['user_id'],view['shop_id'],'finance'))
        sent=await (self.navigation.show(message,text,parse_mode='HTML',reply_markup=markup) if transient else
                    message.answer(text,parse_mode='HTML',reply_markup=markup))
        self._save((message.bot.id,message.chat.id,sent.message_id),view)
        if not transient:await self.navigation.dismiss(message)
        return sent

    async def show_accruals(self, message, marketplace, start, end):
        repo=self.context.repository
        if not message.from_user or not repo.can_user(message.from_user.id,self.context.shop_id,'finance'):
            return await message.answer('⛔ Начисления доступны владельцу и бухгалтеру.')
        builder=build_accrual_ledger if marketplace=='ozon' else build_wb_accrual_ledger
        ledger=builder(repo,self.context.shop_id,start.isoformat(),end.isoformat())
        shop=repo.get_shop(self.context.shop_id);pref=repo.get_shop_preferences(shop.id)
        timezone=pref.timezone if pref else 'Europe/Moscow'
        payload=build_accrual_card(ledger,marketplace,shop.name,timezone=timezone)
        payload.update(start=ledger.start,end=ledger.end,source_run_ids=ledger.source_run_ids,
                       expanded=False,page=0,shop_name=shop.name,timezone=timezone)
        return await self._show(message,marketplace,payload)

    async def show_export(self, message, end):
        shop=self.context.repository.get_shop(self.context.shop_id)
        if shop is None:return await message.answer('Магазин недоступен.')
        return await self._show(message,'export',{'shop_name':shop.name,'end':end.isoformat(),
            'days':None,'expanded':False,'page':0},transient=True)

    async def _edit(self, message, view):
        finance=self.context.repository.can_user(view['user_id'],view['shop_id'],'finance')
        text,markup=render_view(view,finance=finance)
        try:await message.edit_text(text,parse_mode='HTML',reply_markup=markup)
        except TelegramBadRequest as exc:
            if 'message is not modified' not in str(exc).lower():raise
        self._save((message.bot.id,message.chat.id,message.message_id),view)

    async def handle(self, callback, *, on_home):
        if not isinstance(callback.message,types.Message):
            return await callback.answer('Сообщение недоступно.',show_alert=True)
        message=callback.message;key=(message.bot.id,message.chat.id,message.message_id)
        lock=self._locks.setdefault(key,asyncio.Lock())
        async with lock:
            view=self.context.repository.data_view(*key)
            if view is None:return await callback.answer('Откройте раздел заново.',show_alert=True)
            if not self._allowed(view,callback.from_user.id):
                return await callback.answer('Нет доступа к этому отчёту.',show_alert=True)
            action=str(callback.data or '').removeprefix('data_view:')
            p=view['payload']
            if action=='home':
                await callback.answer();return await on_home(callback)
            if action in {'file:csv','file:xlsx'}:
                if (view['kind']=='export' and not p.get('days')) or (view['kind']!='export' and not p.get('available')):
                    return await callback.answer('Сначала выберите период с сохранёнными данными.',show_alert=True)
                await callback.answer('Готовлю файл…')
                try:await self._download(message,view,action.split(':')[1])
                except asyncio.CancelledError:raise
                except (ValueError,PermissionError) as exc:
                    await callback.answer(str(exc),show_alert=True)
                except Exception:
                    log.exception('Data file export failed for shop %s',view['shop_id'])
                    await callback.answer('Файл не создан. Откройте раздел заново и повторите.',show_alert=True)
                return
            if action=='noop':return await callback.answer()
            if action=='toggle':p['expanded']=not p.get('expanded');p['page']=0
            elif action in {'next','prev'} and p.get('expanded'):
                count=len(p['pages']) if view['kind']!='export' else (len(export_sections(
                    self.context.repository.can_user(callback.from_user.id,view['shop_id'],'finance')))+4)//5
                p['page']=max(0,min(p.get('page',0)+(1 if action=='next' else -1),count-1))
            elif action=='period' and view['kind']=='export':p.update(days=None,expanded=False,page=0)
            elif action in {'days:7','days:30','days:90'} and view['kind']=='export':
                p.update(days=int(action.split(':')[1]),expanded=False,page=0)
            else:return await callback.answer('Эта кнопка устарела. Откройте раздел заново.',show_alert=True)
            await callback.answer()
            try:await self._edit(message,view)
            except TelegramAPIError:
                await callback.answer('Сообщение недоступно. Откройте раздел заново.',show_alert=True)

    async def _download(self, message, view, fmt):
        repo=self.context.repository;p=view['payload'];shop_id=view['shop_id'];user=view['user_id']
        finance=repo.can_user(user,shop_id,'finance')
        technical=user in self.context.settings.owner_ids
        with tempfile.TemporaryDirectory(prefix='sellerbot-data-') as folder:
            if view['kind']=='export':
                days=p['days'];end=date.fromisoformat(p['end']);start=(end-timedelta(days=days-1)).isoformat()
                name=export_filename('Данные',p['shop_name'],start,p['end'],'.xlsx' if fmt=='xlsx' else '_CSV.zip')
                path=Path(folder)/name
                exporter=export_xlsx if fmt=='xlsx' else export_csv_zip
                await asyncio.to_thread(exporter,repo,shop_id,end,days,path,include_finance=finance,include_technical=technical)
                caption='📤 Данные магазина'
            else:
                builder=build_accrual_ledger if view['kind']=='ozon' else build_wb_accrual_ledger
                ledger=builder(repo,shop_id,p['start'],p['end'],source_run_ids=p['source_run_ids'])
                label='Ozon' if view['kind']=='ozon' else 'WB'
                start=p['start']
                file_start,file_end=start,p['end']
                prefix='Начисления_'+label
                if view['kind']=='wb':
                    periods={(row['report_from'],row['report_to']) for row in ledger.rows}
                    if len(periods)==1 and all(next(iter(periods))):
                        file_start,file_end=next(iter(periods))
                    else:prefix+='_по_датам_сохранения'
                name=export_filename(prefix,p['shop_name'],file_start,file_end,'.'+fmt)
                path=Path(folder)/name
                exporter=export_accrual_ledger if view['kind']=='ozon' else export_wb_accrual_ledger
                await asyncio.to_thread(exporter,ledger,path,timezone=p['timezone'],shop_name=p['shop_name'])
                caption='🧾 Начисления '+label
            if not self._allowed(view,user) or finance and not repo.can_user(user,shop_id,'finance') or (
                    technical and user not in self.context.settings.owner_ids):
                raise PermissionError('Доступ изменился. Откройте раздел заново.')
            caption+='\n🏪 '+escape(p['shop_name'])+'\n'+('Даты сохранения: ' if view['kind']=='wb' else 'Период: ')+display_day(start)+' — '+display_day(p['end'])
            if view['kind']=='wb':caption+='\nПериоды самих отчётов WB указаны в файле.'
            elif view['kind']=='ozon':caption+='\nРасходы — положительно; корректировки — с исходным знаком.'
            await message.answer_document(types.FSInputFile(path),caption=caption,parse_mode='HTML')
