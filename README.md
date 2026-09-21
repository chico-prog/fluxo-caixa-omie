# fluxo-caixa-omie

Envia o relatório de fluxo de caixa do grupo (saldo D-1, previsão de
hoje, borderô de aprovação, divergências) por email, todo dia útil às
7:00, automaticamente — sem precisar do seu PC ligado.

Mesmo padrão do repositório `pedidos-omie`: um serviço web hospedado no
Render faz o trabalho de verdade, e o GitHub Actions só "acorda" ele no
horário certo (o plano grátis do Render dorme sozinho depois de ~15min
parado).

**Importante (plano grátis do Render, sem disco persistente):** o
quadro "Divergências de ontem" do relatório sempre sai vazio quando
rodado por aqui — o log que compara previsto × realizado precisa
sobreviver de um dia pro outro, e o Render grátis apaga os arquivos
locais toda vez que o serviço dorme e acorda. O envio diário do
relatório por email funciona normalmente, só esse quadro específico
fica sem histórico. (Se um dia quiser resolver isso, a opção é um plano
pago do Render com disco persistente.)

---

## Passo a passo completo (do zero)

Siga na ordem. Cada passo tem o "onde clicar" bem explicado.

### Passo 1. Criar o repositório vazio no GitHub

1. Abra https://github.com/new (logado como `chico-prog`, a mesma conta
   do `pedidos-omie`).
2. Em **Repository name**, digite `fluxo-caixa-omie`.
3. Marque **Private** (é dado financeiro, não pode ser público).
4. NÃO marque nenhuma das caixinhas "Add a README file" / ".gitignore" /
   "license" — já vamos mandar os arquivos prontos.
5. Clique em **Create repository**.
6. Na página que abrir, copie a URL que aparece em destaque, algo como
   `https://github.com/chico-prog/fluxo-caixa-omie.git` — vai precisar
   dela no próximo passo.

### Passo 2. Enviar o código pro repositório

Os arquivos já estão prontos na sua máquina, em
`C:\Users\chico\fluxo_caixa_omie`. Falta só subir pro GitHub. Me avise
com a URL do Passo 1.6 que eu rodo os comandos de `git` pra você (init,
commit, push) — só preciso que, na primeira vez que o Git pedir login,
você autorize pela janela que abrir no navegador.

### Passo 3. Gerar a senha de app do Gmail (pra enviar o email)

Isso é necessário porque o Gmail não deixa um programa comum ("SMTP")
logar com sua senha normal — precisa de uma "Senha de app" separada,
específica pra isso.

1. Abra https://myaccount.google.com/apppasswords (logado com a conta
   que vai *enviar* os relatórios — pode ser `chico@easyice.com.br` ou
   um email dedicado tipo `relatorios@easyice.com.br`, se existir).
   - Se pedir pra ativar a "Verificação em duas etapas" primeiro, ative
     em https://myaccount.google.com/security — é obrigatório pra gerar
     senha de app.
2. Em **Nome do app**, digite `fluxo-caixa-omie` e clique em **Criar**.
3. O Google vai mostrar uma senha de 16 letras (tipo `abcd efgh ijkl
   mnop`). **Copie ela agora** — essa tela não abre de novo depois.
4. Guarde essa senha junto com o email usado — vai precisar dos dois no
   Passo 4.

Se `easyice.com.br` usa outro provedor de email (não Gmail/Google
Workspace), me avisa que eu ajusto o `SMTP_HOST`/`SMTP_PORT` do
`.env.example` pro provedor certo.

### Passo 4. Criar o serviço no Render

1. Abra https://render.com e entre (crie conta grátis se ainda não tem
   — pode entrar direto com a conta do GitHub, é o mais rápido).
2. Clique em **New +** (canto superior direito) → **Web Service**.
3. Escolha **Build and deploy from a Git repository** → conecte sua
   conta do GitHub se pedir → selecione o repositório
   `chico-prog/fluxo-caixa-omie` (criado no Passo 1).
4. Preencha:
   - **Name**: `fluxo-caixa-omie` (isso vira parte da URL do serviço)
   - **Region**: Oregon (ou a mais próxima disponível — não afeta nada
     aqui, o Omie e o Gmail não ligam pra isso)
   - **Branch**: `main`
   - **Runtime**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Instance Type**: **Free**
5. Role até **Environment Variables** e clique em **Add Environment
   Variable** pra cada uma destas (copie os NOMES exatamente, os
   VALORES são os dados reais de cada conta/senha):

   | Nome (Key)                     | Valor (Value)                                    |
   |---------------------------------|---------------------------------------------------|
   | `OMIE_APP_KEY_MATRIZ`           | (do Omie, Configurações > API, conta Matriz)      |
   | `OMIE_APP_SECRET_MATRIZ`        | (idem)                                             |
   | `OMIE_APP_KEY_FILIAL_DF`        | (idem, conta Filial DF)                            |
   | `OMIE_APP_SECRET_FILIAL_DF`     | (idem)                                             |
   | `OMIE_APP_KEY_FILIAL_JSL`       | (idem, conta Filial JSL)                           |
   | `OMIE_APP_SECRET_FILIAL_JSL`    | (idem)                                             |
   | `OMIE_APP_KEY_PURA_FRUTA`       | (idem, conta Pura Fruta)                           |
   | `OMIE_APP_SECRET_PURA_FRUTA`    | (idem)                                             |
   | `OMIE_APP_KEY_FROZEN_LOG`       | (idem, conta Frozen Log)                           |
   | `OMIE_APP_SECRET_FROZEN_LOG`    | (idem)                                             |
   | `SMTP_HOST`                     | `smtp.gmail.com`                                   |
   | `SMTP_PORT`                     | `587`                                              |
   | `SMTP_USER`                     | o email do Passo 3 (ex: `chico@easyice.com.br`)   |
   | `SMTP_PASSWORD`                 | a senha de app de 16 letras do Passo 3             |
   | `EMAIL_TO`                      | `chico@easyice.com.br,financeiro@easyice.com.br`  |
   | `FLUXO_CAIXA_WEBHOOK_SECRET`    | invente uma senha longa e aleatória (ex: gere uma em uuidgenerator.net e cole aqui) |

   (Se um dia a EasyIce ganhar credenciais próprias no Omie, adiciona
   `OMIE_APP_KEY_EASYICE` / `OMIE_APP_SECRET_EASYICE` do mesmo jeito.)
6. Clique em **Create Web Service**. O Render vai buildar e subir o
   serviço — leva uns 2-3 minutos na primeira vez. Acompanhe em **Logs**
   até aparecer algo como `Application startup complete`.
7. Quando terminar, copie a URL do serviço (aparece no topo da página,
   algo como `https://fluxo-caixa-omie.onrender.com`) — precisa dela no
   próximo passo.

### Passo 5. Ligar o GitHub Actions no horário certo

1. No arquivo `.github/workflows/fluxo-caixa.yml` (já está no
   repositório), troque a linha:
   ```
   URL="https://SUBSTITUA-PELO-NOME-DO-SEU-SERVICO.onrender.com/webhook/fluxo-caixa"
   ```
   pela URL real do Passo 4.7, mantendo o `/webhook/fluxo-caixa` no
   final. Me manda a URL que eu edito e subo essa mudança pra você.
2. No GitHub, abra `https://github.com/chico-prog/fluxo-caixa-omie` →
   aba **Settings** → menu lateral **Secrets and variables** → **Actions**.
3. Clique em **New repository secret**.
   - **Name**: `FLUXO_CAIXA_WEBHOOK_SECRET`
   - **Secret**: a MESMA senha que você colocou no Render no Passo 4.5
     (tem que ser idêntica nos dois lugares).
4. Clique em **Add secret**.

### Passo 6. Testar

1. No GitHub, aba **Actions** → clique no workflow **Fluxo de caixa -
   envio diário** na lista à esquerda → botão **Run workflow** (canto
   direito) → **Run workflow** de novo pra confirmar.
2. Espere uns 10-30 segundos e atualize a página — deve aparecer uma
   execução com uma bolinha verde (sucesso) ou vermelha (erro, aí abra
   ela pra ver a mensagem).
3. Confira o email em `chico@easyice.com.br` — deve chegar em até um
   minuto.
4. Se dar erro, me manda o print da execução (aba Actions → clique na
   execução → clique no passo "Chamar webhook do serviço no Render")
   que eu leio e conserto.

A partir daí, roda sozinho todo dia útil às 7:00 — não precisa fazer
mais nada.

---

## Rodando localmente (opcional, pra testar antes de mexer em produção)

```bash
pip install -r requirements.txt
cp .env.example .env   # preencha com as credenciais reais
uvicorn main:app --reload
```

Depois, num terminal separado:

```bash
curl -X POST http://localhost:8000/webhook/fluxo-caixa \
  -H "X-Webhook-Secret: o-valor-que-voce-colocou-no-.env"
```
