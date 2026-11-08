"""Configuração de infraestrutura lida do ambiente (.env).

As configurações operacionais (token do bot, Mercado Pago, chave Pix etc.)
ficam no banco e são editadas pela página /config.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")



def _database_url() -> str:
    """Aceita também o formato padrão postgres:// (Heroku, Supabase, Neon...)."""
    url = os.getenv("DATABASE_URL") or f"sqlite+aiosqlite:///{BASE_DIR / 'data' / 'bot.db'}"
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):]
    return url


DATABASE_URL = _database_url()
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SECRET_KEY = os.getenv("SECRET_KEY", "")
TIMEZONE = os.getenv("TIMEZONE", "America/Sao_Paulo")
# Endereço público do painel (https), usado no webhook do Mercado Pago.
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
# Com domínio https: cookie só por HTTPS e redirecionamento de http para https.
FORCE_HTTPS = PUBLIC_URL.startswith("https://")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1" if FORCE_HTTPS else "0") == "1"
# Chave dos segredos gravados no banco (tokens, senhas). Gere com:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "").strip()
# Desliga bot e agendador (útil em testes e para abrir só o painel).
DISABLE_BACKGROUND = os.getenv("DISABLE_BACKGROUND", "0") == "1"
