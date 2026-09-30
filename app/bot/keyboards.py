from __future__ import annotations

import hashlib
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder

HOME = '🏠 Главное меню'
BACK = '⬅️ Назад'
CANCEL = '❌ Отмена'

# One canonical Telegram button for every public slash command.  The mapping is
# intentionally explicit: tests fail when a new command is added without a UI
# entry, keeping the bot menu-first instead of slowly drifting back to CLI UX.
COMMAND_BUTTONS: dict[str, str] = {
    'start': HOME,
    'cancel': CANCEL,
    'shops': '🏪 Магазины',
    'shop': '🔁 Сменить магазин',
    'shop_add': '➕ Добавить магазин',
    'shop_profile': '🔐 Профиль ключей',
    'shop_archive': '🗄 Архивировать магазин',
    'shop_archived': '🗂 Архив магазинов',
    'shop_restore': '♻️ Вернуть магазин',
    'shop_delete': '🗑 Удалить магазин',
    'profiles': '🗝 Профили окружения',
    'users': '👥 Пользователи',
    'user_add': '➕ Дать доступ',
    'user_remove': '➖ Отозвать доступ',
    'my_access': '🙋 Мой доступ',
    'export': '📤 Скачать данные',
    'backup': '💾 Создать backup',
    'backups': '🗂 История backup',
    'restore': '♻️ Восстановить backup',
    'setup': '🧩 Мастер настройки',
    'readiness': '✅ Готовность',
    'connect_check': '🔌 Проверить API',
    'help': '❓ Как подключить API',
    'demo_on': '🧪 Включить демо',
    'demo_off': '🟢 Выключить демо',
    'settings': '⚙️ Настройки',
    'import_costs': '📥 Импорт себестоимости',
    'link': '🔗 Связать WB ↔ Ozon',
    'day': '🗓 Другой день',
    'backfill': '📥 Догрузить историю',
    'products': '🏆 Товары',
    'stocks': '📦 Остатки',
    'finance': '💰 Финансы',
    'ads': '📣 Реклама',
    'management': '📈 Результат бизнеса',
    'sku_finance': '🧾 Экономика SKU',
    'reconcile': '🔎 Сверить данные',
    'cost': '💲 Задать себестоимость',
    'alerts': '🚨 Проблемы',
    'actions': '🎯 Что делать',
    'action_history': '📋 История решений',
    'action_ack': '✅ Принять действие',
    'action_snooze': '⏰ Отложить действие',
    'supply': '🚚 План поставок',
    'inbound': '📥 Поставки в пути',
    'inbound_refresh': '🔄 Обновить поставки в пути',
    'forecast_quality': '🎯 Точность прогноза',
    'supply_calibration': '🧠 Самокалибровка',
    'supply_calibration_refresh': '🔄 Пересчитать калибровку',
    'promotions': '📅 Акции',
    'promotions_refresh': '🔄 Обновить акции',
    'supply_refresh': '🔄 Обновить план',
    'supply_sku': '🔍 Поставка по SKU',
    'supply_settings': '⚙️ Настройки поставок',
    'supply_defaults': '🧰 Defaults поставок',
    'supply_set': '✏️ Настроить SKU',
    'status': '🔌 Статус API',
    'health': '❤️ Техсостояние',
    'jobs': '🔁 Ошибки и повторы',
    'job_retry': '🔁 Повторить retry-задачу',
    'diagnostics': '🧪 Техдиагностика',
}

MENU_REPORTS = '📊 Продажи и отчёты'
MENU_PRODUCTS = '📦 Товары и остатки'
MENU_MONEY = '💰 Деньги'
MENU_SUPPLY = '🚚 Поставки'
MENU_CONTROL = '🎯 Что требует внимания'
MENU_SHOP = '⚙️ Магазин'
MENU_SERVICE = '🛠 Ещё'


def _role(role: str | None) -> str:
    value = (role or 'viewer').lower()
    return value if value in {'viewer', 'analyst', 'owner'} else 'viewer'


def _build(buttons: tuple[str, ...] | list[str], *, columns: int = 2):
    kb = ReplyKeyboardBuilder()
    for text in buttons:
        kb.button(text=text)
    kb.adjust(columns)
    return kb.as_markup(resize_keyboard=True)


def main_keyboard(role: str | None = 'owner'):
    role = _role(role)
    buttons = [MENU_REPORTS, MENU_PRODUCTS, MENU_MONEY, MENU_SUPPLY, MENU_CONTROL, MENU_SHOP, MENU_SERVICE]
    return _build(buttons, columns=2)


def reports_keyboard(role: str | None = 'owner', *, system_owner: bool = False):
    role = _role(role)
    buttons = ['📊 Вчера', '📅 Неделя', '📅 Месяц', COMMAND_BUTTONS['day'], '📜 История']
    if role in {'analyst', 'owner'}:
        buttons += ['📊 Сегодня', '🔄 Обновить вчера', COMMAND_BUTTONS['backfill']]
    buttons += [BACK, HOME]
    return _build(buttons)


def products_keyboard(role: str | None = 'owner', *, system_owner: bool = False):
    role = _role(role)
    buttons = [COMMAND_BUTTONS['products'], COMMAND_BUTTONS['stocks'], COMMAND_BUTTONS['sku_finance']]
    if role in {'analyst', 'owner'}:
        buttons += ['🔄 Обновить остатки', COMMAND_BUTTONS['import_costs']]
    buttons += [BACK, HOME]
    return _build(buttons)


def money_keyboard(role: str | None = 'owner', *, system_owner: bool = False):
    role = _role(role)
    buttons = [COMMAND_BUTTONS['finance'], COMMAND_BUTTONS['ads'],
               COMMAND_BUTTONS['management'], COMMAND_BUTTONS['reconcile']]
    if role in {'analyst','owner'}:
        buttons += ['🔄 Обновить финансы', '🔄 Обновить рекламу']
    buttons += [BACK, HOME]
    return _build(buttons)


def supply_keyboard(role: str | None = 'owner', *, system_owner: bool = False):
    role = _role(role)
    buttons = [COMMAND_BUTTONS['supply'], COMMAND_BUTTONS['inbound'], COMMAND_BUTTONS['promotions'],
               COMMAND_BUTTONS['forecast_quality'], COMMAND_BUTTONS['supply_settings']]
    if role in {'analyst', 'owner'}:
        buttons += [COMMAND_BUTTONS['inbound_refresh'], COMMAND_BUTTONS['promotions_refresh']]
    buttons += [BACK, HOME]
    return _build(buttons)


def control_keyboard(role: str | None = 'owner', *, system_owner: bool = False):
    role = _role(role)
    buttons = [COMMAND_BUTTONS['actions'], COMMAND_BUTTONS['alerts'], COMMAND_BUTTONS['status'], COMMAND_BUTTONS['action_history']]
    if role in {'analyst', 'owner'}:
        buttons += [COMMAND_BUTTONS['jobs']]
    buttons += [BACK, HOME]
    return _build(buttons)


def shop_keyboard(role: str | None = 'owner', *, system_owner: bool = False):
    role = _role(role)
    buttons = [COMMAND_BUTTONS['shop'], COMMAND_BUTTONS['settings'], COMMAND_BUTTONS['readiness'], COMMAND_BUTTONS['help']]
    if role in {'analyst','owner'}:
        buttons += [COMMAND_BUTTONS['connect_check']]
    if role == 'owner':
        buttons += [COMMAND_BUTTONS['setup'], COMMAND_BUTTONS['users']]
    buttons += [BACK, HOME]
    return _build(buttons)


def service_keyboard(role: str | None = 'owner', *, system_owner: bool = False):
    role = _role(role)
    buttons = [COMMAND_BUTTONS['export']]
    if role in {'analyst','owner'}:
        buttons += [COMMAND_BUTTONS['diagnostics']]
    if system_owner:
        buttons += [COMMAND_BUTTONS['health'], COMMAND_BUTTONS['backup'], COMMAND_BUTTONS['backups'],
                    COMMAND_BUTTONS['restore'], COMMAND_BUTTONS['profiles'], COMMAND_BUTTONS['shop_archived']]
    buttons += [BACK, HOME]
    return _build(buttons)


def input_keyboard():
    return _build([CANCEL, HOME], columns=2)


def shop_picker_keyboard(shops, action: str, *, current_shop_id: int | None = None):
    """Inline one-tap picker for shop lifecycle actions."""
    kb=InlineKeyboardBuilder()
    for shop in shops:
        shop_id=int(shop.id if hasattr(shop,'id') else shop['id'])
        name=str(shop.name if hasattr(shop,'name') else shop['name'])
        mark='✅ ' if current_shop_id is not None and shop_id==int(current_shop_id) else ''
        kb.button(text=f'{mark}{name}',callback_data=f'shop:{action}:{shop_id}')
    kb.button(text='❌ Отмена',callback_data='shop:cancel')
    kb.adjust(1)
    return kb.as_markup()


def shop_confirm_keyboard(action: str, shop_id: int):
    labels={
        'archive': '🗄 Да, архивировать',
        'delete': '🗑 Да, удалить навсегда',
    }
    kb=InlineKeyboardBuilder()
    kb.button(text=labels.get(action,'✅ Подтвердить'),callback_data=f'shop:{action}_confirm:{int(shop_id)}')
    kb.button(text='❌ Отмена',callback_data='shop:cancel')
    kb.adjust(1)
    return kb.as_markup()


def date_picker_keyboard():
    kb=InlineKeyboardBuilder()
    for days,label in ((1,'Вчера'),(2,'2 дня назад'),(3,'3 дня назад'),(7,'7 дней назад')):
        kb.button(text=label,callback_data=f'day:relative:{days}')
    kb.button(text='⌨️ Ввести дату',callback_data='day:custom')
    kb.button(text='❌ Отмена',callback_data='flow:cancel')
    kb.adjust(2,2,1,1)
    return kb.as_markup()


def export_picker_keyboard():
    kb=InlineKeyboardBuilder()
    for days in (7,30,90):
        kb.button(text=f'Excel · {days} дн.',callback_data=f'export:{days}:xlsx')
    kb.button(text='CSV · 30 дн.',callback_data='export:30:csv')
    kb.button(text='CSV · 90 дн.',callback_data='export:90:csv')
    kb.button(text='❌ Отмена',callback_data='flow:cancel')
    kb.adjust(1)
    return kb.as_markup()


def retry_jobs_keyboard(rows):
    kb=InlineKeyboardBuilder()
    added=0
    for row in rows:
        if str(row.get('status')) not in {'dead','pending'}:
            continue
        job_id=int(row['id'])
        kind=str(row.get('job_type') or 'задача')
        kb.button(text=f'🔁 #{job_id} · {kind[:28]}',callback_data=f'job:retry:{job_id}')
        added+=1
        if added>=8:
            break
    kb.button(text='❌ Закрыть',callback_data='flow:cancel')
    kb.adjust(1)
    return kb.as_markup()


def action_token(action_key: str) -> str:
    return hashlib.sha1(action_key.encode('utf-8')).hexdigest()[:12]


def action_center_keyboard(center, *, page: int=0, per_page: int=5, can_operate: bool=True):
    kb=InlineKeyboardBuilder()
    start=max(0,page)*per_page
    rows=list(center.items[start:start+per_page])
    if can_operate:
        for item in rows:
            token=action_token(item.action_key)
            title=item.title.replace('🚨 ','').replace('🚚 ','').replace('📣 ','')[:28]
            kb.button(text=f'✅ {title}',callback_data=f'action:ack:{token}')
            kb.button(text='⏰ 24 ч.',callback_data=f'action:snooze:{token}:24')
    pages=max(1,(len(center.items)+per_page-1)//per_page)
    nav=[]
    if page>0: nav.append(('⬅️',f'actions:page:{page-1}'))
    if page+1<pages: nav.append(('➡️',f'actions:page:{page+1}'))
    for label,data in nav:
        kb.button(text=label,callback_data=data)
    kb.button(text='🔄 Обновить',callback_data=f'actions:page:{page}')
    kb.adjust(2)
    return kb.as_markup()


def alert_actions_keyboard(*, can_operate: bool=True):
    kb=InlineKeyboardBuilder()
    kb.button(text='🎯 Что делать',callback_data='quick:actions')
    if can_operate:
        kb.button(text='🔌 Проверить API',callback_data='quick:connect')
    kb.adjust(1)
    return kb.as_markup()


def backfill_source_keyboard(*, has_ozon: bool, has_wb: bool):
    kb=InlineKeyboardBuilder()
    if has_ozon:
        kb.button(text='🟣 Ozon',callback_data='backfill:source:ozon')
    if has_wb:
        kb.button(text='🔵 Wildberries',callback_data='backfill:source:wildberries')
    if has_ozon and has_wb:
        kb.button(text='🟣🔵 Оба маркетплейса',callback_data='backfill:source:all')
    kb.button(text='❌ Отмена',callback_data='backfill:cancel')
    kb.adjust(1)
    return kb.as_markup()


def backfill_running_keyboard():
    kb=InlineKeyboardBuilder()
    kb.button(text='🛑 Остановить загрузку',callback_data='backfill:stop')
    kb.adjust(1)
    return kb.as_markup()


def backfill_period_keyboard(source: str):
    clean=(source or 'all').strip().lower()
    kb=InlineKeyboardBuilder()
    for days in (7,30,60,90):
        kb.button(text=f'{days} дней',callback_data=f'backfill:period:{clean}:{days}')
    kb.button(text='📅 Свой период',callback_data=f'backfill:custom:{clean}')
    kb.button(text='⬅️ Назад',callback_data='backfill:source_picker')
    kb.button(text='❌ Отмена',callback_data='backfill:cancel')
    kb.adjust(2,2,1,2)
    return kb.as_markup()
