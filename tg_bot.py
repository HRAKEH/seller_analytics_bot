# -*- coding: utf-8 -*-
"""
Telegram-бот отчётности по продажам: Ozon + Wildberries.  (v2, исправленные формулы)

ЧТО СЧИТАЕТСЯ (одинаково для обоих маркетплейсов, сутки по Москве):
  • ЗАКАЗЫ — главная метрика, именно она суммируется в «ИТОГО»:
      Ozon : analytics ordered_units / revenue  + число отправлений (FBS+FBO)
      WB   : /supplier/orders — строки заказов (1 строка = 1 шт), сумма = priceWithDisc,
             «заказов» = уникальные корзины (gNumber)
  • Справочно (в ИТОГО НЕ входят, т.к. это другая метрика):
      WB   : выкупы = продажи (saleID S…) минус возвраты (R…), отмены = isCancel

Отчёт за день сохраняется по источникам независимо: сбой одного API никогда не
затирает уже загруженные данные другого (и свои старые данные тоже).
"""

import os
import asyncio
import functools
import logging
import random
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject
from aiogram.utils.keyboard import ReplyKeyboardBuilder

# ─── КОНФИГ ────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _env_int(name, default, lo=None, hi=None):
    try:
        v = int(os.getenv(name, str(default)))
    except ValueError:
        v = default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
DB_FILE = Path(os.getenv("DB_FILE", "/app/data/daily_reports.sqlite3"))
OWNER_IDS = [int(x.strip()) for x in os.getenv("TELEGRAM_OWNER_ID", "").split(",")
             if x.strip().isdigit()]

OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID", "")
OZON_API_KEY = os.getenv("OZON_API_KEY", "")
WB_API_TOKEN = os.getenv("WB_API_TOKEN", "")

REPORT_HOUR = _env_int("REPORT_HOUR", 9, 0, 23)
REPORT_MINUTE = _env_int("REPORT_MINUTE", 0, 0, 59)
DAILY_RETRIES = _env_int("DAILY_RETRIES", 3, 1, 10)            # попыток авто-отчёта
DAILY_RETRY_DELAY_MIN = _env_int("DAILY_RETRY_DELAY_MIN", 10, 1, 120)

# Интервалы между запросами (лимиты API): Ozon analytics и WB statistics — 1 запрос/мин.
WB_MIN_INTERVAL = _env_int("WB_MIN_INTERVAL", 65, 1)
OZON_ANALYTICS_INTERVAL = _env_int("OZON_MIN_INTERVAL", 65, 1)
OZON_POSTING_INTERVAL = 0.5          # списки отправлений — лимит высокий, 65 с не нужны
WB_ROW_THRESHOLD = 79000             # WB отдаёт до ~80 000 строк за ответ
BACKFILL_DAYS = _env_int("BACKFILL_DAYS", 30, 1, 90)
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "180"))
CACHE_HOURS = _env_int("CACHE_HOURS", 6, 1)
MAX_NET_RETRIES = _env_int("MAX_NET_RETRIES", 5, 1)
MAX_429_RETRIES = _env_int("MAX_429_RETRIES", 8, 1)   # раньше был бесконечный цикл
MAX_WB_CHUNKS = _env_int("MAX_WB_CHUNKS", 100, 1, 1000)

MSK = timezone(timedelta(hours=3))
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

bot = Bot(token=TOKEN) if TOKEN else None
dp = Dispatcher()
_job_lock = asyncio.Lock()          # одна выгрузка за раз


# ─── ВРЕМЯ И ФОРМАТЫ ───────────────────────────────────────────────────────

def msk_now():
    return datetime.now(MSK)


def msk_today():
    return msk_now().date()


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(s):
    """ISO-строка → aware datetime (наивные считаем UTC)."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_fresh(ts):
    dt = parse_ts(ts)
    return bool(dt) and (datetime.now(timezone.utc) - dt) < timedelta(hours=CACHE_HOURS)


def fmt_date(d, weekday=True):
    d = date.fromisoformat(str(d)[:10])
    s = d.strftime("%d.%m.%Y")
    return f"{WEEKDAYS[d.weekday()]} {s}" if weekday else s


def fmt_int(n):
    n = n or 0
    n = int((abs(n) + 0.5) // 1) * (1 if n >= 0 else -1)      # half-up, а не банковское округление
    return f"{n:,}".replace(",", "\u00a0")


def fmt_money(x):
    return fmt_int(x) + "\u00a0₽"


def fmt_orders(v):
    return "—" if v is None else fmt_int(v)


def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def days_between(start, end):
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def msk_day_bounds_utc(d):
    """Границы московских суток d в UTC (для фильтров Ozon)."""
    s = datetime(d.year, d.month, d.day, tzinfo=MSK).astimezone(timezone.utc)
    e = s + timedelta(days=1) - timedelta(seconds=1)
    f = "%Y-%m-%dT%H:%M:%S.000Z"
    return s.strftime(f), e.strftime(f)


def iso_to_msk_date(s):
    """'2026-09-27T21:30:00Z' → '2026-09-28' (московская дата)."""
    if not s:
        return None
    s = str(s)
    try:
        dt = datetime.fromisoformat(s[:19])
    except ValueError:
        return None
    if "+03:00" not in s[19:]:            # Ozon отдаёт UTC ('Z')
        dt += timedelta(hours=3)
    return dt.date().isoformat()


# ─── БАЗА ДАННЫХ ───────────────────────────────────────────────────────────
# Новая таблица daily_reports_v2: в старой WB-колонки хранили ВЫКУПЫ, а здесь — ЗАКАЗЫ,
# смешивать их нельзя. Старая таблица не трогается.

OZON_FIELDS = ("units", "amount", "orders")
WB_FIELDS = ("units", "amount", "orders", "cancel_units", "buyout_units", "buyout_amount")
_REAL = {"amount", "buyout_amount"}

COLUMNS = (["report_date"]
           + [f"ozon_{f}" for f in OZON_FIELDS] + ["ozon_received_at", "ozon_error"]
           + [f"wb_{f}" for f in WB_FIELDS] + ["wb_received_at", "wb_error", "updated_at"])


def _connect():
    conn = sqlite3.connect(str(DB_FILE), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    defs = []
    for c in COLUMNS:
        if c == "report_date":
            defs.append("report_date TEXT PRIMARY KEY")
        elif c.endswith(("_at", "_error")):
            defs.append(f"{c} TEXT")
        else:
            defs.append(f"{c} {'REAL' if c.split('_', 1)[1] in _REAL else 'INTEGER'}")
    conn = _connect()
    conn.execute(f"CREATE TABLE IF NOT EXISTS daily_reports_v2 ({', '.join(defs)})")
    conn.commit()
    conn.close()
    logger.info("📦 База готова: %s", DB_FILE)


def get_report(ds):
    conn = _connect()
    row = conn.execute("SELECT * FROM daily_reports_v2 WHERE report_date = ?",
                       (str(ds),)).fetchone()
    conn.close()
    return dict(row) if row else None


def save_source(ds, src, data, error=None):
    """
    Записывает результат ОДНОГО источника ('ozon' | 'wb') для дня ds.
    data — успех: поля источника + received_at, ошибка сбрасывается.
    data=None — сбой: старые данные остаются, фиксируется только ошибка.
    """
    ds = str(ds)
    row = get_report(ds) or {"report_date": ds}
    if data is not None:
        for f in (OZON_FIELDS if src == "ozon" else WB_FIELDS):
            row[f"{src}_{f}"] = data.get(f)
        row[f"{src}_received_at"] = data["received_at"]
        row[f"{src}_error"] = None
    else:
        row[f"{src}_error"] = error or "нет данных"
    row["updated_at"] = utc_now_iso()
    conn = _connect()
    conn.execute(
        f"INSERT OR REPLACE INTO daily_reports_v2 ({', '.join(COLUMNS)}) "
        f"VALUES ({', '.join('?' * len(COLUMNS))})",
        [row.get(c) for c in COLUMNS])
    conn.commit()
    conn.close()


def src_ok(row, src):
    return bool(row and row.get(f"{src}_received_at") and not row.get(f"{src}_error"))


def is_complete(row):
    return src_ok(row, "ozon") and src_ok(row, "wb")


def src_fresh(row, src):
    return src_ok(row, src) and is_fresh(row.get(f"{src}_received_at"))


# ─── HTTP: ЛИМИТЫ И ПОВТОРЫ ────────────────────────────────────────────────

class RateLimiter:
    """Сериализует запросы и выдерживает минимальный интервал между ними."""

    def __init__(self, interval):
        self.interval = float(interval)
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def __aenter__(self):
        await self._lock.acquire()
        try:
            wait = self._next - asyncio.get_running_loop().time()
            if wait > 0:
                logger.info("⌛ лимит: ждём %.1f с", wait)
                await asyncio.sleep(wait)
        except BaseException:
            self._lock.release()
            raise

    async def __aexit__(self, *exc):
        now = asyncio.get_running_loop().time()
        self._next = max(self._next, now + self.interval)
        self._lock.release()

    def push(self, seconds):
        self._next = max(self._next, asyncio.get_running_loop().time() + seconds)


_wb_limiters = {"orders": RateLimiter(WB_MIN_INTERVAL), "sales": RateLimiter(WB_MIN_INTERVAL)}
_ozon_analytics_limiter = RateLimiter(OZON_ANALYTICS_INTERVAL)
_ozon_posting_limiter = RateLimiter(OZON_POSTING_INTERVAL)


async def api_call(client, limiter, method, url, *, headers, label,
                   params=None, json_body=None):
    """→ (json | None, error | None). 429 и сбои сети/5xx повторяются ограниченное число раз."""
    net_retries = throttled = 0
    while True:
        push = 0.0
        async with limiter:
            try:
                resp = await client.request(method, url, headers=headers,
                                            params=params, json=json_body)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
                if net_retries >= MAX_NET_RETRIES:
                    return None, f"{label}: сеть ({type(e).__name__})"
                net_retries += 1
                push = min(300.0, max(5.0, limiter.interval) * net_retries) + random.uniform(0, 2)
            else:
                sc = resp.status_code
                if sc == 200:
                    try:
                        return resp.json(), None
                    except ValueError:
                        return None, f"{label}: некорректный JSON"
                if sc == 429:
                    throttled += 1
                    if throttled > MAX_429_RETRIES:
                        return None, f"{label}: 429, лимит запросов не снялся"
                    raw = resp.headers.get("X-Ratelimit-Retry") or resp.headers.get("Retry-After")
                    try:
                        push = max(1.0, float(raw))
                    except (TypeError, ValueError):
                        push = max(1.0, limiter.interval)
                    logger.warning("⚠️ %s: 429, повтор через %.0f с", label, push)
                elif 500 <= sc < 600:
                    if net_retries >= MAX_NET_RETRIES:
                        logger.error("%s %s: %s", label, sc, resp.text[:300])
                        return None, f"{label}: {sc}"
                    net_retries += 1
                    push = min(120.0, 5.0 * 2 ** net_retries) + random.uniform(0, 1)
                elif sc in (401, 403):
                    logger.error("%s %s: %s", label, sc, resp.text[:300])
                    return None, f"{label}: {sc} нет доступа (проверь токен и права)"
                else:
                    logger.error("%s %s: %s", label, sc, resp.text[:300])
                    return None, f"{label}: {sc}"
            limiter.push(push)     # следующий заход подождёт в __aenter__


# ─── WILDBERRIES ───────────────────────────────────────────────────────────

WB_URL = "https://statistics-api.wildberries.ru/api/v1/supplier/"


def _wb_headers():
    return {"Authorization": WB_API_TOKEN} if WB_API_TOKEN else {}


def _wb_price(row):
    """
    Цена позиции для продавца: priceWithDisc (= totalPrice × (1 − discountPercent/100)).
    Если поле ещё не заполнено (бывает первые сутки) — считаем по той же формуле.
    НЕ подмешиваем forPay/finishedPrice/сырой totalPrice: это другие величины.
    """
    p = abs(_num(row.get("priceWithDisc")))      # у возвратов значения могут быть отрицательными
    if p > 0:
        return p
    total = abs(_num(row.get("totalPrice")))
    return total * (1 - abs(_num(row.get("discountPercent"))) / 100) if total > 0 else 0.0


def parse_wb_orders(rows):
    """Заказы WB: units, amount, orders (корзины), cancel_units. Дубли по srid отбрасываются."""
    seen, baskets = set(), set()
    units = cancel = 0
    amount = 0.0
    for r in rows or []:
        srid = str(r.get("srid") or "")
        if srid:
            if srid in seen:
                continue
            seen.add(srid)
        units += 1
        amount += _wb_price(r)
        baskets.add(str(r.get("gNumber") or srid or f"row{units}"))
        if r.get("isCancel"):
            cancel += 1
    return {"units": units, "amount": amount, "orders": len(baskets), "cancel_units": cancel}


def parse_wb_sales(rows):
    """Выкупы WB: продажи (S…) минус возвраты (R…). Знак задаёт saleID, сумма берётся по модулю."""
    seen = set()
    units = 0
    amount = 0.0
    for r in rows or []:
        sid = str(r.get("saleID") or "").upper()
        if sid:
            if sid in seen:
                continue
            seen.add(sid)
        if sid.startswith("S"):
            units += 1
            amount += abs(_wb_price(r))
        elif sid.startswith("R"):
            units -= 1
            amount -= abs(_wb_price(r))
    return {"buyout_units": units, "buyout_amount": amount}


async def _wb_pull(client, endpoint, start, end, progress=None):
    """Выгрузка одного метода WB. Один день → flag=1; период → flag=0 с пагинацией lastChangeDate."""
    single = start == end
    flag = 1 if single else 0
    current, rows, chunk = start.isoformat(), [], 0
    while chunk < MAX_WB_CHUNKS:
        chunk += 1
        data, err = await api_call(
            client, _wb_limiters[endpoint], "GET", WB_URL + endpoint, headers=_wb_headers(),
            params={"dateFrom": current, "flag": flag}, label=f"WB {endpoint} #{chunk}")
        if err:
            return rows, err, chunk
        if not isinstance(data, list):
            return rows, f"WB {endpoint}: недопустимый формат ответа", chunk
        rows.extend(data)
        if progress:
            try:
                await progress(endpoint, chunk, len(rows))
            except Exception:
                logger.exception("WB progress error")
        if single or len(data) < WB_ROW_THRESHOLD:
            return rows, None, chunk
        nxt = str(data[-1].get("lastChangeDate") or "")
        if not nxt or nxt == current:
            return rows, "WB: пагинация не продвигается", chunk
        current = nxt
    return rows, f"WB: достигнут лимит чанков ({MAX_WB_CHUNKS})", chunk


def _group_by_day(rows, start, end):
    lo, hi = start.isoformat(), end.isoformat()
    out = {}
    for r in rows:
        ds = str(r.get("date") or "")[:10]
        if lo <= ds <= hi:
            out.setdefault(ds, []).append(r)
    return out


async def fetch_wb(client, start, end, progress=None):
    """→ (results {ds: data}, error, meta). Ошибка любого из двух методов = ошибка источника."""
    (o_rows, o_err, o_ch), (s_rows, s_err, s_ch) = await asyncio.gather(
        _wb_pull(client, "orders", start, end, progress),
        _wb_pull(client, "sales", start, end, progress))
    meta = {"rows": len(o_rows) + len(s_rows), "chunks": o_ch + s_ch}
    if o_err or s_err:
        return {}, o_err or s_err, meta
    o_by, s_by = _group_by_day(o_rows, start, end), _group_by_day(s_rows, start, end)
    now = utc_now_iso()
    results = {}
    for ds in days_between(start, end):
        d = parse_wb_orders(o_by.get(ds, []))
        d.update(parse_wb_sales(s_by.get(ds, [])))
        d["received_at"] = now
        results[ds] = d
    return results, None, meta


# ─── OZON ──────────────────────────────────────────────────────────────────

OZON_URL = "https://api-seller.ozon.ru"


def _ozon_headers():
    h = {"Content-Type": "application/json"}
    if OZON_CLIENT_ID:
        h["Client-Id"] = OZON_CLIENT_ID
    if OZON_API_KEY:
        h["Api-Key"] = OZON_API_KEY
    return h


async def _ozon_analytics(client, start, end):
    """→ ({ds: (units, amount)}, error). Заказано штук / заказано на сумму по дням."""
    payload = {"date_from": start.isoformat(), "date_to": end.isoformat(),
               "metrics": ["ordered_units", "revenue"], "dimension": ["day"],
               "filters": [], "limit": 1000, "offset": 0}
    data, err = await api_call(client, _ozon_analytics_limiter, "POST",
                               OZON_URL + "/v1/analytics/data", headers=_ozon_headers(),
                               json_body=payload, label="Ozon analytics")
    if err:
        return {}, err
    if not isinstance(data, dict) or not isinstance(data.get("result"), dict):
        return {}, "Ozon analytics: недопустимый формат ответа"
    out = {}
    for row in data["result"].get("data") or []:
        dims, metrics = row.get("dimensions") or [], row.get("metrics") or []
        if not dims or len(metrics) < 2:
            continue
        dim = dims[0] if isinstance(dims[0], dict) else {}
        ds = str(dim.get("id") or dim.get("value") or "")[:10]
        if ds:
            out[ds] = (int(_num(metrics[0])), _num(metrics[1]))
    return out, None


def _extract_postings(resp):
    """v3 FBS: result={postings, has_next}; v2 FBO: result=[...]. → (postings, has_next|None)."""
    res = resp.get("result") if isinstance(resp, dict) else None
    if isinstance(res, list):
        return res, None
    if isinstance(res, dict):
        return res.get("postings") or [], res.get("has_next")
    return [], False


async def _ozon_postings_by_day(client, start, end):
    """→ ({ds: число отправлений FBS+FBO}, error). Дни — по Москве, дубли отсекаются."""
    since, _ = msk_day_bounds_utc(start)
    _, to = msk_day_bounds_utc(end)
    lo, hi = start.isoformat(), end.isoformat()
    ids = {}
    limit = 100
    for scheme, path in (("fbs", "/v3/posting/fbs/list"), ("fbo", "/v2/posting/fbo/list")):
        offset = 0
        for _ in range(500):
            body = {"dir": "ASC", "filter": {"since": since, "to": to},
                    "limit": limit, "offset": offset}
            resp, err = await api_call(client, _ozon_posting_limiter, "POST", OZON_URL + path,
                                       headers=_ozon_headers(), json_body=body,
                                       label=f"Ozon {scheme.upper()}")
            if err:
                return {}, err
            postings, has_next = _extract_postings(resp)
            for p in postings:
                key = p.get("posting_number") or p.get("order_id") or p.get("id")
                ds = iso_to_msk_date(p.get("in_process_at") or p.get("created_at"))
                if key and ds and lo <= ds <= hi:
                    ids.setdefault(ds, set()).add((scheme, str(key)))
            offset += len(postings)
            more = has_next if has_next is not None else len(postings) >= limit
            if not more or not postings:
                break
        else:
            return {}, f"Ozon {scheme.upper()}: слишком много страниц"
    return {ds: len(v) for ds, v in ids.items()}, None


async def fetch_ozon(client, start, end):
    """→ (results {ds: data}, error, meta). Нет отправлений (сбой) → orders=None, но units/amount целы."""
    (by_day, a_err), (counts, p_err) = await asyncio.gather(
        _ozon_analytics(client, start, end),
        _ozon_postings_by_day(client, start, end))
    if a_err:
        return {}, a_err, {}
    if p_err:
        logger.warning("Ozon: число заказов не получено (%s), units/amount сохранены", p_err)
    now = utc_now_iso()
    results = {}
    for ds in days_between(start, end):
        units, amount = by_day.get(ds, (0, 0.0))       # дня нет в ответе = заказов не было
        results[ds] = {"units": units, "amount": amount,
                       "orders": None if p_err else counts.get(ds, 0),
                       "received_at": now}
    return results, None, {"orders_error": p_err}


# ─── ВЫГРУЗКА В БАЗУ ───────────────────────────────────────────────────────

async def refresh_source(client, src, start, end, progress=None, force=True):
    days = days_between(start, end)
    if not force and all(src_fresh(get_report(ds), src) for ds in days):
        logger.info("📦 %s: все дни свежие, API не дёргаем", src)
        return {"src": src, "skipped": True, "days": len(days), "ok": len(days), "error": None}
    try:
        if src == "wb":
            results, err, meta = await fetch_wb(client, start, end, progress)
        else:
            results, err, meta = await fetch_ozon(client, start, end)
    except Exception as e:                                # ни один сбой не должен валить бота
        logger.exception("%s fetch failed", src)
        results, err, meta = {}, f"{src}: {type(e).__name__}: {e}", {}
    for ds in days:
        if err or ds not in results:
            save_source(ds, src, None, err or "день не вернулся в ответе")
        else:
            save_source(ds, src, results[ds])
    return {"src": src, "skipped": False, "days": len(days),
            "ok": 0 if err else len(days), "error": err, **meta}


async def refresh_range(start, end, sources=("ozon", "wb"), force=True, progress=None):
    """Загружает источники параллельно (у них разные лимиты). → {src: summary}."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT)) as client:
        res = await asyncio.gather(*[
            refresh_source(client, s, start, end, progress if s == "wb" else None, force)
            for s in sources])
    return {r["src"]: r for r in res}


# ─── ФОРМАТИРОВАНИЕ ОТЧЁТОВ ────────────────────────────────────────────────

def _share(part, total):
    return f"{part / total * 100:.0f}%" if total else "—"


def _src_lines(row, src, title):
    err = (row or {}).get(f"{src}_error")
    if not row or not row.get(f"{src}_received_at"):
        return [f"{title}", f"   {'❌ ' + err if err else '⏳ не загружен'}"]
    g = lambda k: row.get(f"{src}_{k}")
    lines = [title,
             f"   💰 {fmt_money(g('amount'))}   📦 {fmt_int(g('units'))} шт   "
             f"🧾 {fmt_orders(g('orders'))} зак."]
    if src == "wb":
        lines.append(f"   ↳ выкуплено: {fmt_int(g('buyout_units'))} шт · "
                     f"{fmt_money(g('buyout_amount'))} (продажи − возвраты)")
        lines.append(f"   ↳ отменено: {fmt_int(g('cancel_units'))} шт")
    if err:
        ts = parse_ts(row.get(f"{src}_received_at"))
        when = ts.astimezone(MSK).strftime("%d.%m %H:%M") if ts else "?"
        lines.append(f"   ⚠️ обновить не удалось ({err}); данные от {when} МСК")
    return lines


def format_report(row, ds=None):
    if row is None:
        return "📭 Данные не найдены."
    ds = row["report_date"]
    today = msk_today().isoformat()
    head = f"📊 Отчёт за {fmt_date(ds)}"
    if ds == today:
        head += f"\n⏱ на {msk_now().strftime('%H:%M')} МСК, день не завершён"
    lines = [head, "━" * 22, ""]
    lines += _src_lines(row, "ozon", "🔵 OZON · заказы")
    lines.append("")
    lines += _src_lines(row, "wb", "🟣 WILDBERRIES · заказы")
    lines += ["", "━" * 22]

    have = [s for s in ("ozon", "wb") if row.get(f"{s}_received_at")]
    if not have:
        lines.append("🟡 ИТОГО: ⏳ нет данных")
        return "\n".join(lines)
    amount = sum(_num(row.get(f"{s}_amount")) for s in have)
    units = sum(_num(row.get(f"{s}_units")) for s in have)
    o_vals = [row.get(f"{s}_orders") for s in have]
    orders = None if any(v is None for v in o_vals) else sum(o_vals)
    lines.append("🟡 ИТОГО ЗАКАЗЫ")
    lines.append(f"   💰 {fmt_money(amount)}   📦 {fmt_int(units)} шт   🧾 {fmt_orders(orders)} зак.")
    if len(have) == 2:
        extra = [f"Ozon {_share(_num(row['ozon_amount']), amount)} / "
                 f"WB {_share(_num(row['wb_amount']), amount)}"]
        if orders:
            extra.insert(0, f"средний чек {fmt_money(amount / orders)}")
        lines.append("   🧮 " + " · ".join(extra))
    else:
        missing = "WB" if "wb" not in have else "Ozon"
        lines.append(f"   ⚠️ неполный итог: нет данных {missing}")

    prev = get_report((date.fromisoformat(ds) - timedelta(days=1)).isoformat())
    if is_complete(row) and is_complete(prev):
        p_amt = _num(prev["ozon_amount"]) + _num(prev["wb_amount"])
        if p_amt > 0:
            lines.append(f"   📈 к пред. дню: {(amount - p_amt) / p_amt * 100:+.0f}%")
    return "\n".join(lines)


def get_period_report(days, label):
    """Последние `days` ЗАВЕРШЁННЫХ дней (по вчера включительно). Считаются только полные дни."""
    end = msk_today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    conn = _connect()
    rows = conn.execute("""
        SELECT report_date, ozon_units, ozon_amount, ozon_orders,
               wb_units, wb_amount, wb_orders, wb_buyout_units, wb_buyout_amount
        FROM daily_reports_v2
        WHERE report_date BETWEEN ? AND ?
          AND ozon_received_at IS NOT NULL AND COALESCE(ozon_error,'') = ''
          AND wb_received_at   IS NOT NULL AND COALESCE(wb_error,'')   = ''
    """, (start.isoformat(), end.isoformat())).fetchall()
    conn.close()
    if not rows:
        return f"📭 Полных данных за «{label}» нет. Запусти /backfill."

    n = len(rows)
    s = lambda k: sum(_num(r[k]) for r in rows)
    oz_a, wb_a = s("ozon_amount"), s("wb_amount")
    oz_u, wb_u = s("ozon_units"), s("wb_units")
    oz_o = None if any(r["ozon_orders"] is None for r in rows) else s("ozon_orders")
    wb_o = None if any(r["wb_orders"] is None for r in rows) else s("wb_orders")
    tot_a, tot_u = oz_a + wb_a, oz_u + wb_u
    tot_o = None if oz_o is None or wb_o is None else oz_o + wb_o

    got = {r["report_date"] for r in rows}
    miss = [d for d in days_between(start, end) if d not in got]
    lines = [f"📊 {label}: {fmt_date(start, False)} — {fmt_date(end, False)}",
             f"Полных дней: {n} из {days}", "━" * 22, "",
             "🔵 OZON · заказы",
             f"   💰 {fmt_money(oz_a)}   📦 {fmt_int(oz_u)} шт   🧾 {fmt_orders(oz_o)} зак.", "",
             "🟣 WILDBERRIES · заказы",
             f"   💰 {fmt_money(wb_a)}   📦 {fmt_int(wb_u)} шт   🧾 {fmt_orders(wb_o)} зак.",
             f"   ↳ выкуплено: {fmt_int(s('wb_buyout_units'))} шт · {fmt_money(s('wb_buyout_amount'))}",
             "", "━" * 22, "🟡 ИТОГО ЗАКАЗЫ",
             f"   💰 {fmt_money(tot_a)}   📦 {fmt_int(tot_u)} шт   🧾 {fmt_orders(tot_o)} зак.",
             f"   🧮 в среднем за день: {fmt_money(tot_a / n)} · {tot_u / n:.1f} шт",
             f"   Ozon {_share(oz_a, tot_a)} / WB {_share(wb_a, tot_a)}"]
    if miss:
        shown = ", ".join(fmt_date(d, False)[:5] for d in miss[:7])
        lines.append(f"\n⚠️ Нет полных данных за {len(miss)} дн.: {shown}"
                     f"{'…' if len(miss) > 7 else ''} — запусти /backfill")
    return "\n".join(lines)


def get_history(limit=10):
    conn = _connect()
    rows = conn.execute("SELECT * FROM daily_reports_v2 ORDER BY report_date DESC LIMIT ?",
                        (limit,)).fetchall()
    conn.close()
    if not rows:
        return "📜 История пуста. Запусти /backfill."
    today = msk_today().isoformat()
    lines = [f"📜 Последние {len(rows)} дн. (заказы):"]
    for r in map(dict, rows):
        tag = " (идёт)" if r["report_date"] == today else ""
        if is_complete(r):
            total = _num(r["ozon_amount"]) + _num(r["wb_amount"])
            lines.append(f"✅ {fmt_date(r['report_date'])[:8]}: {fmt_money(total)}{tag}")
        else:
            miss = [n for n, s in (("Ozon", "ozon"), ("WB", "wb")) if not src_ok(r, s)]
            lines.append(f"⏳ {fmt_date(r['report_date'])[:8]}: нет {' и '.join(miss)}{tag}")
    return "\n".join(lines)


def get_status():
    size = os.path.getsize(str(DB_FILE)) if DB_FILE.exists() else 0
    conn = _connect()
    r = conn.execute("""SELECT COUNT(*) c,
        SUM(ozon_received_at IS NOT NULL AND COALESCE(ozon_error,'')='') oz,
        SUM(wb_received_at IS NOT NULL AND COALESCE(wb_error,'')='') wb,
        SUM(COALESCE(ozon_error,'')<>'') oz_err, SUM(COALESCE(wb_error,'')<>'') wb_err,
        MAX(updated_at) upd FROM daily_reports_v2""").fetchone()
    last_err = conn.execute("""SELECT report_date, ozon_error, wb_error FROM daily_reports_v2
        WHERE COALESCE(ozon_error,'')<>'' OR COALESCE(wb_error,'')<>''
        ORDER BY report_date DESC LIMIT 1""").fetchone()
    conn.close()
    upd = parse_ts(r["upd"])
    lines = ["⚙️ Статус бота", "━" * 22,
             f"💾 БД: {size} байт, дней: {r['c'] or 0}",
             f"🔵 Ozon API: {'✅' if OZON_CLIENT_ID and OZON_API_KEY else '❌ нет ключей'}",
             f"🟣 WB API: {'✅' if WB_API_TOKEN else '❌ нет токена'}",
             f"📊 Загружено дней: Ozon {r['oz'] or 0}, WB {r['wb'] or 0}",
             f"⚠️ Дней с ошибкой: Ozon {r['oz_err'] or 0}, WB {r['wb_err'] or 0}",
             f"🕒 Последнее обновление: "
             f"{upd.astimezone(MSK).strftime('%d.%m %H:%M') if upd else '—'} МСК",
             f"⏰ Авто-отчёт: {REPORT_HOUR:02d}:{REPORT_MINUTE:02d} МСК "
             f"(до {DAILY_RETRIES} попыток, шаг {DAILY_RETRY_DELAY_MIN} мин)",
             f"📦 Кэш: {CACHE_HOURS} ч · backfill: {BACKFILL_DAYS} дн."]
    if last_err:
        e = last_err["ozon_error"] or last_err["wb_error"]
        lines.append(f"❗ Последняя ошибка ({fmt_date(last_err['report_date'], False)}): {e}")
    return "\n".join(lines)


def run_diagnostics():
    checks = [("TELEGRAM_BOT_TOKEN", bool(TOKEN)), ("TELEGRAM_OWNER_ID", bool(OWNER_IDS)),
              ("Ozon Client-Id + Api-Key", bool(OZON_CLIENT_ID and OZON_API_KEY)),
              ("WB API token", bool(WB_API_TOKEN)), ("Файл БД", DB_FILE.exists())]
    try:
        conn = _connect()
        n = conn.execute("SELECT COUNT(*) FROM daily_reports_v2").fetchone()[0]
        conn.close()
        checks.append(("SQLite / таблица v2", True))
    except Exception:
        logger.exception("diagnostics db")
        n = 0
        checks.append(("SQLite / таблица v2", False))
    lines = ["🧪 Диагностика", "━" * 22]
    lines += [f"{'✅' if ok else '❌'} {name}" for name, ok in checks]
    lines += ["", f"📁 {DB_FILE}", f"📦 Дней в БД: {n}",
              f"⏱ WB: {WB_MIN_INTERVAL} с · Ozon analytics: {OZON_ANALYTICS_INTERVAL} с",
              f"🔢 WB max chunks: {MAX_WB_CHUNKS} · повторов 429: {MAX_429_RETRIES}"]
    return "\n".join(lines)


# ─── ДОСТУП, КЛАВИАТУРА ────────────────────────────────────────────────────

def is_owner(message: types.Message):
    return bool(message.from_user and message.from_user.id in OWNER_IDS)


def owner_only(handler):
    @functools.wraps(handler)
    async def wrapper(message: types.Message, *args, **kwargs):
        if not is_owner(message):
            await message.answer("⛔ Доступ только для владельцев.")
            return
        return await handler(message, *args, **kwargs)
    return wrapper


BTN_YESTERDAY, BTN_TODAY = "📊 Отчёт за вчера", "📊 Отчёт за сегодня"
BTN_WEEK, BTN_MONTH, BTN_HISTORY = "📅 Неделя", "📅 Месяц", "📜 История"
BTN_REFRESH, BTN_STATUS, BTN_DIAG = "🔄 Обновить вчера", "⚙️ Статус", "🧪 Диагностика"


def get_main_keyboard():
    kb = ReplyKeyboardBuilder()
    for t in (BTN_YESTERDAY, BTN_TODAY, BTN_WEEK, BTN_MONTH,
              BTN_HISTORY, BTN_REFRESH, BTN_STATUS, BTN_DIAG):
        kb.button(text=t)
    kb.adjust(2)
    return kb.as_markup()


# ─── ОБЩИЕ СЦЕНАРИИ ────────────────────────────────────────────────────────

async def show_day(message, d, force=False):
    ds = d.isoformat()
    row = get_report(ds)
    fresh = row and src_fresh(row, "ozon") and src_fresh(row, "wb")
    if force or not fresh:
        if _job_lock.locked():
            await message.answer("⏳ Другая выгрузка уже выполняется.")
            return
        async with _job_lock:
            await message.answer(f"⏳ Обновляю {fmt_date(ds)}…")
            await refresh_range(d, d, force=True)
    await message.answer(format_report(get_report(ds)))


async def do_backfill(message, sources):
    if _job_lock.locked():
        await message.answer("⏳ Другая выгрузка уже выполняется.")
        return
    end = msk_today() - timedelta(days=1)
    start = end - timedelta(days=BACKFILL_DAYS - 1)
    await message.answer(f"📥 Выгрузка {fmt_date(start, False)} — {fmt_date(end, False)}\n"
                         f"Из-за лимитов API это займёт несколько минут.")

    async def progress(endpoint, chunk, total):
        await message.answer(f"🟣 WB {endpoint}: чанк #{chunk}, {total} записей")

    async with _job_lock:
        res = await refresh_range(start, end, sources, force=False, progress=progress)
    lines = ["✅ Выгрузка завершена"]
    for s, title in (("ozon", "🔵 Ozon"), ("wb", "🟣 WB")):
        r = res.get(s)
        if r:
            lines.append(f"{title}: ✅ {r['ok']}/{r['days']} дн." if not r["error"]
                         else f"{title}: ❌ {r['error']}")
    full = sum(1 for ds in days_between(start, end) if is_complete(get_report(ds)))
    lines.append(f"📅 Полных дней (оба МП): {full}/{BACKFILL_DAYS}")
    await message.answer("\n".join(lines))


# ─── КОМАНДЫ И КНОПКИ ──────────────────────────────────────────────────────

@dp.message(Command("start"))
@owner_only
async def cmd_start(message: types.Message):
    await message.answer(
        "🤖 Бот отчётности по продажам\n🔵 Ozon + 🟣 Wildberries\n\n"
        "Главная метрика — ЗАКАЗЫ (одинаково для обоих МП). "
        "Выкупы WB показываются справочно.\n\n"
        "Команды:\n"
        f"/backfill — выгрузка за {BACKFILL_DAYS} дн. (Ozon + WB)\n"
        "/backfill_ozon · /backfill_wb — по одному МП\n"
        "/day 2026-09-20 — отчёт за любую дату\n"
        "/test_report — обновить и показать вчера\n"
        "/diagnostics · /status",
        reply_markup=get_main_keyboard())


@dp.message(Command("status"))
@dp.message(F.text == BTN_STATUS)
@owner_only
async def cmd_status(message: types.Message):
    await message.answer(get_status())


@dp.message(Command("diagnostics"))
@dp.message(F.text == BTN_DIAG)
@owner_only
async def cmd_diagnostics(message: types.Message):
    await message.answer(run_diagnostics())


@dp.message(Command("day"))
@owner_only
async def cmd_day(message: types.Message, command: CommandObject):
    try:
        d = date.fromisoformat((command.args or "").strip())
    except ValueError:
        await message.answer("Формат: /day 2026-09-20")
        return
    if d > msk_today():
        await message.answer("Эта дата ещё не наступила.")
        return
    await show_day(message, d, force=(d == msk_today()))


@dp.message(F.text == BTN_YESTERDAY)
@owner_only
async def btn_yesterday(message: types.Message):
    await show_day(message, msk_today() - timedelta(days=1))


@dp.message(F.text == BTN_TODAY)
@owner_only
async def btn_today(message: types.Message):
    await show_day(message, msk_today(), force=True)


@dp.message(Command("test_report"))
@dp.message(F.text == BTN_REFRESH)
@owner_only
async def cmd_refresh_yesterday(message: types.Message):
    await show_day(message, msk_today() - timedelta(days=1), force=True)


@dp.message(F.text == BTN_WEEK)
@owner_only
async def btn_week(message: types.Message):
    await message.answer(get_period_report(7, "Неделя (7 завершённых дней)"))


@dp.message(F.text == BTN_MONTH)
@owner_only
async def btn_month(message: types.Message):
    await message.answer(get_period_report(30, "Месяц (30 завершённых дней)"))


@dp.message(F.text == BTN_HISTORY)
@owner_only
async def btn_history(message: types.Message):
    await message.answer(get_history(10))


@dp.message(Command("backfill"))
@owner_only
async def cmd_backfill(message: types.Message):
    await do_backfill(message, ("ozon", "wb"))


@dp.message(Command("backfill_ozon"))
@owner_only
async def cmd_backfill_ozon(message: types.Message):
    await do_backfill(message, ("ozon",))


@dp.message(Command("backfill_wb"))
@owner_only
async def cmd_backfill_wb(message: types.Message):
    await do_backfill(message, ("wb",))


# ─── АВТО-ОТЧЁТ ────────────────────────────────────────────────────────────

async def _notify_owners(text):
    if not bot:
        return
    for oid in OWNER_IDS:
        try:
            await bot.send_message(oid, text)
        except Exception:
            logger.exception("Не удалось отправить сообщение %s", oid)


async def send_daily_report():
    """
    Отчёт за вчера уходит сразу после первой попытки. Если один из источников не загрузился,
    бот повторяет попытку и присылает обновление, как только отчёт станет полным.
    """
    d = msk_today() - timedelta(days=1)
    ds = d.isoformat()
    for attempt in range(1, DAILY_RETRIES + 1):
        async with _job_lock:                       # ждём очереди, а не пропускаем отчёт
            await refresh_range(d, d, force=True)
        row = get_report(ds)
        complete = is_complete(row)
        if attempt == 1:
            await _notify_owners(format_report(row))
        elif complete:
            await _notify_owners("🔄 Отчёт обновлён — теперь полный:\n\n" + format_report(row))
        if complete:
            return
        if attempt < DAILY_RETRIES:
            logger.warning("Авто-отчёт неполный, попытка %d/%d, повтор через %d мин",
                           attempt, DAILY_RETRIES, DAILY_RETRY_DELAY_MIN)
            await asyncio.sleep(DAILY_RETRY_DELAY_MIN * 60)
    await _notify_owners("❌ Отчёт за вчера так и не стал полным после "
                         f"{DAILY_RETRIES} попыток. Проверь /status.")


async def daily_scheduler_loop():
    while True:
        now = msk_now()
        target = now.replace(hour=REPORT_HOUR, minute=REPORT_MINUTE, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        logger.info("⏰ Следующий авто-отчёт через %d ч %d мин", wait // 3600, wait % 3600 // 60)
        await asyncio.sleep(wait)
        try:
            await send_daily_report()
        except Exception:
            logger.exception("Ошибка авто-отчёта")
            await _notify_owners("❌ Авто-отчёт упал с ошибкой, подробности в логах.")


# ─── ЗАПУСК ────────────────────────────────────────────────────────────────

async def main():
    init_db()
    if not bot:
        logger.error("❌ TELEGRAM_BOT_TOKEN не задан!")
        return
    if not OWNER_IDS:
        logger.warning("⚠️ TELEGRAM_OWNER_ID не задан — доступ закрыт для всех")
    logger.info("🚀 Бот запущен")
    scheduler = asyncio.create_task(daily_scheduler_loop(), name="daily_report_scheduler")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.cancel()
        try:
            await scheduler
        except asyncio.CancelledError:
            pass
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
