"""
relatorio_diario.py
---------------------
Gera fluxo_caixa.html: saldo bancario Omie de D-1/ontem, ja fechado (por
conta corrente), previsao de contas a pagar/receber de D+0/hoje (por
conta corrente e por fornecedor/cliente), e uma projecao de caixa
ACUMULADA para os proximos HORIZONTE_DIAS dias comecando hoje - um
quadro consolidado somando as empresas do grupo e um quadro detalhado
com pagar/receber/saldo de cada empresa. No final, um bordero de
aprovacao (empresa/banco/fornecedor/NF/valor) so das contas a pagar
previstas pra hoje - usado pra aprovacao manual dos pagamentos do dia -
e um quadro de divergencias entre o que foi PREVISTO ontem e o que foi
de fato REALIZADO (baixado no Omie) ontem, pra acompanhar padroes de
atraso/pagamento fora do combinado (ver financeiro.
comparar_previsto_realizado e logs/previsto_diario.csv /
logs/divergencias.csv - a previsao de hoje so vira "comparavel" na
execucao de amanha, entao o primeiro dia desse quadro sai vazio).

Pensado pra rodar todo dia sem ninguem olhando (Task Scheduler): grava um
log de uma linha por execucao em logs/relatorio_diario.csv (sucesso ou
erro) - se um dia a extracao falhar, fica registrado, nao só silencioso.

Uso:
  python relatorio_diario.py
  python relatorio_diario.py --saida "G:/Drives compartilhados/EASYICE/FINANCEIRO/fluxo_caixa.html"
"""

import argparse
import csv
import html
import os
import sys
import traceback
from datetime import datetime, timedelta

import config
import financeiro
from omie_client import OmieError

HORIZONTE_DIAS = 15
SAIDA_PADRAO = os.path.join("saida", "fluxo_caixa.html")
LOG_PADRAO = os.path.join("logs", "relatorio_diario.csv")
PREVISTO_LOG = os.path.join("logs", "previsto_diario.csv")
DIVERGENCIAS_LOG = os.path.join("logs", "divergencias.csv")
_CAMPOS_PREVISTO = ["data", "tipo", "empresa", "codigo_lancamento_omie", "fornecedor", "numero_documento", "categoria", "valor"]


def _fmt(v, forcar_sinal=False):
    """1234.5 -> '1.234,50' (padrao BR). None -> travessao."""
    if v is None:
        return "—"
    neg = v < 0
    v = abs(v)
    inteiro, frac = f"{v:,.2f}".split(".")
    inteiro = inteiro.replace(",", ".")
    s = f"{inteiro},{frac}"
    return ("-" if neg else ("+" if forcar_sinal else "")) + s


def _esc(s):
    return html.escape(str(s) if s is not None else "", quote=True)


def _consolidar_top(itens, top_n=8):
    """Agrupa por (fornecedor, categoria) - pega repeticoes do mesmo
    fornecedor/categoria (ex: dezenas de baixas pequenas de maquininha)
    como uma linha so - soma valor e conta ocorrencias. Devolve (top_n
    maiores por valor, quantidade dos demais, soma dos demais)."""
    agrupado = {}
    for it in itens:
        chave = (it["fornecedor"], it["categoria"])
        g = agrupado.setdefault(chave, {"fornecedor": it["fornecedor"], "categoria": it["categoria"], "valor": 0.0, "qtd": 0})
        g["valor"] += it["valor"] or 0
        g["qtd"] += 1
    linhas = sorted(agrupado.values(), key=lambda x: -x["valor"])
    top = linhas[:top_n]
    resto = linhas[top_n:]
    return top, len(resto), sum(l["valor"] for l in resto)


def _linhas_conta(dados_conta):
    """<tr> de cada conta corrente de uma empresa (saldo/receber/pagar/
    projetado), destacando negativo. dados_conta: lista filtrada de
    resumo_fluxo_por_conta pra uma unica empresa."""
    html_linhas = []
    outras = None
    for c in dados_conta:
        if c["conta"].startswith("(outras"):
            outras = c
            continue
        classe = "conta negativa" if (c["saldo_projetado"] or 0) < 0 else "conta"
        flag = '<span class="flag">negativo</span>' if classe == "conta negativa" else ""
        html_linhas.append(
            f'<tr class="{classe}"><td>{_esc(c["banco"])}{flag}</td>'
            f'<td>{_fmt(c["saldo_atual"])}</td><td>{_fmt(c["a_receber_previsto"])}</td>'
            f'<td>{_fmt(c["a_pagar_previsto"])}</td><td>{_fmt(c["saldo_projetado"])}</td></tr>'
        )
    if outras:
        html_linhas.append(
            f'<tr class="outras"><td>Outras contas <span class="dash">— não operacionais / outro banco</span></td>'
            f'<td class="dash">—</td><td>{_fmt(outras["a_receber_previsto"])}</td>'
            f'<td>{_fmt(outras["a_pagar_previsto"])}</td><td class="dash">—</td></tr>'
        )
    return "\n".join(html_linhas)


def _bloco_empresa(nome, dados_conta, totais):
    return f"""
  <div class="empresa">
    <div class="empresa-head">
      <h2>{_esc(nome)}</h2>
      <div class="empresa-totais">
        <div class="t"><span class="tl">Saldo D-1</span><span class="tv">{_fmt(totais["saldo_atual_omie"])}</span></div>
        <div class="t"><span class="tl">Receber</span><span class="tv">{_fmt(totais["a_receber_previsto"])}</span></div>
        <div class="t"><span class="tl">Pagar</span><span class="tv">{_fmt(totais["a_pagar_previsto"])}</span></div>
        <div class="t"><span class="tl">Projetado</span><span class="tv{' neg' if totais['saldo_projetado'] < 0 else ''}">{_fmt(totais["saldo_projetado"])}</span></div>
      </div>
    </div>
    <div class="tablewrap">
    <table>
      <thead><tr><th>Conta</th><th>Saldo D-1</th><th>Receber</th><th>Pagar</th><th>Projetado</th></tr></thead>
      <tbody>
{_linhas_conta(dados_conta)}
      </tbody>
    </table>
    </div>
  </div>"""


def _linhas_lista(itens, top_n=8):
    if not itens:
        return '<div class="list-empty">Nada previsto para hoje.</div>'
    top, resto_qtd, resto_valor = _consolidar_top(itens, top_n)
    linhas = []
    for l in top:
        sufixo = f" · {l['qtd']} títulos" if l["qtd"] > 1 else ""
        linhas.append(
            f'<div class="list-row"><span class="quem">{_esc(l["fornecedor"])}'
            f'<span class="cat">{_esc(l["categoria"])}{sufixo}</span></span>'
            f'<span class="val">{_fmt(l["valor"])}</span></div>'
        )
    if resto_qtd:
        linhas.append(
            f'<div class="list-row mais"><span class="quem">+ {resto_qtd} outros</span>'
            f'<span class="val">{_fmt(resto_valor)}</span></div>'
        )
    return "\n".join(linhas)


def _bloco_lista_empresa(nome, subtotal, itens):
    return f"""
      <div class="list-empresa">
        <div class="list-empresa-head"><span class="nome">{_esc(nome)}</span><span class="subtotal">{_fmt(subtotal)}</span></div>
{_linhas_lista(itens)}
      </div>"""


def _linhas_bordero(itens):
    """Uma linha por titulo a pagar previsto pra hoje (empresa, banco,
    fornecedor, NF, valor) - ordenado por empresa e, dentro dela, por
    banco (e pelo maior valor), pra facilitar a conferencia/aprovacao
    por lote de transferencia. Fecha cada banco com um subtotal, cada
    empresa com um subtotal, e a tabela inteira com o total geral."""
    if not itens:
        return '<tr><td colspan="5" class="dash" style="text-align:center; font-style:italic;">Nada a pagar hoje.</td></tr>'
    ordenados = sorted(itens, key=lambda i: (i["empresa"], i["banco"], -i["valor"]))

    def _linha_subtotal_banco(banco, valor):
        return f'<tr class="subtotal-banco"><td></td><td colspan="3">Subtotal {_esc(banco)}</td><td>{_fmt(valor)}</td></tr>'

    def _linha_subtotal_empresa(empresa, valor):
        return f'<tr class="subtotal"><td colspan="4">Subtotal {_esc(empresa)}</td><td>{_fmt(valor)}</td></tr>'

    linhas = []
    empresa_atual = banco_atual = None
    subtotal_banco = subtotal_empresa = 0.0
    for i in ordenados:
        if i["empresa"] != empresa_atual:
            if empresa_atual is not None:
                linhas.append(_linha_subtotal_banco(banco_atual, subtotal_banco))
                linhas.append(_linha_subtotal_empresa(empresa_atual, subtotal_empresa))
            empresa_atual, banco_atual = i["empresa"], i["banco"]
            subtotal_banco = subtotal_empresa = 0.0
        elif i["banco"] != banco_atual:
            linhas.append(_linha_subtotal_banco(banco_atual, subtotal_banco))
            banco_atual = i["banco"]
            subtotal_banco = 0.0
        subtotal_banco += i["valor"]
        subtotal_empresa += i["valor"]
        linhas.append(
            f'<tr><td>{_esc(i["empresa"])}</td><td>{_esc(i["banco"])}</td>'
            f'<td>{_esc(i["fornecedor"])}</td><td>{_esc(i["nf"])}</td>'
            f'<td>{_fmt(i["valor"])}</td></tr>'
        )
    linhas.append(_linha_subtotal_banco(banco_atual, subtotal_banco))
    linhas.append(_linha_subtotal_empresa(empresa_atual, subtotal_empresa))
    total = sum(i["valor"] for i in itens)
    linhas.append(
        f'<tr class="total"><td colspan="4">Total geral</td><td>{_fmt(total)}</td></tr>'
    )
    return "\n".join(linhas)


def _linhas_horizonte(periodo, nomes_empresas):
    """Uma linha por dia, com Pagar/Receber/Saldo projetado de cada
    empresa (3 colunas por empresa) - tabela larga, rola horizontalmente
    via .tablewrap."""
    linhas = []
    n_dias = len(next(iter(periodo.values()))["dias"])
    for i in range(n_dias):
        dia_ref = periodo[nomes_empresas[0]]["dias"][i]
        cels = [f'<td>{dia_ref["data"]}</td>']
        for nome in nomes_empresas:
            d = periodo[nome]["dias"][i]
            classe = ' class="neg"' if d["saldo_projetado"] < 0 else ""
            cels.append(
                f'<td>{_fmt(d["a_pagar"])}</td><td>{_fmt(d["a_receber"])}</td>'
                f'<td{classe}>{_fmt(d["saldo_projetado"])}</td>'
            )
        linhas.append("<tr>" + "".join(cels) + "</tr>")
    return "\n".join(linhas)


def _linhas_consolidado_grupo(periodo, nomes_empresas):
    """Uma linha por dia com Pagar/Receber/Saldo projetado somados de
    TODAS as empresas do grupo - visao unica do caixa consolidado."""
    linhas = []
    n_dias = len(next(iter(periodo.values()))["dias"])
    for i in range(n_dias):
        dia_ref = periodo[nomes_empresas[0]]["dias"][i]
        pagar = sum(periodo[nome]["dias"][i]["a_pagar"] for nome in nomes_empresas)
        receber = sum(periodo[nome]["dias"][i]["a_receber"] for nome in nomes_empresas)
        saldo = sum(periodo[nome]["dias"][i]["saldo_projetado"] for nome in nomes_empresas)
        classe = ' class="neg"' if saldo < 0 else ""
        linhas.append(
            f'<tr><td>{dia_ref["data"]}</td><td>{_fmt(pagar)}</td>'
            f'<td>{_fmt(receber)}</td><td{classe}>{_fmt(saldo)}</td></tr>'
        )
    return "\n".join(linhas)


TEMPLATE = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fluxo de Caixa</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;700;800&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
  :root{{
    --bg:#f5f7f5; --surface:#ffffff; --surface-2:#eef2ef;
    --ink:#12231d; --ink-muted:#5c6e66; --ink-faint:#8a9a92;
    --line:#dbe4de; --line-strong:#c3d0c8;
    --accent:#0b6b53; --accent-soft:#e3f1ec;
    --critical:#a92419; --critical-soft:#fbeae8; --critical-line:#e7b8b1;
    --warning:#93590a; --warning-soft:#fbf1de;
  }}
  @media (prefers-color-scheme: dark){{
    :root:not([data-theme="light"]){{
      --bg:#0d1512; --surface:#131e19; --surface-2:#17231d;
      --ink:#e9efeb; --ink-muted:#98a89f; --ink-faint:#657168;
      --line:#243129; --line-strong:#324338;
      --accent:#3fcb9c; --accent-soft:#16342a;
      --critical:#ff7a6b; --critical-soft:#33170f; --critical-line:#5c2c22;
      --warning:#e8b34a; --warning-soft:#2e2510;
    }}
  }}
  :root[data-theme="dark"]{{
    --bg:#0d1512; --surface:#131e19; --surface-2:#17231d;
    --ink:#e9efeb; --ink-muted:#98a89f; --ink-faint:#657168;
    --line:#243129; --line-strong:#324338;
    --accent:#3fcb9c; --accent-soft:#16342a;
    --critical:#ff7a6b; --critical-soft:#33170f; --critical-line:#5c2c22;
    --warning:#e8b34a; --warning-soft:#2e2510;
  }}
  *{{box-sizing:border-box;}}
  body{{background:var(--bg); color:var(--ink); font-family:"IBM Plex Sans", ui-sans-serif, system-ui, sans-serif; padding:40px 20px 64px; margin:0;}}
  .page{{max-width:1000px;margin:0 auto;}}
  .eyebrow{{font-family:"IBM Plex Mono", ui-monospace, monospace; font-size:11.5px; letter-spacing:.12em; text-transform:uppercase; color:var(--accent); font-weight:600; display:flex; align-items:center; gap:8px;}}
  .eyebrow::before{{content:""; width:7px;height:7px;border-radius:50%; background:var(--accent); display:inline-block;}}
  header{{display:flex; justify-content:space-between; align-items:flex-end; gap:24px; flex-wrap:wrap; border-bottom:1px solid var(--line-strong); padding-bottom:22px; margin-bottom:28px;}}
  h1{{font-family:"Archivo", ui-sans-serif, sans-serif; font-weight:800; font-size:clamp(28px,4vw,38px); letter-spacing:-0.01em; text-wrap:balance; margin:6px 0 0; color:var(--ink);}}
  .subline{{font-size:13.5px; color:var(--ink-muted); max-width:46ch; line-height:1.5;}}
  .asof{{text-align:right; font-family:"IBM Plex Mono", monospace; font-size:12px; color:var(--ink-faint); line-height:1.6;}}
  .asof b{{color:var(--ink-muted); font-weight:600;}}
  .kpis{{display:grid; grid-template-columns:repeat(4,1fr); gap:1px; background:var(--line); border:1px solid var(--line); border-radius:10px; overflow:hidden; margin-bottom:34px;}}
  .kpi{{background:var(--surface); padding:18px 20px; display:flex; flex-direction:column; gap:6px;}}
  .kpi .label{{font-size:11.5px; text-transform:uppercase; letter-spacing:.08em; color:var(--ink-faint); font-weight:600;}}
  .kpi .value{{font-family:"IBM Plex Mono", monospace; font-weight:600; font-size:22px; font-variant-numeric:tabular-nums; letter-spacing:-0.01em;}}
  .kpi.negative .value{{color:var(--critical);}}
  .kpi .hint{{font-size:11.5px; color:var(--ink-faint);}}
  .empresa{{margin-bottom:26px; background:var(--surface); border:1px solid var(--line); border-radius:10px; overflow:hidden;}}
  .empresa-head{{display:flex; justify-content:space-between; align-items:center; gap:16px; padding:14px 20px; background:var(--surface-2); border-bottom:1px solid var(--line); flex-wrap:wrap;}}
  .empresa-head h2{{font-family:"Archivo", sans-serif; font-size:16px; font-weight:700; margin:0; letter-spacing:-0.005em;}}
  .empresa-totais{{display:flex; gap:22px; font-family:"IBM Plex Mono", monospace; font-size:13px; font-variant-numeric:tabular-nums;}}
  .empresa-totais .t{{display:flex; flex-direction:column; align-items:flex-end; gap:2px;}}
  .empresa-totais .t .tl{{font-family:"IBM Plex Sans", sans-serif; font-size:10px; text-transform:uppercase; letter-spacing:.06em; color:var(--ink-faint); font-weight:600;}}
  .empresa-totais .t .tv{{font-weight:600;}}
  .empresa-totais .t .tv.neg{{color:var(--critical);}}
  table{{width:100%; border-collapse:collapse;}}
  .tablewrap{{overflow-x:auto;}}
  th{{text-align:right; font-size:10.5px; text-transform:uppercase; letter-spacing:.06em; color:var(--ink-faint); font-weight:600; padding:10px 20px 8px; white-space:nowrap;}}
  th:first-child{{text-align:left;}}
  td{{padding:11px 20px; font-size:13.5px; border-top:1px solid var(--line); white-space:nowrap;}}
  td:not(:first-child){{text-align:right; font-family:"IBM Plex Mono", monospace; font-variant-numeric:tabular-nums;}}
  tr.negativa{{background:var(--critical-soft);}}
  tr.negativa td{{border-top-color:var(--critical-line);}}
  tr.negativa td:last-child{{color:var(--critical); font-weight:600;}}
  tr.outras td{{color:var(--ink-faint); font-style:italic;}}
  tr.outras td:not(:first-child){{font-style:normal;}}
  .dash{{color:var(--ink-faint);}}
  .flag{{display:inline-flex; align-items:center; gap:5px; font-family:"IBM Plex Sans", sans-serif; font-size:10.5px; font-weight:600; letter-spacing:.02em; color:var(--critical); background:var(--critical-soft); border:1px solid var(--critical-line); padding:2px 7px 2px 6px; border-radius:20px; margin-left:9px; vertical-align:1px;}}
  .flag::before{{content:""; width:5px;height:5px;border-radius:50%; background:var(--critical);}}
  .section-title{{font-family:"Archivo", sans-serif; font-size:13px; font-weight:700; text-transform:uppercase; letter-spacing:.06em; color:var(--ink-muted); margin:38px 0 14px; display:flex; align-items:center; gap:10px;}}
  .section-title::after{{content:""; flex:1; height:1px; background:var(--line-strong);}}
  .detail-grid{{display:grid; grid-template-columns:1fr 1fr; gap:18px; margin-bottom:8px;}}
  .listcard{{background:var(--surface); border:1px solid var(--line); border-radius:10px; overflow:hidden;}}
  .listcard-head{{padding:13px 18px; background:var(--surface-2); border-bottom:1px solid var(--line); display:flex; justify-content:space-between; align-items:baseline;}}
  .listcard-head h3{{font-family:"Archivo", sans-serif; font-size:14.5px; font-weight:700; margin:0;}}
  .listcard-head .kind{{font-family:"IBM Plex Mono", monospace; font-size:11px; color:var(--ink-faint);}}
  .list-empresa{{border-bottom:1px solid var(--line);}}
  .list-empresa:last-child{{border-bottom:none;}}
  .list-empresa-head{{display:flex; justify-content:space-between; align-items:baseline; padding:10px 18px 4px;}}
  .list-empresa-head .nome{{font-size:11.5px; font-weight:600; text-transform:uppercase; letter-spacing:.04em; color:var(--ink-muted);}}
  .list-empresa-head .subtotal{{font-family:"IBM Plex Mono", monospace; font-size:12.5px; font-weight:600; font-variant-numeric:tabular-nums;}}
  .list-row{{display:flex; justify-content:space-between; align-items:baseline; gap:14px; padding:7px 18px;}}
  .list-row .quem{{font-size:13px; line-height:1.35;}}
  .list-row .cat{{display:block; font-size:10.5px; color:var(--ink-faint); margin-top:1px;}}
  .list-row .val{{font-family:"IBM Plex Mono", monospace; font-size:13px; font-variant-numeric:tabular-nums; white-space:nowrap; font-weight:500;}}
  .list-row.mais{{padding-top:6px; padding-bottom:10px;}}
  .list-row.mais .quem, .list-row.mais .val{{color:var(--ink-faint); font-style:italic; font-size:12px;}}
  .list-empty{{padding:12px 18px 16px; font-size:12.5px; color:var(--ink-faint); font-style:italic;}}
  table.horizon{{margin-bottom:34px;}}
  table.horizon td, table.horizon th{{text-align:right;}}
  table.horizon td:first-child, table.horizon th:first-child{{text-align:left; font-family:"IBM Plex Mono",monospace; font-size:12px; color:var(--ink-muted);}}
  table.horizon thead tr:first-child th{{border-bottom:1px solid var(--line);}}
  table.horizon thead tr:last-child th:first-child{{text-align:right; font-family:"IBM Plex Mono", monospace; font-size:10.5px; color:var(--ink-faint);}}
  table.horizon td.neg{{color:var(--critical); font-weight:600;}}
  table.bordero{{margin-bottom:34px; table-layout:fixed;}}
  table.bordero th:nth-child(3), table.bordero td:nth-child(3){{white-space:normal; word-break:break-word; width:32%;}}
  table.bordero th:nth-child(1), table.bordero td:nth-child(1){{width:15%;}}
  table.bordero th:nth-child(2), table.bordero td:nth-child(2){{width:14%;}}
  table.bordero th:nth-child(4), table.bordero td:nth-child(4){{width:19%; white-space:normal; word-break:break-word;}}
  table.bordero th:nth-child(5), table.bordero td:nth-child(5){{width:20%;}}
  table.bordero tr.subtotal-banco td{{border-top:1px dashed var(--line-strong); font-weight:500; color:var(--ink-faint); font-style:italic; font-size:12px; padding-top:7px; padding-bottom:7px;}}
  table.bordero tr.subtotal td{{border-top:1px solid var(--line-strong); font-weight:600; color:var(--ink-muted); font-style:italic;}}
  table.bordero tr.total td{{border-top:2px solid var(--line-strong); font-weight:700; background:var(--surface-2);}}
  table.divergencias{{margin-bottom:34px;}}
  table.divergencias td.status{{font-weight:600; text-align:left;}}
  table.divergencias td.status.neg{{color:var(--critical);}}
  table.divergencias td.status.warn{{color:var(--warning);}}
  .callout{{margin:28px 0; padding:16px 20px; background:var(--warning-soft); border:1px solid color-mix(in srgb, var(--warning) 35%, var(--line)); border-left:3px solid var(--warning); border-radius:8px; font-size:13.5px; line-height:1.6; color:var(--ink);}}
  .callout b{{color:var(--warning);}}
  footer{{margin-top:36px; padding-top:18px; border-top:1px solid var(--line); display:grid; gap:8px;}}
  footer p{{font-size:12px; color:var(--ink-faint); line-height:1.6; margin:0; max-width:70ch;}}
  footer p b{{color:var(--ink-muted);}}
  @media (max-width:760px){{ .detail-grid{{grid-template-columns:1fr;}} }}
  @media (max-width:640px){{ .kpis{{grid-template-columns:repeat(2,1fr);}} .empresa-totais{{display:none;}} header{{flex-direction:column; align-items:flex-start;}} .asof{{text-align:left;}} .print-btn{{position:static; margin-bottom:16px; width:100%;}} }}
  .print-btn{{
    position:fixed; top:20px; right:20px; z-index:50;
    font-family:"IBM Plex Sans", sans-serif; font-size:12.5px; font-weight:600;
    padding:9px 18px; border-radius:8px; border:1px solid var(--accent);
    background:var(--accent); color:#fff; cursor:pointer;
    box-shadow:0 2px 10px rgba(0,0,0,.18);
  }}
  .print-btn:hover{{ filter:brightness(1.08); }}
  @media print{{
    @page{{ size:A4 landscape; margin:10mm; }}
    :root{{
      --bg:#f5f7f5; --surface:#ffffff; --surface-2:#eef2ef;
      --ink:#12231d; --ink-muted:#5c6e66; --ink-faint:#8a9a92;
      --line:#dbe4de; --line-strong:#c3d0c8;
      --accent:#0b6b53; --accent-soft:#e3f1ec;
      --critical:#a92419; --critical-soft:#fbeae8; --critical-line:#e7b8b1;
      --warning:#93590a; --warning-soft:#fbf1de;
    }}
    .print-btn{{display:none !important;}}
    body{{background:#fff; padding:0; font-size:11px;}}
    .page{{max-width:none;}}
    header{{padding-bottom:12px; margin-bottom:16px;}}
    h1{{font-size:22px; margin-top:4px;}}
    .subline{{font-size:11px;}}
    .kpis{{margin-bottom:16px;}}
    .kpi{{padding:10px 12px;}}
    .kpi .value{{font-size:16px;}}
    .empresa{{margin-bottom:14px;}}
    .section-title{{margin:16px 0 8px;}}
    /* .tablewrap tinha rolagem horizontal pra tela - em papel nao tem
       "rolar", so corta o que passar da largura, por isso e essencial
       desligar aqui e compensar com fonte/espacamento bem menores. */
    .tablewrap{{overflow-x:visible !important;}}
    table{{font-size:10px;}}
    th{{padding:5px 8px 4px; font-size:8.5px;}}
    td{{padding:5px 8px; font-size:10px;}}
    .empresa-totais{{font-size:10.5px; gap:14px;}}
    /* table.horizon (16 colunas: Dia + 5 empresas x Pagar/Receber/Saldo)
       nao tinha table-layout:fixed - crescia livre pro tamanho do
       conteudo (por isso rolava na tela) e isso vazava pra fora da
       pagina no PDF, cortando dado de verdade (achado real, 2026-09-22).
       IMPORTANTE: font-size tem que mirar TH/TD direto, nao a <table> -
       o CSS de tela ja define font-size em cada td/th, entao um
       font-size na <table> nunca chega por heranca (outro achado real
       da mesma sessao de debug). */
    table.horizon{{table-layout:fixed;}}
    table.horizon th, table.horizon td{{font-size:8px !important; padding:2px 3px; overflow:hidden;}}
    table.bordero td:nth-child(3), table.bordero td:nth-child(4){{white-space:normal;}}
    .listcard-head, .list-empresa-head{{padding:8px 12px 3px;}}
    .list-row{{padding:4px 12px;}}
    footer{{margin-top:16px; padding-top:10px;}}
    footer p{{font-size:9.5px;}}
    tr{{break-inside:avoid;}}
    .empresa{{break-inside:avoid;}}
    .section-title{{break-after:avoid;}}
    .empresa-head{{break-after:avoid;}}
  }}
</style>
</head>
<body>
<div class="page">

  <button class="print-btn" type="button" onclick="window.print()">Imprimir / Salvar PDF</button>

  <header>
    <div>
      <div class="eyebrow">Grupo Frutamix · Fluxo de caixa</div>
      <h1>Previsão para hoje</h1>
      <p class="subline">Saldo bancário Omie já fechado de ontem cruzado com contas a pagar e a receber previstas para hoje, por conta corrente.</p>
    </div>
    <div class="asof">
      Gerado em <b>{gerado_em}</b><br>
      Saldo apurado em <b>{data_saldo}</b> · previsão <b>{data_previsao}</b><br>
      {n_empresas} empresas · {n_bancos} bancos
    </div>
  </header>

  <div class="kpis">
    <div class="kpi"><div class="label">Saldo D-1 (operacional)</div><div class="value">{saldo_total}</div><div class="hint">fechamento de ontem, contas operacionais</div></div>
    <div class="kpi"><div class="label">A receber hoje</div><div class="value">{receber_total}</div><div class="hint">previsão de recebimento</div></div>
    <div class="kpi"><div class="label">A pagar hoje</div><div class="value">{pagar_total}</div><div class="hint">previsão de pagamento</div></div>
    <div class="kpi{kpi_proj_classe}"><div class="label">Saldo projetado</div><div class="value">{projetado_total}</div><div class="hint">{n_negativas_texto}</div></div>
  </div>

{blocos_empresa}

  <div class="section-title">Detalhamento de hoje, por fornecedor/cliente</div>

  <div class="detail-grid">
    <div class="listcard">
      <div class="listcard-head"><h3>Principais contas a pagar</h3><span class="kind">{pagar_total}</span></div>
{listas_pagar}
    </div>
    <div class="listcard">
      <div class="listcard-head"><h3>Principais contas a receber</h3><span class="kind">{receber_total}</span></div>
{listas_receber}
    </div>
  </div>

  <div class="section-title">Situação consolidada do grupo (acumulada)</div>
  <div class="tablewrap">
  <table class="horizon">
    <thead><tr><th>Dia</th><th>A pagar</th><th>A receber</th><th>Saldo projetado</th></tr></thead>
    <tbody>
{linhas_consolidado}
    </tbody>
  </table>
  </div>

  <div class="section-title">Projeção por empresa (acumulada)</div>
  <div class="tablewrap">
  <table class="horizon">
    <thead>
      <tr><th rowspan="2">Dia</th>{horizon_cabecalho}</tr>
      <tr>{horizon_subcabecalho}</tr>
    </thead>
    <tbody>
{linhas_horizonte}
    </tbody>
  </table>
  </div>

  <div class="section-title">Borderô de aprovação — contas a pagar de hoje</div>
  <div class="tablewrap">
  <table class="bordero">
    <thead><tr><th>Empresa</th><th>Banco</th><th>Fornecedor</th><th>NF</th><th>Valor a pagar</th></tr></thead>
    <tbody>
{linhas_bordero}
    </tbody>
  </table>
  </div>

  <div class="section-title">Divergências de ontem — previsto × realizado ({data_saldo})</div>
  <div class="tablewrap">
  <table class="divergencias">
    <thead><tr><th>Empresa</th><th>Tipo</th><th>Fornecedor</th><th>NF</th><th>Previsto</th><th>Realizado</th><th>Status</th></tr></thead>
    <tbody>
{linhas_divergencias}
    </tbody>
  </table>
  </div>

  <footer>
    <p><b>Saldo de D-1 (ontem), já fechado</b> — não é o saldo "ao vivo" de hoje; é o saldo de fechamento do dia anterior, que não muda mais e não fica sujeito a lançamentos de hoje ainda não conciliados no Omie. A previsão de pagar/receber, essa sim, é de hoje (D+0).</p>
    <p><b>Saldo Omie, não saldo bancário confirmado</b> — reflete as baixas lançadas no Omie; a conciliação bancária histórica ainda diverge e não deve ser usada como referência.</p>
    <p><b>Contas operacionais</b> excluem aplicações/investimentos (CDB, Rende Fácil e afins), contas de maquininha/marketplace (PDV) e contas inativas.</p>
    <p><b>Projeção acumulada</b> soma/subtrai dia a dia a partir do saldo de D-1 — cada dia já inclui o efeito dos anteriores, não é o resultado isolado daquele dia. O quadro consolidado soma as {n_empresas} empresas do grupo; o quadro por empresa detalha pagar/receber/saldo de cada uma.</p>
  </footer>

</div>
</body>
</html>
"""


def gerar(saida=SAIDA_PADRAO, horizonte_dias=HORIZONTE_DIAS, codigos_banco=None, rastrear_divergencias=True):
    """Uma UNICA coleta por empresa (financeiro.coletar_dados_empresa),
    usada pra derivar as 3 visoes do relatorio (saldo por conta de hoje,
    detalhe por fornecedor de hoje, projecao dos proximos horizonte_dias
    dias) - evita repetir a mesma busca de titulos 3x, importante pra
    rodar todo dia sem ninguem olhando.

    Regra de datas (pedido explicito do Chico, 2026-09-21): saldo = D-1
    (fechamento de ontem, via financeiro.buscar_saldo_atual/nSaldoAnterior
    - nao usa saldo "ao vivo" de hoje, que pode estar incompleto), previsao
    (pagar/receber/projetado) = D+0 (hoje). O horizonte acumulado comeca
    em D+0 (hoje) e vai ate D+horizonte_dias-1.

    rastrear_divergencias=False pula o log de previsto (logs/
    previsto_diario.csv) e a comparacao contra o realizado - usado nesta
    copia (servico Render) porque o disco nao e persistente aqui, entao
    o log nunca sobrevive de um dia pro outro mesmo (ver README.md); sem
    isso o quadro "Divergencias de ontem" sempre sairia vazio do mesmo
    jeito, so que gastando chamadas extras de API no Omie (buscar
    movimentos baixados) a toa. Decisao de 2026-09-21, pra reduzir carga
    na API depois de testes que sobrecarregaram o Omie com disparos
    simultaneos."""
    entidades = config.entidades_configuradas()
    if not entidades:
        raise RuntimeError("nenhuma conta com credenciais preenchidas no .env")
    if codigos_banco is None:
        codigos_banco = set(config.BANCOS.keys())

    hoje = datetime.now().date()
    hoje_str = hoje.strftime("%d/%m/%Y")
    data_saldo_str = (hoje - timedelta(days=1)).strftime("%d/%m/%Y")
    datas_horizonte = [(hoje + timedelta(days=i)).strftime("%d/%m/%Y") for i in range(0, horizonte_dias)]
    previsao = datas_horizonte[0]

    blocos = []
    listas_pagar = []
    listas_receber = []
    totais_por_empresa = {}
    periodo = {}
    n_negativas = 0
    bordero_pagar = []
    previsto_hoje = []
    divergencias_ontem = []
    previsto_ontem_por_empresa = _carregar_previsto(PREVISTO_LOG, data_saldo_str) if rastrear_divergencias else {}

    for ent in entidades:
        nome = ent["nome"]
        print(f"--- {nome} ---")
        dados = financeiro.coletar_dados_empresa(ent)
        client = dados["client"]
        cache_fornecedor = {}
        banco_por_ncc = {c["nCodCC"]: config.BANCOS.get(c.get("codigo_banco"), c.get("codigo_banco")) for c in dados["contas"]}
        contas_correntes_desc = {c["nCodCC"]: c.get("descricao", "") for c in dados["contas"]}

        contas_op = [
            c for c in dados["contas"]
            if c.get("inativo") == "N" and c.get("codigo_banco") in codigos_banco
            and financeiro._e_conta_operacional(c.get("descricao"))
        ]
        contas_info = {}
        for c in contas_op:
            try:
                saldo = financeiro.buscar_saldo_atual(client, c["nCodCC"], hoje_str)
            except OmieError as e:
                print(f"  [AVISO {nome}/{c.get('descricao')}] {e}")
                saldo = None
            contas_info[c["nCodCC"]] = {
                "banco": config.BANCOS.get(c.get("codigo_banco"), c.get("codigo_banco")),
                "conta": c.get("descricao", ""),
                "saldo": saldo or 0.0,
                "a_pagar": 0.0,
                "a_receber": 0.0,
            }
        saldo_empresa = sum(v["saldo"] for v in contas_info.values())

        outras_pagar_hoje = 0.0
        outras_receber_hoje = 0.0
        itens_pagar_hoje = []
        itens_receber_hoje = []
        pagar_por_dia = {}
        receber_por_dia = {}

        for t in dados["pagar_abertos"]:
            d = t.get("data_previsao")
            valor = t.get("valor_documento") or 0
            pagar_por_dia[d] = pagar_por_dia.get(d, 0) + valor
            if d == previsao:
                ncc = t.get("id_conta_corrente")
                if ncc in contas_info:
                    contas_info[ncc]["a_pagar"] += valor
                else:
                    outras_pagar_hoje += valor
                categoria = dados["categorias_por_codigo"].get(t.get("codigo_categoria") or "", {})
                categoria_desc = categoria.get("descricao") or t.get("codigo_categoria") or "(sem categoria)"
                fornecedor = financeiro.resolver_nome_fornecedor(client, t.get("codigo_cliente_fornecedor"), cache_fornecedor)
                nf = t.get("numero_documento") or "-"
                itens_pagar_hoje.append({
                    "fornecedor": fornecedor,
                    "categoria": categoria_desc,
                    "valor": valor,
                })
                bordero_pagar.append({
                    "empresa": nome,
                    "banco": banco_por_ncc.get(ncc, "-"),
                    "fornecedor": fornecedor,
                    "nf": nf,
                    "valor": valor,
                })
                previsto_hoje.append({
                    "data": previsao, "tipo": "pagar", "empresa": nome,
                    "codigo_lancamento_omie": t.get("codigo_lancamento_omie") or "",
                    "fornecedor": fornecedor, "numero_documento": nf,
                    "categoria": categoria_desc, "valor": valor,
                })

        for t in dados["receber_abertos"]:
            d = t.get("data_previsao")
            valor = t.get("valor_documento") or 0
            receber_por_dia[d] = receber_por_dia.get(d, 0) + valor
            if d == previsao:
                ncc = t.get("id_conta_corrente")
                if ncc in contas_info:
                    contas_info[ncc]["a_receber"] += valor
                else:
                    outras_receber_hoje += valor
                categoria = dados["categorias_por_codigo"].get(t.get("codigo_categoria") or "", {})
                categoria_desc = categoria.get("descricao") or t.get("codigo_categoria") or "(sem categoria)"
                fornecedor = financeiro.resolver_nome_fornecedor(client, t.get("codigo_cliente_fornecedor"), cache_fornecedor)
                itens_receber_hoje.append({
                    "fornecedor": fornecedor,
                    "categoria": categoria_desc,
                    "valor": valor,
                })
                previsto_hoje.append({
                    "data": previsao, "tipo": "receber", "empresa": nome,
                    "codigo_lancamento_omie": t.get("codigo_lancamento_omie") or "",
                    "fornecedor": fornecedor, "numero_documento": t.get("numero_documento") or "-",
                    "categoria": categoria_desc, "valor": valor,
                })

        if nome in previsto_ontem_por_empresa:
            try:
                divergencias_empresa = financeiro.comparar_previsto_realizado(
                    client, nome, data_saldo_str, previsto_ontem_por_empresa[nome],
                    contas_correntes_desc, dados["categorias_por_codigo"],
                )
            except OmieError as e:
                print(f"  [AVISO {nome}] nao consegui comparar previsto x realizado de ontem ({e})")
                divergencias_empresa = []
            divergencias_ontem.extend(divergencias_empresa)
            for div in divergencias_empresa:
                _log(DIVERGENCIAS_LOG, {
                    "data": div["data"], "tipo": div["tipo"], "empresa": div["empresa"],
                    "fornecedor": div["fornecedor"], "numero_documento": div["numero_documento"],
                    "valor_previsto": div["valor"], "valor_realizado": div["valor_realizado"],
                    "status": div["status"],
                })

        a_pagar_hoje = sum(v["a_pagar"] for v in contas_info.values()) + outras_pagar_hoje
        a_receber_hoje = sum(v["a_receber"] for v in contas_info.values()) + outras_receber_hoje
        totais_por_empresa[nome] = {
            "saldo_atual_omie": saldo_empresa,
            "a_pagar_previsto": a_pagar_hoje,
            "a_receber_previsto": a_receber_hoje,
            "saldo_projetado": saldo_empresa + a_receber_hoje - a_pagar_hoje,
        }

        contas_ent = [
            {
                "conta": info["conta"], "banco": info["banco"], "saldo_atual": info["saldo"],
                "a_pagar_previsto": info["a_pagar"], "a_receber_previsto": info["a_receber"],
                "saldo_projetado": info["saldo"] + info["a_receber"] - info["a_pagar"],
            }
            for info in contas_info.values()
        ]
        n_negativas += sum(1 for c in contas_ent if c["saldo_projetado"] < 0)
        if outras_pagar_hoje or outras_receber_hoje:
            contas_ent.append({
                "conta": "(outras contas - nao operacionais/outro banco)", "banco": "",
                "saldo_atual": None, "a_pagar_previsto": outras_pagar_hoje,
                "a_receber_previsto": outras_receber_hoje, "saldo_projetado": None,
            })
        blocos.append(_bloco_empresa(nome, contas_ent, totais_por_empresa[nome]))
        listas_pagar.append(_bloco_lista_empresa(nome, a_pagar_hoje, itens_pagar_hoje))
        listas_receber.append(_bloco_lista_empresa(nome, a_receber_hoje, itens_receber_hoje))

        running = saldo_empresa
        dias_linha = []
        for d in datas_horizonte:
            ap = pagar_por_dia.get(d, 0.0)
            ar = receber_por_dia.get(d, 0.0)
            running = running + ar - ap
            dias_linha.append({"data": d, "a_pagar": ap, "a_receber": ar, "saldo_projetado": running})
        periodo[nome] = {"saldo_inicial": saldo_empresa, "dias": dias_linha}

    if rastrear_divergencias:
        _registrar_previsto(PREVISTO_LOG, previsto_hoje)
    linhas_divergencias = _linhas_divergencias(divergencias_ontem, bool(previsto_ontem_por_empresa))

    nomes_empresas = [ent["nome"] for ent in entidades]
    horizon_cabecalho = "".join(f'<th colspan="3">{_esc(n)}</th>' for n in nomes_empresas)
    horizon_subcabecalho = "<th>Pagar</th><th>Receber</th><th>Saldo</th>" * len(nomes_empresas)
    linhas_horizonte = _linhas_horizonte(periodo, nomes_empresas)
    linhas_consolidado = _linhas_consolidado_grupo(periodo, nomes_empresas)

    saldo_total = sum(t["saldo_atual_omie"] for t in totais_por_empresa.values())
    pagar_total = sum(t["a_pagar_previsto"] for t in totais_por_empresa.values())
    receber_total = sum(t["a_receber_previsto"] for t in totais_por_empresa.values())
    projetado_total = saldo_total + receber_total - pagar_total
    bancos_alvo = codigos_banco

    html_final = TEMPLATE.format(
        gerado_em=datetime.now().strftime("%d/%m/%Y %H:%M"),
        data_saldo=data_saldo_str,
        data_previsao=previsao,
        n_empresas=len(entidades),
        n_bancos=len(bancos_alvo),
        saldo_total="R$ " + _fmt(saldo_total),
        receber_total="R$ " + _fmt(receber_total),
        pagar_total="R$ " + _fmt(pagar_total),
        projetado_total="R$ " + _fmt(projetado_total),
        kpi_proj_classe=" negative" if projetado_total < 0 else "",
        n_negativas_texto=(
            "nenhuma conta negativa hoje" if n_negativas == 0
            else f"1 conta fica negativa hoje" if n_negativas == 1
            else f"{n_negativas} contas ficam negativas hoje"
        ),
        blocos_empresa="\n".join(blocos),
        listas_pagar="\n".join(listas_pagar),
        listas_receber="\n".join(listas_receber),
        horizon_cabecalho=horizon_cabecalho,
        horizon_subcabecalho=horizon_subcabecalho,
        linhas_horizonte=linhas_horizonte,
        linhas_consolidado=linhas_consolidado,
        linhas_bordero=_linhas_bordero(bordero_pagar),
        linhas_divergencias=linhas_divergencias,
    )

    os.makedirs(os.path.dirname(saida) or ".", exist_ok=True)
    with open(saida, "w", encoding="utf-8") as f:
        f.write(html_final)

    return {
        "saida": os.path.abspath(saida),
        "saldo_total": saldo_total,
        "pagar_total": pagar_total,
        "receber_total": receber_total,
        "n_negativas": n_negativas,
        "n_divergencias": len(divergencias_ontem),
    }


def _log(caminho, linha):
    novo = not os.path.exists(caminho)
    os.makedirs(os.path.dirname(caminho) or ".", exist_ok=True)
    with open(caminho, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(linha.keys()))
        if novo:
            w.writeheader()
        w.writerow(linha)


def _registrar_previsto(caminho, linhas):
    """Acrescenta ao log historico (logs/previsto_diario.csv) o que foi
    previsto pra hoje - vira o "previsto" contra o qual a proxima execucao
    (amanha) compara o que foi de fato baixado (financeiro.
    comparar_previsto_realizado). Uma linha por titulo, nao por empresa -
    precisa do codigo_lancamento_omie individual pra casar depois."""
    for linha in linhas:
        _log(caminho, linha)


def _carregar_previsto(caminho, data):
    """Le do log historico as linhas previstas pra `data` (DD/MM/AAAA),
    agrupadas por empresa - usadas pra comparar contra o realizado dessa
    mesma data. Devolve {} se o log ainda nao existe (primeira execucao,
    nada pra comparar ainda) ou nao tem nada pra essa data."""
    if not os.path.exists(caminho):
        return {}
    por_empresa = {}
    with open(caminho, newline="", encoding="utf-8") as f:
        for linha in csv.DictReader(f):
            if linha.get("data") != data:
                continue
            linha["valor"] = float(linha["valor"]) if linha.get("valor") not in (None, "") else 0.0
            por_empresa.setdefault(linha["empresa"], []).append(linha)
    return por_empresa


def _linhas_divergencias(itens, existiam_dados_ontem):
    """Uma linha por divergencia entre o previsto (log de ontem) e o
    realizado (baixas de ontem no Omie) - ver financeiro.
    comparar_previsto_realizado. So aparece o que NAO bateu certinho."""
    if not existiam_dados_ontem:
        return (
            '<tr><td colspan="7" class="dash" style="text-align:center; font-style:italic;">'
            "Ainda não há previsão registrada de ontem pra comparar — a partir de amanhã esse quadro já aparece preenchido.</td></tr>"
        )
    if not itens:
        return (
            '<tr><td colspan="7" style="text-align:center; font-style:italic; color:var(--accent);">'
            "Nenhuma divergência ontem — tudo que estava previsto bateu com o que foi baixado no Omie.</td></tr>"
        )
    rotulo_status = {
        "NAO_REALIZADO": "Não baixado",
        "VALOR_DIFERENTE": "Valor diferente",
        "FORA_DA_PREVISAO": "Fora da previsão",
    }
    ordenados = sorted(itens, key=lambda i: (i["empresa"], i["status"], -(i["valor"] or i["valor_realizado"] or 0)))
    linhas = []
    for i in ordenados:
        classe = " neg" if i["status"] in ("NAO_REALIZADO", "VALOR_DIFERENTE") else " warn"
        linhas.append(
            f'<tr><td>{_esc(i["empresa"])}</td><td>{_esc(i["tipo"].capitalize())}</td>'
            f'<td>{_esc(i["fornecedor"])}</td><td>{_esc(i["numero_documento"] or "-")}</td>'
            f'<td>{_fmt(i["valor"]) if i["valor"] not in (None, "") else "—"}</td>'
            f'<td>{_fmt(i["valor_realizado"]) if i["valor_realizado"] not in (None, "") else "—"}</td>'
            f'<td class="status{classe}">{rotulo_status.get(i["status"], i["status"])}</td></tr>'
        )
    return "\n".join(linhas)


def main():
    ap = argparse.ArgumentParser(description="Gera o relatorio diario de fluxo de caixa (fluxo_caixa.html).")
    ap.add_argument("--saida", default=SAIDA_PADRAO)
    ap.add_argument("--horizonte", type=int, default=HORIZONTE_DIAS)
    ap.add_argument("--log", default=LOG_PADRAO)
    args = ap.parse_args()

    inicio = datetime.now()
    try:
        resultado = gerar(args.saida, args.horizonte)
        _log(args.log, {
            "quando": inicio.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "ok",
            "arquivo": resultado["saida"],
            "saldo_total": resultado["saldo_total"],
            "pagar_total": resultado["pagar_total"],
            "receber_total": resultado["receber_total"],
            "contas_negativas": resultado["n_negativas"],
            "erro": "",
        })
        print(f"OK - relatorio gerado em {resultado['saida']}")
    except Exception as e:
        _log(args.log, {
            "quando": inicio.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "erro",
            "arquivo": "",
            "saldo_total": "",
            "pagar_total": "",
            "receber_total": "",
            "contas_negativas": "",
            "erro": f"{e}",
        })
        print(f"ERRO: {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
