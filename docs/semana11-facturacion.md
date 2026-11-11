# Semana 11 — Integración con el microservicio de facturación

La tienda S09 consume el microservicio `billing` (Supabase Edge Function) **desde el backend/worker**, nunca desde el navegador. El token `X-Billing-Token` vive solo en `.env` del servidor.

## Flujo
1. `POST /api/v1/orders` confirma el pedido (sin cambios en S09). En **la misma transacción local** se guarda una tarea en `billing_records` con el **cuerpo exacto del contrato** construido desde el **snapshot persistido** (`order.id`, `PEN`, `items[].product_id/name/quantity/unit_price_cents`, `total_cents`). No hay llamada remota dentro de la transacción de stock.
2. El **worker** (`billing_loop`) toma las tareas `pending` y hace `POST {BILLING_BASE_URL}/invoices` con `X-Billing-Token` e `Idempotency-Key = order.id`, timeout de 15 s.
3. `201`/`200` → valida que el `order_id` respondido coincida y guarda `issued` con `invoice_id` y `number`.
4. Timeout, error de red, `429` o `5xx` → hasta **2 reintentos automáticos** por ejecución; si sigue fallando → `failed` (recuperable).
5. `401`/`409`/`422`/otros → `failed` con el código del error (no se inventa otro ID para evadir un conflicto).
6. El navegador consulta `GET /api/v1/orders/{id}/billing` y muestra **Comprobante emitido / pendiente**.

La facturación **no revierte la venta** y **no toca `notification_status`** ni el JSON público de `POST/GET /api/v1/orders`.

## Configuración (`.env`, ignorado por Git)
```
BILLING_BASE_URL=https://TU_PROJECT_REF.supabase.co/functions/v1/billing
BILLING_TOKEN=token_de_tu_tienda
```
`compose.yaml` los pasa a `api` y `worker`. Aplicar: `docker compose up -d --build --force-recreate api worker`.
Sin estas variables, el pedido funciona igual y la facturación queda `pending` (`BILLING_NOT_CONFIGURED`).

## Recuperación manual (mismo cuerpo y misma clave)
```
docker compose exec worker python -m app.billing status ORDER_ID
docker compose exec worker python -m app.billing retry ORDER_ID
```
Un timeout puede ocurrir después de que Supabase guardó la factura: el reintento recibe `200` con la **misma** factura, sin duplicarla.

## Comprobación rápida
```
docker compose exec api python /app/tools/billing_check.py
```

## Evidencia de pruebas
- `pytest`: 40 pruebas pasan (34 de S09 + 6 nuevas del adaptador).
- E2E: éxito (201 → `issued`), replay (200, misma factura), conflicto (409 `IDEMPOTENCY_CONFLICT`), proveedor caído (pedido confirmado, facturación `failed`, notificación `sent`) y recuperación con `retry` → `issued`.
- Regresión S09: smoke 8/8 y tareas A/B/C 10/10.
