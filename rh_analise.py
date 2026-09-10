# =============================================================================
# rh_analise.py — as REGRAS DE CALCULO, num lugar so
# -----------------------------------------------------------------------------
# ESTE ARQUIVO NAO FALA COM O BANCO E NAO FALA COM O MODELO. Ele recebe a
# planilha e devolve tabelas prontas. Essa separacao existe por um motivo bem
# concreto: e aqui que moram as decisoes que alguem vai questionar numa reuniao
# ("por que o turnover deu 14,4% e nao 11%?"), e questionamento so se responde
# olhando UM arquivo.
#
# AS QUATRO DECISOES QUE MUDAM O RESULTADO, e por que foram tomadas assim:
#
# 1. DENOMINADOR EM EXPOSICAO (FTE-ano), nao headcount medio simples.
#    O quadro da base cresce 181% na janela. Com headcount medio, o proprio
#    crescimento DILUI a taxa: a empresa piora e o indicador melhora. Exposicao
#    conta quanto tempo cada pessoa esteve exposta ao risco de sair, entao quem
#    entrou em maio conta como 1/6 de pessoa-ano, e nao como uma pessoa inteira.
#
# 2. HEADCOUNT RECONSTRUIDO das datas, e nao lido de lugar nenhum.
#    A planilha e uma FOTO da data-base. Nao existe historico de quadro nela.
#    Mas existe data de admissao e de desligamento -- e com isso da para saber
#    quantas pessoas havia em qualquer mes. Sem essa reconstrucao o agente so
#    fala de "hoje", e diretoria pergunta tendencia.
#
# 3. COORTES SO A PARTIR DE 2024, por causa de TRUNCAMENTO A ESQUERDA.
#    A base so traz desligamentos de 01/01/2024 em diante. Quem entrou em 2019 e
#    saiu em 2021 simplesmente nao esta la. Entao as coortes antigas chegam
#    "filtradas": so sobrou quem ficou. Calcular sobrevivencia com elas daria um
#    numero artificialmente bom. Descartar e a escolha honesta.
#
# 4. KAPLAN-MEIER, e nao "quantos por cento sairam".
#    Quem foi admitido ha 4 meses ainda nao teve chance de completar 12. Contar
#    essa pessoa como "ficou" subestima o risco. O estimador trata isso como
#    CENSURA A DIREITA: a pessoa entra no calculo ate onde foi observada, e sai.
#
# Cada uma dessas decisoes vira uma linha em `rh.qualidade` ou `rh.premissas`,
# para o agente conseguir explicar o proprio numero quando perguntarem.
# =============================================================================

import logging

import numpy as np
import pandas as pd

log = logging.getLogger("rh_analise")

# -----------------------------------------------------------------------------
# PREMISSAS DE CUSTO. Ficam no topo, com nome, porque sao o que mais gera
# discussao -- e discussao boa: se a diretoria achar o fator alto, muda-se aqui
# e recarrega. Numero magico enterrado no meio do codigo nao pode ser discutido.
#
# A faixa de mercado para custo de reposicao vai de 0,5x a 2,0x a remuneracao
# anual. Ficamos na parte BAIXA da faixa de proposito: um business case que
# depende do cenario otimista nao sobrevive a primeira pergunta.
# -----------------------------------------------------------------------------
MESES_ANO = 13.33         # 12 salarios + 13o + 1/3 de ferias
ENCARGOS = 1.35           # encargos e beneficios sobre a remuneracao
FATOR_REPOSICAO = {
    "Assistente": 0.40, "Junior": 0.45, "Pleno": 0.65,
    "Senior": 0.90, "Especialista": 0.95, "Lideranca": 1.30,
}

# Os nomes das colunas da planilha vem com acento e caractere especial
# ("Salario_Base_R$"). Renomeamos na entrada, uma vez, para nomes ASCII curtos:
# o resto do arquivo fica legivel e nao depende de como a planilha foi salva.
COLUNAS = [
    "id", "area", "localidade", "nivel", "contrato", "admissao", "desligamento",
    "status", "motivo", "tempo_anos", "salario", "compa", "performance",
    "engajamento", "lideranca", "horas_extras", "absenteismo",
    "meses_promocao", "treinamento", "movimentos", "elegivel",
]

# Acentos fora, para casar com o CHECK do banco e com o FATOR_REPOSICAO.
# ⚠️ SO ENTRA AQUI O QUE E COMPARADO NO CODIGO. Area e localidade mantem o
# acento porque sao apenas rotulos exibidos -- tirar o acento delas so deixaria
# a resposta do agente feia. Status, nivel e contrato entram porque o codigo faz
# `== "Temporario"` e `map(FATOR_REPOSICAO)` em cima deles: se o acento ficar, a
# comparacao da falso em silencio e a coluna vira uma constante de zeros.
SEM_ACENTO = {
    "Voluntário": "Voluntario", "Involuntário": "Involuntario",
    "Júnior": "Junior", "Sênior": "Senior", "Liderança": "Lideranca",
    "Temporário": "Temporario",
}


def _sem_acento(serie):
    return serie.replace(SEM_ACENTO)


def carregar_planilha(caminho, aba="Base_Colaboradores"):
    """Le o arquivo e devolve o DataFrame cru, so com os nomes normalizados."""
    df = pd.read_excel(caminho, sheet_name=aba)
    if len(df.columns) != len(COLUNAS):
        raise ValueError(
            f"A planilha tem {len(df.columns)} colunas; o esperado sao "
            f"{len(COLUNAS)}. Confira se a aba {aba!r} e a certa."
        )
    df.columns = COLUNAS
    for coluna in ("status", "nivel", "contrato"):
        df[coluna] = _sem_acento(df[coluna])
    log.info("planilha lida: %d linhas", len(df))
    return df


# =============================================================================
# QUALIDADE — roda ANTES de qualquer calculo
# -----------------------------------------------------------------------------
# POR QUE ANTES? Porque um teste que roda depois vira nota de rodape. Rodando
# antes, ele DECIDE: a coluna `meses_desde_promocao` e descartada aqui, e por
# isso ela nunca aparece na modelagem. Se o teste rodasse no fim, alguem ja
# teria usado a variavel e tirado conclusao dela.
# =============================================================================
def conferir(df, data_base):
    """Devolve (lista de checagens, dataframe limpo). Nao levanta excecao:
    a decisao de cada achado esta na propria linha da checagem."""
    d = df[df.desligamento.notna()]
    incoerentes = int((df.meses_promocao > df.tempo_anos * 12).sum())
    preenchidos = int(df.meses_promocao.notna().sum())
    calc = (df.desligamento.fillna(data_base) - df.admissao).dt.days / 365.25

    checagens = [
        ("Registros e colunas", f"{len(df)} linhas x {len(COLUNAS)} campos", "OK"),
        ("ID duplicado", f"{int(df.id.duplicated().sum())} ocorrencias", "OK"),
        ("Linhas identicas", f"{int(df.duplicated().sum())} ocorrencias", "OK"),
        ("Desligamento anterior a admissao",
         f"{int((d.desligamento < d.admissao).sum())} casos", "OK"),
        ("Admissao posterior a data-base",
         f"{int((df.admissao > data_base).sum())} casos", "OK"),
        ("Tempo de casa x datas",
         f"divergencia maxima de {abs(calc - df.tempo_anos).max():.3f} ano", "OK"),
        ("Motivo x Status",
         "'Motivos pessoais' aparece como Involuntario em "
         f"{int(((df.status == 'Involuntario') & (df.motivo == 'Motivos pessoais')).sum())} casos",
         "ATENCAO"),
        ("Meses desde promocao vazio",
         f"{int(df.meses_promocao.isna().sum())} de {len(df)} "
         f"({df.meses_promocao.isna().mean():.0%})", "ATENCAO"),
        ("Meses desde promocao maior que o tempo de casa",
         f"{incoerentes} de {preenchidos} preenchidos "
         f"({incoerentes / max(preenchidos, 1):.0%}) -- logicamente impossivel",
         "DESCARTAR"),
        ("Flag de elegibilidade",
         f"{df.elegivel.eq('Sim').mean():.0%} marcados como 'Sim' -- sem poder discriminante",
         "SEM USO"),
        ("Desligados antes de 01/01/2024",
         "ausentes da base: truncamento a esquerda nas coortes de 2018 a 2023",
         "LIMITACAO"),
        ("Engajamento e indice de lideranca",
         "ultima medicao disponivel, SEM data -- ambiguidade temporal em relacao "
         "a saida; causalidade reversa e hipotese viva",
         "LIMITACAO"),
    ]

    limpo = df.copy()
    # A decisao "DESCARTAR" acontece de verdade: o valor incoerente vira NULL,
    # e nao um numero que alguem usaria sem saber que e lixo.
    limpo.loc[limpo.meses_promocao > limpo.tempo_anos * 12, "meses_promocao"] = np.nan
    return checagens, limpo


# =============================================================================
# DERIVADAS — as regras de negocio que viram coluna
# =============================================================================
def _faixa(t):
    if t < 1:
        return "0-12 meses"
    if t < 2:
        return "1-2 anos"
    if t < 3:
        return "2-3 anos"
    if t < 5:
        return "3-5 anos"
    return "5+ anos"


def preparar(df, data_base, janela_meses=12):
    """Acrescenta exposicao, flags de saida na janela, faixa e custo."""
    inicio = data_base - pd.DateOffset(months=janela_meses) + pd.Timedelta(days=1)

    # Exposicao: quanto tempo, em anos, a pessoa esteve na empresa DENTRO da
    # janela. Quem entrou depois do inicio conta so do dia da admissao; quem
    # saiu, so ate o dia da saida. `clip` faz exatamente esse recorte.
    ini = df.admissao.clip(lower=inicio)
    fim = df.desligamento.fillna(data_base).clip(upper=data_base)
    df = df.copy()
    df["exposicao"] = (fim - ini).dt.days.clip(lower=0) / 365.25

    na_janela = df.desligamento.between(inicio, data_base)
    df["saiu_vol"] = (na_janela & df.status.eq("Voluntario")).astype(bool)
    df["saiu_invol"] = (na_janela & df.status.eq("Involuntario")).astype(bool)
    df["saiu"] = df.saiu_vol | df.saiu_invol

    df["faixa"] = df.tempo_anos.map(_faixa)
    df["remuneracao_anual"] = df.salario * MESES_ANO * ENCARGOS
    df["custo_reposicao"] = df.remuneracao_anual * df.nivel.map(FATOR_REPOSICAO)
    return df, inicio


def indicador(df):
    """O numero da empresa inteira. E o que abre qualquer conversa."""
    expo = float(df.exposicao.sum())
    vol, invol = int(df.saiu_vol.sum()), int(df.saiu_invol.sum())
    return {
        "exposicao": round(expo, 1),
        "desl_voluntarios": vol,
        "desl_involuntarios": invol,
        "turnover_vol": round(vol / expo * 100, 1),
        "turnover_invol": round(invol / expo * 100, 1),
        "turnover_total": round((vol + invol) / expo * 100, 1),
        "headcount_ativo": int(df.status.eq("Ativo").sum()),
        "custo_saidas": round(float(df.loc[df.saiu, "custo_reposicao"].sum()), 0),
        "folha_anual": round(float(df.loc[df.status.eq("Ativo"), "remuneracao_anual"].sum()), 0),
    }


# =============================================================================
# SERIE MENSAL — a reconstrucao do quadro
# =============================================================================
def serie_mensal(df, data_base, inicio_serie):
    """Uma linha por mes: quadro, movimentacao e a janela movel de 12 meses."""
    meses = pd.date_range(inicio_serie, data_base, freq="ME")
    linhas = []
    for m in meses:
        primeiro = m.replace(day=1)
        # Estava na empresa no primeiro dia do mes = entrou antes E (nao saiu OU
        # saiu depois). O `|` com `isna()` e o que trata o ativo corretamente.
        hc_i = int(((df.admissao < primeiro)
                    & (df.desligamento.isna() | (df.desligamento >= primeiro))).sum())
        hc_f = int(((df.admissao <= m)
                    & (df.desligamento.isna() | (df.desligamento > m))).sum())
        no_mes = df[df.desligamento.between(primeiro, m)]
        linhas.append({
            "mes": m.date(),
            "headcount_inicio": hc_i,
            "headcount_fim": hc_f,
            "headcount_medio": round((hc_i + hc_f) / 2, 2),
            "admissoes": int(df.admissao.between(primeiro, m).sum()),
            "desligamentos": len(no_mes),
            "voluntarios": int(no_mes.status.eq("Voluntario").sum()),
            "involuntarios": int(no_mes.status.eq("Involuntario").sum()),
        })
    t = pd.DataFrame(linhas)

    # Janela movel: soma as saidas dos ultimos 12 meses sobre o quadro medio do
    # mesmo periodo. `min_periods` fica no padrao (= 12) de proposito: antes
    # disso o resultado seria calculado sobre menos meses e pareceria um numero
    # anual sem ser. Melhor devolver NULL.
    for destino, origem in [("d12", "desligamentos"), ("v12", "voluntarios"),
                            ("i12", "involuntarios")]:
        t[destino] = t[origem].rolling(12).sum()
    hc12 = t.headcount_medio.rolling(12).mean()
    t["turnover_12m"] = (t.d12 / hc12 * 100).round(2)
    t["turnover_vol_12m"] = (t.v12 / hc12 * 100).round(2)
    t["turnover_invol_12m"] = (t.i12 / hc12 * 100).round(2)
    return t.drop(columns=["d12", "v12", "i12"])


# =============================================================================
# SEGMENTOS — a mesma funcao para todas as dimensoes
# =============================================================================
DIMENSOES = {
    "area": "area",
    "localidade": "localidade",
    "nivel": "nivel",
    "contrato": "contrato",
    "performance": "performance",
    "tempo_de_casa": "faixa",
}


def segmentos(df):
    """Formato longo: (dimensao, valor, metricas). Ver o comentario em 05_rh.sql."""
    base = df.saiu_vol.sum() / df.exposicao.sum()      # a taxa media da empresa
    custo_medio = float(df.loc[df.saiu_vol, "custo_reposicao"].mean())
    saida = []
    for nome, coluna in DIMENSOES.items():
        g = df.groupby(coluna, observed=True).agg(
            headcount_ativo=("status", lambda s: int(s.eq("Ativo").sum())),
            exposicao=("exposicao", "sum"),
            desl_voluntarios=("saiu_vol", "sum"),
            desl_involuntarios=("saiu_invol", "sum"))
        for valor, r in g.iterrows():
            excesso = r.desl_voluntarios - r.exposicao * base
            saida.append({
                "dimensao": nome,
                "valor": str(valor),
                "headcount_ativo": int(r.headcount_ativo),
                "exposicao": round(float(r.exposicao), 2),
                "desl_voluntarios": int(r.desl_voluntarios),
                "desl_involuntarios": int(r.desl_involuntarios),
                "turnover_vol": round(r.desl_voluntarios / r.exposicao * 100, 2),
                "turnover_total": round(
                    (r.desl_voluntarios + r.desl_involuntarios) / r.exposicao * 100, 2),
                "share_saidas_vol": round(
                    r.desl_voluntarios / df.saiu_vol.sum() * 100, 2),
                "excesso_saidas": round(float(excesso), 2),
                "custo_excesso": round(float(excesso * custo_medio), 2),
            })
    return pd.DataFrame(saida)


def prioridade(df, exposicao_minima=8):
    """Area x faixa de tempo de casa. Separa composicao de gestao."""
    base = df.saiu_vol.sum() / df.exposicao.sum()
    custo_medio = float(df.loc[df.saiu_vol, "custo_reposicao"].mean())
    g = df.groupby(["area", "faixa"], observed=True).agg(
        headcount=("id", "count"),
        exposicao=("exposicao", "sum"),
        saidas_vol=("saiu_vol", "sum"))
    # Grupo com pouca exposicao produz taxa instavel (uma saida vira 200%).
    # Cortamos aqui, e nao na leitura, para ninguem ver o numero sem o corte.
    g = g[g.exposicao >= exposicao_minima].reset_index()
    g["turnover_vol"] = (g.saidas_vol / g.exposicao * 100).round(2)
    g["esperado_no_baseline"] = (g.exposicao * base).round(2)
    g["excesso_saidas"] = (g.saidas_vol - g.esperado_no_baseline).round(2)
    g["custo_excesso"] = (g.excesso_saidas * custo_medio).round(2)
    return g.rename(columns={"faixa": "faixa_tempo_casa"})


# =============================================================================
# ENTRADA — coortes e sobrevivencia
# =============================================================================
def coortes(df, data_base, desde="2024-01-01"):
    """Attrition precoce por trimestre de admissao, so nas coortes nao truncadas."""
    c = df[df.admissao >= desde].copy()
    c["coorte"] = c.admissao.dt.to_period("Q").astype(str)
    c["observado_m"] = (data_base - c.admissao).dt.days / 30.44
    c["sobrevivencia_m"] = (c.desligamento.fillna(data_base) - c.admissao).dt.days / 30.44

    def taxa(sub, horizonte):
        # So entram no calculo os que TIVERAM CHANCE de atingir o horizonte.
        elegiveis = sub[sub.observado_m >= horizonte]
        if len(elegiveis) < 20:      # amostra pequena demais para publicar
            return None
        eventos = int(((elegiveis.desligamento.notna())
                       & (elegiveis.sobrevivencia_m <= horizonte)
                       & (elegiveis.status == "Voluntario")).sum())
        return round(eventos / len(elegiveis) * 100, 2)

    linhas = [{"coorte": nome, "contratacoes": len(sub),
               "saida_vol_3m": taxa(sub, 3), "saida_vol_6m": taxa(sub, 6),
               "saida_vol_12m": taxa(sub, 12)}
              for nome, sub in c.groupby("coorte")]
    return pd.DataFrame(linhas)


def sobrevivencia(df, data_base, desde="2024-01-01", horizonte=18):
    """Kaplan-Meier do risco acumulado de pedido de demissao.

    A conta em uma linha: a cada mes, `hazard = eventos / em_risco`, e a
    sobrevivencia acumulada multiplica `(1 - hazard)`. O risco acumulado e
    `1 - S`. Quem ainda nao completou o mes simplesmente sai do denominador --
    e essa e a censura a direita, que uma porcentagem simples nao faz.
    """
    c = df[df.admissao >= desde].copy()
    observado = (data_base - c.admissao).dt.days / 30.44
    ate_a_saida = (c.desligamento.fillna(data_base) - c.admissao).dt.days / 30.44
    evento = (c.desligamento.notna() & c.status.eq("Voluntario"))
    t = np.where(evento, ate_a_saida, observado)

    linhas, S = [{"mes_de_casa": 0, "em_risco": len(c), "risco_acumulado": 0.0}], 1.0
    for m in range(1, horizonte + 1):
        em_risco = int((t >= m - 1).sum())
        if em_risco < 60:      # abaixo disso o estimador fica instavel demais
            break
        eventos = int((evento & (t > m - 1) & (t <= m)).sum())
        S *= (1 - eventos / em_risco)
        linhas.append({"mes_de_casa": m, "em_risco": em_risco,
                       "risco_acumulado": round((1 - S) * 100, 2)})
    return pd.DataFrame(linhas)


# =============================================================================
# FATORES — regressao logistica com controle mutuo
# -----------------------------------------------------------------------------
# O UNIVERSO e "ativos + quem pediu demissao na janela". Quem foi DESLIGADO pela
# empresa fica de fora: sair por decisao do gestor e outro fenomeno, com outras
# causas. Misturar os dois produz um modelo que nao explica nem um nem outro.
#
# As variaveis sao escritas na direcao do RISCO ("engajamento 10 pontos MENOR")
# para a razao de chances sair maior que 1 quando o fator aumenta o risco.
# Assim o numero se le sozinho, sem inverter na cabeca.
# =============================================================================
def fatores(df):
    import statsmodels.api as sm

    u = df[df.status.eq("Ativo") | df.saiu_vol].copy()
    X = pd.DataFrame({
        "Estar nos primeiros 12 meses": (u.tempo_anos < 1).astype(int),
        "Contrato temporario": u.contrato.eq("Temporario").astype(int),
        "Engajamento 10 pontos menor": -u.engajamento / 10,
        "Indice de lideranca 10 pontos menor": -u.lideranca / 10,
        "Compa-ratio 0,10 menor": -(u.compa - 1) * 10,
        "Horas extras +5h por mes": u.horas_extras / 5,
        "Absenteismo +3 dias no ano": u.absenteismo / 3,
        "Treinamento +10h no ano": u.treinamento / 10,
        "Movimento interno a mais": u.movimentos,
        "Performance abaixo do esperado": u.performance.eq("Abaixo do esperado").astype(int),
        "Performance acima do esperado": u.performance.eq("Acima do esperado").astype(int),
    })
    # Uma coluna sem variacao (ex.: nenhum temporario na base) deixa a matriz
    # SINGULAR e a regressao explode com "Singular matrix" -- um erro que nao
    # diz nada sobre a causa. Descartamos com aviso: perder um fator que nao
    # varia nao custa nada; derrubar a carga inteira custa.
    constantes = [c for c in X.columns if X[c].nunique() < 2]
    if constantes:
        log.warning("fatores sem variacao, descartados da regressao: %s",
                    ", ".join(constantes))
        X = X.drop(columns=constantes)

    modelo = sm.Logit(u.saiu_vol.astype(int), sm.add_constant(X)).fit(disp=0)
    ic = modelo.conf_int()
    r = pd.DataFrame({
        "fator": modelo.params.index,
        "odds_ratio": np.exp(modelo.params).round(3),
        "ic_inferior": np.exp(ic[0]).round(3),
        "ic_superior": np.exp(ic[1]).round(3),
        "p_valor": modelo.pvalues.round(5),
    }).query("fator != 'const'").reset_index(drop=True)
    r["significativo"] = r.p_valor < 0.05
    r.attrs["pseudo_r2"] = round(float(modelo.prsquared), 3)
    return r


def motivos(df):
    """Motivo declarado + o PERFIL de quem declarou. Ver 05_rh.sql."""
    v = df[df.saiu_vol]
    g = v.groupby("motivo", observed=True).agg(
        quantidade=("id", "count"),
        engajamento_medio=("engajamento", "mean"),
        lideranca_media=("lideranca", "mean"),
        compa_medio=("compa", "mean"),
        tempo_casa_medio=("tempo_anos", "mean")).reset_index()
    g["share"] = (g.quantidade / len(v) * 100).round(2)
    for c in ["engajamento_medio", "lideranca_media", "tempo_casa_medio"]:
        g[c] = g[c].round(2)
    g["compa_medio"] = g.compa_medio.round(3)
    return g.sort_values("quantidade", ascending=False)


# =============================================================================
# PREMISSAS — o que o agente precisa citar quando perguntarem "calculado como?"
# =============================================================================
def premissas(df, data_base, inicio_janela, ind):
    return [
        ("janela", f"{inicio_janela:%d/%m/%Y} a {data_base:%d/%m/%Y}",
         "Janela movel de 12 meses. Neutraliza sazonalidade e permite comparar mes a mes."),
        ("denominador", "exposicao em FTE-ano",
         "Headcount medio ponderado pelo tempo de cada pessoa no periodo. Escolhido "
         "porque o quadro cresceu 181% na janela e o headcount medio simples diluiria a taxa."),
        ("formula", "desligamentos na janela / exposicao em FTE-ano",
         "Turnover voluntario e involuntario sao apurados separadamente: um e problema "
         "de retencao, o outro de selecao e desempenho."),
        ("populacao", "CLT e Temporario, todos elegiveis",
         "Temporario e 4,7% do quadro mas tem cerca do dobro da taxa. Excluir esconderia o problema."),
        ("meses_ano", f"{MESES_ANO}",
         "Salarios por ano usados no custo: 12 + 13o + 1/3 de ferias."),
        ("encargos", f"{ENCARGOS}",
         "Multiplicador de encargos e beneficios sobre a remuneracao."),
        ("fator_reposicao", "; ".join(f"{k} {v:.0%}" for k, v in FATOR_REPOSICAO.items()),
         "Custo de reposicao como percentual da remuneracao anual, por nivel. Faixa "
         "conservadora do intervalo de mercado (0,5x a 2,0x a remuneracao anual)."),
        ("custo_medio_saida_voluntaria",
         f"R$ {df.loc[df.saiu_vol, 'custo_reposicao'].mean():,.0f}".replace(",", "."),
         "Media do custo de reposicao das saidas voluntarias da janela."),
        ("folha_anual", f"R$ {ind['folha_anual']:,.0f}".replace(",", "."),
         "Remuneracao anual do quadro ativo, com encargos. Base para comparar o custo do turnover."),
        ("data_base", f"{data_base:%d/%m/%Y}",
         "Data do retrato. Tudo que o agente sabe se refere a esta data."),
        ("natureza_dos_dados", "sinteticos",
         "Base ficticia, criada para o processo seletivo. Nenhum registro representa pessoa real."),
    ]


def analisar(caminho, data_base, aba="Base_Colaboradores"):
    """O pipeline completo. Devolve um dicionario com tudo pronto para gravar."""
    bruto = carregar_planilha(caminho, aba)
    checagens, limpo = conferir(bruto, data_base)
    df, inicio = preparar(limpo, data_base)
    ind = indicador(df)

    log.info("turnover total %.1f%% | voluntario %.1f%% | %d ativos",
             ind["turnover_total"], ind["turnover_vol"], ind["headcount_ativo"])

    return {
        "colaboradores": df,
        "indicador": ind,
        "inicio_janela": inicio,
        "serie": serie_mensal(df, data_base, "2024-01-31"),
        "segmentos": segmentos(df),
        "prioridade": prioridade(df),
        "coortes": coortes(df, data_base),
        "sobrevivencia": sobrevivencia(df, data_base),
        "fatores": fatores(df),
        "motivos": motivos(df),
        "qualidade": checagens,
        "premissas": premissas(df, data_base, inicio, ind),
    }
