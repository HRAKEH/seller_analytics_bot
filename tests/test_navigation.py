from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import Bot, Dispatcher, types
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import (SendMessage, SendDocument, DeleteMessage, EditMessageText,
                            EditMessageReplyMarkup, AnswerCallbackQuery)
import pytest

from app.bot import AppContext, register_handlers
from app.bot import handlers
from app.bot.keyboards import MENU_REPORTS, MENU_PRODUCTS, HOME, CANCEL, COMMAND_BUTTONS
from app.bot.runtime import ContextProxy, ShopContextMiddleware
from app.bot.updates import TelegramUpdateMiddleware
from app.config import Settings
from app.services.collection import CollectionService
from app.storage import Database, Repository, LATEST_SCHEMA_VERSION


class TelegramMemory:
    """Exercise real handlers without contacting Telegram or marketplaces."""
    def __init__(self):
        self.messages={}
        self.keyboards={}
        self.methods=[]
        self.sequence=1000
        self.fail_deletes=set()
        self.fail_next_send=False

    async def request(self, bot, method, **kwargs):
        self.methods.append(method)
        if isinstance(method,(SendMessage,SendDocument)):
            if self.fail_next_send:
                self.fail_next_send=False
                raise TelegramNetworkError(method,'offline')
            self.sequence+=1
            chat=types.Chat(id=method.chat_id,type='private' if method.chat_id>0 else 'group')
            fields={'text':method.text,'reply_markup':method.reply_markup
                    if isinstance(method.reply_markup,types.InlineKeyboardMarkup) else None} if isinstance(method,SendMessage) else {
                'caption':method.caption,'document':types.Document(file_id='file',file_unique_id='unique')}
            message=types.Message(message_id=self.sequence,date=datetime.now(timezone.utc),chat=chat,
                from_user=types.User(id=bot.id,is_bot=True,first_name='Bot'),**fields).as_(bot)
            self.messages[(chat.id,message.message_id)]=message
            self.keyboards[(chat.id,message.message_id)]=method.reply_markup
            return message
        if isinstance(method,AnswerCallbackQuery):
            return True
        key=(method.chat_id,method.message_id)
        if isinstance(method,DeleteMessage):
            if method.message_id in self.fail_deletes:
                raise TelegramBadRequest(method,'message cannot be deleted')
            self.messages.pop(key,None)
            return True
        if isinstance(method,(EditMessageText,EditMessageReplyMarkup)):
            updates={'reply_markup':method.reply_markup}
            if isinstance(method,EditMessageText): updates['text']=method.text
            message=self.messages[key].model_copy(update=updates).as_(bot)
            self.messages[key]=message
            return message
        raise AssertionError(type(method).__name__)


@pytest.fixture
def ui(tmp_path):
    settings=replace(Settings.from_env(),telegram_token='123456:LOCAL_TEST',owner_ids=(101,))
    db=Database(tmp_path/'ui.sqlite3'); db.initialize()
    repo=Repository(db)
    seller=repo.ensure_seller(101,'Seller'); shop=repo.ensure_shop(seller.id,'Shop')
    repo.grant_shop_access(101,shop.id,'owner'); repo.grant_shop_access(102,shop.id,'viewer')
    repo.ensure_shop_preferences(shop.id)
    ctx=AppContext(settings,repo,shop.id,CollectionService(repo))
    telegram=TelegramMemory(); bot=Bot(settings.telegram_token)
    bot.session.make_request=AsyncMock(side_effect=telegram.request)
    def dispatcher():
        dp=Dispatcher()
        dp.update.outer_middleware(TelegramUpdateMiddleware('ui-test'))
        register_handlers(dp,ctx)
        return dp
    return SimpleNamespace(dp=dispatcher(),new_dispatcher=dispatcher,bot=bot,repo=repo,ctx=ctx,
                           shop=shop,telegram=telegram,update_id=0)


async def press(ui,text,*,user=101,chat=None,bot=None):
    bot=bot or ui.bot
    ui.update_id+=1
    chat=user if chat is None else chat
    message=types.Message(message_id=ui.update_id,date=datetime.now(timezone.utc),
        chat=types.Chat(id=chat,type='private' if chat>0 else 'group'),
        from_user=types.User(id=user,is_bot=False,first_name='User'),text=text,
        entities=[types.MessageEntity(type='bot_command',offset=0,length=len(text.split()[0]))]
                 if text.startswith('/') else None)
    ui.telegram.messages[(chat,message.message_id)]=message
    await ui.dp.feed_update(bot,types.Update(update_id=ui.update_id,message=message))
    return message.message_id


async def callback(ui,message_id,data,*,user=101):
    ui.update_id+=1
    message=ui.telegram.messages[(user,message_id)]
    await ui.dp.feed_update(ui.bot,types.Update(update_id=ui.update_id,callback_query=types.CallbackQuery(
        id=str(ui.update_id),from_user=types.User(id=user,is_bot=False,first_name='User'),
        chat_instance='chat',message=message,data=data)))


def active(ui,user=101,bot=None):
    return ui.repo.navigation_message((bot or ui.bot).id,user,user)


def inline_data(ui,message_id,label,*,user=101):
    markup=ui.telegram.messages[(user,message_id)].reply_markup
    return next(button.callback_data for row in markup.inline_keyboard for button in row if button.text==label)


@pytest.mark.asyncio
async def test_help_expands_and_collapses_in_same_menu_message_without_api(ui):
    ui.ctx.refresh_reports=AsyncMock(side_effect=AssertionError('Help must not load marketplaces'))
    await press(ui,HOME); menu=active(ui)
    await callback(ui,menu,inline_data(ui,menu,MENU_PRODUCTS))
    assert active(ui)==menu and '<b>Товары</b>' in ui.telegram.messages[(101,menu)].text
    await callback(ui,menu,inline_data(ui,menu,COMMAND_BUTTONS['stocks']))
    brief=ui.telegram.messages[(101,menu)].text
    await callback(ui,menu,inline_data(ui,menu,'Подробнее'))
    assert '3 штуки' in ui.telegram.messages[(101,menu)].text
    assert active(ui)==menu
    await callback(ui,menu,inline_data(ui,menu,'Свернуть'))
    assert ui.telegram.messages[(101,menu)].text==brief
    assert len([m for (chat,_),m in ui.telegram.messages.items() if chat==101 and m.from_user.is_bot])==1
    ui.ctx.refresh_reports.assert_not_called()


@pytest.mark.asyncio
async def test_open_from_help_preserves_new_period_picker(ui):
    from app.bot.keyboards import MENU_MONEY
    await press(ui,HOME); menu=active(ui)
    await callback(ui,menu,inline_data(ui,menu,MENU_MONEY))
    await callback(ui,menu,inline_data(ui,menu,COMMAND_BUTTONS['refresh']))
    await callback(ui,menu,inline_data(ui,menu,'▶️ Открыть'))
    picker=active(ui)
    assert picker!=menu and (101,picker) in ui.telegram.messages
    assert 'Выберите период кнопками' in ui.telegram.messages[(101,picker)].text
    assert inline_data(ui,picker,'Вчера').startswith('p:')


@pytest.mark.asyncio
async def test_calendar_range_runs_exactly_selected_dates_without_typing(ui):
    from datetime import date
    ui.ctx.refresh_reports=AsyncMock(return_value=[])
    await press(ui,COMMAND_BUTTONS['refresh']); picker=active(ui)
    await callback(ui,picker,inline_data(ui,picker,'🗓 Выбрать даты начала и конца'))
    markup=ui.telegram.messages[(101,picker)].reply_markup
    prefix=next(b.callback_data.rsplit(':',2)[0] for row in markup.inline_keyboard for b in row if ':d:' in b.callback_data)
    await callback(ui,picker,prefix+':d:20260928')
    assert 'Начало: 28.09.2026' in ui.telegram.messages[(101,picker)].text
    await callback(ui,picker,prefix+':d:20260930')
    assert '28.09.2026 — 30.09.2026' in ui.telegram.messages[(101,picker)].text
    ui.ctx.refresh_reports.assert_not_called()
    run=inline_data(ui,picker,'🔄 Обновить')
    await callback(ui,picker,run)
    args=ui.ctx.refresh_reports.await_args.args
    assert args==(date(2026,9,28),date(2026,9,30))
    assert active(ui) is None and (101,picker) not in ui.telegram.messages


@pytest.mark.asyncio
async def test_old_period_button_cannot_overwrite_new_choice_or_execute_after_role_revoke(ui):
    ui.ctx.refresh_reports=AsyncMock(return_value=[])
    await press(ui,COMMAND_BUTTONS['refresh']); old=active(ui)
    old_button=inline_data(ui,old,'Вчера')
    # Keep an old card to exercise a queued tap even if deletion is unavailable.
    ui.telegram.fail_deletes.add(old)
    await press(ui,COMMAND_BUTTONS['refresh']); current=active(ui)
    await callback(ui,old,old_button)
    assert 'Выберите период' in ui.telegram.messages[(101,current)].text
    await callback(ui,current,inline_data(ui,current,'Вчера'))
    run=inline_data(ui,current,'🔄 Обновить')
    ui.repo.grant_shop_access(101,ui.shop.id,'viewer')
    await callback(ui,current,run)
    ui.ctx.refresh_reports.assert_not_called()
    assert 'Период:' in ui.telegram.messages[(101,current)].text


@pytest.mark.asyncio
async def test_period_cancel_returns_to_working_main_menu(ui):
    await press(ui,COMMAND_BUTTONS['refresh']); picker=active(ui)
    await callback(ui,picker,inline_data(ui,picker,'❌ Отмена'))
    menu=active(ui)
    assert menu!=picker and (101,picker) not in ui.telegram.messages
    assert inline_data(ui,menu,MENU_REPORTS).startswith('mh:')


@pytest.mark.asyncio
async def test_viewer_can_read_wb_accruals_but_cannot_launch_forged_refresh(ui):
    from app.bot.keyboards import MENU_MONEY
    ui.ctx.refresh_reports=AsyncMock(side_effect=AssertionError('Viewer cannot update'))
    await press(ui,HOME,user=102); menu=active(ui,102)
    await callback(ui,menu,inline_data(ui,menu,MENU_MONEY,user=102),user=102)
    markup=ui.telegram.messages[(102,menu)].reply_markup
    labels={b.text for row in markup.inline_keyboard for b in row}
    assert COMMAND_BUTTONS['wb_accruals'] in labels and COMMAND_BUTTONS['refresh'] not in labels
    await callback(ui,menu,f'mh:{ui.shop.id}:102:money:refresh:run',user=102)
    ui.ctx.refresh_reports.assert_not_called()
    await callback(ui,menu,inline_data(ui,menu,COMMAND_BUTTONS['wb_accruals'],user=102),user=102)
    await callback(ui,menu,inline_data(ui,menu,'▶️ Открыть',user=102),user=102)
    picker=active(ui,102)
    await callback(ui,picker,inline_data(ui,picker,'Вчера',user=102),user=102)
    await callback(ui,picker,inline_data(ui,picker,'📄 Показать',user=102),user=102)
    assert active(ui,102) is None
    assert 'Начисления WB' in ui.telegram.messages[(102,ui.telegram.sequence)].text


def test_every_menu_function_has_plain_language_help_and_valid_callback(ui):
    from app.bot.menu_help import HELP, ALIASES, MenuHelp
    assert set(COMMAND_BUTTONS)<=set(HELP)
    assert set(ALIASES.values())-{f'{section}_menu' for section in ('reports','products','money','supply','control','shop','service','technical')}<=set(HELP)
    help_menu=MenuHelp(ui.ctx,None,{}, {},None,None,None,None)
    for key in HELP:
        brief,detail=HELP[key]
        assert brief and detail
        assert len(help_menu.data(1234567890123,'technical',key,'more').encode())<=64


@pytest.mark.asyncio
async def test_navigation_and_button_taps_are_removed_but_reports_and_commands_stay(ui):
    start=await press(ui,'/start'); first=active(ui)
    click=await press(ui,MENU_REPORTS); second=active(ui)
    assert first!=second and (101,first) not in ui.telegram.messages
    assert (101,click) not in ui.telegram.messages and (101,start) in ui.telegram.messages
    await press(ui,'📅 Неделя')
    report=ui.telegram.sequence
    assert active(ui) is None and (101,second) not in ui.telegram.messages
    assert 'Неделя' in ui.telegram.messages[(101,report)].text
    await press(ui,HOME); await press(ui,MENU_PRODUCTS)
    assert (101,report) in ui.telegram.messages


@pytest.mark.asyncio
async def test_date_callback_retires_only_its_selector_and_preserves_daily_report(ui):
    await press(ui,'🗓 Другая дата'); selector=active(ui)
    await callback(ui,selector,'report_date:1')
    report=ui.telegram.sequence
    assert (101,selector) not in ui.telegram.messages and active(ui) is None
    assert ui.telegram.keyboards[(101,report)] is not None
    await press(ui,HOME)
    assert (101,report) in ui.telegram.messages


@pytest.mark.asyncio
async def test_manual_date_and_parameter_values_are_kept(ui):
    await press(ui,'🗓 Другая дата'); selector=active(ui)
    await callback(ui,selector,'report_date:custom'); prompt=active(ui)
    assert (101,selector) not in ui.telegram.messages
    manual=await press(ui,'2026-09-28')
    assert (101,manual) in ui.telegram.messages and (101,prompt) not in ui.telegram.messages
    await press(ui,COMMAND_BUTTONS['cost']); prompt=active(ui)
    value=await press(ui,'wb 123 199.99')
    assert (101,value) in ui.telegram.messages and (101,prompt) not in ui.telegram.messages


@pytest.mark.asyncio
async def test_export_edits_one_selector_then_keeps_file(ui):
    await press(ui,'📤 Экспорт'); selector=active(ui)
    await callback(ui,selector,'data_view:days:7')
    assert active(ui)==selector
    assert 'Выберите формат' in ui.telegram.messages[(101,selector)].text
    await callback(ui,selector,'data_view:file:csv')
    document=ui.telegram.sequence
    assert ui.telegram.messages[(101,document)].document is not None
    assert active(ui)==selector and (101,selector) in ui.telegram.messages
    await press(ui,HOME)
    assert (101,document) in ui.telegram.messages and (101,selector) not in ui.telegram.messages


@pytest.mark.asyncio
@pytest.mark.parametrize('picker,data',[('🗓 Другая дата','report_date:cancel'),('📤 Экспорт','data_view:home')])
async def test_cancel_returns_to_one_working_main_menu(ui,picker,data):
    await press(ui,picker); selector=active(ui)
    await callback(ui,selector,data)
    current=active(ui)
    assert current!=selector and (101,selector) not in ui.telegram.messages
    assert ui.telegram.keyboards[(101,current)] is not None


@pytest.mark.asyncio
async def test_wizard_replaces_steps_without_deleting_typed_shop_name(ui):
    await press(ui,COMMAND_BUTTONS['setup']); first=active(ui)
    name=await press(ui,'Новый магазин'); second=active(ui)
    assert (101,first) not in ui.telegram.messages and first!=second
    assert (101,name) in ui.telegram.messages
    await press(ui,CANCEL)
    assert (101,second) not in ui.telegram.messages
    assert ui.telegram.keyboards[(101,active(ui))] is not None


@pytest.mark.asyncio
async def test_restarting_dispatcher_still_replaces_previous_menu(ui):
    await press(ui,HOME); previous=active(ui)
    ui.dp=ui.new_dispatcher()
    await press(ui,MENU_REPORTS)
    assert previous!=active(ui) and (101,previous) not in ui.telegram.messages


@pytest.mark.asyncio
async def test_cleanup_isolated_by_bot_chat_and_user_and_skips_groups(ui):
    await press(ui,HOME); owner=active(ui)
    await press(ui,HOME,user=102); viewer=active(ui,102)
    other=Bot('654321:LOCAL_TEST'); other.session.make_request=AsyncMock(side_effect=ui.telegram.request)
    await press(ui,HOME,bot=other); other_menu=active(ui,bot=other)
    group_input=await press(ui,MENU_REPORTS,chat=-100)
    await press(ui,MENU_PRODUCTS)
    assert (101,owner) not in ui.telegram.messages
    assert (102,viewer) in ui.telegram.messages and (101,other_menu) in ui.telegram.messages
    assert (-100,group_input) in ui.telegram.messages
    assert not any(isinstance(m,DeleteMessage) and m.chat_id==-100 for m in ui.telegram.methods)
    unauthorized=await press(ui,HOME,user=999)
    assert (999,unauthorized) in ui.telegram.messages and active(ui,999) is None


@pytest.mark.asyncio
async def test_failed_replacement_keeps_previous_menu_and_user_tap(ui):
    await press(ui,HOME); previous=active(ui)
    ui.telegram.fail_next_send=True
    with pytest.raises(TelegramNetworkError):
        await press(ui,MENU_REPORTS)
    assert active(ui)==previous and (101,previous) in ui.telegram.messages
    assert (101,ui.update_id) in ui.telegram.messages


@pytest.mark.asyncio
async def test_failed_deletion_does_not_break_replacement_or_reports(ui):
    await press(ui,HOME); previous=active(ui)
    ui.telegram.fail_deletes.add(previous)
    await press(ui,MENU_REPORTS); current=active(ui)
    assert current!=previous and ui.telegram.keyboards[(101,current)] is not None
    await press(ui,'📅 Неделя')
    assert active(ui) is None and 'Неделя' in ui.telegram.messages[(101,ui.telegram.sequence)].text


@pytest.mark.asyncio
async def test_slow_report_does_not_delete_menu_opened_while_it_was_loading(ui,monkeypatch):
    await press(ui,HOME)
    entered=asyncio.Event(); finish=asyncio.Event()
    original=handlers.build_daily_report_with_currency
    async def delayed(*args,**kwargs):
        entered.set(); await finish.wait()
        return await original(*args,**kwargs)
    monkeypatch.setattr(handlers,'build_daily_report_with_currency',delayed)
    task=asyncio.create_task(press(ui,'📊 Вчера'))
    await asyncio.wait_for(entered.wait(),2)
    await press(ui,MENU_REPORTS); new_menu=active(ui)
    finish.set(); await task
    assert active(ui)==new_menu and (101,new_menu) in ui.telegram.messages


@pytest.mark.asyncio
async def test_old_inline_callback_does_not_delete_newer_menu(ui):
    await press(ui,'🗓 Другая дата'); old=active(ui)
    ui.telegram.fail_deletes.add(old)
    await press(ui,MENU_REPORTS); current=active(ui)
    await callback(ui,old,'report_date:1')
    assert active(ui)==current and (101,current) in ui.telegram.messages


@pytest.mark.asyncio
async def test_invalid_custom_date_keeps_prompt_and_allows_correction(ui):
    await press(ui,'🗓 Другая дата')
    await callback(ui,active(ui),'report_date:custom'); prompt=active(ui)
    invalid=await press(ui,'не дата')
    assert active(ui)==prompt and (101,invalid) in ui.telegram.messages
    await press(ui,'2026-09-28')
    assert active(ui) is None and (101,prompt) not in ui.telegram.messages


@pytest.mark.asyncio
async def test_shop_context_switch_replaces_menu_and_keeps_archive_result(ui):
    repo=ui.repo
    second=repo.ensure_shop(ui.shop.seller_id,'Second')
    repo.grant_shop_access(101,second.id,'owner'); repo.ensure_shop_preferences(second.id)
    contexts={ui.shop.id:ui.ctx,second.id:AppContext(ui.ctx.settings,repo,second.id,CollectionService(repo))}
    async def context_for_user(uid):
        return contexts[repo.selected_authorized_shop_for_user(uid,ui.shop.id)]
    registry=SimpleNamespace(repository=repo,settings=ui.ctx.settings,seller_id=ui.shop.seller_id,
        default_shop_id=ui.shop.id,maintenance_lock=asyncio.Lock(),context_for_user=context_for_user,
        select_shop=repo.select_authorized_shop_for_user,
        archive_shop=AsyncMock(side_effect=lambda sid,actor_id: repo.archive_shop(actor_id,sid)))
    dp=Dispatcher()
    middleware=ShopContextMiddleware(registry)
    dp.message.middleware(middleware); dp.callback_query.middleware(middleware)
    register_handlers(dp,ContextProxy(),registry)
    ui.dp=dp
    await press(ui,HOME); root=active(ui)
    await press(ui,COMMAND_BUTTONS['shops']); picker=active(ui)
    await callback(ui,picker,f'shop:select:{second.id}')
    assert (101,root) not in ui.telegram.messages and (101,picker) not in ui.telegram.messages
    await press(ui,MENU_REPORTS)
    await press(ui,COMMAND_BUTTONS['shop_archive']); picker=active(ui)
    await callback(ui,picker,f'shop:archive:{second.id}')
    await callback(ui,picker,f'shop:archive_confirm:{second.id}')
    assert active(ui) is None and 'перемещён в архив' in ui.telegram.messages[(101,picker)].text
    await press(ui,HOME)
    assert (101,picker) in ui.telegram.messages


@pytest.mark.asyncio
async def test_concurrent_menu_replacements_leave_only_latest_card(ui):
    await press(ui,HOME); original=active(ui)
    entered=asyncio.Event(); finish=asyncio.Event()
    request=ui.telegram.request
    async def delayed(bot,method,**kwargs):
        if isinstance(method,SendMessage) and '<b>Отчёты</b>' in method.text:
            entered.set(); await finish.wait()
        return await request(bot,method,**kwargs)
    ui.bot.session.make_request=AsyncMock(side_effect=delayed)
    first=asyncio.create_task(press(ui,MENU_REPORTS))
    await asyncio.wait_for(entered.wait(),2)
    second=asyncio.create_task(press(ui,MENU_PRODUCTS))
    finish.set(); await asyncio.gather(first,second)
    assert (101,original) not in ui.telegram.messages
    cards=[m for (chat,mid),m in ui.telegram.messages.items() if chat==101 and m.from_user.is_bot]
    assert len(cards)==1 and cards[0].message_id==active(ui)
    assert 'Товары' in cards[0].text


def test_schema16_upgrade_preserves_data_and_navigation_clear_checks_id(tmp_path):
    from app.storage.database import MIGRATIONS
    db=Database(tmp_path/'schema16.sqlite3')
    with db.connect() as connection:
        connection.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)')
        for version,migration in MIGRATIONS.items():
            if version>16: continue
            migration(connection)
            connection.execute("INSERT INTO schema_migrations VALUES(?,datetime('now'))",(version,))
    repo=Repository(db)
    seller=repo.ensure_seller(101,'Preserved'); shop=repo.ensure_shop(seller.id,'Preserved shop')
    assert db.initialize_safely(backups_dir=tmp_path/'backups')==LATEST_SCHEMA_VERSION
    assert repo.get_shop(shop.id).name=='Preserved shop'
    assert list((tmp_path/'backups').glob('pre_migration_v16_*.sqlite3'))
    repo.save_navigation_message(1,101,101,999)
    repo.clear_navigation_message(1,101,101,998)
    assert repo.navigation_message(1,101,101)==999
    repo.clear_navigation_message(1,101,101,999)
    assert repo.navigation_message(1,101,101) is None
