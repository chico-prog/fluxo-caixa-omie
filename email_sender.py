"""
email_sender.py
-----------------
Envio do relatorio de fluxo de caixa por email, via SMTP (smtplib da
biblioteca padrao do Python - sem depender de credenciais interativas
tipo OAuth, que nao rolam num servidor sem tela). Pensado pra Gmail/
Google Workspace (smtp.gmail.com:587 + Senha de App), mas funciona com
qualquer provedor que aceite SMTP AUTH + STARTTLS - so trocar as
variaveis de ambiente.

Variaveis de ambiente esperadas (ver .env.example):
  SMTP_HOST      - padrao smtp.gmail.com
  SMTP_PORT      - padrao 587
  SMTP_USER      - endereco que autentica e envia (ex: relatorios@easyice.com.br)
  SMTP_PASSWORD  - senha de app (NAO a senha normal da conta)
  SMTP_FROM      - opcional, padrao = SMTP_USER
  EMAIL_TO       - destinatarios separados por virgula
"""

import mimetypes
import os
import smtplib
import socket
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


class EmailError(Exception):
    pass


_getaddrinfo_original = socket.getaddrinfo


def _getaddrinfo_so_ipv4(host, port, family=0, type=0, proto=0, flags=0):
    """Substitui socket.getaddrinfo global, temporariamente, filtrando pra
    so IPv4 (AF_INET). O Render (e varios PaaS) tem IPv6 "ligado" no SO
    mas sem rota de saida de verdade, e o Gmail tem registro AAAA (IPv6)
    - sem isso, smtplib tenta conectar via IPv6 primeiro e cai com
    "OSError: Network is unreachable" (achado real, 2026-09-25, direto
    do log de producao do Render). Continua usando o HOSTNAME (nao IP)
    na conexao - so a resolucao de endereco fica restrita a IPv4 - pra
    nao quebrar a validacao de certificado TLS do STARTTLS (que confere
    o hostname, nao o IP)."""
    return _getaddrinfo_original(host, port, socket.AF_INET, type, proto, flags)


def _anexar_arquivo(msg, caminho, nome_arquivo=None):
    tipo, _ = mimetypes.guess_type(caminho)
    subtipo = tipo.split("/", 1)[1] if tipo else "octet-stream"
    with open(caminho, "rb") as f:
        anexo = MIMEApplication(f.read(), _subtype=subtipo)
    anexo.add_header("Content-Disposition", "attachment", filename=nome_arquivo or os.path.basename(caminho))
    msg.attach(anexo)


def enviar_relatorio(caminho_html, resumo_texto, assunto=None, caminho_pdf=None):
    """Manda o relatorio por email, com `resumo_texto` no corpo (texto
    simples) e o(s) arquivo(s) em anexo - anexado, nao inline, porque o
    CSS/fontes do relatorio nao sobrevivem bem ao "sanitizador" de HTML
    da maioria dos webmails (Gmail, Outlook cortam <style>/<script>);
    anexado, o destinatario abre no navegador e ve o relatorio de
    verdade, com os botoes/tabelas funcionando. `caminho_pdf` e
    opcional - se informado, manda tambem (gerado por
    pdf_generator.gerar_pdf a partir do mesmo HTML)."""
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    usuario = os.getenv("SMTP_USER", "")
    senha = os.getenv("SMTP_PASSWORD", "")
    remetente = os.getenv("SMTP_FROM") or usuario
    destinatarios = [d.strip() for d in os.getenv("EMAIL_TO", "").split(",") if d.strip()]

    if not usuario or not senha:
        raise EmailError("SMTP_USER/SMTP_PASSWORD nao configurados")
    if not destinatarios:
        raise EmailError("EMAIL_TO nao configurado (nenhum destinatario)")

    msg = MIMEMultipart()
    msg["Subject"] = assunto or "Fluxo de caixa - Grupo Frutamix"
    msg["From"] = remetente
    msg["To"] = ", ".join(destinatarios)
    msg.attach(MIMEText(resumo_texto, "plain", "utf-8"))

    _anexar_arquivo(msg, caminho_html, "fluxo_caixa.html")
    if caminho_pdf:
        _anexar_arquivo(msg, caminho_pdf, "fluxo_caixa.pdf")

    socket.getaddrinfo = _getaddrinfo_so_ipv4
    try:
        with smtplib.SMTP(host, port, timeout=60) as smtp:
            smtp.starttls()
            smtp.login(usuario, senha)
            smtp.sendmail(remetente, destinatarios, msg.as_string())
    finally:
        socket.getaddrinfo = _getaddrinfo_original
