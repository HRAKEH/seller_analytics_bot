import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
import pytest

from app.bot.paged_reports import report_page_text
from app.reports.alerts import format_alert_digest, format_alert_digest_pages
from app.reports.text import utf16_length
from app.services.alerts import AlertNotification
from app.services import scheduler

from test_navigation import ui, callback, inline_data
from test_paged_reports import parsed


def notification(message, severity='warning'):
    return AlertNotification('rule', message, severity, message)


def test_digest_groups_changes_prioritizes_critical_and_escapes_names():
    notes = [notification('recovered', 'resolved'), notification('warning'),
             notification('critical <script>&', 'critical')]
    text = format_alert_digest(notes, shop_name='Shop <b>&')
    assert 'Критичных: 1 · предупреждений: 1 · снято предупреждений: 1' in text
    assert text.index('critical') < text.index('\n🟠 warning') < text.index('recovered')
    assert 'Shop &lt;b&gt;&amp;' in text
    assert 'critical &lt;script&gt;&amp;' in text


def test_large_digest_pages_keep_every_event_and_full_escaped_names():
    notes = [notification(f'{i} ' + '📦<&' * 300) for i in range(100)]
    notes.append(notification('critical must stay visible', 'critical'))
    pages = format_alert_digest_pages(notes, shop_name='😀' * 1000)
    assert len(pages) > 1 and 'critical must stay visible' in pages[0]
    bodies = []
    for index, page in enumerate(pages):
        displayed = report_page_text(pages, index)
        assert utf16_length(displayed) <= 3900
        parsed(displayed)
        assert page.count('Сводка оповещений') == 1
        assert 'Критичных: 1 · предупреждений: 100 · снято предупреждений: 0' in page
        assert 'Ещё событий:' not in page and 'Подробности сокращены.' not in page
        body = page.split('\n\n', 1)[1].split('\n\nТекущие проблемы:', 1)[0]
        bodies.append(''.join(parsed(body).text))
    combined = ''.join(bodies)
    assert all(note.message in combined for note in notes)
    assert combined.count('📦<&') == 30000
    assert 'Активные проблемы' in pages[-1]


def test_no_changes_produce_no_digest():
    assert format_alert_digest([], shop_name='Shop') is None
    assert format_alert_digest_pages([], shop_name='Shop') == []


def context(shop_id, notes, users):
    pref = SimpleNamespace(timezone='UTC', alerts_enabled=True, alerts_interval_minutes=30,
        alert_cooldown_minutes=1440, alert_order_drop_pct=30, alert_order_lookback_days=7,
        alert_api_stale_hours=24, alert_drr_pct=20, stock_risk_days=7, stock_velocity_days=7)
    states = {}
    repo = SimpleNamespace(notifications=notes, get_shop=lambda _: SimpleNamespace(name=f'Shop {shop_id}'),
        users_for_shop=lambda _: [{'telegram_user_id': uid} for uid in users],
        get_job_state=lambda shop, key: states.get((shop, key)),
        set_job_state=lambda shop, key, value: states.__setitem__((shop, key), value),
        acquire_lease=lambda *args: True, release_lease=lambda *args: True)
    return SimpleNamespace(shop_id=shop_id, repository=repo, preferences=lambda: pref,
        job_lock=SimpleNamespace(locked=lambda: True))


class Engine:
    def __init__(self, repo, **kwargs):
        self.repo = repo

    def evaluate(self, *args, **kwargs):
        return self.repo.notifications


@pytest.mark.asyncio
async def test_multi_shop_cycle_sends_one_digest_per_recipient_without_mixing_shops(monkeypatch):
    first = context(1, [notification('first alert'), notification('second alert')], [11, 12])
    second = context(2, [notification('other shop')], [21])
    empty = context(3, [], [31])
    registry = SimpleNamespace(contexts=lambda: [first, second, empty],
        maintenance_lock=SimpleNamespace(locked=lambda: False), settings=SimpleNamespace(instance_id='test'))
    bot = SimpleNamespace(id=123456, send_message=AsyncMock())

    async def stop(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(scheduler, 'AlertEngine', Engine)
    monkeypatch.setattr(scheduler.asyncio, 'sleep', stop)
    with pytest.raises(asyncio.CancelledError):
        await scheduler.multi_alerts_loop(bot, registry)
    calls = bot.send_message.await_args_list
    assert [call.args[0] for call in calls] == [11, 12, 21]
    assert all('first alert' in call.args[1] and 'second alert' in call.args[1] for call in calls[:2])
    assert 'first alert' not in calls[2].args[1] and 'other shop' in calls[2].args[1]


@pytest.mark.asyncio
async def test_single_shop_loop_uses_the_same_digest(monkeypatch):
    ctx = context(1, [notification('stock'), notification('recovered', 'resolved')], [11])
    bot = SimpleNamespace(id=123456, send_message=AsyncMock())
    calls = 0

    async def stop_after_cycle(_):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(scheduler, 'AlertEngine', Engine)
    monkeypatch.setattr(scheduler.asyncio, 'sleep', stop_after_cycle)
    with pytest.raises(asyncio.CancelledError):
        await scheduler.alerts_loop(bot, ctx)
    bot.send_message.assert_awaited_once()
    assert 'stock' in bot.send_message.await_args.args[1]
    assert 'recovered' in bot.send_message.await_args.args[1]


@pytest.mark.asyncio
async def test_many_stock_alerts_are_one_message_per_recipient_with_all_pages(ui, monkeypatch):
    notes = [AlertNotification('low_stock', f'ozon:{i}', 'critical',
             f'📦 Ozon · артикул {i}: остаток 0 шт. Товар: Крем <{i}> & склад') for i in range(150)]
    notes.append(AlertNotification('low_stock', 'wildberries:restored', 'resolved',
                                  '📦 WB · артикул restored: запас восстановлен'))
    await scheduler.send_alert_digest(ui.bot, ui.ctx, notes)
    deliveries = [call for call in ui.telegram.methods if isinstance(call, SendMessage)]
    assert [call.chat_id for call in deliveries] == [101, 102]
    mids = {}
    for recipient in (101, 102):
        message = next(message for (chat, _), message in ui.telegram.messages.items() if chat == recipient)
        mids[recipient] = message.message_id
        saved = ui.repo.paged_report(ui.bot.id, recipient, message.message_id)
        assert saved['user_id'] == recipient and saved['shop_id'] == ui.shop.id
        assert saved['permission'] == 'view'
        pages = json.loads(saved['pages_json'])
        assert len(pages) > 1
        assert [code for page in pages for code in parsed(page).codes] == [str(i) for i in range(150)] + ['restored']
        assert all('Критичных: 150' in page and 'снято предупреждений: 1' in page for page in pages)
        assert all(utf16_length(report_page_text(pages, index)) <= 3900 for index in range(len(pages)))
        assert not any(button.copy_text for row in message.reply_markup.inline_keyboard for button in row)

    ui.ctx.collect_inventory = AsyncMock(side_effect=AssertionError('Paging must not refresh inventory'))
    monkeypatch.setattr(scheduler, 'AlertEngine', lambda *a, **kw: pytest.fail('Paging must use saved events'))
    mid = mids[101]
    first = ui.telegram.messages[(101, mid)].text
    await callback(ui, mid, inline_data(ui, mid, '▶️'))
    second = ui.telegram.messages[(101, mid)].text
    assert second != first and second.count('Сводка оповещений') == 1 and 'Страница 2/' in second
    assert ui.repo.paged_report(ui.bot.id, 102, mids[102])['current_page'] == 0
    await callback(ui, mid, inline_data(ui, mid, '◀️'))
    assert ui.telegram.messages[(101, mid)].text == first
    assert len([call for call in ui.telegram.methods if isinstance(call, SendMessage)]) == 2
    ui.ctx.collect_inventory.assert_not_called()


@pytest.mark.asyncio
async def test_auto_digest_keeps_source_shop_dates_and_access_after_restart(ui):
    notes = [AlertNotification('low_stock', f'ozon:2026-10-{i:02d}', 'warning',
             f'📦 Ozon · артикул 2026-10-{i:02d}: остаток 1 шт. Снимок: 2026-10-04T06:09:00Z') for i in range(1, 30)]
    await scheduler.send_alert_digest(ui.bot, ui.ctx, notes)
    message = next(message for (chat, _), message in ui.telegram.messages.items() if chat == 101)
    mid = message.message_id
    saved = ui.repo.paged_report(ui.bot.id, 101, mid)
    pages = json.loads(saved['pages_json'])
    assert all('04.10.2026 09:09' in page for page in pages)
    assert [code for page in pages for code in parsed(page).codes] == [f'2026-10-{i:02d}' for i in range(1, 30)]
    second = ui.repo.ensure_shop(ui.shop.seller_id, 'Second')
    ui.repo.grant_shop_access(101, second.id, 'owner')
    ui.repo.ensure_shop_preferences(second.id)
    ui.repo.update_shop_preferences(second.id, timezone='Asia/Vladivostok')
    ui.ctx.shop_id = second.id
    ui.dp = ui.new_dispatcher()
    await callback(ui, mid, 'report_page:1')
    text = ui.telegram.messages[(101, mid)].text
    assert '🏪 <b>Shop</b>' in text and '04.10.2026 09:09' in text
    assert 'Second' not in text and '04.10.2026 16:09' not in text
    assert ui.repo.paged_report(ui.bot.id, 101, mid)['current_page'] == 1
    edits = len([call for call in ui.telegram.methods if isinstance(call, EditMessageText)])
    ui.repo.revoke_shop_access(101, ui.shop.id)
    await callback(ui, mid, 'report_page:0')
    assert len([call for call in ui.telegram.methods if isinstance(call, EditMessageText)]) == edits
    assert 'Нет доступа' in ui.telegram.methods[-1].text


@pytest.mark.asyncio
async def test_auto_digest_pages_are_bound_to_the_actual_recipient(ui):
    await scheduler.send_alert_digest(ui.bot, ui.ctx, [notification(f'Event {i}') for i in range(120)])
    message = next(message for (chat, _), message in ui.telegram.messages.items() if chat == 101)
    ui.telegram.messages[(102, message.message_id)] = message
    await callback(ui, message.message_id, 'report_page:1', user=102)
    assert isinstance(ui.telegram.methods[-1], AnswerCallbackQuery)
    assert 'Нет доступа' in ui.telegram.methods[-1].text
    assert not any(isinstance(call, EditMessageText) for call in ui.telegram.methods)
    assert ui.repo.paged_report(ui.bot.id, 101, message.message_id)['current_page'] == 0


@pytest.mark.asyncio
async def test_failed_recipient_does_not_block_the_next_auto_digest(ui):
    ui.telegram.fail_next_send = True
    await scheduler.send_alert_digest(ui.bot, ui.ctx, [notification(f'Event {i}') for i in range(120)])
    sends = [call for call in ui.telegram.methods if isinstance(call, SendMessage)]
    assert [call.chat_id for call in sends] == [101, 102]
    assert all(chat == 102 for chat, _ in ui.telegram.messages)
    message = next(iter(ui.telegram.messages.values()))
    assert ui.repo.paged_report(ui.bot.id, 102, message.message_id)['user_id'] == 102
    await callback(ui, message.message_id, 'report_page:1', user=102)
    assert 'Страница 2/' in ui.telegram.messages[(102, message.message_id)].text


@pytest.mark.asyncio
async def test_short_auto_digest_has_no_pages_and_empty_digest_is_not_sent(ui):
    await scheduler.send_alert_digest(ui.bot, ui.ctx, [])
    assert not ui.telegram.methods
    await scheduler.send_alert_digest(ui.bot, ui.ctx, [notification('stock')])
    sends = [call for call in ui.telegram.methods if isinstance(call, SendMessage)]
    assert [call.chat_id for call in sends] == [101, 102]
    assert all(call.reply_markup is None and 'Страница' not in call.text for call in sends)
    with ui.repo.db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM telegram_paged_reports').fetchone()[0] == 0
