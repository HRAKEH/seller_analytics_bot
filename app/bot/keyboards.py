from __future__ import annotations

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
    'readiness': '✅ Готовность магазина',
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
    'management': '📈 Управленческий результат',
    'sku_finance': '🧾 SKU-экономика',
    'reconcile': '🔎 Сверка данных',
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
    'status': '⚙️ Статус источников',
    'health': '❤️ Health-check',
    'jobs': '🧰 Retry-очередь',
    'job_retry': '🔁 Повторить retry-задачу',
    'diagnostics': '🧪 Диагностика',
}

MENU_REPORTS = '📊 Отчёты'
MENU_PRODUCTS = '📦 Товары'
MENU_MONEY = '💰 Финансы'
MENU_SUPPLY = '🚚 Поставки'
MENU_CONTROL = '🚨 Проблемы'
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
    buttons = ['📊 Вчера', '📅 Неделя', '📅 Месяц', '🗓 Другая дата', '📜 Что уже загружено']
    if role in {'analyst', 'owner'}:
        buttons += ['🔄 Обновить вчера', '📥 Догрузить данные']
    buttons += [BACK, HOME]
    return _build(buttons)


def products_keyboard(role: str | None = 'owner', *, system_owner: bool = False):
    role = _role(role)
    buttons = [
        COMMAND_BUTTONS['products'], COMMAND_BUTTONS['stocks'],
        COMMAND_BUTTONS['sku_finance'],
    ]
    if role in {'analyst', 'owner'}:
        buttons += [COMMAND_BUTTONS['cost'], COMMAND_BUTTONS['import_costs'], COMMAND_BUTTONS['link']]
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
    buttons = [COMMAND_BUTTONS['supply'], COMMAND_BUTTONS['inbound'], COMMAND_BUTTONS['forecast_quality'],
               COMMAND_BUTTONS['supply_calibration'], COMMAND_BUTTONS['promotions'], COMMAND_BUTTONS['supply_sku'], COMMAND_BUTTONS['supply_settings']]
    if role in {'analyst', 'owner'}:
        buttons += [COMMAND_BUTTONS['supply_refresh'], COMMAND_BUTTONS['supply_calibration_refresh'], COMMAND_BUTTONS['inbound_refresh'], COMMAND_BUTTONS['promotions_refresh']]
    if role == 'owner':
        buttons += [COMMAND_BUTTONS['supply_set'], COMMAND_BUTTONS['supply_defaults']]
    buttons += [BACK, HOME]
    return _build(buttons)


def control_keyboard(role: str | None = 'owner', *, system_owner: bool = False):
    role = _role(role)
    buttons = ['🚨 Активные проблемы', '🎯 Что делать сегодня', '⚙️ Состояние источников']
    if role in {'analyst', 'owner'}:
        buttons += ['📋 История рекомендаций']
    buttons += [BACK, HOME]
    return _build(buttons)


def shop_keyboard(role: str | None = 'owner', *, system_owner: bool = False):
    role = _role(role)
    buttons = ['⚙️ Параметры отчётов', '🔌 Проверить API', '🏪 Магазины']
    if role == 'owner':
        buttons += ['👥 Доступ пользователей']
    if system_owner:
        buttons += ['🗄 Управление магазинами']
    buttons += [BACK, HOME]
    return _build(buttons)


def service_keyboard(role: str | None = 'owner', *, system_owner: bool = False):
    role = _role(role)
    buttons = ['📤 Экспорт', '❤️ Проверка системы', '🧪 Диагностика']
    if role in {'analyst','owner'}:
        buttons += ['🧰 Ошибки и повторы']
    if system_owner:
        buttons += ['💾 Резервные копии']
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


def report_date_keyboard():
    kb=InlineKeyboardBuilder()
    kb.button(text='Вчера',callback_data='report_date:1')
    kb.button(text='2 дня назад',callback_data='report_date:2')
    kb.button(text='7 дней назад',callback_data='report_date:7')
    kb.button(text='14 дней назад',callback_data='report_date:14')
    kb.button(text='✍️ Ввести дату',callback_data='report_date:custom')
    kb.button(text='❌ Отмена',callback_data='report_date:cancel')
    kb.adjust(2,2,1,1)
    return kb.as_markup()


def export_period_keyboard():
    kb=InlineKeyboardBuilder()
    for days in (7,30,90):
        kb.button(text=f'{days} дней',callback_data=f'export:period:{days}')
    kb.button(text='❌ Отмена',callback_data='export:cancel')
    kb.adjust(3,1)
    return kb.as_markup()


def export_format_keyboard(days: int):
    kb=InlineKeyboardBuilder()
    kb.button(text='📊 Excel',callback_data=f'export:run:{int(days)}:xlsx')
    kb.button(text='🗂 CSV ZIP',callback_data=f'export:run:{int(days)}:csv')
    kb.button(text='⬅️ Назад',callback_data='export:back')
    kb.button(text='❌ Отмена',callback_data='export:cancel')
    kb.adjust(2,2)
    return kb.as_markup()


def users_admin_keyboard():
    kb=InlineKeyboardBuilder()
    kb.button(text='➕ Дать доступ',callback_data='users:add')
    kb.button(text='➖ Отозвать доступ',callback_data='users:remove')
    kb.button(text='❌ Закрыть',callback_data='users:cancel')
    kb.adjust(2,1)
    return kb.as_markup()


def retry_jobs_keyboard(rows):
    kb=InlineKeyboardBuilder()
    for row in rows:
        if str(row.get('status'))!='dead':
            continue
        kb.button(text=f'🔁 Повторить #{int(row["id"])}',callback_data=f'retry:run:{int(row["id"])}')
    kb.adjust(1)
    return kb.as_markup() if kb.buttons else None


def shop_admin_keyboard():
    kb=InlineKeyboardBuilder()
    kb.button(text='➕ Добавить',callback_data='shopadmin:add')
    kb.button(text='🔐 Профиль ключей',callback_data='shopadmin:profile')
    kb.button(text='🗄 Архивировать',callback_data='shopadmin:archive')
    kb.button(text='♻️ Восстановить',callback_data='shopadmin:restore')
    kb.button(text='🗑 Удалить',callback_data='shopadmin:delete')
    kb.button(text='❌ Закрыть',callback_data='shopadmin:cancel')
    kb.adjust(2,2,1,1)
    return kb.as_markup()


def backups_menu_keyboard():
    kb=InlineKeyboardBuilder()
    kb.button(text='💾 Создать backup',callback_data='backups:create')
    kb.button(text='🗂 История backup',callback_data='backups:list')
    kb.button(text='♻️ Восстановить',callback_data='backups:restore')
    kb.button(text='❌ Закрыть',callback_data='backups:cancel')
    kb.adjust(2,1,1)
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
