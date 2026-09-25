"""
main.py
--------
Servico web (FastAPI) que gera o relatorio diario de fluxo de caixa
(mesma logica de config.py/omie_client.py/financeiro.py/
relatorio_diario.py - copias do projeto local omie_relatorios) e manda
por email.

Pensado pra rodar no Render (plano gratis): um workflow do GitHub
Actions (.github/workflows/fluxo-caixa.yml) "acorda" esse servico todo
dia util as 7h (America/Sao_Paulo) batendo no webhook abaixo - mesmo
padrao do repo pedidos-omie pro pedido de sabado da Verdemar. O plano
gratis do Render dorme sozinho depois de uns 15min parado e nao tem
agendador interno, por isso precisa de algo externo "batendo na porta"
no horario certo.

O webhook responde IMEDIATAMENTE (so confirma que recebeu) e faz o
trabalho pesado (buscar no Omie - leva minutos - montar o relatorio,
mandar o email) em segundo plano, depois da resposta ja ter sido
enviada (FastAPI BackgroundTasks). Descoberto na pratica, 2026-09-21:
manter a conexao HTTP aberta esperando o processamento inteiro terminar
estourava o proxy do Render, que devolvia "500 Internal Server Error"
generico (nao vinha do nosso codigo Python - o corpo nao era o nosso
JSON de erro) antes do trabalho terminar de verdade.

IMPORTANTE (plano gratis, sem disco persistente): o quadro "Divergencias
de ontem" do relatorio depende de um log (logs/previsto_diario.csv) que
precisa sobreviver de um dia pro outro - no Render free ele se perde
toda vez que o servico dorme e acorda de novo, entao esse quadro sempre
sai vazio quando rodado por aqui (decisao do Chico, 2026-09-21: aceitar
isso por enquanto em troca de nao pagar por um plano com disco). O
envio diario do relatorio por email nao depende disso, funciona normal.

PDF em anexo (pdf_generator.py): gerado com Chromium headless
(Playwright) - risco conhecido de estourar os 512MB de RAM do plano
gratis (decisao do Chico, 2026-09-25: tentar mesmo assim). Se a geracao
do PDF falhar, o email ainda sai, so com o HTML (ver _processar_e_enviar
abaixo - PdfError nao e fatal).
"""

import os
import threading
import traceback
from datetime import datetime

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException

import relatorio_diario
from email_sender import EmailError, enviar_relatorio
from pdf_generator import PdfError, gerar_pdf

app = FastAPI()

WEBHOOK_SECRET = os.getenv("FLUXO_CAIXA_WEBHOOK_SECRET", "")
SAIDA_HTML = os.path.join("saida", "fluxo_caixa.html")
SAIDA_PDF = os.path.join("saida", "fluxo_caixa.pdf")

# Status da ultima execucao em background - so pra dar visibilidade via
# GET /status (o webhook em si so confirma "recebido", nao espera o
# processamento terminar pra responder). Fica em memoria: some se o
# servico reiniciar/dormir, mas serve pra conferir manualmente se o
# ultimo disparo deu certo sem precisar abrir os Logs do Render.
ultimo_status = {"quando": None, "status": "nunca rodou", "detalhe": ""}

# Trava simples pra impedir dois disparos rodando AO MESMO TEMPO -
# descoberto na pratica, 2026-09-21: dois disparos simultaneos (ex:
# reteste manual em cima de um disparo que ainda nao tinha terminado)
# fazem chamadas duplicadas/concorrentes pra API do Omie, que tem
# protecao contra "consumo redundante" e passa a bloquear/atrasar tudo
# em cadeia. Sem lock global (nao roda em varios processos aqui, so
# threads do mesmo worker), suficiente pro uso real (1 disparo por dia).
_processando = threading.Lock()


@app.get("/")
def raiz():
    """So pra checagem manual de que o servico esta de pe (ex: abrir a
    URL no navegador) - nao faz nada alem de responder."""
    return {"status": "ok", "servico": "fluxo-caixa-omie"}


@app.get("/status")
def status():
    """Resultado da ultima vez que o relatorio foi gerado/enviado."""
    return ultimo_status


def _processar_e_enviar():
    """Roda DEPOIS da resposta HTTP do webhook ja ter sido enviada (ver
    BackgroundTasks abaixo) - erros aqui nao viram HTTP 500 pra quem
    chamou (o GitHub Actions ja recebeu 200 e seguiu a vida), soamente
    fica registrado em ultimo_status (GET /status) e no log do Render."""
    global ultimo_status
    if not _processando.acquire(blocking=False):
        # ja tem um disparo rodando - nao inicia outro em cima (ver
        # comentario do _processando la em cima).
        print("[AVISO] disparo ignorado - ja existe um processamento em andamento")
        return
    try:
        agora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        try:
            # rastrear_divergencias=False: sem disco persistente aqui, o
            # log de previsto nunca sobrevive de um dia pro outro mesmo -
            # sem isso so economiza chamadas de API no Omie (ver
            # docstring de relatorio_diario.gerar).
            resultado = relatorio_diario.gerar(saida=SAIDA_HTML, rastrear_divergencias=False)
        except Exception as e:
            traceback.print_exc()
            ultimo_status = {"quando": agora, "status": "erro", "detalhe": f"falha ao gerar relatorio: {e}"}
            return

        caminho_pdf = None
        try:
            gerar_pdf(resultado["saida"], SAIDA_PDF)
            caminho_pdf = SAIDA_PDF
        except PdfError as e:
            # nao fatal - manda so o HTML mesmo, em vez de perder o
            # email inteiro por causa do PDF (ver pdf_generator.py -
            # risco conhecido de estourar memoria no plano gratis).
            traceback.print_exc()
            print(f"[AVISO] nao consegui gerar o PDF, mandando so o HTML ({e})")

        fmt = relatorio_diario._fmt
        resumo = (
            f"Saldo D-1 (fechamento de ontem): R$ {fmt(resultado['saldo_total'])}\n"
            f"A pagar hoje: R$ {fmt(resultado['pagar_total'])}\n"
            f"A receber hoje: R$ {fmt(resultado['receber_total'])}\n"
            f"Contas negativas hoje: {resultado['n_negativas']}\n"
            "\nRelatorio completo em anexo - abra no navegador (HTML) pra ver todas as tabelas com os botoes funcionando, ou o PDF pra imprimir/arquivar.\n"
        )

        try:
            enviar_relatorio(resultado["saida"], resumo, caminho_pdf=caminho_pdf)
        except EmailError as e:
            traceback.print_exc()
            ultimo_status = {"quando": agora, "status": "erro", "detalhe": f"relatorio gerado, mas falha ao enviar email: {e}"}
            return

        ultimo_status = {
            "quando": agora, "status": "ok",
            "detalhe": {k: v for k, v in resultado.items() if k != "saida"},
        }
    finally:
        _processando.release()


@app.post("/webhook/fluxo-caixa")
def disparar_fluxo_caixa(background_tasks: BackgroundTasks, x_webhook_secret: str = Header(default="")):
    if not WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="FLUXO_CAIXA_WEBHOOK_SECRET nao configurado no servidor")
    if x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="webhook secret invalido")

    if _processando.locked():
        return {"status": "ja_processando", "mensagem": "ja existe um disparo em andamento - aguarde ele terminar (confira GET /status) antes de disparar de novo"}

    background_tasks.add_task(_processar_e_enviar)
    return {"status": "recebido", "mensagem": "gerando o relatorio e enviando por email em segundo plano - confira GET /status em alguns minutos"}
