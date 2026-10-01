# Security model — Seller Analytics Bot

## Secrets

Marketplace and Telegram secrets are read only from environment variables. The Telegram wizard never asks a user to paste WB/Ozon tokens into chat, and SQLite does not store those secrets.

Treat `.env`, hosting environment variables and process logs as sensitive. Do not commit real tokens to a repository. Rotate a token immediately if it was ever pasted into Telegram, a ticket, a public repository or an untrusted log.

## Two administration levels

- **System owner** — Telegram IDs listed in `TELEGRAM_OWNER_ID`. Only these users can perform instance-wide operations: SQLite backup/restore, add shops, change credential profiles and list available credential profiles.
- **Shop owner** — an `owner` role granted inside a specific shop. It can manage that shop, its settings and staff, but it cannot download/restore the whole database or access credential-profile administration.

This distinction is required because one SQLite database can contain multiple shops.

## Backups

A database backup can contain commercial data for every shop in the instance. API secrets are not stored in it, but sales, products, costs, financial metrics, user access and history may be present. Backup files should therefore be protected like business-confidential data.

Restore is system-owner-only, creates a pre-restore safety copy, validates SQLite integrity, removes ephemeral runtime leases/heartbeats and installs the restored file atomically.

`AUTO_BACKUP_SEND_TELEGRAM=true` explicitly opts into daily delivery of the whole instance database to the IDs in `TELEGRAM_OWNER_ID`. It is disabled by default. Shop-only owners/viewers never receive this copy. Each system owner must start the bot to permit private delivery. Failed deliveries retry separately; files above 45 MiB require download from the hosting panel.

## Logs and exports

Known configured secrets are redacted from normal log messages and tracebacks. Logs should still be access-controlled because operational metadata can be commercially sensitive.

File logs rotate at 5 MiB with three retained older files. `.dockerignore` excludes environment secrets, live data, logs, database files and exports from the image build context.

XLSX/CSV exports neutralize leading `=`, `+`, `-` and `@` strings to prevent spreadsheet formula injection from marketplace/user-controlled names or SKU values.

## Runtime isolation

Heavy operations use renewable cross-process leases. Telegram polling also uses a singleton lease. Losing an operation lease cancels the in-flight task instead of allowing two instances to continue the same operation concurrently.

Retry jobs are shop-scoped and ownership-checked. Public HTTP health responses do not expose instance IDs, lease owners, heartbeats or retry internals.

## Reporting a problem

Before sharing diagnostics externally, remove database files, exports, backups, `.env`, tokens, marketplace raw payloads and logs unless the recipient is explicitly authorized to receive that data.
