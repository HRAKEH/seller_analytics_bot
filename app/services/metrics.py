"""Metric registry. Only explicitly comparable concepts may be aggregated."""
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class MetricDefinition:
    key: str
    label: str
    unit: str
    additive_across_marketplaces: bool = False
    description: str = ""

METRICS: dict[str, MetricDefinition] = {
    "revenue": MetricDefinition("revenue", "Выручка", "RUB", False, "Не объединяется, пока методика источников явно не сопоставлена"),
    "ordered_revenue": MetricDefinition("ordered_revenue", "Сумма заказов", "RUB", False, "Предварительная сумма заказов конкретного источника"),
    "orders_count": MetricDefinition("orders_count", "Заказы", "orders", False, "Число заказов, только при сопоставимой семантике"),
    "ordered_units": MetricDefinition("ordered_units", "Заказанные единицы", "units", True, "Операционная метрика заказанных товарных единиц"),
    "sold_units": MetricDefinition("sold_units", "Проданные единицы", "units", False),
    "redeemed_units": MetricDefinition("redeemed_units", "Выкупленные единицы", "units", False),
    "returns_units": MetricDefinition("returns_units", "Возвраты", "units", False),
    "cancellations_units": MetricDefinition("cancellations_units", "Отмены", "units", False),
    "financial_sales": MetricDefinition("financial_sales", "Продажи по финансовому отчёту", "RUB", False, "Финансовая база источника, не оперативная сумма заказов"),
    "goods_payable": MetricDefinition("goods_payable", "К перечислению за товар", "RUB", False, "Сумма после комиссии/приёма платежей, но не всегда финальный банковский платёж"),
    "bank_payment": MetricDefinition("bank_payment", "Итог к оплате по фин. отчёту", "RUB", False, "bankPaymentSum финансового отчёта WB с удержаниями и корректировками; не подтверждение банковского перевода"),
    "marketplace_net": MetricDefinition("marketplace_net", "Нетто маркетплейса", "RUB", False, "Расчёт из официальных финансовых компонент источника"),
    "ad_spend": MetricDefinition("ad_spend", "Рекламные расходы", "RUB", True),
    "finance_ad_spend": MetricDefinition("finance_ad_spend", "Реклама по начислениям Ozon", "RUB", False,
                                       "Подмножество services; уже включено в marketplace_net"),
    "ad_attributed_sales": MetricDefinition("ad_attributed_sales", "Продажи из рекламы", "RUB", True),
    "commission": MetricDefinition("commission", "Комиссия", "RUB", True),
    "logistics": MetricDefinition("logistics", "Логистика", "RUB", True),
    "storage": MetricDefinition("storage", "Хранение", "RUB", True),
    "acceptance": MetricDefinition("acceptance", "Приёмка", "RUB", True),
    "acquiring": MetricDefinition("acquiring", "Эквайринг", "RUB", True),
    "services": MetricDefinition("services", "Прочие услуги/удержания", "RUB", True),
    "penalties": MetricDefinition("penalties", "Штрафы", "RUB", True),
    "compensation": MetricDefinition("compensation", "Компенсации/доплаты", "RUB", True),
    "returns_amount": MetricDefinition("returns_amount", "Возвраты и отмены", "RUB", True),
    "payout": MetricDefinition("payout", "К выплате", "RUB", True),
    "stock_units": MetricDefinition("stock_units", "Остаток", "units", False),
    "fulfillment_units": MetricDefinition("fulfillment_units", "Единицы по схеме", "units", False, "Операционная разбивка по FBO/FBS/FBW; не используется как общая метрика заказов"),
}

def definition(metric_key: str) -> MetricDefinition:
    if metric_key not in METRICS:
        raise KeyError(f"Неизвестная метрика: {metric_key}")
    return METRICS[metric_key]

def sum_comparable(values: dict[str, float], metric_key: str) -> float | None:
    item = definition(metric_key)
    if not item.additive_across_marketplaces:
        return None
    return sum(values.values())
