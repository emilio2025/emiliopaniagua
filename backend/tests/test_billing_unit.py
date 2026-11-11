import asyncio
import json
import uuid

import httpx

from app.billing import build_invoice_request, send_invoice
from app.config import Settings

ORDER_ID = str(uuid.uuid4())
SNAPSHOT = {
    "id": ORDER_ID,
    "status": "confirmed",
    "notification_status": "pending",
    "items": [
        {"product_id": "nova-headphones", "name": "Nova Headphones", "quantity": 2, "unit_price_cents": 129900},
        {"product_id": "arc-mouse", "name": "Arc Mouse", "quantity": 1, "unit_price_cents": 49900},
    ],
    "total_cents": 309700,
    "created_at": "2026-11-11T00:00:00Z",
}


def settings() -> Settings:
    return Settings(
        database_url="postgresql://x@localhost/x",
        redis_url="redis://localhost:6379/0",
        lab_key="k" * 40,
        billing_base_url="https://demo.supabase.co/functions/v1/billing",
        billing_token="a" * 64,
    )


def test_request_uses_exact_contract_fields() -> None:
    body = build_invoice_request(SNAPSHOT)
    assert set(body) == {"order_id", "currency", "items", "total_cents"}
    assert body["order_id"] == ORDER_ID.lower()
    assert body["currency"] == "PEN"
    assert body["total_cents"] == sum(i["quantity"] * i["unit_price_cents"] for i in body["items"])
    for item in body["items"]:
        assert set(item) == {"product_id", "name", "quantity", "unit_price_cents"}
    assert "notification_status" not in json.dumps(body)


def run(handler) -> object:
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await send_invoice(settings(), build_invoice_request(SNAPSHOT), client)

    return asyncio.run(go())


def test_headers_and_issued() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers["Idempotency-Key"]
        seen["token"] = request.headers["X-Billing-Token"]
        return httpx.Response(201, json={"id": str(uuid.uuid4()), "number": "DEMO-x", "order_id": ORDER_ID, "status": "issued"})

    result = run(handler)
    assert result.outcome == "issued"
    assert seen["url"].endswith("/functions/v1/billing/invoices")
    assert seen["key"] == ORDER_ID
    assert seen["token"] == "a" * 64


def test_replay_200_is_issued() -> None:
    result = run(lambda r: httpx.Response(200, json={"id": str(uuid.uuid4()), "order_id": ORDER_ID, "status": "issued"}))
    assert result.outcome == "issued"


def test_mismatched_order_is_rejected() -> None:
    result = run(lambda r: httpx.Response(201, json={"id": str(uuid.uuid4()), "order_id": str(uuid.uuid4()), "status": "issued"}))
    assert result.outcome == "permanent"


def test_conflict_is_permanent() -> None:
    result = run(lambda r: httpx.Response(409, json={"error": {"code": "IDEMPOTENCY_CONFLICT", "message": "x"}}))
    assert result.outcome == "permanent" and result.error == "IDEMPOTENCY_CONFLICT"


def test_timeout_and_5xx_are_transient() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    assert run(boom).outcome == "transient"
    assert run(lambda r: httpx.Response(503, text="down")).outcome == "transient"
