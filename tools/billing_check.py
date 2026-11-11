"""Semana 11: crea un pedido en la tienda y espera su comprobante.

Uso: docker compose exec api python /app/tools/billing_check.py
No imprime tokens: solo códigos HTTP e IDs.
"""
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = "http://127.0.0.1:8000"


def call(method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


status, order = call("POST", "/api/v1/orders",
                     {"items": [{"product_id": "arc-mouse", "quantity": 1}]},
                     {"Idempotency-Key": str(uuid.uuid4())})
print(f"POST /api/v1/orders -> {status} order_id={order.get('id')} total_cents={order.get('total_cents')}")
if status != 201:
    sys.exit(1)

billing = {}
for _ in range(60):
    _, billing = call("GET", f"/api/v1/orders/{order['id']}/billing")
    if billing.get("billing_status") != "pending":
        break
    time.sleep(1)
print(f"GET /api/v1/orders/{{id}}/billing -> {json.dumps(billing, ensure_ascii=False)}")
sys.exit(0 if billing.get("billing_status") == "issued" else 2)
