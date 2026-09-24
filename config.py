"""
config.py
----------
Configuracao central do omie_relatorios: quais contas Omie existem, seus
sufixos de variavel de ambiente, e os endpoints da API usados pelos modulos.

Cada conta/CNPJ do grupo tem sua propria App Key + App Secret no Omie
(Configuracoes > API). Uma conta sem as duas variaveis preenchidas no .env
e pulada automaticamente pelos modulos - nao precisa comentar/remover nada
aqui quando faltar credencial.
"""

import os

from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://app.omie.com.br/api/v1"

ENDPOINTS = {
    "categoria": f"{BASE_URL}/geral/categorias/",
    "conta_corrente": f"{BASE_URL}/geral/contacorrente/",
    "mf": f"{BASE_URL}/financas/mf/",
    "conta_pagar": f"{BASE_URL}/financas/contapagar/",
    "conta_receber": f"{BASE_URL}/financas/contareceber/",
    "clientes": f"{BASE_URL}/geral/clientes/",
    "extrato": f"{BASE_URL}/financas/extrato/",
    "nfe_consultar": f"{BASE_URL}/produtos/nfconsultar/",
    "cupom_fiscal": f"{BASE_URL}/produtos/cupomfiscalconsultar/",
    "produtos": f"{BASE_URL}/geral/produtos/",
    "malha": f"{BASE_URL}/geral/malha/",
    "estoque_consulta": f"{BASE_URL}/estoque/consulta/",
    "op": f"{BASE_URL}/produtos/op/",
}

# Codigo do banco (padrao FEBRABAN, confirmado contra o campo codigo_banco
# real de ListarContasCorrentes) -> nome amigavel.
BANCOS = {
    "001": "Banco do Brasil",
    "077": "Inter",
    "237": "Bradesco",
    "341": "Itau",
    "756": "Sicoob",
}

# Uma conta por linha. "sufixo" define as variaveis de ambiente lidas:
# OMIE_APP_KEY_<sufixo> / OMIE_APP_SECRET_<sufixo>.
ENTIDADES = [
    {"nome": "Matriz (Frutamix)", "sufixo": "MATRIZ"},
    {"nome": "Filial DF", "sufixo": "FILIAL_DF"},
    {"nome": "Steria Soares", "sufixo": "STERIA_SOARES"},
    {"nome": "Pura Fruta", "sufixo": "PURA_FRUTA"},
    {"nome": "Frozen Log", "sufixo": "FROZEN_LOG"},
    {"nome": "EasyIce", "sufixo": "EASYICE"},
]


def credenciais(sufixo):
    """(app_key, app_secret) para o sufixo, ou None se alguma faltar no .env."""
    app_key = os.getenv(f"OMIE_APP_KEY_{sufixo}", "").strip()
    app_secret = os.getenv(f"OMIE_APP_SECRET_{sufixo}", "").strip()
    if not app_key or not app_secret:
        return None
    return app_key, app_secret


def entidades_configuradas():
    """So as entidades com App Key + App Secret preenchidos no .env."""
    ativas = []
    for ent in ENTIDADES:
        cred = credenciais(ent["sufixo"])
        if cred:
            ativas.append({**ent, "app_key": cred[0], "app_secret": cred[1]})
    return ativas
