import pytest

from app.services.mercadopago import verify_webhook_signature
from app.utils import brl, matches_keyword, normalize_text, parse_brl, parse_int_list, parse_keywords, render


@pytest.mark.parametrize(
    "text,cents",
    [("29,90", 2990), ("1.299,90", 129990), ("29.90", 2990), ("R$ 10", 1000), (" 7,5 ", 750)],
)
def test_parse_brl(text, cents):
    assert parse_brl(text) == cents


def test_parse_brl_invalid():
    with pytest.raises(ValueError):
        parse_brl("abc")


def test_brl():
    assert brl(129990) == "R$ 1.299,90"
    assert brl(0) == "R$ 0,00"


def test_render_keeps_unknown_placeholders():
    assert render("Olá {nome}, {desconhecido}", nome="Ana") == "Olá Ana, {desconhecido}"


def test_parse_int_list():
    assert parse_int_list("7, 3,1 0 x") == [7, 3, 1, 0]


def test_webhook_signature():
    import hashlib
    import hmac

    secret, req, data_id, ts = "segredo", "req-1", "123", "1700000000"
    v1 = hmac.new(secret.encode(), f"id:{data_id};request-id:{req};ts:{ts};".encode(), hashlib.sha256).hexdigest()
    assert verify_webhook_signature(secret, f"ts={ts},v1={v1}", req, data_id)
    assert not verify_webhook_signature(secret, f"ts={ts},v1=errado", req, data_id)


KEYWORDS = parse_keywords("oi\nOlá, bom dia\nboa noite;quero assinar")


@pytest.mark.parametrize(
    "text", ["Oi", "oiii", "Oi, tudo bem?", "OLÁ!!", "ola", "Bom dia", "bom diaaa 😀", "Boa noite.", "quero assinar o vip"]
)
def test_matches_keyword(text):
    assert matches_keyword(text, KEYWORDS)


@pytest.mark.parametrize("text", ["oitenta reais", "tudo bem?", "boa", "quero", ""])
def test_not_matches_keyword(text):
    assert not matches_keyword(text, KEYWORDS)


def test_parse_keywords_dedup():
    assert parse_keywords("Oi\noi\n\nOlá , ola") == ["Oi", "Olá"]
    assert normalize_text("  Olááá,   Mundo! ") == "ola mundo"
