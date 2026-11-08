# SaaS de assinaturas para grupos do Telegram: análise de viabilidade

> Data: 02/11/2026 · Referência analisada: https://botgram.club/

---

## 1. Resumo executivo

| Pergunta | Resposta curta |
|---|---|
| Tecnicamente viável? | **Sim.** A Bot API do Telegram cobre tudo: link de convite individual, aprovação de pedido de entrada, remoção e mensagem privada. |
| Existe código pronto para reaproveitar? | **Só em parte.** Não há no GitHub um SaaS multi-tenant com Pix e cartão. Existem bots single-tenant (um grupo, um dono) e boas referências de arquitetura com licença MIT. |
| Comercialmente viável? | **Sim, com ressalvas.** O mercado está validado (Botgram, BotVips, Nexbo, PinkBot, ApexVips), mas é competitivo e a margem por transação é estreita. |
| Maiores riscos | (1) Política do Telegram que exige **Telegram Stars** para bens digitais. (2) Risco **regulatório** se a plataforma guardar o dinheiro dos clientes. (3) **Chargeback** e conteúdo +18 levando a bloqueio no gateway. |
| Decisão-chave de arquitetura | **Não custodiar dinheiro.** Usar **subcontas com split** do gateway (Asaas, Efí, Mercado Pago Marketplace ou Pagar.me): o dinheiro cai direto na conta do criador e a plataforma retém só a comissão. |
| Custo de infraestrutura no MVP | **R$ 60 a R$ 250 por mês**, mais o domínio (cerca de R$ 40 por ano). |
| Prazo do MVP (1 dev) | **6 a 10 semanas.** |

---

## 2. Análise do Botgram (referência de mercado)

**Empresa:** Benet Digital LTDA, CNPJ 41.913.958/0001-50. As transações são processadas pela "BenetCore", estrutura financeira própria.

### Funcionalidades
- Controle de acesso automático: o bot vira admin, "tranca" o grupo e remove quem não pagou.
- Checkout próprio com Pix e cartão, e compra dentro do Telegram (`/buy`).
- Convites manuais gratuitos com data de expiração.
- **Modo privacidade:** a venda aparece em nome da Botgram, sem expor nome ou CPF do criador.
- Métricas: MRR, churn, receita nova e perdida, exportação de assinantes.
- Múltiplos grupos e canais por conta, com mapeamento de qual plano dá acesso a qual espaço.
- Pesquisa de motivo de cancelamento.
- Checkout personalizado (logo e cores).
- SMS com o convite.
- Integração com plataformas de afiliados: Hotmart, Kiwify, Eduzz, Braip, Monetizze, Kirvano, HeroSpark e Doppus.
- Carteira interna com saque via Pix. Em breve: USDT e multimoeda.

### Planos

| | Basic | Starter | Pro |
|---|---|---|---|
| Mensalidade | US$ 0 | US$ 29 | US$ 39 |
| Taxa | 7,9% + US$ 0,99 | 6,9% + US$ 0,69 | 4,9% + US$ 0,29 |
| Liberação Pix | D+1 | 1 h | imediata |
| Liberação cartão | D+30 | D+30 | D+30 |
| Espaços | 1 | 2 | 10 |
| Convites manuais | 10 | 50 | 500 |
| Afiliados | não | sim (0% de taxa) | sim (0% de taxa) |

### Leitura estratégica
- O modelo de receita é **take rate** (percentual sobre o volume transacionado) mais assinatura opcional. A taxa efetiva é alta: em uma mensalidade de R$ 30 no plano Basic, a cobrança passa de **20%**.
- A carteira própria com D+30 no cartão indica que **eles custodiam o dinheiro**, o que exige estrutura financeira e de compliance que um projeto novo não deve replicar no início.
- **Brechas para diferenciação:**
  1. Taxa fixa menor ou preço em **R$** em vez de US$.
  2. **Bot white-label:** o cliente usa o próprio bot criado no @BotFather, com nome e foto dele.
  3. Cobrança recorrente real via **Pix Automático** (Banco Central, em vigor desde 06/2025). Sem ele, a renovação por Pix depende de o membro pagar manualmente.
  4. Lembretes por **WhatsApp e e-mail**, além do Telegram.
  5. Recuperação de inadimplentes: dunning com cupom de retorno.

### Outros concorrentes
- BotVips, com app iOS e Pix instantâneo.
- Nexbo.vip.
- PinkBot ("gerenciador de grupos VIP", sem mensalidade).
- ApexVips.
- HeroSpark, Hotmart e Kiwify, combinados com um bot porteiro.
- **O próprio Telegram:** links de convite por assinatura pagos em Stars (`createChatSubscriptionInviteLink`, ciclo de 30 dias). É gratuito para o criador, mas não tem Pix nem gestão.

---

## 3. Repositórios no GitHub: o que dá para aproveitar

A busca foi feita em 02/11/2026. **Nenhum repositório entrega o produto completo** (multi-tenant, painel web, Pix e cartão, split).

| Repositório | Licença | Stack | O que aproveitar | Observação |
|---|---|---|---|---|
| [snoups/remnashop](https://github.com/snoups/remnashop) | MIT · 533★ · ativo | Python, aiogram 3 | **Arquitetura do bot**: estrutura de pastas, i18n, menus admin dentro do bot, múltiplos gateways, Docker | O domínio é venda de VPN. É a melhor referência de código "de produção". |
| [rovidev95/rovidev-membership-bot](https://github.com/rovidev95/rovidev-membership-bot) | MIT · novo | Python, FastAPI | **Núcleo do ciclo de vida** (`pending → active → canceled → expired/revoked`), webhooks idempotentes, varredura de expiração, adapters com fakes para teste | Só tem Stripe e é single-tenant. É a melhor base para a camada de domínio. |
| [armysarge/Telegram-Subscription-Bot](https://github.com/armysarge/Telegram-Subscription-Bot) | MIT | Node.js, MongoDB | Referência de **funcionalidades**: multi-grupo, analytics, trial e restrição de não-assinantes | Usa gateway PayFast (África do Sul). |
| [lipehsz05/bot-pagamentos-telegram-pushinpay](https://github.com/lipehsz05/bot-pagamentos-telegram-pushinpay) | **sem licença** | Python, SQLite | Fluxo de Pix com PushinPay e expulsão automática | **Sem licença não se pode copiar o código**: use só como referência. |
| [FabioSilva11/Bot-de-Pagamentos-PIX-com-MercadoPago-e-Telegram](https://github.com/FabioSilva11/Bot-de-Pagamentos-PIX-com-MercadoPago-e-Telegram) | **sem licença** | Python | Exemplo de integração Pix com Mercado Pago | Mesma restrição de licença. |
| [ivansantosbom/bot_gerenciador_grupos_vip_telegram](https://github.com/ivansantosbom/bot_gerenciador_grupos_vip_telegram) | GPL-3.0 | Shell script | Lista de funcionalidades (cobrança por hora, dia ou mês; disparo em massa) | Stack inviável, e a licença é copyleft. |
| [env0id/gatekeeper](https://github.com/env0id/gatekeeper) | Apache-2.0 | aiogram, Stripe | — | O código real é vendido no Patreon. |

**Recomendação:** criar um projeto próprio e:
- copiar e adaptar o **padrão de domínio e idempotência** do `rovidev-membership-bot` (MIT, manter o aviso de copyright);
- seguir a **organização do bot e do i18n** do `remnashop` (MIT);
- escrever do zero os adapters de pagamento brasileiros (Asaas, Efí ou Mercado Pago).

---

## 4. Riscos críticos (avaliar antes de codar)

### 4.1 Política do Telegram: Telegram Stars
Desde 12/06/2024, o Telegram exige que pagamentos de **bens e serviços digitais feitos dentro de bots e mini apps** usem **Telegram Stars** (moeda `XTR`). Ele já suspendeu bots que descumpriram a regra.

- **Na prática:** concorrentes como o Botgram mandam o membro para um **checkout web externo** (link) e não usam `sendInvoice` com Pix ou cartão dentro do Telegram. Mesmo assim, é uma área cinzenta e o risco de suspensão existe.
- **Mitigações:**
  1. O checkout é **sempre** uma página web (domínio próprio). Nunca usar a Payments API nativa com provedor fiat.
  2. **Modelo white-label:** cada cliente cadastra o token do próprio bot. Se um bot for banido, os demais clientes não são afetados.
  3. Oferecer **Stars como meio de pagamento adicional**. A Bot API permite isso, e o Telegram fica com a parte dele.
  4. O bot nunca deve ser o único canal: guardar e-mail e telefone do membro para reemitir o acesso.

### 4.2 Regulatório: custódia de dinheiro
Se a plataforma recebe o dinheiro e repassa depois (carteira, saque), ela atua como **subcredenciador ou instituição de pagamento**. Isso traz obrigações junto ao Banco Central, como PLD/FT, KYC e possível autorização, além de risco fiscal de bitributação.

**Solução:** usar **subcontas e split nativos do gateway**:
- o criador abre uma subconta via API, com KYC feito pelo próprio gateway;
- cada cobrança é criada na subconta dele com **split** da comissão da plataforma;
- a plataforma nunca toca no dinheiro do criador e emite nota fiscal **apenas da sua comissão**.

### 4.3 Conteúdo e chargeback
Grande parte dos grupos VIP no Brasil é de conteúdo +18, sinais e apostas, ou "mentorias" de baixa qualidade. Gateways bloqueiam contas por isso, e o cartão traz **chargeback**.

**Mitigações:**
- **Política de uso aceitável** clara nos Termos de Uso, definindo o que é proibido;
- KYC do criador (feito pelo gateway);
- limite de exposição a cartão para contas novas;
- antifraude do gateway ligado;
- reserva de saldo, definida pelo gateway.

### 4.4 Limitações técnicas do Telegram
- O bot **só envia mensagem privada a quem já deu `/start`**. Por isso o fluxo de compra precisa começar no bot (deep link `t.me/SeuBot?start=plano_x`), o que garante o canal para os lembretes de vencimento.
- Limites de envio: cerca de **30 mensagens por segundo** por bot e cerca de **20 mensagens por minuto** por grupo. Os lembretes precisam passar por uma **fila com rate limiting**.
- Para remover sem banir de vez: `banChatMember` seguido de `unbanChatMember` (com `only_if_banned`).
- O bot precisa ser **admin** com as permissões "Convidar usuários" e "Banir usuários".

---

## 5. Requisitos funcionais

### 5.1 Criador (cliente do SaaS): painel web
- **RF01** Cadastro e login (e-mail e senha, magic link ou login com Telegram), com 2FA opcional.
- **RF02** Onboarding financeiro: criar subconta no gateway (KYC com CPF ou CNPJ, dados bancários e documentos).
- **RF03** Conectar o bot: colar o token do @BotFather (white-label) **ou** usar o bot central da plataforma. Validar com `getMe` e registrar o webhook.
- **RF04** Conectar grupos e canais: adicionar o bot como admin, detectar o chat via `my_chat_member` e validar as permissões.
- **RF05** Planos: nome, preço, periodicidade (dia, semana, mês, trimestre, ano, vitalício ou prazo fixo), período de teste, quais espaços o plano libera e limite de vagas.
- **RF06** Cupons de desconto (percentual ou valor, validade, limite de uso).
- **RF07** Convites manuais e gratuitos com data de expiração.
- **RF08** Gestão de membros: lista, filtros por status, busca, adicionar ou remover manualmente, estender prazo, exportar CSV.
- **RF09** Mensagens configuráveis: boas-vindas, lembretes (D-7, D-3, D-1, D0), aviso de expiração e pós-remoção (oferta de retorno).
- **RF10** Disparo em massa (broadcast) para assinantes ativos, expirados ou todos, respeitando o rate limit.
- **RF11** **Estatísticas:** MRR, receita bruta e líquida, novos assinantes, cancelamentos, churn %, LTV, ticket médio, taxa de renovação, conversão do funil (`/start` → checkout → pago) e divisão entre Pix e cartão.
- **RF12** Extrato de vendas, taxas e repasses (lidos do gateway).
- **RF13** Personalização do checkout (logo, cores, textos).
- **RF14** *(fase 2)* Webhooks de entrada de Hotmart, Kiwify e Eduzz (venda externa libera o acesso).
- **RF15** *(fase 2)* Programa de afiliados próprio, links de rastreio e UTM.
- **RF16** Notificação de venda para o criador (no Telegram e por e-mail).

### 5.2 Membro (assinante): bot e checkout
- **RF20** `/start` mostra os planos disponíveis com botões inline.
- **RF21** Checkout web: coleta nome, e-mail, CPF (exigido no Pix por alguns gateways) e telefone.
- **RF22** **Pix:** QR Code com "copia e cola" e expiração. A confirmação via webhook libera o acesso em segundos.
- **RF23** **Cartão:** assinatura recorrente com tokenização no gateway (sem armazenar dados de cartão, pelo PCI-DSS) e 3DS quando disponível.
- **RF24** **Pix Automático** (recorrência autorizada uma vez) quando o gateway suportar. Senão, Pix avulso a cada ciclo com lembrete e link.
- **RF25** Liberação de acesso: **link de convite com `creates_join_request=true`**. O bot aprova só o `user_id` que pagou (`approveChatJoinRequest`), o que impede repassar o link. Alternativa: link com `member_limit=1` e `expire_date`.
- **RF26** `/status`: mostra plano, vencimento e link de renovação.
- **RF27** `/cancelar`: cancela a recorrência no cartão e pergunta o motivo do cancelamento.
- **RF28** Lembretes automáticos de vencimento com botão "Renovar agora".
- **RF29** Período de carência configurável (por exemplo, 24 h) antes da remoção.
- **RF30** Remoção automática ao expirar, com mensagem de "volte quando quiser".

### 5.3 Proteção do grupo
- **RF40** Ao entrar alguém (`chat_member` ou `chat_join_request`) sem assinatura ativa: recusar ou remover.
- **RF41** Varredura periódica (por exemplo, a cada hora) que compara os membros conhecidos com as assinaturas ativas.
- **RF42** Log de auditoria de cada entrada, remoção e ação manual.

> **Limitação:** a Bot API **não lista todos os membros de um grupo**. O bot só conhece quem entrou depois que ele virou admin, ou quem ele aprovou. No onboarding, é preciso orientar o criador a **revogar os links antigos** e, de preferência, começar com um grupo novo ou "limpo".

### 5.4 Administração da plataforma (você)
- **RF50** Painel super-admin: clientes, volume, receita da plataforma, bloqueio de contas.
- **RF51** Planos do próprio SaaS (taxa por plano e mensalidade).
- **RF52** Monitoramento de webhooks com falha e reprocessamento.

---

## 6. Requisitos não funcionais
- **Multi-tenant:** isolamento por `tenant_id` em todas as tabelas. No Postgres, usar Row Level Security se for Supabase.
- **Idempotência:** todo webhook (Telegram e gateway) é registrado pelo `event_id` antes de ser processado.
- **Confiabilidade:** filas com retry e backoff para chamadas ao Telegram e ao gateway; dead letter queue.
- **Latência:** do Pix pago ao convite enviado em **menos de 10 s**.
- **Segurança:**
  - tokens de bot e chaves de API **criptografados** em repouso;
  - validação da assinatura dos webhooks do gateway;
  - `secret_token` no webhook do Telegram;
  - HTTPS obrigatório;
  - rate limit no checkout.
- **LGPD:**
  - política de privacidade;
  - base legal (execução de contrato);
  - minimização de dados (CPF só se o gateway exigir);
  - exclusão a pedido do titular;
  - DPA com gateways e provedores;
  - papéis definidos: o criador é o **controlador** dos dados dos membros e a plataforma é a **operadora**.
- **Observabilidade:** logs estruturados, Sentry e uptime monitor.
- **Backup:** backup diário do banco com retenção mínima de 7 dias.
- **Escalabilidade:** o bot via webhook é stateless e escala na horizontal. Os jobs de expiração e lembrete rodam em um worker separado.

---

## 7. Arquitetura proposta

```
                ┌──────────────── Telegram ────────────────┐
                │  Bot(s) white-label  /  grupos e canais  │
                └───────────────┬──────────────────────────┘
                                │ webhook  /tg/{bot_id}
┌──────────────┐   HTTPS   ┌────▼─────────────────────────┐     ┌──────────────┐
│ Painel web   │──────────▶│  API (FastAPI)               │◀────│ Gateway      │
│ (criador/adm)│           │  - handlers do bot (aiogram) │ wh  │ Asaas / Efí /│
└──────────────┘           │  - checkout + webhooks pgto  │     │ Mercado Pago │
┌──────────────┐           │  - REST p/ painel            │────▶│ (subcontas + │
│ Checkout web │──────────▶│                              │     │  split)      │
│ (membro)     │           └───┬──────────────┬───────────┘     └──────────────┘
└──────────────┘               │              │
                         ┌─────▼─────┐  ┌─────▼──────┐   ┌──────────────────┐
                         │ PostgreSQL│  │   Redis    │◀──│ Worker (arq /    │
                         │ (dados)   │  │ fila/cache │   │ Celery): lembrete│
                         └───────────┘  └────────────┘   │ expiração, sweep │
                                                         └──────────────────┘
```

### Stack recomendada
Esta stack segue o ecossistema Python que você já usa (Supabase).
- **Backend:** Python 3.12, **FastAPI** e **aiogram 3** (webhook, com vários bots no mesmo processo).
- **Banco:** **PostgreSQL** (Supabase ou um container próprio), com SQLAlchemy 2 e Alembic.
- **Fila e agendamento:** **Redis** com **arq** (ou Celery Beat) para lembretes, expiração e broadcast.
- **Painel:** Next.js (React) ou, para ir mais rápido, templates do próprio FastAPI com HTMX. Gráficos com Chart.js.
- **Checkout:** página leve (HTML/HTMX ou Next.js) no subdomínio `pay.seudominio.com.br`.
- **E-mail:** Resend, Brevo ou Amazon SES. **SMS e WhatsApp** *(fase 2)*: Z-API, Twilio ou a API oficial da Meta.
- **Infraestrutura:** Docker Compose com Caddy (TLS automático) em uma VPS.

### Modelo de dados (núcleo)
- `tenants`: o criador, com plano do SaaS e subconta no gateway.
- `bots`: tenant, token criptografado, username e segredo do webhook.
- `spaces`: tenant, `chat_id`, tipo (grupo ou canal) e permissões verificadas.
- `plans`: tenant, preço, período, trial e espaços liberados (via `plan_spaces`).
- `members`: tenant, `telegram_user_id`, nome, e-mail, telefone e CPF opcional.
- `subscriptions`: membro, plano, status, `current_period_end`, método e id no gateway.
- `payments`: assinatura, valor, taxa, status, id no gateway e método.
- `access_grants`: assinatura, espaço, link de convite, `granted_at` e `revoked_at`.
- `webhook_events`: origem, `event_id` (único) e payload.
- `messages_log` e `audit_log`.

---

## 8. Gateways de pagamento: o que escolher

> Taxas mudam com frequência e variam por volume. **Confirme na página oficial** antes de definir o preço. Os valores abaixo são apenas indicativos.

| Gateway | Subconta + split via API | Pix | Cartão recorrente | Pix Automático | Observação |
|---|---|---|---|---|---|
| **Asaas** | Sim (subcontas e split nativos) | Sim (taxa fixa por Pix) | Sim (assinaturas) | Verificar | API simples e muito usada por SaaS no Brasil. **Boa primeira opção.** |
| **Efí (ex-Gerencianet)** | Sim (split e marketplace) | Sim, com Pix forte | Sim | Verificar | Forte em Pix e documentação boa. |
| **Mercado Pago** | Sim (Marketplace via OAuth e `marketplace_fee`) | Sim | Sim (preapproval) | Verificar | O criador conecta a própria conta MP, e o onboarding é fácil para quem já tem conta. |
| **Pagar.me (Stone)** | Sim (recipients e split) | Sim | Sim | Verificar | Mais "enterprise". |
| **Stripe Connect** | Sim | Verificar para contas BR | Sim, excelente | Não | Ótimo para expansão internacional. |

**Recomendação no MVP:**
- **Asaas** (ou Efí) para Pix e cartão no Brasil, com subcontas.
- Desenhar uma interface `PaymentProvider` desde o início, para adicionar Mercado Pago e Stripe depois.

---

## 9. Hospedagem, domínio e serviços

### 9.1 O mínimo para produção (MVP, até cerca de 50 clientes e algumas dezenas de milhares de membros)

| Item | Opção | Custo estimado |
|---|---|---|
| **VPS** (API, bot, worker, Redis e Caddy em Docker) | Hetzner CX22/CPX21 (2 a 3 vCPU, 4 GB) · Contabo · Hostinger KVM 2 · Magalu Cloud (dados no Brasil) | **R$ 30 a R$ 80 por mês** |
| **Banco PostgreSQL** | Supabase Free, depois Pro (US$ 25 por mês, com backup) · ou Postgres na própria VPS com backup para S3 | **R$ 0 a R$ 150 por mês** |
| **Domínio** | `.com.br` no **Registro.br** (exige CPF ou CNPJ) · ou `.com` na Cloudflare Registrar | **cerca de R$ 40 por ano** (.com.br) · cerca de US$ 10 por ano (.com) |
| **DNS + CDN + proteção DDoS** | **Cloudflare** (plano Free) | R$ 0 |
| **Certificado SSL** | Let's Encrypt (automático pelo Caddy) | R$ 0 |
| **E-mail transacional** | Resend (3 mil por mês grátis) · Brevo (300 por dia grátis) · Amazon SES | R$ 0 a R$ 50 |
| **E-mail corporativo** (`contato@`) | Zoho Mail Free · Google Workspace | R$ 0 a R$ 35 por usuário |
| **Monitoramento de erros** | Sentry (plano Free) | R$ 0 |
| **Uptime** | UptimeRobot ou Better Stack (Free) | R$ 0 |
| **Backup externo** | Backblaze B2 · Cloudflare R2 | R$ 0 a R$ 10 |
| **Repositório e CI** | GitHub com Actions | R$ 0 |
| **Total** | | **cerca de R$ 60 a R$ 250 por mês** |

### 9.2 Requisitos técnicos da hospedagem
- **IP público com HTTPS na porta 443.** O webhook do Telegram aceita as portas 443, 80, 88 e 8443, e o certificado precisa ser válido.
- Processos **24/7** com restart automático (Docker `restart: unless-stopped` ou systemd).
- **Relógio sincronizado (NTP) e fuso `America/Sao_Paulo`** nos jobs de vencimento (gravar em UTC no banco).
- Saída de rede liberada para `api.telegram.org` e para a API do gateway.
- Para LGPD, servidor no Brasil é **opcional**, mas facilita: Magalu Cloud, AWS São Paulo, Oracle São Paulo e Locaweb.

### 9.3 Subdomínios sugeridos
- `seudominio.com.br`: landing page e marketing.
- `app.seudominio.com.br`: painel do criador.
- `pay.seudominio.com.br`: checkout.
- `api.seudominio.com.br`: API e webhooks (`/tg/{bot_id}`, `/pay/{provider}`).

### 9.4 Quando escalar (mais de 200 clientes)
- Separar API e worker em VMs ou containers distintos.
- Banco gerenciado com réplica, Redis gerenciado (Upstash) e um balanceador.
- Custo estimado: R$ 500 a R$ 1.500 por mês.

---

## 10. Requisitos legais e administrativos
- **CNPJ** (ME ou LTDA; o MEI não cobre a atividade de software, verificar o CNAE com um contador). É necessário para:
  - a conta PJ no gateway com subcontas e split;
  - emitir NFS-e da comissão;
  - transmitir credibilidade.
- CNAEs típicos: 6311-9/00 (tratamento de dados e hospedagem) e/ou 6203-1/00 (software não customizável). **Validar com contador**, assim como o regime (Simples Nacional, anexo III ou V e Fator R).
- **Termos de Uso**, com política de uso aceitável, responsabilidade do criador pelo conteúdo e regras de reembolso e chargeback.
- **Política de Privacidade** (LGPD) e **acordo de tratamento de dados** com os criadores.
- **CDC:** direito de arrependimento de 7 dias em compras online. Definir como o reembolso funciona.
- Registro da marca no **INPI** (opcional, mas recomendado antes de investir em marketing).

---

## 11. Viabilidade econômica (cenário ilustrativo)

**Premissas:**
- comissão da plataforma de **4,9% + R$ 0,99** (abaixo do plano Basic do Botgram e com preço em R$);
- o custo do gateway é **pago pelo criador** dentro da própria subconta (modelo split), ou embutido na taxa.

| Cenário | Clientes | Assinantes/cliente | Ticket | GMV mensal | Receita da plataforma* | Infra | Resultado bruto |
|---|---|---|---|---|---|---|---|
| Inicial | 10 | 50 | R$ 40 | R$ 20 mil | cerca de R$ 1,5 mil | R$ 150 | cerca de R$ 1,3 mil |
| Tração | 50 | 100 | R$ 40 | R$ 200 mil | cerca de R$ 14,8 mil | R$ 400 | cerca de R$ 14 mil |
| Escala | 300 | 150 | R$ 45 | R$ 2 milhões | cerca de R$ 143 mil | R$ 1,5 mil | cerca de R$ 140 mil |

\* 4,9% do GMV mais R$ 0,99 por transação, antes de impostos (Simples: 6% a 15,5%), marketing e suporte.

**Leitura:**
- O custo de infraestrutura é irrelevante. O produto é **leve**.
- O gargalo é **aquisição de clientes**, com custo e tempo de vendas, e **suporte**.
- Com 50 clientes médios o projeto já se paga para uma pessoa. O canal de vendas principal tende a ser parcerias com infoprodutores e afiliados, além de conteúdo no Telegram, Instagram e YouTube.
- **Diferenciais que justificam a troca:**
  - preço em R$, com taxa menor;
  - bot white-label;
  - recorrência real (Pix Automático e cartão);
  - lembretes por WhatsApp;
  - recuperação de inadimplentes.

---

## 12. Roadmap sugerido

**MVP (6 a 10 semanas):**
1. Base: FastAPI, aiogram, Postgres, Redis e Docker; multi-tenant e auth do painel.
2. Conexão de bot (token white-label) e de grupos e canais.
3. Planos, checkout web e **Asaas**: subconta, split, Pix e cartão recorrente.
4. Liberação por join request com aprovação; remoção automática; varredura periódica.
5. Lembretes de vencimento (D-3, D-1, D0) e carência.
6. Painel: membros, vendas e métricas básicas (MRR, ativos, novos, churn).
7. Termos, privacidade, landing page e deploy em produção.

**Fase 2:**
- cupons, convites manuais e broadcast;
- Mercado Pago;
- Pix Automático;
- webhooks de Hotmart, Kiwify e Eduzz;
- WhatsApp e e-mail;
- motivo de cancelamento;
- exportação CSV.

**Fase 3:**
- afiliados próprios;
- Telegram Stars como meio de pagamento;
- Stripe internacional;
- app mobile e PWA para o criador;
- API pública.

---

## 13. Próximas decisões (para você)
1. **Gateway inicial:** Asaas, Efí ou Mercado Pago? (Recomendação: Asaas.)
2. **Modelo de bot:** white-label (token do cliente), bot central único ou os dois?
3. **Painel:** Next.js (mais bonito e escalável) ou HTMX server-side (mais rápido de entregar)?
4. **Banco:** Supabase gerenciado ou Postgres na VPS?
5. **Nicho de entrada:** educação e mentorias, trading e sinais, comunidades? Isso afeta a política de conteúdo e o risco no gateway.
