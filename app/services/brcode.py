"""Pix estático com valor (BR Code): o texto "copia e cola" e o QR Code, gerados aqui mesmo.

Segue o Manual de Padrões para Iniciação do Pix (BCB), formato EMV-MPM. Não passa por banco
nem por API: o pagamento não é confirmado sozinho, por isso fica no fluxo do Pix manual
(o membro envia o comprovante e o dono aprova).
"""

import io
import re
import unicodedata

import segno

MAX_KEY = 77  # 99 do campo 26 menos o GUI "br.gov.bcb.pix" e os cabeçalhos
MAX_NAME = 25
MAX_CITY = 15
MAX_TXID = 25


def _field(tag: str, value: str) -> str:
    return f"{tag}{len(value):02d}{value}"


def _ascii(value: str, limit: int) -> str:
    """Nome e cidade: sem acentos e só caracteres que todos os bancos aceitam."""
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9 .\-]", "", text).strip()[:limit].strip()


def crc16(payload: str) -> str:
    """CRC16-CCITT (polinômio 0x1021, início 0xFFFF), como pede o padrão."""
    crc = 0xFFFF
    for byte in payload.encode():
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return f"{crc:04X}"


def normalize_key(key: str) -> str:
    key = key.strip()
    if "@" in key:
        return key.lower()
    digits = re.sub(r"\D", "", key)
    if key.startswith("+"):
        return "+" + digits  # celular: +5511999998888
    if re.fullmatch(r"[\d.\-/ ]+", key) and len(digits) in (11, 14):
        return digits  # CPF ou CNPJ com pontuação
    return key


def build(key: str, name: str, city: str, amount_cents: int | None = None, txid: str = "***") -> str:
    key = normalize_key(key)
    name, city = _ascii(name, MAX_NAME), _ascii(city, MAX_CITY)
    txid = re.sub(r"[^A-Za-z0-9]", "", txid)[:MAX_TXID] or "***"
    if not key or len(key) > MAX_KEY or not name or not city:
        raise ValueError("Chave, titular e cidade são obrigatórios para o QR Code Pix.")
    account = _field("00", "br.gov.bcb.pix") + _field("01", key)
    payload = (
        _field("00", "01")
        + _field("26", account)
        + _field("52", "0000")
        + _field("53", "986")  # real
        + (_field("54", f"{amount_cents / 100:.2f}") if amount_cents else "")
        + _field("58", "BR")
        + _field("59", name)
        + _field("60", city)
        + _field("62", _field("05", txid))
        + "6304"
    )
    return payload + crc16(payload)


def qr_png(payload: str) -> bytes:
    out = io.BytesIO()
    segno.make(payload, error="m", micro=False).save(out, kind="png", scale=8, border=4)
    return out.getvalue()


def missing_fields(settings) -> list[str]:
    """O que falta em Configurações para gerar o QR Code."""
    labels = {"pix_key": "chave Pix", "pix_holder": "nome do titular", "pix_city": "cidade"}
    return [label for key, label in labels.items() if not str(settings.get(key, "")).strip()]
