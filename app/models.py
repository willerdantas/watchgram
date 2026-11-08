"""Modelos do banco. Todas as datas são gravadas em UTC (naive).

Multiusuário: cada usuário do painel é um cliente (tenant) isolado. Toda tabela
de dados tem tenant_id, e toda consulta precisa filtrar por ele.
"""

import secrets
from datetime import datetime, timedelta

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.utils import utcnow


RECURRING_GRACE = timedelta(days=3)


class User(Base):
    """Login do painel. Cada usuário é dono de uma área (bot, planos, membros...)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # Personalização da área
    name: Mapped[str] = mapped_column(String(128), default="")
    accent_color: Mapped[str] = mapped_column(String(16), default="")
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Muda ao trocar/resetar a senha: derruba as sessões abertas.
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    # Identifica o usuário na URL do webhook do Mercado Pago.
    webhook_key: Mapped[str] = mapped_column(String(64), unique=True, default=lambda: secrets.token_urlsafe(24))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Assinatura da plataforma: vencida, o bot do usuário para (o painel continua).
    subscription_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Lembretes de vencimento já enviados para o vencimento atual, ex.: "7,3" ("x" = aviso de vencida)
    platform_reminders_sent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Renovação automática no cartão (preapproval no Mercado Pago da plataforma)
    platform_preapproval_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    platform_recurring_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    platform_recurring_plan: Mapped[str | None] = mapped_column(String(32), nullable=True)
    platform_last_charge_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Isento pelo superadmin: o bot funciona sem assinatura.
    subscription_exempt: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)

    @property
    def area_name(self) -> str:
        return self.name or self.username

    @property
    def exempt(self) -> bool:
        """Não paga a plataforma: o superadmin e quem ele isentou."""
        return self.is_superadmin or bool(self.subscription_exempt)

    @property
    def recurring_active(self) -> bool:
        return self.platform_recurring_status == "authorized"

    @property
    def subscription_active(self) -> bool:
        if self.exempt:
            return True
        if self.subscription_until is None:
            return False
        # Com renovação automática, a cobrança do cartão pode cair horas depois do vencimento.
        grace = RECURRING_GRACE if self.recurring_active else timedelta(0)
        return self.subscription_until + grace > utcnow()

    @property
    def subscription_days_left(self) -> int | None:
        if self.exempt or self.subscription_until is None:
            return None
        return max(0, (self.subscription_until - utcnow()).days)


def tenant_fk() -> Mapped[int]:
    return mapped_column(ForeignKey("users.id"), index=True)


class Setting(Base):
    __tablename__ = "settings"

    tenant_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Plan(Base):
    __tablename__ = "plans"

    tenant_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    price_cents: Mapped[int] = mapped_column(Integer)
    # Preço no PayPal, na moeda de Configurações; vazio = plano sem PayPal
    foreign_price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    days: Mapped[int] = mapped_column(Integer)
    recurring: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class MessageTemplate(Base):
    __tablename__ = "message_templates"

    tenant_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    text: Mapped[str] = mapped_column(Text)


class CustomMessage(Base):
    """Resposta automática criada pelo usuário: o assinante manda uma palavra-gatilho e o bot responde."""

    __tablename__ = "custom_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = tenant_fk()
    title: Mapped[str] = mapped_column(String(64))
    # Uma palavra/frase por linha (mesma regra das palavras de entrada)
    triggers: Mapped[str] = mapped_column(Text, default="")
    text: Mapped[str] = mapped_column(Text)
    show_plans: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Member(Base):
    __tablename__ = "members"
    __table_args__ = (UniqueConstraint("tenant_id", "telegram_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = tenant_fk()
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    email: Mapped[str | None] = mapped_column(String(255))

    # none | active | expired
    status: Mapped[str] = mapped_column(String(16), default="none", index=True)
    plan_code: Mapped[str | None] = mapped_column(String(32))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    in_group: Mapped[bool] = mapped_column(Boolean, default=False)
    # offsets (em dias) dos lembretes já enviados para o vencimento atual, ex.: "7,3"
    reminders_sent: Mapped[str] = mapped_column(String(64), default="")

    # Assinatura recorrente no cartão (Mercado Pago preapproval)
    mp_preapproval_id: Mapped[str | None] = mapped_column(String(64))
    recurring_status: Mapped[str | None] = mapped_column(String(16))
    last_recurring_charge_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    payments: Mapped[list["Payment"]] = relationship(back_populates="member")

    @property
    def display_name(self) -> str:
        if self.first_name:
            return self.first_name
        if self.username:
            return f"@{self.username}"
        return str(self.telegram_id)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = tenant_fk()
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    plan_code: Mapped[str] = mapped_column(String(32))
    amount_cents: Mapped[int] = mapped_column(Integer)
    # Moeda de amount_cents; vazio = real (só o PayPal cobra em moeda estrangeira)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    # pix | pix_manual | card | card_recurring | paypal | manual (liberação pelo admin)
    method: Mapped[str] = mapped_column(String(16))
    # pending | approved | rejected | expired
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    external_ref: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    mp_id: Mapped[str | None] = mapped_column(String(64))
    pix_code: Mapped[str | None] = mapped_column(Text)
    checkout_url: Mapped[str | None] = mapped_column(Text)
    proof_file_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)

    member: Mapped[Member] = relationship(back_populates="payments")


class Event(Base):
    """Histórico usado nas estatísticas (novos, renovações, saídas)."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = tenant_fk()
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    # new | renew | reactivate | expired | removed | recurring_canceled | joined | left
    kind: Mapped[str] = mapped_column(String(32), index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# ------------------------------------------------- assinatura da plataforma


class PlatformSetting(Base):
    """Configurações da plataforma (Mercado Pago que recebe dos usuários, teste grátis...)."""

    __tablename__ = "platform_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class PlatformPlan(Base):
    """Planos que os usuários do SaaS assinam."""

    __tablename__ = "platform_plans"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    price_cents: Mapped[int] = mapped_column(Integer)
    days: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class PlatformPayment(Base):
    """Pagamento de um usuário à plataforma."""

    __tablename__ = "platform_payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    plan_code: Mapped[str] = mapped_column(String(32))
    days: Mapped[int] = mapped_column(Integer)
    amount_cents: Mapped[int] = mapped_column(Integer)
    # pix | card | manual (dias dados pelo superadmin)
    method: Mapped[str] = mapped_column(String(16))
    # pending | approved | rejected | expired
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    external_ref: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    mp_id: Mapped[str | None] = mapped_column(String(64))
    pix_code: Mapped[str | None] = mapped_column(Text)
    pix_qr_base64: Mapped[str | None] = mapped_column(Text)
    checkout_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)


# ------------------------------------------------------------- pedidos de ajuda


class SupportTicket(Base):
    """Pedido de ajuda de um usuário: uma conversa com o suporte (superadmin)."""

    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    subject: Mapped[str] = mapped_column(String(120))
    # open (aguardando o suporte) | answered (aguardando o usuário aprovar)
    # | resolved (aprovado) | closed (encerrado sem resposta do usuário)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    # Última resposta do suporte: o prazo de aprovação conta a partir dela.
    answered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[User] = relationship()
    messages: Mapped[list["SupportMessage"]] = relationship(
        back_populates="ticket", order_by="SupportMessage.id", cascade="all, delete-orphan"
    )

    @property
    def is_closed(self) -> bool:
        return self.status in ("resolved", "closed")


class SupportMessage(Base):
    __tablename__ = "support_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("support_tickets.id"), index=True)
    # None = mensagem do sistema (aprovação, encerramento automático...)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    is_staff: Mapped[bool] = mapped_column(Boolean, default=False)
    # message | approved | rejected | auto_closed
    kind: Mapped[str] = mapped_column(String(16), default="message")
    text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    ticket: Mapped[SupportTicket] = relationship(back_populates="messages")
