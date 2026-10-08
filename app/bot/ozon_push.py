"""Owner-only, private-chat setup for each cabinet's Ozon notification URL."""
from __future__ import annotations

from html import escape
import secrets

from aiogram import types
from aiogram.fsm.state import State, StatesGroup

from app.reports.dates import display_time
from app.services.ozon_push_payload import PushError, base_url, digest
from app.storage.ozon_push import OzonPushStore


class OzonPushStates(StatesGroup):
    menu = State()


class OzonPushController:
    def __init__(self, context, navigation, on_home, command_copy):
        self.ctx = context
        self.navigation = navigation
        self.on_home = on_home
        self.command_copy = command_copy

    def allowed(self, user_id):
        return (user_id in self.ctx.settings.owner_ids and
                self.ctx.repository.can_user(user_id,self.ctx.shop_id,'technical'))

    def markup(self, data, action='create', *, confirm=False):
        prefix = f'ozpush:{data["push_shop"]}:{data["push_nonce"]}:'
        button = '✅ Заменить адрес' if confirm else '🔄 Заменить адрес' if action=='rotate' else '🔗 Создать адрес'
        rows = [[types.InlineKeyboardButton(text=button,callback_data=prefix+('issue' if confirm else action))]]
        if confirm:
            rows.append([types.InlineKeyboardButton(text='⬅️ Назад',callback_data=prefix+'back')])
        rows.append([types.InlineKeyboardButton(text='🏠 Главное меню',callback_data=prefix+'home')])
        return types.InlineKeyboardMarkup(inline_keyboard=rows)

    def prerequisites(self):
        if not self.ctx.settings.ozon_push_enabled:
            raise PushError('Включите в БотХосте OZON_PUSH_ENABLED=true и укажите OZON_PUSH_BASE_URL, затем сделайте ребилд.')
        origin = base_url(self.ctx.settings.ozon_push_base_url)
        shop = self.ctx.repository.get_shop(self.ctx.shop_id)
        creds = self.ctx.settings.credentials_for_profile(shop.credential_profile)
        if not creds.has_ozon or not self.ctx.ozon_connection_id:
            raise PushError('Сначала подключите Seller API Ozon для выбранного магазина и перезапустите бота.')
        return origin,creds

    def text(self, binding):
        shop = self.ctx.repository.get_shop(self.ctx.shop_id)
        lines = ['🔵 <b>Подключение отмен Ozon</b>','🏪 '+escape(shop.name),
                 '\nБот получает дату отмены и количество товаров по FBO и FBS/rFBS.']
        if binding:
            _,creds = self.prerequisites()
            valid = bool(binding['active_client'] and binding['client_id_hash']==digest(creds.ozon_client_id.strip())
                         and binding['credential_profile']==creds.profile)
            lines.append('\n'+('✅ Адрес создан.' if valid else '⚠️ Кабинет изменился. Создайте новый адрес.'))
            lines.append('Проверка Ozon: '+(display_time(binding['last_ping_at']) if binding['last_ping_at'] else 'ещё не приходила'))
            lines.append('Последнее уведомление: '+(display_time(binding['last_received_at']) if binding['last_received_at'] else 'ещё не приходило'))
            if binding['seller_id']:
                lines.append(f'Seller ID отправителя: <code>{binding["seller_id"]}</code>')
            counts = binding['counts']
            lines.append(f'Отменённых отправлений: {binding["confirmed_postings"]} · повторов: {binding["repeated_deliveries"]}')
            issues = counts.get('invalid',0)+counts.get('conflict',0)
            if issues:
                lines.append(f'⚠️ Уведомлений для проверки: {issues}. Спорные количества исключены.')
            if binding['last_error']:
                lines.append('⚠️ '+escape(binding['last_error']))
            lines.append('\nНайдите адрес в ранее полученном сообщении. Если адрес потерян, замените его здесь и в кабинете Ozon.')
        else:
            lines.append('\nНажмите «Создать адрес», затем вставьте его в Push уведомления выбранного кабинета Ozon.')
        lines.append('\nВыберите типы <code>TYPE_FBO_POSTING_CANCELLED</code> и <code>TYPE_POSTING_CANCELLED</code>.'
                     '\nСтарые дни не восстанавливаются. Отсутствие уведомлений не означает ноль отмен.')
        return '\n'.join(lines)

    async def open(self, message, state):
        if not message.from_user or not self.allowed(message.from_user.id):
            return await message.answer('⛔ Подключение отмен доступно владельцу бота.')
        if message.chat.type != 'private':
            return await message.answer('Откройте подключение в личном чате с ботом.')
        await state.clear()
        try:
            self.prerequisites()
        except PushError as exc:
            return await self.navigation.show(message,'🔵 <b>Подключение отмен Ozon</b>\n\n'+escape(str(exc)),parse_mode='HTML')
        binding = OzonPushStore(self.ctx.repository.db).status(self.ctx.ozon_connection_id)
        data = {'push_shop':self.ctx.shop_id,'push_connection':self.ctx.ozon_connection_id,
                'push_user':message.from_user.id,'push_chat':message.chat.id,'push_nonce':secrets.token_hex(4),
                'push_old_hash':binding['token_hash'] if binding else None,'push_confirmed':False}
        await state.set_state(OzonPushStates.menu)
        sent = await self.navigation.show(message,self.text(binding),parse_mode='HTML',
            reply_markup=self.markup(data,'rotate' if binding else 'create'))
        await state.update_data(**data,push_message=sent.message_id)
        return sent

    async def handle(self, callback, state):
        data = await state.get_data()
        parts = str(callback.data or '').split(':')
        if (not isinstance(callback.message,types.Message) or not self.allowed(callback.from_user.id) or
                len(parts)!=4 or parts[1]!=str(self.ctx.shop_id) or parts[2]!=data.get('push_nonce') or
                data.get('push_shop')!=self.ctx.shop_id or data.get('push_connection')!=self.ctx.ozon_connection_id or
                data.get('push_user')!=callback.from_user.id or data.get('push_chat')!=callback.message.chat.id or
                callback.message.chat.type!='private' or data.get('push_message')!=callback.message.message_id):
            return await callback.answer('Откройте подключение заново в нужном магазине.',show_alert=True)
        action = parts[3]
        if action=='home':
            await state.clear()
            await callback.answer()
            return await self.on_home(self.command_copy(callback.message,'start','',actor_user=callback.from_user))
        if action=='rotate':
            if data.get('push_old_hash') is None:
                return await callback.answer('Откройте раздел заново.',show_alert=True)
            await state.update_data(push_confirmed=True)
            await callback.answer()
            return await callback.message.edit_text('⚠️ <b>Заменить адрес?</b>\n\nСтарый адрес перестанет работать. '
                'После замены сразу вставьте новый адрес в настройках Ozon. Сохранённые отмены останутся в БД.',
                parse_mode='HTML',reply_markup=self.markup(data,'rotate',confirm=True))
        if action=='back':
            await callback.answer()
            return await self.open(self.command_copy(callback.message,'ozon_push','',actor_user=callback.from_user),state)
        if action not in ('create','issue') or (action=='issue' and not data.get('push_confirmed')):
            return await callback.answer('Откройте раздел заново.',show_alert=True)
        if action=='create' and data.get('push_old_hash') is not None:
            return await callback.answer('Сначала подтвердите замену адреса.',show_alert=True)
        try:
            origin,creds = self.prerequisites()
            token = OzonPushStore(self.ctx.repository.db).issue(self.ctx.ozon_connection_id,self.ctx.shop_id,
                creds.ozon_client_id,creds.profile,actor_id=callback.from_user.id,system_owners=self.ctx.settings.owner_ids,
                replace_hash=data.get('push_old_hash'))
        except PushError as exc:
            return await callback.answer(str(exc),show_alert=True)
        await state.clear()  # A queued second tap cannot rotate/issue again.
        url = f'{origin}/ozon/push/{self.ctx.ozon_connection_id}/{token}'
        shop = self.ctx.repository.get_shop(self.ctx.shop_id)
        # Keep this private message in Telegram, without storing the bearer URL
        # in paged reports, navigation text, logs or FSM data.
        await callback.message.answer('🔗 <b>Адрес для отмен Ozon</b>\n🏪 '+escape(shop.name)+'\n\n<code>'+escape(url)+'</code>'
            '\n\n1. Скопируйте адрес нажатием на текст.\n2. В Ozon откройте «Push уведомления». '
            'Вставьте адрес → «Проверить» → «Сохранить».\n3. Включите '
            '<code>TYPE_FBO_POSTING_CANCELLED</code> и <code>TYPE_POSTING_CANCELLED</code>.'
            '\n\n🔒 Адрес содержит секрет доступа. Передавайте его только выбранному кабинету Ozon. '
            'Для другого магазина создайте отдельный адрес.\nПосле проверки откройте этот раздел заново: появится время проверки Ozon.',
            parse_mode='HTML',link_preview_options=types.LinkPreviewOptions(is_disabled=True))
        await self.navigation.dismiss(self.command_copy(callback.message,'ozon_push','',actor_user=callback.from_user))
        await callback.answer('Адрес создан')
