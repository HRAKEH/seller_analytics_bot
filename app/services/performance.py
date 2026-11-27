"""Bounded, secret-free process diagnostics and cancellation-safe heavy I/O."""
from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from functools import wraps
from pathlib import Path
from threading import Lock
from time import monotonic, process_time

_lock = Lock()
_samples = defaultdict(lambda: deque(maxlen=64))
_started = monotonic()
_cpu_started = process_time()
_cpu_previous = None
_active = 0
_waiting = 0


def observe(name, seconds, status=None):
    # Names are fixed internal operation labels, never URLs, SKUs or credentials.
    with _lock:
        _samples[name].append((max(0.0, seconds), status))


def timed_async(name):
    def decorate(fn):
        @wraps(fn)
        async def wrapped(*args, **kwargs):
            start = monotonic()
            try:
                return await fn(*args, **kwargs)
            finally:
                observe(name, monotonic() - start)
        return wrapped
    return decorate


def timed_api(fn):
    @wraps(fn)
    async def wrapped(self, *args, **kwargs):
        start = monotonic()
        status = None
        try:
            result = await fn(self, *args, **kwargs)
            status = result.status_code
            return result
        finally:
            elapsed=monotonic() - start
            observe('api:' + self.source, elapsed, status)
            # rate_key is an internal method group, never a URL or a credential.
            key=str(kwargs.get('rate_key','default'))
            if key.startswith('ping:'):
                key='ping'
            if key.replace('_','').replace('-','').isalnum() and len(key)<=48:
                observe('api_method:'+self.source+':'+key,elapsed,status)
    return wrapped


async def run_heavy_io(fn, *args, **kwargs):
    """One heavy threaded job at a time per event loop; no orphaned file writer."""
    global _active, _waiting
    loop = asyncio.get_running_loop()
    gate = getattr(loop, '_sellerbot_heavy_io_gate', None)
    if gate is None:
        gate = asyncio.Lock()
        setattr(loop, '_sellerbot_heavy_io_gate', gate)
    with _lock:
        _waiting += 1
    try:
        await gate.acquire()
    finally:
        with _lock:
            _waiting -= 1
    with _lock:
        _active += 1
    start = monotonic()
    try:
        task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # The thread cannot be cancelled. Keep the slot and temporary
            # directory alive until disk/database work has actually finished.
            try:
                await task
            except Exception:
                pass
            raise
    finally:
        observe('heavy:' + getattr(fn, '__name__', 'work'), monotonic() - start)
        with _lock:
            _active -= 1
        gate.release()


def _read_int(path):
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def _rss_bytes():
    try:
        for line in Path('/proc/self/status').read_text().splitlines():
            if line.startswith('VmRSS:'):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _cgroup_memory():
    current = _read_int('/sys/fs/cgroup/memory.current')
    stat_path = '/sys/fs/cgroup/memory.stat'
    if current is None:
        current = _read_int('/sys/fs/cgroup/memory/memory.usage_in_bytes')
        stat_path = '/sys/fs/cgroup/memory/memory.stat'
    cache = None
    try:
        values = dict(line.split() for line in Path(stat_path).read_text().splitlines())
        raw = values.get('file', values.get('total_cache', values.get('cache')))
        if raw is not None:
            cache = int(raw)
    except (OSError, ValueError):
        pass
    return current, cache


def snapshot(db_path):
    global _cpu_previous
    now, cpu = monotonic(), process_time()
    with _lock:
        previous = _cpu_previous or (_started, _cpu_started)
        elapsed = now - previous[0]
        # 100% means one fully utilized logical CPU, not the host's CPU quota.
        utilization = (cpu - previous[1]) / elapsed * 100 if elapsed > 0 else None
        if elapsed >= 1:
            _cpu_previous = (now, cpu)
        samples = {key: list(values) for key, values in _samples.items()}
        active, waiting = _active, _waiting
    path = Path(db_path)
    def size(file):
        try:
            return file.stat().st_size
        except OSError:
            return None
    current, cache = _cgroup_memory()
    return {
        'rss_bytes': _rss_bytes(), 'container_bytes': current, 'file_cache_bytes': cache,
        'cpu_pct': utilization, 'cpu_sample_seconds': elapsed,
        'db_bytes': size(path), 'wal_bytes': size(Path(str(path) + '-wal')),
        'heavy_active': active, 'heavy_waiting': waiting,
        'timings': {key: {'count': len(values),
                          'avg_seconds': sum(v[0] for v in values) / len(values),
                          'max_seconds': max(v[0] for v in values),
                          'rate_limited': sum(v[1] == 429 for v in values)}
                    for key, values in sorted(samples.items()) if values},
    }


def format_performance(data):
    def mb(value):
        return '—' if value is None else f'{value / 1048576:.1f} МиБ'
    cpu = data['cpu_pct']
    lines = ['<b>Производительность</b>',
             'RAM Python (RSS): ' + mb(data['rss_bytes']),
             'Память cgroup: ' + mb(data['container_bytes']),
             'Файловый кеш cgroup: ' + mb(data['file_cache_bytes']),
             'CPU процесса: ' + ('—' if cpu is None else f'{cpu:.1f}% за {data["cpu_sample_seconds"]:.0f} сек. (100% = одно ядро)'),
             'SQLite: ' + mb(data['db_bytes']) + ' · WAL: ' + mb(data['wal_bytes']),
             f'Тяжёлые операции: {data["heavy_active"]} · в очереди: {data["heavy_waiting"]}']
    for key, item in data['timings'].items():
        if key.startswith(('api:','api_method:','api_network:')) or key == 'report_refresh' or key == 'telegram_update' or key.startswith('heavy:'):
            suffix = f' · итоговых HTTP 429: {item["rate_limited"]}' if key.startswith(('api:','api_method:','api_network:')) else ''
            lines.append(f'{key}: среднее {item["avg_seconds"]:.2f} с · макс. {item["max_seconds"]:.2f} с · n={item["count"]}' + suffix)
    return '\n'.join(lines)
