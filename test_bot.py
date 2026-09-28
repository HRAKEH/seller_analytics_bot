import asyncio
import tempfile
from datetime import date
from pathlib import Path

import httpx
import tg_bot


def test_wb_parser_logic_without_network():
    rows = [
        {"saleID": "S001", "srid": "ORDER-1", "finishedPrice": 500},
        {"saleID": "S002", "srid": "ORDER-1", "finishedPrice": 300},
        {"saleID": "S003", "srid": "ORDER-2", "finishedPrice": 700},
        {"saleID": "R004", "srid": "ORDER-2", "finishedPrice": 700},
    ]
    units = 0
    amount = 0
    orders = set()
    for row in rows:
        sid = row["saleID"]
        price = float(row["finishedPrice"])
        if sid.startswith("S"):
            units += 1
            amount += price
            orders.add(row["srid"])
        elif sid.startswith("R"):
            units -= 1
            amount -= price
    assert units == 2
    assert amount == 800
    assert len(orders) == 2


def test_report_format_partial():
    report = tg_bot.build_report_for_date(
        date(2026, 9, 13),
        {"units": 10, "amount": 2000, "orders": 8, "received_at": "now"},
        None,
        None,
        "API error",
    )
    tg_bot.save_report(report["report_date"], report)
    row = tg_bot.get_report(report["report_date"])
    text = tg_bot.format_report(row)
    assert "OZON" in text
    assert "WILDBERRIES" in text
    assert "Итог не рассчитан" in text
    assert "2 000 ₽" in text


def test_full_total():
    report = tg_bot.build_report_for_date(
        date(2026, 9, 14),
        {"units": 10, "amount": 2000, "orders": 8, "received_at": "now"},
        {"units": 5, "amount": 1000, "orders": 4, "received_at": "now"},
        None,
        None,
    )
    assert report["total_units"] == 15
    assert report["total_amount"] == 3000
    assert report["total_orders"] == 12


async def test_api_clients_with_mocks():
    def handler(request: httpx.Request):
        if request.url.host == "api-seller.ozon.ru":
            path = request.url.path
            if path == "/v1/analytics/data":
                return httpx.Response(200, json={"result": {"totals": [12, 3456]}})
            if path == "/v4/posting/fbs/list":
                return httpx.Response(200, json={
                    "postings": [
                        {"order_id": 101, "created_at": "2026-09-24T09:00:00+03:00", "status": "delivered"},
                        {"order_id": 102, "created_at": "2026-09-24T10:00:00+03:00", "status": "cancelled"},
                    ],
                    "has_next": False,
                    "cursor": "",
                })
            if path == "/v3/posting/fbo/list":
                return httpx.Response(200, json={
                    "postings": [
                        {"order_id": 201, "created_at": "2026-09-24T11:00:00+03:00", "status": "delivered"},
                        {"order_id": 202, "created_at": "2026-09-23T11:00:00+03:00", "status": "delivered"},
                    ],
                    "has_next": False,
                    "cursor": "",
                })
        if request.url.host == "statistics-api.wildberries.ru":
            return httpx.Response(200, json=[
                {"saleID": "S1", "srid": "W1", "finishedPrice": 100},
                {"saleID": "S2", "srid": "W1", "finishedPrice": 250},
                {"saleID": "R3", "srid": "W2", "finishedPrice": 50},
            ])
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        oz = await tg_bot.ozon_sales(date(2026, 9, 24), client)
        wb = await tg_bot.wb_sales(date(2026, 9, 24), client)
    assert oz["units"] == 12
    assert oz["amount"] == 3456
    assert oz["orders"] == 2
    assert wb["units"] == 1
    assert wb["amount"] == 300
    assert wb["orders"] == 1


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        old = tg_bot.DB_FILE
        tg_bot.DB_FILE = Path(tmp) / "test.sqlite3"
        tg_bot.init_db()
        test_wb_parser_logic_without_network()
        test_report_format_partial()
        test_full_total()
        tg_bot.DB_FILE = old
    print("ALL TESTS PASSED")

    asyncio.run(test_api_clients_with_mocks())
    print("API MOCK TESTS PASSED")
