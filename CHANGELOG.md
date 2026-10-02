# Changelog

## 1.0.10 — 2026-10-03

- В личном чате новое служебное меню сначала отправляется с рабочей клавиатурой, затем предыдущее удаляется. Нажатия известных кнопок убираются после успешной обработки. Отчёты, документы, ручной ввод и результаты изменения магазина сохраняются.
- Под очистку подключены главная навигация и разделы, выбор магазина, даты и экспорта, подсказки ввода аргументов, импорта, восстановления и шаги первичной настройки. Inline-выбор формата экспорта редактирует ту же карточку; завершённые селекторы удаляются после доставки результата. Неверную дату можно исправить без повторного открытия меню.
- Schema 17 сохраняет только ID последнего служебного сообщения для бота, чата и пользователя, независимо от выбранного магазина. После перезапуска очистка продолжается. Медленный отчёт и нажатие старой inline-кнопки не удаляют новое меню; ошибки Telegram при удалении не прерывают действие. В группах очистка не выполняется.
- Подтверждена работа существующей сводки оповещений: 150 предупреждений об остатках и восстановление отправляются одним сообщением с общими счётчиками, ограничением длины и указанием на полный список активных проблем.
- Добавлены проверки реальных Telegram-обработчиков без внешней отправки: переходы, отчёты, экспорт, ручной ввод, отмена, мастер, перезапуск, разделение чатов/ботов, сбои и конкурирующие действия.

## 1.0.9 — 2026-10-03

- Дневной отчёт Ozon показывает суммы по цене покупателя отдельно для каждой валюты. Цена берётся из сохранённых деталей FBO и финансового блока FBS; умножается на количество из списка отправлений. Отменённые заказы и нулевые цены учитываются в заказах; отсутствие цены или валюты не считается нулём.
- Полнота проверяется по SKU и количеству из того снимка аналитики, который дал основной показатель заказов. Детали FBO используются только для соответствующего `list_source_run_id`; дубли отправлений учитываются один раз. Неполные или неоднозначные данные не публикуются как окончательный денежный итог.
- Для полного набора валютных цен автоматически запрашиваются курсы ЦБ на дату отчёта. Учитываются номиналы валют, фактическая дата действия курса и Decimal; общая сумма округляется один раз до копеек. Рублёвый результат помечен как оценка по ЦБ, без утверждения о совпадении с курсом Ozon.
- Schema 16 добавляет отдельные снимки курсов с исходным XML, датой запроса, датой действия и временем получения. Они входят в backup, повторно используются для исторических отчётов и не подменяют здоровье API маркетплейсов. Сбой ЦБ сохраняет исходные валюты и последний корректный курс для этой даты.
- Автоматический пересчёт подключён к ежедневной рассылке, повторной отправке и просмотру отчётов по дате. Принудительное обновление дня также собирает цены FBO/FBS. Добавлены проверки финансовой арифметики, состава заказов, неполных цен, недоступного/неверного курса, backup и автоматической рассылки.

## 1.0.8 — 2026-10-02

- После успешной загрузки списка FBO запрашивается `/v2/posting/fbo/get` с `with.financial_data=true` для каждого уникального номера отправления, включая отменённые. Ответы с ценой покупателя и валютой сохраняются целиком, без преобразования, отдельно от списка.
- Детали группируются по московской дате из списка и связаны с исходной загрузкой через `list_source_run_id`. Одинаковые номера не вызывают повторных запросов в пределах дня; разные отправления одного заказа получают отдельные запросы.
- Частичные ответы сохраняются в `postings/fbo/details` со статусом `partial` и списком ошибок, не заменяя прошлый успешный снимок. Ошибки запросов регистрируются с HTTP-кодом; неполная детализация вызывает `⚠️` в итогах обновления. После 401/403/429 дальнейшие запросы деталей в этом диапазоне пропускаются с явной отметкой.
- Проверены реальные SQLite backup, сохранение вложенных полей, нулевых цен и валюты, ошибочный номер в ответе, пустые дни и отсутствие номера в списке. Заказы, начисления, исходные списки и схема БД 15 остаются на прежних источниках; автоматическая итоговая сумма по цене реализации ещё не введена.
- Добавлена инструкция обновления и получения бэкапа с деталями FBO. Доступность цен на конкретном аккаунте и рублёвого эквивалента валютных заказов проверяется по фактическим ответам Ozon.

## 1.0.7 — 2026-10-02

- На каждой странице актуальных списков отправлений FBO/FBS запрашивается `with.financial_data=true`. Финансовый блок и неизвестные вложенные поля сохраняются в исходных JSON без преобразования и попадают в SQLite backup.
- Сохранённый ответ содержит отметку `request_with`, позволяющую отличить новый запрос финансового блока от старых загрузок, включая успешные пустые дни и ответы с `financial_data=null`.
- Проверены многостраничная загрузка, московская дата заказа, сохранение блока в созданном backup и отсутствие подмены аналитики/начислений. Ошибка на следующей странице не сохраняет неполный ответ как успешный и оставляет предыдущий успешный снимок.
- Добавлена инструкция обновления и сбора бэкапа за выбранный день. Схема БД 15 и денежные формулы не меняются; цена реализации ещё не рассчитывается автоматически.

## 1.0.6 — 2026-10-02

- Уточнён вид суммы заказов Ozon: ежедневный и товарный отчёты показывают предельную цену из аналитики API. В ежедневном отчёте указана соответствующая колонка личного кабинета для сверки.
- Документировано различие между предельной ценой, ценой реализации покупателю и начислениями после удержаний. Отсутствующая цена реализации не восстанавливается из финансовых расходов.
- Денежные значения, API-запросы, финансовые формулы и schema 15 сохранены. Добавлена инструкция обновления и сравнения без платной выгрузки аналитики.

## 1.0.5 — 2026-10-02

- Оповещения магазина за один цикл проверки отправляются каждому получателю одной сводкой. Критичные события приоритетны, восстановления включены, большой список ограничен размером Telegram-сообщения с явным числом остальных событий.
- Единая отправка сводки используется обоими планировщиками; права доступа, интервал проверки и cooldown сохранены.
- Ежедневный отчёт Ozon явно разделяет сумму заказов из аналитики API и начисления по дате начисления. Денежные формулы и схема БД не менялись.
- Добавлены проверки одного сообщения на получателя, изоляции магазинов, пустых циклов, экранирования и длинных сообщений с emoji, а также инструкция обновления.

## 1.0.4 — 2026-10-01

- Полное обновление отчётов за выбранный завершённый период: `/refresh`, отдельный результат для заказов, финансов и рекламы WB/Ozon, продолжение независимых источников при ошибке и прогресс в Telegram.
- Общие аргументы `[дней] [YYYY-MM-DD]` для финансов, рекламы, управленческого расчёта, SKU и сверки. Кнопки чтения сохранённых отчётов не вызывают API.
- `/sources` и финансовые отчёты показывают загруженные/отсутствующие дни, время проверки API, неполные ответы и неуспешные повторы. Нулевой успешный ответ отличим от отсутствующего.
- `/accruals` показывает итог и выгружает операции Ozon в CSV с source_run_id, строкой ответа и типами удержаний; неизвестные типы сохраняются, исправления берутся из последнего успешного ответа.
- SKU-экономика Ozon исключает повторное вычитание Performance за дни с финансовыми начислениями, включая старые данные без отдельной рекламной метрики. Реклама по товарам из начислений — часть услуг; расходы без SKU не распределяются автоматически.
- Полный успешный рекламный снимок обнуляет исчезнувшие кампании/SKU с сохранением истории; успешные пустые рекламные дни сохраняются как нулевые, неожиданный формат Ozon не считается пустым ответом.
- Денежные составляющие начислений суммируются через Decimal; результат округляется до копеек. Существующая SQLite schema 15 и типы хранения сохранены.
- Ежедневная рассылка и повторная отправка собираются после попыток загрузить задержанные финансы/рекламу. Частичный результат retry больше не считается успешным.
- Логи ограничены ротацией 5 MiB × 4 файла; `.dockerignore` исключает секреты, базы, бэкапы и выгрузки из Docker-образа.
- Необязательная независимая копия ежедневного backup в Telegram system owner: `AUTO_BACKUP_SEND_TELEGRAM=true`. Доставка повторяется без создания новой копии; данные всех магазинов доступны только system owners. По умолчанию выключена.
- Добавлены инструкция установки на аккаунте клиента и инструкция обновления 1.0.4. Коммерческий биллинг/искусственные ограничения функций не включались.

## 1.0.3 — WB analytics orders and Ozon accrual reconciliation

- Use the complete WB Sales Funnel product snapshot as the primary order count/value, with validated pagination and the Analytics token category. Preserve operational Statistics orders for lifecycle reconciliation and warehouse schemes; label fallback explicitly.
- Keep Statistics and Analytics versions independent, prevent operational refreshes from overwriting funnel totals, and upgrade cached legacy days during backfill. Hide WB daily, period and product growth comparisons across different source bases.
- Separate Ozon ordered value from financial net accruals in daily reports; show financial amounts with cents and support `/finance [days] [YYYY-MM-DD]`.
- Identify billed Ozon advertising inside services, including signed corrections. Management deducts it once and uses Performance only on days without accruals; old finance snapshots remain supported.
- Add newly introduced metrics when an unchanged API payload is collected again, preserving raw-response idempotency and schema 15.
- Use Moscow calendar boundaries for Ozon postings, repair historic event dates from their UTC timestamps, and clear obsolete fulfillment entries after a complete refresh.
- Add regression coverage and a Bothost update/reconciliation guide. Database schema remains 15.

## 1.0.2 — Production handoff and financial source audit

- Combine WB report types into one daily metric snapshot, deduplicating report IDs.
- Recognize current WB detailed finance aliases and signed return documents; update corrected stable finance events.
- Preserve signed Ozon expense corrections and scalar/Money fees; reject malformed finance and foreign currencies.
- Match Ozon aggregate advertising to ordersMoney; avoid non-posting synthetic financial sales.
- Warn about unknown marketplace/ad expenses and suppress incompatible WB/Ozon money totals.
- Restrict calibration to matured horizons of the current method and preference horizon. Use full calendar weeks for XYZ and avoid fake growth percentages from zero.
- Redact credentials before HTTP error truncation; hide secrets in configuration repr; fail-fast Performance OAuth readiness on 429.
- Add bounded read-only host live audit, regression tests, accountant formulas, user guide and pilot monetization plan.
- Update pytest to 9.0.3 and pin pip 26.2.1 in CI/Docker. Schema remains 15.

## 1.0.1 — Formula and snapshot consistency audit

- Schema 15 preserves all data while allowing historical A → B → A corrections; consecutive identical responses remain idempotent and refresh source freshness.
- Complete order refreshes clear previously observed SKU values missing from the new snapshot. Independent posting flow stays separate.
- Ozon daily finance refreshes replace obsolete SKU components and reconciliation events, including empty days and legacy positional fingerprints.
- Management COGS must reconcile with aggregate orders per day; missing order revenue or SKU coverage suppresses the result. Action Center uses the same calculation.
- SKU expenses now include acquiring. Partial order runs no longer count as complete days for product comparison or stock velocity.
- Inventory freshness uses the oldest contributing snapshot. Overdue unreceived supplies do not offset replenishment. Promotion uplift is removed from the training baseline before being applied to future promotion days.
- Forecast bias includes zero-actual horizons; backtests exclude history before a product exists. Missing data does not send false order-drop or DRR recovery alerts.
- Reject malformed/non-finite numeric metrics, preserve explicit zero posting quantities, and distinguish unknown demand from zero demand in stock reports.
- Add regression coverage for formula, snapshot, migration and missing-data edge cases.

## 1.0.0 — Stable Release / Phase 20

- Final release audit completed without changing marketplace metric semantics or schema v14.
- Fixed distributed-operation lease loss: the in-flight task is cancelled immediately when lease ownership is lost.
- Hardened persistent retry jobs with renewable job leases and owner-checked complete/fail transitions, preventing stale workers from committing a reclaimed job.
- Fixed daily scheduler outer-retry date so a failed scheduled run always retries the last completed local day, not today's incomplete data.
- Added fail-fast process behavior for crashed critical background services and SQLite integrity failure; startup now always releases the singleton Telegram polling lease on partial initialization failure.
- Strengthened migration rollback and restore through SQLite backup API + quick-check + atomic replacement; fresh failed migrations remove the partial database instead of leaving a half-created schema.
- Runtime leases and process heartbeats remain ephemeral across restore.
- Added multi-tenant security boundary: whole-database backup/restore, shop creation and credential-profile administration are system-owner-only (`TELEGRAM_OWNER_ID`), while shop owners remain scoped to their shop.
- Scoped retry queue, Action Center dead jobs, health and diagnostics to the active shop; public HTTP health no longer exposes instance IDs, lease owners, heartbeats or retry internals.
- Credential profiles are validated before persistence so a typo cannot leave a shop pointing at an invalid secret profile.
- Added future-date guard for `/day` to prevent incomplete future dates from being persisted as operational data.
- Structured logging now redacts configured secrets from exception tracebacks as well as messages; DB/log/backup files are created with restrictive permissions when supported.
- XLSX/CSV exports neutralize spreadsheet-formula prefixes from marketplace/user-controlled strings.
- Added `SECURITY.md`, `RELEASE_CHECKLIST.md` and dependency-light `scripts/release_audit.py`.
- Added Phase 20 regression coverage for release-hardening and security scenarios.

## 0.19.0 — Universal Onboarding, Readiness & Safe Demo

- Added schema v14 with per-shop demo/onboarding state and durable readiness snapshots.
- Added `✅ Готовность магазина`: critical blockers are separated from optional improvements instead of being hidden behind one opaque score.
- Added live `🔌 Проверить подключения`: WB uses official category `/ping` endpoints plus seller-info; Ozon uses `/v1/seller/info` and `/v1/roles`; optional Ozon Performance credentials are verified through OAuth token acquisition.
- Added `🧭 Как подключить магазин` with credential-profile-specific environment variable names. API secrets are never requested in Telegram and remain outside SQLite.
- Added safe `🧪 Включить демо` mode with deterministic synthetic WB/Ozon orders, inventory, finance and advertising data. Demo collectors never call external APIs.
- Demo mode is allowed only for an empty shop and removes its synthetic connections/products when disabled, preventing contamination of production analytics.
- `/setup` now records onboarding version 19 and immediately shows the local readiness checklist after completion.
- Added `/readiness`, `/connect_check`, `/help`, `/demo_on`, `/demo_off`; every command has a role-aware emoji button in the existing menu hierarchy.
- Added Phase 19 regression tests for v13→v14 migration, demo safety, readiness persistence and probe endpoints.

## 0.18.0 — Action Center 2.0 & Operational Workflow

- Added schema v13 with durable Action Center state and daily action history.
- Rebuilt Action Center into four transparent priority bands: P1 urgent, P2 today, P3 planned optimization and P4 observe.
- Stock risk, replenishment need, stale inventory and an upcoming confirmed promotion for the same physical product are consolidated into one action instead of duplicate warnings.
- Added configured-DRR campaign actions and negative management-result actions only when product-cost coverage is complete.
- Added stable action keys, evidence lines and explicit next-step navigation so every priority can be audited.
- Added workflow statuses: open, acknowledged, snoozed and automatically resolved. Acknowledgement never hides an unresolved factual problem.
- Added `/action_history`, `/action_ack` and `/action_snooze`; every command has a role-aware Telegram button.
- Daily scheduler persists Action Center snapshots automatically so resolved conditions close even when nobody manually opens the report.
- XLSX/CSV export now contains both current `Actions` and durable `ActionHistory`.
- Added Phase 18 regression tests for schema v13, acknowledgement/snooze/auto-resolution, supply+promotion deduplication and configured DRR priorities.

## 0.17.0 — Self-Calibrating Replenishment Buffers

- Added schema v12 with durable `supply_calibration_snapshots` and audited calibration fields on supply recommendation snapshots.
- Manual lead-time and safety-stock settings remain immutable policy inputs; calibration only adds bounded risk buffers.
- Safety calibration uses completed rolling forecast backtests plus observed zero-stock snapshots. Missing inventory days remain unknown, never synthetic zero.
- Lead/acceptance calibration uses actual WB fact-date lateness only when at least three observations exist; Ozon planned arrival dates are deliberately excluded from actual-delay learning.
- Added per-shop controls `auto_calibration_enabled`, `max_lead_buffer_days` and `max_safety_buffer_days`.
- Added `/supply_calibration` and `/supply_calibration_refresh` with role-aware Telegram buttons `🧠 Самокалибровка` and `🔄 Пересчитать калибровку`.
- Supply reports and exports expose base vs learned vs effective lead/safety values and calibration confidence.
- Daily scheduler now persists forecast-quality evidence and the supply plan automatically so learning does not depend on manual report views.
- Action Center uses effective calibrated lead time when deciding whether stock risk is critical.
- Added Phase 17 regression tests covering sparse evidence, bounded buffers, disabled auto-apply, WB actual lateness vs Ozon planned dates, export and v11→v12 migration.

## 0.16.0 — Promotion Calendar & Forecast Calibration

- Added schema v11 with durable marketplace promotion calendars and exact product participation.
- Wildberries integration uses the official Promotions Calendar endpoints; auto promotions stay calendar-visible but are not falsely treated as SKU-resolved because WB does not expose nomenclature details for them.
- Ozon integration uses `/v1/actions` and `/v1/actions/products`; product IDs are resolved through `/v3/product/info/list` so promotions can be linked to existing listings by `offer_id` when available.
- Added `/promotions [days]` and `/promotions_refresh` plus Telegram buttons `📅 Акции` and `🔄 Обновить акции`.
- Promotion refresh is part of the daily delayed-data cycle and persistent retry queue.
- Replenishment model upgraded to `promo-bias-wma-v3`. Future promotion days affect demand only when exact SKU participation is known and historical uplift has enough observations.
- Historical promo uplift is bounded and shrunk toward neutral; insufficient history means factor `1.00`, never an invented boost.
- Forecast bias correction uses prior rolling backtests, corrects only half of observed systematic error and is bounded to ±15%.
- Supply reports expose `bias_correction`, `promo_factor` and `promo_days` so the forecast remains auditable.
- Action Center surfaces upcoming linked promotions and export includes a `Promotions` dataset.
- Migration v10→v11 preserves existing supply snapshots and initializes the new calibration columns to neutral values.

## 0.15.0 — Inbound Supplies, Forecast Quality & Action Center

- Added schema v10 with durable inbound shipment/items and forecast-quality snapshots.
- Added active inbound collection for Wildberries FBW supplies and Ozon FBO supply orders with pagination and per-supply goods/bundles.
- Inbound quantities reduce replenishment recommendations only when a known ETA falls inside the planning horizon; unknown ETA remains visible but is not treated as available stock.
- Complete active-supply snapshots close supplies that disappeared from the marketplace active list; partial responses never erase previously known inbound state.
- Supply planning upgraded to `seasonal-wma-v2`: transparent weighted demand baseline plus bounded weekday seasonality when enough complete history exists.
- Added rolling forecast backtesting with WAPE/bias and durable quality snapshots.
- Added `🎯 Что делать сегодня` action center combining active alerts, dead retry jobs, replenishment urgency, stale/unknown inventory, missing cost coverage and forecast-quality warnings.
- Added `/inbound`, `/inbound_refresh`, `/forecast_quality`, `/actions`; every new command has a role-aware Telegram button in the existing menu hierarchy.
- `/supply_refresh` now refreshes inventory and inbound supplies before recomputing replenishment.
- XLSX/CSV export now includes `Inbound`, `ForecastQuality` and `Actions`.
- Scheduler/retry worker can refresh inbound supplies independently of the fast daily sales report.
- Unknown inventory remains unknown; no stock or inbound API failure is converted into a synthetic zero.

## 0.14.0 — Menu-first Telegram UX

- Replaced the flat keyboard with role-aware logical menus: Reports, Products/SKU, Money/Ads, Supply, Control, Shop/Access and Service.
- Every public slash command has a canonical emoji button; parameterized actions launch guided input instead of requiring users to memorize command syntax.
- Added Back, Home and Cancel navigation across menus/FSM flows.
- Slash commands remain available as a compatibility/power-user interface, but normal operation is fully button-driven.
- Added an AST invariant test that fails when a new public command is introduced without a corresponding Telegram button/handler.

## 0.13.0 — ABC/XYZ, Demand Forecast & Replenishment Planning

- Added schema v9 with per-shop supply defaults, per-product replenishment overrides and auditable recommendation snapshots.
- Added `/supply [days]`, `/supply_refresh [days]`, `/supply_sku INTERNAL_SKU`, `/supply_settings`, `/supply_defaults` and `/supply_set`.
- Added role-aware `🚚 Поставка` button; read-only users can inspect cached plans, while analysts/owners can refresh inventory before recalculation.
- ABC classification is deliberately based on comparable `ordered_units`, not mixed marketplace revenue definitions.
- XYZ uses weekly variability only over complete operational-order days; missing API days are excluded rather than treated as zero demand.
- Demand forecast uses a transparent weighted moving average blended with the full observed mean; no opaque ML model is used.
- Reorder point accounts for configured lead time and safety-stock days; recommended quantity targets lead+safety+target cover and respects pack size/minimum order quantity.
- Unknown inventory never becomes zero stock: recommendations are suppressed until a successful inventory snapshot exists.
- Inventory snapshots older than two days are flagged as stale in supply reports.
- Physical-product planning works across linked WB/Ozon listings using `internal_sku`.
- Product supply settings are preserved when listings/products are merged.
- XLSX/CSV export now includes a `Supply` dataset.
- Manual `/backfill` was moved behind the same renewable distributed operation lease as scheduler jobs, closing a multi-process bypass found during hardening review.

## 0.12.0 — Production Hardening, Durable Retries & Health

- Added schema v8 with cross-process runtime leases, durable retry jobs and process heartbeats.
- Telegram long polling is protected by a short renewable singleton lease to prevent accidental duplicate pollers on the same persistent database.
- Every heavy shop collection operation now uses a local asyncio lock plus a renewable SQLite lease, preventing concurrent backfills/finance jobs across processes.
- Daily partial reports and delayed reconciliation/advertising failures are queued in a persistent retry queue instead of relying only on in-memory sleeps.
- Retry jobs survive restarts, use transactional claiming, exponential backoff, maximum attempts and `dead` status; dead jobs notify shop users.
- Added `/jobs` and `/job_retry ID` for operational visibility and owner-controlled retry.
- Added process heartbeat, hourly WAL checkpoint and daily SQLite `quick_check`.
- Added `/health` plus an optional dependency-free HTTP `/health` and `/ready` server.
- Added structured JSON logging with shop/user/job context and redaction of Telegram/WB/Ozon secrets.
- Startup migrations can run through `initialize_safely`: serialized migration file lock, automatic pre-migration online backup, `quick_check`, and rollback on migration failure.
- Restore now treats runtime leases and process heartbeats as ephemeral state so historical backups cannot resurrect stale process ownership.
- Added `scripts/preflight.py` for deployment checks without calling marketplace APIs.
- Bothost health server can use the platform `PORT` automatically when enabled.

## 0.11.0 — RBAC, Advertising Attribution & Management View

- Added schema migration v7 with `bot_users` and per-shop `user_shop_access` roles: `owner`, `analyst`, `viewer`.
- Shop selection is authorization-aware; knowing a shop ID is not enough to switch into it.
- Environment `TELEGRAM_OWNER_ID` users are bootstrapped as owners of every active shop for backward compatibility.
- Added `/users`, `/user_add`, `/user_remove`, `/my_access`; the last owner cannot be removed or demoted through Telegram.
- `viewer` is read-only and sees cached reports without triggering marketplace API requests; `analyst` can refresh analytics; `owner` additionally manages shops, settings, access and backup/restore.
- Main reply keyboard is role-aware and hides mutating owner/operator actions when the role does not permit them.
- Added durable campaign/SKU advertising tables and normalization for WB `fullstats` nmId details and Ozon CPC SKU statistics.
- Added `/ads [days]` and advertising report with campaign/SKU spend, attributed sales, orders, clicks, impressions, DRR and ROAS.
- SKU economics subtracts advertising only when that spend is explicitly attributed to the SKU by the source.
- Added `/management [days]`: transparent operational contribution estimate using order revenue, dated product cost, known marketplace expenses, attributed ad spend and compensation.
- The management estimate is deliberately not labelled accounting/net profit because operational orders, finance and advertising have different recognition lags.
- XLSX/CSV export now includes `Advertising` and `Management` datasets.
- Ozon Performance client supports the 2026 `POST /api/client/statistics/products/sku` endpoint; collection remains backward-compatible with older/injected clients without that method.


## 0.10.0 — Multi-shop, Export & Backup

- Added schema migration v6 with per-user shop selection, per-shop scheduler state and backup history.
- One Telegram owner can switch among multiple shops; multiple owners can keep different active shops concurrently.
- Marketplace secrets remain outside SQLite. Each shop references a credential profile such as `DEFAULT` or `SHOP2`.
- Additional credentials use `SELLERBOT_<PROFILE>_...` environment variables and can be activated without editing source code.
- Runtime registry builds independent API clients, job locks and collectors per shop; shops can be added or reconfigured without restarting the bot.
- Daily reports and alerts are scheduled independently per shop and include the shop name in automatic messages.
- Added `/shops`, `/shop`, `/shop_add`, `/shop_profile`, `/profiles`.
- Added `/export [days] [xlsx|csv]` with Summary, Daily, Products, Inventory, Finance, Reconciliation and Costs datasets.
- XLSX export is a multi-sheet workbook; CSV export is a ZIP containing one UTF-8 CSV per dataset.
- Added verified SQLite online backups, SHA-256 checksum, backup history, automatic daily backup and retention cleanup.
- Added guarded `/restore`: source integrity/schema validation, automatic pre-restore backup, migration after restore, rollback on failure and runtime reload.
- API secrets are never included in exports or database backups because they are stored only in hosting environment variables.

## 0.9.0 — Lifecycle Reconciliation

- Added durable `commerce_events` ledger and schema migration v5.
- WB lifecycle uses source identifiers: order/cancel from Orders, sale/return from Sales, finance rows from Finance v1; `srid` is used for exact matching when present.
- Ozon lifecycle stores FBO/FBS posting events and item-bound finance accrual events; `posting_number` is used for exact posting-to-finance matching.
- Ozon Analytics order totals remain aggregate-only because the analytics response does not provide a posting identifier; the bot does not fabricate one.
- Added `/reconcile [days]` and `🔎 Сверка` report with SKU funnel gaps and exact-ID coverage.
- Delayed finance events are included when they can be linked to an order/posting from the selected period.
- Repeated lifecycle loads are idempotent by stable event fingerprint.
- WB finance list and detailed endpoints now use independent 60-second rate-limit buckets.
- Daily delayed refresh now fills reconciliation sources after the fast morning report.
- Existing successful data remains intact when any reconciliation source fails.

## 0.8.0 — Seller Template & SKU Economics

- Telegram `/setup` wizard for shop name, timezone, report time and core alert thresholds.
- Per-shop persistent preferences in schema v4; environment variables remain bootstrap defaults.
- API secrets stay in hosting environment variables and are never stored in plaintext SQLite settings.
- CSV/XLSX cost import with audit batches, partial-error reporting and optional `effective_date`.
- Historical product cost table; past periods are no longer recalculated using today's cost.
- `internal_sku` mapping joins WB and Ozon listings into one physical product while preserving listing history.
- Manual `/link` command and `/import_costs` workflow.
- `/sku_finance` report: order economics, historical cost coverage and source-grounded SKU finance components.
- WB detailed finance rows and Ozon product-bound accrual components are stored at SKU level when available.
- No proportional fee allocation is invented when a marketplace does not provide trustworthy SKU attribution.
- Ozon/WB listing merge preserves existing product metrics and cost history.
- Added `openpyxl` for XLSX imports and an `examples/costs_template.csv` template.
- Schema migration v4 is backward-compatible with v3 and migrates legacy `products.cost_price` into cost history.

## 0.7.0 — Finance & Alerts

- Wildberries Finance API v1 sales-report summaries with daily storage.
- Ozon Finance Accrual API v1 with `last_id` pagination; no deprecated v3 transaction endpoints.
- Separate WB advertising statistics and optional Ozon Performance API client.
- Financial metrics are kept source-specific: finance sales, payable/net/payment, commission, logistics, storage, acceptance, penalties, services and advertising.
- Product cost entry with `/cost` and explicit COGS coverage estimate.
- `/finance` report with delayed-data warnings and DRR when attributed sales are available.
- Persistent alert engine: stock risk, order drop, API staleness and high DRR.
- Alert cooldown/state/event journal prevents repeating the same active subject every check.
- Scheduler refreshes delayed finance/ads after the operational morning report.
- Schema migration v3 for alert rules/state/events.
- Ozon Performance credentials are optional and separate from Seller API credentials.
- Financial values with different marketplace settlement semantics are not automatically combined.

## 0.6.0 — Product Analytics

- Product-level normalized metrics and SKU history.
- Top products, growth/decline report.
- WB FBW/FBS fulfillment split.
- Ozon current FBO/FBS posting endpoints for fulfillment backfill.
- WB current warehouse + seller-warehouse stock reports.
- Ozon v4 product stocks.
- Inventory snapshots and stock runway estimate.
- Product/inventory Telegram commands and buttons.
- Named HTTP rate-limit buckets.
- Ozon Analytics full offset pagination.
- Schema migration v2 for product metrics and inventory.

## 0.5.0 — Operational Bot

- Modular aiogram application, scheduler, daily/period reports and batch backfill.
- Comparable `ordered_units` model for WB + Ozon.
- Partial report/retry behavior.

## 0.3.0 — Durable Storage

- Multi-seller/multi-shop SQLite schema.
- Source-run journal and idempotent raw/metric storage.
- Successful data protected from later API failures.
