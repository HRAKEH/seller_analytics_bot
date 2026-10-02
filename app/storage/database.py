"""SQLite connection and built-in schema migrations.

The starter remains dependency-light and portable for BotHost. The schema is
multi-seller/multi-shop from day one and can later be migrated to PostgreSQL.
"""
from __future__ import annotations
import sqlite3
import os
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable

Migration = Callable[[sqlite3.Connection], None]


@contextmanager
def _migration_file_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True,exist_ok=True)
    fh=open(lock_path,'a+b')
    try:
        try:
            import fcntl
            fcntl.flock(fh.fileno(),fcntl.LOCK_EX)
        except (ImportError,OSError):
            pass
        yield
    finally:
        try:
            import fcntl
            fcntl.flock(fh.fileno(),fcntl.LOCK_UN)
        except (ImportError,OSError):
            pass
        fh.close()


def _migration_1(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE sellers (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      telegram_user_id INTEGER NOT NULL UNIQUE,
      name TEXT NOT NULL,
      timezone TEXT NOT NULL DEFAULT 'Europe/Moscow',
      active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
      created_at TEXT NOT NULL
    );

    CREATE TABLE shops (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
      name TEXT NOT NULL,
      currency TEXT NOT NULL DEFAULT 'RUB',
      active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
      created_at TEXT NOT NULL,
      UNIQUE(seller_id, name)
    );

    CREATE TABLE marketplace_connections (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
      marketplace TEXT NOT NULL CHECK(marketplace IN ('ozon','wildberries')),
      display_name TEXT NOT NULL,
      external_account_id TEXT,
      enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
      metadata_json TEXT NOT NULL DEFAULT '{}',
      created_at TEXT NOT NULL,
      UNIQUE(shop_id, marketplace, display_name)
    );

    CREATE TABLE source_runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      connection_id INTEGER NOT NULL REFERENCES marketplace_connections(id) ON DELETE CASCADE,
      endpoint TEXT NOT NULL,
      data_date TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('success','partial','failed')),
      started_at TEXT NOT NULL,
      finished_at TEXT,
      error TEXT,
      http_status INTEGER,
      attempts INTEGER NOT NULL DEFAULT 1,
      payload_hash TEXT,
      created_at TEXT NOT NULL
    );
    CREATE INDEX idx_source_runs_lookup
      ON source_runs(connection_id, data_date, endpoint, status, finished_at);
    CREATE UNIQUE INDEX idx_source_runs_success_payload
      ON source_runs(connection_id, data_date, endpoint, payload_hash)
      WHERE status='success' AND payload_hash IS NOT NULL;

    CREATE TABLE raw_payloads (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      source_run_id INTEGER NOT NULL UNIQUE REFERENCES source_runs(id) ON DELETE CASCADE,
      payload_json TEXT NOT NULL,
      created_at TEXT NOT NULL
    );

    CREATE TABLE metric_values (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      connection_id INTEGER NOT NULL REFERENCES marketplace_connections(id) ON DELETE CASCADE,
      data_date TEXT NOT NULL,
      metric_key TEXT NOT NULL,
      value REAL NOT NULL,
      unit TEXT NOT NULL,
      is_preliminary INTEGER NOT NULL DEFAULT 0 CHECK(is_preliminary IN (0,1)),
      as_of TEXT,
      fetched_at TEXT NOT NULL,
      source_run_id INTEGER NOT NULL REFERENCES source_runs(id) ON DELETE CASCADE,
      UNIQUE(source_run_id, metric_key)
    );
    CREATE INDEX idx_metric_latest
      ON metric_values(connection_id, data_date, metric_key, fetched_at DESC);

    CREATE TABLE products (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
      internal_sku TEXT NOT NULL,
      name TEXT NOT NULL,
      cost_price REAL,
      active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
      created_at TEXT NOT NULL,
      UNIQUE(shop_id, internal_sku)
    );

    CREATE TABLE product_listings (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
      connection_id INTEGER NOT NULL REFERENCES marketplace_connections(id) ON DELETE CASCADE,
      marketplace_sku TEXT NOT NULL,
      offer_id TEXT,
      created_at TEXT NOT NULL,
      UNIQUE(connection_id, marketplace_sku)
    );
    """)


def _migration_2(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE product_metric_values (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      listing_id INTEGER NOT NULL REFERENCES product_listings(id) ON DELETE CASCADE,
      data_date TEXT NOT NULL,
      metric_key TEXT NOT NULL,
      value REAL NOT NULL,
      unit TEXT NOT NULL,
      fulfillment_scheme TEXT NOT NULL DEFAULT 'ALL',
      is_preliminary INTEGER NOT NULL DEFAULT 0 CHECK(is_preliminary IN (0,1)),
      as_of TEXT,
      fetched_at TEXT NOT NULL,
      source_run_id INTEGER NOT NULL REFERENCES source_runs(id) ON DELETE CASCADE,
      UNIQUE(source_run_id, listing_id, metric_key, fulfillment_scheme)
    );
    CREATE INDEX idx_product_metric_latest
      ON product_metric_values(listing_id, data_date, metric_key, fulfillment_scheme, fetched_at DESC);

    CREATE TABLE inventory_snapshots (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      listing_id INTEGER NOT NULL REFERENCES product_listings(id) ON DELETE CASCADE,
      captured_at TEXT NOT NULL,
      available_units REAL NOT NULL,
      reserved_units REAL NOT NULL DEFAULT 0,
      fulfillment_scheme TEXT NOT NULL DEFAULT 'ALL',
      warehouse_name TEXT NOT NULL DEFAULT 'ALL',
      as_of TEXT,
      source_run_id INTEGER NOT NULL REFERENCES source_runs(id) ON DELETE CASCADE,
      UNIQUE(source_run_id, listing_id, fulfillment_scheme, warehouse_name)
    );
    CREATE INDEX idx_inventory_latest
      ON inventory_snapshots(listing_id, captured_at DESC, fulfillment_scheme);
    """)


def _migration_3(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE alert_rules (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
      rule_key TEXT NOT NULL,
      enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
      threshold REAL,
      lookback_days INTEGER,
      cooldown_minutes INTEGER NOT NULL DEFAULT 1440,
      config_json TEXT NOT NULL DEFAULT '{}',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE(shop_id, rule_key)
    );

    CREATE TABLE alert_state (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
      rule_key TEXT NOT NULL,
      subject_key TEXT NOT NULL DEFAULT 'shop',
      active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0,1)),
      last_value REAL,
      fingerprint TEXT,
      first_triggered_at TEXT,
      last_triggered_at TEXT,
      last_notified_at TEXT,
      last_resolved_at TEXT,
      updated_at TEXT NOT NULL,
      UNIQUE(shop_id, rule_key, subject_key)
    );
    CREATE INDEX idx_alert_state_active ON alert_state(shop_id, active, rule_key);

    CREATE TABLE alert_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
      rule_key TEXT NOT NULL,
      subject_key TEXT NOT NULL DEFAULT 'shop',
      severity TEXT NOT NULL CHECK(severity IN ('info','warning','critical','resolved')),
      message TEXT NOT NULL,
      value REAL,
      fingerprint TEXT,
      created_at TEXT NOT NULL
    );
    CREATE INDEX idx_alert_events_recent ON alert_events(shop_id, created_at DESC);
    """)


def _migration_4(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE shop_preferences (
      shop_id INTEGER PRIMARY KEY REFERENCES shops(id) ON DELETE CASCADE,
      timezone TEXT NOT NULL DEFAULT 'Europe/Moscow',
      report_time TEXT NOT NULL DEFAULT '09:00',
      product_report_days INTEGER NOT NULL DEFAULT 7,
      stock_velocity_days INTEGER NOT NULL DEFAULT 14,
      stock_risk_days INTEGER NOT NULL DEFAULT 14,
      finance_lookback_days INTEGER NOT NULL DEFAULT 14,
      alerts_enabled INTEGER NOT NULL DEFAULT 1 CHECK(alerts_enabled IN (0,1)),
      alerts_interval_minutes INTEGER NOT NULL DEFAULT 60,
      alert_order_drop_pct REAL NOT NULL DEFAULT 35,
      alert_order_lookback_days INTEGER NOT NULL DEFAULT 7,
      alert_api_stale_hours REAL NOT NULL DEFAULT 26,
      alert_drr_pct REAL NOT NULL DEFAULT 25,
      alert_cooldown_minutes INTEGER NOT NULL DEFAULT 1440,
      setup_completed INTEGER NOT NULL DEFAULT 0 CHECK(setup_completed IN (0,1)),
      updated_at TEXT NOT NULL
    );

    CREATE TABLE import_batches (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
      import_kind TEXT NOT NULL,
      filename TEXT,
      status TEXT NOT NULL CHECK(status IN ('success','partial','failed')),
      total_rows INTEGER NOT NULL DEFAULT 0,
      applied_rows INTEGER NOT NULL DEFAULT 0,
      error_rows INTEGER NOT NULL DEFAULT 0,
      error_json TEXT NOT NULL DEFAULT '[]',
      created_at TEXT NOT NULL
    );
    CREATE INDEX idx_import_batches_shop ON import_batches(shop_id, created_at DESC);

    CREATE TABLE product_cost_history (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
      effective_date TEXT NOT NULL,
      cost_price REAL NOT NULL CHECK(cost_price >= 0),
      source TEXT NOT NULL DEFAULT 'manual',
      import_batch_id INTEGER REFERENCES import_batches(id) ON DELETE SET NULL,
      created_at TEXT NOT NULL,
      UNIQUE(product_id, effective_date)
    );
    CREATE INDEX idx_cost_history_lookup ON product_cost_history(product_id, effective_date DESC);

    INSERT INTO product_cost_history(product_id,effective_date,cost_price,source,created_at)
      SELECT id,'1970-01-01',cost_price,'legacy',datetime('now') FROM products WHERE cost_price IS NOT NULL;
    """)


def _migration_5(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE commerce_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      connection_id INTEGER NOT NULL REFERENCES marketplace_connections(id) ON DELETE CASCADE,
      listing_id INTEGER REFERENCES product_listings(id) ON DELETE SET NULL,
      data_date TEXT NOT NULL,
      event_time TEXT,
      event_kind TEXT NOT NULL CHECK(event_kind IN ('order','cancel','posting','sale','return','finance')),
      external_order_id TEXT,
      external_event_id TEXT,
      quantity REAL NOT NULL DEFAULT 0,
      gross_amount REAL NOT NULL DEFAULT 0,
      net_amount REAL,
      fulfillment_scheme TEXT NOT NULL DEFAULT 'ALL',
      is_preliminary INTEGER NOT NULL DEFAULT 0 CHECK(is_preliminary IN (0,1)),
      source_name TEXT NOT NULL,
      source_run_id INTEGER NOT NULL REFERENCES source_runs(id) ON DELETE CASCADE,
      metadata_json TEXT NOT NULL DEFAULT '{}',
      fingerprint TEXT NOT NULL,
      created_at TEXT NOT NULL,
      UNIQUE(connection_id, fingerprint)
    );
    CREATE INDEX idx_commerce_events_period
      ON commerce_events(connection_id, data_date, event_kind);
    CREATE INDEX idx_commerce_events_order
      ON commerce_events(connection_id, external_order_id, event_kind);
    CREATE INDEX idx_commerce_events_listing
      ON commerce_events(listing_id, data_date, event_kind);
    """)


def _migration_6(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    ALTER TABLE shops ADD COLUMN credential_profile TEXT NOT NULL DEFAULT 'DEFAULT';

    CREATE TABLE user_shop_selection (
      telegram_user_id INTEGER PRIMARY KEY,
      shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
      updated_at TEXT NOT NULL
    );

    CREATE TABLE shop_job_state (
      shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
      job_key TEXT NOT NULL,
      last_run_key TEXT,
      updated_at TEXT NOT NULL,
      PRIMARY KEY(shop_id, job_key)
    );

    CREATE TABLE backup_history (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      kind TEXT NOT NULL CHECK(kind IN ('manual','automatic','pre_restore','restore')),
      filename TEXT NOT NULL,
      checksum TEXT,
      size_bytes INTEGER NOT NULL DEFAULT 0,
      schema_version INTEGER NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('success','failed')),
      message TEXT,
      created_at TEXT NOT NULL
    );
    CREATE INDEX idx_backup_history_recent ON backup_history(created_at DESC);
    """)



def _migration_7(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE bot_users (
      telegram_user_id INTEGER PRIMARY KEY,
      display_name TEXT NOT NULL DEFAULT '',
      active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );

    CREATE TABLE user_shop_access (
      telegram_user_id INTEGER NOT NULL REFERENCES bot_users(telegram_user_id) ON DELETE CASCADE,
      shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
      role TEXT NOT NULL CHECK(role IN ('owner','analyst','viewer')),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY(telegram_user_id, shop_id)
    );
    CREATE INDEX idx_user_shop_access_shop ON user_shop_access(shop_id, role, telegram_user_id);

    CREATE TABLE ad_campaign_daily (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      connection_id INTEGER NOT NULL REFERENCES marketplace_connections(id) ON DELETE CASCADE,
      data_date TEXT NOT NULL,
      campaign_id TEXT NOT NULL,
      campaign_name TEXT NOT NULL DEFAULT '',
      spend REAL NOT NULL DEFAULT 0,
      attributed_sales REAL NOT NULL DEFAULT 0,
      orders REAL NOT NULL DEFAULT 0,
      clicks REAL NOT NULL DEFAULT 0,
      impressions REAL NOT NULL DEFAULT 0,
      source_run_id INTEGER NOT NULL REFERENCES source_runs(id) ON DELETE CASCADE,
      fetched_at TEXT NOT NULL,
      UNIQUE(source_run_id, campaign_id, data_date)
    );
    CREATE INDEX idx_ad_campaign_period ON ad_campaign_daily(connection_id, data_date, campaign_id);

    CREATE TABLE ad_product_daily (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      connection_id INTEGER NOT NULL REFERENCES marketplace_connections(id) ON DELETE CASCADE,
      listing_id INTEGER REFERENCES product_listings(id) ON DELETE SET NULL,
      marketplace_sku TEXT NOT NULL,
      data_date TEXT NOT NULL,
      campaign_id TEXT NOT NULL DEFAULT '',
      campaign_name TEXT NOT NULL DEFAULT '',
      spend REAL NOT NULL DEFAULT 0,
      attributed_sales REAL NOT NULL DEFAULT 0,
      orders REAL NOT NULL DEFAULT 0,
      clicks REAL NOT NULL DEFAULT 0,
      impressions REAL NOT NULL DEFAULT 0,
      source_run_id INTEGER NOT NULL REFERENCES source_runs(id) ON DELETE CASCADE,
      fetched_at TEXT NOT NULL,
      UNIQUE(source_run_id, marketplace_sku, campaign_id, data_date)
    );
    CREATE INDEX idx_ad_product_period ON ad_product_daily(connection_id, data_date, marketplace_sku);
    CREATE INDEX idx_ad_product_listing ON ad_product_daily(listing_id, data_date);
    """)



def _migration_8(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE runtime_leases (
      lease_key TEXT PRIMARY KEY,
      owner_id TEXT NOT NULL,
      acquired_at TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      metadata_json TEXT NOT NULL DEFAULT '{}',
      updated_at TEXT NOT NULL
    );
    CREATE INDEX idx_runtime_leases_expiry ON runtime_leases(expires_at);

    CREATE TABLE retry_jobs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
      job_type TEXT NOT NULL,
      unique_key TEXT NOT NULL,
      payload_json TEXT NOT NULL DEFAULT '{}',
      status TEXT NOT NULL CHECK(status IN ('pending','running','success','dead')),
      attempts INTEGER NOT NULL DEFAULT 0,
      max_attempts INTEGER NOT NULL DEFAULT 6,
      next_attempt_at TEXT NOT NULL,
      locked_by TEXT,
      locked_until TEXT,
      last_error TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE(shop_id, job_type, unique_key)
    );
    CREATE INDEX idx_retry_jobs_due ON retry_jobs(status,next_attempt_at,locked_until);

    CREATE TABLE process_heartbeats (
      instance_id TEXT PRIMARY KEY,
      role TEXT NOT NULL DEFAULT 'bot',
      hostname TEXT NOT NULL DEFAULT '',
      pid INTEGER,
      started_at TEXT NOT NULL,
      heartbeat_at TEXT NOT NULL,
      metadata_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX idx_process_heartbeats_recent ON process_heartbeats(heartbeat_at DESC);
    """)



def _migration_9(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE shop_supply_preferences (
      shop_id INTEGER PRIMARY KEY REFERENCES shops(id) ON DELETE CASCADE,
      lookback_days INTEGER NOT NULL DEFAULT 56,
      xyz_weeks INTEGER NOT NULL DEFAULT 8,
      default_lead_time_days INTEGER NOT NULL DEFAULT 14,
      default_safety_stock_days INTEGER NOT NULL DEFAULT 7,
      default_target_stock_days INTEGER NOT NULL DEFAULT 30,
      min_history_days INTEGER NOT NULL DEFAULT 14,
      updated_at TEXT NOT NULL
    );

    CREATE TABLE product_supply_settings (
      product_id INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
      lead_time_days INTEGER,
      safety_stock_days INTEGER,
      target_stock_days INTEGER,
      pack_size REAL,
      min_order_qty REAL,
      updated_at TEXT NOT NULL
    );

    CREATE TABLE supply_recommendation_snapshots (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
      product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
      as_of_date TEXT NOT NULL,
      generated_at TEXT NOT NULL,
      abc_class TEXT NOT NULL,
      xyz_class TEXT NOT NULL,
      avg_daily_units REAL NOT NULL DEFAULT 0,
      forecast_daily_units REAL NOT NULL DEFAULT 0,
      trend_pct REAL,
      available_units REAL,
      days_cover REAL,
      reorder_point_units REAL NOT NULL DEFAULT 0,
      target_units REAL NOT NULL DEFAULT 0,
      recommended_order_units REAL NOT NULL DEFAULT 0,
      confidence TEXT NOT NULL,
      history_days INTEGER NOT NULL DEFAULT 0,
      method_version TEXT NOT NULL DEFAULT 'wma-v1',
      UNIQUE(shop_id,product_id,as_of_date,method_version)
    );
    CREATE INDEX idx_supply_snapshot_shop ON supply_recommendation_snapshots(shop_id,as_of_date,recommended_order_units DESC);
    """)


def _migration_10(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    ALTER TABLE shop_supply_preferences ADD COLUMN seasonality_enabled INTEGER NOT NULL DEFAULT 1 CHECK(seasonality_enabled IN (0,1));
    ALTER TABLE shop_supply_preferences ADD COLUMN forecast_horizon_days INTEGER NOT NULL DEFAULT 7;

    CREATE TABLE inbound_shipments (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      connection_id INTEGER NOT NULL REFERENCES marketplace_connections(id) ON DELETE CASCADE,
      marketplace TEXT NOT NULL,
      external_supply_id TEXT NOT NULL,
      status TEXT NOT NULL,
      planned_at TEXT,
      arrival_at TEXT,
      warehouse_name TEXT NOT NULL DEFAULT '',
      source_run_id INTEGER NOT NULL REFERENCES source_runs(id) ON DELETE CASCADE,
      metadata_json TEXT NOT NULL DEFAULT '{}',
      updated_at TEXT NOT NULL,
      UNIQUE(connection_id, external_supply_id)
    );
    CREATE INDEX idx_inbound_shipments_active ON inbound_shipments(connection_id,status,planned_at);

    CREATE TABLE inbound_shipment_items (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      shipment_id INTEGER NOT NULL REFERENCES inbound_shipments(id) ON DELETE CASCADE,
      listing_id INTEGER REFERENCES product_listings(id) ON DELETE SET NULL,
      marketplace_sku TEXT NOT NULL,
      planned_units REAL NOT NULL DEFAULT 0,
      accepted_units REAL NOT NULL DEFAULT 0,
      remaining_units REAL NOT NULL DEFAULT 0,
      updated_at TEXT NOT NULL,
      UNIQUE(shipment_id, marketplace_sku)
    );
    CREATE INDEX idx_inbound_items_listing ON inbound_shipment_items(listing_id,shipment_id);

    CREATE TABLE forecast_quality_snapshots (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
      product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
      as_of_date TEXT NOT NULL,
      horizon_days INTEGER NOT NULL,
      predicted_units REAL NOT NULL,
      actual_units REAL NOT NULL,
      absolute_error REAL NOT NULL,
      ape_pct REAL,
      method_version TEXT NOT NULL,
      evaluated_at TEXT NOT NULL,
      UNIQUE(shop_id,product_id,as_of_date,horizon_days,method_version)
    );
    CREATE INDEX idx_forecast_quality_shop ON forecast_quality_snapshots(shop_id,evaluated_at DESC);
    """)



def _migration_11(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE promotions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      connection_id INTEGER NOT NULL REFERENCES marketplace_connections(id) ON DELETE CASCADE,
      marketplace TEXT NOT NULL,
      external_promotion_id TEXT NOT NULL,
      name TEXT NOT NULL DEFAULT '',
      promo_type TEXT NOT NULL DEFAULT '',
      start_at TEXT,
      end_at TEXT,
      source_run_id INTEGER NOT NULL REFERENCES source_runs(id) ON DELETE CASCADE,
      metadata_json TEXT NOT NULL DEFAULT '{}',
      active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
      updated_at TEXT NOT NULL,
      UNIQUE(connection_id, external_promotion_id)
    );
    CREATE INDEX idx_promotions_window ON promotions(connection_id,start_at,end_at,active);

    CREATE TABLE promotion_products (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      promotion_id INTEGER NOT NULL REFERENCES promotions(id) ON DELETE CASCADE,
      listing_id INTEGER REFERENCES product_listings(id) ON DELETE SET NULL,
      marketplace_sku TEXT NOT NULL,
      in_action INTEGER NOT NULL DEFAULT 1 CHECK(in_action IN (0,1)),
      base_price REAL,
      promo_price REAL,
      discount_pct REAL,
      metadata_json TEXT NOT NULL DEFAULT '{}',
      updated_at TEXT NOT NULL,
      UNIQUE(promotion_id, marketplace_sku)
    );
    CREATE INDEX idx_promotion_products_listing ON promotion_products(listing_id,promotion_id,in_action);

    ALTER TABLE supply_recommendation_snapshots ADD COLUMN bias_correction REAL NOT NULL DEFAULT 1;
    ALTER TABLE supply_recommendation_snapshots ADD COLUMN promo_factor REAL NOT NULL DEFAULT 1;
    ALTER TABLE supply_recommendation_snapshots ADD COLUMN promo_days INTEGER NOT NULL DEFAULT 0;
    """)

def _migration_12(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    ALTER TABLE shop_supply_preferences ADD COLUMN auto_calibration_enabled INTEGER NOT NULL DEFAULT 1 CHECK(auto_calibration_enabled IN (0,1));
    ALTER TABLE shop_supply_preferences ADD COLUMN max_lead_buffer_days INTEGER NOT NULL DEFAULT 7;
    ALTER TABLE shop_supply_preferences ADD COLUMN max_safety_buffer_days INTEGER NOT NULL DEFAULT 7;

    CREATE TABLE supply_calibration_snapshots (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
      product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
      as_of_date TEXT NOT NULL,
      generated_at TEXT NOT NULL,
      forecast_samples INTEGER NOT NULL DEFAULT 0,
      forecast_wape_pct REAL,
      forecast_bias_pct REAL,
      inventory_samples INTEGER NOT NULL DEFAULT 0,
      zero_stock_rate_pct REAL,
      inbound_delay_samples INTEGER NOT NULL DEFAULT 0,
      avg_inbound_delay_days REAL,
      p75_inbound_delay_days REAL,
      lead_buffer_days INTEGER NOT NULL DEFAULT 0,
      safety_buffer_days INTEGER NOT NULL DEFAULT 0,
      confidence TEXT NOT NULL DEFAULT 'low',
      details_json TEXT NOT NULL DEFAULT '{}',
      method_version TEXT NOT NULL DEFAULT 'risk-buffer-v1',
      UNIQUE(shop_id,product_id,as_of_date,method_version)
    );
    CREATE INDEX idx_supply_calibration_shop ON supply_calibration_snapshots(shop_id,as_of_date,confidence);

    ALTER TABLE supply_recommendation_snapshots ADD COLUMN lead_buffer_days INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE supply_recommendation_snapshots ADD COLUMN safety_buffer_days INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE supply_recommendation_snapshots ADD COLUMN effective_lead_time_days INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE supply_recommendation_snapshots ADD COLUMN effective_safety_stock_days INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE supply_recommendation_snapshots ADD COLUMN calibration_confidence TEXT NOT NULL DEFAULT 'none';
    """)


def _migration_13(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE action_center_state (
      shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
      action_key TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','acknowledged','snoozed','resolved')),
      snoozed_until TEXT,
      acknowledged_by INTEGER,
      first_seen_at TEXT NOT NULL,
      last_seen_at TEXT NOT NULL,
      resolved_at TEXT,
      last_priority INTEGER NOT NULL,
      last_category TEXT NOT NULL,
      last_title TEXT NOT NULL,
      last_detail TEXT NOT NULL,
      last_evidence_json TEXT NOT NULL DEFAULT '{}',
      PRIMARY KEY(shop_id,action_key)
    );
    CREATE INDEX idx_action_state_shop_status ON action_center_state(shop_id,status,last_priority,last_seen_at);

    CREATE TABLE action_center_history (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
      action_key TEXT NOT NULL,
      as_of_date TEXT NOT NULL,
      priority INTEGER NOT NULL,
      category TEXT NOT NULL,
      title TEXT NOT NULL,
      detail TEXT NOT NULL,
      evidence_json TEXT NOT NULL DEFAULT '{}',
      created_at TEXT NOT NULL,
      UNIQUE(shop_id,action_key,as_of_date)
    );
    CREATE INDEX idx_action_history_shop ON action_center_history(shop_id,as_of_date,priority);
    """)


def _migration_14(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    ALTER TABLE shop_preferences ADD COLUMN demo_mode INTEGER NOT NULL DEFAULT 0 CHECK(demo_mode IN (0,1));
    ALTER TABLE shop_preferences ADD COLUMN onboarding_version TEXT NOT NULL DEFAULT '';

    CREATE TABLE shop_readiness_snapshots (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
      checked_at TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('ready','partial','blocked')),
      critical_ok INTEGER NOT NULL DEFAULT 0,
      critical_total INTEGER NOT NULL DEFAULT 0,
      optional_ok INTEGER NOT NULL DEFAULT 0,
      optional_total INTEGER NOT NULL DEFAULT 0,
      details_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX idx_readiness_shop ON shop_readiness_snapshots(shop_id,checked_at DESC);
    """)


def _migration_15(conn: sqlite3.Connection) -> None:
    # Repeated historical payloads can represent a new corrected snapshot
    # (A -> B -> A). Adjacent-repeat deduplication is enforced transactionally
    # by Repository.record_success rather than by an all-history unique index.
    conn.execute('DROP INDEX IF EXISTS idx_source_runs_success_payload')
    conn.execute('CREATE INDEX idx_source_runs_version ON source_runs(connection_id,data_date,endpoint,id DESC)')


def _migration_16(conn: sqlite3.Connection) -> None:
    # Public reference rates are separate from marketplace API health/metrics.
    # The original XML and both dates make historical estimates reproducible.
    conn.execute('''CREATE TABLE currency_rate_snapshots (
        source TEXT NOT NULL,
        requested_date TEXT NOT NULL,
        effective_date TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        payload BLOB NOT NULL,
        PRIMARY KEY(source,requested_date)
    )''')


MIGRATIONS: dict[int, Migration] = {1: _migration_1, 2: _migration_2, 3: _migration_3, 4: _migration_4, 5: _migration_5, 6: _migration_6, 7: _migration_7, 8: _migration_8, 9: _migration_9, 10: _migration_10, 11: _migration_11, 12: _migration_12, 13: _migration_13, 14: _migration_14, 15: _migration_15, 16: _migration_16}
LATEST_SCHEMA_VERSION = max(MIGRATIONS)


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            # Business data and backup metadata should not be world-readable on
            # a shared host.  Ignore chmod failures on filesystems that do not
            # expose POSIX permissions.
            self.path.chmod(0o600)
        except OSError:
            pass
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def initialize(self) -> int:
        with self.connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            current = conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]
            for version in sorted(MIGRATIONS):
                if version <= current:
                    continue
                MIGRATIONS[version](conn)
                conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, datetime('now'))", (version,))
            return conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]

    def initialize_safely(self, backups_dir: Path | None = None) -> int:
        """Serialize migrations and rollback the DB file if a migration fails."""
        lock_path=self.path.with_suffix(self.path.suffix+'.migration.lock')
        with _migration_file_lock(lock_path):
            existed_before=self.path.exists()
            current=0
            if existed_before:
                try:
                    with self.connect() as conn:
                        row=conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()
                        current=int(row[0]) if row else 0
                except sqlite3.Error:
                    current=0
            backup_path=None
            if current and current < LATEST_SCHEMA_VERSION and self.path.exists():
                target_dir=backups_dir or (self.path.parent/'backups')
                target_dir.mkdir(parents=True,exist_ok=True)
                stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
                backup_path=target_dir/f'pre_migration_v{current}_{stamp}.sqlite3'
                source=sqlite3.connect(self.path)
                dest=sqlite3.connect(backup_path)
                try: source.backup(dest)
                finally:
                    dest.close(); source.close()
            try:
                version=self.initialize()
                if not self.quick_check():
                    raise RuntimeError('SQLite quick_check failed after migration')
                return version
            except Exception:
                if backup_path and backup_path.exists():
                    # Never copy a SQLite file over a database that has just used WAL.
                    # Rebuild a clean standalone database through SQLite's backup API,
                    # validate it, then atomically replace the failed migration target.
                    recovery_path = self.path.with_name(self.path.name + '.migration-recovery')
                    for candidate in (recovery_path, Path(str(recovery_path) + '-wal'), Path(str(recovery_path) + '-shm')):
                        try:
                            candidate.unlink()
                        except FileNotFoundError:
                            pass
                    source = sqlite3.connect(f'file:{backup_path}?mode=ro', uri=True)
                    dest = sqlite3.connect(recovery_path)
                    try:
                        source.backup(dest)
                        dest.commit()
                        if dest.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                            raise RuntimeError('SQLite recovery backup failed quick_check')
                    finally:
                        dest.close()
                        source.close()
                    for suffix in ('-wal', '-shm'):
                        try:
                            Path(str(self.path) + suffix).unlink()
                        except FileNotFoundError:
                            pass
                    os.replace(recovery_path, self.path)
                    # Validate the restored file without enabling WAL first.
                    check = sqlite3.connect(self.path)
                    try:
                        if check.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                            raise RuntimeError('Restored SQLite database failed quick_check')
                    finally:
                        check.close()
                elif not existed_before:
                    # A failed first install has no pre-migration backup.  Do
                    # not leave a half-created schema that makes the next boot
                    # fail with "table already exists".
                    for candidate in (self.path, Path(str(self.path)+'-wal'), Path(str(self.path)+'-shm')):
                        try:
                            candidate.unlink()
                        except FileNotFoundError:
                            pass
                raise

    def schema_version(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()
            return int(row[0]) if row else 0

    def integrity_check(self) -> bool:
        with self.connect() as conn:
            return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    def quick_check(self) -> bool:
        with self.connect() as conn:
            return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"

    def checkpoint(self, mode: str = "PASSIVE") -> tuple[int, int, int]:
        mode = mode.upper()
        if mode not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
            raise ValueError("invalid checkpoint mode")
        with self.connect() as conn:
            row = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
            return tuple(int(x) for x in row)
