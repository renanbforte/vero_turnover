# =============================================================================
# tools/rh.py — as ferramentas do agente de RH
# -----------------------------------------------------------------------------
# COMO ESTE ARQUIVO ESTA ORGANIZADO, e por que assim:
#
# Cada ferramenta faz UMA pergunta ao `rh_consultas.py` e formata a resposta em
# TEXTO. Nenhuma monta SQL, nenhuma abre conexao, nenhuma calcula metrica. Isso
# nao e purismo: e o que garante que o numero que o agente diz numa pergunta
# seja o mesmo de outra pergunta parecida. Formula em dois lugares vira duas
# formulas diferentes no dia em que alguem mexer em uma so.
#
# POR QUE AS FERRAMENTAS DEVOLVEM TEXTO E NAO JSON?
# Porque o consumidor e um modelo de linguagem, e o texto ja carrega a leitura
# junto. Uma tabela com a linha "media da empresa" no rodape faz o modelo
# comparar sem precisar ser instruido. Um JSON com os mesmos numeros deixa a
# comparacao por conta dele -- e ele as vezes esquece.
#
# ⚠️ A AUTORIZACAO FICA AQUI, E FORA DO ALCANCE DO MODELO.
# `identidade_atual()` vem do nucleo (ver identidade.py): o modelo nao consegue
# escolher nem alterar esse valor. Se a identidade nao estiver na lista de
# perfis autorizados, a ferramenta recusa ANTES de tocar no banco. Nao adianta
# pedir bonito, nao adianta "faz de conta que sou diretor": nao ha caminho.
# =============================================================================

import logging
import os

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from identidade import identidade_atual
from rh_insights import OWNER_RH

log = logging.getLogger("tools.rh")

# Injetados pelo nucleo na subida, junto com o RAG. Ver `nucleo.abrir_agente`.
_rh = None
_rag = None


def definir_rh(consultas, rag=None):
    global _rh, _rag
    _rh = consultas
    if rag is not None:
        _rag = rag


def _autorizados():
    """Quem pode consultar dados de RH. Vazio = ninguem (falha FECHANDO).

    Falhar fechando e a mesma decisao do Passo 08 do guia: uma variavel
    esquecida no .env deve BLOQUEAR, nunca liberar. O contrario -- "se ninguem
    configurou, deixa todo mundo" -- e como a maior parte dos vazamentos
    comeca.
    """
    bruto = os.environ.get("RH_PERFIS_AUTORIZADOS", "")
    return {p.strip().lower() for p in bruto.split(",") if p.strip()}


def _checar_acesso():
    """Devolve None se pode; devolve a mensagem de recusa se nao pode.

    ⚠️ A ORDEM DAS CHECAGENS NAO E ARBITRARIA. A autorizacao vem ANTES da
    disponibilidade da base, por duas razoes:

      1. Quem nao esta autorizado nao deve nem descobrir se a base existe ou se
         ela esta no ar. "Voce nao tem acesso" e "a base esta fora" sao respostas
         diferentes, e a segunda ja e informacao sobre a infraestrutura.
      2. Checagem de seguranca nao pode depender de estado de infraestrutura. Na
         ordem inversa, um banco fora do ar faria a autorizacao ser PULADA -- e
         no dia em que alguem trocasse a mensagem de erro por um caminho
         alternativo, a trava simplesmente nao existiria.
    """
    quem = identidade_atual()
    permitidos = _autorizados()
    if not permitidos:
        return (
            "Nenhum perfil esta autorizado a consultar dados de RH. Peca ao "
            "administrador para preencher RH_PERFIS_AUTORIZADOS no .env."
        )
    if quem not in permitidos:
        log.warning("acesso negado a dados de RH (identidade=%s)", quem)
        return (
            "Voce nao tem autorizacao para consultar os dados de RH. "
            "O acesso e restrito e concedido pelo administrador."
        )
    if _rh is None:
        return "A base de RH nao esta disponivel agora."
    return None


# -----------------------------------------------------------------------------
# Formatacao. Texto compacto, numeros em padrao brasileiro.
# -----------------------------------------------------------------------------
def _n(v, casas=1):
    if v is None:
        return "-"
    return f"{float(v):,.{casas}f}".translate(str.maketrans({",": ".", ".": ","}))


def _tabela(linhas, colunas, titulo=None):
    """Tabela em texto de largura fixa. Legivel para o modelo e para quem depura."""
    if not linhas:
        return "(sem dados)"
    cabecalho = [c[1] for c in colunas]
    corpo = [[("-" if l.get(c[0]) is None else
               (_n(l[c[0]], c[2]) if len(c) > 2 else str(l[c[0]])))
              for c in colunas] for l in linhas]
    larguras = [max(len(cabecalho[i]), *(len(li[i]) for li in corpo))
                for i in range(len(colunas))]
    def linha(vals):
        return "  ".join(v.ljust(larguras[i]) if i == 0 else v.rjust(larguras[i])
                         for i, v in enumerate(vals))
    partes = [titulo] if titulo else []
    partes += [linha(cabecalho), "  ".join("-" * w for w in larguras)]
    partes += [linha(li) for li in corpo]
    return "\n".join(partes)


# =============================================================================
# 1. PANORAMA — a pergunta "como estamos?"
# =============================================================================
@tool("panorama_turnover")
async def panorama_turnover() -> str:
    """Devolve o retrato geral do turnover da empresa: taxa total e voluntaria dos
    ultimos 12 meses, quadro ativo, custo estimado das saidas e a tendencia mes a mes.

    Use SEMPRE que a pergunta for aberta sobre a situacao da empresa ("como
    estamos?", "qual a situacao do turnover?", "esta melhorando ou piorando?"),
    e use como PRIMEIRA consulta antes de aprofundar em qualquer recorte -- o
    numero da empresa e a referencia contra a qual todo segmento e comparado.
    """
    if (recusa := _checar_acesso()):
        return recusa
    dados = await _rh.panorama()
    ind, serie, prem = dados["indicador"], dados["serie"], dados["premissas"]

    recentes = serie[-6:]
    tendencia = _tabela(
        recentes,
        [("competencia", "mes"), ("headcount_fim", "quadro", 0),
         ("admissoes", "admissoes", 0), ("voluntarios", "pedidos demissao", 0),
         ("involuntarios", "desligamentos", 0), ("turnover_12m", "turnover 12m %", 1),
         ("turnover_vol_12m", "voluntario 12m %", 1)],
        "Ultimos 6 meses:")
    primeiro = serie[0] if serie else None
    evolucao = ""
    if primeiro:
        evolucao = (f"\nInicio da serie ({primeiro['competencia']}): turnover total "
                    f"{_n(primeiro['turnover_12m'])}%, voluntario "
                    f"{_n(primeiro['turnover_vol_12m'])}%.")

    return f"""PANORAMA DO TURNOVER  (janela: {prem.get('janela', 'ultimos 12 meses')})

Turnover total .......... {_n(ind['turnover_total'])}%
Turnover voluntario ..... {_n(ind['turnover_vol'])}%   ({ind['desl_voluntarios']} pedidos de demissao)
Turnover involuntario ... {_n(float(ind['turnover_total']) - float(ind['turnover_vol']))}%   ({ind['desl_involuntarios']} desligamentos pela empresa)
Quadro ativo ............ {ind['headcount_ativo']} pessoas
Em rampa (< 1 ano) ...... {ind['ativos_em_rampa']} pessoas
Exposicao (denominador) . {_n(ind['exposicao_fte_ano'])} FTE-ano
Custo estimado das saidas R$ {_n(ind['custo_saidas_12m'], 0)}   (folha anual: {prem.get('folha_anual', '-')})

{tendencia}{evolucao}

Denominador: {prem.get('denominador', 'exposicao em FTE-ano')}. Data-base: {prem.get('data_base', '-')}.
Atencao ao interpretar a tendencia: a janela movel de 12 meses suaviza; os meses
mais recentes da tabela mostram o movimento real melhor que o indicador fechado."""


# =============================================================================
# 2. SEGMENTOS
# =============================================================================
class PorSegmento(BaseModel):
    dimensao: str = Field(
        description=(
            "Como recortar. Valores aceitos: 'area', 'localidade', 'nivel', "
            "'contrato', 'performance', 'tempo_de_casa'. Se a pergunta nao "
            "disser qual, comece por 'tempo_de_casa': e o recorte que mais "
            "explica turnover nesta base."
        ),
    )


@tool("turnover_por_segmento", args_schema=PorSegmento)
async def turnover_por_segmento(dimensao: str) -> str:
    """Compara o turnover entre os grupos de uma dimensao (area, localidade, nivel
    de cargo, tipo de contrato, performance ou tempo de casa), com quantas saidas
    cada grupo teve, quanto isso representa do total e quantas saidas ele teve
    ACIMA do esperado se tivesse a taxa media da empresa.

    Use quando a pergunta comparar grupos ("qual area perde mais gente?",
    "como esta cada localidade?", "o pessoal senior sai mais?").

    A coluna 'excesso' e a mais util para decidir onde agir: taxa alta em grupo
    pequeno nao move o resultado da empresa; excesso alto move.
    """
    if (recusa := _checar_acesso()):
        return recusa
    try:
        linhas = await _rh.segmento(dimensao)
    except ValueError as e:
        return str(e)
    media = await _rh.panorama()
    return (
        _tabela(linhas,
                [("valor", dimensao), ("headcount_ativo", "ativos", 0),
                 ("desl_voluntarios", "pedidos demissao", 0),
                 ("turnover_vol", "turnover vol %", 1),
                 ("share_saidas_vol", "% das saidas", 1),
                 ("excesso_saidas", "excesso", 1),
                 ("custo_excesso", "custo do excesso R$", 0)],
                f"TURNOVER VOLUNTARIO POR {dimensao.upper()}")
        + f"\n\nMedia da empresa: {_n(media['indicador']['turnover_vol'])}%."
        + "\nRessalva: grupos que mais contrataram tem mais gente na faixa de risco "
          "por composicao. Antes de concluir sobre gestao, cruze com tempo de casa "
          "(ferramenta onde_agir_primeiro)."
    )


# =============================================================================
# 3. A CONSULTA FLEXIVEL
# =============================================================================
class Filtro(BaseModel):
    campo: str = Field(description="area, localidade, nivel, contrato, performance, "
                                   "tempo_de_casa, situacao, engajamento, lideranca, "
                                   "compa_ratio, horas_extras, salario ou tempo_anos")
    operador: str = Field(default="igual",
                          description="'igual' para texto; 'maior_que' ou 'menor_que' "
                                      "para numero")
    valor: str = Field(description="O valor a comparar. Use o nome exato que aparece "
                                   "nas tabelas (ex.: 'Atendimento', '0-12 meses').")


class Explorar(BaseModel):
    dimensoes: list[str] = Field(
        description="Uma ou duas dimensoes para agrupar: area, localidade, nivel, "
                    "contrato, performance, tempo_de_casa, situacao. Duas e o maximo.",
    )
    filtros: list[Filtro] = Field(
        default_factory=list,
        description="Filtros opcionais. Ex.: [{campo:'area', valor:'Comercial'}].",
    )


@tool("explorar_base_rh", args_schema=Explorar)
async def explorar_base_rh(dimensoes: list[str], filtros: list = None) -> str:
    """Cruza a base de RH por ate duas dimensoes, com filtros, e devolve
    AGREGADOS: quantas pessoas, quantas saidas, turnover, engajamento medio,
    indice de lideranca medio, compa-ratio medio, horas extras e salario medio.

    Use quando a pergunta combinar recortes que as outras ferramentas nao cobrem
    ("como esta o Comercial em Sao Paulo?", "o pessoal de campo com menos de um
    ano faz muita hora extra?", "onde o compa-ratio esta mais baixo?").

    NUNCA devolve pessoas: so grupos, e apenas grupos com 5 pessoas ou mais.
    Grupos menores sao suprimidos e a ferramenta avisa quantos foram. Isso e
    proposital -- media de grupo minusculo e dado individual disfarcado.
    """
    if (recusa := _checar_acesso()):
        return recusa
    limpos = [f if isinstance(f, dict) else f.model_dump() for f in (filtros or [])]
    try:
        dados = await _rh.cubo(dimensoes, limpos)
    except ValueError as e:
        return str(e)

    if not dados["linhas"]:
        return ("Nenhum grupo com 5 pessoas ou mais atende a esses filtros. "
                "Tente um recorte menos especifico.")

    colunas = [(d, d) for d in dimensoes] + [
        ("pessoas", "pessoas", 0), ("headcount_ativo", "ativos", 0),
        ("saidas_voluntarias", "pedidos demissao", 0),
        ("turnover_vol", "turnover vol %", 1),
        ("engajamento_medio", "engajamento", 1),
        ("lideranca_media", "lideranca", 1),
        ("compa_medio", "compa", 2),
        ("horas_extras_medias", "horas extras", 1),
    ]
    # O nome da dimensao na saida do banco e o da coluna real; renomeamos para o
    # nome que o usuario reconhece, para o modelo citar do mesmo jeito que leu.
    from rh_consultas import DIMENSOES
    linhas = []
    for l in dados["linhas"]:
        nova = dict(l)
        for d in dimensoes:
            nova[d] = l.get(DIMENSOES[d])
        linhas.append(nova)

    aviso = ""
    s = dados["suprimidos"]
    if s and s.get("grupos"):
        aviso = (f"\n\n{s['grupos']} grupo(s), somando {s['pessoas']} pessoa(s), "
                 "foram suprimidos por terem menos de 5 pessoas. Os totais desta "
                 "tabela nao incluem essas pessoas.")
    return _tabela(linhas, colunas, "RECORTE SOLICITADO") + aviso


# =============================================================================
# 4. ONDE AGIR PRIMEIRO
# =============================================================================
@tool("onde_agir_primeiro")
async def onde_agir_primeiro() -> str:
    """Devolve os segmentos (area cruzada com tempo de casa) ordenados por quantas
    saidas voluntarias tiveram ACIMA do esperado, com o custo evitavel de cada um.

    Use quando a pergunta for sobre PRIORIZACAO: "por onde comecar?", "onde
    focar?", "qual area precisa de atencao?", "onde esta o maior problema?".

    O cruzamento area x tempo de casa e o que separa efeito de COMPOSICAO (a area
    contratou muita gente nova) de efeito de GESTAO (a area perde gente em todas
    as faixas). Uma dimensao sozinha nao consegue distinguir os dois.
    """
    if (recusa := _checar_acesso()):
        return recusa
    linhas = await _rh.prioridade(limite=8)
    positivos = [l for l in linhas if float(l["excesso_saidas"]) > 0]
    total = sum(float(l["excesso_saidas"]) for l in positivos) or 1
    topo = sum(float(l["excesso_saidas"]) for l in positivos[:3])
    return (
        _tabela(linhas,
                [("area", "area"), ("faixa_tempo_casa", "tempo de casa"),
                 ("headcount", "pessoas", 0), ("saidas_vol", "saidas", 0),
                 ("turnover_vol", "turnover vol %", 1),
                 ("esperado_no_baseline", "esperado", 1),
                 ("excesso_saidas", "excesso", 1),
                 ("custo_excesso", "custo evitavel R$", 0)],
                "SEGMENTOS ORDENADOS POR EXCESSO DE SAIDAS")
        + f"\n\nOs 3 primeiros concentram {topo / total * 100:.0f}% de todo o excesso, "
          f"somando {sum(int(l['headcount']) for l in positivos[:3])} pessoas."
        + "\n\n'Excesso' = saidas observadas menos as que o segmento teria com a taxa "
          "media da empresa. Ranquear por taxa favoreceria grupo pequeno; por volume, "
          "favoreceria grupo grande. O excesso cruza os dois."
    )


# =============================================================================
# 5. A ENTRADA
# =============================================================================
@tool("analise_da_entrada")
async def analise_da_entrada() -> str:
    """Devolve a analise das pessoas recem-contratadas: o risco acumulado de pedido
    de demissao mes a mes desde a admissao (Kaplan-Meier) e a saida precoce de cada
    coorte trimestral de admissao.

    Use quando a pergunta for sobre gente nova, onboarding, integracao,
    experiencia de entrada, contratacoes recentes, ou quando quiser saber se a
    situacao esta piorando ("as contratacoes recentes estao aguentando?",
    "quanto tempo a pessoa fica?", "de cada 100 que entram, quantos saem?").

    Compare as coortes entre si: uma coorte muito fora do padrao das anteriores e
    sinal de que algo mudou no processo de entrada, e vale investigar.
    """
    if (recusa := _checar_acesso()):
        return recusa
    dados = await _rh.entrada()
    km = _tabela(dados["sobrevivencia"],
                 [("mes_de_casa", "mes de casa", 0), ("em_risco", "observados", 0),
                  ("risco_acumulado", "ja pediram demissao %", 1)],
                 "RISCO ACUMULADO DESDE A ADMISSAO (Kaplan-Meier)")
    co = _tabela(dados["coortes"],
                 [("coorte", "coorte"), ("contratacoes", "contratados", 0),
                  ("saida_vol_3m", "sairam ate 3m %", 1),
                  ("saida_vol_6m", "sairam ate 6m %", 1),
                  ("saida_vol_12m", "sairam ate 12m %", 1)],
                 "SAIDA PRECOCE POR COORTE DE ADMISSAO")
    return (
        f"{km}\n\n{co}\n\n"
        "Metodo: Kaplan-Meier com censura a direita -- quem foi admitido ha poucos "
        "meses nao conta como 'ficou', sai do calculo naquele horizonte. As celulas "
        "vazias sao coortes que ainda nao tiveram tempo de ser observadas naquele "
        "prazo; le-las como zero seria erro. So entram coortes admitidas a partir de "
        "2024, porque a base nao traz desligamentos anteriores e as coortes mais "
        "antigas chegariam truncadas."
    )


# =============================================================================
# 6. POR QUE SAEM
# =============================================================================
@tool("explicar_saidas")
async def explicar_saidas() -> str:
    """Devolve os motivos declarados nos pedidos de demissao com o PERFIL de quem
    declarou cada um, e os fatores estatisticamente associados a saida voluntaria
    (regressao logistica com razao de chances e intervalo de confianca).

    Use quando a pergunta for sobre CAUSA: "por que as pessoas estao saindo?",
    "e salario?", "e a lideranca?", "o que faz alguem pedir demissao?".

    Ao responder, NUNCA apresente o motivo declarado como causa: compare o perfil
    de quem declarou com a referencia de quem ficou -- e ai que esta a leitura
    util. E cite o pseudo-R2: ele diz o quanto os fatores medidos realmente
    explicam, e costuma ser baixo.
    """
    if (recusa := _checar_acesso()):
        return recusa
    m = await _rh.motivos()
    f = await _rh.fatores()
    ref = m["referencia_ativos"]
    sig = [x for x in f["fatores"] if x["significativo"]]

    return (
        _tabela(m["motivos"],
                [("motivo", "motivo declarado"), ("quantidade", "saidas", 0),
                 ("share", "% das saidas", 1),
                 ("engajamento_medio", "engajamento", 1),
                 ("lideranca_media", "lideranca", 1),
                 ("compa_medio", "compa", 2)],
                "MOTIVOS DECLARADOS")
        + f"\n\nReferencia de quem FICOU (ativos): engajamento {_n(ref['engajamento_medio'])}, "
          f"lideranca {_n(ref['lideranca_media'])}, compa {_n(ref['compa_medio'], 2)}.\n\n"
        + _tabela(sig,
                  [("fator", "fator"), ("odds_ratio", "razao de chances", 2),
                   ("ic_inferior", "IC 95% de", 2), ("ic_superior", "ate", 2)],
                  "FATORES ASSOCIADOS (significativos a 5%)")
        + f"\n\nPseudo-R2 do modelo: {f['pseudo_r2']}.\n"
          "⚠️ Limites que a resposta deve carregar: isto e ASSOCIACAO, nao causa. "
          "Engajamento e indice de lideranca sao a ultima medicao disponivel, sem "
          "data -- podem ter caido depois de a pessoa decidir sair (causalidade "
          "reversa). Um pseudo-R2 baixo significa que os fatores medidos explicam "
          "pouco do fenomeno: servem para priorizar atencao e formular hipoteses, "
          "nao para afirmar causa."
    )


# =============================================================================
# 7. A MEMORIA SEMANTICA
# =============================================================================
class BuscaAnalise(BaseModel):
    consulta: str = Field(
        description=(
            "O que procurar, em frase natural e com os termos do usuario. A busca "
            "e por SIGNIFICADO, entao uma frase completa funciona melhor que "
            "palavra-chave solta. Se a primeira busca nao trouxer o que precisa, "
            "reformule com outras palavras."
        ),
    )


@tool("buscar_analise_rh", args_schema=BuscaAnalise)
async def buscar_analise_rh(consulta: str) -> str:
    """Procura na analise estrategica ja feita sobre esta base: interpretacoes,
    implicacoes, ressalvas metodologicas, limites da base, recomendacoes de acao e
    o raciocinio que liga os numeros.

    Use SEMPRE que a pergunta pedir INTERPRETACAO e nao so numero: "o que isso
    significa?", "o que voce recomenda?", "por que isso importa?", "isso e grave?",
    "qual sua leitura?", "o que dizer para o conselho?".

    Use TAMBEM antes de dar uma recomendacao: a analise ja registrou as frentes
    propostas, as hipoteses de cada uma e como testa-las. Recomendar sem consultar
    aqui e recomendar de memoria.

    Combine com as ferramentas numericas: o numero vem do banco, a leitura vem
    daqui. Resposta boa tem os dois.
    """
    if (recusa := _checar_acesso()):
        return recusa
    if _rag is None:
        return "A busca na analise nao esta disponivel agora."

    achados = await _rag.buscar(OWNER_RH, consulta, k=4)
    log.info("busca na analise %r -> %d trecho(s)", consulta[:50], len(achados))
    if not achados:
        return (
            f"Nada encontrado na analise para {consulta!r}. O tema provavelmente "
            "nao foi analisado. Diga isso em vez de deduzir uma resposta."
        )
    partes = []
    for a in achados:
        meta = a.get("metadata") or {}
        partes.append(f"[fonte: {a['fonte']} · {meta.get('titulo', '')}]\n{a['trecho']}")
    return "\n\n---\n\n".join(partes)


# =============================================================================
# 8. CENARIO
# =============================================================================
class Cenario(BaseModel):
    reducao_pct: float = Field(
        description="Reducao pretendida nas saidas voluntarias da faixa, em "
                    "porcentagem. Ex.: 30 para uma meta de -30%.",
    )
    faixa: str = Field(
        default="0-12 meses",
        description="Faixa de tempo de casa a atacar: '0-12 meses', '1-2 anos', "
                    "'2-3 anos', '3-5 anos' ou '5+ anos'. O padrao e a faixa de "
                    "entrada, onde esta a concentracao do problema.",
    )


@tool("simular_cenario_retencao", args_schema=Cenario)
async def simular_cenario_retencao(reducao_pct: float, faixa: str = "0-12 meses") -> str:
    """Calcula quanto vale, em reais e em pontos de turnover, reduzir uma
    porcentagem das saidas voluntarias de uma faixa de tempo de casa.

    Use quando a pergunta envolver meta, retorno, quanto se economiza ou se vale
    a pena investir ("se reduzirmos 30%, quanto poupamos?", "qual a meta
    razoavel?", "isso se paga?").

    Ao responder, apresente SEMPRE as premissas junto do numero e diga o que o
    calculo NAO inclui. Numero de economia sem premissa ao lado vira promessa.
    """
    if (recusa := _checar_acesso()):
        return recusa
    try:
        c = await _rh.cenario(reducao_pct, faixa)
    except ValueError as e:
        return str(e)
    return f"""CENARIO: reduzir {_n(c['reducao_pct'], 0)}% das saidas voluntarias na faixa "{c['faixa']}"

Saidas voluntarias na faixa (12 meses) . {c['saidas_na_faixa']}
Saidas evitadas ........................ {_n(c['saidas_evitadas'])}
Custo medio por saida .................. R$ {_n(c['custo_medio_por_saida'], 0)}
Economia estimada em 12 meses .......... R$ {_n(c['economia_12m'], 0)}
Turnover voluntario hoje ............... {_n(c['turnover_vol_hoje'])}%
Turnover voluntario projetado .......... {_n(c['turnover_vol_projetado'])}%

Premissas: economia = saidas evitadas x custo medio de reposicao da faixa. O custo
de reposicao usa a parte BAIXA da faixa de mercado (0,5x a 2,0x a remuneracao anual).
NAO inclui perda de receita, retrabalho, custo da vaga aberta nem sobrecarga de quem
fica -- o numero e conservador de proposito. Tambem nao inclui o custo do programa
que produziria a reducao: para chegar a um retorno liquido, subtraia o investimento."""


# =============================================================================
# 9. COMO FOI CALCULADO
# =============================================================================
@tool("premissas_e_limites")
async def premissas_e_limites() -> str:
    """Devolve as premissas de calculo (janela, denominador, formula, fatores de
    custo) e os testes de qualidade da base, incluindo o que foi descartado e o
    que a base NAO consegue responder.

    Use quando perguntarem como um numero foi calculado, se ele e confiavel, de
    onde vem, ou quando a pergunta cair fora do que os dados sustentam -- para
    dizer com precisao o que falta em vez de responder vagamente.
    """
    if (recusa := _checar_acesso()):
        return recusa
    prem = await _rh.premissas()
    qual = await _rh.qualidade()
    return (
        "PREMISSAS DE CALCULO\n"
        + "\n".join(f"- {p['chave']}: {p['valor']}\n    {p['descricao']}" for p in prem)
        + "\n\nQUALIDADE DA BASE\n"
        + "\n".join(f"- [{q['decisao']}] {q['teste']}: {q['resultado']}" for q in qual)
        + "\n\nUse isto para responder 'como voce calculou?' com precisao, e para "
          "dizer claramente quando a pergunta cai fora do que a base sustenta."
    )


# A lista que o chat de diretoria usa. Fica aqui, e nao em tools/__init__.py,
# porque este e um conjunto FECHADO: o agente de diretoria nao deve ter CEP,
# cotacao nem agenda. Ferramenta a mais e contexto gasto em toda mensagem e uma
# chance a mais de o modelo escolher a errada.
TOOLS_RH = [
    panorama_turnover,
    turnover_por_segmento,
    onde_agir_primeiro,
    analise_da_entrada,
    explicar_saidas,
    explorar_base_rh,
    buscar_analise_rh,
    simular_cenario_retencao,
    premissas_e_limites,
]
