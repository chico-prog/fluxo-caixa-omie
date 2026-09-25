"""
pdf_generator.py
------------------
Gera um PDF do relatorio de fluxo de caixa a partir do proprio arquivo
HTML, usando um Chromium headless (Playwright) - a MESMA engine de
renderizacao de um navegador de verdade, entao usa o mesmo CSS de
impressao (@media print, @page A4 paisagem) ja validado manualmente
clicando no botao "Imprimir / Salvar PDF" (ver relatorio_diario.py).

Precisa do Chromium instalado no build do servico - o Build Command no
Render tem que ser:
  pip install -r requirements.txt && playwright install --with-deps chromium
(so "pip install" nao basta - o playwright python so fala com o
Chromium, quem baixa o navegador de verdade e o "playwright install").

Risco conhecido (plano gratis do Render, 512MB de RAM): Chromium
headless consome uns 150-250MB so pra abrir - decisao do Chico,
2026-09-25: tentar mesmo assim, e se o servico ficar instavel
(memoria estourando), reavaliar plano pago ou desistir do PDF.
"""

from pathlib import Path

from playwright.sync_api import sync_playwright


class PdfError(Exception):
    pass


def gerar_pdf(caminho_html, caminho_pdf):
    """Abre `caminho_html` num Chromium headless e salva o PDF em
    `caminho_pdf`, respeitando o CSS de impressao do proprio relatorio
    (paisagem, A4, fundo colorido das celulas negativas etc)."""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(Path(caminho_html).resolve().as_uri())
                page.emulate_media(media="print")
                page.pdf(path=caminho_pdf, print_background=True, prefer_css_page_size=True)
            finally:
                browser.close()
    except Exception as e:
        raise PdfError(f"falha ao gerar PDF: {e}")
