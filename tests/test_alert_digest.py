import asyncio
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.reports.alerts import format_alert_digest
from app.services.alerts import AlertNotification
from app.services import scheduler


def notification(message, severity='warning'):
    return AlertNotification('rule', message, severity, message)


def test_digest_groups_changes_prioritizes_critical_and_escapes_names():
    notes = [notification('recovered', 'resolved'), notification('warning'),
             notification('critical <script>&', 'critical')]
    text = format_alert_digest(notes, shop_name='Shop <b>&')
    assert 'Критичных: 1 · предупреждений: 1 · восстановлено: 1' in text
    assert text.index('critical') < text.index('\n🟠 warning') < text.index('recovered')
    assert 'Shop &lt;b&gt;&amp;' in text
    assert 'critical &lt;script&gt;&amp;' in text


def test_large_digest_stays_in_one_message_and_announces_omitted_changes():
    notes = [notification(f'{i} ' + '📦<&' * 300) for i in range(100)]
    notes.append(notification('critical must stay visible', 'critical'))
    text = format_alert_digest(notes, shop_name='😀' * 1000)
    assert len(text.encode('utf-16-le')) // 2 <= 3900
    assert 'critical must stay visible' in text
    assert 'Ещё событий:' in text and 'Подробности сокращены.' in text
    assert 'предупреждений: 100' in text

    class Tags(HTMLParser):
        def __init__(self):
            super().__init__()
            self.starts, self.ends = [], []

        def handle_starttag(self, tag, attrs):
            self.starts.append(tag)

        def handle_endtag(self, tag):
            self.ends.append(tag)

    parser = Tags()
    parser.feed(text)
    assert parser.starts == parser.ends == ['b', 'b']


def test_no_changes_produce_no_digest():
    assert format_alert_digest([], shop_name='Shop') is None


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
    bot = SimpleNamespace(send_message=AsyncMock())

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
    bot = SimpleNamespace(send_message=AsyncMock())
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
async def test_many_stock_alerts_and_recoveries_are_one_bounded_delivery():
    notes=[AlertNotification('low_stock',f'ozon:{i}','critical',
           f'📦 Товар {i} <крем>: остаток 0, доступно 0 шт.') for i in range(150)]
    notes.append(AlertNotification('low_stock','wb:restored','resolved','📦 Запас восстановлен'))
    ctx=context(1,notes,[11])
    bot=SimpleNamespace(send_message=AsyncMock())
    await scheduler.send_alert_digest(bot,ctx,notes)
    bot.send_message.assert_awaited_once()
    text=bot.send_message.await_args.args[1]
    assert 'Критичных: 150' in text and 'восстановлено: 1' in text
    assert 'Ещё событий:' in text and 'Активные проблемы' in text
    assert len(text.encode('utf-16-le'))//2<=3900
