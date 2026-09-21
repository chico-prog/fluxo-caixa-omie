"""
omie_client.py
----------------
Cliente Omie generico: autenticacao por conta (App Key + App Secret), sem
nenhum endpoint/dominio fixo - qualquer modulo (financeiro, vendas, estoque,
compras...) usa a mesma instancia pra chamar qualquer area da API a que essa
conta tenha acesso.

Logica de retry/rate-limit copiada do omie_client.py de CONCILIACAO
FINANCEIRA (mesmo grupo, ja validada em producao contra bloqueios reais de
"numero maximo de requisicoes" e "consumo redundante" da Omie).
"""

import re
import time

import requests


class OmieError(Exception):
    pass


class OmieClient:
    def __init__(self, app_key, app_secret, nome_conta=""):
        self.app_key = app_key
        self.app_secret = app_secret
        self.nome_conta = nome_conta or app_key

    def chamar(self, endpoint, call, param, tentativas=10, pausa=0.3):
        """Faz uma chamada padrao a API do Omie (POST JSON), com retry em
        timeout/erro de rede e em limite de requisicoes/consumo redundante."""
        payload = {
            "call": call,
            "app_key": self.app_key,
            "app_secret": self.app_secret,
            "param": [param],
        }
        for tentativa in range(1, tentativas + 1):
            try:
                resp = requests.post(endpoint, json=payload, timeout=60)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if tentativa < tentativas:
                    time.sleep(2 * tentativa)
                    continue
                raise OmieError(
                    f"[{self.nome_conta}] {call}: erro de rede apos {tentativas} tentativas ({e})"
                )
            try:
                data = resp.json()
            except ValueError:
                resp.raise_for_status()
                raise OmieError(
                    f"[{self.nome_conta}] {call}: resposta nao-JSON inesperada: {resp.text[:300]}"
                )
            if "faultstring" in data:
                msg = data.get("faultstring", "")
                msg_lower = msg.lower()
                limite_excedido = "429" in str(data.get("faultcode", "")) or "numero maximo de requisi" in msg_lower
                consumo_redundante = "redundante" in msg_lower or "ja existe uma requisi" in msg_lower or "já existe uma requisi" in msg_lower
                bloqueio_temporario = "consumo indevido" in msg_lower or str(data.get("faultcode", "")) == "MISUSE_API_PROCESS"
                if (limite_excedido or consumo_redundante or bloqueio_temporario) and tentativa < tentativas:
                    match = re.search(r"(\d+)\s*segundos?", msg)
                    espera = int(match.group(1)) + 2 if match else min(30, 3 * tentativa)
                    time.sleep(espera)
                    continue
                raise OmieError(f"[{self.nome_conta}] {call}: {msg} (codigo {data.get('faultcode')})")
            resp.raise_for_status()
            if pausa:
                time.sleep(pausa)
            return data
