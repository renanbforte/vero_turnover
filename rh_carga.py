# =============================================================================
# rh_carga.py — da planilha para o PostgreSQL
# -----------------------------------------------------------------------------
# O CAMINHO INTEIRO, em quatro movimentos:
#
#   .xlsx  ->  rh_analise.analisar()  ->  tabelas do schema `rh`  ->  cartoes no RAG
#            (calculo, sem banco)        (este arquivo)              (rh_insights.py)
#
# DUAS DECISOES QUE VALEM EXPLICAR:
#
# 1. A CARGA E "SUBSTITUI TUDO", NAO "ACRESCENTA".
#    Cada execucao apaga o conteudo das tabelas `rh.*` e grava de novo, DENTRO
#    DE UMA TRANSACAO SO. Parece agressivo, e e o certo aqui: as tabelas
#    agregadas sao DERIVADAS da planilha inteira. Se a planilha mudar e a gente
#    so acrescentasse, ficariam dois retratos misturados -- e o agente somaria
#    os dois sem saber. Uma transacao unica garante que ninguem leia o banco no
#    meio da troca: ou esta tudo velho, ou esta tudo novo.
#
# 2. A CARGA NAO CALCULA NADA.
#    Todo numero vem pronto do `rh_analise.py`. Este arquivo so traduz DataFrame
#    em INSERT. Se um numero estiver errado, ha um lugar so para procurar --
#    e nao dois, com a duvida de qual dos dois esta certo.
#
# COMO RODAR:
#   uv run python -m passos.passo25_carregar_rh "C:\\caminho\\Base.xlsx"
# =============================================================================

import logging
from contextlib import contextmanager

import pandas as pd
from psycopg_pool import ConnectionPool

from config import exigir

log = logging.getLogger("rh_carga")

# A ORDEM IMPORTA: e a ordem em que as tabelas sao limpas. Nao ha chave
# estrangeira entre elas hoje, mas manter a ordem de dependencia logica
# (agregado antes do detalhe) evita surpresa se um dia houver.
TABELAS = [
    "rh.qualidade", "rh.premissas", "rh.motivos", "rh.fatores",
    "rh.sobrevivencia", "rh.coortes", "rh.prioridade", "rh.segmentos",
    "rh.metricas_mensais", "rh.colaboradores",
]


def _linhas(df, colunas):
    """DataFrame -> lista de tuplas, com NaN virando None.

    POR QUE ISTO EXISTE. O pandas usa `NaN` (um float) para "vazio". O
    PostgreSQL nao conhece NaN em coluna numerica -- ele quer `NULL`. Sem esta
    conversao, uma coorte sem horizonte observavel gravaria o texto 'NaN' ou
    daria erro de tipo, dependendo da coluna. `where(notna)` troca todos de uma
    vez, e a lista sai pronta para o `executemany`.
    """
    limpo = df[colunas].astype(object).where(pd.notna(df[colunas]), None)
    return [tuple(linha) for linha in limpo.to_numpy()]


def _inserir(cur, tabela, colunas, linhas):
    if not linhas:
        log.warning("%s: nada a inserir", tabela)
        return 0
    marcadores = ", ".join(["%s"] * len(colunas))
    cur.executemany(
        f"INSERT INTO {tabela} ({', '.join(colunas)}) VALUES ({marcadores})",
        linhas,
    )
    log.info("%s: %d linhas", tabela, len(linhas))
    return len(linhas)


def gravar(pool, resultado):
    """Grava o resultado de `rh_analise.analisar()`. Devolve {tabela: linhas}."""
    df = resultado["colaboradores"]
    contagem = {}

    # UMA transacao para tudo. O `with conn` do psycopg faz commit no fim e
    # ROLLBACK se qualquer coisa levantar excecao no meio -- que e exatamente o
    # que se quer numa troca de retrato: nunca ficar pela metade.
    with pool.connection() as conn, conn.cursor() as cur:
        for t in TABELAS:
            cur.execute(f"DELETE FROM {t}")

        # -- 1. o retrato ------------------------------------------------------
        colab = df.rename(columns={
            "id": "id_colaborador", "nivel": "nivel_cargo",
            "contrato": "tipo_contrato", "admissao": "data_admissao",
            "desligamento": "data_desligamento", "status": "status_desligamento",
            "motivo": "motivo_desligamento", "tempo_anos": "tempo_empresa_anos",
            "salario": "salario_base", "compa": "compa_ratio",
            "lideranca": "indice_lideranca", "horas_extras": "horas_extras_mes",
            "absenteismo": "absenteismo_dias_12m",
            "meses_promocao": "meses_desde_promocao",
            "treinamento": "horas_treinamento_12m",
            "movimentos": "movimentos_internos_24m",
            "faixa": "faixa_tempo_casa", "exposicao": "exposicao_ltm",
            "saiu": "saiu_ltm", "saiu_vol": "saiu_vol_ltm",
            "saiu_invol": "saiu_invol_ltm",
        }).copy()
        # As datas viram `date` do Python: o psycopg mapeia direto para DATE.
        for c in ["data_admissao", "data_desligamento"]:
            colab[c] = colab[c].dt.date
        # O motivo "Nao se aplica" e ruido de planilha: para um ativo, o campo
        # simplesmente nao tem valor. NULL diz isso; um texto fingiria que sim.
        colab.loc[colab.status_desligamento.eq("Ativo"), "motivo_desligamento"] = None

        cols_colab = [
            "id_colaborador", "area", "localidade", "nivel_cargo", "tipo_contrato",
            "data_admissao", "data_desligamento", "status_desligamento",
            "motivo_desligamento", "tempo_empresa_anos", "salario_base",
            "compa_ratio", "performance", "engajamento", "indice_lideranca",
            "horas_extras_mes", "absenteismo_dias_12m", "meses_desde_promocao",
            "horas_treinamento_12m", "movimentos_internos_24m",
            "faixa_tempo_casa", "exposicao_ltm", "saiu_ltm", "saiu_vol_ltm",
            "saiu_invol_ltm", "custo_reposicao",
        ]
        contagem["rh.colaboradores"] = _inserir(
            cur, "rh.colaboradores", cols_colab, _linhas(colab, cols_colab))

        # -- 2. as agregacoes --------------------------------------------------
        blocos = [
            ("rh.metricas_mensais", resultado["serie"],
             ["mes", "headcount_inicio", "headcount_fim", "headcount_medio",
              "admissoes", "desligamentos", "voluntarios", "involuntarios",
              "turnover_12m", "turnover_vol_12m", "turnover_invol_12m"]),
            ("rh.segmentos", resultado["segmentos"],
             ["dimensao", "valor", "headcount_ativo", "exposicao",
              "desl_voluntarios", "desl_involuntarios", "turnover_vol",
              "turnover_total", "share_saidas_vol", "excesso_saidas", "custo_excesso"]),
            ("rh.prioridade", resultado["prioridade"],
             ["area", "faixa_tempo_casa", "headcount", "exposicao", "saidas_vol",
              "turnover_vol", "esperado_no_baseline", "excesso_saidas", "custo_excesso"]),
            ("rh.coortes", resultado["coortes"],
             ["coorte", "contratacoes", "saida_vol_3m", "saida_vol_6m", "saida_vol_12m"]),
            ("rh.sobrevivencia", resultado["sobrevivencia"],
             ["mes_de_casa", "em_risco", "risco_acumulado"]),
            ("rh.fatores", resultado["fatores"],
             ["fator", "odds_ratio", "ic_inferior", "ic_superior", "p_valor", "significativo"]),
            ("rh.motivos", resultado["motivos"],
             ["motivo", "quantidade", "share", "engajamento_medio",
              "lideranca_media", "compa_medio", "tempo_casa_medio"]),
        ]
        for tabela, dados, colunas in blocos:
            contagem[tabela] = _inserir(cur, tabela, colunas, _linhas(dados, colunas))

        # -- 3. premissas e qualidade -----------------------------------------
        contagem["rh.premissas"] = _inserir(
            cur, "rh.premissas", ["chave", "valor", "descricao"],
            resultado["premissas"])
        contagem["rh.qualidade"] = _inserir(
            cur, "rh.qualidade", ["teste", "resultado", "decisao"],
            resultado["qualidade"])

        # O pseudo-R2 nao cabe em `rh.fatores` (que e uma linha por fator) e e
        # o numero que impede a leitura ingenua da regressao. Vai para premissas.
        cur.execute(
            "INSERT INTO rh.premissas (chave, valor, descricao) VALUES (%s, %s, %s)",
            ("pseudo_r2_regressao",
             str(resultado["fatores"].attrs.get("pseudo_r2", "n/d")),
             "Fracao da variacao explicada pelos fatores medidos. Baixo significa "
             "que a base mede pouco do fenomeno -- as associacoes valem, a explicacao nao."),
        )
    return contagem


@contextmanager
def abrir_carga():
    """Pool proprio, curto. A carga roda uma vez e fecha -- nao compartilha o
    pool do agente porque nem sempre o agente esta de pe quando ela roda."""
    url = exigir("DATABASE_URL", "Rode `uv run python scripts/preparar.py`.")
    pool = ConnectionPool(url, min_size=1, max_size=2, open=False)
    pool.open()
    try:
        yield pool
    finally:
        pool.close()
