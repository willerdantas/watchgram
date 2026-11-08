"""Cliente mínimo da API do Mercado Pago (https://api.mercadopago.com).

Endpoints usados:
- POST /v1/payments              Pix dinâmico (QR Code + copia e cola)
- GET  /v1/payments/{id}         status de um pagamento
- GET  /v1/payments/search       busca por external_reference (Checkout Pro)
- POST /checkout/preferences     link de pagamento com cartão (Checkout Pro)
- POST /preapproval              assinatura recorrente no cartão
- GET/PUT /preapproval/{id}      status / cancelamento da assinatura
- GET  /authorized_payments/{id} cobrança mensal de uma assinatura
"""

import hashlib
import hmac
import uuid
from datetime import timedelta

import httpx

from app.utils import utcnow

API = "https://api.mercadopago.com"


class MercadoPagoError(Exception):
    pass


class MercadoPago:
    def __init__(self, access_token: str, timeout: float = 20.0):
        if not access_token:
            raise MercadoPagoError("Access token do Mercado Pago não configurado.")
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._timeout = timeout

    async def _request(self, method: str, path: str, *, json=None, params=None, idempotent=False) -> dict:
        headers = dict(self._headers)
        if idempotent:
            headers["X-Idempotency-Key"] = str(uuid.uuid4())
        async with httpx.AsyncClient(base_url=API, timeout=self._timeout) as client:
            resp = await client.request(method, path, json=json, params=params, headers=headers)
        if resp.status_code >= 400:
            raise MercadoPagoError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    async def create_pix(
        self,
        *,
        amount_cents: int,
        description: str,
        payer_email: str,
        external_reference: str,
        notification_url: str | None,
        expires_minutes: int = 30,
    ) -> dict:
        expiration = utcnow() + timedelta(minutes=expires_minutes)
        body = {
            "transaction_amount": amount_cents / 100,
            "payment_method_id": "pix",
            "description": description,
            "external_reference": external_reference,
            "payer": {"email": payer_email},
            "date_of_expiration": expiration.strftime("%Y-%m-%dT%H:%M:%S.000+00:00"),
        }
        if notification_url:
            body["notification_url"] = notification_url
        return await self._request("POST", "/v1/payments", json=body, idempotent=True)

    async def get_payment(self, payment_id: str) -> dict:
        return await self._request("GET", f"/v1/payments/{payment_id}")

    async def search_payments(self, external_reference: str) -> list[dict]:
        data = await self._request(
            "GET",
            "/v1/payments/search",
            params={
                "external_reference": external_reference,
                "sort": "date_created",
                "criteria": "desc",
            },
        )
        return data.get("results", [])

    async def create_preference(
        self,
        *,
        title: str,
        amount_cents: int,
        payer_email: str,
        external_reference: str,
        notification_url: str | None,
        back_url: str | None,
    ) -> dict:
        body = {
            "items": [
                {"title": title, "quantity": 1, "unit_price": amount_cents / 100, "currency_id": "BRL"}
            ],
            "payer": {"email": payer_email},
            "external_reference": external_reference,
            # Pix tem fluxo próprio no bot; aqui fica só o cartão.
            "payment_methods": {
                "excluded_payment_types": [{"id": "ticket"}, {"id": "atm"}, {"id": "bank_transfer"}],
            },
            "statement_descriptor": "GRUPO VIP",
        }
        if notification_url:
            body["notification_url"] = notification_url
        if back_url:
            body["back_urls"] = {"success": back_url, "pending": back_url, "failure": back_url}
            body["auto_return"] = "approved"
        return await self._request("POST", "/checkout/preferences", json=body)

    async def create_preapproval(
        self,
        *,
        reason: str,
        amount_cents: int,
        payer_email: str,
        external_reference: str,
        back_url: str,
        frequency: int = 1,
        frequency_type: str = "months",
    ) -> dict:
        body = {
            "reason": reason,
            "external_reference": external_reference,
            "payer_email": payer_email,
            "back_url": back_url,
            "status": "pending",
            "auto_recurring": {
                "frequency": frequency,
                "frequency_type": frequency_type,
                "transaction_amount": amount_cents / 100,
                "currency_id": "BRL",
            },
        }
        return await self._request("POST", "/preapproval", json=body)

    async def get_preapproval(self, preapproval_id: str) -> dict:
        return await self._request("GET", f"/preapproval/{preapproval_id}")

    async def cancel_preapproval(self, preapproval_id: str) -> dict:
        return await self._request("PUT", f"/preapproval/{preapproval_id}", json={"status": "cancelled"})

    async def get_authorized_payment(self, authorized_payment_id: str) -> dict:
        return await self._request("GET", f"/authorized_payments/{authorized_payment_id}")


def verify_webhook_signature(secret: str, x_signature: str, x_request_id: str, data_id: str) -> bool:
    """Valida o header x-signature do Mercado Pago ("ts=...,v1=...")."""
    if not secret:
        return True
    parts = dict(p.strip().split("=", 1) for p in x_signature.split(",") if "=" in p)
    ts, v1 = parts.get("ts"), parts.get("v1")
    if not ts or not v1:
        return False
    manifest = f"id:{data_id.lower()};request-id:{x_request_id};ts:{ts};"
    expected = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)
