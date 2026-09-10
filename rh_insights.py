# =============================================================================
# rh_insights.py — a MEMORIA SEMANTICA: o que os numeros QUEREM DIZER
# -----------------------------------------------------------------------------
# POR QUE ISTO EXISTE, se o agente ja consulta o banco?
#
# Porque tabela responde "quanto", e diretor pergunta "e dai?".
#
#   Pergunta:  "como esta o Atendimento?"
#   Só banco:  "turnover voluntario de 14,5%, 29 saidas, 210 ativos."
#   Com isto:  "14,5%, o mais alto da empresa -- mas antes de culpar a gestao:
#              25,7% da area entrou ha menos de um ano, e e nessa faixa que
#              esta o problema. Dentro dela a taxa e 54%."
#
# A segunda resposta exige uma coisa que nao esta em nenhuma celula: o
# RACIOCINIO que liga os numeros. Esse raciocinio vira TEXTO aqui, o texto vira
# vetor, e o vetor e recuperado por SIGNIFICADO -- entao "o pessoal do call
# center esta saindo muito?" acha o cartao do Atendimento sem que a palavra
# "Atendimento" apareça na pergunta. E isso que o SQL nao faz.
#
# DE ONDE VEM O CONTEUDO: dos proprios dados carregados. Nenhum cartao e escrito
# a mao com numero fixo. Recarregou a base, os cartoes mudam junto. Um cartao
# escrito a mao envelhece em silencio e vira a pior coisa que um agente pode
# ter: uma fonte confiante e desatualizada.
#
# COMO O CARTAO E ESCRITO: numero -> metodo -> ressalva -> implicacao. Sempre
# nessa ordem, e sempre com a ressalva ANTES da implicacao. Assim, se o modelo
# usar so o comeco do trecho, ele ainda leva o numero e a ressalva junto.
# =============================================================================

import logging

from langchain_core.documents import Document

from rh_analise import ENCARGOS, FATOR_REPOSICAO, MESES_ANO

log = logging.getLogger("rh_insights")

# O DONO dos cartoes. Diferente do RAG normal (onde `owner` = quem esta
# falando), este conhecimento e ORGANIZACIONAL: os mesmos cartoes servem para
# qualquer diretor autorizado. Quem pode ler nao e decidido pelo `owner` da
# linha, e sim pela lista de perfis autorizados (ver tools/rh.py) -- checagem
# que acontece no servidor, fora do alcance do modelo.
OWNER_RH = "rh-turnover"


def _pct(v, casas=1):
    return f"{float(v):.{casas}f}".replace(".", ",") + "%"


def _num(v, casas=1):
    """Numero em padrao brasileiro. Existe porque o agente CITA estes textos
    literalmente para um leitor brasileiro -- e "7.8 vezes" num relatorio de
    diretoria parece erro de digitacao, mesmo estando certo."""
    return f"{float(v):.{casas}f}".replace(".", ",")


def _reais(v):
    return "R$ " + f"{float(v):,.0f}".replace(",", ".")


def _milhoes(v):
    return "R$ " + f"{float(v) / 1e6:,.1f}".replace(".", ",") + " milhoes"


def _cartao(titulo, corpo, tema):
    """Um Document com metadata suficiente para o agente citar a origem."""
    return Document(
        page_content=f"{titulo}\n\n{corpo.strip()}",
        metadata={"titulo": titulo, "tema": tema, "origem": "analise da base de RH"},
    )


# =============================================================================
# OS CARTOES
# -----------------------------------------------------------------------------
# Cada funcao recebe o resultado de `rh_analise.analisar()` e devolve uma lista
# de Documents. Separadas por assunto para dar para acrescentar um tema novo
# sem mexer nos outros.
# =============================================================================

def _dimensao(res, nome):
    s = res["segmentos"]
    return s[s.dimensao == nome].sort_values("turnover_vol", ascending=False)


def cartoes_panorama(res):
    ind = res["indicador"]
    serie = res["serie"].dropna(subset=["turnover_12m"])
    primeiro, ultimo = serie.iloc[0], serie.iloc[-1]
    de_ate = f"{primeiro.mes:%m/%Y} a {ultimo.mes:%m/%Y}"
    variacao = (ultimo.turnover_12m / primeiro.turnover_12m - 1) * 100
    custo_folha = ind["custo_saidas"] / ind["folha_anual"] * 100

    return [
        _cartao(
            "Panorama do turnover: onde a empresa esta hoje",
            f"""
Na janela de 12 meses encerrada na data-base, o turnover total foi de
{_pct(ind['turnover_total'])} e o voluntario, {_pct(ind['turnover_vol'])}.
Sao {ind['desl_voluntarios']} pedidos de demissao e {ind['desl_involuntarios']}
desligamentos por decisao da empresa, sobre uma exposicao de
{_num(ind['exposicao'])} FTE-ano. O quadro ativo e de {ind['headcount_ativo']} pessoas.

Metodo: o denominador e exposicao em FTE-ano, e nao headcount medio simples.
A escolha muda o resultado porque o quadro cresceu muito na janela -- com
headcount medio, o proprio crescimento diluiria a taxa e a empresa pareceria
melhorar enquanto piorava.

Implicacao: {_pct(ind['turnover_vol'])} de turnover voluntario nao e um numero
que se resolve com acao generica. O passo seguinte de qualquer conversa e
descobrir ONDE ele se concentra -- por tempo de casa, antes de por area.
""",
            "panorama",
        ),
        _cartao(
            "A tendencia: o turnover esta subindo, e a curva suaviza isso",
            f"""
A janela movel de 12 meses ({de_ate}) saiu de {_pct(primeiro.turnover_12m)}
para {_pct(ultimo.turnover_12m)} na data-base -- uma variacao de
{'+' if variacao >= 0 else ''}{_num(variacao, 0)}%. O voluntario acompanhou: de {_pct(primeiro.turnover_vol_12m)}
para {_pct(ultimo.turnover_vol_12m)}.

Ressalva importante para a leitura: a janela movel de 12 meses SUAVIZA. Ela
ainda carrega meses antigos e bons. O dado mensal bruto do periodo mais recente
e pior do que a curva sugere -- ao responder sobre tendencia, vale olhar os
ultimos meses da serie, e nao so o indicador fechado.

Implicacao: o problema esta em curso, nao passou. Um plano com leitura anual
chega tarde; a leitura precisa ser mensal, por coorte de entrada.
""",
            "panorama",
        ),
    ]


def cartoes_entrada(res):
    faixas = _dimensao(res, "tempo_de_casa").set_index("valor")
    entrada = faixas.loc["0-12 meses"] if "0-12 meses" in faixas.index else None
    if entrada is None:
        return []
    resto = faixas.drop(index="0-12 meses")
    taxa_resto = (resto.desl_voluntarios.sum() / resto.exposicao.sum() * 100)
    razao = entrada.turnover_vol / taxa_resto

    km = res["sobrevivencia"].set_index("mes_de_casa")["risco_acumulado"]
    co = res["coortes"].dropna(subset=["saida_vol_3m"])
    pior = co.loc[co.saida_vol_3m.idxmax()] if len(co) else None
    padrao = co[co.coorte != (pior.coorte if pior is not None else "")].saida_vol_3m

    cartoes = [
        _cartao(
            "O achado central: o problema vive nos primeiros 12 meses",
            f"""
Quem tem menos de um ano de casa sai a {_pct(entrada.turnover_vol)} ao ano.
Quem ja passou do primeiro ano sai a {_pct(taxa_resto)}. E uma razao de
{_num(razao)} vezes. Essa faixa concentra {_pct(entrada.share_saidas_vol)} de
todas as saidas voluntarias e praticamente todo o excesso sobre o esperado.

Metodo: taxa anualizada por exposicao dentro de cada faixa, o que impede que
uma faixa com pouca gente pareça pior so por ter poucos FTE-ano.

Implicacao estrategica -- e esta e a conclusao mais importante da analise
inteira: nao e um problema de clima nem de beneficio. A empresa RETEM bem quem
atravessa o primeiro ano. O que ela nao consegue e fazer as pessoas
atravessarem. Isso muda o endereco da solucao para selecao, integracao e
primeira lideranca, que sao mais baratas, mais rapidas e mensuraveis.
Qualquer campanha aplicada ao quadro inteiro gasta a maior parte do orcamento
em gente que nao esta pensando em sair.
""",
            "entrada",
        ),
    ]

    if len(km) and 12 in km.index:
        cartoes.append(_cartao(
            "Sobrevivencia: de cada 100 contratados, quantos pedem demissao e quando",
            f"""
Pelo estimador de Kaplan-Meier, o risco acumulado de pedido de demissao chega a
{_pct(km.get(3, 0))} no terceiro mes, {_pct(km.get(6, 0))} no sexto e
{_pct(km.get(12, 0))} no primeiro aniversario.
{('No mes 18 chega a ' + _pct(km.get(18)) + '.') if 18 in km.index else ''}

Metodo: Kaplan-Meier com censura a direita, e nao uma porcentagem simples. Quem
foi admitido ha poucos meses ainda nao teve chance de completar o horizonte --
conta-la como "ficou" subestimaria o risco. So entram coortes admitidas a partir
de 2024, porque a base nao traz desligamentos anteriores e as coortes mais
antigas chegam truncadas (so sobrou quem ficou).

Implicacao: o desenho de qualquer intervencao deve ter marco em 30, 60 e 90
dias. O risco se materializa cedo demais para uma pesquisa anual capturar.
""",
            "entrada",
        ))

    if pior is not None and len(padrao):
        cartoes.append(_cartao(
            f"Alerta: a coorte {pior.coorte} rompeu o padrao de entrada",
            f"""
{_pct(pior.saida_vol_3m)} dos admitidos na coorte {pior.coorte} ja haviam pedido
demissao em ate 3 meses. Nas demais coortes observaveis, esse numero fica entre
{_pct(padrao.min())} e {_pct(padrao.max())}. E uma ruptura, nao uma oscilacao.

O que NAO explica: volume de contratacao. As coortes tem tamanho parecido, entao
a hipotese "contratamos demais e baixamos a regua" nao se sustenta com este dado.

O que a base nao responde: qual foi a mudanca. Fonte de recrutamento, criterio
de selecao, proposta de entrada e estrutura de lideranca nao estao na base.

Implicacao: e o achado mais urgente e o mais respondivel com dado que a empresa
ja tem em outro sistema. Deve ser a primeira investigacao, e nao depende de
orcamento aprovado.
""",
            "entrada",
        ))
    return cartoes


def cartoes_segmentos(res, quantos=4):
    ind = res["indicador"]
    media = ind["turnover_vol"]
    cartoes = []

    areas = _dimensao(res, "area")
    for _, r in areas.head(quantos).iterrows():
        prio = res["prioridade"]
        entrada = prio[(prio.area == r.valor) & (prio.faixa_tempo_casa == "0-12 meses")]
        detalhe = ""
        if len(entrada):
            e = entrada.iloc[0]
            detalhe = (
                f"Dentro da area, a faixa de 0 a 12 meses tem {int(e.headcount)} "
                f"pessoas e turnover de {_pct(e.turnover_vol)}, com "
                f"{_num(e.excesso_saidas)} saidas acima do esperado -- "
                f"{_reais(e.custo_excesso)} de custo evitavel."
            )
        cartoes.append(_cartao(
            f"Area: {r.valor}",
            f"""
Turnover voluntario de {_pct(r.turnover_vol)} contra {_pct(media)} da empresa.
Sao {int(r.desl_voluntarios)} pedidos de demissao e
{int(r.desl_involuntarios)} desligamentos, com {int(r.headcount_ativo)} pessoas
ativas. A area responde por {_pct(r.share_saidas_vol)} de todas as saidas
voluntarias. {detalhe}

Ressalva obrigatoria antes de concluir qualquer coisa sobre a gestao da area:
areas que mais contrataram tem, por composicao, mais gente na faixa de risco.
Parte da diferenca entre areas e composicao, nao qualidade de lideranca. Por
isso a leitura correta cruza area COM tempo de casa, e nao area sozinha.
""",
            "segmento",
        ))

    locais = _dimensao(res, "localidade")
    acima = locais[locais.turnover_vol > media]
    if len(acima):
        linhas = "\n".join(
            f"- {r.valor}: {_pct(r.turnover_vol)} ({int(r.desl_voluntarios)} saidas, "
            f"{int(r.headcount_ativo)} ativos)" for _, r in acima.iterrows())
        abaixo = locais[locais.turnover_vol <= media].sort_values("turnover_vol")
        cartoes.append(_cartao(
            "Localidades acima da media da empresa",
            f"""
Media da empresa: {_pct(media)}.

{linhas}

A localidade mais estavel e {abaixo.iloc[0].valor}, com
{_pct(abaixo.iloc[0].turnover_vol)}.

Implicacao: diferenca por localidade que sobrevive ao controle por area aponta
para algo local -- mercado de trabalho da praca, estrutura de lideranca do site
ou condicao de operacao. E uma hipotese a testar com quem conhece a operacao,
nao uma conclusao.
""",
            "segmento",
        ))

    contratos = _dimensao(res, "contrato")
    if len(contratos) > 1:
        pior, melhor = contratos.iloc[0], contratos.iloc[-1]
        cartoes.append(_cartao(
            "Tipo de contrato: temporario x efetivo",
            f"""
{pior.valor}: {_pct(pior.turnover_vol)} de turnover voluntario, com
{int(pior.headcount_ativo)} pessoas ativas.
{melhor.valor}: {_pct(melhor.turnover_vol)}, com {int(melhor.headcount_ativo)} ativas.

Decisao metodologica declarada: os dois vinculos ficam DENTRO do indicador.
Excluir o temporario melhoraria a taxa no papel e esconderia o problema --
a diferenca de comportamento entre os vinculos e informacao, nao ruido a limpar.

Implicacao: se a operacao depende de temporario numa area critica, o desenho da
jornada de entrada precisa contemplar esse vinculo explicitamente. Trilha de
integracao pensada so para efetivo deixa de fora justamente quem mais sai.
""",
            "segmento",
        ))

    perf = _dimensao(res, "performance")
    if len(perf):
        acima_esperado = perf[perf.valor.str.contains("Acima", case=False, na=False)]
        if len(acima_esperado):
            a = acima_esperado.iloc[0]
            cartoes.append(_cartao(
                "Attrition regretido: a empresa esta perdendo quem entrega bem",
                f"""
Quem foi avaliado acima do esperado pede demissao a {_pct(a.turnover_vol)},
praticamente a mesma taxa da empresa ({_pct(media)}). Sao
{int(a.desl_voluntarios)} pessoas de alta performance perdidas na janela.

Implicacao: a retencao atual nao esta protegendo o talento reconhecido -- ela e
neutra em relacao a performance. Uma politica de retencao que nao diferencia
quem entrega e uma politica que trata a saida do melhor e a do pior como o
mesmo evento.

Ressalva: a avaliacao de performance da base e a ultima disponivel e vem sem
data. Ela descreve como a pessoa era vista, nao necessariamente o que ela
entregaria depois.
""",
                "segmento",
            ))
    return cartoes


def cartoes_prioridade(res):
    p = res["prioridade"].sort_values("excesso_saidas", ascending=False)
    top = p[p.excesso_saidas > 0].head(3)
    if not len(top):
        return []
    total_excesso = p[p.excesso_saidas > 0].excesso_saidas.sum()
    concentracao = top.excesso_saidas.sum() / total_excesso * 100
    linhas = "\n".join(
        f"- {r.area}, faixa {r.faixa_tempo_casa}: {int(r.headcount)} pessoas, "
        f"turnover {_pct(r.turnover_vol)}, {_num(r.excesso_saidas)} saidas acima do "
        f"esperado, {_reais(r.custo_excesso)} de custo evitavel"
        for _, r in top.iterrows())
    return [_cartao(
        "Onde agir primeiro: os tres segmentos que concentram o excesso",
        f"""
{linhas}

Juntos, concentram {_pct(concentracao, 0)} de todo o excesso de saidas
voluntarias sobre o esperado, e somam {int(top.headcount.sum())} pessoas.

Criterio de priorizacao -- e ele importa mais que o resultado: "excesso" e
quantas saidas o segmento teve A MAIS do que teria com a taxa media da empresa.
Ranquear por TAXA favoreceria segmento pequeno (uma saida em dez pessoas vira
10%); ranquear por VOLUME favoreceria o segmento grande. O excesso cruza os
dois automaticamente.

Implicacao: o alvo e pequeno o bastante para um piloto de verdade, com grupo de
comparacao, e grande o bastante para o resultado ser legivel estatisticamente.
E a diferenca entre um programa que se prova e um que so se anuncia.
""",
        "prioridade",
    )]


def cartoes_motivos(res):
    m = res["motivos"]
    if not len(m):
        return []
    df = res["colaboradores"]
    ativos = df[df.status == "Ativo"]
    ref_eng = ativos.engajamento.mean()
    ref_lid = ativos.lideranca.mean()

    principal = m.iloc[0]
    suspeitos = m[m.lideranca_media < ref_lid - 4]
    alerta = ""
    if len(suspeitos):
        nomes = " e ".join(f'"{s.motivo}"' for _, s in suspeitos.iterrows())
        alerta = (
            f"\nRepare num padrao: quem declarou {nomes} tem indice de lideranca "
            f"medio de {_num(suspeitos.lideranca_media.mean())}, contra "
            f"{_num(ref_lid)} de quem ficou. Sao motivos declarados diferentes com a "
            "mesma assinatura por tras -- o que sugere que a relacao com o gestor "
            "entrou na conta mesmo quando a pessoa nomeou outra coisa.\n"
        )

    linhas = "\n".join(
        f"- {r.motivo}: {int(r.quantidade)} saidas ({_pct(r.share, 0)}), "
        f"engajamento medio {_num(r.engajamento_medio)}, "
        f"lideranca {_num(r.lideranca_media)}"
        for _, r in m.iterrows())
    return [_cartao(
        "O motivo declarado e o gatilho, nao necessariamente a causa",
        f"""
{linhas}

Referencia de quem ficou: engajamento {_num(ref_eng)}, lideranca {_num(ref_lid)}.

O motivo mais frequente e "{principal.motivo}" ({_pct(principal.share, 0)}). Mas
quem saiu por esse motivo tinha engajamento medio de
{_num(principal.engajamento_medio)}, contra {_num(ref_eng)} de quem ficou. Ou seja: a pessoa ja estava disponivel para
ser recrutada antes de aparecer a proposta.
{alerta}
Ressalva: sao grupos pequenos, de dezenas de pessoas por motivo, e o indice de
lideranca e a ultima medicao disponivel, sem data. Isto e associacao, nao prova.

Implicacao pratica: responder literalmente ao motivo declarado -- dar aumento
para quem disse "remuneracao", flexibilizar horario para quem disse
"flexibilidade" -- tem chance alta de tratar o sintoma. A entrevista de
desligamento e ponto de partida, nao diagnostico.
""",
        "motivos",
    )]


def cartoes_fatores(res):
    f = res["fatores"]
    sig = f[f.significativo].sort_values("odds_ratio", ascending=False)
    if not len(sig):
        return []
    linhas = "\n".join(
        f"- {r.fator}: {_num(r.odds_ratio, 2)}x (IC 95%: {_num(r.ic_inferior, 2)} a {_num(r.ic_superior, 2)})"
        for _, r in sig.iterrows())
    r2 = f.attrs.get("pseudo_r2", "n/d")
    r2 = _num(r2, 3) if isinstance(r2, (int, float)) else r2
    return [_cartao(
        "Fatores associados a saida voluntaria, e onde a analise para de afirmar",
        f"""
Regressao logistica sobre ativos e quem pediu demissao na janela. Fatores com
significancia estatistica a 5%, em razao de chances:

{linhas}

Como ler: a razao de chances vale mantendo os demais fatores constantes. Ou
seja, o efeito de estar no primeiro ano sobrevive ao controle por engajamento,
salario e lideranca -- nao e so composicao.

⚠️ ONDE A ANALISE PARA. O pseudo-R2 e {r2}: os fatores que a base mede explicam
uma fracao pequena da variacao. Alem disso, engajamento e indice de lideranca
sao a ultima medicao disponivel, SEM data -- podem ter caido DEPOIS de a pessoa
decidir sair. Causalidade reversa e hipotese viva aqui.

Implicacao: estes fatores servem para PRIORIZAR atencao e para formular
hipoteses testaveis. Nao servem para afirmar causa, e nenhuma recomendacao
deveria depender de uma afirmacao causal que esta base nao sustenta. O caminho
correto e desenhar um experimento com grupo de comparacao.
""",
        "fatores",
    )]


def cartoes_custo(res):
    ind = res["indicador"]
    df = res["colaboradores"]
    entrada = df[(df.faixa == "0-12 meses") & df.saiu]
    custo_entrada = float(entrada.custo_reposicao.sum())
    parcela = custo_entrada / ind["custo_saidas"] * 100
    custo_medio = float(df.loc[df.saiu_vol, "custo_reposicao"].mean())
    folha = ind["custo_saidas"] / ind["folha_anual"] * 100
    return [_cartao(
        "Quanto custa: o turnover em reais, e onde o dinheiro esta",
        f"""
Custo estimado das saidas dos ultimos 12 meses: {_milhoes(ind['custo_saidas'])},
o equivalente a {_pct(folha)} da folha anual do quadro ativo. O custo medio de
uma saida voluntaria e {_reais(custo_medio)}.

Onde o dinheiro esta: {_milhoes(custo_entrada)} -- {_pct(parcela, 0)} do total --
vem de {len(entrada)} pessoas que sairam com menos de um ano de casa. E dinheiro
gasto em recrutar, admitir e treinar quem nunca chegou a produzir plenamente.

Premissas declaradas (e discutiveis de proposito): remuneracao anual =
salario-base x {_num(MESES_ANO, 2)} x {_num(ENCARGOS, 2)} de encargos e beneficios; custo de
reposicao entre {min(FATOR_REPOSICAO.values()):.0%} e {max(FATOR_REPOSICAO.values()):.0%}
da remuneracao anual conforme o nivel do cargo. Isso fica na
parte BAIXA da faixa usual de mercado, que vai de 0,5x a 2,0x.

O que NAO esta incluido: perda de receita, retrabalho, custo de oportunidade da
vaga aberta e sobrecarga de quem fica. O numero e conservador por construcao.

Implicacao: se alguem achar os fatores altos, o modelo esta em rh.premissas e
recalcula. Um business case com premissa escondida nao sobrevive a primeira
pergunta da diretoria.
""",
        "custo",
    )]


def cartoes_metodo(res):
    q = res["qualidade"]
    descartados = [c for c in q if c[2] in ("DESCARTAR", "SEM USO")]
    limitacoes = [c for c in q if c[2] == "LIMITACAO"]
    cartoes = [_cartao(
        "O que esta base NAO consegue responder",
        f"""
{chr(10).join('- ' + c[0] + ': ' + c[1] for c in limitacoes + descartados)}

Por que isto importa numa conversa de diretoria: um agente que responde tudo e
um agente em que nao se pode confiar em nada. Quando a pergunta cai fora do que
a base sustenta, a resposta correta e dizer isso e apontar onde o dado estaria
(outro sistema, uma pesquisa nova, uma conversa com a operacao).

Exemplos de pergunta que esta base NAO responde: quem individualmente esta em
risco de sair; qual foi a causa de uma mudanca recente na contratacao; qual o
efeito de uma acao que ainda nao foi feita; qualquer coisa sobre pessoas
nomeadas.
""",
        "metodo",
    )]

    atencao = [c for c in q if c[2] in ("ATENCAO", "DESCARTAR")]
    if atencao:
        cartoes.append(_cartao(
            "Qualidade da base: o que foi testado e o que foi descartado",
            f"""
{chr(10).join('- ' + c[0] + ': ' + c[1] + ' -> ' + c[2] for c in atencao)}

A base passou nos testes de integridade estrutural: sem duplicidade de
identificador, sem desligamento anterior a admissao, sem data fora da janela
declarada, e o tempo de casa bate com as datas.

Decisao que muda analise: a variavel de meses desde a ultima promocao foi
DESCARTADA, porque a maior parte dos valores preenchidos era maior que o proprio
tempo de casa da pessoa -- logicamente impossivel. Isso tem consequencia:
estagnacao de carreira seria uma hipotese natural para explicar saida, e ela
simplesmente nao pode ser testada com esta base. Imputar a mediana teria criado
um fator de risco a partir de ruido.
""",
            "metodo",
        ))

    cartoes.append(_cartao(
        "Limites eticos: o que este agente nao faz, e por que",
        """
Este agente nao responde sobre pessoas. Nao existe consulta que devolva linha de
colaborador, e qualquer agrupamento com menos de 5 pessoas e suprimido antes de
sair do banco -- inclusive medias, porque media de grupo minusculo e dado
individual disfarçado.

Nao ha score de risco por pessoa, nao ha lista nominal, e nenhuma decisao sobre
individuo -- avaliacao, promocao, merito, desligamento -- deve ser tomada a
partir do que este agente responde.

A razao nao e so legal, e pratica: no dia em que um gestor souber quem "vai
sair", ou trata diferente ou para de investir naquela pessoa. Um modelo de risco
mal governado nao erra a previsao -- ele a realiza.

O que o agente faz: identificar CELULAS e SEGMENTOS que precisam de atencao, com
o encaminhamento junto. A conversa que resolve o problema e a mesma nos dois
casos; a diferenca e que uma versao nao expoe ninguem.
""",
        "etica",
    ))
    return cartoes


def cartoes_acao(res):
    p = res["prioridade"].sort_values("excesso_saidas", ascending=False)
    top = p[p.excesso_saidas > 0].head(3)
    alvo = ", ".join(sorted(set(top.area))) if len(top) else "as areas criticas"
    pessoas = int(top.headcount.sum()) if len(top) else 0
    return [_cartao(
        "As tres frentes recomendadas, com a hipotese de cada uma",
        f"""
Frente 1 -- blindar os primeiros 90 dias, em {alvo} ({pessoas} pessoas).
Hipotese: a saida precoce vem de expectativa desalinhada e ausencia de rampa
estruturada. Acoes: trilha de rampa por funcao com marcos de 30-60-90, buddy
designado, primeiro feedback formal no 30o dia, pulso curto em 30/60/90 dias e
apresentacao realista da vaga no processo seletivo.
Metrica: saida voluntaria ate 90 dias da coorte.

Frente 2 -- qualificar a lideranca de entrada.
Hipotese: a primeira lideranca define a permanencia, e hoje isso nao e medido
por celula, so por area -- nivel em que o indicador nao distingue uma area que
perde gente de uma que nao perde.
Metrica: indice de lideranca por celula de gestao.

Frente 3 -- corrigir desvios de equidade, de forma cirurgica.
Hipotese: compa-ratio muito abaixo do ponto de referencia na entrada cria
vulnerabilidade a proposta externa. Nao e reajuste linear: e correcao dos casos
abaixo do piso nas areas criticas.
Metrica: percentual do quadro abaixo do piso.

Como saber se funcionou: rollout escalonado por celula, com ordem sorteada.
Todas recebem a intervencao, mas em momentos diferentes -- o que resolve a
objecao etica de negar beneficio e ainda assim permite comparacao valida.
Criterio de decisao definido ANTES de comecar: escala se reduzir 20% ou mais,
ajusta entre 10% e 20%, encerra abaixo de 10%.
""",
        "acao",
    )]


def gerar(res):
    """Monta todos os cartoes. Devolve lista de Documents."""
    cartoes = []
    for funcao in (cartoes_panorama, cartoes_entrada, cartoes_segmentos,
                   cartoes_prioridade, cartoes_motivos, cartoes_fatores,
                   cartoes_custo, cartoes_metodo, cartoes_acao):
        try:
            cartoes += funcao(res)
        except Exception:
            # Um tema que falha nao derruba os outros. O log diz qual foi --
            # e um cartao a menos e muito melhor que a carga inteira abortada.
            log.exception("falha ao gerar cartoes de %s", funcao.__name__)
    log.info("%d cartoes gerados", len(cartoes))
    return cartoes


async def indexar(rag, res):
    """Gera os cartoes e grava no RAG, um `fonte` por tema.

    UM `fonte` POR TEMA, e nao um so para tudo: o `guardar` do RAG apaga a fonte
    antes de inserir, entao temas separados podem ser regerados isoladamente.
    E, na hora de citar, "analise-entrada" diz mais ao leitor que "cartoes".
    """
    from collections import defaultdict

    por_tema = defaultdict(list)
    for doc in gerar(res):
        por_tema[doc.metadata.get("tema", "geral")].append(doc)

    total = {}
    for tema, docs in por_tema.items():
        total[f"analise-{tema}"] = await rag.guardar(OWNER_RH, f"analise-{tema}", docs)
    return total
