from __future__ import annotations

from aiogram.utils.keyboard import ReplyKeyboardBuilder

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
MENU_PRODUCTS = '📦 Товары и SKU'
MENU_MONEY = '💰 Деньги и реклама'
MENU_SUPPLY = '🚚 Поставки'
MENU_CONTROL = '🚨 Контроль'
MENU_SHOP = '🏪 Магазин и доступ'
MENU_SERVICE = '🛠 Сервис'


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
        '📅 Месяц', COMMAND_BUTTONS['day'],
        '📜 История',
    ]
    if role in {'analyst', 'owner'}:
        buttons += ['📊 Сегодня', '🔄 Обновить вчера', COMMAND_BUTTONS['backfill']]
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
    buttons = [COMMAND_BUTTONS['actions'], COMMAND_BUTTONS['action_history'], COMMAND_BUTTONS['alerts'], COMMAND_BUTTONS['status'], COMMAND_BUTTONS['health'], COMMAND_BUTTONS['diagnostics']]
    if role in {'analyst', 'owner'}:
        buttons += [COMMAND_BUTTONS['action_ack'], COMMAND_BUTTONS['action_snooze'], COMMAND_BUTTONS['jobs']]
    if role == 'owner':
        buttons += [COMMAND_BUTTONS['job_retry']]
    buttons += [BACK, HOME]
    return _build(buttons)


def shop_keyboard(role: str | None = 'owner', *, system_owner: bool = False):
    role = _role(role)
    buttons = [COMMAND_BUTTONS['shops'], COMMAND_BUTTONS['shop'], COMMAND_BUTTONS['my_access'],
               COMMAND_BUTTONS['settings'], COMMAND_BUTTONS['readiness'], COMMAND_BUTTONS['help']]
    if role in {'analyst','owner'}:
        buttons += [COMMAND_BUTTONS['connect_check']]
    if role == 'owner':
        buttons += [
            COMMAND_BUTTONS['setup'], COMMAND_BUTTONS['demo_on'], COMMAND_BUTTONS['demo_off'],
            COMMAND_BUTTONS['users'], COMMAND_BUTTONS['user_add'], COMMAND_BUTTONS['user_remove'],
        ]
    if system_owner:
        buttons += [
            COMMAND_BUTTONS['shop_add'], COMMAND_BUTTONS['shop_profile'],
            COMMAND_BUTTONS['shop_archive'], COMMAND_BUTTONS['shop_archived'],
            COMMAND_BUTTONS['shop_restore'], COMMAND_BUTTONS['shop_delete'],
            COMMAND_BUTTONS['profiles'],
        ]
    buttons += [BACK, HOME]
    return _build(buttons)


def service_keyboard(role: str | None = 'owner', *, system_owner: bool = False):
    role = _role(role)
    buttons = [COMMAND_BUTTONS['export']]
    if system_owner:
        buttons += [COMMAND_BUTTONS['backup'], COMMAND_BUTTONS['backups'], COMMAND_BUTTONS['restore']]
    buttons += [BACK, HOME]
    return _build(buttons)


def input_keyboard():
    return _build([CANCEL, HOME], columns=2)
