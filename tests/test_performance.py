import asyncio
import sqlite3
import threading
import time
import pytest

from app.services.performance import run_heavy_io, snapshot, format_performance, timed_api
from app.storage import Database


def test_database_session_commits_rolls_back_and_closes(tmp_path):
    db = Database(tmp_path / 'db.sqlite')
    db.initialize()
    with db.session() as c:
        c.execute('CREATE TABLE sample (value INTEGER)')
        c.execute('INSERT INTO sample VALUES (7)')
    with pytest.raises(sqlite3.ProgrammingError):
        c.execute('SELECT 1')
    with pytest.raises(ValueError):
        with db.session() as c:
            c.execute('INSERT INTO sample VALUES (8)')
            raise ValueError('rollback')
    with db.session() as c:
        assert [row[0] for row in c.execute('SELECT value FROM sample')] == [7]
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='idx_inventory_scheme_history'").fetchone()


@pytest.mark.asyncio
async def test_heavy_work_does_not_block_loop_or_overlap(tmp_path):
    lock = threading.Lock()
    state = {'active': 0, 'peak': 0}
    def work():
        with lock:
            state['active'] += 1
            state['peak'] = max(state['peak'], state['active'])
        time.sleep(.05)
        with lock:
            state['active'] -= 1
        return 1
    tasks = [asyncio.create_task(run_heavy_io(work)) for _ in range(3)]
    await asyncio.sleep(.01)
    assert not all(t.done() for t in tasks)
    assert await asyncio.gather(*tasks) == [1, 1, 1]
    assert state['peak'] == 1
    data = snapshot(tmp_path / 'no-db')
    assert data['heavy_active'] == data['heavy_waiting'] == 0
    assert data['timings']['heavy:work']['count'] >= 3
    assert 'RAM Python' in format_performance(data)


@pytest.mark.asyncio
async def test_cancelled_work_finishes_before_next_job():
    started = threading.Event()
    finished = threading.Event()
    def first():
        started.set()
        time.sleep(.05)
        finished.set()
    task = asyncio.create_task(run_heavy_io(first))
    while not started.is_set():
        await asyncio.sleep(.001)
    task.cancel()
    other = asyncio.create_task(run_heavy_io(finished.is_set))
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await other is True
