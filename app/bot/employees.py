"""Owner-only employee editor with shop/user/message-bound button flows."""
from __future__ import annotations

from html import escape
import secrets

from aiogram import types
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.state import State, StatesGroup

from app.access import ASSIGNABLE_ROLES, role_label
from app.reports.text import page_slice


class EmployeeStates(StatesGroup):
    menu = State()
    waiting_id = State()


class EmployeeController:
    def __init__(self, context, navigation, on_home, command_copy):
        self.ctx=context; self.navigation=navigation
        self.on_home=on_home; self.command_copy=command_copy

    def _button(self, data, text, action, value=''):
        callback=f'staff:{data["employee_shop"]}:{data["employee_nonce"]}:{action}:{value}'
        if len(callback.encode())>64:
            raise ValueError('Employee callback exceeds Telegram limit')
        return types.InlineKeyboardButton(text=text,callback_data=callback)

    def _markup(self, data, rows):
        rows.append([self._button(data,'🏠 Главное меню','home')])
        return types.InlineKeyboardMarkup(inline_keyboard=rows)

    def _owner(self, user):
        return self.ctx.repository.can_user(user,self.ctx.shop_id,'manage')

    async def _new_flow(self, message, state):
        await state.clear(); await state.set_state(EmployeeStates.menu)
        data={'employee_shop':self.ctx.shop_id,'employee_user':message.from_user.id,
              'employee_chat':message.chat.id,'employee_nonce':secrets.token_hex(3),'employee_page':0}
        await state.update_data(**data)
        return data

    def _list(self, data, page=0):
        users=self.ctx.repository.users_for_shop(self.ctx.shop_id)
        visible,page,pages=page_slice(users,page)
        shop=self.ctx.repository.get_shop(self.ctx.shop_id)
        lines=['👥 <b>Сотрудники</b>','🏪 '+escape(shop.name),f'Всего: {len(users)}']
        rows=[]
        for row in visible:
            uid=int(row['telegram_user_id']); name=str(row.get('display_name') or uid)
            label=role_label(row['role'])
            lines.append(f'\n<b>{escape(name)}</b> · {label}\nTelegram ID: <code>{uid}</code>')
            rows.append([self._button(data,name[:55]+' · '+label,'view',str(uid))])
        if pages>1:
            rows.append([self._button(data,'◀️','list',str(max(0,page-1))),
                         self._button(data,f'{page+1}/{pages}','list',str(page)),
                         self._button(data,'▶️','list',str(min(pages-1,page+1)))])
        rows.append([self._button(data,'➕ Добавить сотрудника','add')])
        lines.append('\nВыберите человека, чтобы изменить роль или отозвать доступ.')
        return '\n'.join(lines),self._markup(data,rows),page

    async def open(self, message, state):
        if not message.from_user or not self._owner(message.from_user.id):
            return await message.answer('⛔ Управлять сотрудниками может только владелец магазина.')
        data=await self._new_flow(message,state)
        text,markup,_=self._list(data)
        sent=await self.navigation.show(message,text,parse_mode='HTML',reply_markup=markup)
        await state.update_data(employee_message=sent.message_id)
        return sent

    async def add(self, message, state):
        if not message.from_user or not self._owner(message.from_user.id):
            return await message.answer('⛔ Управлять сотрудниками может только владелец магазина.')
        data=await self._new_flow(message,state)
        await state.set_state(EmployeeStates.waiting_id)
        text=self._id_prompt()
        sent=await self.navigation.show(message,text,parse_mode='HTML',
            reply_markup=self._markup(data,[[self._button(data,'⬅️ К сотрудникам','list','0')]]))
        await state.update_data(employee_message=sent.message_id)
        return sent

    @staticmethod
    def _id_prompt():
        return ('➕ <b>Добавить сотрудника</b>\nОтправьте его числовой Telegram ID.\n'
                'Человек может нажать /start в этом боте и узнать свой ID. Затем выберете роль кнопкой.')

    def _role_form(self, data, uid):
        shop=self.ctx.repository.get_shop(self.ctx.shop_id)
        text=(f'👤 Telegram ID: <code>{uid}</code>\n🏪 {escape(shop.name)}\n\n'
              'Выберите роль:\n'
              'Владелец — все рабочие разделы и управление сотрудниками.\n'
              'Бухгалтер — отчёты, финансы, себестоимость и рабочие настройки.\n'
              'Менеджер — продажи, остатки, реклама и поставки.\n'
              'Технические функции доступны владельцу всего бота.')
        rows=[[self._button(data,role_label(role),'role',role)] for role in ASSIGNABLE_ROLES]
        rows.append([self._button(data,'⬅️ К сотрудникам','list',str(data.get('employee_page',0)))])
        return text,self._markup(data,rows)

    async def input_id(self, message, state):
        data=await state.get_data()
        if (not message.from_user or not self._owner(message.from_user.id) or
            data.get('employee_user')!=message.from_user.id or data.get('employee_shop')!=self.ctx.shop_id or
            data.get('employee_chat')!=message.chat.id):
            await state.clear()
            return await message.answer('⛔ Откройте «Сотрудники» заново в нужном магазине.')
        try:
            uid=int(str(message.text or '').strip())
            if not 0<uid<2**63: raise ValueError
        except ValueError:
            return await message.answer('Введите только положительный числовой Telegram ID, без @username.')
        previous=self.ctx.repository.role_for_user(uid,self.ctx.shop_id)
        await state.set_state(EmployeeStates.menu)
        data.update(employee_target=uid,employee_previous=previous)
        await state.update_data(**data)
        text,markup=self._role_form(data,uid)
        try:
            return await message.bot.edit_message_text(text,chat_id=message.chat.id,
                message_id=data['employee_message'],parse_mode='HTML',reply_markup=markup)
        except TelegramBadRequest:
            sent=await self.navigation.show(message,text,parse_mode='HTML',reply_markup=markup)
            await state.update_data(employee_message=sent.message_id)
            return sent

    async def _edit(self, callback, text, markup):
        try:
            return await callback.message.edit_text(text,parse_mode='HTML',reply_markup=markup)
        except TelegramBadRequest as exc:
            if 'message is not modified' not in str(exc).lower():
                return await callback.answer('Откройте «Сотрудники» заново.',show_alert=True)
        except TelegramAPIError:
            return await callback.answer('Не удалось изменить сообщение. Откройте «Сотрудники» заново.',show_alert=True)

    async def handle(self, callback, state):
        if not isinstance(callback.message,types.Message):
            return await callback.answer('Сообщение недоступно.',show_alert=True)
        parts=str(callback.data or '').split(':')
        if len(parts)!=5:
            return await callback.answer('Некорректная кнопка.',show_alert=True)
        _,shop,nonce,action,value=parts
        if action=='home':
            await state.clear();await callback.answer()
            return await self.on_home(self.command_copy(callback.message,'start','',actor_user=callback.from_user))
        data=await state.get_data()
        if (not self._owner(callback.from_user.id) or str(self.ctx.shop_id)!=shop or
            data.get('employee_nonce')!=nonce or data.get('employee_user')!=callback.from_user.id or
            data.get('employee_chat')!=callback.message.chat.id or data.get('employee_message')!=callback.message.message_id):
            return await callback.answer('Нет доступа или эта кнопка устарела. Откройте «Сотрудники» заново.',show_alert=True)
        if action=='list':
            try:page=int(value)
            except ValueError:return await callback.answer('Некорректная страница.',show_alert=True)
            text,markup,page=self._list(data,page)
            await state.set_state(EmployeeStates.menu)
            await state.update_data(employee_page=page,employee_target=None,employee_previous=None)
        elif action=='add':
            await state.set_state(EmployeeStates.waiting_id)
            text=self._id_prompt()
            markup=self._markup(data,[[self._button(data,'⬅️ К сотрудникам','list',str(data.get('employee_page',0)))]])
        elif action=='view':
            try:uid=int(value)
            except ValueError:return await callback.answer('Некорректный сотрудник.',show_alert=True)
            row=next((r for r in self.ctx.repository.users_for_shop(self.ctx.shop_id) if r['telegram_user_id']==uid),None)
            if not row:return await callback.answer('Доступ уже отозван. Вернитесь к списку.',show_alert=True)
            await state.update_data(employee_target=uid,employee_previous=row['role'])
            text=f'👤 <b>{escape(str(row.get("display_name") or uid))}</b>\nTelegram ID: <code>{uid}</code>\nРоль: <b>{role_label(row["role"])}</b>'
            rows=[]
            if uid in self.ctx.settings.owner_ids:
                text+='\n\nВладелец всего бота. Его доступ задаётся на хостинге.'
            else:
                rows.append([self._button(data,'✏️ Изменить роль','choose'),self._button(data,'➖ Отозвать доступ','remove')])
            rows.append([self._button(data,'⬅️ К сотрудникам','list',str(data.get('employee_page',0)))])
            markup=self._markup(data,rows)
        elif action in {'choose','role','remove','confirm'}:
            uid=data.get('employee_target')
            if not uid:return await callback.answer('Выберите сотрудника заново.',show_alert=True)
            if action=='choose':
                text,markup=self._role_form(data,uid)
            elif action=='remove':
                text=f'Отозвать доступ к этому магазину у <code>{uid}</code>?\nДоступ к другим магазинам сохранится.'
                markup=self._markup(data,[[self._button(data,'➖ Да, отозвать','confirm'),
                                           self._button(data,'⬅️ Назад','view',str(uid))]])
            else:
                if action=='role' and value not in ASSIGNABLE_ROLES:
                    return await callback.answer('Выберите роль кнопкой.',show_alert=True)
                role=value if action=='role' else None
                try:
                    self.ctx.repository.manage_employee_access(callback.from_user.id,self.ctx.shop_id,uid,role,
                        protected_user_ids=self.ctx.settings.owner_ids,expected_role=data.get('employee_previous'))
                except (ValueError,PermissionError) as exc:
                    return await callback.answer(str(exc),show_alert=True)
                await callback.answer('Роль сохранена' if role else 'Доступ отозван')
                if uid==callback.from_user.id:
                    await state.clear()
                    return await self._edit(callback,'✅ Ваш доступ изменён. Откройте главное меню.',self._markup(data,[]))
                text,markup,page=self._list(data,data.get('employee_page',0))
                await state.update_data(employee_target=None,employee_previous=None,employee_page=page)
                return await self._edit(callback,text,markup)
        else:
            return await callback.answer('Некорректная кнопка.',show_alert=True)
        await callback.answer()
        return await self._edit(callback,text,markup)
