# Bot de Assinaturas para Telegram

Bot que vende acesso a um grupo ou canal VIP do Telegram. Ele inclui e remove membros automaticamente, avisa antes do vencimento e recebe por **Pix** e **cartão** pelo Mercado Pago. Vem com um **painel web** de estatísticas e configuração.

É **multiusuário (SaaS)**: cada usuário criado pelo superadmin tem a própria área, com bot, grupo, conta do Mercado Pago, planos, mensagens, membros, pagamentos e estatísticas. Um usuário nunca vê a área de outro.

> Análise de mercado e viabilidade: [docs/ANALISE_VIABILIDADE.md](docs/ANALISE_VIABILIDADE.md)

## Funcionalidades

**Assinante (no Telegram)**
- `/start` lista os planos: **Mensal**, **Mensal recorrente**, **Trimestral** e **Semestral**.
- A conversa também começa com saudações como "Oi", "Olá", "Bom dia", "Boa tarde" e "Boa noite". A lista é configurável no painel, e a resposta usa a saudação certa para o horário.
- Formas de pagamento:
  - **Pix automático** (QR Code do Mercado Pago, liberação em segundos);
  - **Cartão** (link do Mercado Pago);
  - **Recorrente no cartão** (cobrança automática todo mês);
  - **Pix manual na sua chave**, com envio de comprovante e aprovação pelo admin. Opcionalmente o bot envia **QR Code e Pix copia e cola já com o valor do plano** (BR Code estático gerado no próprio app, sem banco nem API; ligue em Configurações e informe a cidade do titular);
  - **PayPal em moeda estrangeira** (USD, EUR, GBP, CAD, AUD ou CHF), pagamento único, com preço próprio por plano e liberação automática.
- Depois do pagamento, recebe um **convite pessoal**. O link pede aprovação, e o bot só aprova quem pagou, então repassar o link não adianta.
- Lembretes configuráveis antes do vencimento (padrão: 7, 3 e 1 dias antes e no dia).
- Remoção automática após o vencimento mais a carência.
- `/status` mostra plano e vencimento. `/cancelar` cancela a recorrência.

**Usuários (SaaS)**
- O primeiro login (`ADMIN_USER` e `ADMIN_PASSWORD` do `.env`) vira o **superadmin**. Esses valores só são usados para criar a conta na primeira execução. Depois disso, a senha é trocada em **Minha conta**.
- O superadmin acessa **Usuários**, onde cria contas (com senha própria ou gerada), desativa e reativa contas (o login e o bot da conta param) e gera nova senha. Ele não vê os dados das áreas.
- Em **Minha conta**, cada usuário personaliza o nome e a cor da área e troca a própria senha. Trocar a senha encerra as outras sessões.
- **Assinatura da plataforma:** cada usuário (exceto o superadmin) paga uma assinatura para manter o **bot** funcionando.
  - **Usuário novo:** ganha dias de teste grátis (7 por padrão).
  - **Assinatura vencida:** **só o bot para**. Ele não responde, não envia lembretes, não remove membros e não processa os webhooks da área. O painel continua acessível, com um aviso e a página **Assinatura** para renovar.
  - **Como renovar:** a página **Assinatura** gera a cobrança por Pix (QR Code na tela) ou cartão, no **Mercado Pago da plataforma**, configurado pelo superadmin em **Plataforma**. Ao confirmar o pagamento, o bot volta na hora. Renovar antes do vencimento soma os dias.
  - **Configuração do superadmin:** em **Plataforma**, define os planos (Mensal, Trimestral e Semestral), os preços e os dias de teste. Em **Usuários**, pode dar "+ dias" manualmente.
  - **Renovação automática no cartão:** o usuário pode assinar com cobrança automática (assinatura recorrente do Mercado Pago). Cada cobrança aprovada soma os dias do plano. Se uma cobrança atrasar, o bot ainda funciona por 3 dias após o vencimento. O usuário pode cancelar a qualquer momento.
  - **Lembretes:** por padrão 7, 3 e 1 dia antes e no dia do vencimento, mais um aviso quando o bot é parado. Vão por **e-mail** (SMTP configurado em Plataforma) e pelo **bot do próprio usuário**, para os ids de admin dele. Quem tem renovação automática não recebe os lembretes.
- **Isolamento:** todas as tabelas têm `tenant_id`, e todas as consultas filtram pelo usuário logado. Acessar o id de outro usuário retorna 404. Cada bot só atende a área do seu dono, e cada usuário tem a própria URL de webhook. Um mesmo bot não pode ser cadastrado em duas contas.

**Painel de cada usuário**
- **Estatísticas:**
  - assinantes ativos, MRR, receita do mês e do mês anterior, ticket médio;
  - novos, retornos, saídas, churn e conversão;
  - gráficos diários, mensais, por forma de pagamento e por plano;
  - próximos vencimentos.
- **Membros:** busca, filtro, "+ dias", reenvio de convite, remoção, cancelamento de recorrência e liberação manual (cortesia).
- **Site público e cadastro:** página inicial, preços e cadastro pelo site com teste grátis (pode ser desligado em Plataforma). Quem se cadastra entra com o e-mail e cai no assistente de **Primeiros passos** (bot, grupo e forma de receber).
- **Seu grupo:** quem entra e sai do grupo, retenção, gráfico de membros e atividade recente em linguagem simples.
- **Alertas:** venda, entrada, saída, muitas saídas no dia e resumo semanal, pelo Telegram e/ou e-mail.
- **Relatórios:** resumo por período com download em PDF e Excel.
- **Visual:** menu lateral, modo claro e escuro, cor de destaque por área, primeiros passos na página de estatísticas e telas pensadas para o celular.
- **Pagamentos:** histórico, conferência de comprovante Pix (imagem/PDF) com aprovar e recusar, e verificação no Mercado Pago.
- **Planos:** nome, valor, duração, descrição e ativar/desativar.
- **Mensagens:** todos os textos automáticos editáveis, com marcadores (`{nome}`, `{vencimento}`...), envio de teste e restaurar padrão.
- **Palavras de entrada** (em Mensagens): palavras e frases que abrem o menu, e a opção de responder ou não as mensagens que o bot não entende.
- **Configurações:**
  - token do bot, com reinício automático;
  - grupo, com detecção automática e checagem de permissões;
  - admins;
  - Mercado Pago, chave Pix e formas de pagamento ativas;
  - lembretes e carência.

**Admin no Telegram:**
- recebe aviso de cada venda;
- recebe os comprovantes com botões **Aprovar** e **Recusar**;
- `/stats` mostra um resumo;
- `/id` mostra o id do chat.

## Segurança

- **Segredos:** tokens do bot, do Mercado Pago e do PayPal e a senha do SMTP ficam **criptografados** no banco (Fernet). A chave fica só no `.env` (`ENCRYPTION_KEY`): **guarde uma cópia**, sem ela os segredos salvos precisam ser cadastrados de novo. O painel nunca mostra um segredo salvo.
- **Sessão:** cookie assinado com `SECRET_KEY` (aleatória, 32+ caracteres; o app não sobe com o valor de exemplo), `HttpOnly`, `SameSite=Lax`, 12 h. Com `PUBLIC_URL` em https vira `__Host-sessao` e `Secure`, e o app redireciona http para https e envia HSTS.
- **Isolamento:** toda rota da área exige login e filtra pela conta. No PostgreSQL, a **segurança em nível de linha** também esconde as outras contas: o app roda as consultas com o papel `watchgram_app` (sem superusuário) e liga a conta logada a cada transação.
- **Senhas:** scrypt com sal. Login limitado por IP (5 tentativas) e por conta (10), troca de senha limitada, cadastro limitado por IP, com campo-armadilha e tempo mínimo de preenchimento.
- **Requisições:** formulários só são aceitos do próprio site (Origin/Referer), até 1 MB; campos com limite de tamanho; comprovantes até 10 MB e só imagem/PDF exibidos.
- **Cabeçalhos:** CSP com nonce (sem JavaScript embutido no HTML), `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`, `no-store`; Chart.js com verificação de integridade (SRI).
- **API:** sem documentação pública (`/docs`, `/openapi.json`); `/health` responde só `{"ok": true}`.
- **Dependências:** verifique com `pip install pip-audit && pip-audit -r requirements.txt`.

## Como funciona

```
Telegram  ◀──polling──▶  app (FastAPI + aiogram + agendador)  ──▶  Mercado Pago API
                             │                     ▲
                        PostgreSQL           webhook (opcional, precisa de HTTPS)
```

- Um único processo roda o painel web, o bot (polling, sem precisar de domínio) e o agendador.
- **Sem domínio:** o agendador consulta os pagamentos pendentes no Mercado Pago a cada 20 s.
- **Com domínio HTTPS:** o Mercado Pago avisa na hora (webhook) e a consulta periódica continua como reserva.

## Passo a passo

### 1. Criar o bot
1. No Telegram, fale com o **@BotFather** → `/newbot` → copie o **token**.
2. Adicione o bot ao seu grupo ou canal VIP como **administrador**, com as permissões **Convidar usuários** e **Banir usuários**.

### 2. Credenciais do Mercado Pago
1. Acesse <https://www.mercadopago.com.br/developers/panel/app> → crie uma aplicação.
2. Em **Credenciais de produção**, copie o **Access Token** (`APP_USR-...`). Para testar, use as credenciais de teste (`TEST-...`).

### 3. Rodar localmente
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # defina ADMIN_PASSWORD, SECRET_KEY e DATABASE_URL
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```
Abra <http://localhost:8000>, entre com o usuário e a senha do `.env` e:
1. Em **Configurações**:
   - cole o token do bot;
   - salve;
   - clique em **Usar este** no grupo detectado;
   - informe seu id de admin (envie `/id` ao bot);
   - cole o Access Token do Mercado Pago.
2. Em **Planos**, ajuste os valores.
3. Em **Mensagens**, personalize os textos.
4. Teste: envie `/start` ao bot.

Sem `DATABASE_URL`, o app usa SQLite em `data/bot.db`. Isso basta para testar. Para usar PostgreSQL fora do Docker, informe `DATABASE_URL=postgresql+asyncpg://usuario:senha@host:5432/banco`. O formato `postgres://...` também é aceito.

### 4. Produção (VPS com Docker)
```bash
cp .env.example .env   # edite: ADMIN_PASSWORD, SECRET_KEY e POSTGRES_PASSWORD
docker compose up -d --build
```
O compose sobe o **PostgreSQL 17** junto com o app. O banco fica no volume `pgdata` e não é exposto para fora. As tabelas e o superadmin são criados na primeira execução.
O painel fica em `127.0.0.1:8000`. Para acessar de fora e receber o **webhook** do Mercado Pago na hora:
1. Aponte um domínio (ex.: `bot.seudominio.com.br`) para o IP da VPS.
2. Edite o `Caddyfile` com esse domínio e rode `docker compose --profile https up -d`. O HTTPS é emitido automaticamente.
3. No `.env`, defina `PUBLIC_URL=https://bot.seudominio.com.br` e `COOKIE_SECURE=1` (o cookie de login só trafega por HTTPS). Depois rode `docker compose up -d` de novo.
4. Cada usuário vê **a própria URL de webhook** em **Configurações** (`https://bot.seudominio.com.br/webhooks/mercadopago/<chave-do-usuário>`) e a cadastra no painel do Mercado Pago dele (**Suas integrações → Webhooks**):
   - eventos: **Pagamentos** e **Planos e assinaturas**;
   - a **assinatura secreta** deve ser copiada para o campo correspondente no painel.

Sem domínio, você ainda pode usar o painel por túnel SSH: `ssh -L 8000:localhost:8000 usuario@vps` e depois abrir <http://localhost:8000>.

**Backup do banco:**
```bash
docker compose exec -T db pg_dump -U bot -Fc bot_telegram > backup-$(date +%F).dump          # gerar
docker compose exec -T db pg_restore -U bot -d bot_telegram --clean < backup-AAAA-MM-DD.dump  # restaurar
```

## Hospedagem: requisitos e custos

| Item | Recomendação | Custo |
|---|---|---|
| VPS | 1 vCPU e 2 GB de RAM (Hetzner, Contabo, Hostinger, Magalu Cloud) ou Oracle Cloud Free | R$ 0 a R$ 40 por mês |
| Domínio | Opcional. `.com.br` no Registro.br | cerca de R$ 40 por ano |
| HTTPS | Caddy + Let's Encrypt (incluso no compose) | grátis |
| Mercado Pago | Taxas por transação (confira no seu painel) | variável |

## Estrutura

```
app/
  main.py             FastAPI + inicialização dos bots e do agendador
  scheduler.py        conciliação de pagamentos, lembretes, expiração (por usuário)
  models.py           tabelas (SQLAlchemy); users + dados com tenant_id
  bot/runner.py       um bot por usuário, cada update marcado com o tenant_id do dono
  bot/                handlers do Telegram (assinante, admin, porteiro do grupo)
  services/           Mercado Pago, pagamentos, regras da assinatura, estatísticas, senhas
  web/auth.py         login, sessão e dependências de isolamento
  web/routes.py       área do usuário · web/admin.py: gestão de usuários (superadmin)
tests/                testes (pytest); test_isolation.py cobre o isolamento entre usuários
```

## Testes
```bash
.venv/bin/pip install pytest pytest-asyncio
.venv/bin/python -m pytest
# no PostgreSQL (use um banco descartável: as tabelas são apagadas)
TEST_DATABASE_URL=postgresql+asyncpg://usuario:senha@localhost:5432/teste .venv/bin/python -m pytest
```

## Limitações e cuidados
- **Membros que já estão no grupo:** o Telegram não deixa o bot listar membros antigos. O bot só controla quem entra depois dele. O ideal é **começar com um grupo novo** ou revogar os links de convite antigos e liberar os atuais em **Membros → Liberar acesso manualmente**.
- **Recorrência no cartão:** usa as *Assinaturas* do Mercado Pago (preapproval). Valide o fluxo com credenciais de teste antes de vender. Alterar o valor do plano recorrente vale só para novas assinaturas.
- **Pix manual:** depende de você conferir o extrato antes de aprovar, porque comprovantes podem ser falsos.
- **Limite de cobranças:** cada assinante gera no máximo 1 cobrança a cada 30 s e 5 por hora no Mercado Pago. Um Pix ainda válido do mesmo plano é reenviado em vez de gerar outro.
- **Política do Telegram:** pagamentos de produtos digitais *dentro* do Telegram devem usar Telegram Stars. Por isso o bot sempre usa QR Code ou link externo do Mercado Pago. Guarde o e-mail dos assinantes (o bot coleta) para o caso de precisar recriar o bot.
- **Segredos:** os tokens ficam no banco de dados. Proteja o servidor e os backups.
- **Imposto:** recebimentos como pessoa física devem ser declarados (carnê-leão e IR). Consulte um contador.
