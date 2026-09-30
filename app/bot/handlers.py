from __future__ import annotations
from datetime import date, datetime, timedelta
import asyncio
from html import escape
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

from aiogram import Dispatcher, F, types
from aiogram.types import FSInputFile
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from app.reports import (
    build_daily_report, build_period_report, format_daily, format_period,
    build_product_report, format_product_report, format_stock_report,
    build_finance_report, format_finance, build_sku_economics, format_sku_economics,
    build_reconciliation_report, format_reconciliation,
    build_advertising_report, format_advertising,
    build_management_report, format_management, format_supply_plan, format_supply_product,
    format_inbound, format_forecast_quality, format_supply_calibration, format_action_center, format_action_history, format_promotions,
)
from app.services.alerts import AlertEngine
from app.services.imports import import_costs
from app.services.preferences import update_validated
from app.services.backups import BackupService
from app.services.exporting import export_xlsx, export_csv_zip
from app.services.health import build_health, format_health
from app.services.supply import build_supply_plan, evaluate_forecast_quality, build_supply_calibration
from app.services.actions import build_action_center
from app.services.readiness import build_readiness, format_readiness, format_onboarding_help
from app.services.demo import enable_demo, disable_demo
from .context import AppContext
from .keyboards import (
    main_keyboard, reports_keyboard, products_keyboard, money_keyboard, supply_keyboard,
    control_keyboard, shop_keyboard, service_keyboard, input_keyboard, shop_picker_keyboard,
    shop_confirm_keyboard, backfill_source_keyboard, backfill_period_keyboard, backfill_running_keyboard, COMMAND_BUTTONS,
    MENU_REPORTS, MENU_PRODUCTS, MENU_MONEY, MENU_SUPPLY, MENU_CONTROL, MENU_SHOP, MENU_SERVICE,
    HOME, BACK, CANCEL,
)


class SetupStates(StatesGroup):
    shop_name = State()
    timezone = State()
    report_time = State()
    stock_risk_days = State()
    order_drop_pct = State()
    drr_pct = State()


class CostImportStates(StatesGroup):
    waiting_file = State()


class RestoreStates(StatesGroup):
    waiting_file = State()


class MenuInputStates(StatesGroup):
    """Single-step inputs launched from menu buttons for commands with arguments."""
    waiting_value = State()


class BackfillStates(StatesGroup):
    custom_period = State()


def register_handlers(dp: Dispatcher, ctx: AppContext, registry=None) -> None:
    def allowed(message: types.Message, permission: str = 'view') -> bool:
        return bool(message.from_user and ctx.repository.can_user(message.from_user.id,ctx.shop_id,permission))

    async def denied(message: types.Message, permission: str = 'view'):
        need={'view':'просмотр','operate':'аналитика/обновление данных','manage':'управление магазином'}.get(permission,permission)
        await message.answer(f'⛔ Недостаточно прав: требуется доступ «{need}».')

    def is_system_owner(message: types.Message) -> bool:
        return bool(message.from_user and message.from_user.id in ctx.settings.owner_ids)

    async def system_denied(message: types.Message):
        await message.answer('⛔ Эта операция управляет всем экземпляром бота и доступна только system owner из TELEGRAM_OWNER_ID.')

    def keyboard_for(message: types.Message):
        uid=message.from_user.id if message.from_user else 0
        return main_keyboard(ctx.repository.role_for_user(uid,ctx.shop_id))

    async def send(message: types.Message, text: str):
        await message.answer(text, parse_mode='HTML', reply_markup=keyboard_for(message))

    def role_for(message: types.Message) -> str:
        uid=message.from_user.id if message.from_user else 0
        return ctx.repository.role_for_user(uid,ctx.shop_id) or 'viewer'

    async def show_main_menu(message: types.Message, *, text: str | None = None):
        shop=ctx.repository.get_shop(ctx.shop_id)
        title=text or (
            f'🏠 <b>Главное меню</b>\n'
            f'🏪 {escape(shop.name if shop else "Магазин")}\n'
            f'Выберите раздел:'
        )
        await message.answer(title,parse_mode='HTML',reply_markup=main_keyboard(role_for(message)))

    async def start_menu_input(message: types.Message, state: FSMContext, action: str, prompt: str):
        await state.clear()
        await state.set_state(MenuInputStates.waiting_value)
        await state.update_data(menu_action=action)
        await message.answer(prompt,parse_mode='HTML',reply_markup=input_keyboard())

    def command_copy(message: types.Message, command: str, value: str):
        text=f'/{command}' + (f' {value.strip()}' if value.strip() else '')

        class MessageTextProxy:
            # Keep the original aiogram Message (and therefore its Bot binding)
            # intact; only override .text for reuse of the slash-command parser.
            def __init__(self, base, overridden_text):
                self._base=base; self.text=overridden_text
            def __getattr__(self, name):
                return getattr(self._base,name)

        return MessageTextProxy(message,text)

    # Global navigation is registered before wizard state handlers so menu
    # buttons can never be accidentally consumed as a shop name/date/etc.
    @dp.message(F.text == CANCEL)
    async def btn_cancel_any(message: types.Message, state: FSMContext):
        if not allowed(message): return await denied(message)
        await state.clear()
        await show_main_menu(message,text='↩️ Действие отменено.\n\n🏠 <b>Главное меню</b>')

    @dp.message(F.text == HOME)
    async def btn_home_any(message: types.Message, state: FSMContext):
        if not allowed(message): return await denied(message)
        await state.clear()
        await show_main_menu(message)

    def pref():
        return ctx.preferences()

    def local_now() -> datetime:
        return datetime.now(ZoneInfo(pref().timezone))

    def backfill_source_label(source: str) -> str:
        return {
            'ozon':'🟣 Ozon',
            'wildberries':'🔵 Wildberries',
            'wb':'🔵 Wildberries',
            'all':'🟣🔵 Ozon + Wildberries',
        }.get(source,source)

    async def show_backfill_source_picker(message: types.Message):
        if not allowed(message,'operate'): return await denied(message,'operate')
        has_ozon=ctx.ozon_connection_id is not None
        has_wb=ctx.wb_connection_id is not None
        if not has_ozon and not has_wb:
            return await message.answer('⚠️ В текущем магазине не подключены Ozon или Wildberries.')
        await message.answer(
            '📥 <b>Загрузка истории</b>\nВыберите источник:',
            parse_mode='HTML',
            reply_markup=backfill_source_keyboard(has_ozon=has_ozon,has_wb=has_wb))

    async def execute_backfill(message: types.Message, *, source: str, start: date, end: date,
                               actor_user_id: int | None = None):
        uid=actor_user_id if actor_user_id is not None else (message.from_user.id if message.from_user else 0)
        if not ctx.repository.can_user(uid,ctx.shop_id,'operate'):
            return await message.answer('⛔ Недостаточно прав: требуется доступ «аналитика/обновление данных».')
        if end < start:
            return await message.answer('⚠️ Конечная дата раньше начальной.')
        yesterday=local_now().date()-timedelta(days=1)
        if end > yesterday:
            return await message.answer(f'⚠️ История загружается только по завершённым дням. Последняя доступная дата: {yesterday}.')
        span=(end-start).days+1
        if span > 90:
            return await message.answer('⚠️ За один запуск можно загрузить максимум 90 дней.')
        if ctx.job_lock.locked():
            return await message.answer('⏳ Уже выполняется другая выгрузка.')
        label=backfill_source_label(source)
        wait_hint='\nЕсли WB вернёт 429, бот автоматически дождётся X-Ratelimit-Retry и повторит запрос.' if source in {'wildberries','wb','all'} else ''
        progress=await message.answer(
            f'📥 <b>{label}</b>\nЗагружаю {start} — {end} ({span} дн.).'
            f'{wait_hint}\nОшибки не затирают успешные данные.',
            parse_mode='HTML',
            reply_markup=backfill_running_keyboard())
        try:
            outcomes=await ctx.backfill_orders(start,end,marketplace=source)
        except asyncio.CancelledError:
            return await message.answer(
                '🛑 <b>Загрузка истории остановлена.</b>\n'
                'Уже сохранённые данные остались в БД. Отмена не сбрасывает лимит WB: '
                'если API уже вернул 429, при новом запуске нужно учитывать его cooldown.',
                parse_mode='HTML')
        except (RuntimeError,ValueError) as exc:
            return await message.answer(f'⚠️ {escape(str(exc)[:400])}')
        finally:
            try:
                await progress.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        core_ok=sum(1 for x in outcomes if x.ok and x.message=='orders loaded')
        total_failed=sum(1 for x in outcomes if not x.ok)
        source_count=2 if source=='all' and ctx.wb_connection_id is not None and ctx.ozon_connection_id is not None else 1
        expected_core=span*source_count
        lines=[
            f'✅ <b>Загрузка завершена · {label}</b>',
            f'Период: {start} — {end}',
            f'Основные заказы по дням: {core_ok}/{expected_core}',
            f'Ошибок API/доп. источников: {total_failed}',
        ]
        await message.answer('\n'.join(lines),parse_mode='HTML')
        role=ctx.repository.role_for_user(uid,ctx.shop_id) or 'viewer'
        await message.answer(
            format_daily(build_daily_report(ctx.repository,ctx.shop_id,end)),
            parse_mode='HTML',reply_markup=main_keyboard(role))

    async def collect_and_report(message: types.Message, day: date, force: bool = True):
        if force:
            await message.answer(f'⏳ Обновляю данные за {day.isoformat()}…')
            try:
                await ctx.collect_day(day)
            except Exception as exc:
                await message.answer(f'⚠️ Неожиданная ошибка загрузки: {escape(str(exc)[:250])}')
        await send(message, format_daily(build_daily_report(ctx.repository, ctx.shop_id, day)))

    def settings_text() -> str:
        p=pref(); shop=ctx.repository.get_shop(ctx.shop_id)
        core=[]
        if p.demo_mode:
            core += ['🧪 Demo WB: ✅', '🧪 Demo Ozon: ✅']
        else:
            core.append('🟣 Ozon: ✅' if ctx.ozon_connection_id is not None else '🟣 Ozon: ❌ ключи не заданы')
        if not p.demo_mode:
            core.append('🔵 Wildberries: ✅' if ctx.wb_connection_id is not None else '🔵 Wildberries: ❌ токен не задан')
            core.append('📣 Ozon Performance: ✅' if ctx.collector.ozon_performance is not None else '📣 Ozon Performance: —')
        return '\n'.join([
            '⚙️ <b>Настройки магазина</b>', '━━━━━━━━━━━━━━━━',
            f'Название: <b>{escape(shop.name if shop else "Магазин")}</b>',
            f'Профиль ключей: <code>{escape(shop.credential_profile if shop else "DEFAULT")}</code>',
            f'Часовой пояс: <code>{escape(p.timezone)}</code>',
            f'Ежедневный отчёт: <b>{p.report_time}</b>',
            f'Товарный отчёт: {p.product_report_days} дн.',
            f'Окно скорости продаж: {p.stock_velocity_days} дн.',
            f'Риск остатка: ≤ {p.stock_risk_days} дн.',
            f'Падение заказов: ≥ {p.alert_order_drop_pct:g}%',
            f'Порог ДРР: ≥ {p.alert_drr_pct:g}%',
            f'Алерты: {"✅" if p.alerts_enabled else "⏸"} · каждые {p.alerts_interval_minutes} мин.',
            f'Первичная настройка: {"✅ завершена" if p.setup_completed else "⚠️ не завершена"}',
            f'Режим данных: {"🧪 DEMO" if p.demo_mode else "🟢 реальные API"}',
            f'Onboarding: <code>{escape(p.onboarding_version or "—")}</code>',
            '', *core,
            '', 'Секреты API хранятся только в переменных окружения хостинга и здесь не показываются.'
        ])

    # --- multi-shop / export / backup -----------------------------------
    @dp.message(Command('shops'))
    async def cmd_shops(message: types.Message):
        if not allowed(message): return await denied(message)
        if registry is None: return await message.answer('Multi-shop runtime не подключён.')
        shops=registry.repository.shops_for_user(message.from_user.id)
        lines=['🏪 <b>Доступные магазины</b>','━━━━━━━━━━━━━━━━']
        for row in shops:
            shop=registry.repository.get_shop(int(row['id']))
            if not shop: continue
            mark='✅' if shop.id==ctx.shop_id else '▫️'
            creds=registry.settings.credentials_for_profile(shop.credential_profile)
            sources=[]
            if creds.has_wb: sources.append('WB')
            if creds.has_ozon: sources.append('Ozon')
            lines.append(f'{mark} <b>#{shop.id} {escape(shop.name)}</b> · {escape(str(row["role"]))} · <code>{escape(shop.credential_profile)}</code> · {" + ".join(sources) or "без API"}')
        lines += ['', 'Нажмите на магазин ниже, чтобы переключиться.']
        if allowed(message,'manage'):
            lines += ['Для нового магазина: «➕ Добавить магазин».',
                      'Для смены профиля ключей: «🔐 Профиль ключей».']
        picker=[registry.repository.get_shop(int(row['id'])) for row in shops]
        picker=[shop for shop in picker if shop is not None]
        await message.answer('\n'.join(lines),parse_mode='HTML',
                             reply_markup=shop_picker_keyboard(picker,'select',current_shop_id=ctx.shop_id))

    async def show_shop_picker(message: types.Message, action: str):
        if registry is None: return await message.answer('Multi-shop runtime не подключён.')
        if action=='select':
            shops=[registry.repository.get_shop(int(row['id'])) for row in registry.repository.shops_for_user(message.from_user.id)]
            shops=[shop for shop in shops if shop is not None]
            if not shops: return await message.answer('⚠️ Нет доступных магазинов.')
            return await message.answer('🔁 <b>Выберите магазин</b>',parse_mode='HTML',
                reply_markup=shop_picker_keyboard(shops,'select',current_shop_id=ctx.shop_id))
        if not is_system_owner(message): return await system_denied(message)
        if action=='archive':
            shops=registry.repository.list_shops(registry.seller_id)
            if len(shops)<=1:
                return await message.answer('⚠️ Нельзя архивировать последний активный магазин.')
            return await message.answer('🗄 <b>Какой магазин архивировать?</b>\nДанные сохранятся, API-запросы и scheduler для него остановятся.',
                parse_mode='HTML',reply_markup=shop_picker_keyboard(shops,'archive',current_shop_id=ctx.shop_id))
        if action in {'restore','delete'}:
            shops=registry.repository.archived_shops(registry.seller_id)
            if not shops:
                return await message.answer('🗂 Архив магазинов пуст.')
            title='♻️ <b>Какой магазин вернуть?</b>' if action=='restore' else '🗑 <b>Какой магазин удалить навсегда?</b>'
            return await message.answer(title,parse_mode='HTML',reply_markup=shop_picker_keyboard(shops,action))
        raise ValueError('unknown shop picker action')

    @dp.message(Command('shop'))
    async def cmd_shop(message: types.Message):
        if not allowed(message): return await denied(message)
        if registry is None: return await message.answer('Multi-shop runtime не подключён.')
        parts=(message.text or '').split(maxsplit=1)
        if len(parts)<2: return await cmd_shops(message)
        try: shop_id=int(parts[1])
        except ValueError: return await message.answer('Формат: /shop 2')
        allowed_ids={int(s['id']) for s in registry.repository.shops_for_user(message.from_user.id)}
        if shop_id not in allowed_ids: return await message.answer('⚠️ У вас нет доступа к этому магазину.')
        registry.select_shop(message.from_user.id,shop_id)
        shop=registry.repository.get_shop(shop_id)
        await message.answer(f'✅ Текущий магазин: <b>#{shop.id} {escape(shop.name)}</b>\nСледующая команда уже будет выполнена в нём.',parse_mode='HTML',reply_markup=keyboard_for(message))

    @dp.message(Command('shop_add'))
    async def cmd_shop_add(message: types.Message):
        if not allowed(message,'manage'): return await denied(message,'manage')
        if not is_system_owner(message): return await system_denied(message)
        if registry is None: return await message.answer('Multi-shop runtime не подключён.')
        body=(message.text or '').partition(' ')[2].strip()
        if not body: return await message.answer('Формат: /shop_add Второй магазин | SHOP2')
        if '|' in body: name,profile=(x.strip() for x in body.rsplit('|',1))
        else: name,profile=body,'DEFAULT'
        if not name: return await message.answer('Название магазина не может быть пустым.')
        try:
            ctx2=await registry.create_shop(name,profile)
            registry.select_shop(message.from_user.id,ctx2.shop_id)
            shop=registry.repository.get_shop(ctx2.shop_id)
            creds=registry.settings.credentials_for_profile(shop.credential_profile)
        except Exception as exc:
            return await message.answer(f'⚠️ Не удалось создать магазин: {escape(str(exc)[:300])}')
        sources=[]
        if creds.has_wb: sources.append('WB')
        if creds.has_ozon: sources.append('Ozon')
        await message.answer(f'✅ Создан и выбран <b>#{shop.id} {escape(shop.name)}</b>.\nПрофиль: <code>{escape(shop.credential_profile)}</code> · API: {" + ".join(sources) or "не настроены"}\nОткройте «🧩 Мастер настройки» для параметров отчётов.',parse_mode='HTML',reply_markup=keyboard_for(message))

    @dp.message(Command('shop_profile'))
    async def cmd_shop_profile(message: types.Message):
        if not allowed(message,'manage'): return await denied(message,'manage')
        if not is_system_owner(message): return await system_denied(message)
        if registry is None: return await message.answer('Multi-shop runtime не подключён.')
        parts=(message.text or '').split(maxsplit=1)
        if len(parts)<2: return await message.answer('Формат: /shop_profile SHOP2')
        profile=parts[1].strip().upper()
        try:
            ctx2=await registry.set_profile(ctx.shop_id,profile)
            creds=registry.settings.credentials_for_profile(profile)
        except Exception as exc:
            return await message.answer(f'⚠️ {escape(str(exc)[:300])}')
        sources=[]
        if creds.has_wb: sources.append('WB')
        if creds.has_ozon: sources.append('Ozon')
        await message.answer(f'✅ Профиль магазина изменён на <code>{escape(profile)}</code>. API: {" + ".join(sources) or "не найдены в окружении"}.',parse_mode='HTML')

    @dp.message(Command('shop_archive'))
    async def cmd_shop_archive(message: types.Message):
        if not allowed(message,'manage'): return await denied(message,'manage')
        if not is_system_owner(message): return await system_denied(message)
        if registry is None: return await message.answer('Multi-shop runtime не подключён.')
        parts=(message.text or '').split(maxsplit=1)
        if len(parts)<2: return await message.answer('Формат: /shop_archive ID')
        try: shop_id=int(parts[1])
        except ValueError: return await message.answer('ID магазина должен быть числом.')
        shop=registry.repository.get_shop(shop_id)
        if not shop or shop.seller_id!=registry.seller_id:
            return await message.answer('⚠️ Магазин не найден.')
        if shop_id==ctx.shop_id:
            note='\nТекущий магазин будет переключён при следующем сообщении.'
        else:
            note=''
        try:
            await registry.archive_shop(shop_id)
        except Exception as exc:
            return await message.answer(f'⚠️ Не удалось архивировать магазин: {escape(str(exc)[:300])}')
        await message.answer(
            f'🗄 Магазин <b>#{shop.id} {escape(shop.name)}</b> перемещён в архив.\n'
            'Его данные сохранены, scheduler и API-запросы для него остановлены.'
            + note, parse_mode='HTML', reply_markup=keyboard_for(message))

    @dp.message(Command('shop_archived'))
    async def cmd_shop_archived(message: types.Message):
        if not allowed(message,'manage'): return await denied(message,'manage')
        if not is_system_owner(message): return await system_denied(message)
        if registry is None: return await message.answer('Multi-shop runtime не подключён.')
        shops=registry.repository.archived_shops(registry.seller_id)
        lines=['🗂 <b>Архив магазинов</b>','━━━━━━━━━━━━━━━━']
        if not shops:
            lines.append('Архив пуст.')
        else:
            for shop in shops:
                lines.append(f'• <b>#{shop.id} {escape(shop.name)}</b> · <code>{escape(shop.credential_profile)}</code>')
            lines += ['', 'Вернуть: «♻️ Вернуть магазин».',
                      'Удалить навсегда: «🗑 Удалить магазин».']
        await send(message,'\n'.join(lines))

    @dp.message(Command('shop_restore'))
    async def cmd_shop_restore(message: types.Message):
        if not allowed(message,'manage'): return await denied(message,'manage')
        if not is_system_owner(message): return await system_denied(message)
        if registry is None: return await message.answer('Multi-shop runtime не подключён.')
        parts=(message.text or '').split(maxsplit=1)
        if len(parts)<2: return await message.answer('Формат: /shop_restore ID')
        try: shop_id=int(parts[1])
        except ValueError: return await message.answer('ID магазина должен быть числом.')
        try:
            ctx2=await registry.restore_shop(shop_id)
        except Exception as exc:
            return await message.answer(f'⚠️ Не удалось вернуть магазин: {escape(str(exc)[:300])}')
        shop=registry.repository.get_shop(ctx2.shop_id)
        await message.answer(f'♻️ Магазин <b>#{shop.id} {escape(shop.name)}</b> восстановлен и снова участвует в scheduler.',parse_mode='HTML')

    @dp.message(Command('shop_delete'))
    async def cmd_shop_delete(message: types.Message):
        if not allowed(message,'manage'): return await denied(message,'manage')
        if not is_system_owner(message): return await system_denied(message)
        if registry is None: return await message.answer('Multi-shop runtime не подключён.')
        parts=(message.text or '').split()
        if len(parts)<3 or parts[2].upper()!='DELETE':
            return await message.answer(
                '⚠️ Безвозвратное удаление. Сначала магазин должен быть в архиве.\n'
                'Формат: <code>/shop_delete ID DELETE</code>', parse_mode='HTML')
        try: shop_id=int(parts[1])
        except ValueError: return await message.answer('ID магазина должен быть числом.')
        shop=registry.repository.get_shop(shop_id)
        if not shop or shop.seller_id!=registry.seller_id:
            return await message.answer('⚠️ Магазин не найден.')
        try:
            await registry.delete_archived_shop(shop_id)
        except Exception as exc:
            return await message.answer(f'⚠️ Не удалось удалить магазин: {escape(str(exc)[:300])}')
        await message.answer(
            f'🗑 Магазин <b>#{shop.id} {escape(shop.name)}</b> удалён безвозвратно вместе с его данными.',
            parse_mode='HTML')

    @dp.callback_query(F.data == 'shop:cancel')
    async def cb_shop_cancel(callback: types.CallbackQuery):
        await callback.answer('Отменено')
        if callback.message:
            await callback.message.edit_text('❌ Действие отменено.')

    @dp.callback_query(F.data.startswith('shop:select:'))
    async def cb_shop_select(callback: types.CallbackQuery):
        if registry is None or callback.from_user is None:
            return await callback.answer('Runtime недоступен.',show_alert=True)
        try: shop_id=int((callback.data or '').rsplit(':',1)[1])
        except (ValueError,IndexError):
            return await callback.answer('Некорректный магазин.',show_alert=True)
        allowed_ids={int(row['id']) for row in registry.repository.shops_for_user(callback.from_user.id)}
        if shop_id not in allowed_ids:
            return await callback.answer('Нет доступа к этому магазину.',show_alert=True)
        registry.select_shop(callback.from_user.id,shop_id)
        shop=registry.repository.get_shop(shop_id)
        await callback.answer('Магазин выбран')
        if callback.message and shop:
            await callback.message.edit_text(f'✅ Текущий магазин: <b>#{shop.id} {escape(shop.name)}</b>',parse_mode='HTML')
            role=registry.repository.role_for_user(callback.from_user.id,shop_id) or 'viewer'
            await callback.message.answer('🏠 Выберите раздел:',reply_markup=main_keyboard(role))

    @dp.callback_query(F.data.startswith('shop:archive:'))
    async def cb_shop_archive_pick(callback: types.CallbackQuery):
        if registry is None or callback.from_user is None:
            return await callback.answer('Runtime недоступен.',show_alert=True)
        if callback.from_user.id not in registry.settings.owner_ids:
            return await callback.answer('Только system owner.',show_alert=True)
        try: shop_id=int((callback.data or '').rsplit(':',1)[1])
        except (ValueError,IndexError):
            return await callback.answer('Некорректный магазин.',show_alert=True)
        shop=registry.repository.get_shop(shop_id)
        if not shop or not shop.active or shop.seller_id!=registry.seller_id:
            return await callback.answer('Магазин недоступен.',show_alert=True)
        await callback.answer()
        if callback.message:
            await callback.message.edit_text(
                f'🗄 Архивировать <b>#{shop.id} {escape(shop.name)}</b>?\n\n'
                'Данные сохранятся. Фоновые задачи и API-запросы для этого магазина остановятся.',
                parse_mode='HTML',reply_markup=shop_confirm_keyboard('archive',shop.id))

    @dp.callback_query(F.data.startswith('shop:archive_confirm:'))
    async def cb_shop_archive_confirm(callback: types.CallbackQuery):
        if registry is None or callback.from_user is None:
            return await callback.answer('Runtime недоступен.',show_alert=True)
        if callback.from_user.id not in registry.settings.owner_ids:
            return await callback.answer('Только system owner.',show_alert=True)
        try: shop_id=int((callback.data or '').rsplit(':',1)[1])
        except (ValueError,IndexError):
            return await callback.answer('Некорректный магазин.',show_alert=True)
        shop=registry.repository.get_shop(shop_id)
        if not shop:
            return await callback.answer('Магазин не найден.',show_alert=True)
        try:
            await registry.archive_shop(shop_id)
            fallback=registry.repository.selected_authorized_shop_for_user(callback.from_user.id,registry.default_shop_id)
            if fallback is not None:
                registry.select_shop(callback.from_user.id,int(fallback))
        except Exception as exc:
            return await callback.answer(str(exc)[:180],show_alert=True)
        await callback.answer('Магазин архивирован')
        if callback.message:
            fallback_shop=registry.repository.get_shop(int(fallback)) if fallback is not None else None
            suffix=f'\nТекущий магазин: <b>{escape(fallback_shop.name)}</b>.' if fallback_shop else ''
            await callback.message.edit_text(
                f'🗄 Магазин <b>#{shop.id} {escape(shop.name)}</b> перемещён в архив.\n'
                'Данные сохранены, фоновые запросы остановлены.'+suffix,parse_mode='HTML')

    @dp.callback_query(F.data.startswith('shop:restore:'))
    async def cb_shop_restore(callback: types.CallbackQuery):
        if registry is None or callback.from_user is None:
            return await callback.answer('Runtime недоступен.',show_alert=True)
        if callback.from_user.id not in registry.settings.owner_ids:
            return await callback.answer('Только system owner.',show_alert=True)
        try: shop_id=int((callback.data or '').rsplit(':',1)[1])
        except (ValueError,IndexError):
            return await callback.answer('Некорректный магазин.',show_alert=True)
        try:
            ctx2=await registry.restore_shop(shop_id)
        except Exception as exc:
            return await callback.answer(str(exc)[:180],show_alert=True)
        shop=registry.repository.get_shop(ctx2.shop_id)
        await callback.answer('Магазин восстановлен')
        if callback.message and shop:
            await callback.message.edit_text(
                f'♻️ Магазин <b>#{shop.id} {escape(shop.name)}</b> восстановлен.\n'
                'Он снова участвует в scheduler и может обращаться к API.',parse_mode='HTML')

    @dp.callback_query(F.data.startswith('shop:delete:'))
    async def cb_shop_delete_pick(callback: types.CallbackQuery):
        if registry is None or callback.from_user is None:
            return await callback.answer('Runtime недоступен.',show_alert=True)
        if callback.from_user.id not in registry.settings.owner_ids:
            return await callback.answer('Только system owner.',show_alert=True)
        try: shop_id=int((callback.data or '').rsplit(':',1)[1])
        except (ValueError,IndexError):
            return await callback.answer('Некорректный магазин.',show_alert=True)
        shop=registry.repository.get_shop(shop_id)
        if not shop or shop.active or shop.seller_id!=registry.seller_id:
            return await callback.answer('Удалять можно только архивный магазин.',show_alert=True)
        await callback.answer()
        if callback.message:
            await callback.message.edit_text(
                f'🗑 <b>Безвозвратно удалить #{shop.id} {escape(shop.name)}?</b>\n\n'
                'Будут удалены данные магазина, история загрузок, товары, настройки и доступы. Отменить это действие нельзя.',
                parse_mode='HTML',reply_markup=shop_confirm_keyboard('delete',shop.id))

    @dp.callback_query(F.data.startswith('shop:delete_confirm:'))
    async def cb_shop_delete_confirm(callback: types.CallbackQuery):
        if registry is None or callback.from_user is None:
            return await callback.answer('Runtime недоступен.',show_alert=True)
        if callback.from_user.id not in registry.settings.owner_ids:
            return await callback.answer('Только system owner.',show_alert=True)
        try: shop_id=int((callback.data or '').rsplit(':',1)[1])
        except (ValueError,IndexError):
            return await callback.answer('Некорректный магазин.',show_alert=True)
        shop=registry.repository.get_shop(shop_id)
        if not shop:
            return await callback.answer('Магазин не найден.',show_alert=True)
        try:
            await registry.delete_archived_shop(shop_id)
        except Exception as exc:
            return await callback.answer(str(exc)[:180],show_alert=True)
        await callback.answer('Магазин удалён')
        if callback.message:
            await callback.message.edit_text(
                f'🗑 Магазин <b>#{shop.id} {escape(shop.name)}</b> удалён безвозвратно.',
                parse_mode='HTML')

    @dp.message(Command('profiles'))
    async def cmd_profiles(message: types.Message):
        if not allowed(message,'manage'): return await denied(message,'manage')
        if not is_system_owner(message): return await system_denied(message)
        profiles=ctx.settings.available_credential_profiles()
        await message.answer('🔐 Профили ключей в окружении:\n'+'\n'.join('• <code>'+escape(p)+'</code>' for p in profiles),parse_mode='HTML')

    @dp.message(Command('users'))
    async def cmd_users(message: types.Message):
        if not allowed(message,'manage'): return await denied(message,'manage')
        rows=ctx.repository.users_for_shop(ctx.shop_id)
        lines=['👥 <b>Доступ к текущему магазину</b>','━━━━━━━━━━━━━━━━']
        if not rows: lines.append('Нет пользователей.')
        for row in rows:
            name=f" · {escape(str(row['display_name']))}" if row.get('display_name') else ''
            lines.append(f"• <code>{int(row['telegram_user_id'])}</code>{name} · <b>{escape(str(row['role']))}</b>")
        lines += ['', 'Выдать/изменить роль: кнопка «➕ Дать доступ».',
                  'Отозвать доступ: кнопка «➖ Отозвать доступ».']
        await send(message,'\n'.join(lines))

    @dp.message(Command('user_add'))
    async def cmd_user_add(message: types.Message):
        if not allowed(message,'manage'): return await denied(message,'manage')
        parts=(message.text or '').split(maxsplit=3)
        if len(parts)<3: return await message.answer('Формат: /user_add TELEGRAM_ID viewer|analyst|owner [Имя]')
        try: uid=int(parts[1])
        except ValueError: return await message.answer('TELEGRAM_ID должен быть числом.')
        role=parts[2].lower(); name=parts[3] if len(parts)>3 else ''
        if role not in {'viewer','analyst','owner'}: return await message.answer('Роль: viewer, analyst или owner.')
        current_role=ctx.repository.role_for_user(uid,ctx.shop_id)
        if current_role=='owner' and role!='owner':
            owners=ctx.repository.users_for_shop(ctx.shop_id,roles=['owner'])
            if len(owners)<=1:
                return await message.answer('⚠️ Нельзя понизить роль последнего владельца магазина.')
        ctx.repository.grant_shop_access(uid,ctx.shop_id,role,display_name=name)
        await message.answer(f'✅ Пользователю <code>{uid}</code> выдана роль <b>{role}</b> для текущего магазина.',parse_mode='HTML')

    @dp.message(Command('user_remove'))
    async def cmd_user_remove(message: types.Message):
        if not allowed(message,'manage'): return await denied(message,'manage')
        parts=(message.text or '').split(maxsplit=1)
        if len(parts)<2: return await message.answer('Формат: /user_remove TELEGRAM_ID')
        try: uid=int(parts[1])
        except ValueError: return await message.answer('TELEGRAM_ID должен быть числом.')
        if uid==message.from_user.id and ctx.repository.role_for_user(uid,ctx.shop_id)=='owner':
            owners=[x for x in ctx.repository.users_for_shop(ctx.shop_id,roles=['owner'])]
            if len(owners)<=1: return await message.answer('⚠️ Нельзя удалить последнего владельца магазина.')
        removed=ctx.repository.revoke_shop_access(uid,ctx.shop_id)
        await message.answer('✅ Доступ отозван.' if removed else 'ℹ️ У пользователя не было доступа к этому магазину.')

    @dp.message(Command('my_access'))
    async def cmd_my_access(message: types.Message):
        if not allowed(message): return await denied(message)
        rows=ctx.repository.shops_for_user(message.from_user.id)
        lines=['🔐 <b>Мой доступ</b>']
        for row in rows:
            mark='✅' if int(row['id'])==ctx.shop_id else '▫️'
            lines.append(f"{mark} #{int(row['id'])} {escape(str(row['name']))} · <b>{escape(str(row['role']))}</b>")
        await send(message,'\n'.join(lines))

    @dp.message(Command('export'))
    async def cmd_export(message: types.Message):
        if not allowed(message): return await denied(message)
        parts=(message.text or '').split()
        try: days=int(parts[1]) if len(parts)>1 else 30
        except ValueError: return await message.answer('Формат: /export [дней] [xlsx|csv]')
        fmt=(parts[2].lower() if len(parts)>2 else 'xlsx')
        if fmt not in {'xlsx','csv'}: return await message.answer('Формат экспорта: xlsx или csv.')
        days=max(1,min(days,365)); end=local_now().date()-timedelta(days=1)
        shop=ctx.repository.get_shop(ctx.shop_id)
        await message.answer(f'📤 Формирую экспорт за {days} дн. · {fmt.upper()}…')
        try:
            with tempfile.TemporaryDirectory(prefix='seller-bot-export-') as tmp:
                suffix='.xlsx' if fmt=='xlsx' else '_csv.zip'
                path=Path(tmp)/f'sellerbot_shop_{ctx.shop_id}_{end.isoformat()}{suffix}'
                result=export_xlsx(ctx.repository,ctx.shop_id,end,days,path) if fmt=='xlsx' else export_csv_zip(ctx.repository,ctx.shop_id,end,days,path)
                caption=f'📤 {shop.name if shop else "Магазин"} · {result.start} — {result.end}'
                await message.answer_document(FSInputFile(str(result.path)),caption=caption)
        except Exception as exc:
            await message.answer(f'⚠️ Экспорт не создан: {escape(str(exc)[:300])}')

    @dp.message(Command('backup'))
    async def cmd_backup(message: types.Message):
        if not allowed(message,'manage'): return await denied(message,'manage')
        if not is_system_owner(message): return await system_denied(message)
        service=BackupService(ctx.repository.db,ctx.repository,ctx.repository.db.path.parent/'backups')
        try:
            result=service.create(kind='manual')
            await message.answer_document(FSInputFile(str(result.path)),caption=f'💾 Backup schema v{result.schema_version}\nSHA256: {result.checksum[:16]}…')
        except Exception as exc:
            await message.answer(f'⚠️ Backup не создан: {escape(str(exc)[:300])}')

    @dp.message(Command('backups'))
    async def cmd_backups(message: types.Message):
        if not allowed(message,'manage'): return await denied(message,'manage')
        if not is_system_owner(message): return await system_denied(message)
        rows=ctx.repository.recent_backups(10)
        if not rows: return await message.answer('💾 История backup пока пуста.')
        lines=['💾 <b>Последние backup/restore</b>']
        for r in rows:
            icon='✅' if r['status']=='success' else '❌'
            lines.append(f"{icon} {escape(r['kind'])} · {escape(r['filename'])} · schema v{r['schema_version']} · {escape(r['created_at'])}")
        await message.answer('\n'.join(lines),parse_mode='HTML')

    @dp.message(Command('restore'))
    async def cmd_restore(message: types.Message, state: FSMContext):
        if not allowed(message,'manage'): return await denied(message,'manage')
        if not is_system_owner(message): return await system_denied(message)
        if registry is None: return await message.answer('Restore требует multi-shop runtime.')
        await state.set_state(RestoreStates.waiting_file)
        await message.answer('⚠️ <b>Восстановление базы</b>\nПришлите SQLite backup (.sqlite3 или .db). Перед заменой бот автоматически создаст pre-restore backup и проверит integrity/schema.\nНажмите «❌ Отмена», чтобы выйти.',parse_mode='HTML',reply_markup=input_keyboard())

    @dp.message(StateFilter(RestoreStates.waiting_file), F.document)
    async def restore_file(message: types.Message, state: FSMContext):
        if not allowed(message,'manage'): return await denied(message,'manage')
        if not is_system_owner(message): return await system_denied(message)
        doc=message.document; filename=Path(doc.file_name or 'backup.sqlite3').name
        if Path(filename).suffix.lower() not in {'.sqlite3','.db','.sqlite'}:
            return await message.answer('⚠️ Нужен SQLite-файл .sqlite3/.db/.sqlite')
        if doc.file_size and doc.file_size > 200*1024*1024:
            return await message.answer('⚠️ Backup больше 200 МБ; восстановите его через файловую систему сервера.')
        if any(x.job_lock.locked() for x in registry.contexts()):
            return await message.answer('⏳ Сейчас идёт загрузка данных. Повторите restore после её завершения.')
        await message.answer('🛠 Проверяю backup и создаю страховочную копию…')
        result=None
        try:
            with tempfile.TemporaryDirectory(prefix='seller-bot-restore-') as tmp:
                local=Path(tmp)/filename
                remote=await message.bot.get_file(doc.file_id)
                await message.bot.download_file(remote.file_path,destination=str(local))
                async with registry.maintenance_lock:
                    service=BackupService(ctx.repository.db,ctx.repository,ctx.repository.db.path.parent/'backups')
                    result=service.restore(local)
                    # restore() atomically installed and validated the DB. Runtime
                    # state is rebuilt only after that point.
                    if not ctx.repository.acquire_lease('singleton:telegram-poller',ctx.settings.instance_id,90,metadata={'role':'poller'}):
                        raise RuntimeError('База восстановлена, но не удалось переизбрать singleton polling lease.')
                    await registry.reload()
        except Exception as exc:
            await state.clear()
            if result is None:
                return await message.answer(f'❌ Restore не выполнен; исходная база сохранена: {escape(str(exc)[:500])}',reply_markup=keyboard_for(message))
            return await message.answer(
                '⚠️ <b>База восстановлена, но runtime не перезагрузился полностью.</b>\n'
                f'{escape(str(exc)[:500])}\nПерезапустите приложение перед дальнейшей работой.',
                parse_mode='HTML',reply_markup=keyboard_for(message))
        await state.clear()
        await message.answer(f'✅ База восстановлена. Текущая schema v{result.schema_version}. Runtime магазинов перезагружен.',reply_markup=keyboard_for(message))

    # --- setup wizard ----------------------------------------------------
    @dp.message(Command('cancel'))
    async def cmd_cancel(message: types.Message, state: FSMContext):
        if not allowed(message): return await denied(message)
        await state.clear(); await message.answer('↩️ Текущий мастер отменён.', reply_markup=keyboard_for(message))

    @dp.message(Command('setup'))
    async def cmd_setup(message: types.Message, state: FSMContext):
        if not allowed(message,'manage'): return await denied(message,'manage')
        await state.clear(); await state.set_state(SetupStates.shop_name)
        shop=ctx.repository.get_shop(ctx.shop_id)
        await message.answer(
            '🧩 <b>Первичная настройка · 1/6</b>\n'
            f'Введите название магазина. Сейчас: <b>{escape(shop.name if shop else "Основной магазин")}</b>\n\n'
            'Для отмены нажмите «❌ Отмена».', parse_mode='HTML', reply_markup=input_keyboard())

    @dp.message(StateFilter(SetupStates.shop_name))
    async def setup_name(message: types.Message, state: FSMContext):
        if not allowed(message,'manage'): return await denied(message,'manage')
        value=(message.text or '').strip()
        if not value or value.startswith('/'):
            return await message.answer('Введите обычным текстом название магазина.')
        try:
            ctx.repository.rename_shop(ctx.shop_id,value)
        except ValueError as exc:
            return await message.answer(f'⚠️ {escape(str(exc))}')
        await state.set_state(SetupStates.timezone)
        await message.answer('🧩 <b>2/6</b> Введите часовой пояс IANA.\nПример: <code>Europe/Moscow</code>',parse_mode='HTML')

    @dp.message(StateFilter(SetupStates.timezone))
    async def setup_timezone(message: types.Message, state: FSMContext):
        if not allowed(message,'manage'): return await denied(message,'manage')
        try: update_validated(ctx.repository,ctx.shop_id,timezone=(message.text or '').strip())
        except ValueError as exc: return await message.answer(f'⚠️ {escape(str(exc))}')
        await state.set_state(SetupStates.report_time)
        await message.answer('🧩 <b>3/6</b> Время ежедневного отчёта в формате <code>HH:MM</code>.\nНапример: <code>09:00</code>',parse_mode='HTML')

    @dp.message(StateFilter(SetupStates.report_time))
    async def setup_report_time(message: types.Message, state: FSMContext):
        if not allowed(message,'manage'): return await denied(message,'manage')
        try: update_validated(ctx.repository,ctx.shop_id,report_time=(message.text or '').strip())
        except ValueError as exc: return await message.answer(f'⚠️ {escape(str(exc))}')
        await state.set_state(SetupStates.stock_risk_days)
        await message.answer('🧩 <b>4/6</b> За сколько дней до окончания запаса предупреждать?\nНапример: <code>14</code>',parse_mode='HTML')

    @dp.message(StateFilter(SetupStates.stock_risk_days))
    async def setup_stock(message: types.Message, state: FSMContext):
        if not allowed(message,'manage'): return await denied(message,'manage')
        try: update_validated(ctx.repository,ctx.shop_id,stock_risk_days=(message.text or '').strip())
        except ValueError as exc: return await message.answer(f'⚠️ {escape(str(exc))}')
        await state.set_state(SetupStates.order_drop_pct)
        await message.answer('🧩 <b>5/6</b> При каком падении заказов присылать алерт? В процентах.\nНапример: <code>35</code>',parse_mode='HTML')

    @dp.message(StateFilter(SetupStates.order_drop_pct))
    async def setup_order_drop(message: types.Message, state: FSMContext):
        if not allowed(message,'manage'): return await denied(message,'manage')
        try: update_validated(ctx.repository,ctx.shop_id,alert_order_drop_pct=(message.text or '').strip())
        except ValueError as exc: return await message.answer(f'⚠️ {escape(str(exc))}')
        await state.set_state(SetupStates.drr_pct)
        await message.answer('🧩 <b>6/6</b> Порог ДРР для предупреждения, %.\nНапример: <code>25</code>',parse_mode='HTML')

    @dp.message(StateFilter(SetupStates.drr_pct))
    async def setup_drr(message: types.Message, state: FSMContext):
        if not allowed(message,'manage'): return await denied(message,'manage')
        try:
            update_validated(ctx.repository,ctx.shop_id,alert_drr_pct=(message.text or '').strip(),setup_completed=True,onboarding_version='19')
        except ValueError as exc:
            return await message.answer(f'⚠️ {escape(str(exc))}')
        await state.clear()
        report=await build_readiness(ctx,live=False,persist=True)
        await send(message,'✅ <b>Первичная настройка завершена.</b>\n\n'+settings_text()+'\n\n'+format_readiness(report))

    @dp.message(Command('readiness'))
    async def cmd_readiness(message: types.Message):
        if not allowed(message): return await denied(message)
        await send(message,format_readiness(await build_readiness(ctx,live=False,persist=True)))

    @dp.message(Command('connect_check'))
    async def cmd_connect_check(message: types.Message):
        if not allowed(message,'operate'): return await denied(message,'operate')
        await message.answer('🔌 Проверяю реальные подключения и доступные категории API…')
        await send(message,format_readiness(await build_readiness(ctx,live=True,persist=True)))

    @dp.message(Command('help'))
    async def cmd_help(message: types.Message):
        if not allowed(message): return await denied(message)
        shop=ctx.repository.get_shop(ctx.shop_id)
        await send(message,format_onboarding_help(shop.credential_profile if shop else 'DEFAULT'))

    @dp.message(Command('demo_on'))
    async def cmd_demo_on(message: types.Message):
        if not allowed(message,'manage'): return await denied(message,'manage')
        try:
            result=enable_demo(ctx.repository,ctx.shop_id)
        except ValueError as exc:
            return await send(message,'⚠️ '+escape(str(exc)))
        await send(message,f'🧪 <b>Демо-режим включён.</b>\nСоздано товаров: {result["products"]}. Внешние API для этого магазина не вызываются.\n\nОткройте «📊 Отчёты», «📦 Товары и SKU» и «🚚 Поставки».')

    @dp.message(Command('demo_off'))
    async def cmd_demo_off(message: types.Message):
        if not allowed(message,'manage'): return await denied(message,'manage')
        disable_demo(ctx.repository,ctx.shop_id)
        await send(message,'🟢 <b>Демо-режим выключен.</b> Теперь обновления снова используют реальные API, если ключи настроены.')

    @dp.message(Command('settings'))
    async def cmd_settings(message: types.Message):
        if not allowed(message): return await denied(message)
        await send(message,settings_text())

    # --- imports / product linking --------------------------------------
    @dp.message(Command('import_costs'))
    async def cmd_import_costs(message: types.Message, state: FSMContext):
        if not allowed(message,'operate'): return await denied(message,'operate')
        await state.set_state(CostImportStates.waiting_file)
        await message.answer(
            '📥 <b>Импорт себестоимости</b>\nПришлите CSV или XLSX файлом.\n\n'
            'Обязательные колонки: <code>marketplace, sku, cost_price</code>\n'
            'Необязательные: <code>effective_date, internal_sku, name</code>.\n'
            'marketplace: <code>wb</code> или <code>ozon</code>.\n\n'
            'Если WB и Ozon строки имеют одинаковый <code>internal_sku</code>, листинги будут объединены в один товар.\n'
            'Для отмены нажмите «❌ Отмена».', parse_mode='HTML', reply_markup=input_keyboard())

    @dp.message(StateFilter(CostImportStates.waiting_file), F.document)
    async def import_cost_file(message: types.Message, state: FSMContext):
        if not allowed(message,'operate'): return await denied(message,'operate')
        doc=message.document
        filename=Path(doc.file_name or 'costs.csv').name
        suffix=Path(filename).suffix.lower()
        if suffix not in {'.csv','.xlsx','.xlsm'}:
            return await message.answer('⚠️ Поддерживаются только CSV и XLSX.')
        if doc.file_size and doc.file_size > 10*1024*1024:
            return await message.answer('⚠️ Файл больше 10 МБ. Разделите импорт на несколько файлов.')
        await message.answer('⏳ Проверяю файл и применяю себестоимость…')
        try:
            with tempfile.TemporaryDirectory(prefix='seller-bot-import-') as tmp:
                local=Path(tmp)/filename
                remote=await message.bot.get_file(doc.file_id)
                await message.bot.download_file(remote.file_path,destination=str(local))
                summary=import_costs(ctx.repository,ctx.shop_id,local,
                                     default_effective_date=local_now().date().isoformat())
        except Exception as exc:
            await state.clear()
            return await message.answer(f'❌ Импорт не выполнен: {escape(str(exc)[:500])}')
        await state.clear()
        lines=[f'📥 <b>Импорт {escape(summary.filename)}</b>',
               f'Строк: {summary.total_rows} · применено: {summary.applied_rows} · ошибок: {len(summary.errors)}']
        if summary.errors:
            lines += ['', '<b>Первые ошибки</b>']
            for e in summary.errors[:8]:
                lines.append(f"• строка {e.get('row','?')}: {escape(str(e.get('error','')))}")
        await send(message,'\n'.join(lines))

    @dp.message(StateFilter(CostImportStates.waiting_file))
    async def import_cost_file_expected(message: types.Message):
        if not allowed(message,'operate'): return await denied(message,'operate')
        await message.answer('Пришлите именно CSV/XLSX как документ или нажмите «❌ Отмена».')

    @dp.message(Command('link'))
    async def cmd_link(message: types.Message):
        if not allowed(message,'operate'): return await denied(message,'operate')
        parts=(message.text or '').split()
        if len(parts)<3:
            return await message.answer('Формат: /link <internal_sku> wb:<sku> ozon:<sku>')
        internal=parts[1]; refs=[]
        try:
            for token in parts[2:]:
                market,sku=token.split(':',1)
                if not sku.strip(): raise ValueError
                refs.append((market,sku))
            product=ctx.repository.link_marketplace_listings(ctx.shop_id,internal,refs)
        except Exception as exc:
            return await message.answer(f'⚠️ Не удалось связать товары: {escape(str(exc)[:300])}')
        await message.answer(f'✅ Листинги связаны с товаром <b>{escape(product.internal_sku)}</b>.',parse_mode='HTML')

    # --- main commands ---------------------------------------------------
    @dp.message(Command('start'))
    async def cmd_start(message: types.Message):
        if not allowed(message): return await denied(message)
        p=pref(); shop=ctx.repository.get_shop(ctx.shop_id)
        setup_hint='' if p.setup_completed else '\n⚠️ Первичная настройка не завершена: откройте «🏪 Магазин и доступ» → «🧩 Мастер настройки».\n'
        readiness_hint='\n🧪 Магазин работает в DEMO-режиме.' if p.demo_mode else ''
        await message.answer(
            '🤖 <b>Seller Analytics · Ozon + Wildberries</b>\n'
            '━━━━━━━━━━━━━━━━\n'
            f'🏪 <b>{escape(shop.name if shop else "Магазин")}</b>\n'
            'Главная общая метрика — <b>заказанные товарные единицы</b>.\n'
            'Продажи, выкупы и выплаты не смешиваются с заказами.\n'
            + setup_hint + readiness_hint +
            '\nВсе функции доступны через кнопки. Выберите раздел:',
            parse_mode='HTML', reply_markup=main_keyboard(role_for(message)))

    @dp.message(Command('day'))
    async def cmd_day(message: types.Message):
        if not allowed(message,'operate'): return await denied(message,'operate')
        try: target=date.fromisoformat((message.text or '').split(maxsplit=1)[1])
        except (IndexError,ValueError): return await message.answer('Формат: /day YYYY-MM-DD')
        if target > local_now().date():
            return await message.answer('⚠️ Нельзя загружать отчёт за будущую дату.')
        await collect_and_report(message,target,True)

    @dp.message(Command('backfill'))
    async def cmd_backfill(message: types.Message):
        if not allowed(message,'operate'): return await denied(message,'operate')
        parts=(message.text or '').split()[1:]
        if not parts:
            return await show_backfill_source_picker(message)
        source='all'
        days=None
        for part in parts:
            low=part.strip().lower()
            if low in {'wb','wildberries'}:
                source='wildberries'
            elif low in {'ozon','all','both'}:
                source='all' if low in {'all','both'} else 'ozon'
            elif days is None:
                try: days=int(low)
                except ValueError:
                    return await message.answer('Формат: /backfill [7..90] [ozon|wb|all]')
            else:
                return await message.answer('Формат: /backfill [7..90] [ozon|wb|all]')
        days=max(1,min(days if days is not None else ctx.settings.backfill_days,90))
        end=local_now().date()-timedelta(days=1)
        start=end-timedelta(days=days-1)
        await execute_backfill(message,source=source,start=start,end=end)

    @dp.callback_query(F.data == 'backfill:stop')
    async def cb_backfill_stop(callback: types.CallbackQuery):
        if callback.from_user is None or not ctx.repository.can_user(callback.from_user.id,ctx.shop_id,'operate'):
            return await callback.answer('Недостаточно прав.',show_alert=True)
        if not ctx.cancel_backfill():
            return await callback.answer('Активной загрузки уже нет.',show_alert=True)
        await callback.answer('Останавливаю загрузку…')
        if callback.message:
            try:
                await callback.message.edit_text(
                    '🛑 <b>Останавливаю загрузку истории…</b>',
                    parse_mode='HTML')
            except Exception:
                pass

    @dp.callback_query(F.data == 'backfill:cancel')
    async def cb_backfill_cancel(callback: types.CallbackQuery, state: FSMContext):
        await state.clear()
        await callback.answer('Отменено')
        if callback.message:
            await callback.message.edit_text('❌ Загрузка истории отменена.')

    @dp.callback_query(F.data == 'backfill:source_picker')
    async def cb_backfill_source_picker(callback: types.CallbackQuery):
        if callback.from_user is None or not ctx.repository.can_user(callback.from_user.id,ctx.shop_id,'operate'):
            return await callback.answer('Недостаточно прав.',show_alert=True)
        await callback.answer()
        if callback.message:
            await callback.message.edit_text(
                '📥 <b>Загрузка истории</b>\nВыберите источник:',
                parse_mode='HTML',
                reply_markup=backfill_source_keyboard(
                    has_ozon=ctx.ozon_connection_id is not None,
                    has_wb=ctx.wb_connection_id is not None))

    @dp.callback_query(F.data.startswith('backfill:source:'))
    async def cb_backfill_source(callback: types.CallbackQuery):
        if callback.from_user is None or not ctx.repository.can_user(callback.from_user.id,ctx.shop_id,'operate'):
            return await callback.answer('Недостаточно прав.',show_alert=True)
        source=(callback.data or '').rsplit(':',1)[-1]
        if source=='ozon' and ctx.ozon_connection_id is None:
            return await callback.answer('Ozon не подключён.',show_alert=True)
        if source=='wildberries' and ctx.wb_connection_id is None:
            return await callback.answer('Wildberries не подключён.',show_alert=True)
        await callback.answer()
        if callback.message:
            await callback.message.edit_text(
                f'📥 <b>{backfill_source_label(source)}</b>\nВыберите период:',
                parse_mode='HTML',reply_markup=backfill_period_keyboard(source))

    @dp.callback_query(F.data.startswith('backfill:period:'))
    async def cb_backfill_period(callback: types.CallbackQuery):
        if callback.from_user is None or not ctx.repository.can_user(callback.from_user.id,ctx.shop_id,'operate'):
            return await callback.answer('Недостаточно прав.',show_alert=True)
        parts=(callback.data or '').split(':')
        if len(parts)!=4:
            return await callback.answer('Некорректный период.',show_alert=True)
        source=parts[2]
        try: days=int(parts[3])
        except ValueError: return await callback.answer('Некорректный период.',show_alert=True)
        days=max(1,min(days,90))
        end=local_now().date()-timedelta(days=1)
        start=end-timedelta(days=days-1)
        await callback.answer()
        if callback.message:
            await callback.message.edit_text(
                f'⏳ Запускаю {backfill_source_label(source)} за {days} дн.…',
                parse_mode='HTML')
            await execute_backfill(callback.message,source=source,start=start,end=end,actor_user_id=callback.from_user.id)

    @dp.callback_query(F.data.startswith('backfill:custom:'))
    async def cb_backfill_custom(callback: types.CallbackQuery, state: FSMContext):
        if callback.from_user is None or not ctx.repository.can_user(callback.from_user.id,ctx.shop_id,'operate'):
            return await callback.answer('Недостаточно прав.',show_alert=True)
        source=(callback.data or '').rsplit(':',1)[-1]
        await state.clear()
        await state.set_state(BackfillStates.custom_period)
        await state.update_data(backfill_source=source)
        await callback.answer()
        if callback.message:
            await callback.message.edit_text(
                f'📅 <b>{backfill_source_label(source)} · свой период</b>\n'
                'Введите две даты через пробел:\n<code>YYYY-MM-DD YYYY-MM-DD</code>\n'
                'Максимум 90 завершённых дней.',
                parse_mode='HTML')

    @dp.message(StateFilter(BackfillStates.custom_period), F.text)
    async def backfill_custom_period(message: types.Message, state: FSMContext):
        if not allowed(message,'operate'):
            await state.clear()
            return await denied(message,'operate')
        parts=(message.text or '').split()
        if len(parts)!=2:
            return await message.answer('⚠️ Введите две даты: <code>YYYY-MM-DD YYYY-MM-DD</code>',parse_mode='HTML')
        try:
            start=date.fromisoformat(parts[0]); end=date.fromisoformat(parts[1])
        except ValueError:
            return await message.answer('⚠️ Неверный формат даты. Пример: <code>2026-09-01 2026-09-29</code>',parse_mode='HTML')
        data=await state.get_data()
        source=str(data.get('backfill_source') or 'all')
        await state.clear()
        await execute_backfill(message,source=source,start=start,end=end)

    @dp.message(Command('products'))
    async def cmd_products(message: types.Message):
        if not allowed(message): return await denied(message)
        p=pref(); parts=(message.text or '').split()
        try: days=int(parts[1]) if len(parts)>1 else p.product_report_days
        except ValueError: return await message.answer('Формат: /products или /products 7')
        days=max(1,min(days,30)); end=local_now().date()-timedelta(days=1)
        await send(message,format_product_report(build_product_report(ctx.repository,ctx.shop_id,end,
            days=days,stock_lookback_days=p.stock_velocity_days,stock_risk_days=p.stock_risk_days)))

    @dp.message(Command('stocks'))
    async def cmd_stocks(message: types.Message):
        if not allowed(message): return await denied(message)
        p=pref()
        if allowed(message,'operate'):
            if ctx.job_lock.locked(): return await message.answer('⏳ Уже выполняется другая выгрузка.')
            await message.answer('📦 Обновляю текущие остатки WB и Ozon…')
            try: outcomes=await ctx.collect_inventory(local_now().date())
            except Exception as exc:
                await message.answer(f'⚠️ Ошибка обновления остатков: {escape(str(exc)[:250])}'); outcomes=[]
            if any(not x.ok for x in outcomes):
                await message.answer('⚠️ Часть источников остатков не обновилась. Ранее успешные снимки сохранены.')
        else:
            await message.answer('👁 Режим viewer: показываю последний сохранённый снимок без обращения к API.')
        end=local_now().date()-timedelta(days=1)
        await send(message,format_stock_report(build_product_report(ctx.repository,ctx.shop_id,end,
            days=p.product_report_days,stock_lookback_days=p.stock_velocity_days,stock_risk_days=p.stock_risk_days)))

    @dp.message(Command('finance'))
    async def cmd_finance(message: types.Message):
        if not allowed(message): return await denied(message)
        p=pref(); parts=(message.text or '').split()
        try: days=int(parts[1]) if len(parts)>1 else p.finance_lookback_days
        except ValueError: return await message.answer('Формат: /finance или /finance 7')
        days=max(1,min(days,31)); end=local_now().date()-timedelta(days=1); start=end-timedelta(days=days-1)
        if allowed(message,'operate') and not ctx.job_lock.locked():
            await message.answer(f'💰 Обновляю финансовые данные {start} — {end}…')
            outcomes=await ctx.collect_finance(start,end)
            try: outcomes += await ctx.collect_advertising(start,end)
            except Exception as exc: await message.answer(f'⚠️ Реклама обновилась не полностью: {escape(str(exc)[:180])}')
            if any(not x.ok for x in outcomes):
                await message.answer('⚠️ Часть финансовых источников не обновилась; старые успешные данные сохранены.')
        elif not allowed(message,'operate'):
            await message.answer('👁 Режим viewer: показываю сохранённые финансы без обновления API.')
        await send(message,format_finance(build_finance_report(ctx.repository,ctx.shop_id,end,days)))

    @dp.message(Command('ads'))
    async def cmd_ads(message: types.Message):
        if not allowed(message): return await denied(message)
        p=pref(); parts=(message.text or '').split()
        try: days=int(parts[1]) if len(parts)>1 else p.finance_lookback_days
        except ValueError: return await message.answer('Формат: /ads или /ads 7')
        days=max(1,min(days,31)); end=local_now().date()-timedelta(days=1); start=end-timedelta(days=days-1)
        if allowed(message,'operate') and not ctx.job_lock.locked():
            await message.answer(f'📣 Обновляю рекламную статистику {start} — {end}…')
            try:
                outcomes=await ctx.collect_advertising(start,end)
                if any(not x.ok for x in outcomes):
                    await message.answer('⚠️ Часть рекламных источников не обновилась; сохранённые данные не затёрты.')
            except Exception as exc:
                await message.answer(f'⚠️ Реклама обновилась не полностью: {escape(str(exc)[:220])}')
        elif not allowed(message,'operate'):
            await message.answer('👁 Режим viewer: показываю сохранённую рекламную статистику без обновления API.')
        await send(message,format_advertising(build_advertising_report(ctx.repository,ctx.shop_id,end,days)))

    @dp.message(Command('management'))
    async def cmd_management(message: types.Message):
        if not allowed(message): return await denied(message)
        p=pref(); parts=(message.text or '').split()
        try: days=int(parts[1]) if len(parts)>1 else p.finance_lookback_days
        except ValueError: return await message.answer('Формат: /management или /management 14')
        days=max(1,min(days,31)); end=local_now().date()-timedelta(days=1); start=end-timedelta(days=days-1)
        if allowed(message,'operate') and not ctx.job_lock.locked():
            await message.answer(f'📈 Обновляю финансовые и рекламные данные {start} — {end}…')
            try:
                outcomes=await ctx.collect_finance(start,end)
                outcomes += await ctx.collect_advertising(start,end)
                if any(not x.ok for x in outcomes):
                    await message.answer('⚠️ Часть источников не обновилась; расчёт использует доступные сохранённые данные.')
            except Exception as exc:
                await message.answer(f'⚠️ Обновление завершилось частично: {escape(str(exc)[:220])}')
        elif not allowed(message,'operate'):
            await message.answer('👁 Режим viewer: расчёт по уже сохранённым данным.')
        await send(message,format_management(build_management_report(ctx.repository,ctx.shop_id,end,days)))

    @dp.message(Command('sku_finance'))
    async def cmd_sku_finance(message: types.Message):
        if not allowed(message): return await denied(message)
        p=pref(); parts=(message.text or '').split()
        try: days=int(parts[1]) if len(parts)>1 else p.finance_lookback_days
        except ValueError: return await message.answer('Формат: /sku_finance или /sku_finance 14')
        days=max(1,min(days,31)); end=local_now().date()-timedelta(days=1)
        await send(message,format_sku_economics(build_sku_economics(ctx.repository,ctx.shop_id,end,days)))

    @dp.message(Command('reconcile'))
    async def cmd_reconcile(message: types.Message):
        if not allowed(message): return await denied(message)
        p=pref(); parts=(message.text or '').split()
        try: days=int(parts[1]) if len(parts)>1 else p.finance_lookback_days
        except ValueError: return await message.answer('Формат: /reconcile или /reconcile 14')
        days=max(1,min(days,31)); end=local_now().date()-timedelta(days=1); start=end-timedelta(days=days-1)
        if allowed(message,'operate'):
            if ctx.job_lock.locked():
                return await message.answer('⏳ Уже выполняется другая загрузка.')
            await message.answer(f'🔎 Обновляю цепочку событий {start} — {end}… Это тяжелее обычного отчёта: загружаются заказы, продажи/возвраты и финансы.')
            try:
                outcomes=await ctx.collect_reconciliation(start,end)
            except Exception as exc:
                await message.answer(f'⚠️ Сверка обновилась не полностью: {escape(str(exc)[:220])}')
            else:
                failed=sum(1 for x in outcomes if not x.ok)
                if failed:
                    await message.answer(f'⚠️ {failed} загрузок завершились ошибкой; ранее успешные данные не затёрты.')
        else:
            await message.answer('👁 Режим viewer: показываю последнюю сохранённую сверку без обновления API.')
        await send(message,format_reconciliation(build_reconciliation_report(ctx.repository,ctx.shop_id,end,days)))

    @dp.message(Command('cost'))
    async def cmd_cost(message: types.Message):
        if not allowed(message,'operate'): return await denied(message,'operate')
        parts=(message.text or '').split()
        if len(parts) not in {4,5}:
            return await message.answer('Формат: /cost wb 12345678 350 [YYYY-MM-DD]')
        market={'wb':'wildberries','wildberries':'wildberries','ozon':'ozon'}.get(parts[1].lower())
        if not market: return await message.answer('Маркетплейс: wb или ozon')
        try: cost=float(parts[3].replace(',','.')); day=date.fromisoformat(parts[4]).isoformat() if len(parts)==5 else local_now().date().isoformat()
        except ValueError: return await message.answer('Проверьте себестоимость и дату YYYY-MM-DD.')
        conn=next((c for c in ctx.repository.list_connections(ctx.shop_id) if c.marketplace==market and c.enabled),None)
        if not conn: return await message.answer('Этот маркетплейс не подключён.')
        if ctx.repository.set_product_cost_by_listing(conn.id,parts[2],cost,effective_date=day):
            await message.answer(f'✅ Себестоимость SKU {escape(parts[2])} с {day}: {cost:.2f} ₽')
        else: await message.answer('SKU пока не найден. Сначала выполните /backfill.')

    @dp.message(Command('alerts'))
    async def cmd_alerts(message: types.Message):
        if not allowed(message): return await denied(message)
        p=pref(); notes=[]
        if allowed(message,'operate'):
            engine=AlertEngine(ctx.repository,cooldown_minutes=p.alert_cooldown_minutes)
            notes=engine.evaluate(ctx.shop_id,today=local_now().date(),order_drop_pct=p.alert_order_drop_pct,
                order_lookback_days=p.alert_order_lookback_days,api_stale_hours=p.alert_api_stale_hours,
                drr_pct=p.alert_drr_pct,stock_risk_days=p.stock_risk_days,stock_velocity_days=p.stock_velocity_days)
        active=ctx.repository.active_alert_states(ctx.shop_id); lines=[]
        lines += ['🚨 <b>Изменения алертов</b>']+['• '+escape(n.message) for n in notes] if notes else ['✅ Новых изменений алертов нет.']
        if active:
            lines += ['', '<b>Активные проблемы</b>']
            for row in active[:20]:
                value='' if row.get('last_value') is None else f" · значение {float(row['last_value']):.1f}"
                lines.append(f"• {escape(str(row['rule_key']))} · {escape(str(row['subject_key']))}{value}")
        else: lines += ['', '🟢 Активных проблем нет.']
        await send(message,'\n'.join(lines))

    @dp.message(Command('actions'))
    async def cmd_actions(message: types.Message):
        if not allowed(message): return await denied(message)
        end=local_now().date()-timedelta(days=1)
        await send(message,format_action_center(build_action_center(ctx.repository,ctx.shop_id,end,persist=allowed(message,'operate'))))

    @dp.message(Command('action_history'))
    async def cmd_action_history(message: types.Message):
        if not allowed(message): return await denied(message)
        parts=(message.text or '').split()
        try: days=max(1,min(int(parts[1]) if len(parts)>1 else 14,90))
        except ValueError: return await message.answer('Период должен быть числом дней, например 14.')
        end=local_now().date()-timedelta(days=1); start=end-timedelta(days=days-1)
        rows=ctx.repository.action_history(ctx.shop_id,start.isoformat(),end.isoformat())
        await send(message,format_action_history(rows,days=days))

    @dp.message(Command('action_ack'))
    async def cmd_action_ack(message: types.Message):
        if not allowed(message,'operate'): return await denied(message,'operate')
        parts=(message.text or '').split(maxsplit=1)
        if len(parts)<2: return await message.answer('Укажите код действия из Action Center.')
        key=parts[1].strip()
        if ctx.repository.set_action_status(ctx.shop_id,key,'acknowledged',telegram_user_id=message.from_user.id):
            await message.answer('✅ Действие отмечено как принято. Оно останется видимым, пока фактическая проблема не исчезнет.')
        else: await message.answer('⚠️ Активное действие с таким кодом не найдено.')

    @dp.message(Command('action_snooze'))
    async def cmd_action_snooze(message: types.Message):
        if not allowed(message,'operate'): return await denied(message,'operate')
        parts=(message.text or '').split()
        if len(parts)<2: return await message.answer('Укажите код действия и, при необходимости, часы: <code>action_key 24</code>',parse_mode='HTML')
        key=parts[1]
        try: hours=max(1,min(int(parts[2]) if len(parts)>2 else 24,720))
        except ValueError: return await message.answer('Часы должны быть целым числом от 1 до 720.')
        if ctx.repository.set_action_status(ctx.shop_id,key,'snoozed',telegram_user_id=message.from_user.id,snooze_hours=hours):
            await message.answer(f'⏰ Действие отложено на {hours} ч. Критические факты при этом не удаляются из истории.')
        else: await message.answer('⚠️ Активное действие с таким кодом не найдено.')

    @dp.message(Command('inbound'))
    async def cmd_inbound(message: types.Message):
        if not allowed(message): return await denied(message)
        await send(message,format_inbound(ctx.repository,ctx.shop_id))

    @dp.message(Command('inbound_refresh'))
    async def cmd_inbound_refresh(message: types.Message):
        if not allowed(message,'operate'): return await denied(message,'operate')
        await message.answer('⏳ Обновляю активные поставки WB/Ozon…')
        try:
            await ctx.collect_inbound(local_now().date())
        except Exception as exc:
            await message.answer(f'⚠️ Не удалось полностью обновить поставки: {escape(str(exc)[:250])}. Показываю последний успешный снимок.')
        await send(message,format_inbound(ctx.repository,ctx.shop_id))

    @dp.message(Command('forecast_quality'))
    async def cmd_forecast_quality(message: types.Message):
        if not allowed(message): return await denied(message)
        end=local_now().date()-timedelta(days=1)
        report=evaluate_forecast_quality(ctx.repository,ctx.shop_id,end,persist=allowed(message,'operate'))
        await send(message,format_forecast_quality(report))

    @dp.message(Command('supply_calibration'))
    async def cmd_supply_calibration(message: types.Message):
        if not allowed(message): return await denied(message)
        end=local_now().date()-timedelta(days=1)
        report=build_supply_calibration(ctx.repository,ctx.shop_id,end,persist=False)
        await send(message,format_supply_calibration(report))

    @dp.message(Command('supply_calibration_refresh'))
    async def cmd_supply_calibration_refresh(message: types.Message):
        if not allowed(message,'operate'): return await denied(message,'operate')
        end=local_now().date()-timedelta(days=1)
        # Refresh the rolling backtest first; calibration must learn only from completed history.
        evaluate_forecast_quality(ctx.repository,ctx.shop_id,end,persist=True)
        report=build_supply_calibration(ctx.repository,ctx.shop_id,end,persist=True)
        await send(message,format_supply_calibration(report))

    @dp.message(Command('promotions'))
    async def cmd_promotions(message: types.Message):
        if not allowed(message): return await denied(message)
        parts=(message.text or '').split(maxsplit=1)
        try: days=max(1,min(int(parts[1]),180)) if len(parts)>1 else 60
        except ValueError: return await message.answer('Формат: /promotions [дней]')
        await send(message,format_promotions(ctx.repository,ctx.shop_id,local_now().date(),future_days=days))

    @dp.message(Command('promotions_refresh'))
    async def cmd_promotions_refresh(message: types.Message):
        if not allowed(message,'operate'): return await denied(message,'operate')
        await message.answer('⏳ Обновляю календарь акций WB/Ozon…')
        try:
            outcomes=await ctx.collect_promotions(local_now().date())
            if outcomes and not all(x.ok for x in outcomes):
                await message.answer('⚠️ Один из источников акций обновился частично. Показываю подтверждённые данные.')
        except Exception as exc:
            await message.answer(f'⚠️ Календарь обновлён не полностью: {escape(str(exc)[:250])}. Показываю последний успешный снимок.')
        await send(message,format_promotions(ctx.repository,ctx.shop_id,local_now().date(),future_days=60))

    @dp.message(Command('supply'))
    async def cmd_supply(message: types.Message):
        if not allowed(message): return await denied(message)
        parts=(message.text or '').split(maxsplit=1)
        try: days=int(parts[1]) if len(parts)>1 else None
        except ValueError: return await message.answer('Формат: /supply [дней]')
        end=local_now().date()-timedelta(days=1)
        plan=build_supply_plan(ctx.repository,ctx.shop_id,end,lookback_days=days,persist=False)
        await send(message,format_supply_plan(plan))

    @dp.message(Command('supply_refresh'))
    async def cmd_supply_refresh(message: types.Message):
        if not allowed(message,'operate'): return await denied(message,'operate')
        parts=(message.text or '').split(maxsplit=1)
        try: days=int(parts[1]) if len(parts)>1 else None
        except ValueError: return await message.answer('Формат: /supply_refresh [дней]')
        await message.answer('⏳ Обновляю остатки, поставки в пути и пересчитываю план…')
        try: await ctx.collect_inventory(local_now().date())
        except Exception as exc: await message.answer(f'⚠️ Остатки не обновлены: {escape(str(exc)[:250])}. Использую последний успешный снимок.')
        try: await ctx.collect_inbound(local_now().date())
        except Exception as exc: await message.answer(f'⚠️ Поставки в пути не обновлены: {escape(str(exc)[:250])}. Использую последний успешный снимок.')
        try: await ctx.collect_promotions(local_now().date())
        except Exception as exc: await message.answer(f'⚠️ Акции не обновлены: {escape(str(exc)[:250])}. Использую последний успешный календарь.')
        end=local_now().date()-timedelta(days=1)
        plan=build_supply_plan(ctx.repository,ctx.shop_id,end,lookback_days=days,persist=True)
        await send(message,format_supply_plan(plan))

    @dp.message(Command('supply_sku'))
    async def cmd_supply_sku(message: types.Message):
        if not allowed(message): return await denied(message)
        parts=(message.text or '').split(maxsplit=1)
        if len(parts)<2: return await message.answer('Формат: /supply_sku INTERNAL_SKU')
        sku=parts[1].strip(); end=local_now().date()-timedelta(days=1)
        plan=build_supply_plan(ctx.repository,ctx.shop_id,end,persist=False)
        row=next((r for r in plan.rows if r.internal_sku==sku),None)
        if row is None: return await message.answer('⚠️ Физический товар с таким internal_sku не найден.')
        await send(message,format_supply_product(row))

    @dp.message(Command('supply_settings'))
    async def cmd_supply_settings(message: types.Message):
        if not allowed(message): return await denied(message)
        p=ctx.repository.ensure_shop_supply_preferences(ctx.shop_id)
        text='\n'.join([
            '🚚 <b>Настройки планирования поставок</b>','━━━━━━━━━━━━━━━━',
            f'История: {p["lookback_days"]} дней',f'XYZ: {p["xyz_weeks"]} недель',
            f'Lead time default: {p["default_lead_time_days"]} дн.',
            f'Safety stock default: {p["default_safety_stock_days"]} дн.',
            f'Target stock default: {p["default_target_stock_days"]} дн.',
            f'Минимум истории: {p["min_history_days"]} дней',
            f'Недельная сезонность: {"✅" if p.get("seasonality_enabled",1) else "⏸"}',
            f'Горизонт оценки прогноза: {p.get("forecast_horizon_days",7)} дн.',
            f'Самокалибровка: {"✅" if p.get("auto_calibration_enabled",1) else "⏸"}',
            f'Макс. lead-буфер: {p.get("max_lead_buffer_days",7)} дн.',
            f'Макс. safety-буфер: {p.get("max_safety_buffer_days",7)} дн.',
            '', 'Товар: кнопка «✏️ Настроить SKU».',
            'Общие defaults (owner): кнопка «🧰 Defaults поставок».'])
        await send(message,text)

    @dp.message(Command('supply_defaults'))
    async def cmd_supply_defaults(message: types.Message):
        if not allowed(message,'manage'): return await denied(message,'manage')
        parts=(message.text or '').split()[1:]
        if len(parts)<3: return await message.answer('Формат: /supply_defaults LEAD SAFETY TARGET [LOOKBACK] [XYZ_WEEKS] [SEASONALITY_0_1] [FORECAST_HORIZON] [AUTO_CAL_0_1] [MAX_LEAD_BUFFER] [MAX_SAFETY_BUFFER]')
        try:
            values={'default_lead_time_days':int(parts[0]),'default_safety_stock_days':int(parts[1]),'default_target_stock_days':int(parts[2])}
            if len(parts)>3: values['lookback_days']=int(parts[3])
            if len(parts)>4: values['xyz_weeks']=int(parts[4])
            if len(parts)>5: values['seasonality_enabled']=int(parts[5])
            if len(parts)>6: values['forecast_horizon_days']=int(parts[6])
            if len(parts)>7: values['auto_calibration_enabled']=int(parts[7])
            if len(parts)>8: values['max_lead_buffer_days']=int(parts[8])
            if len(parts)>9: values['max_safety_buffer_days']=int(parts[9])
            p=ctx.repository.update_shop_supply_preferences(ctx.shop_id,**values)
        except ValueError as exc: return await message.answer(f'⚠️ {escape(str(exc))}')
        await message.answer('✅ Defaults планирования обновлены. Откройте «⚙️ Настройки поставок» для проверки.')

    @dp.message(Command('supply_set'))
    async def cmd_supply_set(message: types.Message):
        if not allowed(message,'manage'): return await denied(message,'manage')
        parts=(message.text or '').split()[1:]
        if len(parts)<4: return await message.answer('Формат: /supply_set SKU LEAD SAFETY TARGET [PACK] [MIN_ORDER]')
        sku=parts[0]
        try:
            kwargs={'lead_time_days':int(parts[1]),'safety_stock_days':int(parts[2]),'target_stock_days':int(parts[3])}
            if len(parts)>4: kwargs['pack_size']=float(parts[4].replace(',','.'))
            if len(parts)>5: kwargs['min_order_qty']=float(parts[5].replace(',','.'))
            ctx.repository.set_product_supply_settings(ctx.shop_id,sku,**kwargs)
        except ValueError as exc: return await message.answer(f'⚠️ {escape(str(exc))}')
        await message.answer(f'✅ Параметры поставки для <code>{escape(sku)}</code> сохранены.',parse_mode='HTML')

    @dp.message(Command('status'))
    async def cmd_status(message: types.Message):
        if not allowed(message): return await denied(message)
        lines=['⚙️ <b>Статус источников</b>','━━━━━━━━━━━━━━━━']
        for conn in ctx.repository.list_connections(ctx.shop_id):
            latest=ctx.repository.latest_run(conn.id); success=ctx.repository.last_successful_run(conn.id)
            icon='🟣' if conn.marketplace=='ozon' else '🔵'
            lines.append(f'{icon} {escape(conn.display_name)}: {"✅" if conn.enabled else "⏸"}')
            lines.append(f'  последний успех: {success.finished_at if success else "—"}')
            if latest and latest.status=='failed': lines.append(f'  ⚠️ {escape((latest.error or "ошибка")[:180])}')
        await send(message,'\n'.join(lines))

    @dp.message(Command('health'))
    async def cmd_health(message: types.Message):
        if not allowed(message): return await denied(message)
        if registry is None: return await message.answer('Health runtime не подключён.')
        report=build_health(registry,deep=False,shop_id=ctx.shop_id,include_runtime_details=is_system_owner(message))
        await send(message,format_health(report))

    @dp.message(Command('jobs'))
    async def cmd_jobs(message: types.Message):
        if not allowed(message,'operate'): return await denied(message,'operate')
        counts=ctx.repository.retry_job_counts(ctx.shop_id); rows=ctx.repository.recent_retry_jobs(12,shop_id=ctx.shop_id)
        lines=['🧰 <b>Persistent retry queue</b>','━━━━━━━━━━━━━━━━',
               f'pending: {counts.get("pending",0)} · running: {counts.get("running",0)} · dead: {counts.get("dead",0)} · success: {counts.get("success",0)}']
        for r in rows:
            icon={'pending':'⏳','running':'▶️','success':'✅','dead':'❌'}.get(str(r['status']),'•')
            lines.append(f'{icon} #{r["id"]} · <code>{escape(str(r["job_type"]))}</code> · попыток {r["attempts"]}/{r["max_attempts"]}')
            if r.get('last_error'): lines.append(f'  {escape(str(r["last_error"])[:140])}')
        if allowed(message,'manage'): lines += ['', 'Повторить вручную: кнопка «🔁 Повторить retry-задачу».']
        await send(message,'\n'.join(lines))

    @dp.message(Command('job_retry'))
    async def cmd_job_retry(message: types.Message):
        if not allowed(message,'manage'): return await denied(message,'manage')
        parts=(message.text or '').split(maxsplit=1)
        if len(parts)<2: return await message.answer('Формат: /job_retry ID')
        try: job_id=int(parts[1])
        except ValueError: return await message.answer('ID задачи должен быть числом.')
        if not ctx.repository.requeue_retry_job(job_id,shop_id=ctx.shop_id): return await message.answer('⚠️ Задача не найдена в текущем магазине.')
        await message.answer(f'✅ Retry-задача #{job_id} возвращена в очередь.')

    @dp.message(Command('diagnostics'))
    async def cmd_diagnostics(message: types.Message):
        if not allowed(message): return await denied(message)
        repo=ctx.repository; shop=repo.get_shop(ctx.shop_id)
        retry=repo.retry_job_counts(ctx.shop_id)
        active_alerts=repo.active_alert_states(ctx.shop_id)
        recent_days=repo.recent_metric_days(ctx.shop_id,limit=10)
        text='\n'.join([
            '🧪 <b>Диагностика текущего магазина</b>','━━━━━━━━━━━━━━━━',
            f'Магазин: <b>{escape(shop.name if shop else str(ctx.shop_id))}</b>',
            f'Целостность SQLite: {"✅" if repo.db.quick_check() else "❌"}',
            f'Версия схемы: {repo.db.schema_version()}',
            f'Подключений магазина: {len(repo.list_connections(ctx.shop_id))}',
            f'Полных/частичных дат в истории: {len(recent_days)} из последних 10 проверенных',
            f'Активных алертов: {len(active_alerts)}',
            f'Retry: pending {retry.get("pending",0)} · running {retry.get("running",0)} · dead {retry.get("dead",0)}',
            f'Ozon Performance: {"✅" if ctx.collector.ozon_performance is not None else "—"}',
            f'Настроено core-источников: {ctx.configured_sources()}',
        ])
        if is_system_owner(message):
            text += f'\nSystem DB: <code>{escape(str(repo.db.path))}</code>'
        await send(message,text)

    # --- structured menu navigation --------------------------------------
    async def show_submenu(message: types.Message, title: str, keyboard):
        if not allowed(message): return await denied(message)
        await message.answer(title,parse_mode='HTML',reply_markup=keyboard(role_for(message),system_owner=is_system_owner(message)))

    @dp.message(F.text == MENU_REPORTS)
    async def menu_reports(message: types.Message):
        await show_submenu(message,'📊 <b>Отчёты</b>\nДень, периоды, история и загрузка данных.',reports_keyboard)

    @dp.message(F.text == MENU_PRODUCTS)
    async def menu_products(message: types.Message):
        await show_submenu(message,'📦 <b>Товары и SKU</b>\nАссортимент, остатки, себестоимость и связка листингов.',products_keyboard)

    @dp.message(F.text == MENU_MONEY)
    async def menu_money(message: types.Message):
        await show_submenu(message,'💰 <b>Деньги и реклама</b>\nФинансы, реклама, управленческий результат и сверка.',money_keyboard)

    @dp.message(F.text == MENU_SUPPLY)
    async def menu_supply(message: types.Message):
        await show_submenu(message,'🚚 <b>Поставки</b>\nABC/XYZ, прогноз спроса и параметры пополнения.',supply_keyboard)

    @dp.message(F.text == MENU_CONTROL)
    async def menu_control(message: types.Message):
        await show_submenu(message,'🚨 <b>Контроль</b>\nАлерты, состояние API, health-check и retry-очередь.',control_keyboard)

    @dp.message(F.text == MENU_SHOP)
    async def menu_shop(message: types.Message):
        await show_submenu(message,'🏪 <b>Магазин и доступ</b>\nМагазины, роли сотрудников, профили ключей и настройки.',shop_keyboard)

    @dp.message(F.text == MENU_SERVICE)
    async def menu_service(message: types.Message):
        await show_submenu(message,'🛠 <b>Сервис</b>\nЭкспорт и резервное копирование.',service_keyboard)

    @dp.message(F.text == BACK)
    async def menu_back(message: types.Message, state: FSMContext):
        # Reply keyboards do not have a navigation stack; BACK intentionally
        # returns to the stable root instead of guessing the previous screen.
        await state.clear()
        await show_main_menu(message)

    # Menu buttons for commands requiring one textual argument line.  Users
    # never need to remember a slash command: the button explains exactly what
    # value is expected and the next message is routed to the same command
    # implementation used by power users.
    input_actions = {
        'shop': ('shop', cmd_shop),
        'shop_add': ('shop_add', cmd_shop_add),
        'shop_profile': ('shop_profile', cmd_shop_profile),
        'shop_archive': ('shop_archive', cmd_shop_archive),
        'shop_restore': ('shop_restore', cmd_shop_restore),
        'shop_delete': ('shop_delete', cmd_shop_delete),
        'user_add': ('user_add', cmd_user_add),
        'user_remove': ('user_remove', cmd_user_remove),
        'export': ('export', cmd_export),
        'day': ('day', cmd_day),
        'link': ('link', cmd_link),
        'cost': ('cost', cmd_cost),
        'supply_sku': ('supply_sku', cmd_supply_sku),
        'supply_defaults': ('supply_defaults', cmd_supply_defaults),
        'supply_set': ('supply_set', cmd_supply_set),
        'job_retry': ('job_retry', cmd_job_retry),
        'action_ack': ('action_ack', cmd_action_ack),
        'action_snooze': ('action_snooze', cmd_action_snooze),
    }

    prompts = {
        'shop': '🔁 <b>Выбор магазина</b>\nВведите ID магазина. Его можно посмотреть кнопкой «🏪 Список магазинов».',
        'shop_add': '➕ <b>Новый магазин</b>\nВведите: <code>Название | PROFILE</code>\nПример: <code>Мой второй магазин | SHOP2</code>',
        'shop_profile': '🔐 <b>Профиль ключей</b>\nВведите имя профиля окружения, например <code>SHOP2</code>.',
        'shop_archive': '🗄 <b>Архивировать магазин</b>\nВведите ID магазина. Данные сохранятся, scheduler и API-запросы остановятся.',
        'shop_restore': '♻️ <b>Вернуть магазин</b>\nВведите ID магазина из «🗂 Архив магазинов».',
        'shop_delete': '🗑 <b>Удалить магазин</b>\nБезвозвратно удаляет только архивный магазин. Введите: <code>ID DELETE</code>.',
        'user_add': '➕ <b>Дать доступ</b>\nВведите: <code>TELEGRAM_ID viewer|analyst|owner [Имя]</code>',
        'user_remove': '➖ <b>Отозвать доступ</b>\nВведите Telegram ID пользователя.',
        'export': '📤 <b>Экспорт</b>\nВведите: <code>дней формат</code>\nНапример: <code>30 xlsx</code> или <code>90 csv</code>.',
        'day': '🗓 <b>Отчёт по дате</b>\nВведите дату в формате <code>YYYY-MM-DD</code>.',
        'link': '🔗 <b>Связать листинги</b>\nВведите: <code>INTERNAL_SKU wb:SKU ozon:SKU</code>',
        'cost': '💲 <b>Себестоимость</b>\nВведите: <code>wb|ozon SKU сумма [YYYY-MM-DD]</code>\nПример: <code>wb 12345678 350 2026-09-01</code>',
        'supply_sku': '🔍 <b>Поставка по SKU</b>\nВведите <code>internal_sku</code> физического товара.',
        'supply_defaults': '🧰 <b>Defaults поставок</b>\nВведите: <code>LEAD SAFETY TARGET [LOOKBACK] [XYZ_WEEKS] [SEASONALITY_0_1] [FORECAST_HORIZON] [AUTO_CAL_0_1] [MAX_LEAD_BUFFER] [MAX_SAFETY_BUFFER]</code>',
        'supply_set': '✏️ <b>Настроить SKU</b>\nВведите: <code>SKU LEAD SAFETY TARGET [PACK] [MIN_ORDER]</code>',
        'job_retry': '🔁 <b>Повтор retry-задачи</b>\nВведите ID задачи из раздела «🧰 Retry-очередь».',
        'action_ack': '✅ <b>Принять действие</b>\nВведите код действия из Action Center, например <code>supply:42</code>.',
        'action_snooze': '⏰ <b>Отложить действие</b>\nВведите: <code>action_key часы</code>, например <code>supply:42 24</code>.',
    }

    permissions = {
        'shop': 'view', 'shop_add': 'manage', 'shop_profile': 'manage',
        'shop_archive': 'manage', 'shop_restore': 'manage', 'shop_delete': 'manage',
        'user_add': 'manage', 'user_remove': 'manage', 'export': 'view',
        'day': 'operate', 'link': 'operate', 'cost': 'operate',
        'supply_sku': 'view', 'supply_defaults': 'manage', 'supply_set': 'manage',
        'job_retry': 'manage', 'action_ack': 'operate', 'action_snooze': 'operate',
    }

    async def launch_input_action(message: types.Message, state: FSMContext, action: str):
        permission=permissions[action]
        if not allowed(message,permission): return await denied(message,permission)
        if action in {'shop_add','shop_profile','shop_archive','shop_restore','shop_delete'} and not is_system_owner(message):
            return await system_denied(message)
        await start_menu_input(message,state,action,prompts[action])

    @dp.message(F.text == COMMAND_BUTTONS['shop'])
    async def btn_menu_shop_select(message: types.Message,state: FSMContext):
        await state.clear(); await show_shop_picker(message,'select')
    @dp.message(F.text == COMMAND_BUTTONS['shop_add'])
    async def btn_menu_shop_add(message: types.Message,state: FSMContext): await launch_input_action(message,state,'shop_add')
    @dp.message(F.text == COMMAND_BUTTONS['shop_profile'])
    async def btn_menu_shop_profile(message: types.Message,state: FSMContext): await launch_input_action(message,state,'shop_profile')
    @dp.message(F.text == COMMAND_BUTTONS['shop_archive'])
    async def btn_menu_shop_archive(message: types.Message,state: FSMContext):
        await state.clear(); await show_shop_picker(message,'archive')
    @dp.message(F.text == COMMAND_BUTTONS['shop_restore'])
    async def btn_menu_shop_restore(message: types.Message,state: FSMContext):
        await state.clear(); await show_shop_picker(message,'restore')
    @dp.message(F.text == COMMAND_BUTTONS['shop_delete'])
    async def btn_menu_shop_delete(message: types.Message,state: FSMContext):
        await state.clear(); await show_shop_picker(message,'delete')
    @dp.message(F.text == COMMAND_BUTTONS['user_add'])
    async def btn_menu_user_add(message: types.Message,state: FSMContext): await launch_input_action(message,state,'user_add')
    @dp.message(F.text == COMMAND_BUTTONS['user_remove'])
    async def btn_menu_user_remove(message: types.Message,state: FSMContext): await launch_input_action(message,state,'user_remove')
    @dp.message(F.text == COMMAND_BUTTONS['export'])
    async def btn_menu_export(message: types.Message,state: FSMContext): await launch_input_action(message,state,'export')
    @dp.message(F.text == COMMAND_BUTTONS['day'])
    async def btn_menu_day(message: types.Message,state: FSMContext): await launch_input_action(message,state,'day')
    @dp.message(F.text == COMMAND_BUTTONS['link'])
    async def btn_menu_link(message: types.Message,state: FSMContext): await launch_input_action(message,state,'link')
    @dp.message(F.text == COMMAND_BUTTONS['cost'])
    async def btn_menu_cost(message: types.Message,state: FSMContext): await launch_input_action(message,state,'cost')
    @dp.message(F.text == COMMAND_BUTTONS['supply_sku'])
    async def btn_menu_supply_sku(message: types.Message,state: FSMContext): await launch_input_action(message,state,'supply_sku')
    @dp.message(F.text == COMMAND_BUTTONS['supply_defaults'])
    async def btn_menu_supply_defaults(message: types.Message,state: FSMContext): await launch_input_action(message,state,'supply_defaults')
    @dp.message(F.text == COMMAND_BUTTONS['supply_set'])
    async def btn_menu_supply_set(message: types.Message,state: FSMContext): await launch_input_action(message,state,'supply_set')
    @dp.message(F.text == COMMAND_BUTTONS['job_retry'])
    async def btn_menu_job_retry(message: types.Message,state: FSMContext): await launch_input_action(message,state,'job_retry')
    @dp.message(F.text == COMMAND_BUTTONS['action_ack'])
    async def btn_menu_action_ack(message: types.Message,state: FSMContext): await launch_input_action(message,state,'action_ack')
    @dp.message(F.text == COMMAND_BUTTONS['action_snooze'])
    async def btn_menu_action_snooze(message: types.Message,state: FSMContext): await launch_input_action(message,state,'action_snooze')

    @dp.message(StateFilter(MenuInputStates.waiting_value), F.text)
    async def menu_input_value(message: types.Message,state: FSMContext):
        data=await state.get_data(); action=str(data.get('menu_action') or '')
        pair=input_actions.get(action)
        if pair is None:
            await state.clear(); return await show_main_menu(message,text='⚠️ Действие меню устарело. Выберите его заново.')
        value=(message.text or '').strip()
        await state.clear()
        command,handler=pair
        await handler(command_copy(message,command,value))

    # Direct command buttons ------------------------------------------------
    @dp.message(F.text == COMMAND_BUTTONS['shops'])
    async def btn_menu_shops(message: types.Message): await cmd_shops(message)
    @dp.message(F.text == COMMAND_BUTTONS['profiles'])
    async def btn_menu_profiles(message: types.Message): await cmd_profiles(message)
    @dp.message(F.text == COMMAND_BUTTONS['shop_archived'])
    async def btn_menu_shop_archived(message: types.Message): await cmd_shop_archived(message)
    @dp.message(F.text == COMMAND_BUTTONS['users'])
    async def btn_menu_users(message: types.Message): await cmd_users(message)
    @dp.message(F.text == COMMAND_BUTTONS['my_access'])
    async def btn_menu_my_access(message: types.Message): await cmd_my_access(message)
    @dp.message(F.text == COMMAND_BUTTONS['backup'])
    async def btn_menu_backup(message: types.Message): await cmd_backup(message)
    @dp.message(F.text == COMMAND_BUTTONS['backups'])
    async def btn_menu_backups(message: types.Message): await cmd_backups(message)
    @dp.message(F.text == COMMAND_BUTTONS['restore'])
    async def btn_menu_restore(message: types.Message,state: FSMContext): await cmd_restore(message,state)
    @dp.message(F.text == COMMAND_BUTTONS['setup'])
    async def btn_menu_setup(message: types.Message,state: FSMContext): await cmd_setup(message,state)
    @dp.message(F.text == COMMAND_BUTTONS['settings'])
    async def btn_menu_settings(message: types.Message): await cmd_settings(message)
    @dp.message(F.text == COMMAND_BUTTONS['readiness'])
    async def btn_menu_readiness(message: types.Message): await cmd_readiness(message)
    @dp.message(F.text == COMMAND_BUTTONS['connect_check'])
    async def btn_menu_connect_check(message: types.Message): await cmd_connect_check(message)
    @dp.message(F.text == COMMAND_BUTTONS['help'])
    async def btn_menu_help(message: types.Message): await cmd_help(message)
    @dp.message(F.text == COMMAND_BUTTONS['demo_on'])
    async def btn_menu_demo_on(message: types.Message): await cmd_demo_on(message)
    @dp.message(F.text == COMMAND_BUTTONS['demo_off'])
    async def btn_menu_demo_off(message: types.Message): await cmd_demo_off(message)
    @dp.message(F.text == COMMAND_BUTTONS['import_costs'])
    async def btn_menu_import_costs(message: types.Message,state: FSMContext): await cmd_import_costs(message,state)
    @dp.message(F.text == COMMAND_BUTTONS['backfill'])
    async def btn_menu_backfill(message: types.Message): await cmd_backfill(command_copy(message,'backfill',''))
    @dp.message(F.text == COMMAND_BUTTONS['products'])
    async def btn_menu_products_report(message: types.Message): await cmd_products(command_copy(message,'products',''))
    @dp.message(F.text == COMMAND_BUTTONS['finance'])
    async def btn_menu_finance(message: types.Message): await cmd_finance(command_copy(message,'finance',''))
    @dp.message(F.text == COMMAND_BUTTONS['ads'])
    async def btn_menu_ads(message: types.Message): await cmd_ads(command_copy(message,'ads',''))
    @dp.message(F.text == COMMAND_BUTTONS['management'])
    async def btn_menu_management(message: types.Message): await cmd_management(command_copy(message,'management',''))
    @dp.message(F.text == COMMAND_BUTTONS['sku_finance'])
    async def btn_menu_sku_finance(message: types.Message): await cmd_sku_finance(command_copy(message,'sku_finance',''))
    @dp.message(F.text == COMMAND_BUTTONS['reconcile'])
    async def btn_menu_reconcile(message: types.Message): await cmd_reconcile(command_copy(message,'reconcile',''))
    @dp.message(F.text == COMMAND_BUTTONS['actions'])
    async def btn_menu_actions(message: types.Message): await cmd_actions(message)
    @dp.message(F.text == COMMAND_BUTTONS['action_history'])
    async def btn_menu_action_history(message: types.Message): await cmd_action_history(command_copy(message,'action_history',''))
    @dp.message(F.text == COMMAND_BUTTONS['alerts'])
    async def btn_menu_alerts(message: types.Message): await cmd_alerts(message)
    @dp.message(F.text == COMMAND_BUTTONS['supply'])
    async def btn_menu_supply_plan(message: types.Message): await cmd_supply(command_copy(message,'supply',''))
    @dp.message(F.text == COMMAND_BUTTONS['inbound'])
    async def btn_menu_inbound(message: types.Message): await cmd_inbound(message)
    @dp.message(F.text == COMMAND_BUTTONS['inbound_refresh'])
    async def btn_menu_inbound_refresh(message: types.Message): await cmd_inbound_refresh(message)
    @dp.message(F.text == COMMAND_BUTTONS['forecast_quality'])
    async def btn_menu_forecast_quality(message: types.Message): await cmd_forecast_quality(message)
    @dp.message(F.text == COMMAND_BUTTONS['supply_calibration'])
    async def btn_menu_supply_calibration(message: types.Message): await cmd_supply_calibration(message)
    @dp.message(F.text == COMMAND_BUTTONS['supply_calibration_refresh'])
    async def btn_menu_supply_calibration_refresh(message: types.Message): await cmd_supply_calibration_refresh(message)
    @dp.message(F.text == COMMAND_BUTTONS['promotions'])
    async def btn_menu_promotions(message: types.Message): await cmd_promotions(command_copy(message,'promotions',''))
    @dp.message(F.text == COMMAND_BUTTONS['promotions_refresh'])
    async def btn_menu_promotions_refresh(message: types.Message): await cmd_promotions_refresh(message)
    @dp.message(F.text == COMMAND_BUTTONS['supply_refresh'])
    async def btn_menu_supply_refresh(message: types.Message): await cmd_supply_refresh(command_copy(message,'supply_refresh',''))
    @dp.message(F.text == COMMAND_BUTTONS['supply_settings'])
    async def btn_menu_supply_settings(message: types.Message): await cmd_supply_settings(message)
    @dp.message(F.text == COMMAND_BUTTONS['status'])
    async def btn_menu_status(message: types.Message): await cmd_status(message)
    @dp.message(F.text == COMMAND_BUTTONS['health'])
    async def btn_menu_health(message: types.Message): await cmd_health(message)
    @dp.message(F.text == COMMAND_BUTTONS['jobs'])
    async def btn_menu_jobs(message: types.Message): await cmd_jobs(message)
    @dp.message(F.text == COMMAND_BUTTONS['diagnostics'])
    async def btn_menu_diagnostics(message: types.Message): await cmd_diagnostics(message)

    # --- keyboard buttons ------------------------------------------------
    @dp.message(F.text == '📊 Вчера')
    async def btn_yesterday(message: types.Message):
        if not allowed(message): return await denied(message)
        await collect_and_report(message,local_now().date()-timedelta(days=1),False)

    @dp.message(F.text == '🔄 Обновить вчера')
    async def btn_refresh(message: types.Message):
        if not allowed(message,'operate'): return await denied(message,'operate')
        await collect_and_report(message,local_now().date()-timedelta(days=1),True)

    @dp.message(F.text == '📊 Сегодня')
    async def btn_today(message: types.Message):
        if not allowed(message,'operate'): return await denied(message,'operate')
        await collect_and_report(message,local_now().date(),True)

    @dp.message(F.text == '📅 Неделя')
    async def btn_week(message: types.Message):
        if not allowed(message): return await denied(message)
        end=local_now().date()-timedelta(days=1)
        await send(message,format_period(build_period_report(ctx.repository,ctx.shop_id,end,7,'Неделя')))

    @dp.message(F.text == '📅 Месяц')
    async def btn_month(message: types.Message):
        if not allowed(message): return await denied(message)
        end=local_now().date()-timedelta(days=1)
        await send(message,format_period(build_period_report(ctx.repository,ctx.shop_id,end,30,'Последние 30 дней')))

    @dp.message(F.text == '🏆 Товары')
    async def btn_products(message: types.Message):
        if not allowed(message): return await denied(message)
        p=pref(); end=local_now().date()-timedelta(days=1)
        await send(message,format_product_report(build_product_report(ctx.repository,ctx.shop_id,end,
            days=p.product_report_days,stock_lookback_days=p.stock_velocity_days,stock_risk_days=p.stock_risk_days)))

    @dp.message(F.text == '📦 Остатки')
    async def btn_stocks(message: types.Message): await cmd_stocks(message)

    @dp.message(F.text == '🚚 Поставка')
    async def btn_supply(message: types.Message):
        if not allowed(message): return await denied(message)
        end=local_now().date()-timedelta(days=1)
        await send(message,format_supply_plan(build_supply_plan(ctx.repository,ctx.shop_id,end,persist=False)))

    @dp.message(F.text == '📈 Результат')
    async def btn_management(message: types.Message): await cmd_management(message)

    @dp.message(F.text == '🔎 Сверка')
    async def btn_reconcile(message: types.Message):
        if not allowed(message): return await denied(message)
        p=pref(); end=local_now().date()-timedelta(days=1)
        await send(message,format_reconciliation(build_reconciliation_report(ctx.repository,ctx.shop_id,end,p.finance_lookback_days)))

    @dp.message(F.text == '📜 История')
    async def btn_history(message: types.Message):
        if not allowed(message): return await denied(message)
        rows=ctx.repository.recent_metric_days(ctx.shop_id,limit=10)
        if not rows: return await send(message,'📜 История пока пуста. Откройте «📊 Отчёты» → «📥 Загрузить историю». ')
        await send(message,'\n'.join(['📜 <b>Последние даты</b>']+
            [f'{"✅" if got==total else "⏳"} {day} · источников {got}/{total}' for day,got,total in rows]))

    @dp.message(F.text == '🏪 Магазины')
    async def btn_shops(message: types.Message): await cmd_shops(message)

    @dp.message(F.text == '📤 Экспорт')
    async def btn_export(message: types.Message): await cmd_export(message)

    @dp.message(F.text == '💾 Backup')
    async def btn_backup(message: types.Message): await cmd_backup(message)

    @dp.message(F.text == '⚙️ Настройки')
    async def btn_settings(message: types.Message): await cmd_settings(message)

    @dp.message(F.text == '⚙️ Статус')
    async def btn_status(message: types.Message): await cmd_status(message)
