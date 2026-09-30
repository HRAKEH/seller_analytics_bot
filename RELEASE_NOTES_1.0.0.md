# Seller Analytics Bot 1.0.0

Первый стабильный релиз универсального Telegram-бота аналитики Wildberries + Ozon.

## Что входит в v1.0

- единая сопоставимая операционная метрика заказанных товарных единиц;
- раздельные продажи/возвраты/финансы/выплаты/реклама без подмены одной метрики другой;
- дневные, недельные, месячные, товарные, финансовые, рекламные и SKU-отчёты;
- lifecycle reconciliation WB/Ozon;
- остатки, inbound-поставки, ABC/XYZ, прозрачный прогноз спроса и рекомендации по пополнению;
- акции, backtesting и ограниченная самокалибровка supply-модели;
- Action Center с приоритетами и workflow действий;
- multi-shop, RBAC owner/analyst/viewer и system-owner boundary;
- safe DEMO/onboarding/readiness;
- XLSX/CSV export, verified SQLite backup/restore;
- persistent retry queue, distributed renewable leases, health/preflight и structured logs;
- полностью кнопочный Telegram UX: каждая публичная slash-команда имеет каноническую emoji-кнопку.

## Что изменилось непосредственно в Phase 20

Phase 20 не меняет бизнес-формулы и не повышает schema version (остаётся v14). Это hardening-релиз: исправлена multi-tenant изоляция глобальных административных операций, ownership retry-задач, потеря distributed lease, дата scheduler retry, атомарный restore/migration rollback, traceback redaction, spreadsheet formula injection и fail-fast поведение критичных фоновых сервисов.

## Перед первым production-запуском

1. Прочитайте `SECURITY.md` и `RELEASE_CHECKLIST.md`.
2. Настройте секреты только через environment variables.
3. Для Bothost используйте `DB_FILE=/app/data/seller_analytics.sqlite3`.
4. Выполните `python scripts/preflight.py`.
5. После запуска пройдите кнопки `🔌 Проверить подключения` и `✅ Готовность магазина`.
6. Сверьте первый отчёт за завершённый день с кабинетами WB/Ozon.

## Ограничения v1.0

- Проверенный production backend — SQLite. PostgreSQL не заявлен как поддерживаемый backend.
- Финансовые и рекламные данные могут приходить позже операционных; бот сохраняет эту разницу и не превращает неполные данные в ноль.
- Прогноз спроса и план поставок — прозрачная операционная оценка, не гарантия будущего спроса.
- Для некоторых Ozon/WB связок точная event-level атрибуция невозможна, если API не предоставляет общий идентификатор; бот показывает агрегат вместо выдуманной связи.
