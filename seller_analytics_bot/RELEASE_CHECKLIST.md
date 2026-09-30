# v1.0 release / deployment checklist

## Before deploy

1. Use Python 3.11+ and install `requirements.txt`.
2. Set `TELEGRAM_BOT_TOKEN` and at least one `TELEGRAM_OWNER_ID`.
3. Put marketplace credentials in environment variables only; never paste them into Telegram.
4. Use persistent storage for `DB_FILE` (for Bothost, `/app/data/seller_analytics.sqlite3`).
5. Run `python scripts/preflight.py`.
6. Run `python scripts/release_audit.py` on the source tree used for deployment.
7. Run the automated test suite in CI/development: `python -m pytest -q`.

## First start

1. Open the bot and use the button-based onboarding menu.
2. Run **🔌 Проверить подключения** and resolve every critical readiness blocker.
3. Run initial backfill.
4. Verify yesterday's report against the marketplace cabinets before relying on automation.
5. Import/enter cost prices if management-result reports are needed.

## Operational safety

- Keep exactly one persistent database shared by replicas that belong to the same bot instance. Runtime leases protect against accidental duplicate work, but SQLite is intended for one-host/small deployment, not a distributed database cluster.
- Do not manually edit the SQLite file while the bot is running.
- Keep automatic backups enabled and periodically test restore on a non-production copy.
- If `/health` reports an integrity failure, stop writes and restore/inspect the database instead of recreating sales data with zeros.
- A partial marketplace API response must remain partial; never replace missing data with synthetic zero.

## Upgrade

1. Make/verify a backup.
2. Deploy the new source without manually changing SQLite schema.
3. Startup migrations are serialized, backed up and integrity-checked automatically.
4. After upgrade, check **❤️ Health-check**, **✅ Готовность магазина**, and yesterday's report.

## Rollback

Code rollback is safe only if the older code supports the database schema already installed. Database restore is the authoritative rollback for incompatible schema changes. Keep the pre-upgrade backup until the new version has been verified in production.
