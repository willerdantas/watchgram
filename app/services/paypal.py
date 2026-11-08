"""Cliente mínimo da API do PayPal (Orders v2), para pagamento único em moeda estrangeira.

Endpoints usados:
- POST /v1/oauth2/token                  token de acesso (client credentials)
- POST /v2/checkout/orders               cria o pedido e devolve o link de aprovação
- GET  /v2/checkout/orders/{id}          status do pedido
- POST /v2/checkout/orders/{id}/capture  recebe o valor de um pedido aprovado
"""

from decimal import Decimal, InvalidOperation

import httpx

LIVE_API = "https://api-m.paypal.com"
SANDBOX_API = "https://api-m.sandbox.paypal.com"

# Só moedas com duas casas decimais: os valores ficam em centavos, como no resto do sistema.
CURRENCIES = {
    "USD": "Dólar americano",
    "EUR": "Euro",
    "GBP": "Libra esterlina",
    "CAD": "Dólar canadense",
    "AUD": "Dólar australiano",
    "CHF": "Franco suíço",
}


class PayPalError(Exception):
    pass


def to_value(cents: int) -> str:
    return f"{Decimal(cents) / 100:.2f}"


def to_cents(value: str | None) -> int | None:
    try:
        return int(Decimal(value or "") * 100)
    except InvalidOperation:
        return None


def approval_link(order: dict) -> str | None:
    """Com payment_source o link vem como payer-action; sem ele, como approve."""
    links = {link.get("rel"): link.get("href") for link in order.get("links") or []}
    return links.get("payer-action") or links.get("approve")


def first_capture(order: dict) -> dict | None:
    for unit in order.get("purchase_units") or []:
        captures = (unit.get("payments") or {}).get("captures") or []
        if captures:
            return captures[0]
    return None


class PayPal:
    def __init__(self, client_id: str, client_secret: str, sandbox: bool = False, timeout: float = 20.0):
        if not client_id or not client_secret:
            raise PayPalError("Client ID e Secret do PayPal não configurados.")
        self._auth = (client_id, client_secret)
        self._base = SANDBOX_API if sandbox else LIVE_API
        self._timeout = timeout

    async def get_token(self) -> str:
        try:
            async with httpx.AsyncClient(base_url=self._base, timeout=self._timeout) as client:
                r = await client.post("/v1/oauth2/token", data={"grant_type": "client_credentials"}, auth=self._auth)
        except httpx.HTTPError as exc:
            raise PayPalError(f"Falha de conexão com o PayPal: {exc.__class__.__name__}") from exc
        if r.status_code >= 400:
            raise PayPalError(f"PayPal recusou as credenciais ({r.status_code}).")
        token = r.json().get("access_token")
        if not token:
            raise PayPalError("PayPal não devolveu o token de acesso.")
        return token

    async def _request(self, method: str, path: str, *, json=None, request_id: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {await self.get_token()}", "Prefer": "return=representation"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id  # repetir a chamada não repete a operação
        try:
            async with httpx.AsyncClient(base_url=self._base, timeout=self._timeout) as client:
                r = await client.request(method, path, json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise PayPalError(f"Falha de conexão com o PayPal: {exc.__class__.__name__}") from exc
        if r.status_code >= 400:
            raise PayPalError(f"PayPal {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    async def create_order(
        self, *, amount_cents: int, currency: str, description: str, reference: str, return_url: str
    ) -> dict:
        return await self._request(
            "POST",
            "/v2/checkout/orders",
            json={
                "intent": "CAPTURE",
                "purchase_units": [
                    {
                        "reference_id": reference,
                        "description": description[:127],
                        "amount": {"currency_code": currency, "value": to_value(amount_cents)},
                    }
                ],
                "payment_source": {
                    "paypal": {
                        "experience_context": {
                            "return_url": return_url,
                            "cancel_url": return_url,
                            "user_action": "PAY_NOW",
                            "shipping_preference": "NO_SHIPPING",  # acesso digital: não pede endereço
                        }
                    }
                },
            },
            request_id=f"order-{reference}",
        )

    async def get_order(self, order_id: str) -> dict:
        return await self._request("GET", f"/v2/checkout/orders/{order_id}")

    async def capture_order(self, order_id: str) -> dict:
        return await self._request(
            "POST", f"/v2/checkout/orders/{order_id}/capture", json={}, request_id=f"capture-{order_id}"
        )
