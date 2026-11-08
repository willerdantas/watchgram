import re
import unicodedata
from collections import defaultdict
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app import config

TZ = ZoneInfo(config.TIMEZONE)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def utcnow() -> datetime:
    """UTC sem tzinfo: é o formato gravado no banco."""
    return datetime.now(UTC).replace(tzinfo=None)


def to_local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC).astimezone(TZ)


def local_to_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=TZ).astimezone(UTC).replace(tzinfo=None)


def fmt_date(dt: datetime | None) -> str:
    local = to_local(dt)
    return local.strftime("%d/%m/%Y") if local else "-"


def fmt_datetime(dt: datetime | None) -> str:
    local = to_local(dt)
    return local.strftime("%d/%m/%Y %H:%M") if local else "-"


def time_ago(dt: datetime | None, now: datetime | None = None) -> str:
    """"há 5 minutos", em linguagem de gente."""
    if dt is None:
        return "-"
    seconds = ((now or utcnow()) - dt).total_seconds()
    if seconds < 60:
        return "agora mesmo"
    if seconds < 3600:
        minutes = int(seconds // 60)
        return f"há {minutes} minuto{'s' if minutes > 1 else ''}"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"há {hours} hora{'s' if hours > 1 else ''}"
    if seconds < 7 * 86400:
        days = int(seconds // 86400)
        return f"há {days} dia{'s' if days > 1 else ''}"
    return fmt_date(dt)


def brl(cents: int | None) -> str:
    value = (cents or 0) / 100
    text = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {text}"


def money(cents: int | None, currency: str | None = None) -> str:
    """Real no formato de sempre; moeda estrangeira como "USD 19.90"."""
    if not currency or currency == "BRL":
        return brl(cents)
    return f"{currency} {(cents or 0) / 100:,.2f}"


def parse_brl(text: str) -> int:
    """Converte "29,90", "1.299,90", "29.90" ou "1,299.90" em centavos."""
    cleaned = text.strip().replace("R$", "").replace(" ", "")
    if not cleaned:
        raise ValueError("valor vazio")
    if "," in cleaned and cleaned.rfind(".") > cleaned.rfind(","):
        cleaned = cleaned.replace(",", "")  # o ponto é o separador decimal
    elif "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    value = round(float(cleaned) * 100)
    if value < 0:
        raise ValueError("valor negativo")
    return value


def render(template: str, **values) -> str:
    """format_map tolerante: placeholders desconhecidos ficam como estão."""

    class _Safe(defaultdict):
        def __missing__(self, key):
            return "{" + key + "}"

    try:
        return template.format_map(_Safe(None, **values))
    except (ValueError, IndexError):
        return template


def parse_id_list(text: str) -> list[int]:
    ids = []
    for part in re.split(r"[,\s;]+", text or ""):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.append(int(part))
    return ids


# Em "123456 @fulano" ou "123456 (Fulano)", o texto após @ / ( é só identificação.
ADMIN_LABEL_RE = re.compile(r"[@(][^,;\n]*")


def parse_admin_ids(text: str) -> list[int]:
    return list(dict.fromkeys(parse_id_list(ADMIN_LABEL_RE.sub(" ", text or ""))))


def admin_entry(user_id: int, username: str | None, name: str = "") -> str:
    """Linha do campo de admins: id seguido do @ (ou do nome) para facilitar a identificação."""
    if username:
        return f"{user_id} @{username}"
    name = re.sub(r"[,;()\r\n]", " ", name or "").strip()
    return f"{user_id} ({name})" if name else str(user_id)


def parse_int_list(text: str) -> list[int]:
    return sorted({n for n in parse_id_list(text) if n >= 0}, reverse=True)


def normalize_text(text: str) -> str:
    """Minúsculas, sem acentos, pontuação ou letras repetidas: "Olááá!!" -> "ola"."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"(\w)\1+", r"\1", text)
    return " ".join(text.split())


def parse_keywords(text: str) -> list[str]:
    """Uma palavra/frase por linha ou separadas por vírgula."""
    seen, result = set(), []
    for part in re.split(r"[,;\n]+", text or ""):
        part = " ".join(part.split())
        key = normalize_text(part)
        if key and key not in seen:
            seen.add(key)
            result.append(part)
    return result


def slugify(text: str) -> str:
    """"Plano Anual VIP 2" -> "plano_anual_vip_2"."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def matches_keyword(text: str, keywords: list[str]) -> bool:
    """A mensagem é a palavra-chave ou começa com ela ("Oi, tudo bem?" casa com "oi")."""
    norm = normalize_text(text)
    for keyword in keywords:
        key = normalize_text(keyword)
        if key and (norm == key or norm.startswith(key + " ")):
            return True
    return False


def time_greeting(dt: datetime | None = None) -> str:
    hour = to_local(dt or utcnow()).hour
    if 5 <= hour < 12:
        return "Bom dia"
    if 12 <= hour < 18:
        return "Boa tarde"
    return "Boa noite"
