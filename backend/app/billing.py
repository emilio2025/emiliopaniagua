"""Adaptador de facturación (Semana 11).

Consume el microservicio externo `billing` (Supabase Edge Function) desde el
backend/worker, nunca desde el navegador:

* El pedido confirmado guarda, en su misma transacción local, una tarea de
  facturación con el cuerpo exacto del contrato (snapshot inmutable).
* El worker envía esa tarea por HTTPS con timeout finito, `X-Billing-Token`
  privado e `Idempotency-Key` igual al ID del pedido.
* Un fallo de facturación nunca revierte la venta ni toca `notification_status`.
* Recuperación: reintento manual con el mismo cuerpo y la misma clave
  (`python -m app.billing retry <order_id>`), más un máximo de dos reintentos
  automáticos por ejecución para fallos transitorios.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import Settings, get_settings
from .db import utcnow
from .models import BillingRecord

logger = logging.getLogger(__name__)

PENDING = "pending"
ISSUED = "issued"
FAILED = "failed"

CURRENCY = "PEN"
MAX_AUTOMATIC_RETRIES = 2
TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}


def build_invoice_request(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Transforma el snapshot persistido del pedido S09 en el contrato exacto de facturación."""
    return {
        "order_id": str(uuid.UUID(str(snapshot["id"]))).lower(),
        "currency": CURRENCY,
        "items": [
            {
                "product_id": item["product_id"],
                "name": item["name"],
                "quantity": item["quantity"],
                "unit_price_cents": item["unit_price_cents"],
            }
            for item in snapshot["items"]
        ],
        "total_cents": snapshot["total_cents"],
    }


def is_configured(settings: Settings) -> bool:
    return bool(settings.billing_base_url.strip()) and bool(settings.billing_token_value())


@dataclass
class SendResult:
    outcome: str  # issued | permanent | transient
    http_status: int | None = None
    invoice: dict[str, Any] | None = None
    error: str | None = None


def _error_code(response: httpx.Response) -> str:
    try:
        payload = response.json()
        code = payload["error"]["code"]
        if isinstance(code, str):
            return code[:60]
    except Exception:
        pass
    return f"HTTP_{response.status_code}"


async def send_invoice(
    settings: Settings,
    body: dict[str, Any],
    client: httpx.AsyncClient | None = None,
) -> SendResult:
    url = settings.billing_base_url.rstrip("/") + "/invoices"
    headers = {
        "Content-Type": "application/json",
        "X-Billing-Token": settings.billing_token_value(),
        "Idempotency-Key": body["order_id"],
    }
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=settings.billing_timeout_seconds)
    try:
        response = await client.post(url, json=body, headers=headers)
    except httpx.TimeoutException:
        return SendResult("transient", error="TIMEOUT")
    except httpx.HTTPError as exc:
        return SendResult("transient", error=f"NETWORK_{type(exc).__name__}"[:60])
    finally:
        if owns_client:
            await client.aclose()

    if response.status_code in (200, 201):
        try:
            invoice = response.json()
        except ValueError:
            return SendResult("transient", response.status_code, error="INVALID_JSON")
        if (
            not isinstance(invoice, dict)
            or invoice.get("order_id") != body["order_id"]
            or invoice.get("status") != "issued"
            or not isinstance(invoice.get("id"), str)
        ):
            return SendResult("permanent", response.status_code, error="RESPONSE_MISMATCH")
        return SendResult("issued", response.status_code, invoice=invoice)

    code = _error_code(response)
    if response.status_code in TRANSIENT_STATUS:
        return SendResult("transient", response.status_code, error=code)
    return SendResult("permanent", response.status_code, error=code)


async def _save_result(
    session_factory: async_sessionmaker[AsyncSession],
    order_id: uuid.UUID,
    result: SendResult,
    attempts: int,
) -> None:
    values: dict[str, Any] = {
        "attempts": BillingRecord.attempts + attempts,
        "http_status": result.http_status,
        "updated_at": utcnow(),
    }
    if result.outcome == "issued" and result.invoice is not None:
        values.update(
            status=ISSUED,
            invoice_id=result.invoice["id"],
            invoice_number=str(result.invoice.get("number") or "")[:120] or None,
            last_error=None,
        )
    else:
        values.update(status=FAILED, last_error=(result.error or "UNKNOWN")[:200])
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(BillingRecord)
                .where(BillingRecord.order_id == order_id, BillingRecord.status != ISSUED)
                .values(**values)
            )


async def bill_order(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    order_id: uuid.UUID,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Envía una tarea de facturación. Devuelve el estado local resultante."""
    async with session_factory() as session:
        record = await session.get(BillingRecord, order_id)
        if record is None:
            return "missing"
        if record.status == ISSUED:
            return ISSUED
        body = dict(record.request)

    result = SendResult("transient", error="NOT_SENT")
    attempts = 0
    for attempt in range(1 + MAX_AUTOMATIC_RETRIES):
        attempts += 1
        result = await send_invoice(settings, body, client)
        if result.outcome != "transient":
            break
        if attempt < MAX_AUTOMATIC_RETRIES:
            await asyncio.sleep(settings.billing_retry_backoff_seconds * (attempt + 1))
    await _save_result(session_factory, order_id, result, attempts)
    logger.info(
        "billing order=%s outcome=%s http=%s error=%s",
        order_id,
        result.outcome,
        result.http_status,
        result.error,
    )
    return ISSUED if result.outcome == "issued" else FAILED


async def process_pending_billing(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    batch_size: int = 10,
) -> int:
    if not is_configured(settings):
        return 0
    async with session_factory() as session:
        order_ids = (
            await session.execute(
                select(BillingRecord.order_id)
                .where(BillingRecord.status == PENDING)
                .order_by(BillingRecord.created_at)
                .limit(batch_size)
            )
        ).scalars().all()
    if not order_ids:
        return 0
    async with httpx.AsyncClient(timeout=settings.billing_timeout_seconds) as client:
        for order_id in order_ids:
            await bill_order(session_factory, settings, order_id, client)
    return len(order_ids)


async def request_manual_retry(
    session_factory: async_sessionmaker[AsyncSession],
    order_id: uuid.UUID,
) -> str:
    """Devuelve una tarea `failed` a `pending`, conservando el mismo cuerpo y clave."""
    async with session_factory() as session:
        async with session.begin():
            record = await session.get(BillingRecord, order_id, with_for_update=True)
            if record is None:
                return "missing"
            if record.status == FAILED:
                record.status = PENDING
                record.updated_at = utcnow()
            return record.status


def public_billing_view(record: BillingRecord | None, settings: Settings) -> dict[str, Any]:
    if record is None:
        return {"billing_status": "not_applicable", "invoice_id": None, "invoice_number": None, "error": None}
    error = record.last_error
    if record.status == PENDING and not is_configured(settings):
        error = "BILLING_NOT_CONFIGURED"
    return {
        "billing_status": record.status,
        "invoice_id": record.invoice_id,
        "invoice_number": record.invoice_number,
        "error": error,
    }


async def _cli(args: argparse.Namespace) -> None:
    from .db import Database

    settings = get_settings()
    database = Database(settings)
    try:
        await database.initialize()
        order_id = uuid.UUID(args.order_id)
        if args.command == "retry":
            status = await request_manual_retry(database.session_factory, order_id)
            if status == PENDING:
                if not is_configured(settings):
                    print("Facturación no configurada (BILLING_BASE_URL / BILLING_TOKEN).")
                    return
                status = await bill_order(database.session_factory, settings, order_id)
            print(f"order={order_id} billing_status={status}")
        async with database.session_factory() as session:
            record = await session.get(BillingRecord, order_id)
            print(public_billing_view(record, settings))
    finally:
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Facturación de pedidos S09")
    parser.add_argument("command", choices=["status", "retry"])
    parser.add_argument("order_id")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(_cli(parser.parse_args()))


if __name__ == "__main__":
    main()
