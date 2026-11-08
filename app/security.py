"""Proteções transversais: segredos criptografados no banco e middlewares HTTP.

- Segredos (tokens, senhas de SMTP...) são gravados com Fernet (AES + HMAC) e a
  chave fica só no ambiente (ENCRYPTION_KEY), nunca no banco.
- Cabeçalhos de segurança com CSP por nonce, HTTPS obrigatório com domínio,
  bloqueio de POST vindo de outro site e limite de tamanho das requisições.
"""

import base64
import hashlib
import hmac
import logging
import secrets
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

from app import config

log = logging.getLogger(__name__)

PREFIX = "enc1:"
MAX_BODY = 1024 * 1024  # 1 MB: o painel só recebe formulários e notificações pequenas
LOCAL_HOSTS = {"localhost", "127.0.0.1", "testserver"}


def _key() -> bytes:
    if config.ENCRYPTION_KEY:
        return config.ENCRYPTION_KEY.encode()
    # Sem chave própria, deriva da SECRET_KEY (trocar a SECRET_KEY torna os segredos ilegíveis).
    return base64.urlsafe_b64encode(hashlib.sha256(b"dados:" + config.SECRET_KEY.encode()).digest())


_fernet = Fernet(_key())


def encrypt(value: str) -> str:
    if not value or value.startswith(PREFIX):
        return value
    return PREFIX + _fernet.encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    if not value or not value.startswith(PREFIX):
        return value  # vazio ou gravado antes da criptografia (migrado na inicialização)
    try:
        return _fernet.decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken:
        log.error("Segredo ilegível: a ENCRYPTION_KEY mudou? Cadastre o valor de novo no painel.")
        return ""


def fingerprint(value: str) -> str:
    """Comparar segredos sem guardá-los em claro (ex.: o mesmo bot em duas contas)."""
    return hmac.new(_key(), value.encode(), hashlib.sha256).hexdigest() if value else ""


# ----------------------------------------------------------------- HTTP


def _csp(nonce: str) -> str:
    return "; ".join(
        [
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}' https://cdnjs.cloudflare.com",
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
            "font-src 'self' https://fonts.gstatic.com",
            "img-src 'self' data:",
            "connect-src 'self'",
            "object-src 'none'",
            "base-uri 'self'",
            "frame-ancestors 'none'",
            # O pagamento da assinatura redireciona o formulário para o Mercado Pago.
            "form-action 'self' https://*.mercadopago.com.br https://*.mercadopago.com",
        ]
    )


def _is_https(request: Request) -> bool:
    return request.url.scheme == "https"


def _same_origin(request: Request) -> bool:
    """POST do próprio painel: Origin (ou Referer) igual ao host. Sem os dois: cliente que não é navegador."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source or source == "null":
        return source is None
    host = request.headers.get("host", "")
    allowed = {host}
    if config.PUBLIC_URL:
        allowed.add(urlsplit(config.PUBLIC_URL).netloc)
    return urlsplit(source).netloc in allowed


async def security_middleware(request: Request, call_next) -> Response:
    host = (request.headers.get("host") or "").split(":")[0]
    if config.FORCE_HTTPS and not _is_https(request) and host not in LOCAL_HOSTS:
        return RedirectResponse(str(request.url.replace(scheme="https")), status_code=308)

    if request.method not in ("GET", "HEAD", "OPTIONS"):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY:
            return PlainTextResponse("Requisição grande demais.", status_code=413)
        # Webhooks vêm de outros servidores; o resto só pode ser enviado pelo próprio painel.
        if not request.url.path.startswith("/webhooks/") and not _same_origin(request):
            return PlainTextResponse("Origem não permitida.", status_code=403)

    request.state.csp_nonce = secrets.token_urlsafe(16)
    response = await call_next(request)
    headers = response.headers
    headers.setdefault("Content-Security-Policy", _csp(request.state.csp_nonce))
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    if _is_https(request):
        headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if not request.url.path.startswith("/static/"):
        headers.setdefault("Cache-Control", "no-store")  # páginas com dados da conta não ficam em cache
    return response
