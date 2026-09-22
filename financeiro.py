"""
financeiro.py
--------------
Primeiro modulo pluggavel do omie_relatorios: extrai, por conta Omie,

  1. o plano de contas completo (ListarCategorias) - codigo, descricao,
     tipo_categoria e se esta ativa. Serve de base pra revisar/criticar o
     plano de contas do grupo (proximo passo e olhar isso no chat).
  2. os movimentos financeiros BAIXADOS (pago/recebido) no periodo
     (financas/mf), ja com o nome da categoria e da conta corrente
     resolvidos.

Sem De-Para entre contas por enquanto: cada conta mantem suas categorias
como estao cadastradas nela. Os CSVs consolidados so colocam as contas
lado a lado (coluna "conta"), sem tentar unificar nomenclatura.

USO:
  python financeiro.py --inicio 01/01/2026 --fim 31/08/2026
  python financeiro.py --inicio 01/01/2026 --fim 31/08/2026 --conta MATRIZ
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timedelta

import config
from omie_client import OmieClient, OmieError

SAIDA_DIR = "saida"


def buscar_categorias(client):
    """Plano de contas completo da conta (estrutura, nao so nome)."""
    categorias = []
    pagina = 1
    while True:
        corpo = client.chamar(
            config.ENDPOINTS["categoria"],
            "ListarCategorias",
            {"pagina": pagina, "registros_por_pagina": 100},
        )
        registros = corpo.get("categoria_cadastro", [])
        if not registros:
            break
        for c in registros:
            categorias.append({
                "codigo": c.get("codigo"),
                "descricao": c.get("descricao"),
                "tipo_categoria": c.get("tipo_categoria", ""),
                "conta_inativa": c.get("conta_inativa", "N"),
            })
        total_paginas = corpo.get("total_de_paginas", 1)
        if pagina >= total_paginas:
            break
        pagina += 1
    return categorias


def buscar_contas_correntes(client):
    """{nCodCC: descricao} - so pra resolver nome legivel na hora de montar o relatorio."""
    contas = {}
    pagina = 1
    while True:
        corpo = client.chamar(
            config.ENDPOINTS["conta_corrente"],
            "ListarContasCorrentes",
            {"pagina": pagina, "registros_por_pagina": 100, "apenas_importado_api": "N"},
        )
        registros = corpo.get("ListarContasCorrentes", [])
        if not registros:
            break
        for c in registros:
            contas[c.get("nCodCC")] = c.get("descricao", "")
        total_paginas = corpo.get("total_de_paginas", 1)
        if pagina >= total_paginas:
            break
        pagina += 1
    return contas


def buscar_contas_correntes_detalhado(client):
    """Todas as contas correntes cadastradas na conta, com o registro
    completo (nao so nCodCC:descricao) - usado pra filtrar por banco
    (codigo_banco) e por ativo/inativo antes de puxar saldo."""
    contas = []
    pagina = 1
    while True:
        corpo = client.chamar(
            config.ENDPOINTS["conta_corrente"],
            "ListarContasCorrentes",
            {"pagina": pagina, "registros_por_pagina": 100, "apenas_importado_api": "N"},
        )
        registros = corpo.get("ListarContasCorrentes", [])
        if not registros:
            break
        contas.extend(registros)
        total_paginas = corpo.get("total_de_paginas", 1)
        if pagina >= total_paginas:
            break
        pagina += 1
    return contas


def buscar_saldo_atual(client, n_cod_cc, hoje):
    """Saldo FECHADO de ontem (D-1) de uma conta corrente (financas/extrato,
    ListarExtrato, cExibirApenasSaldo=S - nao traz lista de movimentos, so
    os saldos). dPeriodoInicial/Final sao obrigatorios mesmo so pedindo o
    saldo (confirmado contra a API real - sem eles da erro de campo
    obrigatorio); pedimos hoje-hoje mas lemos `nSaldoAnterior` (saldo de
    fechamento do dia ANTERIOR ao periodo pedido), nao `nSaldoAtual`.

    Confirmado contra a API real, 2026-09-21: `nSaldoAtual` e sempre o
    saldo em tempo real de AGORA, independente do periodo pedido (pedir
    o periodo de ontem nao muda nSaldoAtual) - por isso nao serve pra
    "saldo de D-1". `nSaldoAnterior` com periodo=hoje e o saldo ja
    FECHADO de ontem, nao sujeito a lancamentos de hoje ainda nao
    conciliados - pedido explicito do Chico: saldo = D-1, previsao = D+0."""
    corpo = client.chamar(
        config.ENDPOINTS["extrato"],
        "ListarExtrato",
        {
            "nCodCC": n_cod_cc,
            "cExibirApenasSaldo": "S",
            "dPeriodoInicial": hoje,
            "dPeriodoFinal": hoje,
        },
    )
    return corpo.get("nSaldoAnterior")


def _e_conta_operacional(descricao):
    """Exclui contas de aplicacao/investimento e transitorias de
    maquininha/marketplace (PDV) - pedido explicito do Chico, 2026-09-03:
    essas nao contam como saldo bancario disponivel de verdade. Filtra por
    TIPO (palavras-chave), nao so pelo prefixo "Z." - a convencao "Z." nao
    e seguida em todas as empresas (achado real: "Itaú Unibanco -
    Aplicação" da Pura Fruta nao tem "Z." mas e uma aplicacao igual as
    outras). Mesma coisa com "PDV": so pegar "[pdv]" (colchetes) deixava
    passar "PDV - IFOOD" e "PDV - 99FOOD" (Matriz, Frutamix) - contas
    ATIVAS de marketplace contando R$36k+ como saldo operacional
    disponivel sem ser (achado real, 2026-09-22, o Chico nao reconheceu
    o saldo da conta "PDV - IFOOD" no relatorio). Substring "pdv" pega
    qualquer formatacao (colchetes, hifen, com/sem espaco)."""
    d_lower = (descricao or "").lower()
    if d_lower.startswith("z."):
        return False
    if "pdv" in d_lower:
        return False
    if "rende f" in d_lower:  # "Rende Facil"/"Rende Fácil"
        return False
    if "aplica" in d_lower:  # "Aplicação"/"Aplicacao"
        return False
    if "cdb" in d_lower:
        return False
    return True


def saldos_bancarios(entidades, codigos_banco=None, apenas_operacionais=True):
    """Saldo atual de cada conta corrente ATIVA nos bancos informados
    (codigos_banco: set de codigos FEBRABAN, ver config.BANCOS - None usa
    todos os bancos mapeados), em cada conta Omie configurada. Devolve uma
    lista de dicts: empresa, banco, conta, saldo_atual.

    apenas_operacionais=True (padrao) exclui contas "Z."/PDV/Rende Facil -
    ver _e_conta_operacional. Passe False pra ver a lista completa (ex:
    auditoria de todas as contas cadastradas)."""
    if codigos_banco is None:
        codigos_banco = set(config.BANCOS.keys())
    hoje = datetime.now().strftime("%d/%m/%Y")
    linhas = []
    for ent in entidades:
        client = OmieClient(ent["app_key"], ent["app_secret"], nome_conta=ent["nome"])
        try:
            contas = buscar_contas_correntes_detalhado(client)
        except OmieError as e:
            print(f"  [ERRO {ent['nome']}] {e}")
            continue
        alvo = [c for c in contas if c.get("inativo") == "N" and c.get("codigo_banco") in codigos_banco]
        if apenas_operacionais:
            alvo = [c for c in alvo if _e_conta_operacional(c.get("descricao"))]
        for c in alvo:
            try:
                saldo = buscar_saldo_atual(client, c["nCodCC"], hoje)
            except OmieError as e:
                print(f"  [AVISO {ent['nome']}/{c.get('descricao')}] {e}")
                saldo = None
            linhas.append({
                "empresa": ent["nome"],
                "banco": config.BANCOS.get(c.get("codigo_banco"), c.get("codigo_banco")),
                "conta": c.get("descricao", ""),
                "saldo_atual": saldo,
            })
    return linhas


# (endpoint, call, campo da lista na resposta) - contapagar e contareceber
# tem exatamente a mesma forma (confirmado contra a API real), so muda o
# status "quitado": PAGO (pagar) vs RECEBIDO (receber).
_CONFIG_TITULOS = {
    "pagar": ("conta_pagar", "ListarContasPagar", "conta_pagar_cadastro"),
    "receber": ("conta_receber", "ListarContasReceber", "conta_receber_cadastro"),
}


def buscar_titulos_abertos(client, tipo, status="EMABERTO"):
    """Titulos (a pagar ou a receber) ainda nao quitados (status_titulo em
    CANCELADO/PAGO/RECEBIDO/LIQUIDADO/EMABERTO/PAGTO_PARCIAL/VENCEHOJE/
    AVENCER/ATRASADO - confirmado contra a API real). "EMABERTO" cobre tudo
    que ainda nao foi pago/recebido/cancelado (inclui parcial, vence hoje, a
    vencer, atrasado). Sem filtro de data no lado do servidor (o parametro
    filtrar_por_data_de/ate desta chamada filtra por emissao/registro, NAO
    por vencimento/previsao - confirmado contra dado real) - filtrar por
    data no lado do cliente depois de buscar tudo."""
    endpoint_key, call, campo_lista = _CONFIG_TITULOS[tipo]
    titulos = []
    pagina = 1
    while True:
        corpo = client.chamar(
            config.ENDPOINTS[endpoint_key],
            call,
            {"pagina": pagina, "registros_por_pagina": 100, "filtrar_por_status": status},
        )
        titulos.extend(corpo.get(campo_lista, []))
        total_paginas = corpo.get("total_de_paginas", 1)
        if pagina >= total_paginas:
            break
        pagina += 1
    return titulos


def resolver_nome_fornecedor(client, codigo_cliente_fornecedor, cache):
    """Nome do fornecedor/cliente (ConsultarCliente) - com cache por conta,
    ja que varios titulos costumam repetir o mesmo fornecedor."""
    if codigo_cliente_fornecedor in cache:
        return cache[codigo_cliente_fornecedor]
    try:
        corpo = client.chamar(
            config.ENDPOINTS["clientes"],
            "ConsultarCliente",
            {"codigo_cliente_omie": codigo_cliente_fornecedor},
        )
        nome = corpo.get("nome_fantasia") or corpo.get("razao_social") or str(codigo_cliente_fornecedor)
    except OmieError:
        nome = str(codigo_cliente_fornecedor)
    cache[codigo_cliente_fornecedor] = nome
    return nome


def titulos_no_dia(entidades, tipo, data_previsao):
    """Para cada conta configurada: titulos (pagar ou receber) em aberto
    cuja data_previsao (previsao de pagamento/recebimento - o que a
    empresa de fato planeja, pode diferir do vencimento contratual) bate
    exatamente com data_previsao (DD/MM/AAAA). Devolve uma lista de dicts
    prontos pra tabela: empresa, fornecedor/cliente, valor, categoria
    (descricao, nao codigo), numero_documento."""
    linhas = []
    for ent in entidades:
        client = OmieClient(ent["app_key"], ent["app_secret"], nome_conta=ent["nome"])
        cache_fornecedor = {}
        try:
            categorias_por_codigo = {c["codigo"]: c for c in buscar_categorias(client)}
        except OmieError as e:
            print(f"  [AVISO {ent['nome']}] nao consegui buscar categorias ({e}) - coluna categoria ficara so com o codigo")
            categorias_por_codigo = {}
        try:
            titulos = buscar_titulos_abertos(client, tipo)
        except OmieError as e:
            print(f"  [ERRO {ent['nome']}] {e}")
            continue
        for t in titulos:
            if t.get("data_previsao") != data_previsao:
                continue
            fornecedor = resolver_nome_fornecedor(client, t.get("codigo_cliente_fornecedor"), cache_fornecedor)
            codigo_categoria = t.get("codigo_categoria") or ""
            categoria = categorias_por_codigo.get(codigo_categoria, {})
            linhas.append({
                "empresa": ent["nome"],
                "fornecedor": fornecedor,
                "valor": t.get("valor_documento"),
                "previsao": t.get("data_previsao"),
                "vencimento": t.get("data_vencimento"),
                "status": t.get("status_titulo"),
                "categoria": categoria.get("descricao") or codigo_categoria or "(sem categoria)",
                "numero_documento": t.get("numero_documento", ""),
                "id_conta_corrente": t.get("id_conta_corrente"),
            })
    return linhas


def contas_a_pagar_no_dia(entidades, data_previsao):
    return titulos_no_dia(entidades, "pagar", data_previsao)


def contas_a_receber_no_dia(entidades, data_previsao):
    return titulos_no_dia(entidades, "receber", data_previsao)


def coletar_dados_empresa(ent):
    """Uma UNICA passada de coleta por empresa: contas correntes (registro
    completo), categorias e TODOS os titulos abertos (pagar e receber).
    Usada por relatorios que precisam de mais de uma visao dos mesmos
    dados (ex: saldo por conta + detalhe de amanha + projecao de N dias)
    sem repetir a mesma busca varias vezes - relevante pra rodar sem
    ninguem olhando (Task Scheduler), onde menos chamadas = menos risco
    de bloqueio por "consumo indevido" (ver omie_client.py)."""
    client = OmieClient(ent["app_key"], ent["app_secret"], nome_conta=ent["nome"])
    contas = buscar_contas_correntes_detalhado(client)
    try:
        categorias_por_codigo = {c["codigo"]: c for c in buscar_categorias(client)}
    except OmieError as e:
        print(f"  [AVISO {ent['nome']}] nao consegui buscar categorias ({e})")
        categorias_por_codigo = {}
    pagar_abertos = buscar_titulos_abertos(client, "pagar")
    receber_abertos = buscar_titulos_abertos(client, "receber")
    return {
        "empresa": ent["nome"],
        "client": client,
        "contas": contas,
        "categorias_por_codigo": categorias_por_codigo,
        "pagar_abertos": pagar_abertos,
        "receber_abertos": receber_abertos,
    }


def resumo_fluxo_por_conta(entidades, data_previsao, codigos_banco=None):
    """Como resumo_fluxo_dia, mas abre saldo + a pagar/a receber previstos
    POR CONTA CORRENTE (banco), nao so por empresa - usa o
    id_conta_corrente de cada titulo (campo real da API, confirmado)
    pra casar cada previsao com a conta de onde ela vai sair/entrar.
    Titulos presos a uma conta fora do filtro operacional (aplicacao/PDV/
    outro banco) caem numa linha "(outras contas)" por empresa - pra nao
    sumir valor nenhum do total, so deixar claro que nao esta numa conta
    operacional das que voce pediu."""
    if codigos_banco is None:
        codigos_banco = set(config.BANCOS.keys())
    hoje = datetime.now().strftime("%d/%m/%Y")
    linhas = []
    for ent in entidades:
        client = OmieClient(ent["app_key"], ent["app_secret"], nome_conta=ent["nome"])
        try:
            contas = buscar_contas_correntes_detalhado(client)
        except OmieError as e:
            print(f"  [ERRO {ent['nome']}] {e}")
            continue
        mapa_contas = {c["nCodCC"]: c for c in contas}
        alvo = [
            c for c in contas
            if c.get("inativo") == "N" and c.get("codigo_banco") in codigos_banco and _e_conta_operacional(c.get("descricao"))
        ]

        por_conta = {}
        for c in alvo:
            por_conta[c["nCodCC"]] = {
                "empresa": ent["nome"],
                "banco": config.BANCOS.get(c.get("codigo_banco"), c.get("codigo_banco")),
                "conta": c.get("descricao", ""),
                "saldo_atual": 0.0,
                "a_pagar_previsto": 0.0,
                "a_receber_previsto": 0.0,
            }
            try:
                saldo = buscar_saldo_atual(client, c["nCodCC"], hoje)
            except OmieError as e:
                print(f"  [AVISO {ent['nome']}/{c.get('descricao')}] {e}")
                saldo = None
            por_conta[c["nCodCC"]]["saldo_atual"] = saldo or 0.0

        outras = {
            "empresa": ent["nome"],
            "banco": "",
            "conta": "(outras contas - nao operacionais/outro banco)",
            "saldo_atual": None,
            "a_pagar_previsto": 0.0,
            "a_receber_previsto": 0.0,
        }
        tem_outras = False

        for tipo, campo in (("pagar", "a_pagar_previsto"), ("receber", "a_receber_previsto")):
            try:
                titulos = buscar_titulos_abertos(client, tipo)
            except OmieError as e:
                print(f"  [ERRO {ent['nome']}/{tipo}] {e}")
                continue
            for t in titulos:
                if t.get("data_previsao") != data_previsao:
                    continue
                n_cod_cc = t.get("id_conta_corrente")
                valor = t.get("valor_documento") or 0
                if n_cod_cc in por_conta:
                    por_conta[n_cod_cc][campo] += valor
                else:
                    outras[campo] += valor
                    tem_outras = True

        for linha in por_conta.values():
            linha["saldo_projetado"] = linha["saldo_atual"] + linha["a_receber_previsto"] - linha["a_pagar_previsto"]
            linhas.append(linha)
        if tem_outras:
            outras["saldo_projetado"] = None
            linhas.append(outras)
    return linhas


def resumo_fluxo_dia(entidades, data_previsao, codigos_banco=None):
    """Por empresa: saldo bancario Omie hoje (soma das contas ativas nos
    bancos-alvo) + total a receber previsto pra data_previsao - total a
    pagar previsto pra data_previsao = saldo projetado. Saldo "Omie" (nao
    "saldo bancario confirmado") - ver conversa sobre conciliacao
    historica divergente."""
    saldos = saldos_bancarios(entidades, codigos_banco)
    pagar = contas_a_pagar_no_dia(entidades, data_previsao)
    receber = contas_a_receber_no_dia(entidades, data_previsao)

    resumo = {}
    for ent in entidades:
        resumo[ent["nome"]] = {
            "empresa": ent["nome"],
            "saldo_atual_omie": 0.0,
            "a_pagar_previsto": 0.0,
            "a_receber_previsto": 0.0,
        }
    for s in saldos:
        if s["saldo_atual"] is not None:
            resumo[s["empresa"]]["saldo_atual_omie"] += s["saldo_atual"]
    for p in pagar:
        resumo[p["empresa"]]["a_pagar_previsto"] += p["valor"] or 0
    for r in receber:
        resumo[r["empresa"]]["a_receber_previsto"] += r["valor"] or 0

    linhas = list(resumo.values())
    for l in linhas:
        l["saldo_projetado"] = l["saldo_atual_omie"] + l["a_receber_previsto"] - l["a_pagar_previsto"]
    return linhas


def resumo_fluxo_periodo(entidades, dias, codigos_banco=None, data_base=None):
    """Projecao de caixa ACUMULADA dia a dia, por empresa, para os proximos
    `dias` dias corridos a partir de hoje (ou data_base). Busca os titulos
    em aberto (EMABERTO) UMA VEZ so - o mesmo custo de API pra 3 ou pra 15
    dias, porque o filtro por data_previsao e feito no lado do cliente,
    nao em cada chamada. Retorna {empresa: [{data, a_pagar, a_receber,
    saldo_projetado}, ...]} - saldo_projetado de cada dia ja soma/subtrai
    os dias anteriores (nao e o resultado isolado daquele dia)."""
    hoje = data_base or datetime.now().date()
    datas = [(hoje + timedelta(days=i)).strftime("%d/%m/%Y") for i in range(1, dias + 1)]

    saldos = saldos_bancarios(entidades, codigos_banco)
    saldo_inicial = {ent["nome"]: 0.0 for ent in entidades}
    for s in saldos:
        if s["saldo_atual"] is not None:
            saldo_inicial[s["empresa"]] += s["saldo_atual"]

    resultado = {}
    for ent in entidades:
        client = OmieClient(ent["app_key"], ent["app_secret"], nome_conta=ent["nome"])
        try:
            pagar_abertos = buscar_titulos_abertos(client, "pagar")
        except OmieError as e:
            print(f"  [ERRO {ent['nome']}/pagar] {e}")
            pagar_abertos = []
        try:
            receber_abertos = buscar_titulos_abertos(client, "receber")
        except OmieError as e:
            print(f"  [ERRO {ent['nome']}/receber] {e}")
            receber_abertos = []

        pagar_por_dia = {}
        for t in pagar_abertos:
            d = t.get("data_previsao")
            pagar_por_dia[d] = pagar_por_dia.get(d, 0) + (t.get("valor_documento") or 0)
        receber_por_dia = {}
        for t in receber_abertos:
            d = t.get("data_previsao")
            receber_por_dia[d] = receber_por_dia.get(d, 0) + (t.get("valor_documento") or 0)

        running = saldo_inicial[ent["nome"]]
        dias_linha = []
        for d in datas:
            a_pagar = pagar_por_dia.get(d, 0.0)
            a_receber = receber_por_dia.get(d, 0.0)
            running = running + a_receber - a_pagar
            dias_linha.append({
                "data": d,
                "a_pagar": a_pagar,
                "a_receber": a_receber,
                "saldo_projetado": running,
            })
        resultado[ent["nome"]] = {
            "saldo_inicial": saldo_inicial[ent["nome"]],
            "dias": dias_linha,
        }
    return resultado


def buscar_movimentos(client, data_inicio, data_fim, tipos=("pagar", "receber")):
    """Movimentos financeiros baixados (pago/recebido) no periodo, todos os
    tipos de lancamento (titulo formal BXCP/BXCR + transferencia direta CC),
    deduplicados por (nCodBaixa, nCodMovCC, nCodTitulo)."""
    mapa_tipo = {"pagar": ("P", ["BXCP", "CC"]), "receber": ("R", ["BXCR", "CC"])}
    movimentos = []
    vistos = set()
    for tipo in tipos:
        natureza, tipos_lancamento = mapa_tipo[tipo]
        for tp_lancamento in tipos_lancamento:
            pagina = 1
            while True:
                corpo = client.chamar(
                    config.ENDPOINTS["mf"],
                    "ListarMovimentos",
                    {
                        "nPagina": pagina,
                        "nRegPorPagina": 100,
                        "cNatureza": natureza,
                        "cTpLancamento": tp_lancamento,
                        "dDtPagtoDe": data_inicio,
                        "dDtPagtoAte": data_fim,
                    },
                )
                registros = corpo.get("movimentos", [])
                if not registros:
                    break
                for reg in registros:
                    det = reg.get("detalhes", {})
                    chave = (det.get("nCodBaixa"), det.get("nCodMovCC"), det.get("nCodTitulo"))
                    if chave in vistos:
                        continue
                    vistos.add(chave)
                    movimentos.append({"tipo": "Pagar" if tipo == "pagar" else "Receber", "reg": reg})
                total_paginas = corpo.get("nTotPaginas", 1)
                if pagina >= total_paginas:
                    break
                pagina += 1
    return movimentos


def normalizar_movimento(item, contas_correntes, categorias_por_codigo):
    det = item["reg"].get("detalhes", {})
    resumo = item["reg"].get("resumo", {})
    codigo_categoria = det.get("cCodCateg") or ""
    categoria = categorias_por_codigo.get(codigo_categoria, {})
    return {
        "tipo": item["tipo"],
        "data_baixa": det.get("dDtPagamento") or "",
        "valor": resumo.get("nValPago") or det.get("nValorMovCC") or 0,
        "cnpj_cpf_cliente_fornecedor": det.get("cCPFCNPJCliente") or "",
        "numero_documento": det.get("cNumTitulo") or det.get("cNumDocFiscal") or "",
        "codigo_lancamento_omie": det.get("nCodTitulo") or "",
        "conta_corrente": contas_correntes.get(det.get("nCodCC"), ""),
        "categoria_codigo": codigo_categoria,
        "categoria_descricao": categoria.get("descricao", ""),
        "categoria_tipo": categoria.get("tipo_categoria", ""),
        "cod_movimento_cc": det.get("nCodMovCC") or "",
    }


def comparar_previsto_realizado(client, empresa, data, previsto_do_dia, contas_correntes, categorias_por_codigo, tolerancia=0.01):
    """Compara titulos PREVISTOS pra `data` (lista de dicts vinda do log
    historico - ver relatorio_diario._registrar_previsto - com pelo menos
    tipo/codigo_lancamento_omie/fornecedor/numero_documento/valor) contra
    o que foi de fato BAIXADO em `data` (financas/mf ListarMovimentos, via
    buscar_movimentos). Casa pelo `codigo_lancamento_omie` - ID interno do
    titulo no Omie, o MESMO nos dois lados (previsao e baixa, confirmado
    contra a API real) - nao pelo numero_documento (esse e digitado pelo
    usuario, pode repetir ou ficar vazio).

    Devolve so as DIVERGENCIAS (o que bateu certinho nao aparece, pra nao
    poluir o log que o Chico quer acompanhar):
    - NAO_REALIZADO: previsto pra `data`, ainda nao baixado.
    - VALOR_DIFERENTE: baixado em `data`, mas com valor diferente do
      previsto (alem da `tolerancia`, ex: pagamento parcial).
    - FORA_DA_PREVISAO: baixado em `data` mas nao estava previsto pra
      esse dia (pagamento antecipado/adiantado, ou titulo novo lancado
      depois da previsao ter sido registrada)."""
    movimentos = buscar_movimentos(client, data, data)
    realizado_por_chave = {}
    for item in movimentos:
        norm = normalizar_movimento(item, contas_correntes, categorias_por_codigo)
        codigo = norm["codigo_lancamento_omie"]
        if codigo:
            realizado_por_chave[(norm["tipo"].lower(), str(codigo))] = norm

    divergencias = []
    vistos = set()
    for p in previsto_do_dia:
        chave = (p["tipo"].lower(), str(p["codigo_lancamento_omie"]))
        vistos.add(chave)
        realizado = realizado_por_chave.get(chave)
        if realizado is None:
            divergencias.append({**p, "valor_realizado": None, "status": "NAO_REALIZADO"})
        elif abs((realizado["valor"] or 0) - (p["valor"] or 0)) > tolerancia:
            divergencias.append({**p, "valor_realizado": realizado["valor"], "status": "VALOR_DIFERENTE"})

    for (tipo, codigo), realizado in realizado_por_chave.items():
        if (tipo, codigo) in vistos:
            continue
        divergencias.append({
            "data": data,
            "tipo": tipo,
            "empresa": empresa,
            "codigo_lancamento_omie": codigo,
            "fornecedor": f"CNPJ/CPF {realizado['cnpj_cpf_cliente_fornecedor']}" if realizado["cnpj_cpf_cliente_fornecedor"] else "(nao identificado)",
            "numero_documento": realizado["numero_documento"],
            "categoria": realizado["categoria_descricao"] or realizado["categoria_codigo"],
            "valor": None,
            "valor_realizado": realizado["valor"],
            "status": "FORA_DA_PREVISAO",
        })
    return divergencias


def gerar_relatorio(entidades, data_inicio, data_fim, saida_dir=SAIDA_DIR):
    os.makedirs(saida_dir, exist_ok=True)
    categorias_consolidado = []
    movimentos_consolidado = []

    for ent in entidades:
        print(f"--- {ent['nome']} ---")
        client = OmieClient(ent["app_key"], ent["app_secret"], nome_conta=ent["nome"])

        try:
            categorias = buscar_categorias(client)
            print(f"  Categorias: {len(categorias)}")
        except OmieError as e:
            print(f"  [ERRO categorias] {e}")
            categorias = []

        categorias_por_codigo = {c["codigo"]: c for c in categorias}

        try:
            contas_correntes = buscar_contas_correntes(client)
        except OmieError as e:
            print(f"  [AVISO contas correntes] {e} - coluna conta_corrente ficara vazia")
            contas_correntes = {}

        try:
            brutos = buscar_movimentos(client, data_inicio, data_fim)
            movimentos = [normalizar_movimento(m, contas_correntes, categorias_por_codigo) for m in brutos]
            print(f"  Movimentos baixados no periodo: {len(movimentos)}")
        except OmieError as e:
            print(f"  [ERRO movimentos] {e}")
            movimentos = []

        for c in categorias:
            categorias_consolidado.append({"conta": ent["nome"], **c})
        for m in movimentos:
            movimentos_consolidado.append({"conta": ent["nome"], **m})

        sufixo_arquivo = ent["sufixo"].lower()
        _salvar_csv(categorias, os.path.join(saida_dir, f"categorias_{sufixo_arquivo}.csv"))
        _salvar_csv(movimentos, os.path.join(saida_dir, f"movimentos_{sufixo_arquivo}.csv"))

    _salvar_csv(categorias_consolidado, os.path.join(saida_dir, "categorias_consolidado.csv"))
    _salvar_csv(movimentos_consolidado, os.path.join(saida_dir, "movimentos_consolidado.csv"))
    print(f"\nOK - relatorios salvos em {os.path.abspath(saida_dir)}/")


def _salvar_csv(linhas, caminho):
    if not linhas:
        # ainda cria o arquivo vazio (com cabecalho, se possivel) pra deixar claro que rodou
        open(caminho, "w", encoding="utf-8").close()
        return
    campos = list(linhas[0].keys())
    with open(caminho, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos)
        w.writeheader()
        w.writerows(linhas)


def main():
    ap = argparse.ArgumentParser(description="Extrai plano de contas + movimentos financeiros do Omie, por conta.")
    ap.add_argument("--inicio", required=True, help="Data inicio (DD/MM/AAAA)")
    ap.add_argument("--fim", required=True, help="Data fim (DD/MM/AAAA)")
    ap.add_argument("--conta", help="Roda so uma conta (sufixo, ex: MATRIZ) - util pra testar antes de rodar todas")
    ap.add_argument("--saida", default=SAIDA_DIR, help="Pasta de saida dos CSVs (padrao: saida/)")
    args = ap.parse_args()

    entidades = config.entidades_configuradas()
    if not entidades:
        print("ERRO: nenhuma conta com credenciais preenchidas no .env (veja .env.example).")
        sys.exit(1)

    faltando = [e["nome"] for e in config.ENTIDADES if e["sufixo"] not in {x["sufixo"] for x in entidades}]
    if faltando:
        print(f"[AVISO] sem credenciais no .env, pulando: {', '.join(faltando)}")

    if args.conta:
        entidades = [e for e in entidades if e["sufixo"] == args.conta.upper()]
        if not entidades:
            print(f"ERRO: conta '{args.conta}' nao encontrada nas credenciais preenchidas.")
            sys.exit(1)

    gerar_relatorio(entidades, args.inicio, args.fim, args.saida)


if __name__ == "__main__":
    main()
