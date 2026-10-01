from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import logging
from unittest.mock import AsyncMock

from aiogram import Bot, Dispatcher, types
import pytest

from app.bot import AppContext, register_handlers
from app.bot.updates import TelegramUpdateMiddleware
from app.config import Settings
from app.services.collection import CollectionService
from app.storage import Database, Repository


def message_update(update_id, message_id=1, *, chat_id=101, text='/start'):
    return types.Update(
        update_id=update_id,
        message=types.Message(
            message_id=message_id,
            date=datetime(2026, 10, 1, tzinfo=timezone.utc),
            chat=types.Chat(id=chat_id, type='private'),
            from_user=types.User(id=chat_id, is_bot=False, first_name='PRIVATE_NAME'),
            text=text,
            entities=[types.MessageEntity(type='bot_command', offset=0, length=6)],
        ),
    )


@pytest.fixture
def dispatcher(tmp_path):
    settings=replace(Settings.from_env(), telegram_token='123456:LOCAL_TEST', owner_ids=(101,))
    db=Database(tmp_path/'telegram.sqlite3')
    db.initialize()
    repo=Repository(db)
    seller=repo.ensure_seller(101, 'Seller')
    shop=repo.ensure_shop(seller.id, 'Shop')
    repo.grant_shop_access(101, shop.id, 'owner')
    repo.grant_shop_access(102, shop.id, 'viewer')
    repo.ensure_shop_preferences(shop.id)
    ctx=AppContext(settings, repo, shop.id, CollectionService(repo))
    bot=Bot(settings.telegram_token)
    calls=[]

    async def record(bot, method, **kwargs):
        calls.append(method)
        return types.Message(
            message_id=100+len(calls), date=datetime.now(timezone.utc),
            chat=types.Chat(id=101, type='private'), text=method.text,
        )

    bot.session.make_request=AsyncMock(side_effect=record)
    dp=Dispatcher()
    dp.update.outer_middleware(TelegramUpdateMiddleware('test-instance'))
    register_handlers(dp, ctx)
    return dp, bot, calls


@pytest.mark.asyncio
async def test_real_start_handler_replies_once_and_ignores_message_replay(dispatcher):
    dp, bot, calls=dispatcher
    await dp.feed_update(bot, message_update(585))
    assert len(calls)==1
    assert 'Seller Analytics' in calls[0].text
    await dp.feed_update(bot, message_update(585))
    await dp.feed_update(bot, message_update(586))
    assert len(calls)==1


@pytest.mark.asyncio
async def test_identical_new_commands_and_other_chats_still_receive_replies(dispatcher):
    dp, bot, calls=dispatcher
    await dp.feed_update(bot, message_update(585, 1))
    await dp.feed_update(bot, message_update(586, 2))
    await dp.feed_update(bot, message_update(587, 1, chat_id=102))
    assert len(calls)==3


@pytest.mark.asyncio
async def test_concurrent_redelivery_does_not_enter_handler_twice():
    middleware=TelegramUpdateMiddleware('instance')
    entered=asyncio.Event()
    finish=asyncio.Event()
    calls=[]

    async def handler(event, data):
        calls.append(event.update_id)
        entered.set()
        await finish.wait()

    task=asyncio.create_task(middleware(handler, message_update(585), {}))
    await entered.wait()
    await middleware(handler, message_update(586), {})
    finish.set()
    await task
    assert calls==[585]


@pytest.mark.asyncio
async def test_different_callbacks_on_same_menu_are_not_collapsed():
    dp=Dispatcher()
    dp.update.outer_middleware(TelegramUpdateMiddleware('instance'))
    bot=Bot('123456:LOCAL_TEST')
    calls=[]

    @dp.callback_query()
    async def handler(callback):
        calls.append(callback.id)

    def update(uid, callback_id):
        return types.Update(update_id=uid, callback_query=types.CallbackQuery(
            id=callback_id, from_user=types.User(id=101, is_bot=False, first_name='User'),
            chat_instance='chat-instance', message=message_update(1).message,
            data='PRIVATE_CALLBACK_PAYLOAD',
        ))

    await dp.feed_update(bot, update(1, 'click-1'))
    await dp.feed_update(bot, update(2, 'click-1'))
    await dp.feed_update(bot, update(3, 'click-2'))
    assert calls==['click-1', 'click-2']


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [RuntimeError, asyncio.CancelledError])
async def test_failed_or_cancelled_delivery_can_be_retried(error):
    middleware=TelegramUpdateMiddleware('instance')
    handler=AsyncMock(side_effect=[error(), 'ok'])
    with pytest.raises(error):
        await middleware(handler, message_update(1), {})
    assert await middleware(handler, message_update(1), {})=='ok'
    assert handler.await_count==2


@pytest.mark.asyncio
async def test_edits_and_cache_expiry_do_not_drop_new_events(monkeypatch):
    import app.bot.updates as updates
    clock=[0.0]
    monkeypatch.setattr(updates, 'monotonic', lambda: clock[0])
    middleware=TelegramUpdateMiddleware('instance', ttl=10, max_entries=2)
    handler=AsyncMock()
    await middleware(handler, message_update(1), {})
    edit=types.Update(update_id=2, edited_message=message_update(1).message)
    await middleware(handler, edit, {})
    await middleware(handler, edit, {})
    assert handler.await_count==2
    clock[0]=10.0
    await middleware(handler, message_update(1), {})
    assert handler.await_count==3


@pytest.mark.asyncio
async def test_trace_logs_identities_without_text_names_or_callback_payload(caplog, dispatcher):
    caplog.set_level(logging.INFO, logger='app.bot.updates')
    dp, bot, _=dispatcher
    await dp.feed_update(bot, message_update(585, text='/start PRIVATE_SECRET_TOKEN'))
    await dp.feed_update(bot, message_update(586, text='/start PRIVATE_SECRET_TOKEN'))
    await dp.feed_update(bot, types.Update(update_id=587, callback_query=types.CallbackQuery(
        id='private-click', from_user=types.User(id=101, is_bot=False, first_name='PRIVATE_NAME'),
        chat_instance='chat-instance', message=message_update(1).message,
        data='PRIVATE_CALLBACK_PAYLOAD',
    )))
    text=caplog.text
    assert 'update=585' in text and 'message=1' in text and 'chat=101' in text
    assert 'instance=test-instance' in text and 'Duplicate Telegram delivery ignored' in text
    assert 'PRIVATE_NAME' not in text and 'PRIVATE_SECRET_TOKEN' not in text
    assert 'PRIVATE_CALLBACK_PAYLOAD' not in text
