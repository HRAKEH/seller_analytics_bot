import os
import aiosqlite
from pathlib import Path

# --- ЛОГИКА ПУТИ К БД (УМНАЯ) ---
# 1. Если в панели BotHost задана переменная DB_FILE - берем её.
# 2. Если нет, но мы на BotHost (папка /app/data существует) - пишем туда.
# 3. Если локально (на компьютере) - пишем в текущую папку.
raw_path = os.getenv("DB_FILE")

if raw_path:
    DB_PATH = Path(raw_path)
elif Path("/app/data").exists():
    # Мы на BotHost, используем специальную папку, которая НЕ удаляется при Rebuild
    DB_PATH = Path("/app/data/daily_reports.sqlite3")
else:
    # Локальный запуск
    DB_PATH = Path("./daily_reports.sqlite3")

# Создаем родительскую папку, если её нет (для локального запуска)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

print(f"[DB] Инициализация базы данных по пути: {DB_PATH}")

async def init_db():
    """Создает таблицу, если её нет, и настраивает SQLite для работы в асинхронном режиме."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        # КРИТИЧЕСКИ ВАЖНО для BotHost и asyncio:
        # WAL режим позволяет читать и писать одновременно (избегает ошибки "database is locked")
        await db.execute("PRAGMA journal_mode=WAL")
        # Если база занята, ждать до 5 секунд перед ошибкой
        await db.execute("PRAGMA busy_timeout=5000")
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                source TEXT NOT NULL, 
                revenue REAL DEFAULT 0,
                orders_count INTEGER DEFAULT 0,
                UNIQUE(date, source) ON CONFLICT REPLACE
            )
        """)
        await db.commit()
        print("[DB] Таблицы готовы к работе.")

async def save_report(date, source, revenue, orders_count):
    """Сохраняет отчет. Если дата+источник уже есть - обновляет данные."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("""
            INSERT INTO reports (date, source, revenue, orders_count)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(date, source) DO UPDATE SET
                revenue = excluded.revenue,
                orders_count = excluded.orders_count
        """, (date, source, revenue, orders_count))
        await db.commit()

async def get_report(date):
    """Получает отчеты за конкретную дату."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        cursor = await db.execute("""
            SELECT source, revenue, orders_count 
            FROM reports 
            WHERE date = ?
        """, (date,))
        return await cursor.fetchall()
