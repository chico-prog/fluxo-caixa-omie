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

IMPORTANTE (plano gratis, sem disco persistente): o quadro "Divergencias
de ontem" do relatorio depende de um log (logs/previsto_diario.csv) que
precisa sobreviver de um dia pro outro - no Render free ele se perde
toda vez que o servico dorme e acorda de novo, entao esse quadro sempre
sai vazio quando rodado por aqui (decisao do Chico, 2026-09-21: aceitar
isso por enquanto em troca de nao pagar por um plano com disco). O
envio diario do relatorio por email nao depende disso, funciona normal.
"""

import os

from fastapi import FastAPI, Header, HTTPException

import relatorio_diario
from email_sender import EmailError, enviar_relatorio

app = FastAPI()

WEBHOOK_SECRET = os.getenv("FLUXO_CAIXA_WEBHOOK_SECRET", "")
SAIDA_HTML = os.path.join("saida", "fluxo_caixa.html")


@app.get("/")
def raiz():
    """So pra checagem manual de que o servico esta de pe (ex: abrir a
    URL no navegador) - nao faz nada alem de responder."""
    return {"status": "ok", "servico": "fluxo-caixa-omie"}


@app.post("/webhook/fluxo-caixa")
def disparar_fluxo_caixa(x_webhook_secret: str = Header(default="")):
    if not WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="FLUXO_CAIXA_WEBHOOK_SECRET nao configurado no servidor")
    if x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="webhook secret invalido")

    try:
        resultado = relatorio_diario.gerar(saida=SAIDA_HTML)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"falha ao gerar relatorio: {e}")

    fmt = relatorio_diario._fmt
    resumo = (
        f"Saldo D-1 (fechamento de ontem): R$ {fmt(resultado['saldo_total'])}\n"
        f"A pagar hoje: R$ {fmt(resultado['pagar_total'])}\n"
        f"A receber hoje: R$ {fmt(resultado['receber_total'])}\n"
        f"Contas negativas hoje: {resultado['n_negativas']}\n"
        f"Divergencias de ontem (previsto x realizado): {resultado['n_divergencias']}\n"
        "\nRelatorio completo em anexo - abra no navegador pra ver todas as tabelas.\n"
    )

    try:
        enviar_relatorio(resultado["saida"], resumo)
    except EmailError as e:
        raise HTTPException(status_code=500, detail=f"relatorio gerado, mas falha ao enviar email: {e}")

    return {"status": "ok", **{k: v for k, v in resultado.items() if k != "saida"}}
