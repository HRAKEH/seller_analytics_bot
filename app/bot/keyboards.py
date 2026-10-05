from __future__ import annotations

import hashlib
import calendar
from datetime import date
from app.access import action_permission, can_role, normalize_role

from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder

HOME = '🏠 Главное меню'
BACK = '⬅️ Назад'
CANCEL = '❌ Отмена'


def daily_card_keyboard(section: str = 'summary', *, can_refresh: bool = True, can_finance: bool = True,
                        page: int = 0, pages: int = 1):
    builder=InlineKeyboardBuilder()
    if section!='summary':
        if pages>1:
            builder.button(text='◀️',callback_data=f'daily_card:page:{max(0,page-1)}')
            builder.button(text=f'{page+1}/{pages}',callback_data=f'daily_card:page:{page}')
            builder.button(text='▶️',callback_data=f'daily_card:page:{min(pages-1,page+1)}')
        builder.button(text='Свернуть',callback_data='daily_card:collapse')
        builder.button(text='🗓 Выбрать дату',callback_data='daily_card:date')
        builder.button(text=HOME,callback_data='daily_card:home')
        builder.adjust(3,1,2) if pages>1 else builder.adjust(1,2)
    else:
        builder.button(text='Подробнее',callback_data='daily_card:details')
        if can_finance:builder.button(text='Начисления',callback_data='daily_card:accruals')
        if can_refresh:builder.button(text='Обновить',callback_data='daily_card:refresh')
        builder.button(text='🗓 Выбрать дату',callback_data='daily_card:date')
        builder.button(text=HOME,callback_data='daily_card:home')
        builder.adjust(2 if can_finance else 1,2 if can_refresh else 1,1)
    return builder.as_markup()


def daily_date_keyboard(month: date, today: date):
    """Calendar stays attached to the persisted daily card and its shop."""
    kb=InlineKeyboardBuilder()
    previous=(month.replace(day=1)-date.resolution).replace(day=1)
    following=(month.replace(day=28)+4*date.resolution).replace(day=1)
    names=('Январь','Февраль','Март','Апрель','Май','Июнь','Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь')
    kb.button(text='◀️',callback_data=f'daily_card:month:{previous:%Y%m}' if previous.year>=2000 else 'daily_card:noop')
    kb.button(text=f'{names[month.month-1]} {month.year}',callback_data='daily_card:noop')
    kb.button(text='▶️',callback_data=f'daily_card:month:{following:%Y%m}' if following<today else 'daily_card:noop')
    for day in ('Пн','Вт','Ср','Чт','Пт','Сб','Вс'):
        kb.button(text=day,callback_data='daily_card:noop')
    weeks=calendar.monthcalendar(month.year,month.month)
    for week in weeks:
        for number in week:
            selected=month.replace(day=number) if number else None
            kb.button(text=str(number) if number else '·',callback_data=(
                f'daily_card:day:{selected:%Y%m%d}' if selected and selected<today else 'daily_card:noop'))
    kb.button(text='⬅️ К отчёту',callback_data='daily_card:collapse')
    kb.button(text=HOME,callback_data='daily_card:home')
    kb.adjust(3,7,*([7]*len(weeks)),2)
    return kb.as_markup()


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
    'users': '👥 Сотрудники',
    'user_add': '➕ Добавить сотрудника',
    'user_remove': '➖ Отозвать доступ',
    'my_access': '🙋 Мой доступ',
    'export': '📤 Скачать данные магазина',
    'backup': '💾 Создать backup',
    'backups': '🗂 История backup',
    'restore': '♻️ Восстановить backup',
    'setup': '🧩 Мастер настройки',
    'readiness': '✅ Что настроено',
    'connect_check': '🔌 Проверить API',
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
    'refresh': '🔄 Обновить все отчёты',
    'sources': '📡 Полнота источников',
    'accruals': '🧮 Начисления Ozon',
    'wb_accruals': '🧮 Начисления WB',
    'ads': '📣 Реклама',
    'management': '📈 Результат магазина',
    'sku_finance': '🧾 Экономика по товарам',
    'reconcile': '🔎 Проверка расхождений',
    'cost': '💲 Задать себестоимость',
    'alerts': '🚨 Активные проблемы',
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
    'jobs': '🔁 Ошибки и повторы',
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
MENU_TECH = '🧰 Техническое'


def _role(role: str | None) -> str:
    try:return normalize_role(role or 'viewer')
    except ValueError:return 'viewer'


def _build(buttons: tuple[str, ...] | list[str], *, columns: int = 2, role=None, system_owner=False):
    kb = ReplyKeyboardBuilder()
    actions={v:k for k,v in COMMAND_BUTTONS.items()} | {
        '🔄 Обновить финансы':'finance_update', '🔄 Обновить рекламу':'ads_update',
        MENU_TECH:'technical_menu'}
    for text in buttons:
        permission=action_permission(actions.get(text,''))
        if role is not None and (not can_role(role,permission) or permission=='technical' and not system_owner):
            continue
        kb.button(text=text)
    kb.adjust(columns)
    return kb.as_markup(resize_keyboard=True)


def main_keyboard(role: str | None = 'owner'):
    role = _role(role)
    buttons = [MENU_REPORTS, MENU_PRODUCTS, MENU_MONEY, MENU_SUPPLY, MENU_CONTROL, MENU_SHOP, MENU_SERVICE]
    return _build(buttons, columns=2, role=role)


def menu_button_texts() -> frozenset[str]:
    """Known reply-button taps; arbitrary typed arguments are never removed."""
    texts=set(COMMAND_BUTTONS.values()) | {BACK, '📊 Сегодня'}
    keyboards=(main_keyboard, reports_keyboard, products_keyboard, money_keyboard,
               supply_keyboard, control_keyboard, shop_keyboard, service_keyboard,
               technical_keyboard)
    for keyboard in keyboards:
        markup=keyboard('owner') if keyboard is main_keyboard else keyboard('owner',system_owner=True)
        texts.update(button.text for row in markup.keyboard for button in row)
    return frozenset(texts)


def reports_keyboard(role: str | None = 'owner', *, system_owner: bool = False):
    role = _role(role)
    buttons = [
        '📊 Вчера', '📅 Неделя',
        '📅 Месяц', '🗓 Другая дата',
        '📜 Что уже загружено',
    ]
    if can_role(role,'operate'):
        buttons += ['🔄 Обновить вчера', '📥 Догрузить данные', COMMAND_BUTTONS['refresh']]
    buttons += [BACK, HOME]
    return _build(buttons,role=role,system_owner=system_owner)


def products_keyboard(role: str | None = 'owner', *, system_owner: bool = False):
    role = _role(role)
    buttons = [
        COMMAND_BUTTONS['products'], COMMAND_BUTTONS['stocks'],
        COMMAND_BUTTONS['sku_finance'],
    ]
    if can_role(role,'operate'):
        buttons += ['🔄 Обновить остатки', COMMAND_BUTTONS['import_costs']]
    buttons += [BACK, HOME]
    return _build(buttons,role=role,system_owner=system_owner)


def money_keyboard(role: str | None = 'owner', *, system_owner: bool = False):
    role = _role(role)
    buttons = [
        COMMAND_BUTTONS['finance'], COMMAND_BUTTONS['ads'],
        COMMAND_BUTTONS['management'], COMMAND_BUTTONS['reconcile'],
        COMMAND_BUTTONS['sources'], COMMAND_BUTTONS['accruals'], COMMAND_BUTTONS['wb_accruals'],
    ]
    if can_role(role,'operate'):
        buttons += [COMMAND_BUTTONS['refresh'], '🔄 Обновить финансы', '🔄 Обновить рекламу']
    buttons += [BACK, HOME]
    return _build(buttons,role=role,system_owner=system_owner)


def supply_keyboard(role: str | None = 'owner', *, system_owner: bool = False):
    role = _role(role)
    buttons = [
        COMMAND_BUTTONS['supply'], COMMAND_BUTTONS['inbound'],
        COMMAND_BUTTONS['promotions'], COMMAND_BUTTONS['forecast_quality'],
    ]
    if can_role(role,'operate'):
        buttons += [COMMAND_BUTTONS['supply_refresh'], COMMAND_BUTTONS['inbound_refresh'], COMMAND_BUTTONS['promotions_refresh']]
    buttons += [COMMAND_BUTTONS['supply_settings']]
    buttons += [BACK, HOME]
    return _build(buttons,role=role,system_owner=system_owner)


def control_keyboard(role: str | None = 'owner', *, system_owner: bool = False):
    buttons = [
        COMMAND_BUTTONS['actions'], COMMAND_BUTTONS['alerts'],
        COMMAND_BUTTONS['status'], COMMAND_BUTTONS['action_history'],
        BACK, HOME,
    ]
    return _build(buttons,role=_role(role),system_owner=system_owner)


def shop_keyboard(role: str | None = 'owner', *, system_owner: bool = False):
    role = _role(role)
    buttons = [
        COMMAND_BUTTONS['shops'], COMMAND_BUTTONS['shop'],
        COMMAND_BUTTONS['settings'], COMMAND_BUTTONS['readiness'],
        COMMAND_BUTTONS['help'], COMMAND_BUTTONS['my_access'],
    ]
    if system_owner:
        buttons += [COMMAND_BUTTONS['connect_check']]
    if can_role(role,'settings'):
        buttons += [COMMAND_BUTTONS['setup']]
    if role == 'owner':
        buttons += [COMMAND_BUTTONS['users'],
                    COMMAND_BUTTONS['user_add'], COMMAND_BUTTONS['user_remove']]
    if system_owner:
        buttons += [
            COMMAND_BUTTONS['shop_add'], COMMAND_BUTTONS['shop_profile'], COMMAND_BUTTONS['profiles'],
            COMMAND_BUTTONS['shop_archive'], COMMAND_BUTTONS['shop_archived'],
            COMMAND_BUTTONS['shop_restore'], COMMAND_BUTTONS['shop_delete'],
        ]
    buttons += [BACK, HOME]
    return _build(buttons,role=role,system_owner=system_owner)


def service_keyboard(role: str | None = 'owner', *, system_owner: bool = False):
    role = _role(role)
    buttons = [COMMAND_BUTTONS['export']]
    if system_owner:
        buttons += [MENU_TECH]
    buttons += [BACK, HOME]
    return _build(buttons,role=role,system_owner=system_owner)


def technical_keyboard(role: str | None = 'owner', *, system_owner: bool = False):
    role = _role(role)
    buttons = [COMMAND_BUTTONS['health'], COMMAND_BUTTONS['diagnostics'], COMMAND_BUTTONS['jobs']]
    if system_owner:
        buttons += [
            COMMAND_BUTTONS['backup'], COMMAND_BUTTONS['backups'], COMMAND_BUTTONS['restore'],
            COMMAND_BUTTONS['shop_add'], COMMAND_BUTTONS['shop_profile'], COMMAND_BUTTONS['profiles'],
        ]
    buttons += [BACK, HOME]
    return _build(buttons, role=role, system_owner=system_owner)


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
    kb.button(text='📊 Excel (.xlsx)',callback_data=f'export:run:{int(days)}:xlsx')
    kb.button(text='🗂 Архив CSV (.zip)',callback_data=f'export:run:{int(days)}:csv')
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
    kb=InlineKeyboardBuilder(); added=False
    for row in rows:
        if str(row.get('status'))!='dead':
            continue
        kb.button(text=f'🔁 Повторить #{int(row["id"])}',callback_data=f'retry:run:{int(row["id"])}')
        added=True
    kb.adjust(1)
    return kb.as_markup() if added else None


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
