"""Transactional repositories and idempotent source-run persistence."""
from __future__ import annotations
import hashlib
import json
import sqlite3
from datetime import date, datetime, timezone, timedelta
from typing import Iterable, Any
from .database import Database
from .models import (Seller, Shop, MarketplaceConnection, MetricPoint, SourceRun,
                     Product, ProductListing, ProductMetricPoint, InventoryPoint,
                     ShopPreferences, CommerceEventPoint, AdCampaignPoint, AdProductPoint)
from app.services.metrics import definition


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def payload_hash(data: Any) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()

class Repository:
    def __init__(self, database: Database):
        self.db = database

    @staticmethod
    def _shop_from_row(r) -> Shop:
        keys=set(r.keys())
        return Shop(r["id"], r["seller_id"], r["name"], r["currency"], bool(r["active"]),
                    str(r["credential_profile"]) if "credential_profile" in keys else "DEFAULT")

    # --- tenants / shops -------------------------------------------------
    def ensure_seller(self, telegram_user_id: int, name: str = "Seller", timezone_name: str = "Europe/Moscow") -> Seller:
        now = utcnow()
        with self.db.connect() as c:
            c.execute("""INSERT INTO sellers(telegram_user_id,name,timezone,created_at)
                         VALUES(?,?,?,?) ON CONFLICT(telegram_user_id) DO NOTHING""",
                      (telegram_user_id, name, timezone_name, now))
            r = c.execute("SELECT * FROM sellers WHERE telegram_user_id=?", (telegram_user_id,)).fetchone()
        return Seller(r["id"], r["telegram_user_id"], r["name"], r["timezone"], bool(r["active"]))

    def ensure_shop(self, seller_id: int, name: str = "Основной магазин", currency: str = "RUB",
                    credential_profile: str = "DEFAULT") -> Shop:
        now = utcnow(); profile=(credential_profile or "DEFAULT").strip().upper()[:64]
        with self.db.connect() as c:
            c.execute("""INSERT INTO shops(seller_id,name,currency,created_at,credential_profile) VALUES(?,?,?,?,?)
                         ON CONFLICT(seller_id,name) DO NOTHING""", (seller_id, name, currency, now, profile))
            r = c.execute("SELECT * FROM shops WHERE seller_id=? AND name=?", (seller_id, name)).fetchone()
        return self._shop_from_row(r)

    def get_shop(self, shop_id: int) -> Shop | None:
        with self.db.connect() as c:
            r=c.execute("SELECT * FROM shops WHERE id=?",(shop_id,)).fetchone()
        return self._shop_from_row(r) if r else None

    def list_shops(self, seller_id: int | None = None, *, active_only: bool = True) -> list[Shop]:
        sql="SELECT * FROM shops WHERE 1=1"; params=[]
        if seller_id is not None:
            sql += " AND seller_id=?"; params.append(seller_id)
        if active_only:
            sql += " AND active=1"
        sql += " ORDER BY id"
        with self.db.connect() as c:
            rows=c.execute(sql,params).fetchall()
        return [self._shop_from_row(r) for r in rows]

    def set_shop_credential_profile(self, shop_id: int, profile: str) -> Shop:
        clean=(profile or "DEFAULT").strip().upper()[:64]
        if not clean.replace('_','').isalnum():
            raise ValueError('credential profile may contain only letters, numbers and underscore')
        with self.db.connect() as c:
            c.execute("UPDATE shops SET credential_profile=? WHERE id=?",(clean,shop_id))
            r=c.execute("SELECT * FROM shops WHERE id=?",(shop_id,)).fetchone()
        if not r: raise ValueError('shop not found')
        return self._shop_from_row(r)

    def selected_shop_for_user(self, telegram_user_id: int, fallback_shop_id: int | None = None) -> int | None:
        with self.db.connect() as c:
            r=c.execute("""SELECT us.shop_id FROM user_shop_selection us JOIN shops s ON s.id=us.shop_id
                             WHERE us.telegram_user_id=? AND s.active=1""",(telegram_user_id,)).fetchone()
            if r: return int(r['shop_id'])
        return fallback_shop_id

    def select_shop_for_user(self, telegram_user_id: int, shop_id: int) -> None:
        with self.db.connect() as c:
            r=c.execute("SELECT id FROM shops WHERE id=? AND active=1",(shop_id,)).fetchone()
            if not r: raise ValueError('shop not found or inactive')
            c.execute("""INSERT INTO user_shop_selection(telegram_user_id,shop_id,updated_at) VALUES(?,?,?)
                         ON CONFLICT(telegram_user_id) DO UPDATE SET shop_id=excluded.shop_id,updated_at=excluded.updated_at""",
                      (telegram_user_id,shop_id,utcnow()))


    # --- users / role-based access -------------------------------------
    def ensure_bot_user(self, telegram_user_id: int, display_name: str = '') -> None:
        now=utcnow(); clean=(display_name or '').strip()[:200]
        with self.db.connect() as c:
            c.execute("""INSERT INTO bot_users(telegram_user_id,display_name,active,created_at,updated_at)
                         VALUES(?,?,1,?,?)
                         ON CONFLICT(telegram_user_id) DO UPDATE SET
                         display_name=CASE WHEN excluded.display_name<>'' THEN excluded.display_name ELSE bot_users.display_name END,
                         active=1,updated_at=excluded.updated_at""",
                      (int(telegram_user_id),clean,now,now))

    def grant_shop_access(self, telegram_user_id: int, shop_id: int, role: str,
                          display_name: str = '') -> None:
        role=(role or '').strip().lower()
        if role not in {'owner','analyst','viewer'}: raise ValueError('role must be owner, analyst or viewer')
        self.ensure_bot_user(telegram_user_id,display_name)
        now=utcnow()
        with self.db.connect() as c:
            if not c.execute('SELECT 1 FROM shops WHERE id=? AND active=1',(shop_id,)).fetchone():
                raise ValueError('shop not found or inactive')
            c.execute("""INSERT INTO user_shop_access(telegram_user_id,shop_id,role,created_at,updated_at)
                         VALUES(?,?,?,?,?) ON CONFLICT(telegram_user_id,shop_id) DO UPDATE SET
                         role=excluded.role,updated_at=excluded.updated_at""",
                      (int(telegram_user_id),int(shop_id),role,now,now))

    def revoke_shop_access(self, telegram_user_id: int, shop_id: int) -> bool:
        with self.db.connect() as c:
            cur=c.execute('DELETE FROM user_shop_access WHERE telegram_user_id=? AND shop_id=?',
                          (int(telegram_user_id),int(shop_id)))
            selected=c.execute('SELECT shop_id FROM user_shop_selection WHERE telegram_user_id=?',(int(telegram_user_id),)).fetchone()
            if selected and int(selected['shop_id'])==int(shop_id):
                c.execute('DELETE FROM user_shop_selection WHERE telegram_user_id=?',(int(telegram_user_id),))
            return bool(cur.rowcount)

    def role_for_user(self, telegram_user_id: int, shop_id: int) -> str | None:
        with self.db.connect() as c:
            r=c.execute("""SELECT usa.role FROM user_shop_access usa JOIN bot_users bu
                           ON bu.telegram_user_id=usa.telegram_user_id
                           JOIN shops s ON s.id=usa.shop_id
                           WHERE usa.telegram_user_id=? AND usa.shop_id=? AND bu.active=1 AND s.active=1""",
                        (int(telegram_user_id),int(shop_id))).fetchone()
        return str(r['role']) if r else None

    def shops_for_user(self, telegram_user_id: int) -> list[dict[str,Any]]:
        with self.db.connect() as c:
            rows=c.execute("""SELECT s.*,usa.role FROM user_shop_access usa
                JOIN bot_users bu ON bu.telegram_user_id=usa.telegram_user_id
                JOIN shops s ON s.id=usa.shop_id
                WHERE usa.telegram_user_id=? AND bu.active=1 AND s.active=1
                ORDER BY s.id""",(int(telegram_user_id),)).fetchall()
        return [dict(r) for r in rows]

    def users_for_shop(self, shop_id: int, *, roles: Iterable[str] | None = None) -> list[dict[str,Any]]:
        params: list[Any]=[int(shop_id)]
        sql="""SELECT bu.telegram_user_id,bu.display_name,usa.role FROM user_shop_access usa
               JOIN bot_users bu ON bu.telegram_user_id=usa.telegram_user_id
               WHERE usa.shop_id=? AND bu.active=1"""
        if roles:
            clean=[r for r in roles if r in {'owner','analyst','viewer'}]
            if clean:
                sql += ' AND usa.role IN ('+','.join('?' for _ in clean)+')'; params.extend(clean)
        sql += " ORDER BY CASE usa.role WHEN 'owner' THEN 1 WHEN 'analyst' THEN 2 ELSE 3 END,bu.telegram_user_id"
        with self.db.connect() as c:
            rows=c.execute(sql,params).fetchall()
        return [dict(r) for r in rows]

    def can_user(self, telegram_user_id: int, shop_id: int, permission: str = 'view') -> bool:
        role=self.role_for_user(telegram_user_id,shop_id)
        levels={'viewer':10,'analyst':20,'owner':30}
        needed={'view':10,'operate':20,'manage':30}.get(permission)
        if needed is None: raise ValueError('unknown permission')
        return role is not None and levels.get(role,0) >= needed

    def select_authorized_shop_for_user(self, telegram_user_id: int, shop_id: int) -> None:
        if not self.can_user(telegram_user_id,shop_id,'view'):
            raise PermissionError('Нет доступа к этому магазину.')
        self.select_shop_for_user(telegram_user_id,shop_id)

    def selected_authorized_shop_for_user(self, telegram_user_id: int, fallback_shop_id: int | None = None) -> int | None:
        allowed=self.shops_for_user(telegram_user_id)
        if not allowed: return None
        allowed_ids={int(x['id']) for x in allowed}
        selected=self.selected_shop_for_user(telegram_user_id,None)
        if selected in allowed_ids: return selected
        if fallback_shop_id in allowed_ids: return fallback_shop_id
        return int(allowed[0]['id'])

    def get_job_state(self, shop_id: int, job_key: str) -> str | None:
        with self.db.connect() as c:
            r=c.execute("SELECT last_run_key FROM shop_job_state WHERE shop_id=? AND job_key=?",(shop_id,job_key)).fetchone()
        return str(r['last_run_key']) if r and r['last_run_key'] is not None else None

    def set_job_state(self, shop_id: int, job_key: str, run_key: str) -> None:
        with self.db.connect() as c:
            c.execute("""INSERT INTO shop_job_state(shop_id,job_key,last_run_key,updated_at) VALUES(?,?,?,?)
                         ON CONFLICT(shop_id,job_key) DO UPDATE SET last_run_key=excluded.last_run_key,updated_at=excluded.updated_at""",
                      (shop_id,job_key,run_key,utcnow()))

    def record_backup(self, kind: str, filename: str, checksum: str | None, size_bytes: int,
                      schema_version: int, status: str, message: str | None = None) -> int:
        if kind not in {'manual','automatic','pre_restore','restore'}: raise ValueError('invalid backup kind')
        if status not in {'success','failed'}: raise ValueError('invalid backup status')
        with self.db.connect() as c:
            cur=c.execute("""INSERT INTO backup_history(kind,filename,checksum,size_bytes,schema_version,status,message,created_at)
                             VALUES(?,?,?,?,?,?,?,?)""",(kind,filename,checksum,size_bytes,schema_version,status,message,utcnow()))
            return int(cur.lastrowid)

    def recent_backups(self, limit: int = 10) -> list[dict[str,Any]]:
        with self.db.connect() as c:
            rows=c.execute("SELECT * FROM backup_history ORDER BY created_at DESC,id DESC LIMIT ?",(limit,)).fetchall()
        return [dict(r) for r in rows]

    def ensure_connection(self, shop_id: int, marketplace: str, display_name: str | None = None,
                          external_account_id: str | None = None) -> MarketplaceConnection:
        if marketplace not in {"ozon", "wildberries"}:
            raise ValueError("marketplace must be 'ozon' or 'wildberries'")
        name = display_name or marketplace.title()
        now = utcnow()
        with self.db.connect() as c:
            c.execute("""INSERT INTO marketplace_connections
                         (shop_id,marketplace,display_name,external_account_id,created_at)
                         VALUES(?,?,?,?,?) ON CONFLICT(shop_id,marketplace,display_name) DO NOTHING""",
                      (shop_id, marketplace, name, external_account_id, now))
            r = c.execute("""SELECT * FROM marketplace_connections
                             WHERE shop_id=? AND marketplace=? AND display_name=?""",
                          (shop_id, marketplace, name)).fetchone()
        return MarketplaceConnection(r["id"], r["shop_id"], r["marketplace"], r["display_name"], r["external_account_id"], bool(r["enabled"]))

    def list_connections(self, shop_id: int) -> list[MarketplaceConnection]:
        with self.db.connect() as c:
            rows = c.execute("SELECT * FROM marketplace_connections WHERE shop_id=? ORDER BY id", (shop_id,)).fetchall()
        return [MarketplaceConnection(r["id"], r["shop_id"], r["marketplace"], r["display_name"], r["external_account_id"], bool(r["enabled"])) for r in rows]

    def rename_shop(self, shop_id: int, name: str) -> Shop:
        clean = (name or "").strip()[:120]
        if not clean:
            raise ValueError("shop name must not be empty")
        with self.db.connect() as c:
            c.execute("UPDATE shops SET name=? WHERE id=?", (clean, shop_id))
            r = c.execute("SELECT * FROM shops WHERE id=?", (shop_id,)).fetchone()
        if not r:
            raise ValueError("shop not found")
        return self._shop_from_row(r)

    @staticmethod
    def _preferences_from_row(r) -> ShopPreferences:
        return ShopPreferences(
            int(r["shop_id"]), str(r["timezone"]), str(r["report_time"]),
            int(r["product_report_days"]), int(r["stock_velocity_days"]),
            int(r["stock_risk_days"]), int(r["finance_lookback_days"]),
            bool(r["alerts_enabled"]), int(r["alerts_interval_minutes"]),
            float(r["alert_order_drop_pct"]), int(r["alert_order_lookback_days"]),
            float(r["alert_api_stale_hours"]), float(r["alert_drr_pct"]),
            int(r["alert_cooldown_minutes"]), bool(r["setup_completed"]),
            bool(r["demo_mode"]) if "demo_mode" in r.keys() else False,
            str(r["onboarding_version"]) if "onboarding_version" in r.keys() else "",
        )

    def ensure_shop_preferences(self, shop_id: int,
                                defaults: dict[str, Any] | None = None) -> ShopPreferences:
        d = defaults or {}
        now = utcnow()
        values = (
            shop_id, str(d.get("timezone", "Europe/Moscow")), str(d.get("report_time", "09:00")),
            int(d.get("product_report_days", 7)), int(d.get("stock_velocity_days", 14)),
            int(d.get("stock_risk_days", 14)), int(d.get("finance_lookback_days", 14)),
            int(bool(d.get("alerts_enabled", True))), int(d.get("alerts_interval_minutes", 60)),
            float(d.get("alert_order_drop_pct", 35)), int(d.get("alert_order_lookback_days", 7)),
            float(d.get("alert_api_stale_hours", 26)), float(d.get("alert_drr_pct", 25)),
            int(d.get("alert_cooldown_minutes", 1440)), now,
        )
        with self.db.connect() as c:
            c.execute("""INSERT INTO shop_preferences(
                shop_id,timezone,report_time,product_report_days,stock_velocity_days,stock_risk_days,
                finance_lookback_days,alerts_enabled,alerts_interval_minutes,alert_order_drop_pct,
                alert_order_lookback_days,alert_api_stale_hours,alert_drr_pct,alert_cooldown_minutes,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(shop_id) DO NOTHING""", values)
            r = c.execute("SELECT * FROM shop_preferences WHERE shop_id=?", (shop_id,)).fetchone()
        return self._preferences_from_row(r)

    def get_shop_preferences(self, shop_id: int) -> ShopPreferences | None:
        with self.db.connect() as c:
            r = c.execute("SELECT * FROM shop_preferences WHERE shop_id=?", (shop_id,)).fetchone()
        return self._preferences_from_row(r) if r else None

    def update_shop_preferences(self, shop_id: int, **changes: Any) -> ShopPreferences:
        allowed = {
            "timezone", "report_time", "product_report_days", "stock_velocity_days", "stock_risk_days",
            "finance_lookback_days", "alerts_enabled", "alerts_interval_minutes", "alert_order_drop_pct",
            "alert_order_lookback_days", "alert_api_stale_hours", "alert_drr_pct", "alert_cooldown_minutes",
            "setup_completed", "demo_mode", "onboarding_version",
        }
        bad = set(changes) - allowed
        if bad:
            raise ValueError(f"unsupported preference fields: {sorted(bad)}")
        if not changes:
            pref = self.get_shop_preferences(shop_id)
            if pref is None:
                raise ValueError("shop preferences not initialized")
            return pref
        normalized = dict(changes)
        for key in ("alerts_enabled", "setup_completed", "demo_mode"):
            if key in normalized:
                normalized[key] = int(bool(normalized[key]))
        assignments = ", ".join(f"{k}=?" for k in normalized)
        params = list(normalized.values()) + [utcnow(), shop_id]
        with self.db.connect() as c:
            c.execute(f"UPDATE shop_preferences SET {assignments}, updated_at=? WHERE shop_id=?", params)
            r = c.execute("SELECT * FROM shop_preferences WHERE shop_id=?", (shop_id,)).fetchone()
        if not r:
            raise ValueError("shop preferences not initialized")
        return self._preferences_from_row(r)

    def save_readiness_snapshot(self, shop_id: int, status: str, critical_ok: int, critical_total: int,
                                optional_ok: int, optional_total: int, details: Any) -> int:
        if status not in {"ready","partial","blocked"}:
            raise ValueError("invalid readiness status")
        with self.db.connect() as c:
            cur=c.execute("""INSERT INTO shop_readiness_snapshots
                (shop_id,checked_at,status,critical_ok,critical_total,optional_ok,optional_total,details_json)
                VALUES(?,?,?,?,?,?,?,?)""",
                (shop_id,utcnow(),status,int(critical_ok),int(critical_total),int(optional_ok),int(optional_total),canonical_json(details)))
            return int(cur.lastrowid)

    def latest_readiness_snapshot(self, shop_id: int) -> dict[str,Any] | None:
        with self.db.connect() as c:
            r=c.execute("SELECT * FROM shop_readiness_snapshots WHERE shop_id=? ORDER BY checked_at DESC,id DESC LIMIT 1",(shop_id,)).fetchone()
        return dict(r) if r else None

    # --- source runs ------------------------------------------------------
    def record_failure(self, connection_id: int, endpoint: str, data_date: str, error: str,
                       *, http_status: int | None = None, attempts: int = 1,
                       started_at: str | None = None) -> int:
        now = utcnow()
        with self.db.connect() as c:
            cur = c.execute("""INSERT INTO source_runs
                (connection_id,endpoint,data_date,status,started_at,finished_at,error,http_status,attempts,created_at)
                VALUES(?,?,?,'failed',?,?,?,?,?,?)""",
                (connection_id, endpoint, data_date, started_at or now, now, error[:2000], http_status, attempts, now))
            return int(cur.lastrowid)

    def record_success(self, connection_id: int, endpoint: str, data_date: str,
                       raw_payload: Any, metrics: Iterable[MetricPoint], *,
                       attempts: int = 1, started_at: str | None = None,
                       store_raw: bool = True, status: str = "success") -> int:
        if status not in {"success", "partial"}:
            raise ValueError("status must be success or partial")
        now = utcnow()
        body = canonical_json(raw_payload)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        points = list(metrics)
        for p in points:
            if p.connection_id != connection_id or p.data_date != data_date:
                raise ValueError("MetricPoint belongs to another connection/date")
            known = definition(p.metric_key)
            if p.unit != known.unit:
                raise ValueError(f"Unexpected unit for {p.metric_key}: {p.unit}; expected {known.unit}")

        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if status == "success":
                previous = c.execute("""SELECT id FROM source_runs WHERE connection_id=? AND endpoint=?
                    AND data_date=? AND status='success' AND payload_hash=?""",
                    (connection_id, endpoint, data_date, digest)).fetchone()
                if previous:
                    c.commit()
                    return int(previous["id"])
            cur = c.execute("""INSERT INTO source_runs
                (connection_id,endpoint,data_date,status,started_at,finished_at,http_status,attempts,payload_hash,created_at)
                VALUES(?,?,?,?,?,?,200,?,?,?)""",
                (connection_id, endpoint, data_date, status, started_at or now, now, attempts, digest, now))
            run_id = int(cur.lastrowid)
            if store_raw:
                c.execute("INSERT INTO raw_payloads(source_run_id,payload_json,created_at) VALUES(?,?,?)", (run_id, body, now))
            for p in points:
                c.execute("""INSERT INTO metric_values
                    (connection_id,data_date,metric_key,value,unit,is_preliminary,as_of,fetched_at,source_run_id)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (p.connection_id, p.data_date, p.metric_key, p.value, p.unit,
                     int(p.is_preliminary), p.as_of, now, run_id))
            c.commit()
            return run_id

    def latest_metric(self, connection_id: int, data_date: str, metric_key: str):
        """Latest metric from a successful/partial run; failed runs never shadow it."""
        with self.db.connect() as c:
            return c.execute("""SELECT mv.*, sr.status, sr.finished_at
                FROM metric_values mv JOIN source_runs sr ON sr.id=mv.source_run_id
                WHERE mv.connection_id=? AND mv.data_date=? AND mv.metric_key=?
                  AND sr.status IN ('success','partial')
                ORDER BY mv.fetched_at DESC, mv.id DESC LIMIT 1""",
                (connection_id, data_date, metric_key)).fetchone()

    def metrics_for_day(self, connection_id: int, data_date: str) -> dict[str, float]:
        with self.db.connect() as c:
            rows = c.execute("""SELECT mv.* FROM metric_values mv
                JOIN source_runs sr ON sr.id=mv.source_run_id
                WHERE mv.connection_id=? AND mv.data_date=? AND sr.status IN ('success','partial')
                ORDER BY mv.metric_key, mv.fetched_at DESC, mv.id DESC""", (connection_id, data_date)).fetchall()
        result: dict[str, float] = {}
        for r in rows:
            result.setdefault(r["metric_key"], float(r["value"]))
        return result

    def last_successful_run(self, connection_id: int, endpoint: str | None = None) -> SourceRun | None:
        sql = "SELECT * FROM source_runs WHERE connection_id=? AND status IN ('success','partial')"
        params: list[Any] = [connection_id]
        if endpoint:
            sql += " AND endpoint=?"; params.append(endpoint)
        sql += " ORDER BY finished_at DESC, id DESC LIMIT 1"
        with self.db.connect() as c:
            r = c.execute(sql, params).fetchone()
        if not r: return None
        return SourceRun(r["id"], r["connection_id"], r["endpoint"], r["data_date"], r["status"], r["started_at"], r["finished_at"], r["error"], r["http_status"], r["attempts"], r["payload_hash"])

    # --- products / listings ----------------------------------------------
    def ensure_product(self, shop_id: int, internal_sku: str, name: str, cost_price: float | None = None) -> Product:
        now = utcnow()
        clean_name = (name or internal_sku).strip()[:500]
        with self.db.connect() as c:
            c.execute("""INSERT INTO products(shop_id,internal_sku,name,cost_price,created_at)
                         VALUES(?,?,?,?,?) ON CONFLICT(shop_id,internal_sku) DO UPDATE SET
                         name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE products.name END""",
                      (shop_id, internal_sku, clean_name, cost_price, now))
            r=c.execute("SELECT * FROM products WHERE shop_id=? AND internal_sku=?", (shop_id,internal_sku)).fetchone()
            if cost_price is not None:
                c.execute("""INSERT INTO product_cost_history(product_id,effective_date,cost_price,source,created_at)
                             VALUES(?,'1970-01-01',?,'initial',?)
                             ON CONFLICT(product_id,effective_date) DO UPDATE SET cost_price=excluded.cost_price""",
                          (int(r['id']), float(cost_price), now))
                c.execute("UPDATE products SET cost_price=? WHERE id=?", (float(cost_price), int(r['id'])))
                r=c.execute("SELECT * FROM products WHERE id=?", (int(r['id']),)).fetchone()
        return Product(r['id'],r['shop_id'],r['internal_sku'],r['name'],r['cost_price'],bool(r['active']))

    def ensure_listing(self, product_id: int, connection_id: int, marketplace_sku: str,
                       offer_id: str | None = None) -> ProductListing:
        now=utcnow(); sku=str(marketplace_sku)
        with self.db.connect() as c:
            c.execute("""INSERT INTO product_listings(product_id,connection_id,marketplace_sku,offer_id,created_at)
                         VALUES(?,?,?,?,?) ON CONFLICT(connection_id,marketplace_sku) DO UPDATE SET
                         offer_id=COALESCE(excluded.offer_id,product_listings.offer_id)""",
                      (product_id,connection_id,sku,offer_id,now))
            r=c.execute("SELECT * FROM product_listings WHERE connection_id=? AND marketplace_sku=?",
                        (connection_id,sku)).fetchone()
        return ProductListing(r['id'],r['product_id'],r['connection_id'],r['marketplace_sku'],r['offer_id'])

    def listing_by_marketplace_sku(self, connection_id: int, marketplace_sku: str) -> ProductListing | None:
        with self.db.connect() as c:
            r=c.execute("SELECT * FROM product_listings WHERE connection_id=? AND marketplace_sku=?",
                        (connection_id,str(marketplace_sku))).fetchone()
        if not r: return None
        return ProductListing(r['id'],r['product_id'],r['connection_id'],r['marketplace_sku'],r['offer_id'])

    def listing_for_shop(self, shop_id: int, marketplace: str, marketplace_sku: str) -> dict[str, Any] | None:
        with self.db.connect() as c:
            r=c.execute("""SELECT pl.*,p.internal_sku,p.name,p.cost_price,mc.marketplace
                FROM product_listings pl JOIN products p ON p.id=pl.product_id
                JOIN marketplace_connections mc ON mc.id=pl.connection_id
                WHERE p.shop_id=? AND mc.marketplace=? AND pl.marketplace_sku=?
                ORDER BY pl.id LIMIT 1""", (shop_id,marketplace,str(marketplace_sku))).fetchone()
        return dict(r) if r else None

    def link_marketplace_listings(self, shop_id: int, internal_sku: str,
                                  refs: Iterable[tuple[str, str]], *,
                                  name: str | None = None) -> Product:
        """Attach existing WB/Ozon listings to one physical product.

        Product/listing metric history is preserved because metrics reference listing_id.
        Cost history of merged source products is copied to the canonical product.
        """
        clean_sku=(internal_sku or '').strip()
        if not clean_sku:
            raise ValueError('internal_sku must not be empty')
        refs=list(refs)
        if not refs:
            raise ValueError('at least one listing is required')
        resolved=[]
        for market,sku in refs:
            market={'wb':'wildberries','wildberries':'wildberries','ozon':'ozon'}.get(str(market).lower())
            if market is None:
                raise ValueError(f'unsupported marketplace: {market}')
            row=self.listing_for_shop(shop_id,market,str(sku))
            if not row:
                raise ValueError(f'listing not found: {market}:{sku}')
            resolved.append(row)

        canonical=self.ensure_product(shop_id,clean_sku,name or resolved[0]['name'])
        now=utcnow()
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            source_product_ids={int(r['product_id']) for r in resolved if int(r['product_id']) != canonical.id}
            for source_id in source_product_ids:
                histories=c.execute("""SELECT effective_date,cost_price,source,import_batch_id,created_at
                    FROM product_cost_history WHERE product_id=? ORDER BY effective_date""",(source_id,)).fetchall()
                for h in histories:
                    c.execute("""INSERT INTO product_cost_history
                        (product_id,effective_date,cost_price,source,import_batch_id,created_at)
                        VALUES(?,?,?,?,?,?) ON CONFLICT(product_id,effective_date) DO NOTHING""",
                        (canonical.id,h['effective_date'],h['cost_price'],h['source'],h['import_batch_id'],h['created_at']))
                # Preserve replenishment overrides when products are merged. Existing
                # canonical settings win; otherwise copy the source product settings.
                try:
                    ps=c.execute('SELECT * FROM product_supply_settings WHERE product_id=?',(source_id,)).fetchone()
                    if ps:
                        c.execute("""INSERT INTO product_supply_settings(product_id,lead_time_days,safety_stock_days,target_stock_days,pack_size,min_order_qty,updated_at)
                            VALUES(?,?,?,?,?,?,?) ON CONFLICT(product_id) DO NOTHING""",
                            (canonical.id,ps['lead_time_days'],ps['safety_stock_days'],ps['target_stock_days'],ps['pack_size'],ps['min_order_qty'],ps['updated_at']))
                except sqlite3.OperationalError:
                    pass
                c.execute('UPDATE product_listings SET product_id=? WHERE product_id=?',(canonical.id,source_id))
                c.execute("UPDATE products SET active=0 WHERE id=? AND NOT EXISTS(SELECT 1 FROM product_listings WHERE product_id=?)",
                          (source_id,source_id))
            for r in resolved:
                c.execute('UPDATE product_listings SET product_id=? WHERE id=?',(canonical.id,int(r['id'])))
            if name:
                c.execute('UPDATE products SET name=? WHERE id=?',((name or '')[:500],canonical.id))
            latest=c.execute("""SELECT cost_price FROM product_cost_history WHERE product_id=?
                                ORDER BY effective_date DESC,id DESC LIMIT 1""",(canonical.id,)).fetchone()
            if latest:
                c.execute('UPDATE products SET cost_price=? WHERE id=?',(latest['cost_price'],canonical.id))
            c.commit()
            r=c.execute('SELECT * FROM products WHERE id=?',(canonical.id,)).fetchone()
        return Product(r['id'],r['shop_id'],r['internal_sku'],r['name'],r['cost_price'],bool(r['active']))

    def save_product_metrics(self, source_run_id: int, points: Iterable[ProductMetricPoint]) -> int:
        now=utcnow(); rows=list(points)
        if not rows: return 0
        with self.db.connect() as c:
            run=c.execute("SELECT connection_id,status FROM source_runs WHERE id=?",(source_run_id,)).fetchone()
            if not run or run['status'] not in ('success','partial'):
                raise ValueError('Product metrics require a successful/partial source run')
            c.execute('BEGIN IMMEDIATE')
            saved=0
            for p in rows:
                listing=c.execute("SELECT connection_id FROM product_listings WHERE id=?",(p.listing_id,)).fetchone()
                if not listing or int(listing['connection_id']) != int(run['connection_id']):
                    raise ValueError('ProductMetricPoint belongs to another connection')
                known=definition(p.metric_key)
                if p.unit != known.unit:
                    raise ValueError(f'Unexpected unit for {p.metric_key}: {p.unit}; expected {known.unit}')
                cur=c.execute("""INSERT OR IGNORE INTO product_metric_values
                    (listing_id,data_date,metric_key,value,unit,fulfillment_scheme,is_preliminary,as_of,fetched_at,source_run_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (p.listing_id,p.data_date,p.metric_key,p.value,p.unit,p.fulfillment_scheme,
                     int(p.is_preliminary),p.as_of,now,source_run_id))
                saved += cur.rowcount
            c.commit()
        return saved

    def save_inventory(self, source_run_id: int, points: Iterable[InventoryPoint]) -> int:
        now=utcnow(); rows=list(points)
        if not rows: return 0
        with self.db.connect() as c:
            run=c.execute("SELECT connection_id,status FROM source_runs WHERE id=?",(source_run_id,)).fetchone()
            if not run or run['status'] not in ('success','partial'):
                raise ValueError('Inventory requires a successful/partial source run')
            c.execute('BEGIN IMMEDIATE'); saved=0
            for p in rows:
                listing=c.execute("SELECT connection_id FROM product_listings WHERE id=?",(p.listing_id,)).fetchone()
                if not listing or int(listing['connection_id']) != int(run['connection_id']):
                    raise ValueError('InventoryPoint belongs to another connection')
                cur=c.execute("""INSERT OR IGNORE INTO inventory_snapshots
                    (listing_id,captured_at,available_units,reserved_units,fulfillment_scheme,warehouse_name,as_of,source_run_id)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (p.listing_id,now,p.available_units,p.reserved_units,p.fulfillment_scheme,
                     p.warehouse_name,p.as_of,source_run_id))
                saved += cur.rowcount
            c.commit()
        return saved

    def product_metric_series(self, listing_id: int, start_date: str, end_date: str,
                              metric_key: str, fulfillment_scheme: str = 'ALL') -> dict[str,float]:
        with self.db.connect() as c:
            rows=c.execute("""SELECT pm.data_date,pm.value,pm.fetched_at,pm.id
                FROM product_metric_values pm JOIN source_runs sr ON sr.id=pm.source_run_id
                WHERE pm.listing_id=? AND pm.data_date BETWEEN ? AND ? AND pm.metric_key=?
                  AND pm.fulfillment_scheme=? AND sr.status IN ('success','partial')
                ORDER BY pm.data_date ASC,pm.fetched_at DESC,pm.id DESC""",
                (listing_id,start_date,end_date,metric_key,fulfillment_scheme)).fetchall()
        out={}
        for r in rows: out.setdefault(r['data_date'],float(r['value']))
        return out

    def product_period_totals(self, shop_id: int, start_date: str, end_date: str,
                              metric_key: str = 'ordered_units', fulfillment_scheme: str = 'ALL') -> list[dict[str,Any]]:
        """Latest value per listing/day, summed for the requested period."""
        with self.db.connect() as c:
            rows=c.execute("""WITH ranked AS (
                SELECT pm.*, ROW_NUMBER() OVER (
                  PARTITION BY pm.listing_id,pm.data_date,pm.metric_key,pm.fulfillment_scheme
                  ORDER BY pm.fetched_at DESC,pm.id DESC) rn
                FROM product_metric_values pm
                JOIN source_runs sr ON sr.id=pm.source_run_id
                JOIN product_listings pl ON pl.id=pm.listing_id
                JOIN products p ON p.id=pl.product_id
                WHERE p.shop_id=? AND pm.data_date BETWEEN ? AND ? AND pm.metric_key=?
                  AND pm.fulfillment_scheme=? AND sr.status IN ('success','partial'))
              SELECT p.id product_id,p.internal_sku,p.name,pl.id listing_id,pl.marketplace_sku,pl.offer_id,
                     mc.marketplace,SUM(r.value) value
              FROM ranked r JOIN product_listings pl ON pl.id=r.listing_id
              JOIN products p ON p.id=pl.product_id
              JOIN marketplace_connections mc ON mc.id=pl.connection_id
              WHERE r.rn=1 GROUP BY pl.id ORDER BY value DESC""",
              (shop_id,start_date,end_date,metric_key,fulfillment_scheme)).fetchall()
        return [dict(r) for r in rows]

    def fulfillment_totals(self, shop_id: int, start_date: str, end_date: str) -> list[dict[str,Any]]:
        with self.db.connect() as c:
            rows=c.execute("""WITH ranked AS (
                SELECT pm.*, ROW_NUMBER() OVER (
                  PARTITION BY pm.listing_id,pm.data_date,pm.metric_key,pm.fulfillment_scheme
                  ORDER BY pm.fetched_at DESC,pm.id DESC) rn
                FROM product_metric_values pm JOIN source_runs sr ON sr.id=pm.source_run_id
                JOIN product_listings pl ON pl.id=pm.listing_id JOIN products p ON p.id=pl.product_id
                WHERE p.shop_id=? AND pm.data_date BETWEEN ? AND ? AND pm.metric_key='fulfillment_units'
                  AND sr.status IN ('success','partial'))
              SELECT mc.marketplace,r.fulfillment_scheme,SUM(r.value) value
              FROM ranked r JOIN product_listings pl ON pl.id=r.listing_id
              JOIN marketplace_connections mc ON mc.id=pl.connection_id
              WHERE r.rn=1 GROUP BY mc.marketplace,r.fulfillment_scheme ORDER BY mc.marketplace,r.fulfillment_scheme""",
              (shop_id,start_date,end_date)).fetchall()
        return [dict(r) for r in rows]

    def latest_inventory_by_listing(self, shop_id: int) -> list[dict[str,Any]]:
        """Aggregate newest snapshot per listing+scheme without dropping another scheme."""
        with self.db.connect() as c:
            rows=c.execute("""WITH latest_run AS (
                SELECT i.listing_id,i.fulfillment_scheme,MAX(i.source_run_id) source_run_id
                FROM inventory_snapshots i JOIN product_listings pl ON pl.id=i.listing_id
                JOIN products p ON p.id=pl.product_id WHERE p.shop_id=?
                GROUP BY i.listing_id,i.fulfillment_scheme)
              SELECT p.id product_id,p.internal_sku,p.name,pl.id listing_id,pl.marketplace_sku,pl.offer_id,
                     pl.connection_id,mc.marketplace,
                     SUM(i.available_units) available_units,SUM(i.reserved_units) reserved_units,
                     MAX(i.captured_at) captured_at
              FROM latest_run lr JOIN inventory_snapshots i ON i.listing_id=lr.listing_id
                AND i.fulfillment_scheme=lr.fulfillment_scheme AND i.source_run_id=lr.source_run_id
              JOIN product_listings pl ON pl.id=i.listing_id JOIN products p ON p.id=pl.product_id
              JOIN marketplace_connections mc ON mc.id=pl.connection_id
              GROUP BY pl.id ORDER BY available_units ASC""",(shop_id,)).fetchall()
        return [dict(r) for r in rows]

    def latest_inventory_by_scheme(self, shop_id: int) -> list[dict[str,Any]]:
        """Newest inventory run per listing+scheme, aggregated over its warehouses."""
        with self.db.connect() as c:
            rows=c.execute("""WITH latest_run AS (
                SELECT i.listing_id,i.fulfillment_scheme,MAX(i.source_run_id) source_run_id
                FROM inventory_snapshots i JOIN product_listings pl ON pl.id=i.listing_id
                JOIN products p ON p.id=pl.product_id WHERE p.shop_id=?
                GROUP BY i.listing_id,i.fulfillment_scheme)
              SELECT p.id product_id,p.internal_sku,p.name,pl.id listing_id,pl.marketplace_sku,pl.offer_id,
                     pl.connection_id,mc.marketplace,i.fulfillment_scheme,
                     SUM(i.available_units) available_units,SUM(i.reserved_units) reserved_units,
                     MAX(i.captured_at) captured_at
              FROM latest_run lr JOIN inventory_snapshots i ON i.listing_id=lr.listing_id
                AND i.fulfillment_scheme=lr.fulfillment_scheme AND i.source_run_id=lr.source_run_id
              JOIN product_listings pl ON pl.id=i.listing_id JOIN products p ON p.id=pl.product_id
              JOIN marketplace_connections mc ON mc.id=pl.connection_id
              GROUP BY pl.id,i.fulfillment_scheme ORDER BY mc.marketplace,pl.id,i.fulfillment_scheme""",
              (shop_id,)).fetchall()
        return [dict(r) for r in rows]

    def successful_order_dates(self, connection_id: int, start_date: str, end_date: str) -> list[str]:
        """Dates for which a complete operational-order source run succeeded.

        Empty product rows on one of these dates can therefore be interpreted as
        zero orders for that SKU, rather than missing collection.
        """
        with self.db.connect() as c:
            rows=c.execute("""SELECT DISTINCT data_date FROM source_runs
                WHERE connection_id=? AND data_date BETWEEN ? AND ?
                  AND status IN ('success','partial')
                  AND (endpoint='statistics/orders' OR endpoint='statistics/orders/backfill'
                       OR endpoint='analytics/orders' OR endpoint='analytics/orders/backfill')
                ORDER BY data_date""",(connection_id,start_date,end_date)).fetchall()
        return [str(r['data_date']) for r in rows]

    def count(self, table: str) -> int:
        allowed = {"sellers","shops","marketplace_connections","source_runs","raw_payloads","metric_values",
                   "products","product_listings","product_metric_values","inventory_snapshots","alert_rules",
                   "alert_state","alert_events","shop_preferences","import_batches","product_cost_history",
                   "commerce_events","user_shop_selection","shop_job_state","backup_history","bot_users","user_shop_access",
                   "ad_campaign_daily","ad_product_daily","runtime_leases","retry_jobs","process_heartbeats",
                   "shop_supply_preferences","product_supply_settings","supply_recommendation_snapshots",
                   "inbound_shipments","inbound_shipment_items","forecast_quality_snapshots","supply_calibration_snapshots","promotions","promotion_products"}
        if table not in allowed: raise ValueError("unsupported table")
        with self.db.connect() as c:
            return int(c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def latest_run(self, connection_id: int, endpoint: str | None = None, data_date: str | None = None) -> SourceRun | None:
        sql = "SELECT * FROM source_runs WHERE connection_id=?"
        params: list[Any] = [connection_id]
        if endpoint:
            sql += " AND endpoint=?"; params.append(endpoint)
        if data_date:
            sql += " AND data_date=?"; params.append(data_date)
        sql += " ORDER BY COALESCE(finished_at,started_at) DESC, id DESC LIMIT 1"
        with self.db.connect() as c:
            r = c.execute(sql, params).fetchone()
        if not r: return None
        return SourceRun(r["id"], r["connection_id"], r["endpoint"], r["data_date"], r["status"], r["started_at"], r["finished_at"], r["error"], r["http_status"], r["attempts"], r["payload_hash"])

    def metric_series(self, connection_id: int, start_date: str, end_date: str, metric_key: str) -> dict[str, float]:
        with self.db.connect() as c:
            rows = c.execute("""SELECT mv.data_date,mv.value,mv.fetched_at,mv.id
                FROM metric_values mv JOIN source_runs sr ON sr.id=mv.source_run_id
                WHERE mv.connection_id=? AND mv.data_date BETWEEN ? AND ? AND mv.metric_key=?
                  AND sr.status IN ('success','partial')
                ORDER BY mv.data_date ASC, mv.fetched_at DESC, mv.id DESC""",
                (connection_id,start_date,end_date,metric_key)).fetchall()
        result: dict[str,float] = {}
        for r in rows:
            result.setdefault(r['data_date'], float(r['value']))
        return result

    def recent_metric_days(self, shop_id: int, metric_key: str = 'ordered_units', limit: int = 10) -> list[tuple[str,int,int]]:
        """Return (date, sources_with_metric, enabled_sources) newest first."""
        conns=self.list_connections(shop_id)
        enabled=[c for c in conns if c.enabled]
        if not enabled: return []
        ids=[c.id for c in enabled]
        placeholders=','.join('?' for _ in ids)
        with self.db.connect() as c:
            rows=c.execute(f"""SELECT mv.data_date, COUNT(DISTINCT mv.connection_id) AS source_count
                FROM metric_values mv JOIN source_runs sr ON sr.id=mv.source_run_id
                WHERE mv.connection_id IN ({placeholders}) AND mv.metric_key=?
                  AND sr.status IN ('success','partial')
                GROUP BY mv.data_date ORDER BY mv.data_date DESC LIMIT ?""", (*ids,metric_key,limit)).fetchall()
        return [(r['data_date'],int(r['source_count']),len(enabled)) for r in rows]

    # --- finance / costs -------------------------------------------------
    def set_product_cost(self, product_id: int, cost_price: float, *, effective_date: str,
                         source: str = 'manual', import_batch_id: int | None = None) -> bool:
        if cost_price < 0: raise ValueError('cost_price must be >= 0')
        # ISO date validation without importing app-level timezone rules.
        try: datetime.fromisoformat(effective_date)
        except ValueError as exc: raise ValueError('effective_date must be YYYY-MM-DD') from exc
        day=effective_date[:10]; now=utcnow()
        with self.db.connect() as c:
            row=c.execute('SELECT id FROM products WHERE id=?',(product_id,)).fetchone()
            if not row: return False
            c.execute("""INSERT INTO product_cost_history(product_id,effective_date,cost_price,source,import_batch_id,created_at)
                         VALUES(?,?,?,?,?,?) ON CONFLICT(product_id,effective_date) DO UPDATE SET
                         cost_price=excluded.cost_price,source=excluded.source,import_batch_id=excluded.import_batch_id,
                         created_at=excluded.created_at""",
                      (product_id,day,float(cost_price),source,import_batch_id,now))
            latest=c.execute("""SELECT cost_price FROM product_cost_history WHERE product_id=?
                                ORDER BY effective_date DESC,id DESC LIMIT 1""",(product_id,)).fetchone()
            if latest:
                c.execute('UPDATE products SET cost_price=? WHERE id=?',(latest['cost_price'],product_id))
        return True

    def set_product_cost_by_listing(self, connection_id: int, marketplace_sku: str, cost_price: float,
                                    *, effective_date: str | None = None, source: str = 'manual',
                                    import_batch_id: int | None = None) -> bool:
        with self.db.connect() as c:
            row=c.execute('SELECT product_id FROM product_listings WHERE connection_id=? AND marketplace_sku=?',
                          (connection_id,str(marketplace_sku))).fetchone()
        if not row: return False
        day=effective_date or datetime.now(timezone.utc).date().isoformat()
        return self.set_product_cost(int(row['product_id']),cost_price,effective_date=day,
                                     source=source,import_batch_id=import_batch_id)

    def product_cost_history(self, product_id: int) -> list[dict[str,Any]]:
        with self.db.connect() as c:
            rows=c.execute("""SELECT * FROM product_cost_history WHERE product_id=?
                              ORDER BY effective_date DESC,id DESC""",(product_id,)).fetchall()
        return [dict(r) for r in rows]

    def record_import_batch(self, shop_id: int, import_kind: str, filename: str | None, *,
                            status: str, total_rows: int, applied_rows: int,
                            errors: list[dict[str,Any]]) -> int:
        if status not in {'success','partial','failed'}:
            raise ValueError('unsupported import status')
        now=utcnow()
        with self.db.connect() as c:
            cur=c.execute("""INSERT INTO import_batches(shop_id,import_kind,filename,status,total_rows,
                applied_rows,error_rows,error_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (shop_id,import_kind,filename,status,total_rows,applied_rows,len(errors),
                 canonical_json(errors[:200]),now))
            return int(cur.lastrowid)

    def create_import_batch(self, shop_id: int, import_kind: str, filename: str | None) -> int:
        return self.record_import_batch(shop_id,import_kind,filename,status='partial',
                                        total_rows=0,applied_rows=0,errors=[])

    def finish_import_batch(self, batch_id: int, *, status: str, total_rows: int,
                            applied_rows: int, errors: list[dict[str,Any]]) -> None:
        if status not in {'success','partial','failed'}:
            raise ValueError('unsupported import status')
        with self.db.connect() as c:
            c.execute("""UPDATE import_batches SET status=?,total_rows=?,applied_rows=?,error_rows=?,error_json=?
                         WHERE id=?""",(status,total_rows,applied_rows,len(errors),canonical_json(errors[:200]),batch_id))

    def recent_import_batches(self, shop_id: int, limit: int = 10) -> list[dict[str,Any]]:
        with self.db.connect() as c:
            rows=c.execute("""SELECT * FROM import_batches WHERE shop_id=?
                              ORDER BY created_at DESC,id DESC LIMIT ?""",(shop_id,limit)).fetchall()
        return [dict(r) for r in rows]

    def products_without_cost(self, shop_id: int, limit: int = 20) -> list[dict[str,Any]]:
        with self.db.connect() as c:
            rows=c.execute('''SELECT DISTINCT p.id,p.internal_sku,p.name,mc.marketplace,pl.marketplace_sku
                FROM products p JOIN product_listings pl ON pl.product_id=p.id
                JOIN marketplace_connections mc ON mc.id=pl.connection_id
                WHERE p.shop_id=? AND p.active=1 AND p.cost_price IS NULL ORDER BY p.id LIMIT ?''',(shop_id,limit)).fetchall()
        return [dict(r) for r in rows]

    def financial_metric_totals(self, shop_id: int, start_date: str, end_date: str) -> dict[str,dict[str,float]]:
        with self.db.connect() as c:
            rows=c.execute('''WITH ranked AS (
                SELECT mv.*,mc.marketplace,ROW_NUMBER() OVER(
                  PARTITION BY mv.connection_id,mv.data_date,mv.metric_key ORDER BY mv.fetched_at DESC,mv.id DESC) rn
                FROM metric_values mv JOIN source_runs sr ON sr.id=mv.source_run_id
                JOIN marketplace_connections mc ON mc.id=mv.connection_id
                WHERE mc.shop_id=? AND mv.data_date BETWEEN ? AND ? AND sr.status IN ('success','partial'))
              SELECT marketplace,metric_key,SUM(value) value FROM ranked WHERE rn=1
              GROUP BY marketplace,metric_key''',(shop_id,start_date,end_date)).fetchall()
        out: dict[str,dict[str,float]]={}
        for r in rows: out.setdefault(r['marketplace'],{})[r['metric_key']]=float(r['value'])
        return out

    def estimated_order_cogs(self, shop_id: int, start_date: str, end_date: str) -> dict[str,dict[str,float]]:
        """Cost estimate based on ordered units and cost effective on each order date.

        This remains an estimate of ordered-goods cost, not realized accounting COGS.
        """
        with self.db.connect() as c:
            rows=c.execute('''WITH ranked AS (
                SELECT pm.*,ROW_NUMBER() OVER(PARTITION BY pm.listing_id,pm.data_date,pm.metric_key,pm.fulfillment_scheme
                  ORDER BY pm.fetched_at DESC,pm.id DESC) rn
                FROM product_metric_values pm JOIN source_runs sr ON sr.id=pm.source_run_id
                JOIN product_listings pl ON pl.id=pm.listing_id JOIN products p ON p.id=pl.product_id
                WHERE p.shop_id=? AND pm.data_date BETWEEN ? AND ? AND pm.metric_key='ordered_units'
                  AND pm.fulfillment_scheme='ALL' AND sr.status IN ('success','partial')),
              costed AS (
                SELECT r.*,pl.product_id,mc.marketplace,
                  (SELECT h.cost_price FROM product_cost_history h
                   WHERE h.product_id=pl.product_id AND h.effective_date<=r.data_date
                   ORDER BY h.effective_date DESC,h.id DESC LIMIT 1) effective_cost
                FROM ranked r JOIN product_listings pl ON pl.id=r.listing_id
                JOIN marketplace_connections mc ON mc.id=pl.connection_id WHERE r.rn=1)
              SELECT marketplace,SUM(value) units,
                     SUM(CASE WHEN effective_cost IS NOT NULL THEN value ELSE 0 END) covered_units,
                     SUM(CASE WHEN effective_cost IS NOT NULL THEN value*effective_cost ELSE 0 END) estimated_cost
              FROM costed GROUP BY marketplace''',(shop_id,start_date,end_date)).fetchall()
        return {r['marketplace']:{'units':float(r['units'] or 0),'covered_units':float(r['covered_units'] or 0),
                                  'estimated_cost':float(r['estimated_cost'] or 0)} for r in rows}

    def sku_economics(self, shop_id: int, start_date: str, end_date: str) -> list[dict[str,Any]]:
        """Order-level SKU economics using marketplace order amount and historical cost.

        The result intentionally does not allocate marketplace fees or advertising to SKU.
        """
        with self.db.connect() as c:
            rows=c.execute('''WITH ranked AS (
                SELECT pm.*,ROW_NUMBER() OVER(PARTITION BY pm.listing_id,pm.data_date,pm.metric_key,pm.fulfillment_scheme
                  ORDER BY pm.fetched_at DESC,pm.id DESC) rn
                FROM product_metric_values pm JOIN source_runs sr ON sr.id=pm.source_run_id
                JOIN product_listings pl0 ON pl0.id=pm.listing_id JOIN products p0 ON p0.id=pl0.product_id
                WHERE p0.shop_id=? AND pm.data_date BETWEEN ? AND ?
                  AND pm.metric_key IN ('ordered_units','ordered_revenue') AND pm.fulfillment_scheme='ALL'
                  AND sr.status IN ('success','partial')),
              daily AS (
                SELECT r.listing_id,r.data_date,
                  MAX(CASE WHEN r.metric_key='ordered_units' AND r.rn=1 THEN r.value END) units,
                  MAX(CASE WHEN r.metric_key='ordered_revenue' AND r.rn=1 THEN r.value END) revenue
                FROM ranked r GROUP BY r.listing_id,r.data_date),
              costed AS (
                SELECT d.*,pl.product_id,pl.marketplace_sku,pl.offer_id,p.internal_sku,p.name,mc.marketplace,
                  (SELECT h.cost_price FROM product_cost_history h WHERE h.product_id=p.id AND h.effective_date<=d.data_date
                   ORDER BY h.effective_date DESC,h.id DESC LIMIT 1) effective_cost
                FROM daily d JOIN product_listings pl ON pl.id=d.listing_id JOIN products p ON p.id=pl.product_id
                JOIN marketplace_connections mc ON mc.id=pl.connection_id)
              SELECT product_id,internal_sku,name,marketplace,marketplace_sku,offer_id,
                     SUM(COALESCE(units,0)) units,SUM(COALESCE(revenue,0)) order_revenue,
                     SUM(CASE WHEN effective_cost IS NOT NULL THEN COALESCE(units,0)*effective_cost ELSE 0 END) estimated_cost,
                     SUM(CASE WHEN effective_cost IS NOT NULL THEN COALESCE(units,0) ELSE 0 END) covered_units
              FROM costed GROUP BY marketplace,marketplace_sku ORDER BY order_revenue DESC''',
              (shop_id,start_date,end_date)).fetchall()
        return [dict(r) for r in rows]

    def sku_financial_totals(self, shop_id: int, start_date: str, end_date: str) -> dict[tuple[str,str],dict[str,float]]:
        keys=('financial_sales','goods_payable','commission','logistics','storage','acceptance','services','penalties','compensation')
        placeholders=','.join('?' for _ in keys)
        with self.db.connect() as c:
            rows=c.execute(f'''WITH ranked AS (
                SELECT pm.*,ROW_NUMBER() OVER(PARTITION BY pm.listing_id,pm.data_date,pm.metric_key,pm.fulfillment_scheme
                  ORDER BY pm.fetched_at DESC,pm.id DESC) rn
                FROM product_metric_values pm JOIN source_runs sr ON sr.id=pm.source_run_id
                JOIN product_listings pl0 ON pl0.id=pm.listing_id JOIN products p0 ON p0.id=pl0.product_id
                WHERE p0.shop_id=? AND pm.data_date BETWEEN ? AND ? AND pm.metric_key IN ({placeholders})
                  AND pm.fulfillment_scheme='ALL' AND sr.status IN ('success','partial'))
              SELECT mc.marketplace,pl.marketplace_sku,r.metric_key,SUM(r.value) value
              FROM ranked r JOIN product_listings pl ON pl.id=r.listing_id
              JOIN marketplace_connections mc ON mc.id=pl.connection_id
              WHERE r.rn=1 GROUP BY mc.marketplace,pl.marketplace_sku,r.metric_key''',
              (shop_id,start_date,end_date,*keys)).fetchall()
        out: dict[tuple[str,str],dict[str,float]]={}
        for r in rows:
            out.setdefault((str(r['marketplace']),str(r['marketplace_sku'])),{})[str(r['metric_key'])]=float(r['value'] or 0)
        return out


    # --- advertising detail --------------------------------------------
    def save_ad_details(self, source_run_id: int, *, campaigns: Iterable[AdCampaignPoint] = (),
                        products: Iterable[AdProductPoint] = ()) -> tuple[int,int]:
        campaign_rows=list(campaigns); product_rows=list(products); now=utcnow()
        with self.db.connect() as c:
            run=c.execute('SELECT connection_id,status FROM source_runs WHERE id=?',(source_run_id,)).fetchone()
            if not run or run['status'] not in ('success','partial'):
                raise ValueError('Ad details require a successful/partial source run')
            conn_id=int(run['connection_id']); c.execute('BEGIN IMMEDIATE')
            saved_campaigns=saved_products=0
            for p in campaign_rows:
                if int(p.connection_id)!=conn_id: raise ValueError('Ad campaign belongs to another connection')
                cur=c.execute("""INSERT OR IGNORE INTO ad_campaign_daily(
                    connection_id,data_date,campaign_id,campaign_name,spend,attributed_sales,orders,clicks,impressions,source_run_id,fetched_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (conn_id,p.data_date,str(p.campaign_id),p.campaign_name,float(p.spend),float(p.attributed_sales),
                     float(p.orders),float(p.clicks),float(p.impressions),source_run_id,now))
                saved_campaigns += cur.rowcount
            for p in product_rows:
                if int(p.connection_id)!=conn_id: raise ValueError('Ad product belongs to another connection')
                listing_id=p.listing_id
                if listing_id is not None:
                    row=c.execute('SELECT connection_id FROM product_listings WHERE id=?',(listing_id,)).fetchone()
                    if not row or int(row['connection_id'])!=conn_id: raise ValueError('Ad product listing belongs to another connection')
                cur=c.execute("""INSERT OR IGNORE INTO ad_product_daily(
                    connection_id,listing_id,marketplace_sku,data_date,campaign_id,campaign_name,spend,attributed_sales,
                    orders,clicks,impressions,source_run_id,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (conn_id,listing_id,str(p.marketplace_sku),p.data_date,str(p.campaign_id),p.campaign_name,
                     float(p.spend),float(p.attributed_sales),float(p.orders),float(p.clicks),float(p.impressions),source_run_id,now))
                saved_products += cur.rowcount
            c.commit()
        return saved_campaigns,saved_products

    def ad_campaign_totals(self, shop_id: int, start_date: str, end_date: str) -> list[dict[str,Any]]:
        with self.db.connect() as c:
            rows=c.execute("""WITH ranked AS (
                SELECT a.*,mc.marketplace,ROW_NUMBER() OVER(
                  PARTITION BY a.connection_id,a.data_date,a.campaign_id ORDER BY a.fetched_at DESC,a.id DESC) rn
                FROM ad_campaign_daily a JOIN marketplace_connections mc ON mc.id=a.connection_id
                JOIN source_runs sr ON sr.id=a.source_run_id
                WHERE mc.shop_id=? AND a.data_date BETWEEN ? AND ? AND sr.status IN ('success','partial'))
              SELECT marketplace,campaign_id,MAX(campaign_name) campaign_name,SUM(spend) spend,
                     SUM(attributed_sales) attributed_sales,SUM(orders) orders,SUM(clicks) clicks,SUM(impressions) impressions
              FROM ranked WHERE rn=1 GROUP BY marketplace,campaign_id
              ORDER BY spend DESC,campaign_id""",(shop_id,start_date,end_date)).fetchall()
        return [dict(r) for r in rows]

    def ad_product_totals(self, shop_id: int, start_date: str, end_date: str) -> list[dict[str,Any]]:
        with self.db.connect() as c:
            rows=c.execute("""WITH ranked AS (
                SELECT a.*,mc.marketplace,p.internal_sku,p.name,ROW_NUMBER() OVER(
                  PARTITION BY a.connection_id,a.data_date,a.marketplace_sku,a.campaign_id ORDER BY a.fetched_at DESC,a.id DESC) rn
                FROM ad_product_daily a JOIN marketplace_connections mc ON mc.id=a.connection_id
                JOIN source_runs sr ON sr.id=a.source_run_id
                LEFT JOIN product_listings pl ON pl.id=a.listing_id
                LEFT JOIN products p ON p.id=pl.product_id
                WHERE mc.shop_id=? AND a.data_date BETWEEN ? AND ? AND sr.status IN ('success','partial'))
              SELECT marketplace,marketplace_sku,COALESCE(MAX(internal_sku),marketplace_sku) internal_sku,
                     COALESCE(MAX(name),marketplace_sku) name,SUM(spend) spend,SUM(attributed_sales) attributed_sales,
                     SUM(orders) orders,SUM(clicks) clicks,SUM(impressions) impressions
              FROM ranked WHERE rn=1 GROUP BY marketplace,marketplace_sku
              ORDER BY spend DESC,marketplace_sku""",(shop_id,start_date,end_date)).fetchall()
        return [dict(r) for r in rows]

    def ad_product_totals_map(self, shop_id: int, start_date: str, end_date: str) -> dict[tuple[str,str],dict[str,float]]:
        out={}
        for r in self.ad_product_totals(shop_id,start_date,end_date):
            out[(str(r['marketplace']),str(r['marketplace_sku']))]={
                'ad_spend':float(r.get('spend') or 0),'ad_attributed_sales':float(r.get('attributed_sales') or 0),
                'ad_orders':float(r.get('orders') or 0),'ad_clicks':float(r.get('clicks') or 0),'ad_impressions':float(r.get('impressions') or 0)}
        return out

    def daily_shop_metric(self, shop_id: int, start_date: str, end_date: str, metric_key: str) -> dict[str,float]:
        conns=[c for c in self.list_connections(shop_id) if c.enabled]
        result: dict[str,float]={}
        for conn in conns:
            for day,value in self.metric_series(conn.id,start_date,end_date,metric_key).items():
                result[day]=result.get(day,0.0)+value
        return result


    # --- order/sale/finance reconciliation ------------------------------
    def save_commerce_events(self, source_run_id: int, points: Iterable[CommerceEventPoint]) -> int:
        rows=list(points)
        if not rows:
            return 0
        allowed={'order','cancel','posting','sale','return','finance'}
        now=utcnow()
        with self.db.connect() as c:
            run=c.execute('SELECT connection_id,status FROM source_runs WHERE id=?',(source_run_id,)).fetchone()
            if not run or run['status'] not in ('success','partial'):
                raise ValueError('Commerce events require a successful/partial source run')
            c.execute('BEGIN IMMEDIATE')
            saved=0
            for p in rows:
                if p.event_kind not in allowed:
                    raise ValueError(f'Unsupported commerce event kind: {p.event_kind}')
                if p.listing_id is not None:
                    listing=c.execute('SELECT connection_id FROM product_listings WHERE id=?',(p.listing_id,)).fetchone()
                    if not listing or int(listing['connection_id']) != int(run['connection_id']):
                        raise ValueError('Commerce event listing belongs to another connection')
                cur=c.execute("""INSERT OR IGNORE INTO commerce_events(
                    connection_id,listing_id,data_date,event_time,event_kind,external_order_id,external_event_id,
                    quantity,gross_amount,net_amount,fulfillment_scheme,is_preliminary,source_name,source_run_id,
                    metadata_json,fingerprint,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (int(run['connection_id']),p.listing_id,p.data_date,p.event_time,p.event_kind,
                     p.external_order_id,p.external_event_id,float(p.quantity),float(p.gross_amount),
                     None if p.net_amount is None else float(p.net_amount),p.fulfillment_scheme,
                     int(p.is_preliminary),p.source_name,source_run_id,p.metadata_json,p.fingerprint,now))
                saved += cur.rowcount
            c.commit()
        return saved

    def reconciliation_summary(self, shop_id: int, start_date: str, end_date: str) -> list[dict[str,Any]]:
        """Aggregate stored lifecycle events by marketplace/SKU without inventing joins."""
        with self.db.connect() as c:
            rows=c.execute("""WITH base_ids AS (
                SELECT DISTINCT ce.connection_id,ce.external_order_id
                FROM commerce_events ce JOIN marketplace_connections mc0 ON mc0.id=ce.connection_id
                WHERE mc0.shop_id=? AND ce.data_date BETWEEN ? AND ?
                  AND ce.event_kind IN ('order','cancel','posting','sale','return')
                  AND ce.external_order_id IS NOT NULL
              ), selected AS (
                SELECT ce.* FROM commerce_events ce JOIN marketplace_connections mc1 ON mc1.id=ce.connection_id
                WHERE mc1.shop_id=? AND (
                  (ce.event_kind<>'finance' AND ce.data_date BETWEEN ? AND ?)
                  OR (ce.event_kind='finance' AND (
                    ce.data_date BETWEEN ? AND ? OR EXISTS(
                      SELECT 1 FROM base_ids b WHERE b.connection_id=ce.connection_id AND b.external_order_id=ce.external_order_id)))
                )
              )
              SELECT mc.marketplace,p.internal_sku,p.name,pl.marketplace_sku,
                SUM(CASE WHEN ce.event_kind='order' THEN ce.quantity ELSE 0 END) ordered_units,
                SUM(CASE WHEN ce.event_kind='cancel' THEN ce.quantity ELSE 0 END) cancelled_units,
                SUM(CASE WHEN ce.event_kind='posting' THEN ce.quantity ELSE 0 END) posting_units,
                SUM(CASE WHEN ce.event_kind='sale' THEN ce.quantity ELSE 0 END) sale_units,
                SUM(CASE WHEN ce.event_kind='return' THEN ce.quantity ELSE 0 END) return_units,
                SUM(CASE WHEN ce.event_kind='finance' THEN ce.gross_amount ELSE 0 END) finance_gross,
                SUM(CASE WHEN ce.event_kind='finance' THEN COALESCE(ce.net_amount,0) ELSE 0 END) finance_net,
                COUNT(DISTINCT CASE WHEN ce.event_kind='order' AND ce.external_order_id IS NOT NULL THEN ce.external_order_id END) order_ids,
                COUNT(DISTINCT CASE WHEN ce.event_kind IN ('sale','return','posting','finance') AND ce.external_order_id IS NOT NULL THEN ce.external_order_id END) downstream_ids
              FROM selected ce
              JOIN marketplace_connections mc ON mc.id=ce.connection_id
              LEFT JOIN product_listings pl ON pl.id=ce.listing_id
              LEFT JOIN products p ON p.id=pl.product_id
              GROUP BY mc.marketplace,pl.id
              ORDER BY mc.marketplace,finance_gross DESC,ordered_units DESC""",
              (shop_id,start_date,end_date,shop_id,start_date,end_date,start_date,end_date)).fetchall()
        return [dict(r) for r in rows]

    def reconciliation_match_stats(self, shop_id: int, start_date: str, end_date: str) -> dict[str,dict[str,float]]:
        """Exact identifier coverage. WB uses srid; Ozon uses posting_number for posting->finance."""
        out: dict[str,dict[str,float]]={}
        with self.db.connect() as c:
            row=c.execute("""WITH orders AS (
                SELECT DISTINCT ce.connection_id,ce.external_order_id FROM commerce_events ce
                JOIN marketplace_connections mc ON mc.id=ce.connection_id
                WHERE mc.shop_id=? AND mc.marketplace='wildberries' AND ce.data_date BETWEEN ? AND ?
                  AND ce.event_kind='order' AND ce.external_order_id IS NOT NULL),
              downstream AS (
                SELECT DISTINCT ce.connection_id,ce.external_order_id FROM commerce_events ce
                JOIN marketplace_connections mc ON mc.id=ce.connection_id
                WHERE mc.shop_id=? AND mc.marketplace='wildberries' AND ce.event_kind IN ('sale','return','finance')
                  AND ce.external_order_id IS NOT NULL)
              SELECT COUNT(*) total, SUM(CASE WHEN d.external_order_id IS NOT NULL THEN 1 ELSE 0 END) matched
              FROM orders o LEFT JOIN downstream d ON d.connection_id=o.connection_id AND d.external_order_id=o.external_order_id""",
              (shop_id,start_date,end_date,shop_id)).fetchone()
            total=float((row['total'] if row else 0) or 0); matched=float((row['matched'] if row else 0) or 0)
            out['wildberries']={'total':total,'matched':matched,'coverage_pct':(matched/total*100 if total else 0.0)}

            row=c.execute("""WITH postings AS (
                SELECT DISTINCT ce.connection_id,ce.external_order_id FROM commerce_events ce
                JOIN marketplace_connections mc ON mc.id=ce.connection_id
                WHERE mc.shop_id=? AND mc.marketplace='ozon' AND ce.data_date BETWEEN ? AND ?
                  AND ce.event_kind='posting' AND ce.external_order_id IS NOT NULL),
              finance AS (
                SELECT DISTINCT ce.connection_id,ce.external_order_id FROM commerce_events ce
                JOIN marketplace_connections mc ON mc.id=ce.connection_id
                WHERE mc.shop_id=? AND mc.marketplace='ozon' AND ce.event_kind='finance'
                  AND ce.external_order_id IS NOT NULL)
              SELECT COUNT(*) total, SUM(CASE WHEN f.external_order_id IS NOT NULL THEN 1 ELSE 0 END) matched
              FROM postings p LEFT JOIN finance f ON f.connection_id=p.connection_id AND f.external_order_id=p.external_order_id""",
              (shop_id,start_date,end_date,shop_id)).fetchone()
            total=float((row['total'] if row else 0) or 0); matched=float((row['matched'] if row else 0) or 0)
            out['ozon']={'total':total,'matched':matched,'coverage_pct':(matched/total*100 if total else 0.0)}
        return out

    def commerce_event_counts(self, shop_id: int, start_date: str, end_date: str) -> dict[str,dict[str,float]]:
        with self.db.connect() as c:
            rows=c.execute("""SELECT mc.marketplace,ce.event_kind,COUNT(*) rows_count,SUM(ce.quantity) quantity
              FROM commerce_events ce JOIN marketplace_connections mc ON mc.id=ce.connection_id
              WHERE mc.shop_id=? AND ce.data_date BETWEEN ? AND ?
              GROUP BY mc.marketplace,ce.event_kind""",(shop_id,start_date,end_date)).fetchall()
        out: dict[str,dict[str,float]]={}
        for r in rows:
            out.setdefault(str(r['marketplace']),{})[str(r['event_kind'])]=float(r['quantity'] or r['rows_count'] or 0)
        return out

    # --- alert persistence -----------------------------------------------
    def get_alert_state(self, shop_id: int, rule_key: str, subject_key: str = 'shop') -> dict[str,Any] | None:
        with self.db.connect() as c:
            row=c.execute('SELECT * FROM alert_state WHERE shop_id=? AND rule_key=? AND subject_key=?',
                          (shop_id,rule_key,subject_key)).fetchone()
        return dict(row) if row else None

    def save_alert_state(self, shop_id: int, rule_key: str, subject_key: str, *, active: bool,
                         value: float | None, fingerprint: str | None, notified_at: str | None = None,
                         resolved_at: str | None = None) -> None:
        now=utcnow()
        old=self.get_alert_state(shop_id,rule_key,subject_key)
        first=(old or {}).get('first_triggered_at') or (now if active else None)
        with self.db.connect() as c:
            c.execute('''INSERT INTO alert_state(shop_id,rule_key,subject_key,active,last_value,fingerprint,
                first_triggered_at,last_triggered_at,last_notified_at,last_resolved_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(shop_id,rule_key,subject_key) DO UPDATE SET
                active=excluded.active,last_value=excluded.last_value,fingerprint=excluded.fingerprint,
                first_triggered_at=COALESCE(alert_state.first_triggered_at,excluded.first_triggered_at),
                last_triggered_at=excluded.last_triggered_at,
                last_notified_at=COALESCE(excluded.last_notified_at,alert_state.last_notified_at),
                last_resolved_at=COALESCE(excluded.last_resolved_at,alert_state.last_resolved_at),updated_at=excluded.updated_at''',
                (shop_id,rule_key,subject_key,int(active),value,fingerprint,first,now if active else (old or {}).get('last_triggered_at'),
                 notified_at,resolved_at,now))

    def record_alert_event(self, shop_id: int, rule_key: str, subject_key: str, severity: str,
                           message: str, value: float | None = None, fingerprint: str | None = None) -> int:
        now=utcnow()
        with self.db.connect() as c:
            cur=c.execute('''INSERT INTO alert_events(shop_id,rule_key,subject_key,severity,message,value,fingerprint,created_at)
                             VALUES(?,?,?,?,?,?,?,?)''',(shop_id,rule_key,subject_key,severity,message,value,fingerprint,now))
            return int(cur.lastrowid)

    def recent_alert_events(self, shop_id: int, limit: int = 20) -> list[dict[str,Any]]:
        with self.db.connect() as c:
            rows=c.execute('SELECT * FROM alert_events WHERE shop_id=? ORDER BY created_at DESC,id DESC LIMIT ?',
                           (shop_id,limit)).fetchall()
        return [dict(r) for r in rows]

    def active_alert_states(self, shop_id: int) -> list[dict[str,Any]]:
        with self.db.connect() as c:
            rows=c.execute('''SELECT * FROM alert_state WHERE shop_id=? AND active=1
                              ORDER BY rule_key, subject_key''',(shop_id,)).fetchall()
        return [dict(r) for r in rows]

    # --- production runtime / leases / retry queue ----------------------
    def acquire_lease(self, lease_key: str, owner_id: str, ttl_seconds: int, *, metadata: dict[str,Any] | None = None) -> bool:
        now_dt=datetime.now(timezone.utc); now=now_dt.isoformat(timespec='seconds')
        expires=(now_dt+timedelta(seconds=max(1,ttl_seconds))).isoformat(timespec='seconds')
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT owner_id,expires_at FROM runtime_leases WHERE lease_key=?',(lease_key,)).fetchone()
            if row is not None:
                try: expired=datetime.fromisoformat(str(row['expires_at'])) <= now_dt
                except ValueError: expired=True
                if not expired and str(row['owner_id']) != owner_id:
                    return False
            c.execute("""INSERT INTO runtime_leases(lease_key,owner_id,acquired_at,expires_at,metadata_json,updated_at)
                         VALUES(?,?,?,?,?,?) ON CONFLICT(lease_key) DO UPDATE SET
                         owner_id=excluded.owner_id,
                         acquired_at=CASE WHEN runtime_leases.owner_id=excluded.owner_id THEN runtime_leases.acquired_at ELSE excluded.acquired_at END,
                         expires_at=excluded.expires_at,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
                      (lease_key,owner_id,now,expires,canonical_json(metadata or {}),now))
            return True

    def renew_lease(self, lease_key: str, owner_id: str, ttl_seconds: int) -> bool:
        now_dt=datetime.now(timezone.utc); now=now_dt.isoformat(timespec='seconds')
        expires=(now_dt+timedelta(seconds=max(1,ttl_seconds))).isoformat(timespec='seconds')
        with self.db.connect() as c:
            cur=c.execute('UPDATE runtime_leases SET expires_at=?,updated_at=? WHERE lease_key=? AND owner_id=?',
                          (expires,now,lease_key,owner_id))
            return cur.rowcount==1

    def release_lease(self, lease_key: str, owner_id: str) -> bool:
        with self.db.connect() as c:
            cur=c.execute('DELETE FROM runtime_leases WHERE lease_key=? AND owner_id=?',(lease_key,owner_id))
            return cur.rowcount==1

    def lease_info(self, lease_key: str) -> dict[str,Any] | None:
        with self.db.connect() as c:
            row=c.execute('SELECT * FROM runtime_leases WHERE lease_key=?',(lease_key,)).fetchone()
        return dict(row) if row else None

    def heartbeat(self, instance_id: str, *, role: str='bot', hostname: str='', pid: int | None=None, metadata: dict[str,Any] | None=None) -> None:
        now=utcnow()
        with self.db.connect() as c:
            c.execute("""INSERT INTO process_heartbeats(instance_id,role,hostname,pid,started_at,heartbeat_at,metadata_json)
                         VALUES(?,?,?,?,?,?,?) ON CONFLICT(instance_id) DO UPDATE SET
                         role=excluded.role,hostname=excluded.hostname,pid=excluded.pid,heartbeat_at=excluded.heartbeat_at,metadata_json=excluded.metadata_json""",
                      (instance_id,role,hostname,pid,now,now,canonical_json(metadata or {})))

    def recent_heartbeats(self, limit: int=20) -> list[dict[str,Any]]:
        with self.db.connect() as c:
            rows=c.execute('SELECT * FROM process_heartbeats ORDER BY heartbeat_at DESC LIMIT ?',(limit,)).fetchall()
        return [dict(r) for r in rows]

    def enqueue_retry_job(self, shop_id: int, job_type: str, unique_key: str, payload: dict[str,Any], *, max_attempts: int=6, delay_seconds: int=0, last_error: str | None=None) -> int:
        now_dt=datetime.now(timezone.utc); now=now_dt.isoformat(timespec='seconds')
        due=(now_dt+timedelta(seconds=max(0,delay_seconds))).isoformat(timespec='seconds')
        with self.db.connect() as c:
            c.execute("""INSERT INTO retry_jobs(shop_id,job_type,unique_key,payload_json,status,attempts,max_attempts,next_attempt_at,last_error,created_at,updated_at)
                         VALUES(?,?,?,?, 'pending',0,?,?,?,?,?) ON CONFLICT(shop_id,job_type,unique_key) DO UPDATE SET
                         payload_json=excluded.payload_json,
                         status=CASE WHEN retry_jobs.status='running' THEN 'running' ELSE 'pending' END,
                         attempts=CASE WHEN retry_jobs.status IN ('success','dead') THEN 0 ELSE retry_jobs.attempts END,
                         max_attempts=excluded.max_attempts,
                         next_attempt_at=CASE WHEN retry_jobs.status='running' THEN retry_jobs.next_attempt_at ELSE excluded.next_attempt_at END,
                         last_error=CASE WHEN retry_jobs.status='running' THEN retry_jobs.last_error ELSE excluded.last_error END,
                         updated_at=excluded.updated_at""",
                      (shop_id,job_type,unique_key,canonical_json(payload),max_attempts,due,last_error,now,now))
            row=c.execute('SELECT id FROM retry_jobs WHERE shop_id=? AND job_type=? AND unique_key=?',(shop_id,job_type,unique_key)).fetchone()
            return int(row['id'])

    def claim_retry_job(self, owner_id: str, *, lease_seconds: int=900) -> dict[str,Any] | None:
        now_dt=datetime.now(timezone.utc); now=now_dt.isoformat(timespec='seconds')
        locked_until=(now_dt+timedelta(seconds=max(30,lease_seconds))).isoformat(timespec='seconds')
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute("""SELECT * FROM retry_jobs WHERE
                (status='pending' AND next_attempt_at<=?) OR
                (status='running' AND locked_until IS NOT NULL AND locked_until<=?)
                ORDER BY next_attempt_at,id LIMIT 1""",(now,now)).fetchone()
            if not row: return None
            c.execute("UPDATE retry_jobs SET status='running',locked_by=?,locked_until=?,updated_at=? WHERE id=?",
                      (owner_id,locked_until,now,row['id']))
            out=dict(row); out['locked_by']=owner_id; out['locked_until']=locked_until; out['status']='running'
            try: out['payload']=json.loads(out.pop('payload_json') or '{}')
            except Exception: out['payload']={}
            return out

    def renew_retry_job(self, job_id: int, owner_id: str, lease_seconds: int=900) -> bool:
        now_dt=datetime.now(timezone.utc); now=now_dt.isoformat(timespec='seconds')
        locked_until=(now_dt+timedelta(seconds=max(30,lease_seconds))).isoformat(timespec='seconds')
        with self.db.connect() as c:
            cur=c.execute("""UPDATE retry_jobs SET locked_until=?,updated_at=?
                             WHERE id=? AND status='running' AND locked_by=?""",
                          (locked_until,now,job_id,owner_id))
            return cur.rowcount==1

    def complete_retry_job(self, job_id: int, *, owner_id: str | None=None) -> bool:
        now=utcnow()
        sql="UPDATE retry_jobs SET status='success',locked_by=NULL,locked_until=NULL,last_error=NULL,updated_at=? WHERE id=?"
        params: list[Any]=[now,job_id]
        if owner_id is not None:
            sql += " AND status='running' AND locked_by=?"; params.append(owner_id)
        with self.db.connect() as c:
            cur=c.execute(sql,params)
            return cur.rowcount==1

    def fail_retry_job(self, job_id: int, error: str, *, base_delay_seconds: int=60, owner_id: str | None=None) -> str:
        now_dt=datetime.now(timezone.utc); now=now_dt.isoformat(timespec='seconds')
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            sql='SELECT attempts,max_attempts,status,locked_by FROM retry_jobs WHERE id=?'
            row=c.execute(sql,(job_id,)).fetchone()
            if not row: raise ValueError('retry job not found')
            if owner_id is not None and (str(row['status'])!='running' or str(row['locked_by'] or '')!=owner_id):
                return 'lost'
            attempts=int(row['attempts'])+1; max_attempts=int(row['max_attempts'])
            status='dead' if attempts>=max_attempts else 'pending'
            delay=min(6*3600,base_delay_seconds*(2**max(0,attempts-1)))
            due=(now_dt+timedelta(seconds=delay)).isoformat(timespec='seconds')
            params=[status,attempts,due,error[:2000],now,job_id]
            where='id=?'
            if owner_id is not None:
                where += " AND status='running' AND locked_by=?"; params.append(owner_id)
            cur=c.execute(f"UPDATE retry_jobs SET status=?,attempts=?,next_attempt_at=?,locked_by=NULL,locked_until=NULL,last_error=?,updated_at=? WHERE {where}",params)
            return status if cur.rowcount==1 else 'lost'

    def retry_job_counts(self, shop_id: int | None=None) -> dict[str,int]:
        with self.db.connect() as c:
            if shop_id is None:
                rows=c.execute('SELECT status,COUNT(*) n FROM retry_jobs GROUP BY status').fetchall()
            else:
                rows=c.execute('SELECT status,COUNT(*) n FROM retry_jobs WHERE shop_id=? GROUP BY status',(shop_id,)).fetchall()
        return {str(r['status']):int(r['n']) for r in rows}

    def recent_retry_jobs(self, limit: int=20, *, shop_id: int | None=None) -> list[dict[str,Any]]:
        with self.db.connect() as c:
            if shop_id is None:
                rows=c.execute('SELECT * FROM retry_jobs ORDER BY updated_at DESC,id DESC LIMIT ?',(limit,)).fetchall()
            else:
                rows=c.execute('SELECT * FROM retry_jobs WHERE shop_id=? ORDER BY updated_at DESC,id DESC LIMIT ?',(shop_id,limit)).fetchall()
        return [dict(r) for r in rows]

    def requeue_retry_job(self, job_id: int, *, shop_id: int | None=None) -> bool:
        now=utcnow()
        with self.db.connect() as c:
            sql="""UPDATE retry_jobs SET status='pending',attempts=0,next_attempt_at=?,locked_by=NULL,
                   locked_until=NULL,last_error=NULL,updated_at=? WHERE id=?"""
            params: list[Any]=[now,now,job_id]
            if shop_id is not None:
                sql += ' AND shop_id=?'; params.append(shop_id)
            cur=c.execute(sql,params)
            return cur.rowcount==1

    # --- supply planning / demand history --------------------------------
    def ensure_shop_supply_preferences(self, shop_id: int) -> dict[str,Any]:
        now=utcnow()
        with self.db.connect() as c:
            c.execute("""INSERT INTO shop_supply_preferences(shop_id,updated_at) VALUES(?,?)
                         ON CONFLICT(shop_id) DO NOTHING""",(shop_id,now))
            row=c.execute('SELECT * FROM shop_supply_preferences WHERE shop_id=?',(shop_id,)).fetchone()
        return dict(row)

    def update_shop_supply_preferences(self, shop_id: int, **values) -> dict[str,Any]:
        allowed={'lookback_days','xyz_weeks','default_lead_time_days','default_safety_stock_days','default_target_stock_days','min_history_days','seasonality_enabled','forecast_horizon_days','auto_calibration_enabled','max_lead_buffer_days','max_safety_buffer_days'}
        clean={k:int(v) for k,v in values.items() if k in allowed}
        if not clean: return self.ensure_shop_supply_preferences(shop_id)
        bounds={
            'lookback_days':(14,365),'xyz_weeks':(4,26),'default_lead_time_days':(0,365),
            'default_safety_stock_days':(0,180),'default_target_stock_days':(1,365),'min_history_days':(3,180),
            'seasonality_enabled':(0,1),'forecast_horizon_days':(1,30),
            'auto_calibration_enabled':(0,1),'max_lead_buffer_days':(0,30),'max_safety_buffer_days':(0,30),
        }
        for k,v in clean.items():
            lo,hi=bounds[k]
            if not lo <= v <= hi: raise ValueError(f'{k} must be {lo}..{hi}')
        self.ensure_shop_supply_preferences(shop_id)
        assignments=','.join(f'{k}=?' for k in clean); params=list(clean.values())+[utcnow(),shop_id]
        with self.db.connect() as c:
            c.execute(f'UPDATE shop_supply_preferences SET {assignments},updated_at=? WHERE shop_id=?',params)
            row=c.execute('SELECT * FROM shop_supply_preferences WHERE shop_id=?',(shop_id,)).fetchone()
        return dict(row)

    def set_product_supply_settings(self, shop_id: int, internal_sku: str, *, lead_time_days: int | None=None,
                                    safety_stock_days: int | None=None, target_stock_days: int | None=None,
                                    pack_size: float | None=None, min_order_qty: float | None=None) -> dict[str,Any]:
        with self.db.connect() as c:
            p=c.execute('SELECT id FROM products WHERE shop_id=? AND internal_sku=? AND active=1',(shop_id,internal_sku)).fetchone()
        if not p: raise ValueError('product not found')
        vals={'lead_time_days':lead_time_days,'safety_stock_days':safety_stock_days,'target_stock_days':target_stock_days,
              'pack_size':pack_size,'min_order_qty':min_order_qty}
        if lead_time_days is not None and not 0<=int(lead_time_days)<=365: raise ValueError('lead_time_days must be 0..365')
        if safety_stock_days is not None and not 0<=int(safety_stock_days)<=180: raise ValueError('safety_stock_days must be 0..180')
        if target_stock_days is not None and not 1<=int(target_stock_days)<=365: raise ValueError('target_stock_days must be 1..365')
        if pack_size is not None and float(pack_size)<=0: raise ValueError('pack_size must be > 0')
        if min_order_qty is not None and float(min_order_qty)<0: raise ValueError('min_order_qty must be >= 0')
        now=utcnow(); pid=int(p['id'])
        with self.db.connect() as c:
            c.execute("""INSERT INTO product_supply_settings(product_id,lead_time_days,safety_stock_days,target_stock_days,pack_size,min_order_qty,updated_at)
                         VALUES(?,?,?,?,?,?,?) ON CONFLICT(product_id) DO UPDATE SET
                         lead_time_days=excluded.lead_time_days,safety_stock_days=excluded.safety_stock_days,
                         target_stock_days=excluded.target_stock_days,pack_size=excluded.pack_size,
                         min_order_qty=excluded.min_order_qty,updated_at=excluded.updated_at""",
                      (pid,lead_time_days,safety_stock_days,target_stock_days,pack_size,min_order_qty,now))
            row=c.execute('SELECT * FROM product_supply_settings WHERE product_id=?',(pid,)).fetchone()
        return dict(row)

    def products_for_supply(self, shop_id: int) -> list[dict[str,Any]]:
        defaults=self.ensure_shop_supply_preferences(shop_id)
        with self.db.connect() as c:
            rows=c.execute("""SELECT p.id product_id,p.internal_sku,p.name,
                COALESCE(ps.lead_time_days,?) lead_time_days,
                COALESCE(ps.safety_stock_days,?) safety_stock_days,
                COALESCE(ps.target_stock_days,?) target_stock_days,
                COALESCE(ps.pack_size,1) pack_size,COALESCE(ps.min_order_qty,0) min_order_qty,
                (SELECT MIN(pl.created_at) FROM product_listings pl WHERE pl.product_id=p.id) first_listing_at
                FROM products p LEFT JOIN product_supply_settings ps ON ps.product_id=p.id
                WHERE p.shop_id=? AND p.active=1 ORDER BY p.internal_sku""",
                (defaults['default_lead_time_days'],defaults['default_safety_stock_days'],defaults['default_target_stock_days'],shop_id)).fetchall()
        return [dict(r) for r in rows]

    def complete_order_dates(self, shop_id: int, start_date: str, end_date: str) -> list[str]:
        """Dates with a successful core-order run for every enabled core connection."""
        conns=self.list_connections(shop_id)
        enabled=[c for c in conns if c.enabled and c.marketplace in {'ozon','wildberries'}]
        if not enabled: return []
        ids=[c.id for c in enabled]; placeholders=','.join('?' for _ in ids)
        with self.db.connect() as c:
            rows=c.execute(f"""SELECT sr.data_date,COUNT(DISTINCT sr.connection_id) n
                FROM source_runs sr WHERE sr.connection_id IN ({placeholders}) AND sr.data_date BETWEEN ? AND ?
                AND sr.status='success' AND (sr.endpoint='statistics/orders' OR sr.endpoint='statistics/orders/backfill'
                    OR sr.endpoint='analytics/orders' OR sr.endpoint='analytics/orders/backfill')
                GROUP BY sr.data_date HAVING COUNT(DISTINCT sr.connection_id)=? ORDER BY sr.data_date""",
                (*ids,start_date,end_date,len(ids))).fetchall()
        return [str(r['data_date']) for r in rows]

    def physical_product_daily_units(self, shop_id: int, start_date: str, end_date: str) -> dict[int,dict[str,float]]:
        """Latest ordered_units per listing/day, aggregated to a physical product."""
        with self.db.connect() as c:
            rows=c.execute("""WITH ranked AS (
                SELECT p.id product_id,pm.data_date,pm.value,
                       ROW_NUMBER() OVER(PARTITION BY pm.listing_id,pm.data_date,pm.metric_key,pm.fulfillment_scheme
                           ORDER BY pm.fetched_at DESC,pm.id DESC) rn
                FROM product_metric_values pm JOIN source_runs sr ON sr.id=pm.source_run_id
                JOIN product_listings pl ON pl.id=pm.listing_id JOIN products p ON p.id=pl.product_id
                WHERE p.shop_id=? AND p.active=1 AND pm.data_date BETWEEN ? AND ?
                  AND pm.metric_key='ordered_units' AND pm.fulfillment_scheme='ALL' AND sr.status='success')
              SELECT product_id,data_date,SUM(value) value FROM ranked WHERE rn=1
              GROUP BY product_id,data_date ORDER BY product_id,data_date""",(shop_id,start_date,end_date)).fetchall()
        out: dict[int,dict[str,float]]={}
        for r in rows: out.setdefault(int(r['product_id']),{})[str(r['data_date'])]=float(r['value'] or 0)
        return out

    def physical_product_inventory(self, shop_id: int) -> dict[int,float]:
        out: dict[int,float]={}
        for row in self.latest_inventory_by_listing(shop_id):
            pid=int(row['product_id']); out[pid]=out.get(pid,0.0)+float(row['available_units'] or 0)
        return out

    def save_supply_recommendations(self, shop_id: int, as_of_date: str, rows: Iterable[dict[str,Any]], *, method_version: str='wma-v1') -> int:
        now=utcnow(); saved=0
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for r in rows:
                c.execute("""INSERT INTO supply_recommendation_snapshots(
                    shop_id,product_id,as_of_date,generated_at,abc_class,xyz_class,avg_daily_units,
                    forecast_daily_units,trend_pct,available_units,days_cover,reorder_point_units,target_units,
                    recommended_order_units,confidence,history_days,method_version,bias_correction,promo_factor,promo_days,
                    lead_buffer_days,safety_buffer_days,effective_lead_time_days,effective_safety_stock_days,calibration_confidence)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(shop_id,product_id,as_of_date,method_version) DO UPDATE SET
                    generated_at=excluded.generated_at,abc_class=excluded.abc_class,xyz_class=excluded.xyz_class,
                    avg_daily_units=excluded.avg_daily_units,forecast_daily_units=excluded.forecast_daily_units,
                    trend_pct=excluded.trend_pct,available_units=excluded.available_units,days_cover=excluded.days_cover,
                    reorder_point_units=excluded.reorder_point_units,target_units=excluded.target_units,
                    recommended_order_units=excluded.recommended_order_units,confidence=excluded.confidence,history_days=excluded.history_days,
                    bias_correction=excluded.bias_correction,promo_factor=excluded.promo_factor,promo_days=excluded.promo_days,
                    lead_buffer_days=excluded.lead_buffer_days,safety_buffer_days=excluded.safety_buffer_days,
                    effective_lead_time_days=excluded.effective_lead_time_days,effective_safety_stock_days=excluded.effective_safety_stock_days,
                    calibration_confidence=excluded.calibration_confidence""",
                    (shop_id,r['product_id'],as_of_date,now,r['abc_class'],r['xyz_class'],r['avg_daily_units'],
                     r['forecast_daily_units'],r.get('trend_pct'),r['available_units'],r.get('days_cover'),
                     r['reorder_point_units'],r['target_units'],r['recommended_order_units'],r['confidence'],
                     r['history_days'],method_version,float(r.get('bias_correction',1)),float(r.get('promo_factor',1)),int(r.get('promo_days',0)),
                     int(r.get('lead_buffer_days',0)),int(r.get('safety_buffer_days',0)),int(r.get('effective_lead_time_days',0)),
                     int(r.get('effective_safety_stock_days',0)),str(r.get('calibration_confidence','none'))))
                saved += 1
            c.commit()
        return saved

    def latest_supply_recommendations(self, shop_id: int, as_of_date: str | None=None) -> list[dict[str,Any]]:
        with self.db.connect() as c:
            if as_of_date is None:
                row=c.execute('SELECT MAX(as_of_date) d FROM supply_recommendation_snapshots WHERE shop_id=?',(shop_id,)).fetchone()
                as_of_date=str(row['d']) if row and row['d'] else None
            if not as_of_date: return []
            rows=c.execute("""SELECT s.*,p.internal_sku,p.name FROM supply_recommendation_snapshots s
                JOIN products p ON p.id=s.product_id WHERE s.shop_id=? AND s.as_of_date=?
                ORDER BY s.recommended_order_units DESC,s.days_cover ASC""",(shop_id,as_of_date)).fetchall()
        return [dict(r) for r in rows]

    # --- inbound supplies / forecast quality -----------------------------
    def upsert_inbound_shipments(self, connection_id: int, source_run_id: int,
                                 marketplace: str, shipments: Iterable[dict[str,Any]],
                                 *, active_external_ids: Iterable[str] | None = None) -> int:
        """Persist the latest known state of marketplace inbound supplies.

        ``active_external_ids`` is optional and must only be supplied after a
        *complete* active-supply listing was received.  When present, previously
        active supplies missing from that listing are closed as ``NOT_ACTIVE``.
        This prevents completed/cancelled supplies from remaining "in transit"
        forever, while a partial/failed API response can never erase good state.
        """
        shipments=list(shipments)
        active_ids={str(x) for x in active_external_ids} if active_external_ids is not None else None
        now=utcnow(); saved=0
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for shipment in shipments:
                external_id=str(shipment['external_supply_id'])
                c.execute("""INSERT INTO inbound_shipments(
                    connection_id,marketplace,external_supply_id,status,planned_at,arrival_at,
                    warehouse_name,source_run_id,metadata_json,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(connection_id,external_supply_id) DO UPDATE SET
                    marketplace=excluded.marketplace,status=excluded.status,planned_at=excluded.planned_at,
                    arrival_at=excluded.arrival_at,warehouse_name=excluded.warehouse_name,
                    source_run_id=excluded.source_run_id,metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at""",
                    (connection_id,marketplace,external_id,str(shipment.get('status') or 'UNKNOWN'),
                     shipment.get('planned_at'),shipment.get('arrival_at'),str(shipment.get('warehouse_name') or ''),
                     source_run_id,canonical_json(shipment.get('metadata') or {}),now))
                row=c.execute('SELECT id FROM inbound_shipments WHERE connection_id=? AND external_supply_id=?',
                              (connection_id,external_id)).fetchone()
                shipment_id=int(row['id'])
                seen=[]
                for item in shipment.get('items') or []:
                    sku=str(item.get('marketplace_sku') or '')
                    if not sku: continue
                    seen.append(sku)
                    listing=c.execute('SELECT id FROM product_listings WHERE connection_id=? AND marketplace_sku=?',
                                      (connection_id,sku)).fetchone()
                    planned=max(0.0,float(item.get('planned_units') or 0))
                    accepted=max(0.0,float(item.get('accepted_units') or 0))
                    remaining=max(0.0,float(item.get('remaining_units') if item.get('remaining_units') is not None else planned-accepted))
                    c.execute("""INSERT INTO inbound_shipment_items(
                        shipment_id,listing_id,marketplace_sku,planned_units,accepted_units,remaining_units,updated_at)
                        VALUES(?,?,?,?,?,?,?) ON CONFLICT(shipment_id,marketplace_sku) DO UPDATE SET
                        listing_id=excluded.listing_id,planned_units=excluded.planned_units,
                        accepted_units=excluded.accepted_units,remaining_units=excluded.remaining_units,
                        updated_at=excluded.updated_at""",
                        (shipment_id,int(listing['id']) if listing else None,sku,planned,accepted,remaining,now))
                if seen:
                    placeholders=','.join('?' for _ in seen)
                    c.execute(f'DELETE FROM inbound_shipment_items WHERE shipment_id=? AND marketplace_sku NOT IN ({placeholders})',
                              (shipment_id,*seen))
                else:
                    c.execute('DELETE FROM inbound_shipment_items WHERE shipment_id=?',(shipment_id,))
                saved += 1

            if active_ids is not None:
                if active_ids:
                    placeholders=','.join('?' for _ in active_ids)
                    stale=c.execute(
                        f"SELECT id FROM inbound_shipments WHERE connection_id=? "
                        f"AND external_supply_id NOT IN ({placeholders}) AND status<>'NOT_ACTIVE'",
                        (connection_id,*sorted(active_ids)),
                    ).fetchall()
                else:
                    stale=c.execute(
                        "SELECT id FROM inbound_shipments WHERE connection_id=? AND status<>'NOT_ACTIVE'",
                        (connection_id,),
                    ).fetchall()
                stale_ids=[int(r['id']) for r in stale]
                if stale_ids:
                    placeholders=','.join('?' for _ in stale_ids)
                    c.execute(
                        f"UPDATE inbound_shipments SET status='NOT_ACTIVE',source_run_id=?,updated_at=? "
                        f"WHERE id IN ({placeholders})",
                        (source_run_id,now,*stale_ids),
                    )
                    c.execute(
                        f"UPDATE inbound_shipment_items SET remaining_units=0,updated_at=? "
                        f"WHERE shipment_id IN ({placeholders})",
                        (now,*stale_ids),
                    )
            c.commit()
        return saved

    def active_inbound_items(self, shop_id: int) -> list[dict[str,Any]]:
        """Current not-yet-completed inbound quantities linked to physical products."""
        completed_wb={'5','ACCEPTED','NOT_ACTIVE'}
        completed_ozon={'ORDER_STATE_COMPLETED','COMPLETED','ORDER_STATE_CANCELLED','CANCELLED',
                        'ORDER_STATE_REJECTED_AT_SUPPLY_WAREHOUSE','REJECTED_AT_SUPPLY_WAREHOUSE','NOT_ACTIVE'}
        with self.db.connect() as c:
            rows=c.execute("""SELECT s.marketplace,s.external_supply_id,s.status,s.planned_at,s.arrival_at,
                    s.warehouse_name,i.marketplace_sku,i.planned_units,i.accepted_units,i.remaining_units,
                    pl.product_id,p.internal_sku,p.name,s.updated_at
                FROM inbound_shipment_items i
                JOIN inbound_shipments s ON s.id=i.shipment_id
                JOIN marketplace_connections mc ON mc.id=s.connection_id
                LEFT JOIN product_listings pl ON pl.id=i.listing_id
                LEFT JOIN products p ON p.id=pl.product_id
                WHERE mc.shop_id=? AND i.remaining_units>0
                ORDER BY s.planned_at,s.marketplace,s.external_supply_id,i.marketplace_sku""",(shop_id,)).fetchall()
        out=[]
        for row in rows:
            d=dict(row); status=str(d['status'] or '')
            if d['marketplace']=='wildberries' and status in completed_wb: continue
            if d['marketplace']=='ozon' and status in completed_ozon: continue
            out.append(d)
        return out

    def inbound_units_by_product(self, shop_id: int, cutoff_date: str | None=None) -> dict[int,float]:
        out: dict[int,float]={}
        for row in self.active_inbound_items(shop_id):
            pid=row.get('product_id'); planned=row.get('planned_at')
            if pid is None: continue
            if cutoff_date and planned:
                day=str(planned)[:10]
                if day and day > cutoff_date: continue
            elif cutoff_date and not planned:
                # Unknown ETA is visible in the inbound report but not trusted in a replenishment calculation.
                continue
            out[int(pid)]=out.get(int(pid),0.0)+float(row.get('remaining_units') or 0)
        return out

    def save_forecast_quality(self, shop_id: int, rows: Iterable[dict[str,Any]], *, method_version: str) -> int:
        now=utcnow(); saved=0
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for r in rows:
                predicted=float(r['predicted_units']); actual=float(r['actual_units']); err=abs(predicted-actual)
                ape=(err/actual*100.0) if actual>0 else None
                c.execute("""INSERT INTO forecast_quality_snapshots(
                    shop_id,product_id,as_of_date,horizon_days,predicted_units,actual_units,
                    absolute_error,ape_pct,method_version,evaluated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(shop_id,product_id,as_of_date,horizon_days,method_version)
                    DO UPDATE SET predicted_units=excluded.predicted_units,actual_units=excluded.actual_units,
                    absolute_error=excluded.absolute_error,ape_pct=excluded.ape_pct,evaluated_at=excluded.evaluated_at""",
                    (shop_id,r.get('product_id'),r['as_of_date'],int(r['horizon_days']),predicted,actual,err,ape,method_version,now))
                saved += 1
            c.commit()
        return saved

    def forecast_quality_rows(self, shop_id: int, *, limit: int=500) -> list[dict[str,Any]]:
        with self.db.connect() as c:
            rows=c.execute("""SELECT q.*,p.internal_sku,p.name FROM forecast_quality_snapshots q
                LEFT JOIN products p ON p.id=q.product_id WHERE q.shop_id=?
                ORDER BY q.evaluated_at DESC,q.id DESC LIMIT ?""",(shop_id,max(1,int(limit)))).fetchall()
        return [dict(r) for r in rows]


    # --- supply self-calibration ----------------------------------------
    def product_inventory_daily(self, shop_id: int, start_date: str, end_date: str) -> dict[int,dict[str,float]]:
        """Observed available inventory by physical product/day.

        Each listing+scheme contributes only its newest source run for that UTC
        capture day; warehouse rows from that run are summed. Missing days stay
        missing and are never interpreted as zero stock.
        """
        with self.db.connect() as c:
            rows=c.execute("""WITH latest_run AS (
                SELECT i.listing_id,i.fulfillment_scheme,substr(i.captured_at,1,10) d,MAX(i.source_run_id) source_run_id
                FROM inventory_snapshots i JOIN product_listings pl ON pl.id=i.listing_id
                JOIN products p ON p.id=pl.product_id
                WHERE p.shop_id=? AND substr(i.captured_at,1,10) BETWEEN ? AND ?
                GROUP BY i.listing_id,i.fulfillment_scheme,substr(i.captured_at,1,10)),
              listing_day AS (
                SELECT pl.product_id,lr.d,SUM(i.available_units) available_units
                FROM latest_run lr JOIN inventory_snapshots i ON i.listing_id=lr.listing_id
                  AND i.fulfillment_scheme=lr.fulfillment_scheme AND i.source_run_id=lr.source_run_id
                JOIN product_listings pl ON pl.id=i.listing_id
                GROUP BY pl.product_id,lr.d,i.listing_id,i.fulfillment_scheme)
              SELECT product_id,d,SUM(available_units) available_units
              FROM listing_day GROUP BY product_id,d ORDER BY product_id,d""",
              (shop_id,start_date,end_date)).fetchall()
        out: dict[int,dict[str,float]]={}
        for r in rows: out.setdefault(int(r['product_id']),{})[str(r['d'])]=float(r['available_units'] or 0)
        return out

    def inbound_delay_samples_by_product(self, shop_id: int, start_date: str, end_date: str) -> dict[int,list[float]]:
        """Observed WB arrival lateness in days for linked products.

        WB ``factDate`` is an actual fact date; Ozon's current normalized
        ``arrival_at`` is a planned warehouse date, so Ozon is deliberately
        excluded instead of pretending it is an observed delay.
        """
        with self.db.connect() as c:
            rows=c.execute("""SELECT pl.product_id,s.planned_at,s.arrival_at
                FROM inbound_shipments s JOIN inbound_shipment_items i ON i.shipment_id=s.id
                JOIN product_listings pl ON pl.id=i.listing_id
                JOIN marketplace_connections mc ON mc.id=s.connection_id
                WHERE mc.shop_id=? AND s.marketplace='wildberries'
                  AND s.planned_at IS NOT NULL AND s.arrival_at IS NOT NULL
                  AND substr(s.arrival_at,1,10) BETWEEN ? AND ?
                GROUP BY pl.product_id,s.id""",(shop_id,start_date,end_date)).fetchall()
        out: dict[int,list[float]]={}
        for r in rows:
            try:
                planned=date.fromisoformat(str(r['planned_at'])[:10]); actual=date.fromisoformat(str(r['arrival_at'])[:10])
            except ValueError:
                continue
            out.setdefault(int(r['product_id']),[]).append(float(max(0,(actual-planned).days)))
        return out

    def forecast_quality_samples_by_product(self, shop_id: int, *, limit_per_product: int=24) -> dict[int,list[dict[str,Any]]]:
        with self.db.connect() as c:
            rows=c.execute("""WITH ranked AS (
                SELECT q.*,ROW_NUMBER() OVER (PARTITION BY q.product_id ORDER BY q.as_of_date DESC,q.id DESC) rn
                FROM forecast_quality_snapshots q WHERE q.shop_id=? AND q.product_id IS NOT NULL)
              SELECT * FROM ranked WHERE rn<=? ORDER BY product_id,as_of_date""",
              (shop_id,max(1,int(limit_per_product)))).fetchall()
        out: dict[int,list[dict[str,Any]]]={}
        for r in rows: out.setdefault(int(r['product_id']),[]).append(dict(r))
        return out

    def save_supply_calibrations(self, shop_id: int, as_of_date: str, rows: Iterable[dict[str,Any]], *, method_version: str) -> int:
        now=utcnow(); saved=0
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for r in rows:
                c.execute("""INSERT INTO supply_calibration_snapshots(
                    shop_id,product_id,as_of_date,generated_at,forecast_samples,forecast_wape_pct,forecast_bias_pct,
                    inventory_samples,zero_stock_rate_pct,inbound_delay_samples,avg_inbound_delay_days,p75_inbound_delay_days,
                    lead_buffer_days,safety_buffer_days,confidence,details_json,method_version)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(shop_id,product_id,as_of_date,method_version) DO UPDATE SET
                    generated_at=excluded.generated_at,forecast_samples=excluded.forecast_samples,
                    forecast_wape_pct=excluded.forecast_wape_pct,forecast_bias_pct=excluded.forecast_bias_pct,
                    inventory_samples=excluded.inventory_samples,zero_stock_rate_pct=excluded.zero_stock_rate_pct,
                    inbound_delay_samples=excluded.inbound_delay_samples,avg_inbound_delay_days=excluded.avg_inbound_delay_days,
                    p75_inbound_delay_days=excluded.p75_inbound_delay_days,lead_buffer_days=excluded.lead_buffer_days,
                    safety_buffer_days=excluded.safety_buffer_days,confidence=excluded.confidence,details_json=excluded.details_json""",
                    (shop_id,r['product_id'],as_of_date,now,int(r.get('forecast_samples',0)),r.get('forecast_wape_pct'),
                     r.get('forecast_bias_pct'),int(r.get('inventory_samples',0)),r.get('zero_stock_rate_pct'),
                     int(r.get('inbound_delay_samples',0)),r.get('avg_inbound_delay_days'),r.get('p75_inbound_delay_days'),
                     int(r.get('lead_buffer_days',0)),int(r.get('safety_buffer_days',0)),str(r.get('confidence','low')),
                     canonical_json(r.get('details') or {}),method_version))
                saved += 1
            c.commit()
        return saved

    def latest_supply_calibrations(self, shop_id: int, as_of_date: str | None=None) -> list[dict[str,Any]]:
        with self.db.connect() as c:
            if as_of_date is None:
                row=c.execute('SELECT MAX(as_of_date) d FROM supply_calibration_snapshots WHERE shop_id=?',(shop_id,)).fetchone()
                as_of_date=str(row['d']) if row and row['d'] else None
            if not as_of_date: return []
            rows=c.execute("""SELECT s.*,p.internal_sku,p.name FROM supply_calibration_snapshots s
                JOIN products p ON p.id=s.product_id WHERE s.shop_id=? AND s.as_of_date=?
                ORDER BY CASE s.confidence WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                         s.safety_buffer_days DESC,s.lead_buffer_days DESC,p.internal_sku""",
                (shop_id,as_of_date)).fetchall()
        return [dict(r) for r in rows]



    # --- promotion calendars / forecast calibration ---------------------
    def listing_by_offer_id(self, connection_id: int, offer_id: str):
        clean=str(offer_id or '').strip()
        if not clean: return None
        with self.db.connect() as c:
            r=c.execute('SELECT * FROM product_listings WHERE connection_id=? AND offer_id=? ORDER BY id LIMIT 1',
                        (connection_id,clean)).fetchone()
        return ProductListing(r['id'],r['product_id'],r['connection_id'],r['marketplace_sku'],r['offer_id']) if r else None

    def upsert_promotions(self, connection_id: int, source_run_id: int, marketplace: str,
                          promotions: Iterable[dict[str,Any]], *, complete_external_ids: Iterable[str] | None=None) -> int:
        """Persist a promotion calendar and exact SKU participation.

        ``complete_external_ids`` must only be supplied for a complete calendar
        response. Promotions missing from a complete refresh are marked inactive;
        a partial/failed response can therefore never erase good state.
        """
        rows=list(promotions); complete={str(x) for x in complete_external_ids} if complete_external_ids is not None else None
        now=utcnow(); saved=0
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for promo in rows:
                external=str(promo.get('external_promotion_id') or '').strip()
                if not external: continue
                c.execute("""INSERT INTO promotions(connection_id,marketplace,external_promotion_id,name,promo_type,
                    start_at,end_at,source_run_id,metadata_json,active,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,1,?) ON CONFLICT(connection_id,external_promotion_id) DO UPDATE SET
                    marketplace=excluded.marketplace,name=excluded.name,promo_type=excluded.promo_type,
                    start_at=excluded.start_at,end_at=excluded.end_at,source_run_id=excluded.source_run_id,
                    metadata_json=excluded.metadata_json,active=1,updated_at=excluded.updated_at""",
                    (connection_id,marketplace,external,str(promo.get('name') or ''),str(promo.get('promo_type') or ''),
                     promo.get('start_at'),promo.get('end_at'),source_run_id,canonical_json(promo.get('metadata') or {}),now))
                pr=c.execute('SELECT id FROM promotions WHERE connection_id=? AND external_promotion_id=?',
                             (connection_id,external)).fetchone(); promotion_id=int(pr['id'])
                seen=[]
                for item in promo.get('products') or []:
                    sku=str(item.get('marketplace_sku') or '').strip()
                    offer=str(item.get('offer_id') or '').strip()
                    if not sku and not offer: continue
                    lookup_sku=sku or offer; seen.append(lookup_sku)
                    listing=None
                    if sku:
                        listing=c.execute('SELECT id FROM product_listings WHERE connection_id=? AND marketplace_sku=? ORDER BY id LIMIT 1',
                                          (connection_id,sku)).fetchone()
                    if listing is None and offer:
                        listing=c.execute('SELECT id FROM product_listings WHERE connection_id=? AND offer_id=? ORDER BY id LIMIT 1',
                                          (connection_id,offer)).fetchone()
                    c.execute("""INSERT INTO promotion_products(promotion_id,listing_id,marketplace_sku,in_action,
                        base_price,promo_price,discount_pct,metadata_json,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(promotion_id,marketplace_sku) DO UPDATE SET
                        listing_id=COALESCE(excluded.listing_id,promotion_products.listing_id),in_action=excluded.in_action,
                        base_price=excluded.base_price,promo_price=excluded.promo_price,discount_pct=excluded.discount_pct,
                        metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
                        (promotion_id,int(listing['id']) if listing else None,lookup_sku,1 if item.get('in_action',True) else 0,
                         item.get('base_price'),item.get('promo_price'),item.get('discount_pct'),
                         canonical_json(item.get('metadata') or {}),now))
                if seen:
                    ph=','.join('?' for _ in seen)
                    c.execute(f'DELETE FROM promotion_products WHERE promotion_id=? AND marketplace_sku NOT IN ({ph})',
                              (promotion_id,*seen))
                elif promo.get('products_complete'):
                    c.execute('DELETE FROM promotion_products WHERE promotion_id=?',(promotion_id,))
                saved += 1
            if complete is not None:
                if complete:
                    ph=','.join('?' for _ in complete)
                    c.execute(f'UPDATE promotions SET active=0,updated_at=? WHERE connection_id=? AND external_promotion_id NOT IN ({ph})',
                              (now,connection_id,*sorted(complete)))
                else:
                    c.execute('UPDATE promotions SET active=0,updated_at=? WHERE connection_id=?',(now,connection_id))
            c.commit()
        return saved

    def promotions_for_shop(self, shop_id: int, start_date: str, end_date: str) -> list[dict[str,Any]]:
        with self.db.connect() as c:
            rows=c.execute("""SELECT pr.*,mc.marketplace,
                COUNT(pp.id) product_rows,SUM(CASE WHEN pp.in_action=1 THEN 1 ELSE 0 END) participating_products,
                SUM(CASE WHEN pp.in_action=1 AND pp.listing_id IS NOT NULL THEN 1 ELSE 0 END) linked_products
                FROM promotions pr JOIN marketplace_connections mc ON mc.id=pr.connection_id
                LEFT JOIN promotion_products pp ON pp.promotion_id=pr.id
                WHERE mc.shop_id=? AND pr.active=1
                  AND COALESCE(substr(pr.end_at,1,10),'9999-12-31')>=?
                  AND COALESCE(substr(pr.start_at,1,10),'0001-01-01')<=?
                GROUP BY pr.id ORDER BY pr.start_at,mc.marketplace,pr.name""",
                (shop_id,start_date,end_date)).fetchall()
        return [dict(r) for r in rows]

    def promotion_products_for_shop(self, shop_id: int, start_date: str, end_date: str) -> list[dict[str,Any]]:
        with self.db.connect() as c:
            rows=c.execute("""SELECT pr.marketplace,pr.external_promotion_id,pr.name promotion_name,pr.promo_type,
                pr.start_at,pr.end_at,pp.marketplace_sku,pp.in_action,pp.base_price,pp.promo_price,pp.discount_pct,
                pp.listing_id,pl.product_id,p.internal_sku,p.name product_name
                FROM promotion_products pp JOIN promotions pr ON pr.id=pp.promotion_id
                JOIN marketplace_connections mc ON mc.id=pr.connection_id
                LEFT JOIN product_listings pl ON pl.id=pp.listing_id
                LEFT JOIN products p ON p.id=pl.product_id
                WHERE mc.shop_id=? AND pr.active=1 AND pp.in_action=1
                  AND COALESCE(substr(pr.end_at,1,10),'9999-12-31')>=?
                  AND COALESCE(substr(pr.start_at,1,10),'0001-01-01')<=?
                ORDER BY pr.start_at,pr.marketplace,pr.name,pp.marketplace_sku""",
                (shop_id,start_date,end_date)).fetchall()
        return [dict(r) for r in rows]

    def promotion_dates_by_product(self, shop_id: int, start_date: str, end_date: str) -> dict[int,set[str]]:
        start=date.fromisoformat(start_date); end=date.fromisoformat(end_date); out: dict[int,set[str]]={}
        for row in self.promotion_products_for_shop(shop_id,start_date,end_date):
            if row.get('product_id') is None: continue
            try:
                a=date.fromisoformat(str(row.get('start_at') or start_date)[:10]); b=date.fromisoformat(str(row.get('end_at') or end_date)[:10])
            except ValueError: continue
            a=max(start,a); b=min(end,b)
            if b<a: continue
            bucket=out.setdefault(int(row['product_id']),set())
            cur=a
            while cur<=b:
                bucket.add(cur.isoformat()); cur += timedelta(days=1)
        return out

    def forecast_bias_corrections(self, shop_id: int, *, limit_per_product: int=12) -> dict[int,float]:
        """Return damped correction factors derived only from already evaluated forecasts."""
        with self.db.connect() as c:
            rows=c.execute("""SELECT q.product_id,q.predicted_units,q.actual_units,q.evaluated_at
                FROM forecast_quality_snapshots q WHERE q.shop_id=? AND q.product_id IS NOT NULL
                ORDER BY q.product_id,q.evaluated_at DESC,q.id DESC""",(shop_id,)).fetchall()
        grouped: dict[int,list[tuple[float,float]]]={}
        for r in rows:
            pid=int(r['product_id']); bucket=grouped.setdefault(pid,[])
            if len(bucket)<max(1,int(limit_per_product)):
                bucket.append((float(r['predicted_units'] or 0),float(r['actual_units'] or 0)))
        out={}
        for pid,vals in grouped.items():
            usable=[x for x in vals if x[1]>0]
            if len(usable)<3: continue
            predicted=sum(x[0] for x in usable); actual=sum(x[1] for x in usable)
            if actual<=0: continue
            bias=(predicted-actual)/actual
            # Correct only half of the observed systematic bias, bounded to ±15%.
            out[pid]=max(0.85,min(1.15,1.0-0.5*bias))
        return out

    # --- Action Center workflow ------------------------------------------
    def sync_action_center(self, shop_id: int, as_of_date: str, items: Iterable[dict[str,Any]]) -> None:
        rows=list(items); now=utcnow(); active={str(x['action_key']) for x in rows}
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for item in rows:
                key=str(item['action_key']); evidence=canonical_json(item.get('evidence') or {})
                old=c.execute('SELECT status,snoozed_until FROM action_center_state WHERE shop_id=? AND action_key=?',(shop_id,key)).fetchone()
                status='open'
                snooze=None
                if old:
                    status=str(old['status']); snooze=old['snoozed_until']
                    if status=='resolved': status='open'
                    if status=='snoozed' and snooze:
                        try:
                            if datetime.fromisoformat(str(snooze)) <= datetime.now(timezone.utc): status='open'; snooze=None
                        except ValueError:
                            status='open'; snooze=None
                c.execute("""INSERT INTO action_center_state(shop_id,action_key,status,snoozed_until,first_seen_at,last_seen_at,
                    last_priority,last_category,last_title,last_detail,last_evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(shop_id,action_key) DO UPDATE SET status=excluded.status,snoozed_until=excluded.snoozed_until,
                    last_seen_at=excluded.last_seen_at,resolved_at=NULL,last_priority=excluded.last_priority,last_category=excluded.last_category,
                    last_title=excluded.last_title,last_detail=excluded.last_detail,last_evidence_json=excluded.last_evidence_json""",
                    (shop_id,key,status,snooze,now,now,int(item['priority']),str(item['category']),str(item['title']),str(item['detail']),evidence))
                c.execute("""INSERT INTO action_center_history(shop_id,action_key,as_of_date,priority,category,title,detail,evidence_json,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(shop_id,action_key,as_of_date) DO UPDATE SET
                    priority=excluded.priority,category=excluded.category,title=excluded.title,detail=excluded.detail,evidence_json=excluded.evidence_json""",
                    (shop_id,key,as_of_date,int(item['priority']),str(item['category']),str(item['title']),str(item['detail']),evidence,now))
            current=c.execute("SELECT action_key FROM action_center_state WHERE shop_id=? AND status<>'resolved'",(shop_id,)).fetchall()
            for r in current:
                key=str(r['action_key'])
                if key not in active:
                    c.execute("UPDATE action_center_state SET status='resolved',resolved_at=?,last_seen_at=? WHERE shop_id=? AND action_key=?",(now,now,shop_id,key))
            c.commit()

    def action_states(self, shop_id: int, *, include_resolved: bool=False, limit: int=100) -> list[dict[str,Any]]:
        sql='SELECT * FROM action_center_state WHERE shop_id=?'
        params=[shop_id]
        if not include_resolved: sql += " AND status<>'resolved'"
        sql += " ORDER BY last_priority,last_seen_at DESC LIMIT ?"; params.append(limit)
        with self.db.connect() as c: rows=c.execute(sql,params).fetchall()
        return [dict(r) for r in rows]

    def set_action_status(self, shop_id: int, action_key: str, status: str, *, telegram_user_id: int | None=None, snooze_hours: int | None=None) -> bool:
        if status not in {'open','acknowledged','snoozed'}: raise ValueError('unsupported action status')
        snooze=None
        if status=='snoozed':
            hours=max(1,min(int(snooze_hours or 24),720))
            snooze=(datetime.now(timezone.utc)+timedelta(hours=hours)).isoformat(timespec='seconds')
        with self.db.connect() as c:
            cur=c.execute("""UPDATE action_center_state SET status=?,snoozed_until=?,acknowledged_by=?
                WHERE shop_id=? AND action_key=? AND status<>'resolved'""",
                (status,snooze,telegram_user_id,shop_id,str(action_key)))
        return cur.rowcount>0

    def action_history(self, shop_id: int, start_date: str, end_date: str, limit: int=200) -> list[dict[str,Any]]:
        with self.db.connect() as c:
            rows=c.execute("""SELECT h.*,s.status current_status,s.snoozed_until FROM action_center_history h
                LEFT JOIN action_center_state s ON s.shop_id=h.shop_id AND s.action_key=h.action_key
                WHERE h.shop_id=? AND h.as_of_date BETWEEN ? AND ?
                ORDER BY h.as_of_date DESC,h.priority,h.id DESC LIMIT ?""",(shop_id,start_date,end_date,limit)).fetchall()
        return [dict(r) for r in rows]

