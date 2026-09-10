# =============================================================================
# rh_sql_graficos.py — a consulta que reproduz CADA grafico
# -----------------------------------------------------------------------------
# POR QUE ESTE ARQUIVO EXISTE
#
# A pagina executa UM unico SELECT (`SELECT * FROM rh.colaboradores WHERE ...`)
# e refaz todas as contas em pandas. Isso e proposital -- e o que permite mudar
# uma premissa e ver os nove graficos mudarem juntos, sem ir ao banco de novo.
#
# So que, na hora de responder "de onde veio ESTE numero?", exibir aquele SELECT
# nove vezes nao responde nada: ele e igual para todos os graficos e nao mostra
# nem o recorte proprio de cada um, nem a agregacao. E a pergunta que a banca faz
# nao e "o que a pagina executou" -- e "como este numero foi montado".
#
# Entao aqui esta a outra metade: para cada grafico, a consulta que produz A
# TABELA DAQUELE GRAFICO, com o recorte proprio dele dentro do WHERE, o GROUP BY
# que ele usa e as mesmas contas. Sao consultas de verdade: copiar, colar no
# psql e comparar com a tela. `passos/passo30_conferir_sql.py` faz exatamente
# isso, uma a uma, e falha se algum numero divergir do pandas.
#
# ⚠️ A UNICA QUE NAO REPRODUZ O RESULTADO E A DOS FATORES, e por um motivo
# honesto: ajustar uma regressao logistica nao e coisa de SQL. Ali a consulta
# monta a MATRIZ que entra no modelo -- o universo e as variaveis, que e onde
# moram as decisoes discutiveis -- e o ajuste continua em Python.
#
# AS PREMISSAS EDITAVEIS ENTRAM NO TEXTO DA CONSULTA.
# O banco tem uma coluna `custo_reposicao` gravada na carga, com os fatores
# padrao. Se a gente usasse ela aqui, a consulta divergiria da tela assim que
# alguem editasse um fator. Por isso o custo e ESCRITO POR EXTENSO no SQL, a
# partir do salario e do nivel, com os numeros que estao na tela naquele
# instante -- a consulta muda quando a premissa muda, como o grafico.
#
# SEGURANCA: nada aqui e concatenado a partir de texto do usuario. Os campos
# passam pela mesma lista branca do agente (`DIMENSOES`/`FILTROS_NUMERICOS`) e
# os valores sao literais escapados por `_lit()`. Este texto e para LER e COPIAR;
# a execucao de verdade continua sendo por parametro (%s), em `rh_exploracao`.
# =============================================================================

from rh_consultas import DIMENSOES, FILTROS_NUMERICOS

NIVEIS = ["Assistente", "Junior", "Pleno", "Senior", "Especialista", "Lideranca"]


def _lit(valor):
    """Literal SQL. Aspas simples viram duas -- e o escape do proprio SQL."""
    if isinstance(valor, (int, float)):
        return repr(float(valor)) if isinstance(valor, float) else str(valor)
    return "'" + str(valor).replace("'", "''") + "'"


def _condicoes(filtros):
    """Os filtros da tela como pedacos de WHERE, ja com os valores no texto.

    Mesma lista branca de `rh_exploracao._montar_filtro`: o nome da coluna e
    PROCURADO no dicionario, nunca vem do texto do usuario.
    """
    saida = []
    for f in filtros or []:
        campo = f.get("campo")
        operador = f.get("operador", "igual")
        valor = f.get("valor")
        if valor in (None, "", "todos"):
            continue
        if campo in DIMENSOES and operador == "igual":
            saida.append(f"{DIMENSOES[campo]} = {_lit(str(valor))}")
        elif campo in FILTROS_NUMERICOS and operador in ("maior_que", "menor_que"):
            sinal = ">=" if operador == "maior_que" else "<"
            saida.append(f"{FILTROS_NUMERICOS[campo]} {sinal} {_lit(float(valor))}")
        else:
            raise ValueError(f"Filtro inválido: {campo!r} / {operador!r}")
    return saida


def _where(condicoes, recuo=" "):
    """Monta o bloco WHERE alinhado. Vazio vira string vazia, e nao 'WHERE true'.

    Alinhamento importa: esta consulta vai ser LIDA numa tela, por alguem que
    quer achar em dois segundos qual e o recorte proprio do grafico.
    """
    if not condicoes:
        return ""
    corpo = ("\n" + recuo + "   AND ").join(condicoes)
    return "\n" + recuo + "WHERE " + corpo


def _custo(prem, recuo=""):
    """O custo de reposicao ESCRITO POR EXTENSO, com as premissas da tela.

    Nao usamos a coluna `custo_reposicao` do banco de proposito: ela foi gravada
    na carga com os fatores padrao, e divergiria da tela no instante em que
    alguem editasse um fator. Escrevendo a conta, a consulta acompanha a edicao.
    """
    linhas = [f"{recuo}salario_base * {prem['meses_ano']:g} * {prem['encargos']:g}",
              f"{recuo}       * CASE nivel_cargo"]
    for nivel in NIVEIS:
        fator = prem.get(f"fator_{nivel.lower()}")
        if fator is None:
            continue
        linhas.append(f"{recuo}           WHEN {_lit(nivel):<16} THEN {fator:g}")
    linhas.append(f"{recuo}         END")
    return "\n".join(linhas)


def _remuneracao(prem, recuo=""):
    return f"{recuo}salario_base * {prem['meses_ano']:g} * {prem['encargos']:g}"


# =============================================================================
# UMA FUNCAO POR GRAFICO. A assinatura e sempre (condicoes, premissas, data_base).
# =============================================================================

def _serie_ctes(cond, data_base):
    """As duas CTEs que os graficos de serie compartilham.

    ESTA E A PARTE QUE MAIS SURPREENDE QUEM LE: a planilha nao tem historico de
    quadro nenhum. O headcount de cada mes e RECONSTRUIDO aqui, cruzando cada
    colaborador com cada mes do calendario e perguntando "esta pessoa estava na
    empresa neste mes?". O CROSS JOIN e literalmente essa pergunta.
    """
    return f"""WITH recorte AS (
    SELECT *
      FROM rh.colaboradores{_where(cond, "   ")}
),
meses AS (
    -- O calendario, de jan/2024 ate a data-base. Sem esta tabela nao existe
    -- serie: a base e uma FOTO, e uma foto nao tem meses.
    SELECT date_trunc('month', g)::date                                AS primeiro,
           (date_trunc('month', g) + INTERVAL '1 month - 1 day')::date AS ultimo
      FROM generate_series(TIMESTAMP '2024-01-01',
                           date_trunc('month', DATE {_lit(data_base)}),
                           INTERVAL '1 month') AS g
),
mensal AS (
    -- CROSS JOIN: cada pessoa x cada mes. Os FILTER abaixo sao as regras de
    -- "estava na empresa" e "saiu neste mes", escritas uma unica vez.
    SELECT m.ultimo AS mes,
           count(*) FILTER (WHERE c.data_admissao < m.primeiro
                              AND (c.data_desligamento IS NULL
                                   OR c.data_desligamento >= m.primeiro))   AS hc_inicio,
           count(*) FILTER (WHERE c.data_admissao <= m.ultimo
                              AND (c.data_desligamento IS NULL
                                   OR c.data_desligamento > m.ultimo))      AS hc_fim,
           count(*) FILTER (WHERE c.data_admissao
                                  BETWEEN m.primeiro AND m.ultimo)          AS admissoes,
           count(*) FILTER (WHERE c.data_desligamento
                                  BETWEEN m.primeiro AND m.ultimo)          AS desligamentos,
           count(*) FILTER (WHERE c.data_desligamento
                                  BETWEEN m.primeiro AND m.ultimo
                              AND c.status_desligamento = 'Voluntario')     AS voluntarios,
           count(*) FILTER (WHERE c.data_desligamento
                                  BETWEEN m.primeiro AND m.ultimo
                              AND c.status_desligamento = 'Involuntario')   AS involuntarios
      FROM meses m
      CROSS JOIN recorte c
     GROUP BY m.ultimo
)"""


def _sql_evolucao(cond, prem, data_base):
    return f"""{_serie_ctes(cond, data_base)}
-- A janela movel: 12 meses de saidas sobre o quadro medio dos MESMOS 12 meses.
-- O `count(*) OVER j = 12` e o que deixa os 11 primeiros meses VAZIOS: com a
-- janela ainda aberta o numero pareceria anual sem ser.
SELECT mes,
       hc_fim                                                          AS headcount_fim,
       desligamentos,
       voluntarios,
       CASE WHEN count(*) OVER j = 12 THEN
            round(sum(desligamentos) OVER j
                  / avg((hc_inicio + hc_fim) / 2.0) OVER j * 100, 2)
       END                                                             AS turnover_12m,
       CASE WHEN count(*) OVER j = 12 THEN
            round(sum(voluntarios) OVER j
                  / avg((hc_inicio + hc_fim) / 2.0) OVER j * 100, 2)
       END                                                             AS turnover_vol_12m,
       CASE WHEN count(*) OVER j = 12 THEN
            round(sum(involuntarios) OVER j
                  / avg((hc_inicio + hc_fim) / 2.0) OVER j * 100, 2)
       END                                                             AS turnover_invol_12m
  FROM mensal
WINDOW j AS (ORDER BY mes ROWS BETWEEN 11 PRECEDING AND CURRENT ROW)
 ORDER BY mes;"""


def _sql_quadro(cond, prem, data_base):
    return f"""{_serie_ctes(cond, data_base)}
-- Aqui NAO HA DIVISAO NENHUMA: este grafico e contagem pura. E por isso ele
-- comeca em jan/2024 e nao tem buraco no inicio, ao contrario do de cima.
SELECT mes,
       hc_fim AS headcount_fim,
       admissoes,
       voluntarios,
       involuntarios
  FROM mensal
 ORDER BY mes;"""


def _sql_tempo_de_casa(cond, prem, data_base):
    return f"""-- O denominador e EXPOSICAO em FTE-ano, nao o numero de pessoas: e o que
-- impede uma faixa pequena de parecer pior so por ter pouca gente.
SELECT faixa_tempo_casa,
       count(*) FILTER (WHERE status_desligamento = 'Ativo')      AS headcount_ativo,
       round(sum(exposicao_ltm), 2)                               AS exposicao,
       count(*) FILTER (WHERE saiu_vol_ltm)                       AS desl_voluntarios,
       round(count(*) FILTER (WHERE saiu_vol_ltm)
             / sum(exposicao_ltm) * 100, 2)                       AS turnover_vol
  FROM rh.colaboradores{_where(cond)}
 GROUP BY faixa_tempo_casa
 ORDER BY faixa_tempo_casa;"""


def _sql_sobrevivencia(cond, prem, data_base):
    corte = ["data_admissao >= DATE '2024-01-01'"] + cond
    return f"""-- KAPLAN-MEIER EM SQL. O recorte proprio esta na primeira linha do WHERE:
-- so coortes de 2024 em diante, porque a base nao traz desligamento anterior.
WITH base AS (
    SELECT CASE WHEN data_desligamento IS NOT NULL
                 AND status_desligamento = 'Voluntario'
                -- quem pediu demissao: observado ate a saida (evento)
                THEN (data_desligamento - data_admissao) / 30.44
                -- ativo ou desligado pela empresa: observado ate a data-base
                ELSE (DATE {_lit(data_base)} - data_admissao) / 30.44
           END AS t,
           (data_desligamento IS NOT NULL
            AND status_desligamento = 'Voluntario') AS evento
      FROM rh.colaboradores{_where(corte, "   ")}
),
risco AS (
    SELECT m.m,
           -- CENSURA A DIREITA: quem ainda nao chegou no mes m simplesmente
           -- nao entra no denominador daquele mes.
           count(*) FILTER (WHERE b.t >= m.m - 1)                       AS em_risco,
           count(*) FILTER (WHERE b.evento
                              AND b.t >  m.m - 1
                              AND b.t <= m.m)                           AS eventos
      FROM generate_series(1, 18) AS m(m)
      CROSS JOIN base b
     GROUP BY m.m
)
-- Nao existe agregado de PRODUTO em SQL, entao o produto dos (1 - risco) sai
-- como exp(sum(ln(...))). E a mesma conta do estimador.
SELECT m AS mes_de_casa,
       em_risco,
       eventos,
       round((1 - exp(sum(ln(1 - eventos::numeric / em_risco))
                      OVER (ORDER BY m))) * 100, 2) AS risco_acumulado
  FROM risco
 WHERE em_risco >= 60          -- abaixo disso o estimador fica instavel demais
 ORDER BY m;"""


def _sql_coortes(cond, prem, data_base):
    corte = ["data_admissao >= DATE '2024-01-01'"] + cond

    def horizonte(h):
        return f"""       CASE WHEN count(*) FILTER (WHERE observado_m >= {h}) >= 20 THEN
            round(count(*) FILTER (WHERE observado_m >= {h}
                                     AND saiu_voluntario
                                     AND sobrevivencia_m <= {h})::numeric
                  / count(*) FILTER (WHERE observado_m >= {h}) * 100, 2)
       END AS saida_vol_{h}m"""

    return f"""-- O recorte proprio tem DOIS cortes: coortes de 2024 em diante (a base nao
-- traz desligamento anterior) e, dentro de cada horizonte, so quem TEVE CHANCE
-- de atingi-lo. Coorte com menos de 20 elegiveis fica NULL -- e NULL nao e zero.
WITH base AS (
    SELECT to_char(data_admissao, 'YYYY"Q"Q')                       AS coorte,
           (DATE {_lit(data_base)} - data_admissao) / 30.44         AS observado_m,
           (COALESCE(data_desligamento, DATE {_lit(data_base)})
            - data_admissao) / 30.44                                AS sobrevivencia_m,
           (data_desligamento IS NOT NULL
            AND status_desligamento = 'Voluntario')                 AS saiu_voluntario
      FROM rh.colaboradores{_where(corte, "   ")}
)
SELECT coorte,
       count(*) AS contratacoes,
{horizonte(3)},
{horizonte(6)},
{horizonte(12)}
  FROM base
 GROUP BY coorte
 ORDER BY coorte;"""


def _sql_prioridade(cond, prem, data_base):
    return f"""-- EXCESSO SOBRE O ESPERADO. A CTE `taxa` calcula a media do RECORTE ATUAL --
-- e nao a da empresa inteira: filtrando por uma area, a comparacao passa a ser
-- interna aquela area, que e o que faz sentido na tela.
WITH recorte AS (
    SELECT area,
           faixa_tempo_casa,
           exposicao_ltm,
           saiu_vol_ltm,
{_custo(prem, "           ")} AS custo_reposicao
      FROM rh.colaboradores{_where(cond, "   ")}
),
taxa AS (
    SELECT count(*) FILTER (WHERE saiu_vol_ltm) / sum(exposicao_ltm)  AS media,
           avg(custo_reposicao) FILTER (WHERE saiu_vol_ltm)           AS custo_medio
      FROM recorte
)
SELECT r.area,
       r.faixa_tempo_casa,
       count(*)                                                       AS headcount,
       round(sum(r.exposicao_ltm), 2)                                 AS exposicao,
       count(*) FILTER (WHERE r.saiu_vol_ltm)                         AS saidas_vol,
       round(count(*) FILTER (WHERE r.saiu_vol_ltm)
             / sum(r.exposicao_ltm) * 100, 2)                         AS turnover_vol,
       round(sum(r.exposicao_ltm) * t.media, 2)                       AS esperado_no_baseline,
       round(count(*) FILTER (WHERE r.saiu_vol_ltm)
             - sum(r.exposicao_ltm) * t.media, 2)                     AS excesso_saidas,
       -- O `round(..., 2)` de dentro nao e enfeite: o pandas arredonda o
       -- excesso ANTES de multiplicar pelo custo medio, e sem repetir isso
       -- aqui a consulta erraria o custo em ~0,2% nos segmentos pequenos.
       round(round(count(*) FILTER (WHERE r.saiu_vol_ltm)
                   - sum(r.exposicao_ltm) * t.media, 2)
             * t.custo_medio, 2)                                      AS custo_excesso
  FROM recorte r
 CROSS JOIN taxa t
 GROUP BY r.area, r.faixa_tempo_casa, t.media, t.custo_medio
-- O outro recorte proprio: segmento com pouca exposicao produz taxa instavel
-- (uma unica saida vira 200%). O corte fica AQUI, e nao na leitura, para
-- ninguem chegar a ver o numero sem ele.
HAVING sum(r.exposicao_ltm) >= 8
 ORDER BY excesso_saidas DESC;"""


def _sql_motivos(cond, prem, data_base):
    corte = ["saiu_vol_ltm"] + cond
    return f"""-- O recorte proprio e a primeira linha do WHERE: SO saidas voluntarias da
-- janela. As medias ao lado da contagem sao o argumento do slide -- e a
-- comparacao delas com quem ficou que mostra que o motivo declarado e gatilho.
SELECT motivo_desligamento                                        AS motivo,
       count(*)                                                   AS quantidade,
       round(count(*) * 100.0 / sum(count(*)) OVER (), 2)          AS share,
       round(avg(engajamento), 2)                                 AS engajamento_medio,
       round(avg(indice_lideranca), 2)                            AS lideranca_media,
       round(avg(compa_ratio), 3)                                 AS compa_medio,
       round(avg(tempo_empresa_anos), 2)                          AS tempo_casa_medio
  FROM rh.colaboradores{_where(corte)}
 GROUP BY motivo_desligamento
 ORDER BY quantidade DESC;"""


def _sql_fatores(cond, prem, data_base):
    corte = ["(status_desligamento = 'Ativo' OR saiu_vol_ltm)"] + cond
    return f"""-- ⚠️ ESTA E A UNICA CONSULTA QUE NAO DEVOLVE O GRAFICO PRONTO, e a diferenca
-- e honesta: ajustar uma regressao logistica nao e coisa que SQL faca. O que
-- SQL faz -- e e aqui que moram as decisoes discutiveis -- e definir O UNIVERSO
-- e escrever AS VARIAVEIS. O ajuste dos coeficientes segue em Python
-- (statsmodels.Logit), e a razao de chances e a exponencial deles.
--
-- Repare no WHERE: quem foi DESLIGADO pela empresa fica de fora. Sair por
-- decisao do gestor e outro fenomeno; misturar produz um modelo que nao
-- explica nem um nem outro.
--
-- Repare tambem no SINAL das variaveis: elas sao escritas na direcao do RISCO
-- ("engajamento 10 pontos MENOR"), para a razao de chances sair acima de 1
-- quando o fator aumenta o risco -- e o numero se ler sem inverter na cabeca.
SELECT saiu_vol_ltm::int                              AS "pediu demissao (desfecho)",
       (tempo_empresa_anos < 1)::int                  AS "Estar nos primeiros 12 meses",
       (tipo_contrato = 'Temporario')::int            AS "Contrato temporario",
       -engajamento / 10.0                            AS "Engajamento 10 pontos menor",
       -indice_lideranca / 10.0                       AS "Indice de lideranca 10 pontos menor",
       -(compa_ratio - 1) * 10                        AS "Compa-ratio 0,10 menor",
       horas_extras_mes / 5.0                         AS "Horas extras +5h por mes",
       absenteismo_dias_12m / 3.0                     AS "Absenteismo +3 dias no ano",
       horas_treinamento_12m / 10.0                   AS "Treinamento +10h no ano",
       movimentos_internos_24m                        AS "Movimento interno a mais",
       (performance = 'Abaixo do esperado')::int      AS "Performance abaixo do esperado",
       (performance = 'Acima do esperado')::int       AS "Performance acima do esperado"
  FROM rh.colaboradores{_where(corte)};"""


def _sql_custo(cond, prem, data_base):
    return f"""-- O CUSTO ESTA ESCRITO POR EXTENSO, e nao lido da coluna `custo_reposicao`
-- do banco. Os tres numeros abaixo -- meses/ano, encargos e o fator de cada
-- nivel -- sao os que estao NOS CAMPOS EDITAVEIS no topo da pagina agora.
-- Editar um deles reescreve esta consulta e muda o grafico junto.
WITH recorte AS (
    SELECT status_desligamento,
           saiu_ltm,
           saiu_vol_ltm,
           saiu_invol_ltm,
           faixa_tempo_casa,
{_remuneracao(prem, "           ")} AS remuneracao_anual,
{_custo(prem, "           ")} AS custo_reposicao
      FROM rh.colaboradores{_where(cond, "   ")}
)
SELECT round(sum(custo_reposicao) FILTER (WHERE saiu_vol_ltm))      AS custo_vol,
       round(sum(custo_reposicao) FILTER (WHERE saiu_invol_ltm))    AS custo_invol,
       round(sum(custo_reposicao) FILTER (WHERE saiu_ltm))          AS custo_total,
       -- A quarta barra e um SUBCONJUNTO das anteriores, e por isso ela nao
       -- soma com elas: dentre as saidas, as de quem tinha menos de 1 ano.
       round(sum(custo_reposicao) FILTER (WHERE saiu_ltm
             AND faixa_tempo_casa = '0-12 meses'))                  AS custo_entrada,
       round(avg(custo_reposicao) FILTER (WHERE saiu_vol_ltm))      AS custo_medio_vol,
       round(sum(remuneracao_anual)
             FILTER (WHERE status_desligamento = 'Ativo'))          AS folha_anual
  FROM recorte;"""


CONSULTAS = {
    "evolucao": _sql_evolucao,
    "quadro": _sql_quadro,
    "tempo_de_casa": _sql_tempo_de_casa,
    "sobrevivencia": _sql_sobrevivencia,
    "coortes": _sql_coortes,
    "prioridade": _sql_prioridade,
    "motivos": _sql_motivos,
    "fatores": _sql_fatores,
    "custo": _sql_custo,
}

# Uma frase por grafico dizendo o que a consulta devolve -- porque "SELECT" nao
# diz se aquilo e uma linha, uma serie ou uma matriz.
DEVOLVE = {
    "evolucao": "uma linha por mês, com a taxa anualizada da janela móvel",
    "quadro": "uma linha por mês, com quadro, admissões e saídas — contagem, sem divisão",
    "tempo_de_casa": "uma linha por faixa de tempo de casa",
    "sobrevivencia": "uma linha por mês de casa, com o risco acumulado",
    "coortes": "uma linha por trimestre de admissão, com um horizonte por coluna",
    "prioridade": "uma linha por segmento (área × faixa) que passou do corte de exposição",
    "motivos": "uma linha por motivo declarado, com o perfil médio de quem o declarou",
    "fatores": "a matriz que entra no modelo — uma linha por pessoa do universo",
    "custo": "uma linha só, com os quatro totais do gráfico",
}


def sql_do_grafico(id_grafico, filtros=None, premissas=None, data_base=None):
    """A consulta que reproduz o grafico `id_grafico` no recorte pedido.

    `premissas` e o dicionario de `Premissas.como_dict()`; `data_base` e a data
    do retrato. Ambos entram no TEXTO da consulta, e nao como parametro, porque
    esta consulta e para ler e copiar -- ela precisa rodar sozinha no psql.
    """
    montar = CONSULTAS.get(id_grafico)
    if montar is None:
        return None
    return montar(_condicoes(filtros), premissas or {}, str(data_base or "2026-06-30")[:10])
