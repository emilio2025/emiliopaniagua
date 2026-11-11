# Semana 9 — Correcciones según `contracts/semana09.md`

| Tarea | Problema | Causa | Cambio | Evidencia |
|---|---|---|---|---|
| **A · Entrada REST válida** | `quantity: "2"` (string) era aceptado y creaba el pedido (201). | `quantity: int` en Pydantic (modo laxo) convierte `"2"` → `2`. El validador previo solo rechazaba `bool` y `float`. | `backend/app/schemas.py`: `quantity: StrictInt = Field(ge=1, le=5)`. Solo acepta enteros JSON entre 1 y 5. | `"2"`, `2.0`, `true`, `0`, `6` → **422 VALIDATION_ERROR**; `2` → 201. |
| **B · Evento completo** | El worker rechazaba el evento y la notificación terminaba en `failed`. | `build_order_created_event` no incluía `data.total_cents`, obligatorio en `order-created.schema.json`. | `backend/app/events.py` recibe `total_cents` y lo agrega a `data`; `orders.py` lo pasa. La validación del consumidor **no se debilitó**. | Antes: evento `rejected` (*data.total_cents must be an integer…*) y pedido `failed`. Ahora: evento `processed` y pedido `sent`. |
| **C · Confirmación vs. notificación** | `POST /api/v1/orders` tardaba lo mismo que la notificación (≈2 s con retardo de 2000 ms). | `create_order` llamaba a `_confirmation_lead()`, que hacía `asyncio.sleep(notification_delay_ms)` antes de responder. | `backend/app/orders.py`: se eliminó la espera artificial. La API responde al confirmar la transacción (pedido + inventario + outbox); el worker actualiza la notificación después. | Con retardo de 2000 ms: antes 2.02 s; ahora 0.02 s, `notification_status = pending` y luego `sent`. |

## Pruebas
- Pruebas del repositorio (`pytest`, incluidas las de integración con PostgreSQL + Redis): **34 pasan**.
- Smoke: **8/8**.
- Comprobaciones de aceptación A/B/C: código original **6/10** → corregido **10/10**.

## Qué no se cambió
Rutas, esquemas, encabezados, códigos HTTP, idempotencia, autenticación `/_lab/*`, límites de tamaño y frecuencia.

## Llamada REST y evento de un pedido
1. `POST /api/v1/orders` con `Idempotency-Key` y `{"items":[{"product_id":"nova-headphones","quantity":2}]}`.
2. En **una transacción**, la API guarda el pedido, descuenta el inventario y deja el evento en la **outbox**; responde **201** con `notification_status: "pending"`.
3. El **relay** del worker publica en Redis Streams el CloudEvent `pe.unamba.flash.order.created.v1` (`specversion`, `id`, `source`, `type`, `time`, `datacontenttype`, `data.order_id`, `data.items`, `data.total_cents`).
4. El **consumidor** valida el evento contra el contrato y marca la notificación como `sent` (o `failed` si el evento es inválido). `GET /api/v1/orders/{id}` muestra ese estado eventual.
