"""Button-only report periods with shop/user binding and stale-button protection."""
from calendar import monthrange
from datetime import date, timedelta
import secrets

from aiogram import types
from aiogram.utils.keyboard import InlineKeyboardBuilder
from app.access import action_permission


MONTHS=('Январь','Февраль','Март','Апрель','Май','Июнь','Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь')


def validate_period(start: date, end: date, today: date):
    if start>end or end>=today or not 1<=(end-start).days+1<=31:
        raise ValueError('Выберите от 1 до 31 завершённого дня.')
    return (end-start).days+1


class PeriodPicker:
    def __init__(self,ctx,navigation,today,actions,on_cancel=None):
        self.ctx=ctx; self.navigation=navigation; self.today=today; self.actions=actions
        self.running=set()
        self.on_cancel=on_cancel

    def prefix(self,data):
        return f'p:{data["picker_shop"]}:{data["picker_user"]}:{data["picker_nonce"]}:{data["picker_action"]}'

    def presets(self,data):
        kb=InlineKeyboardBuilder(); p=self.prefix(data)
        for days,label in ((1,'Вчера'),(7,'7 дней'),(14,'14 дней'),(30,'30 дней')):
            kb.button(text=label,callback_data=f'{p}:n:{days}')
        kb.button(text='📅 Выбрать один день',callback_data=f'{p}:one:0')
        kb.button(text='🗓 Выбрать даты начала и конца',callback_data=f'{p}:range:0')
        kb.button(text='❌ Отмена',callback_data=f'{p}:cancel:0')
        kb.adjust(2,2,1,1,1)
        return kb.as_markup()

    def calendar(self,data,month):
        p=self.prefix(data); kb=InlineKeyboardBuilder()
        kb.row(types.InlineKeyboardButton(text=f'{MONTHS[month.month-1]} {month.year}',callback_data=f'{p}:noop:0'))
        kb.row(*(types.InlineKeyboardButton(text=d,callback_data=f'{p}:noop:0') for d in ('Пн','Вт','Ср','Чт','Пт','Сб','Вс')))
        first_weekday,total=monthrange(month.year,month.month)
        cells=[]; today=self.today()
        minimum=date.fromisoformat(data['picker_start']) if data.get('picker_step')=='end' else date(2000,1,1)
        maximum=min(today-timedelta(days=1),minimum+timedelta(days=30)) if data.get('picker_step')=='end' else today-timedelta(days=1)
        for _ in range(first_weekday):cells.append(types.InlineKeyboardButton(text=' ',callback_data=f'{p}:noop:0'))
        for day in range(1,total+1):
            value=month.replace(day=day); enabled=minimum<=value<=maximum
            cells.append(types.InlineKeyboardButton(text=str(day) if enabled else '·',
                callback_data=f'{p}:d:{value:%Y%m%d}' if enabled else f'{p}:noop:0'))
        while len(cells)%7:cells.append(types.InlineKeyboardButton(text=' ',callback_data=f'{p}:noop:0'))
        for i in range(0,len(cells),7):kb.row(*cells[i:i+7])
        previous=(month-timedelta(days=1)).replace(day=1)
        following=(month+timedelta(days=32)).replace(day=1)
        arrows=[]
        if previous.year>=2000 and previous.replace(day=monthrange(previous.year,previous.month)[1])>=minimum:
            arrows.append(types.InlineKeyboardButton(text='◀️',callback_data=f'{p}:m:{previous:%Y%m}'))
        if following<=maximum:arrows.append(types.InlineKeyboardButton(text='▶️',callback_data=f'{p}:m:{following:%Y%m}'))
        if arrows:kb.row(*arrows)
        kb.row(types.InlineKeyboardButton(text='⬅️ К выбору периода',callback_data=f'{p}:back:0'),
               types.InlineKeyboardButton(text='❌ Отмена',callback_data=f'{p}:cancel:0'))
        return kb.as_markup()

    async def launch(self,message,state,action):
        title,permission,_=self.actions[action]
        if not message.from_user or not all(self.ctx.repository.can_user(message.from_user.id,self.ctx.shop_id,p)
                for p in (permission,action_permission(action))):
            return await message.answer('⛔ Недостаточно прав для этого действия.')
        await state.clear()
        # Keep legacy one-line input supported, while the normal flow needs no typing.
        from .handlers import MenuInputStates
        await state.set_state(MenuInputStates.waiting_value)
        data={'menu_action':action,'picker_action':action,'picker_shop':self.ctx.shop_id,
              'picker_user':message.from_user.id,'picker_nonce':secrets.token_hex(3)}
        await state.update_data(**data)
        await self.navigation.show(message,f'{title}\nВыберите период кнопками. Даты вводить не нужно.',
            parse_mode='HTML',reply_markup=self.presets(data))

    async def handle(self,callback,state,command_copy):
        if not isinstance(callback.message,types.Message):return await callback.answer('Сообщение недоступно.')
        parts=(callback.data or '').split(':')
        if len(parts)!=7:return await callback.answer('Некорректная кнопка.',show_alert=True)
        _,shop,user,nonce,action,kind,value=parts
        data=await state.get_data()
        try: bound=int(shop)==self.ctx.shop_id and int(user)==callback.from_user.id
        except ValueError:bound=False
        if not bound or data.get('picker_nonce')!=nonce or data.get('picker_action')!=action or data.get('picker_shop')!=self.ctx.shop_id:
            return await callback.answer('Этот выбор периода устарел. Откройте функцию заново.',show_alert=True)
        if action not in self.actions:return await callback.answer('Неизвестное действие.',show_alert=True)
        title,permission,handler=self.actions[action]
        if not all(self.ctx.repository.can_user(callback.from_user.id,self.ctx.shop_id,p)
                for p in (permission,action_permission(action))):
            return await callback.answer('Недостаточно прав.',show_alert=True)
        if kind=='noop':return await callback.answer()
        if kind=='cancel':
            await state.clear(); await callback.answer('Отменено')
            if self.on_cancel:
                message=command_copy(callback.message,action,'',actor_user=callback.from_user)
                return await self.on_cancel(message)
            await callback.message.edit_text('↩️ Действие отменено. Выберите раздел в главном меню.')
            return
        if kind=='back':
            await state.update_data(picker_step=None,picker_start=None,picker_end=None)
            await callback.answer()
            return await callback.message.edit_text(f'{title}\nВыберите период кнопками.',reply_markup=self.presets(data),parse_mode='HTML')
        if kind in {'one','range','m'}:
            if kind=='m':
                if data.get('picker_step') not in {'one','start','end'}:return await callback.answer('Сначала выберите способ выбора дат.')
                try:month=date(int(value[:4]),int(value[4:]),1)
                except ValueError:return await callback.answer('Некорректный месяц.')
                if not 2000<=month.year<=self.today().year:return await callback.answer('Этот месяц недоступен.')
            else:
                data['picker_step']='one' if kind=='one' else 'start'
                await state.update_data(picker_step=data['picker_step'],picker_start=None,picker_end=None)
                month=(self.today()-timedelta(days=1)).replace(day=1)
            step={'one':'Выберите день','start':'Выберите первую дату','end':'Выберите последнюю дату'}[data['picker_step']]
            await callback.answer()
            return await callback.message.edit_text(f'{title}\n{step}. Максимум 31 день.',
                reply_markup=self.calendar(data,month),parse_mode='HTML')
        if kind=='n':
            try:days=int(value)
            except ValueError:return await callback.answer('Некорректный период.')
            if days not in {1,7,14,30}:return await callback.answer('Некорректный период.')
            end=self.today()-timedelta(days=1);start=end-timedelta(days=days-1)
        elif kind=='d':
            try:selected=date(int(value[:4]),int(value[4:6]),int(value[6:8]))
            except ValueError:return await callback.answer('Некорректная дата.')
            if selected>=self.today() or selected<date(2000,1,1):return await callback.answer('Выберите завершённый день.')
            if data.get('picker_step')=='start':
                data.update(picker_start=selected.isoformat(),picker_step='end')
                await state.update_data(picker_start=selected.isoformat(),picker_step='end')
                await callback.answer()
                return await callback.message.edit_text(f'{title}\nНачало: {selected:%d.%m.%Y}\nТеперь выберите последнюю дату.',
                    reply_markup=self.calendar(data,selected.replace(day=1)),parse_mode='HTML')
            if data.get('picker_step')=='one':start=end=selected
            elif data.get('picker_step')=='end':start=date.fromisoformat(data['picker_start']);end=selected
            else:return await callback.answer('Выберите период заново.')
        elif kind=='run':
            if not data.get('picker_start') or not data.get('picker_end'):return await callback.answer('Сначала выберите период.')
            start=date.fromisoformat(data['picker_start']);end=date.fromisoformat(data['picker_end'])
        else:return await callback.answer('Неизвестная кнопка.')
        try:days=validate_period(start,end,self.today())
        except ValueError as exc:return await callback.answer(str(exc),show_alert=True)
        if kind=='run':
            key=(self.ctx.shop_id,callback.from_user.id,nonce)
            if key in self.running:return await callback.answer('Уже выполняется.')
            self.running.add(key)
            try:
                await state.clear();await callback.answer('Запускаю…')
                await callback.message.edit_text(f'{title}\n{start:%d.%m.%Y} — {end:%d.%m.%Y}\n⏳ Выполняю…',parse_mode='HTML')
                message=command_copy(callback.message,action,f'{days} {end.isoformat()}',actor_user=callback.from_user)
                await handler(message)
                await self.navigation.dismiss(message)
            finally:self.running.discard(key)
            return
        await state.update_data(picker_start=start.isoformat(),picker_end=end.isoformat(),picker_step=None)
        p=self.prefix(data);kb=InlineKeyboardBuilder()
        kb.button(text='🔄 Обновить' if permission=='operate' else '📄 Показать',callback_data=f'{p}:run:0')
        kb.button(text='⬅️ Другой период',callback_data=f'{p}:back:0')
        kb.button(text='❌ Отмена',callback_data=f'{p}:cancel:0');kb.adjust(1)
        await callback.answer()
        note='Загрузит данные из API площадок. Это может занять несколько минут.' if permission=='operate' else 'Покажет уже сохранённые данные без загрузки из API.'
        await callback.message.edit_text(f'{title}\nПериод: <b>{start:%d.%m.%Y} — {end:%d.%m.%Y}</b> · {days} дн.\n{note}',
            parse_mode='HTML',reply_markup=kb.as_markup())
