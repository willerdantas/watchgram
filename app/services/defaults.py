"""Valores iniciais: configurações, planos e mensagens automáticas."""

DEFAULT_SETTINGS: dict[str, str] = {
    "bot_token": "",
    "group_chat_id": "",
    "admin_ids": "",
    "support_contact": "",
    "mp_access_token": "",
    "mp_webhook_secret": "",
    "pix_key": "",
    "pix_holder": "",
    "pix_city": "",
    "enable_pix_auto": "1",
    "enable_pix_manual": "0",
    "pix_manual_qr": "0",  # QR Code e copia e cola com o valor do plano no Pix manual
    "enable_card": "1",
    "enable_paypal": "0",
    "paypal_client_id": "",
    "paypal_client_secret": "",
    "paypal_sandbox": "0",
    "paypal_currency": "USD",
    "grace_hours": "24",
    "reminder_days": "7,3,1,0",
    "remove_on_expire": "1",
    "detected_chats": "[]",
    "start_keywords": (
        "oi\nolá\nola\noie\nbom dia\nboa tarde\nboa noite\nopa\neae\ne aí\nhey\nhello\nhi\n"
        "menu\nplanos\nassinar\nquero assinar\nquero entrar\nvalores\npreço\ncomeçar\niniciar"
    ),
    "reply_unknown": "1",
    # Alertas para o dono da área
    "alert_sale": "1",
    "alert_joined": "0",
    "alert_left": "0",
    "alert_exits": "0",
    "alert_exits_min": "5",
    "alert_exits_date": "",
    "alert_weekly": "0",
    "alert_weekly_date": "",
    "alert_telegram": "1",
    "alert_email": "0",
    "alert_email_to": "",
}

# Configurações sensíveis: o painel nunca as exibe de volta.
SECRET_SETTINGS = {"bot_token", "mp_access_token", "mp_webhook_secret", "paypal_client_secret"}

DEFAULT_PLANS = [
    {
        "code": "mensal",
        "name": "Mensal",
        "description": "Acesso por 30 dias, pagamento único.",
        "price_cents": 2990,
        "days": 30,
        "recurring": False,
        "sort_order": 1,
    },
    {
        "code": "mensal_recorrente",
        "name": "Mensal recorrente",
        "description": "Cobrança automática todo mês no cartão. Cancele quando quiser.",
        "price_cents": 2490,
        "days": 30,
        "recurring": True,
        "sort_order": 2,
    },
    {
        "code": "trimestral",
        "name": "Trimestral",
        "description": "Acesso por 90 dias, pagamento único.",
        "price_cents": 7990,
        "days": 90,
        "recurring": False,
        "sort_order": 3,
    },
    {
        "code": "semestral",
        "name": "Semestral",
        "description": "Acesso por 180 dias, pagamento único.",
        "price_cents": 14990,
        "days": 180,
        "recurring": False,
        "sort_order": 4,
    },
]

# key -> (título exibido no painel, texto padrão, placeholders disponíveis)
DEFAULT_MESSAGES: dict[str, tuple[str, str, str]] = {
    "welcome": (
        "Boas-vindas (/start ou palavra de entrada)",
        "{saudacao}, {nome}! 👋\n\nBem-vindo(a) ao nosso grupo VIP. Escolha um plano abaixo:",
        "{nome} {saudacao}",
    ),
    "unknown_text": (
        "Mensagem não reconhecida",
        "Desculpe, {nome}, não entendi 🤔\n\nToque no botão abaixo para ver os planos ou use /status para ver sua assinatura.",
        "{nome} {saudacao}",
    ),
    "choose_method": (
        "Escolha da forma de pagamento",
        "<b>{plano}</b> — {valor}\n{descricao}\n\nComo você quer pagar?",
        "{nome} {plano} {valor} {descricao}",
    ),
    "ask_email": (
        "Pedido de e-mail",
        "Para emitir a cobrança, envie seu <b>e-mail</b>:",
        "{nome}",
    ),
    "pix_created": (
        "Pix automático gerado",
        "Pix de <b>{valor}</b> gerado para o plano {plano}.\n\n"
        "Copie o código abaixo e pague no app do seu banco. "
        "A liberação é automática, em poucos segundos.\n\n<code>{pix_codigo}</code>",
        "{nome} {plano} {valor} {pix_codigo}",
    ),
    "pix_manual": (
        "Pix manual (chave)",
        "Faça um Pix de <b>{valor}</b> para a chave abaixo:\n\n<code>{pix_chave}</code>\n"
        "Titular: {pix_titular}\n\nDepois <b>envie aqui a foto ou o PDF do comprovante</b>.",
        "{nome} {plano} {valor} {pix_chave} {pix_titular}",
    ),
    "pix_manual_qr": (
        "Pix manual com QR Code",
        "Pague <b>{valor}</b> lendo o QR Code acima ou com o Pix copia e cola:\n\n<code>{pix_codigo}</code>\n\n"
        "Titular: {pix_titular}\n\nDepois <b>envie aqui a foto ou o PDF do comprovante</b>.",
        "{nome} {plano} {valor} {pix_codigo} {pix_chave} {pix_titular}",
    ),
    "proof_received": (
        "Comprovante recebido",
        "Comprovante recebido! ✅ Assim que for conferido, você recebe o acesso aqui.",
        "{nome} {plano} {valor}",
    ),
    "card_link": (
        "Link de pagamento com cartão",
        "Toque no botão abaixo para pagar <b>{valor}</b> com cartão. "
        "A liberação é automática após a aprovação.",
        "{nome} {plano} {valor}",
    ),
    "paypal_link": (
        "Link de pagamento PayPal",
        "Toque no botão abaixo para pagar <b>{valor}</b> com PayPal. "
        "A liberação é automática após a confirmação.",
        "{nome} {plano} {valor}",
    ),
    "recurring_link": (
        "Link da assinatura recorrente",
        "Toque no botão abaixo para ativar a assinatura de <b>{valor}/mês</b> no cartão.\n"
        "Você pode cancelar quando quiser com /cancelar.",
        "{nome} {plano} {valor}",
    ),
    "payment_approved": (
        "Pagamento aprovado (primeiro acesso)",
        "✅ Pagamento confirmado, {nome}!\n\nPlano: {plano}\nAcesso válido até <b>{vencimento}</b>.\n\n"
        "Toque no botão abaixo para entrar no grupo:",
        "{nome} {plano} {valor} {vencimento}",
    ),
    "renewed": (
        "Renovação confirmada",
        "✅ Renovação confirmada, {nome}! Seu novo vencimento é <b>{vencimento}</b>.",
        "{nome} {plano} {valor} {vencimento}",
    ),
    "payment_rejected": (
        "Pagamento recusado",
        "❌ Não conseguimos confirmar seu pagamento. Escolha um plano abaixo para tentar novamente "
        "ou fale com o suporte: {suporte}",
        "{nome} {plano} {valor} {suporte}",
    ),
    "reminder": (
        "Lembrete de vencimento",
        "⏰ {nome}, sua assinatura vence em <b>{dias} dia(s)</b> ({vencimento}).\n"
        "Renove agora para não perder o acesso:",
        "{nome} {plano} {dias} {vencimento}",
    ),
    "reminder_today": (
        "Lembrete no dia do vencimento",
        "⚠️ {nome}, sua assinatura vence <b>hoje</b> ({vencimento}). Renove para continuar no grupo:",
        "{nome} {plano} {vencimento}",
    ),
    "expired": (
        "Assinatura expirada",
        "Sua assinatura expirou e o acesso ao grupo foi encerrado. 😢\n"
        "Quando quiser voltar, é só escolher um plano:",
        "{nome} {plano} {vencimento}",
    ),
    "status_active": (
        "Status: assinatura ativa (/status)",
        "📋 Plano: <b>{plano}</b>\nVencimento: <b>{vencimento}</b>\nRecorrência: {recorrencia}",
        "{nome} {plano} {vencimento} {recorrencia}",
    ),
    "status_none": (
        "Status: sem assinatura (/status)",
        "Você não tem uma assinatura ativa. Escolha um plano abaixo para assinar:",
        "{nome}",
    ),
    "join_denied": (
        "Entrada no grupo recusada",
        "Seu pedido para entrar no grupo foi recusado porque não encontramos uma assinatura ativa. "
        "Escolha um plano abaixo para assinar:",
        "{nome}",
    ),
    "recurring_canceled": (
        "Recorrência cancelada",
        "Sua cobrança recorrente foi cancelada. Seu acesso continua até <b>{vencimento}</b>.",
        "{nome} {vencimento}",
    ),
    "admin_new_sale": (
        "Aviso ao admin: nova venda",
        "💰 Nova venda: {nome} (id {telegram_id}) — {plano} — {valor} via {metodo}",
        "{nome} {telegram_id} {plano} {valor} {metodo}",
    ),
}
