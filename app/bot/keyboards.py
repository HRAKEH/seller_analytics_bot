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
    'shops': '🏪 Список магазинов',
    'shop': '🔁 Выбрать магазин',
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
    'export': '📤 Экспорт данных',
    'backup': '💾 Создать backup',
    'backups': '🗂 История backup',
    'restore': '♻️ Восстановить backup',
    'setup': '🧩 Мастер настройки',
    'readiness': '✅ Что настроено',
    'connect_check': '🔌 Проверить подключения',
    'help': '🧭 Как подключить магазин',
    'demo_on': '🧪 Включить демо',
    'demo_off': '🟢 Выключить демо',
    'settings': '⚙️ Настройки магазина',
    'import_costs': '📥 Импорт себестоимости',
    'link': '🔗 Связать WB ↔ Ozon',
    'day': '🗓 Отчёт по дате',
    'backfill': '📥 Загрузить историю',
    'products': '🏆 Топ товаров',
    'stocks': '📦 Остатки',
    'finance': '💰 Финансы',
    'ads': '📣 Реклама',
    'management': '📈 Результат магазина',
    'sku_finance': '🧾 Экономика по товарам',
    'reconcile': '🔎 Проверка расхождений',
    'cost': '💲 Задать себестоимость',
    'alerts': '🚨 Алерты',
    'actions': '🎯 Что делать сегодня',
    'action_history': '📋 История действий',
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
    'status': '📡 Состояние данных',
    'health': '❤️ Проверка бота',
    'jobs': '🧰 Фоновые задачи',
    'job_retry': '🔁 Повторить retry-задачу',
    'diagnostics': '🧪 Техническая диагностика',
}

MENU_REPORTS = '📊 Отчёты'
MENU_PRODUCTS = '📦 Товары'
MENU_MONEY = '💰 Деньги и реклама'
MENU_SUPPLY = '🚚 Поставки'
MENU_CONTROL = '🚨 Проблемы'
MENU_SHOP = '🏪 Магазин'
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
    buttons = [
        '📊 Вчера', '📅 Неделя',
        '📅 Месяц', '📜 История',
    ]
    if role in {'analyst', 'owner'}:
        buttons += ['🔄 Обновить вчера', COMMAND_BUTTONS['backfill']]
    buttons += [BACK, HOME]
    return _build(buttons)


def products_keyboard(role: str | None = 'owner', *, system_owner: bool = False):
    role = _role(role)
    buttons = [
        COMMAND_BUTTONS['products'], COMMAND_BUTTONS['stocks'],
        COMMAND_BUTTONS['sku_finance'],
    ]
    if role in {'analyst', 'owner'}:
        buttons += [COMMAND_BUTTONS['import_costs']]
    buttons += [BACK, HOME]
    return _build(buttons)


def money_keyboard(role: str | None = 'owner', *, system_owner: bool = False):
    buttons = [
        COMMAND_BUTTONS['finance'], COMMAND_BUTTONS['ads'],
        COMMAND_BUTTONS['management'], COMMAND_BUTTONS['reconcile'],
        BACK, HOME,
    ]
    return _build(buttons)


def supply_keyboard(role: str | None = 'owner', *, system_owner: bool = False):
    role = _role(role)
    buttons = [
        COMMAND_BUTTONS['supply'], COMMAND_BUTTONS['inbound'],
        COMMAND_BUTTONS['promotions'], COMMAND_BUTTONS['forecast_quality'],
    ]
    if role in {'analyst', 'owner'}:
        buttons += [COMMAND_BUTTONS['inbound_refresh'], COMMAND_BUTTONS['promotions_refresh']]
    buttons += [BACK, HOME]
    return _build(buttons)


def control_keyboard(role: str | None = 'owner', *, system_owner: bool = False):
    buttons = [
        COMMAND_BUTTONS['actions'], COMMAND_BUTTONS['alerts'],
        COMMAND_BUTTONS['status'], COMMAND_BUTTONS['action_history'],
        BACK, HOME,
    ]
    return _build(buttons)


def shop_keyboard(role: str | None = 'owner', *, system_owner: bool = False):
    role = _role(role)
    buttons = [
        COMMAND_BUTTONS['shops'], COMMAND_BUTTONS['shop'],
        COMMAND_BUTTONS['settings'], COMMAND_BUTTONS['readiness'],
    ]
    if role in {'analyst','owner'}:
        buttons += [COMMAND_BUTTONS['connect_check']]
    if role == 'owner':
        buttons += [COMMAND_BUTTONS['setup'], COMMAND_BUTTONS['users']]
    if system_owner:
        buttons += [
            COMMAND_BUTTONS['shop_archive'], COMMAND_BUTTONS['shop_archived'],
            COMMAND_BUTTONS['shop_restore'], COMMAND_BUTTONS['shop_delete'],
        ]
    buttons += [BACK, HOME]
    return _build(buttons)


def service_keyboard(role: str | None = 'owner', *, system_owner: bool = False):
    role = _role(role)
    buttons = [COMMAND_BUTTONS['export']]
    if role in {'analyst','owner'}:
        buttons += [COMMAND_BUTTONS['health'], COMMAND_BUTTONS['diagnostics'], COMMAND_BUTTONS['jobs']]
    if system_owner:
        buttons += [
            COMMAND_BUTTONS['backup'], COMMAND_BUTTONS['backups'], COMMAND_BUTTONS['restore'],
            COMMAND_BUTTONS['shop_add'], COMMAND_BUTTONS['shop_profile'], COMMAND_BUTTONS['profiles'],
        ]
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


def action_ref(action_key: str) -> str:
    return hashlib.blake2s(action_key.encode('utf-8'),digest_size=6).hexdigest()


def action_center_keyboard(items):
    kb=InlineKeyboardBuilder()
    for item in list(items)[:8]:
        title=str(getattr(item,'title','Действие'))
        short=title if len(title)<=42 else title[:39]+'…'
        kb.button(text=f'➡️ {short}',callback_data=f'action:view:{action_ref(str(item.action_key))}')
    kb.adjust(1)
    return kb.as_markup()


def action_item_keyboard(ref: str):
    kb=InlineKeyboardBuilder()
    kb.button(text='✅ Принято',callback_data=f'action:ack:{ref}')
    kb.button(text='⏰ Отложить на 24 ч',callback_data=f'action:snooze:{ref}')
    kb.button(text='⬅️ К списку',callback_data='action:list')
    kb.adjust(2,1)
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
