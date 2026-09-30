"""Storage DTOs. Database rows stay behind repositories."""
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class Seller:
    id: int
    telegram_user_id: int
    name: str
    timezone: str
    active: bool

@dataclass(frozen=True)
class Shop:
    id: int
    seller_id: int
    name: str
    currency: str
    active: bool
    credential_profile: str = "DEFAULT"

@dataclass(frozen=True)
class MarketplaceConnection:
    id: int
    shop_id: int
    marketplace: str
    display_name: str
    external_account_id: str | None
    enabled: bool

@dataclass(frozen=True)
class MetricPoint:
    connection_id: int
    data_date: str
    metric_key: str
    value: float
    unit: str
    is_preliminary: bool = False
    as_of: str | None = None

@dataclass(frozen=True)
class SourceRun:
    id: int
    connection_id: int
    endpoint: str
    data_date: str
    status: str
    started_at: str
    finished_at: str | None
    error: str | None
    http_status: int | None
    attempts: int
    payload_hash: str | None

@dataclass(frozen=True)
class Product:
    id: int
    shop_id: int
    internal_sku: str
    name: str
    cost_price: float | None
    active: bool

@dataclass(frozen=True)
class ProductListing:
    id: int
    product_id: int
    connection_id: int
    marketplace_sku: str
    offer_id: str | None

@dataclass(frozen=True)
class ProductMetricPoint:
    listing_id: int
    data_date: str
    metric_key: str
    value: float
    unit: str
    fulfillment_scheme: str = "ALL"
    is_preliminary: bool = False
    as_of: str | None = None

@dataclass(frozen=True)
class InventoryPoint:
    listing_id: int
    available_units: float
    reserved_units: float = 0.0
    fulfillment_scheme: str = "ALL"
    warehouse_name: str = "ALL"
    as_of: str | None = None

@dataclass(frozen=True)
class ShopPreferences:
    shop_id: int
    timezone: str
    report_time: str
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
    setup_completed: bool
    demo_mode: bool = False
    onboarding_version: str = ""


@dataclass(frozen=True)
class CommerceEventPoint:
    listing_id: int | None
    data_date: str
    event_kind: str
    source_name: str
    fingerprint: str
    event_time: str | None = None
    external_order_id: str | None = None
    external_event_id: str | None = None
    quantity: float = 0.0
    gross_amount: float = 0.0
    net_amount: float | None = None
    fulfillment_scheme: str = 'ALL'
    is_preliminary: bool = False
    metadata_json: str = '{}'


@dataclass(frozen=True)
class UserShopAccess:
    telegram_user_id: int
    shop_id: int
    role: str
    active: bool = True

@dataclass(frozen=True)
class AdCampaignPoint:
    connection_id: int
    data_date: str
    campaign_id: str
    campaign_name: str = ''
    spend: float = 0.0
    attributed_sales: float = 0.0
    orders: float = 0.0
    clicks: float = 0.0
    impressions: float = 0.0

@dataclass(frozen=True)
class AdProductPoint:
    connection_id: int
    data_date: str
    marketplace_sku: str
    campaign_id: str = ''
    campaign_name: str = ''
    listing_id: int | None = None
    spend: float = 0.0
    attributed_sales: float = 0.0
    orders: float = 0.0
    clicks: float = 0.0
    impressions: float = 0.0
