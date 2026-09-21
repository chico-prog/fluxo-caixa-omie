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

import os
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


class EmailError(Exception):
    pass


def enviar_relatorio(caminho_html, resumo_texto, assunto=None):
    """Manda o relatorio (arquivo HTML em `caminho_html`) por email, com
    `resumo_texto` no corpo (texto simples) e o HTML completo em anexo -
    anexado, nao inline, porque o CSS/fontes do relatorio nao sobrevivem
    bem ao "sanitizador" de HTML da maioria dos webmails (Gmail, Outlook
    cortam <style>/<script>); anexado, o destinatario abre no navegador e
    ve o relatorio de verdade, com os botoes/tabelas funcionando."""
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

    with open(caminho_html, "rb") as f:
        anexo = MIMEApplication(f.read(), _subtype="html")
    anexo.add_header("Content-Disposition", "attachment", filename="fluxo_caixa.html")
    msg.attach(anexo)

    with smtplib.SMTP(host, port, timeout=60) as smtp:
        smtp.starttls()
        smtp.login(usuario, senha)
        smtp.sendmail(remetente, destinatarios, msg.as_string())
