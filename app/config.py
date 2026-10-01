"""Centralized environment configuration."""
from __future__ import annotations
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import socket
import uuid


def _owners(raw: str) -> tuple[int, ...]:
    return tuple(int(x.strip()) for x in raw.split(",") if x.strip().isdigit())


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да"}


@dataclass(frozen=True)
class MarketplaceCredentials:
    profile: str
    ozon_client_id: str = field(default="",repr=False)
    ozon_api_key: str = field(default="",repr=False)
    ozon_perf_client_id: str = field(default="",repr=False)
    ozon_perf_client_secret: str = field(default="",repr=False)
    wb_api_token: str = field(default="",repr=False)

    @property
    def has_ozon(self) -> bool:
        return bool(self.ozon_client_id and self.ozon_api_key)

    @property
    def has_wb(self) -> bool:
        return bool(self.wb_api_token)

    @property
    def has_ozon_performance(self) -> bool:
        return bool(self.ozon_perf_client_id and self.ozon_perf_client_secret)


@dataclass(frozen=True)
class Settings:
    telegram_token: str = field(repr=False)
    owner_ids: tuple[int, ...]
    ozon_client_id: str = field(repr=False)
    ozon_api_key: str = field(repr=False)
    ozon_perf_client_id: str = field(repr=False)
    ozon_perf_client_secret: str = field(repr=False)
    wb_api_token: str = field(repr=False)
    db_file: Path
    timezone: str
    report_time: str
    backfill_days: int
    http_timeout: float
    ozon_min_interval: float
    wb_min_interval: float
    daily_retry_minutes: int
    daily_retry_attempts: int
    product_report_days: int
    stock_velocity_days: int
    stock_risk_days: int
    finance_lookback_days: int
    alerts_enabled: bool
    alerts_interval_minutes: int
    alert_order_drop_pct: float
    alert_order_lookback_days: int
    alert_api_stale_hours: float
    alert_drr_pct: float
    alert_cooldown_minutes: int
    auto_backup_enabled: bool
    auto_backup_hour_utc: int
    backup_retention_days: int
    instance_id: str
    distributed_lock_ttl_seconds: int
    retry_worker_interval_seconds: int
    retry_max_attempts: int
    health_server_enabled: bool
    health_host: str
    health_port: int
    log_format: str
    log_level: str
    auto_backup_send_telegram: bool = False


    def credentials_for_profile(self, profile: str = "DEFAULT") -> MarketplaceCredentials:
        clean=(profile or "DEFAULT").strip().upper()
        if clean == "DEFAULT":
            return MarketplaceCredentials(clean,self.ozon_client_id,self.ozon_api_key,
                self.ozon_perf_client_id,self.ozon_perf_client_secret,self.wb_api_token)
        if not re.fullmatch(r"[A-Z0-9_]{1,64}",clean):
            raise ValueError("Invalid credential profile")
        prefix=f"SELLERBOT_{clean}_"
        return MarketplaceCredentials(
            clean,
            os.getenv(prefix+"OZON_CLIENT_ID",""),
            os.getenv(prefix+"OZON_API_KEY",""),
            os.getenv(prefix+"OZON_PERF_CLIENT_ID",""),
            os.getenv(prefix+"OZON_PERF_CLIENT_SECRET",""),
            os.getenv(prefix+"WB_API_TOKEN",""),
        )

    def available_credential_profiles(self) -> tuple[str, ...]:
        profiles={"DEFAULT"}
        pattern=re.compile(r"^SELLERBOT_([A-Z0-9_]{1,64})_(?:OZON_CLIENT_ID|OZON_API_KEY|OZON_PERF_CLIENT_ID|OZON_PERF_CLIENT_SECRET|WB_API_TOKEN)$")
        for key in os.environ:
            m=pattern.match(key.upper())
            if m: profiles.add(m.group(1))
        return tuple(sorted(profiles))

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN", ""),
            owner_ids=_owners(os.getenv("TELEGRAM_OWNER_ID", "")),
            ozon_client_id=os.getenv("OZON_CLIENT_ID", ""),
            ozon_api_key=os.getenv("OZON_API_KEY", ""),
            ozon_perf_client_id=os.getenv("OZON_PERF_CLIENT_ID", ""),
            ozon_perf_client_secret=os.getenv("OZON_PERF_CLIENT_SECRET", ""),
            wb_api_token=os.getenv("WB_API_TOKEN", ""),
            db_file=Path(os.getenv("DB_FILE", "./data/seller_analytics.sqlite3")),
            timezone=os.getenv("TIMEZONE", "Europe/Moscow"),
            report_time=os.getenv("REPORT_TIME", "09:00"),
            backfill_days=max(1, min(int(os.getenv("BACKFILL_DAYS", "30")), 365)),
            http_timeout=max(5.0, float(os.getenv("HTTP_TIMEOUT", "60"))),
            ozon_min_interval=max(0.0, float(os.getenv("OZON_MIN_INTERVAL", "65"))),
            wb_min_interval=max(0.0, float(os.getenv("WB_MIN_INTERVAL", "65"))),
            daily_retry_minutes=max(1, int(os.getenv("DAILY_RETRY_MINUTES", "20"))),
            daily_retry_attempts=max(0, min(int(os.getenv("DAILY_RETRY_ATTEMPTS", "3")), 10)),
            product_report_days=max(1, min(int(os.getenv("PRODUCT_REPORT_DAYS", "7")), 30)),
            stock_velocity_days=max(3, min(int(os.getenv("STOCK_VELOCITY_DAYS", "14")), 60)),
            stock_risk_days=max(1, min(int(os.getenv("STOCK_RISK_DAYS", "14")), 90)),
            finance_lookback_days=max(1, min(int(os.getenv("FINANCE_LOOKBACK_DAYS", "14")), 90)),
            alerts_enabled=_bool("ALERTS_ENABLED", True),
            alerts_interval_minutes=max(15, min(int(os.getenv("ALERTS_INTERVAL_MINUTES", "60")), 1440)),
            alert_order_drop_pct=max(1.0, min(float(os.getenv("ALERT_ORDER_DROP_PCT", "35")), 95.0)),
            alert_order_lookback_days=max(3, min(int(os.getenv("ALERT_ORDER_LOOKBACK_DAYS", "7")), 60)),
            alert_api_stale_hours=max(1.0, min(float(os.getenv("ALERT_API_STALE_HOURS", "26")), 720.0)),
            alert_drr_pct=max(1.0, min(float(os.getenv("ALERT_DRR_PCT", "25")), 500.0)),
            alert_cooldown_minutes=max(30, min(int(os.getenv("ALERT_COOLDOWN_MINUTES", "1440")), 43200)),
            auto_backup_enabled=_bool("AUTO_BACKUP_ENABLED", True),
            auto_backup_hour_utc=max(0, min(int(os.getenv("AUTO_BACKUP_HOUR_UTC", "3")), 23)),
            backup_retention_days=max(1, min(int(os.getenv("BACKUP_RETENTION_DAYS", "14")), 365)),
            instance_id=os.getenv("INSTANCE_ID", f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"),
            distributed_lock_ttl_seconds=max(60, min(int(os.getenv("DISTRIBUTED_LOCK_TTL_SECONDS", "7200")), 86400)),
            retry_worker_interval_seconds=max(5, min(int(os.getenv("RETRY_WORKER_INTERVAL_SECONDS", "30")), 600)),
            retry_max_attempts=max(1, min(int(os.getenv("RETRY_MAX_ATTEMPTS", "6")), 20)),
            health_server_enabled=_bool("HEALTH_SERVER_ENABLED", False),
            health_host=os.getenv("HEALTH_HOST", "0.0.0.0"),
            health_port=max(1, min(int(os.getenv("HEALTH_PORT") or os.getenv("PORT", "8080")), 65535)),
            log_format=os.getenv("LOG_FORMAT", "json").strip().lower(),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
            auto_backup_send_telegram=_bool("AUTO_BACKUP_SEND_TELEGRAM",False),
        )

settings = Settings.from_env()
