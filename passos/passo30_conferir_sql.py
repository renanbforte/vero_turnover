# =============================================================================
# passo30_conferir_sql.py — a consulta exibida bate com o numero exibido?
# -----------------------------------------------------------------------------
# O PAINEL FAZ DUAS AFIRMACOES ao lado de cada grafico:
#   "este e o numero"  e  "esta e a consulta que produz este numero".
#
# A segunda e uma promessa facil de quebrar em silencio. Basta alguem mexer numa
# regra do `rh_analise.py` -- o corte de exposicao minima, a definicao da faixa
# de tempo de casa -- e o SQL exibido continua ali, bonito e ERRADO. Ninguem
# percebe, porque consulta exibida nao roda.
#
# Aqui ela roda. Para cada grafico do catalogo, este passo:
#   1. calcula o grafico em pandas, do jeito que a pagina calcula;
#   2. EXECUTA no PostgreSQL a consulta que a pagina exibe;
#   3. compara valor a valor, e falha se divergir alem da tolerancia.
#
# ⚠️ O QUE NAO E CONFERIDO, e por que: `fatores`. A consulta dele monta a matriz
# que entra na regressao, e nao os coeficientes -- ajustar Logit nao e coisa de
# SQL. Entao ali conferimos o que da para conferir: que o UNIVERSO e o mesmo
# (mesmo numero de linhas, mesmo numero de desfechos). O universo e justamente a
# decisao discutivel; o ajuste e aritmetica de biblioteca.
#
# COMO RODAR (da raiz do projeto):
#   uv run python -m passos.passo30_conferir_sql
#   uv run python -m passos.passo30_conferir_sql --area Atendimento
# =============================================================================

import logging
import math
import sys

import pandas as pd

from logs import configurar
from rh_carga import abrir_carga
from rh_exploracao import Premissas, carregar
from rh_sql_graficos import sql_do_grafico

configurar()
log = logging.getLogger("passo30")

# Tolerancia. Nao e frouxidao: pandas e PostgreSQL arredondam em momentos
# diferentes (um soma NUMERIC, o outro float64), e uma diferenca de centesimo
# num percentual nao e divergencia de regra. Ja um decimo seria.
TOLERANCIA = 0.02


def _vazio(v):
    """NaN do pandas e NULL do SQL sao A MESMA AFIRMACAO: "nao da para saber".

    Os dois aparecem nos mesmos lugares e pelo mesmo motivo -- janela de 12
    meses ainda aberta, horizonte de coorte ainda nao observavel. Tratar um como
    divergencia do outro faria o teste gritar exatamente onde os dois estao
    certos.
    """
    if v is None:
        return True
    try:
        return math.isnan(float(v))
    except (TypeError, ValueError):
        return False


def _perto(a, b):
    """Compara dois valores tolerando vazio dos dois lados e tipo diferente."""
    if _vazio(a) or _vazio(b):
        return _vazio(a) and _vazio(b)
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)
    return abs(fa - fb) <= max(TOLERANCIA, abs(fb) * 0.001)


def _rodar(pool, sql):
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql)
        colunas = [c.name for c in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=colunas)


def _comparar(nome, esperado: pd.DataFrame, obtido: pd.DataFrame, chaves, campos):
    """Confere `campos` linha a linha, casando as linhas por `chaves`.

    Casar por chave, e nao por posicao, e o que faz o teste apontar o PROBLEMA
    certo: se o SQL trouxer um segmento a mais, queremos ver "sobrou o segmento
    X", e nao trinta linhas desalinhadas.
    """
    problemas = []
    if esperado.empty and obtido.empty:
        return [], 0        # nada dos dois lados: 0 conferidos conta a historia

    e = esperado.set_index(chaves) if chaves else esperado
    o = obtido.set_index(chaves) if chaves else obtido

    faltando = [k for k in e.index if k not in set(o.index)]
    sobrando = [k for k in o.index if k not in set(e.index)]
    for k in faltando[:3]:
        problemas.append(f"o SQL nao trouxe a linha {k!r}")
    for k in sobrando[:3]:
        problemas.append(f"o SQL trouxe a linha extra {k!r}")

    conferidos = 0
    for k in e.index:
        if k not in set(o.index):
            continue
        for campo_pandas, campo_sql in campos:
            va = e.loc[k, campo_pandas]
            vb = o.loc[k, campo_sql]
            conferidos += 1
            if not _perto(va, vb):
                problemas.append(
                    f"{k!r} · {campo_pandas}: pandas={va!r}  sql={vb!r}")
    return problemas, conferidos


def conferir(pool, ctx, filtros, prem):
    """Roda as nove consultas e devolve [(grafico, problemas, conferidos)]."""
    db = ctx.data_base
    resultados = []

    def sql(id_grafico):
        return sql_do_grafico(id_grafico, filtros, prem.como_dict(), db)

    # --- evolucao: a serie mensal com a janela movel -------------------------
    e = ctx.serie.copy()
    e["mes"] = pd.to_datetime(e.mes).dt.date
    o = _rodar(pool, sql("evolucao"))
    resultados.append(("evolucao", *_comparar(
        "evolucao", e, o, ["mes"],
        [("headcount_fim", "headcount_fim"), ("desligamentos", "desligamentos"),
         ("turnover_12m", "turnover_12m"), ("turnover_vol_12m", "turnover_vol_12m"),
         ("turnover_invol_12m", "turnover_invol_12m")])))

    # --- quadro: contagem pura ----------------------------------------------
    o = _rodar(pool, sql("quadro"))
    resultados.append(("quadro", *_comparar(
        "quadro", e, o, ["mes"],
        [("headcount_fim", "headcount_fim"), ("admissoes", "admissoes"),
         ("voluntarios", "voluntarios"), ("involuntarios", "involuntarios")])))

    # --- tempo_de_casa: uma linha por faixa ---------------------------------
    seg = ctx.segmentos
    e = seg[seg.dimensao == "tempo_de_casa"].rename(columns={"valor": "faixa_tempo_casa"})
    o = _rodar(pool, sql("tempo_de_casa"))
    resultados.append(("tempo_de_casa", *_comparar(
        "tempo_de_casa", e, o, ["faixa_tempo_casa"],
        [("headcount_ativo", "headcount_ativo"), ("exposicao", "exposicao"),
         ("desl_voluntarios", "desl_voluntarios"), ("turnover_vol", "turnover_vol")])))

    # --- sobrevivencia: Kaplan-Meier ----------------------------------------
    e = ctx.sobrevivencia[ctx.sobrevivencia.mes_de_casa > 0]
    o = _rodar(pool, sql("sobrevivencia"))
    resultados.append(("sobrevivencia", *_comparar(
        "sobrevivencia", e, o, ["mes_de_casa"],
        [("em_risco", "em_risco"), ("risco_acumulado", "risco_acumulado")])))

    # --- coortes ------------------------------------------------------------
    o = _rodar(pool, sql("coortes"))
    resultados.append(("coortes", *_comparar(
        "coortes", ctx.coortes, o, ["coorte"],
        [("contratacoes", "contratacoes"), ("saida_vol_3m", "saida_vol_3m"),
         ("saida_vol_6m", "saida_vol_6m"), ("saida_vol_12m", "saida_vol_12m")])))

    # --- prioridade ---------------------------------------------------------
    if len(ctx.prioridade):
        o = _rodar(pool, sql("prioridade"))
        resultados.append(("prioridade", *_comparar(
            "prioridade", ctx.prioridade, o, ["area", "faixa_tempo_casa"],
            [("headcount", "headcount"), ("exposicao", "exposicao"),
             ("saidas_vol", "saidas_vol"), ("turnover_vol", "turnover_vol"),
             ("esperado_no_baseline", "esperado_no_baseline"),
             ("excesso_saidas", "excesso_saidas"),
             ("custo_excesso", "custo_excesso")])))

    # --- motivos ------------------------------------------------------------
    o = _rodar(pool, sql("motivos"))
    resultados.append(("motivos", *_comparar(
        "motivos", ctx.motivos, o, ["motivo"],
        [("quantidade", "quantidade"), ("share", "share"),
         ("engajamento_medio", "engajamento_medio"),
         ("lideranca_media", "lideranca_media"),
         ("compa_medio", "compa_medio"),
         ("tempo_casa_medio", "tempo_casa_medio")])))

    # --- custo: uma linha so ------------------------------------------------
    o = _rodar(pool, sql("custo"))
    e = ctx.custo
    problemas, conferidos = [], 0
    for campo in ["custo_vol", "custo_invol", "custo_total", "custo_entrada",
                  "custo_medio_vol", "folha_anual"]:
        va, vb = float(e[campo].iloc[0]), float(o[campo].iloc[0])
        conferidos += 1
        # R$ 1 de tolerancia: pandas soma float, o banco soma NUMERIC.
        if abs(va - vb) > max(1.0, abs(vb) * 0.0001):
            problemas.append(f"{campo}: pandas={va:,.2f}  sql={vb:,.2f}")
    resultados.append(("custo", problemas, conferidos))

    # --- fatores: so o universo (o ajuste nao e SQL) -------------------------
    o = _rodar(pool, sql("fatores"))
    universo = ctx.colaboradores[
        ctx.colaboradores.status.eq("Ativo") | ctx.colaboradores.saiu_vol]
    problemas, conferidos = [], 2
    if len(o) != len(universo):
        problemas.append(f"universo: pandas={len(universo)} linhas  sql={len(o)}")
    alvo_sql = int(o['pediu demissao (desfecho)'].sum())
    alvo_pd = int(universo.saiu_vol.sum())
    if alvo_sql != alvo_pd:
        problemas.append(f"desfechos: pandas={alvo_pd}  sql={alvo_sql}")
    resultados.append(("fatores (universo)", problemas, conferidos))

    return resultados


def main():
    filtros = []
    argumentos = sys.argv[1:]
    for i in range(0, len(argumentos) - 1, 2):
        campo = argumentos[i].lstrip("-")
        filtros.append({"campo": campo, "operador": "igual", "valor": argumentos[i + 1]})

    rotulo = ", ".join(f"{f['campo']}={f['valor']}" for f in filtros) or "base inteira"
    print(f"\n  Conferindo as consultas exibidas no painel — recorte: {rotulo}\n")

    prem = Premissas()
    with abrir_carga() as pool:
        ctx = carregar(pool, filtros, prem)
        print(f"  {ctx.pessoas} pessoas · data-base {ctx.data_base:%d/%m/%Y}\n")
        resultados = conferir(pool, ctx, filtros, prem)

    falhas = 0
    for nome, problemas, conferidos in resultados:
        ruins = problemas
        if ruins:
            falhas += 1
            print(f"  ✗ {nome:22} {len(ruins)} divergência(s)")
            for p in ruins[:6]:
                print(f"      {p}")
        else:
            nota = (f"{conferidos} valores conferidos" if conferidos
                    else "vazio dos dois lados — nada a comparar")
            print(f"  ✓ {nome:22} {nota}")

    total = sum(c for _, _, c in resultados)
    print(f"\n  {len(resultados) - falhas}/{len(resultados)} consultas conferem "
          f"({total} valores comparados).\n")
    if falhas:
        print("  ⚠️  A consulta exibida no painel NAO reproduz o número exibido.")
        print("      Corrija antes de apresentar: a promessa do painel é justamente essa.\n")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
