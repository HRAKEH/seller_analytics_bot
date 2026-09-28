# -*- coding: utf-8 -*-
"""
Telegram-бот для отчётности по продажам на Ozon и Wildberries.
Архитектура: httpx (async HTTP) + sqlite3 (sync DB) + aiogram 3.x.

Ключевые решения:
- WB: чанкованная выгрузка через lastChangeDate (лимит 80 000 строк на ответ).
- Ozon: 1 запрос analytics с dimension=day для массовой выгрузки.
  Ozon API возвращает данные в result.data (не result.rows!).
- Авто-отчёт за вчера каждый день в 09:00 МСК (UTC+3).
- Команды: /backfill, /backfill_ozon, /backfill_wb, /test_report, /status.
"""

import os
import logging
import asyncio
import sqlite3
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.utils.keyboard import ReplyKeyboardBuilder

# ─── КОНФИГ ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
DB_FILE = Path(os.getenv("DB_FILE", "/app/data/daily_reports.sqlite3"))

_owner_raw = os.getenv("TELEGRAM_OWNER_ID", "")
OWNER_IDS = [int(x.strip()) for x in _owner_raw.split(",") if x.strip().isdigit()]

OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID", "")
OZON_API_KEY = os.getenv("OZON_API_KEY", "")
WB_API_TOKEN = os.getenv("WB_API_TOKEN", "")

# Время авто-отчёта (МСК). По умолчанию 09:00.
REPORT_HOUR = int(os.getenv("REPORT_HOUR", "9"))
REPORT_MINUTE = int(os.getenv("REPORT_MINUTE", "0"))
_MSK_TZ = timezone(timedelta(hours=3))

def _msk_today():
    """Текущая дата по московскому времени (UTC+3)."""
    return datetime.now(_MSK_TZ).date()

# Лимиты задаются с запасом. Это не обход лимитов, а их соблюдение.
_WB_MIN_INTERVAL = max(1, int(os.getenv("WB_MIN_INTERVAL", "65")))
_WB_ROW_THRESHOLD = 79000
_OZON_MIN_INTERVAL = max(1, int(os.getenv("OZON_MIN_INTERVAL", "65")))
BACKFILL_DAYS = max(1, min(int(os.getenv("BACKFILL_DAYS", "30")), 90))
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "180"))
CACHE_HOURS = max(1, int(os.getenv("CACHE_HOURS", "6")))
MAX_NET_RETRIES = max(1, int(os.getenv("MAX_NET_RETRIES", "5")))
MAX_WB_CHUNKS = max(1, min(int(os.getenv("MAX_WB_CHUNKS", "100")), 1000))

bot = Bot(token=TOKEN) if TOKEN else None
dp = Dispatcher()

# Один процесс бота: запросы к каждому API сериализуются.
_wb_rate_lock = asyncio.Lock()
_ozon_rate_lock = asyncio.Lock()
_job_lock = asyncio.Lock()
_wb_next_request_at = 0.0
_ozon_next_request_at = 0.0

# ─── КЭШ ─────────────────────────────────────────────────────────────────

def _is_cache_fresh(received_at_str):
    """Проверяет, что кэшированные данные моложе CACHE_HOURS."""
    if not received_at_str:
        return False
    try:
        received = datetime.fromisoformat(received_at_str)
        age_hours = (datetime.now() - received).total_seconds() / 3600
        return age_hours < CACHE_HOURS
    except (ValueError, TypeError):
        return False


# ─── БАЗА ДАННЫХ (sqlite3, синхронно) ──────────────────────────────────────

def init_db():
    """Создаёт таблицы. Вызывается без await."""
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_reports (
            report_date      TEXT PRIMARY KEY,
            ozon_units       INTEGER DEFAULT 0,
            ozon_amount      REAL    DEFAULT 0,
            ozon_orders      INTEGER DEFAULT 0,
            wb_units         INTEGER DEFAULT 0,
            wb_amount        REAL    DEFAULT 0,
            wb_orders        INTEGER DEFAULT 0,
            total_units      INTEGER DEFAULT 0,
            total_amount     REAL    DEFAULT 0,
            total_orders     INTEGER DEFAULT 0,
            wb_error         TEXT,
            ozon_received_at TEXT,
            wb_received_at   TEXT,
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
    logger.info("📦 База данных готова: " + str(DB_FILE))


def save_report(report_date, report):
    """Сохраняет отчёт в БД (INSERT OR REPLACE)."""
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        INSERT OR REPLACE INTO daily_reports
        (report_date, ozon_units, ozon_amount, ozon_orders,
         wb_units, wb_amount, wb_orders,
         total_units, total_amount, total_orders,
         wb_error, ozon_received_at, wb_received_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        str(report_date),
        report.get("ozon_units", 0),
        report.get("ozon_amount", 0),
        report.get("ozon_orders", 0),
        report.get("wb_units", 0),
        report.get("wb_amount", 0),
        report.get("wb_orders", 0),
        report.get("total_units", 0),
        report.get("total_amount", 0),
        report.get("total_orders", 0),
        report.get("wb_error"),
        report.get("ozon_received_at"),
        report.get("wb_received_at"),
    ))
    conn.commit()
    conn.close()


def get_report(report_date):
    """Возвращает sqlite3.Row или None."""
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM daily_reports WHERE report_date = ?", (str(report_date),)
    ).fetchone()
    conn.close()
    return row


# ─── ФОРМАТИРОВАНИЕ ───────────────────────────────────────────────────────

def format_money(amount):
    """format_money(2000) -> '2 000 ₽'"""
    if amount is None:
        return "0 ₽"
    return "{:,}".format(int(round(amount))).replace(",", " ") + " ₽"


# ─── ПАРСЕР WB ─────────────────────────────────────────────────────────────

def _parse_wb_sales(rows, target_date_str=None):
    units = 0
    amount = 0.0
    orders = set()
    for row in rows or []:
        if target_date_str and row.get("date") and str(row["date"])[:10] != target_date_str:
            continue
        sid = str(row.get("saleID", "")).upper()
        try:
            raw_price = row.get("forPay")
            if raw_price is None:
                raw_price = row.get("finishedPrice")
            if raw_price is None:
                raw_price = row.get("totalCost")
            price = float(raw_price or 0)
        except (TypeError, ValueError):
            price = 0.0
        srid = str(row.get("srid", "") or "")
        if sid.startswith("S"):
            units += 1
            amount += price
            if srid:
                orders.add(srid)
        elif sid.startswith("R"):
            units -= 1
            amount -= price
    return {"units": units, "amount": amount, "orders": len(orders),
            "received_at": datetime.now().isoformat(), "error": None}


# ─── API: OZON ─────────────────────────────────────────────────────────────

def _ozon_headers():
    h = {"Content-Type": "application/json"}
    if OZON_CLIENT_ID:
        h["Client-Id"] = OZON_CLIENT_ID
    if OZON_API_KEY:
        h["Api-Key"] = OZON_API_KEY
    return h


async def _ozon_request_with_retry(client, url, payload, headers, max_retries=MAX_NET_RETRIES):
    """POST Ozon: бесконечный 429 retry, ограниченные сетевые retry."""
    global _ozon_next_request_at
    net_retries = 0
    while True:
        async with _ozon_rate_lock:
            loop = asyncio.get_running_loop()
            wait = max(0.0, _ozon_next_request_at - loop.time())
            if wait:
                logger.info("⌛ Ozon: ждём %.1f сек", wait)
                await asyncio.sleep(wait)
            retry_wait = 0.0
            try:
                resp = await client.post(url, json=payload, headers=headers)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
                _ozon_next_request_at = loop.time() + _OZON_MIN_INTERVAL
                if net_retries >= max_retries:
                    return None, f"Ozon: {e}"
                net_retries += 1
                retry_wait = min(300.0, _OZON_MIN_INTERVAL * net_retries) + random.uniform(0, 2)
                logger.warning("⚠️ Ozon network error: %s; retry %.1fs", e, retry_wait)
            else:
                _ozon_next_request_at = loop.time() + _OZON_MIN_INTERVAL
                if resp.status_code == 200:
                    try:
                        return resp.json(), None
                    except ValueError:
                        return None, "Ozon: некорректный JSON"
                if resp.status_code == 429:
                    raw = resp.headers.get("Retry-After") or resp.headers.get("X-Ratelimit-Retry")
                    try:
                        retry_wait = float(raw) if raw else _OZON_MIN_INTERVAL
                    except (TypeError, ValueError):
                        retry_wait = _OZON_MIN_INTERVAL
                    retry_wait = max(1.0, retry_wait)
                    _ozon_next_request_at = loop.time() + retry_wait
                    logger.warning("⚠️ Ozon 429: retry %.1fs", retry_wait)
                elif 500 <= resp.status_code < 600:
                    if net_retries >= max_retries:
                        logger.error("Ozon %s: %s", resp.status_code, resp.text[:500])
                        return None, f"Ozon {resp.status_code}"
                    net_retries += 1
                    retry_wait = min(120.0, 5.0 * (2 ** net_retries)) + random.uniform(0, 1)
                    logger.warning("⚠️ Ozon %s: retry %.1fs", resp.status_code, retry_wait)
                else:
                    logger.error("Ozon %s: %s", resp.status_code, resp.text[:500])
                    return None, f"Ozon {resp.status_code}"
        if retry_wait:
            await asyncio.sleep(retry_wait)


async def ozon_sales(d: date, client: httpx.AsyncClient, force_refresh: bool = False):
    """Ozon: units/revenue из analytics и число FBS postings. С кэшированием."""
    if not force_refresh:
        cached = get_report(d.isoformat())
        if cached and _is_cache_fresh(cached["ozon_received_at"]):
            return {"units": cached["ozon_units"], "amount": cached["ozon_amount"],
                    "orders": cached["ozon_orders"], "received_at": cached["ozon_received_at"],
                    "error": None}
    ds = d.isoformat()
    payload = {
        "metrics": ["ordered_units", "revenue"], "dimension": ["day"],
        "date_from": ds, "date_to": ds, "limit": 1000, "offset": 0
    }
    data, err = await _ozon_request_with_retry(
        client, "https://api-seller.ozon.ru/v1/analytics/data", payload, _ozon_headers()
    )
    if err or not data:
        return {"units": 0, "amount": 0.0, "orders": None, "received_at": None, "error": err or "пустой ответ"}

    obj = data.get("result", {}) if isinstance(data, dict) else {}
    rows = obj.get("data") or obj.get("rows") or []
    metrics = rows[0].get("metrics", []) if rows else obj.get("totals", [])
    if len(metrics) < 2:
        return {"units": 0, "amount": 0.0, "orders": None, "received_at": None,
                "error": "analytics: нет metrics"}

    units, amount = int(metrics[0] or 0), float(metrics[1] or 0)

    # FBS имеет отдельный rate limit, поэтому каждый запрос проходит через тот же gate.
    orders, cursor = 0, ""
    try:
        for _ in range(100):
            payload = {
                "dir": "asc",
                "filter": {"since": ds + "T00:00:00.000Z",
                           "to": (d + timedelta(days=1)).isoformat() + "T00:00:00.000Z"},
                "limit": 100, "cursor": cursor
            }
            fbs, ferr = await _ozon_request_with_retry(
                client, "https://api-seller.ozon.ru/v4/posting/fbs/list", payload, _ozon_headers()
            )
            if ferr:
                return {"units": units, "amount": amount, "orders": None,
                        "received_at": datetime.now().isoformat(), "error": ferr}
            obj2 = (fbs or {}).get("result", fbs or {})
            postings = obj2.get("postings", []) or []
            ids = {str(x.get("posting_number") or x.get("order_id") or x.get("id"))
                   for x in postings if x.get("posting_number") or x.get("order_id") or x.get("id")}
            orders += len(ids) if ids else len(postings)
            if not obj2.get("has_next"):
                break
            new_cursor = obj2.get("cursor") or obj2.get("last_id") or ""
            if not new_cursor or new_cursor == cursor:
                return {"units": units, "amount": amount, "orders": None,
                        "received_at": datetime.now().isoformat(), "error": "FBS: курсор не продвинулся"}
            cursor = new_cursor
        else:
            return {"units": units, "amount": amount, "orders": None,
                    "received_at": datetime.now().isoformat(), "error": "FBS: safety limit страниц"}
    except Exception as e:
        logger.exception("Ozon FBS error")
        return {"units": units, "amount": amount, "orders": None,
                "received_at": datetime.now().isoformat(), "error": str(e)}

    return {"units": units, "amount": amount, "orders": orders,
            "received_at": datetime.now().isoformat(), "error": None}


async def bulk_ozon_sales(start_date: date, end_date: date, client: httpx.AsyncClient, skip_cached: bool = False):
    """Analytics Ozon по дням. Ошибка не превращается в загруженные нули."""
    if skip_cached:
        all_fresh = True
        cur = start_date
        while cur <= end_date:
            cached = get_report(cur.isoformat())
            if not cached or not _is_cache_fresh(cached["ozon_received_at"]):
                all_fresh = False
                break
            cur += timedelta(days=1)
        if all_fresh:
            result = {}
            cur = start_date
            while cur <= end_date:
                cached = get_report(cur.isoformat())
                result[cur.isoformat()] = {"units": cached["ozon_units"],
                    "amount": cached["ozon_amount"], "orders": cached["ozon_orders"],
                    "received_at": cached["ozon_received_at"], "error": None}
                cur += timedelta(days=1)
            logger.info("📦 Ozon: все дни свежие, API не дёргаем")
            return result
    result = {}
    cur = start_date
    while cur <= end_date:
        result[cur.isoformat()] = {"units": 0, "amount": 0.0, "orders": None,
                                    "received_at": None, "error": None}
        cur += timedelta(days=1)

    payload = {
        "metrics": ["ordered_units", "revenue"], "dimension": ["day"],
        "date_from": start_date.isoformat(), "date_to": end_date.isoformat(),
        "limit": 1000, "offset": 0
    }
    data, err = await _ozon_request_with_retry(
        client, "https://api-seller.ozon.ru/v1/analytics/data", payload, _ozon_headers()
    )
    if err or not data:
        msg = err or "пустой ответ Ozon"
        for x in result.values(): x["error"] = msg
        return result

    obj = data.get("result", {}) if isinstance(data, dict) else {}
    rows = obj.get("data") or obj.get("rows") or []
    for row in rows:
        dims, metrics = row.get("dimensions", []), row.get("metrics", [])
        if not isinstance(dims, list) or not dims or len(metrics) < 2:
            continue
        dim = dims[0] if isinstance(dims[0], dict) else {}
        ds = str(dim.get("id") or dim.get("value") or "")[:10]
        if ds in result:
            result[ds].update(
                units=int(metrics[0] or 0),
                amount=float(metrics[1] or 0),
                received_at=datetime.now().isoformat(),
            )
    for ds, x in result.items():
        if x["received_at"] is None:
            x["error"] = "день отсутствует в ответе Ozon"
    return result


# ─── API: WILDBERRIES ───────────────────────────────────────────────────────

def _wb_headers():
    h = {}
    if WB_API_TOKEN:
        h["Authorization"] = WB_API_TOKEN
    return h


async def _wb_request_with_rate_limit(client, params, headers, attempt_label=""):
    """GET WB: бесконечный 429 retry, X-Ratelimit-Remaining, ограниченные сетевые retry."""
    global _wb_next_request_at
    net_retries = 0
    while True:
        async with _wb_rate_lock:
            loop = asyncio.get_running_loop()
            wait = max(0.0, _wb_next_request_at - loop.time())
            if wait:
                logger.info("⌛ WB: ждём %.1f сек%s", wait, f" ({attempt_label})" if attempt_label else "")
                await asyncio.sleep(wait)
            retry_wait = 0.0
            try:
                resp = await client.get(
                    "https://statistics-api.wildberries.ru/api/v1/supplier/sales",
                    params=params, headers=headers
                )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
                _wb_next_request_at = loop.time() + _WB_MIN_INTERVAL
                if net_retries >= MAX_NET_RETRIES:
                    return None, f"WB: {e}"
                net_retries += 1
                retry_wait = min(300.0, _WB_MIN_INTERVAL * net_retries) + random.uniform(0, 2)
                logger.warning("⚠️ WB network error: %s; retry %.1fs", e, retry_wait)
            else:
                _wb_next_request_at = loop.time() + _WB_MIN_INTERVAL
                if resp.status_code == 200:
                    remaining = resp.headers.get("X-Ratelimit-Remaining")
                    if remaining is not None:
                        try:
                            rem = int(remaining)
                            if rem <= 1:
                                logger.info("WB: X-Ratelimit-Remaining=%d, замедляемся", rem)
                                _wb_next_request_at = loop.time() + _WB_MIN_INTERVAL * 2
                        except ValueError:
                            pass
                    try:
                        data = resp.json()
                    except ValueError:
                        return None, "WB: некорректный JSON"
                    return (data, None) if isinstance(data, list) else (None, "WB: недопустимый формат")
                if resp.status_code == 429:
                    raw = resp.headers.get("X-Ratelimit-Retry") or resp.headers.get("Retry-After")
                    try:
                        retry_wait = float(raw) if raw else _WB_MIN_INTERVAL
                    except (TypeError, ValueError):
                        retry_wait = _WB_MIN_INTERVAL
                    retry_wait = max(1.0, retry_wait)
                    _wb_next_request_at = loop.time() + retry_wait
                    logger.warning("⚠️ WB 429: retry %.1fs%s", retry_wait,
                                   f" ({attempt_label})" if attempt_label else "")
                elif 500 <= resp.status_code < 600:
                    if net_retries >= MAX_NET_RETRIES:
                        logger.error("WB %s: %s", resp.status_code, resp.text[:500])
                        return None, f"WB {resp.status_code}"
                    net_retries += 1
                    retry_wait = min(120.0, 5.0 * (2 ** net_retries)) + random.uniform(0, 1)
                    logger.warning("⚠️ WB %s: retry %.1fs", resp.status_code, retry_wait)
                else:
                    logger.error("WB %s: %s", resp.status_code, resp.text[:500])
                    return None, f"WB {resp.status_code}"
        if retry_wait:
            await asyncio.sleep(retry_wait)


async def wb_sales(d: date, client: httpx.AsyncClient, force_refresh: bool = False):
    """Продажи WB за день через flag=1. С кэшированием."""
    if not force_refresh:
        cached = get_report(d.isoformat())
        if cached and _is_cache_fresh(cached["wb_received_at"]) and not cached["wb_error"]:
            return {"units": cached["wb_units"], "amount": cached["wb_amount"],
                    "orders": cached["wb_orders"], "received_at": cached["wb_received_at"],
                    "error": None}
    data, err = await _wb_request_with_rate_limit(
        client, {"dateFrom": d.isoformat(), "flag": 1}, _wb_headers(), f"flag=1 {d.isoformat()}"
    )
    if err:
        logger.error("🔵 WB %s: %s", d, err)
        return None
    return _parse_wb_sales(data, d.isoformat())


async def bulk_wb_sales(start_date: date, end_date: date, client: httpx.AsyncClient, progress_callback=None, skip_cached: bool = False):
    """Выгрузка WB с официальной пагинацией через lastChangeDate."""
    if skip_cached:
        all_fresh = True
        cur = start_date
        while cur <= end_date:
            cached = get_report(cur.isoformat())
            if not cached or not _is_cache_fresh(cached["wb_received_at"]) or cached["wb_error"]:
                all_fresh = False
                break
            cur += timedelta(days=1)
        if all_fresh:
            result = {}
            cur = start_date
            while cur <= end_date:
                cached = get_report(cur.isoformat())
                result[cur.isoformat()] = {"units": cached["wb_units"],
                    "amount": cached["wb_amount"], "orders": cached["wb_orders"],
                    "received_at": cached["wb_received_at"], "error": None}
                cur += timedelta(days=1)
            logger.info("📦 WB: все дни свежие, API не дёргаем")
            return {"data": result, "error": None, "rows": 0, "chunks": 0}
    all_rows, current = [], start_date.isoformat()
    chunk_num, error = 0, None
    seen_signatures = set()

    while chunk_num < MAX_WB_CHUNKS:
        chunk_num += 1
        data, err = await _wb_request_with_rate_limit(
            client, {"dateFrom": current, "flag": 0}, _wb_headers(),
            f"chunk #{chunk_num} from {current}"
        )
        if err:
            error = err
            break
        if not data:
            break

        signature = (len(data), str(data[0].get("srid", "")),
                     str(data[-1].get("srid", "")),
                     str(data[-1].get("lastChangeDate", "")))
        if signature in seen_signatures:
            error = "WB: повтор страницы, пагинация не продвигается"
            break
        seen_signatures.add(signature)

        all_rows.extend(data)
        if progress_callback:
            try:
                await progress_callback(chunk_num, len(all_rows))
            except Exception:
                logger.exception("WB progress callback error")

        if len(data) < _WB_ROW_THRESHOLD:
            break

        next_from = str(data[-1].get("lastChangeDate", "") or "")
        if not next_from:
            error = "WB: отсутствует lastChangeDate"
            break
        if next_from == current:
            error = "WB: lastChangeDate не продвинулся; остановлено"
            break
        current = next_from
    else:
        error = f"WB: достигнут safety limit чанков ({MAX_WB_CHUNKS})"

    by_date = {}
    for row in all_rows:
        ds = str(row.get("date", ""))[:10]
        if start_date.isoformat() <= ds <= end_date.isoformat():
            by_date.setdefault(ds, []).append(row)

    result = {}
    if error is None:
        now = datetime.now().isoformat()
        cur = start_date
        while cur <= end_date:
            ds = cur.isoformat()
            result[ds] = _parse_wb_sales(by_date.get(ds, []))
            result[ds]["received_at"] = now
            cur += timedelta(days=1)

    return {"data": result, "error": error, "rows": len(all_rows), "chunks": chunk_num}


# ─── ОТЧЁТЫ ────────────────────────────────────────────────────────────────

def build_report_for_date(d, ozon_data, wb_data, wb_error=None):
    report = {
        "report_date": str(d),
        "ozon_units": 0, "ozon_amount": 0.0, "ozon_orders": None,
        "wb_units": 0, "wb_amount": 0.0, "wb_orders": 0,
        "total_units": 0, "total_amount": 0.0, "total_orders": None,
        "wb_error": wb_error, "ozon_received_at": None, "wb_received_at": None,
    }
    if ozon_data:
        report.update(
            ozon_units=ozon_data.get("units", 0),
            ozon_amount=ozon_data.get("amount", 0),
            ozon_orders=ozon_data.get("orders"),
            ozon_received_at=ozon_data.get("received_at"),
        )
    if wb_data:
        report.update(
            wb_units=wb_data.get("units", 0),
            wb_amount=wb_data.get("amount", 0),
            wb_orders=wb_data.get("orders", 0),
            wb_received_at=wb_data.get("received_at"),
        )
    return _recalc_totals(report)


def _recalc_totals(report):
    oz_ok = report.get("ozon_received_at") is not None
    wb_ok = report.get("wb_received_at") is not None and not report.get("wb_error")
    if oz_ok and wb_ok:
        report["total_units"] = (report.get("ozon_units") or 0) + (report.get("wb_units") or 0)
        report["total_amount"] = (report.get("ozon_amount") or 0) + (report.get("wb_amount") or 0)
        oo, wo = report.get("ozon_orders"), report.get("wb_orders")
        report["total_orders"] = oo + wo if oo is not None and wo is not None else None
    else:
        report["total_units"], report["total_amount"], report["total_orders"] = 0, 0.0, None
    return report


def format_report(row):
    if row is None:
        return "📭 Данные не найдены."
    lines = [f"📊 Отчёт за {row['report_date']}", "━" * 20]
    if row["ozon_received_at"]:
        oo = "—" if row["ozon_orders"] is None else str(row["ozon_orders"])
        lines.append(f"🟣 OZON: {format_money(row['ozon_amount'] or 0)} | {row['ozon_units'] or 0} шт | {oo} зак.")
    else:
        lines.append("🟣 OZON: ⏳ не загружен")
    if row["wb_error"]:
        lines.append(f"🔵 WILDBERRIES: ❌ {row['wb_error']}")
    elif row["wb_received_at"]:
        lines.append(f"🔵 WILDBERRIES: {format_money(row['wb_amount'] or 0)} | {row['wb_units'] or 0} шт | {row['wb_orders'] or 0} зак.")
    else:
        lines.append("🔵 WILDBERRIES: ⏳ не загружен")
    if row["ozon_received_at"] and row["wb_received_at"] and not row["wb_error"]:
        to = "—" if row["total_orders"] is None else str(row["total_orders"])
        lines.append(f"🟡 ИТОГО: {format_money(row['total_amount'] or 0)} | {row['total_units'] or 0} шт | {to} зак.")
    else:
        lines.append("🟡 ИТОГО: ⏳ не рассчитан (не все источники загружены)")
    return "\n".join(lines)


# ─── ПЕРИОДИЧЕСКИЕ ОТЧЁТЫ ──────────────────────────────────────────────────

def get_period_report(start_date, end_date, label):
    conn = sqlite3.connect(str(DB_FILE), timeout=30)
    conn.row_factory = sqlite3.Row
    row = conn.execute("""
        SELECT COALESCE(SUM(ozon_amount),0) oz_a, COALESCE(SUM(ozon_units),0) oz_u,
               SUM(ozon_orders) oz_o, COALESCE(SUM(wb_amount),0) wb_a,
               COALESCE(SUM(wb_units),0) wb_u, SUM(wb_orders) wb_o, COUNT(*) days
        FROM daily_reports
        WHERE report_date BETWEEN ? AND ?
          AND ozon_received_at IS NOT NULL
          AND wb_received_at IS NOT NULL
          AND (wb_error IS NULL OR wb_error='')
    """,(start_date,end_date)).fetchone()
    conn.close()
    if not row or not row["days"]:
        return f"📭 Полных данных за «{label}» нет. Запусти /backfill."
    total_o = row["oz_o"] + row["wb_o"] if row["oz_o"] is not None and row["wb_o"] is not None else None
    return "\n".join([
        f"📊 Отчёт: {label}", f"Период: {start_date} — {end_date} ({row['days']} полных дн.)", "━"*20,
        f"🟣 OZON: {format_money(row['oz_a'])} | {row['oz_u']} шт | {'—' if row['oz_o'] is None else row['oz_o']} зак.",
        f"🔵 WILDBERRIES: {format_money(row['wb_a'])} | {row['wb_u']} шт | {'—' if row['wb_o'] is None else row['wb_o']} зак.",
        f"🟡 ИТОГО: {format_money(row['oz_a']+row['wb_a'])} | {row['oz_u']+row['wb_u']} шт | {'—' if total_o is None else total_o} зак.",
    ])


def get_history(limit=10):
    conn=sqlite3.connect(str(DB_FILE),timeout=30); conn.row_factory=sqlite3.Row
    rows=conn.execute("SELECT * FROM daily_reports ORDER BY report_date DESC LIMIT ?",(limit,)).fetchall()
    conn.close()
    if not rows: return "📜 История пуста. Запусти /backfill."
    lines=[f"📜 Последние {len(rows)} дней:"]
    for r in rows:
        ok=r["ozon_received_at"] and r["wb_received_at"] and not r["wb_error"]
        if ok:
            total=(r["ozon_amount"] or 0)+(r["wb_amount"] or 0)
            lines.append(f"✅ {r['report_date']}: {format_money(total)}")
        else:
            lines.append(f"⏳ {r['report_date']}: неполные данные")
    return "\n".join(lines)


def get_status():
    db_size=os.path.getsize(str(DB_FILE)) if DB_FILE.exists() else 0
    conn=sqlite3.connect(str(DB_FILE),timeout=30); conn.row_factory=sqlite3.Row
    row=conn.execute("""SELECT COUNT(*) c,
      SUM(CASE WHEN ozon_received_at IS NOT NULL THEN 1 ELSE 0 END) oz,
      SUM(CASE WHEN wb_received_at IS NOT NULL AND (wb_error IS NULL OR wb_error='') THEN 1 ELSE 0 END) wb,
      SUM(CASE WHEN wb_error IS NOT NULL AND wb_error<>'' THEN 1 ELSE 0 END) err
      FROM daily_reports""").fetchone()
    conn.close()
    return "\n".join([
        "⚙️ Статус бота","━"*20,f"💾 БД: {db_size} байт, записей: {row['c'] or 0}",
        f"🟣 Ozon API: {'✅' if OZON_CLIENT_ID and OZON_API_KEY else '❌'}",
        f"🔵 WB API: {'✅' if WB_API_TOKEN else '❌'}",
        f"📊 Загружено: Ozon {row['oz'] or 0}, WB {row['wb'] or 0}",
        f"⚠️ Ошибок WB: {row['err'] or 0}",f"📦 Backfill: {BACKFILL_DAYS} полных дней",
        f"⏰ Авто-отчёт: {REPORT_HOUR:02d}:{REPORT_MINUTE:02d} МСК",
        f"📦 Кэш: {CACHE_HOURS} ч",
        f"🔄 429: бесконечный retry",
        "🔒 Защита от параллельных выгрузок: ✅"
    ])


# ─── КЛАВИАТУРА ────────────────────────────────────────────────────────────

def get_main_keyboard():
    kb = ReplyKeyboardBuilder()
    kb.button(text="📊 Отчёт за вчера")
    kb.button(text="📊 Отчёт за сегодня")
    kb.button(text="📅 Неделя")
    kb.button(text="📅 Месяц")
    kb.button(text="📜 История")
    kb.button(text="🔄 Обновить вчера")
    kb.button(text="⚙️ Статус")
    kb.button(text="🧪 Диагностика")
    kb.adjust(2)
    return kb.as_markup()


# ─── ХЕЛПЕР ────────────────────────────────────────────────────────────────

def is_owner(message: types.Message):
    return message.from_user and message.from_user.id in OWNER_IDS


# ─── АВТО-ОТЧЁТ (09:00 МСК) ─────────────────────────────────────────────────

async def _send_daily_report():
    d=_msk_today()-timedelta(days=1); ds=d.isoformat()
    if _job_lock.locked():
        logger.warning("⏳ Авто-отчёт пропущен: занята другая выгрузка")
        return
    async with _job_lock:
        async with httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT)) as client:
            oz=await ozon_sales(d,client,force_refresh=True); wb=await wb_sales(d,client,force_refresh=True)
        wb_err=None if wb else "WB: ошибка запроса"
        save_report(ds,build_report_for_date(d,oz,wb,wb_err))
    if bot:
        text=format_report(get_report(ds))
        for owner_id in OWNER_IDS:
            try: await bot.send_message(owner_id,text)
            except Exception: logger.exception("⏰ Ошибка отправки авто-отчёта %s",owner_id)


async def _daily_scheduler_loop():
    """Бесконечный цикл: спит до 09:00 МСК, затем отправляет отчёт."""
    while True:
        now_msk = datetime.now(_MSK_TZ)
        target = now_msk.replace(hour=REPORT_HOUR, minute=REPORT_MINUTE, second=0, microsecond=0)
        if now_msk >= target:
            target += timedelta(days=1)

        wait_sec = (target - now_msk).total_seconds()
        hours = int(wait_sec // 3600)
        minutes = int((wait_sec % 3600) // 60)
        logger.info("⏰ Следующий авто-отчёт через " + str(hours) + " ч " + str(minutes) + " мин (в " + str(REPORT_HOUR).zfill(2) + ":" + str(REPORT_MINUTE).zfill(2) + " МСК)")

        await asyncio.sleep(wait_sec)

        try:
            await _send_daily_report()
        except Exception as e:
            logger.error("⏰ Ошибка авто-отчёта: " + str(e))

        # Спим 60 сек чтобы не запустить дважды в ту же минуту
        await asyncio.sleep(60)


def run_diagnostics():
    """Локальная диагностика конфигурации, БД и зарегистрированных обработчиков."""
    checks = []

    checks.append(("TELEGRAM_BOT_TOKEN", bool(TOKEN)))
    checks.append(("TELEGRAM_OWNER_ID", bool(OWNER_IDS)))
    checks.append(("OZON credentials", bool(OZON_CLIENT_ID and OZON_API_KEY)))
    checks.append(("WB API token", bool(WB_API_TOKEN)))
    checks.append(("DB", DB_FILE.exists()))

    conn_ok = False
    try:
        conn = sqlite3.connect(str(DB_FILE), timeout=10)
        conn.execute("SELECT 1 FROM daily_reports LIMIT 1").fetchone()
        conn.close()
        conn_ok = True
    except Exception as e:
        logger.exception("Diagnostics DB error: %s", e)
    checks.append(("SQLite", conn_ok))

    lines = ["🧪 Локальная диагностика", "━" * 20]
    for name, ok in checks:
        lines.append(f"{'✅' if ok else '❌'} {name}")

    lines.extend([
        "",
        f"📁 БД: {DB_FILE}",
        f"📦 Записей: {get_status_record_count()}",
        f"⏱ WB интервал: {_WB_MIN_INTERVAL} сек",
        f"⏱ Ozon интервал: {_OZON_MIN_INTERVAL} сек",
        f"📅 Backfill: {BACKFILL_DAYS} дней",
        f"🔢 WB max chunks: {MAX_WB_CHUNKS}",
        f"📦 Кэш: {CACHE_HOURS} ч",
        f"🔄 429: бесконечный retry",
    ])
    return "\n".join(lines)


def get_status_record_count():
    if not DB_FILE.exists():
        return 0
    try:
        conn = sqlite3.connect(str(DB_FILE), timeout=10)
        row = conn.execute("SELECT COUNT(*) FROM daily_reports").fetchone()
        conn.close()
        return int(row[0] or 0)
    except Exception:
        logger.exception("Diagnostics count error")
        return 0


# ─── КОМАНДЫ ────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not is_owner(message):
        await message.answer("⛔ Доступ только для владельцев.")
        return
    await message.answer(
        "🤖 Бот отчётности по продажам\n"
        "━" * 20 + "\n"
        "🟣 Ozon + 🔵 Wildberries\n\n"
        "Команды:\n"
        "/backfill — выгрузка за 30 дней (Ozon + WB)\n"
        "/backfill_ozon — только Ozon\n"
        "/backfill_wb — только WB\n"
        "/test_report — тест авто-отчёта за вчера\n"
        "/diagnostics — локальная диагностика\n"
        "/status — статус бота",
        reply_markup=get_main_keyboard(),
    )


@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    if not is_owner(message):
        await message.answer("⛔ Доступ только для владельцев.")
        return
    await message.answer(get_status())


@dp.message(Command("test_report"))
async def cmd_test_report(message: types.Message):
    if not is_owner(message): await message.answer("⛔ Доступ только для владельцев."); return
    if _job_lock.locked(): await message.answer("⏳ Другая выгрузка уже выполняется."); return
    d=_msk_today()-timedelta(days=1)
    async with _job_lock:
        await message.answer(f"⏳ Тест отчёта за {d}...")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT)) as client:
                oz=await ozon_sales(d,client,force_refresh=True); wb=await wb_sales(d,client,force_refresh=True)
            save_report(d.isoformat(),build_report_for_date(d,oz,wb,None if wb else "WB: ошибка запроса"))
        except Exception as e:
            logger.exception("test_report error"); await message.answer(f"❌ Ошибка теста: {e}"); return
    await message.answer("✅ Тест:\n\n"+format_report(get_report(d.isoformat())))


@dp.message(Command("backfill_ozon"))
async def cmd_backfill_ozon(message: types.Message):
    if not is_owner(message): await message.answer("⛔ Доступ только для владельцев."); return
    if _job_lock.locked(): await message.answer("⏳ Другая выгрузка уже выполняется."); return
    today=_msk_today(); end=today-timedelta(days=1); start=end-timedelta(days=BACKFILL_DAYS-1)
    await message.answer(f"📥 Ozon за {start} — {end}\n🟣 Analytics по дням.")
    async with _job_lock:
        async with httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT)) as client:
            bulk=await bulk_ozon_sales(start,end,client,skip_cached=True)
        cur=start
        while cur<=end:
            ds=cur.isoformat(); oz=bulk.get(ds); existing=get_report(ds)
            if existing:
                report={
                    "report_date":ds,
                    "ozon_units":oz.get("units",0) if oz and oz.get("received_at") else existing["ozon_units"] or 0,
                    "ozon_amount":oz.get("amount",0) if oz and oz.get("received_at") else existing["ozon_amount"] or 0,
                    "ozon_orders":oz.get("orders") if oz and oz.get("received_at") else existing["ozon_orders"],
                    "ozon_received_at":oz.get("received_at") if oz and oz.get("received_at") else existing["ozon_received_at"],
                    "wb_units":existing["wb_units"] or 0,"wb_amount":existing["wb_amount"] or 0,
                    "wb_orders":existing["wb_orders"],"wb_received_at":existing["wb_received_at"],"wb_error":existing["wb_error"]}
                save_report(ds,_recalc_totals(report))
            elif oz and oz.get("received_at"):
                save_report(ds,build_report_for_date(cur,oz,None))
            cur+=timedelta(days=1)
    ok=sum(1 for x in bulk.values() if x.get("received_at"))
    await message.answer(f"🟣 Ozon готово\n✅ Загружено: {ok}/{len(bulk)} дн.\n⚠️ Не загружено: {len(bulk)-ok} дн.")


@dp.message(Command("backfill_wb"))
async def cmd_backfill_wb(message: types.Message):
    if not is_owner(message): await message.answer("⛔ Доступ только для владельцев."); return
    if _job_lock.locked(): await message.answer("⏳ Другая выгрузка уже выполняется."); return
    today=_msk_today(); end=today-timedelta(days=1); start=end-timedelta(days=BACKFILL_DAYS-1)
    await message.answer(f"📥 WB за {start} — {end}\n🔵 Собираю чанки, лимит соблюдается автоматически.")
    async with _job_lock:
        async with httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT)) as client:
            async def progress(chunk,total):
                try: await message.answer(f"🔵 WB chunk #{chunk}: {total} записей")
                except Exception: pass
            res=await bulk_wb_sales(start,end,client,progress,skip_cached=True)
        wb_bulk, wb_error=res["data"],res["error"]
        cur=start; loaded=0
        while cur<=end:
            ds=cur.isoformat(); existing=get_report(ds); wb=wb_bulk.get(ds)
            if existing:
                report={
                    "report_date":ds,"ozon_units":existing["ozon_units"] or 0,
                    "ozon_amount":existing["ozon_amount"] or 0,"ozon_orders":existing["ozon_orders"],
                    "ozon_received_at":existing["ozon_received_at"],
                    "wb_units":wb.get("units",0) if wb else 0,"wb_amount":wb.get("amount",0) if wb else 0,
                    "wb_orders":wb.get("orders",0) if wb else 0,"wb_received_at":wb.get("received_at") if wb else None,
                    "wb_error":None if wb else (wb_error or "WB: день не загружен")}
            else:
                report=build_report_for_date(cur,None,wb,None if wb else (wb_error or "WB: день не загружен"))
            save_report(ds,_recalc_totals(report)); loaded+=1; cur+=timedelta(days=1)
    status="✅ без ошибок" if not wb_error else f"⚠️ {wb_error}"
    await message.answer(f"🔵 WB: {status}\n📅 Обработано: {loaded} дн.\n📦 Строк: {res['rows']}\n🔢 Чанков: {res['chunks']}")


@dp.message(Command("backfill"))
async def cmd_backfill(message: types.Message):
    if not is_owner(message): await message.answer("⛔ Доступ только для владельцев."); return
    if _job_lock.locked(): await message.answer("⏳ Другая выгрузка уже выполняется."); return
    today=_msk_today(); end=today-timedelta(days=1); start=end-timedelta(days=BACKFILL_DAYS-1)
    await message.answer(f"📥 Полная выгрузка за {start} — {end}\n🟣 Ozon → 🔵 WB")
    async with _job_lock:
        async with httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT)) as client:
            oz=await bulk_ozon_sales(start,end,client,skip_cached=True)
            async def progress(chunk,total):
                try: await message.answer(f"🔵 WB chunk #{chunk}: {total} записей")
                except Exception: pass
            wr=await bulk_wb_sales(start,end,client,progress,skip_cached=True)
        wb, wb_error=wr["data"],wr["error"]; cur=start; complete=0
        while cur<=end:
            ds=cur.isoformat(); od=oz.get(ds); wd=wb.get(ds)
            report=build_report_for_date(
                cur, od if od and od.get("received_at") else None,
                wd if wd else None,
                None if wd else (wb_error or "WB: день не загружен")
            )
            save_report(ds,report)
            if report["ozon_received_at"] and report["wb_received_at"] and not report["wb_error"]: complete+=1
            cur+=timedelta(days=1)
    await message.answer(f"✅ Backfill завершён\n📅 Дней: {BACKFILL_DAYS}\n✅ Полных: {complete}\n🟣 Ozon: {sum(1 for x in oz.values() if x.get('received_at'))} дн.\n🔵 WB: {len(wb)} дн." + (f"\n⚠️ WB: {wb_error}" if wb_error else ""))


# ─── КНОПКИ ─────────────────────────────────────────────────────────────────

@dp.message(lambda msg: msg.text == "📊 Отчёт за вчера")
async def btn_yesterday(message: types.Message):
    if not is_owner(message): await message.answer("⛔ Доступ только для владельцев."); return
    d=_msk_today()-timedelta(days=1)
    row=get_report(d.isoformat())
    if row and row["ozon_received_at"] and row["wb_received_at"] and not row["wb_error"]:
        if _is_cache_fresh(row["ozon_received_at"]) and _is_cache_fresh(row["wb_received_at"]):
            await message.answer(format_report(row)); return
    if _job_lock.locked(): await message.answer("⏳ Другая выгрузка уже выполняется."); return
    async with _job_lock:
        await message.answer(f"⏳ Обновляю {d}...")
        async with httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT)) as client:
            oz=await ozon_sales(d,client); wb=await wb_sales(d,client)
        save_report(d.isoformat(),build_report_for_date(d,oz,wb,None if wb else "WB: ошибка запроса"))
    await message.answer(format_report(get_report(d.isoformat())))


@dp.message(lambda msg: msg.text == "📊 Отчёт за сегодня")
async def btn_today(message: types.Message):
    if not is_owner(message): await message.answer("⛔ Доступ только для владельцев."); return
    if _job_lock.locked(): await message.answer("⏳ Другая выгрузка уже выполняется."); return
    d=_msk_today()
    async with _job_lock:
        await message.answer(f"⏳ Загружаю актуальные данные за {d}...")
        async with httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT)) as client:
            oz=await ozon_sales(d,client,force_refresh=True); wb=await wb_sales(d,client,force_refresh=True)
        save_report(d.isoformat(),build_report_for_date(d,oz,wb,None if wb else "WB: ошибка запроса"))
    await message.answer(format_report(get_report(d.isoformat())))


@dp.message(lambda msg: msg.text == "📅 Неделя")
async def btn_week(message: types.Message):
    if not is_owner(message):
        await message.answer("⛔ Доступ только для владельцев.")
        return
    today = _msk_today()
    start = today - timedelta(days=6)
    await message.answer(get_period_report(start.isoformat(), today.isoformat(), "Неделя"))


@dp.message(lambda msg: msg.text == "📅 Месяц")
async def btn_month(message: types.Message):
    if not is_owner(message):
        await message.answer("⛔ Доступ только для владельцев.")
        return
    today = _msk_today()
    start = today - timedelta(days=29)
    await message.answer(get_period_report(start.isoformat(), today.isoformat(), "Месяц"))


@dp.message(lambda msg: msg.text == "📜 История")
async def btn_history(message: types.Message):
    if not is_owner(message):
        await message.answer("⛔ Доступ только для владельцев.")
        return
    await message.answer(get_history(10))



@dp.message(Command("diagnostics"))
async def cmd_diagnostics(message: types.Message):
    if not is_owner(message): await message.answer("⛔ Доступ только для владельцев."); return
    await message.answer(run_diagnostics())

@dp.message(lambda msg: msg.text == "🧪 Диагностика")
async def btn_diagnostics(message: types.Message):
    if not is_owner(message): await message.answer("⛔ Доступ только для владельцев."); return
    await message.answer(run_diagnostics())

@dp.message(lambda msg: msg.text == "🔄 Обновить вчера")
async def btn_refresh_yesterday(message: types.Message):
    if not is_owner(message): await message.answer("⛔ Доступ только для владельцев."); return
    if _job_lock.locked(): await message.answer("⏳ Другая выгрузка уже выполняется."); return
    d=_msk_today()-timedelta(days=1)
    async with _job_lock:
        await message.answer(f"⏳ Обновляю {d}...")
        async with httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT)) as client:
            oz=await ozon_sales(d,client,force_refresh=True); wb=await wb_sales(d,client,force_refresh=True)
        save_report(d.isoformat(),build_report_for_date(d,oz,wb,None if wb else "WB: ошибка запроса"))
    await message.answer(format_report(get_report(d.isoformat())))

@dp.message(lambda msg: msg.text == "⚙️ Статус")
async def btn_status(message: types.Message):
    if not is_owner(message):
        await message.answer("⛔ Доступ только для владельцев.")
        return
    await message.answer(get_status())


# ─── ЗАПУСК ──────────────────────────────────────────────────────────────────

async def main():
    init_db()
    if not bot:
        logger.error("❌ TELEGRAM_BOT_TOKEN не задан!"); return
    if not OWNER_IDS:
        logger.warning("⚠️ TELEGRAM_OWNER_ID не задан")
    logger.info("🚀 Бот запущен")
    scheduler=asyncio.create_task(_daily_scheduler_loop(),name="daily_report_scheduler")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.cancel()
        try: await scheduler
        except asyncio.CancelledError: pass
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
