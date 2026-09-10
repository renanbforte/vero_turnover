# =============================================================================
# rh_graficos.py — o CATALOGO: o que cada grafico e, e de onde ele vem
# -----------------------------------------------------------------------------
# ESTE ARQUIVO NAO DESENHA NADA. Ele descreve.
#
# O desenho acontece no navegador, em SVG (ver `web/painel.html`), porque a
# pagina precisa ser explorada: passar o mouse e ver o valor exato, clicar numa
# barra e filtrar por ela. Imagem nao faz isso.
#
# ⚠️ O RISCO DE TER DOIS DESENHOS -- e por que ele e menor do que parece.
# O deck usa matplotlib; a pagina usa SVG. Sao dois codigos que desenham a mesma
# figura, e dois codigos divergem. MAS: os dois consomem o MESMO calculo, feito
# uma vez em pandas por `rh_exploracao.py`, com as mesmas funcoes que geraram os
# numeros do deck. Entao a divergencia possivel e de TRACO -- cor, espessura,
# rotulo -- e nunca de NUMERO. Uma barra pode ficar mais grossa; ela nao pode
# dizer 38,8% enquanto a outra diz 39,3%.
#
# O QUE CADA ENTRADA GUARDA, e por que:
#   chave     onde os dados estao no JSON devolvido por /painel/api/contexto
#   pergunta  o que o grafico responde, em uma frase
#   slide     onde ele aparece na apresentacao
#   fonte     a origem no banco
#   colunas   as colunas usadas
#   metodo    a conta, em portugues -- e a resposta para "de onde saiu?"
#   funcao    a funcao de `rh_analise.py` que faz a agregacao
#
# A CONSULTA DE CADA GRAFICO NAO MORA AQUI, e sim em `rh_sql_graficos.py`.
# Ela nao e texto fixo: depende dos filtros da tela e das premissas editadas no
# momento, entao precisa ser MONTADA a cada requisicao. Descricao fica aqui;
# consulta fica la.
#
# Com isso a pagina consegue exibir, ao lado de cada figura, o caminho completo
# do numero: qual tabela, qual recorte, qual funcao, qual conta.
# =============================================================================

from dataclasses import dataclass, field

@dataclass
class Grafico:
    """Um grafico e TUDO que responde 'de onde veio esse numero'.

    A procedencia tem quatro camadas, e nenhuma delas basta sozinha:
      1. a CONSULTA QUE REPRODUZ ESTE GRAFICO -- montada em `rh_sql_graficos.py`
         com o recorte proprio dele no WHERE, a agregacao e as premissas da tela
      2. o RECORTE PROPRIO deste grafico, em portugues -- a restricao que so
         ele aplica, dita por extenso para quem nao le SQL
      3. os PASSOS de calculo, em ordem
      4. a LEITURA VISUAL -- o que cada eixo, cor e tamanho representam

    A camada 2 e a que costuma faltar e a que mais gera duvida na banca: dois
    graficos podem sair da mesma consulta e ainda assim falar de populacoes
    diferentes, porque um olha so quem pediu demissao e o outro so quem foi
    admitido depois de 2024.
    """
    id: str
    chave: str             # a chave no JSON de /painel/api/contexto
    titulo: str
    pergunta: str
    slide: str
    fonte: str
    colunas: str
    metodo: str
    funcao: str            # a funcao de rh_analise que agrega
    tipo: str              # como a pagina desenha: linha, barra, passo, bolha...
    recorte: str = "Nenhum além dos filtros escolhidos na tela."
    passos: list = field(default_factory=list)     # o calculo, em ordem
    visual: list = field(default_factory=list)     # [elemento, o que representa]
    leitura: str = ""
    filtravel: bool = True
    tags: list = field(default_factory=list)


CATALOGO: dict[str, Grafico] = {}


def registrar(g: Grafico):
    CATALOGO[g.id] = g
    return g


registrar(Grafico(
    id="evolucao", chave="serie", tipo="linhas",
    titulo="Turnover anualizado — janela móvel de 12 meses",
    pergunta="O turnover está subindo, estável ou caindo?",
    slide="Slide 5 · Dobrou em 18 meses",
    fonte="rh.colaboradores → reconstrução mensal",
    colunas="data_admissao, data_desligamento, status_desligamento",
    funcao="rh_analise.serie_mensal()",
    metodo=(
        "Para cada mês, conta quem estava na empresa (admitido antes e ainda não "
        "desligado) e quantos saíram. Depois soma os desligamentos dos 12 meses "
        "anteriores e divide pelo quadro médio do mesmo período.\n\n"
        "Os 11 primeiros meses ficam vazios de propósito: a janela ainda não "
        "fechou, e um número sobre menos meses pareceria anual sem ser."
    ),
    leitura="De 6,4% para 14,3% em 18 meses. A área rosa — o voluntário — é o que engorda.",
    recorte='Nenhum além dos filtros da tela. Mas os 11 primeiros meses da série ficam vazios: a janela de 12 meses ainda não fechou, e uma taxa calculada sobre menos meses pareceria anual sem ser.',
    passos=[
        'Para cada mês entre jan/2024 e a data-base, conta quem estava na empresa: admissão ≤ fim do mês E (sem data de desligamento OU desligamento > fim do mês).',
        'Conta as saídas ocorridas dentro do mês, separadas em voluntárias e involuntárias.',
        'Soma móvel de 12 meses das saídas; média móvel de 12 meses do quadro.',
        'turnover_12m = saídas dos 12 meses ÷ quadro médio dos 12 meses × 100.',
    ],
    visual=[
        ['Eixo X', 'mês de competência'],
        ['Eixo Y', 'taxa anualizada, em %'],
        ['Linha azul', 'turnover total'],
        ['Linha magenta + área', 'turnover voluntário'],
        ['Linha laranja', 'turnover involuntário'],
        ['Rótulos', 'só no primeiro e no último ponto, para não poluir'],
    ],
    tags=["tendência"],
))

registrar(Grafico(
    id="quadro", chave="serie", tipo="barras_duplas",
    titulo="Quadro e desligamentos, mês a mês",
    pergunta="Quantas pessoas havia na empresa em cada mês — e quantas saíram?",
    slide="Slide 5 · Dobrou em 18 meses",
    fonte="rh.colaboradores → reconstrução mensal",
    colunas="data_admissao, data_desligamento, status_desligamento",
    funcao="rh_analise.serie_mensal()",
    metodo=(
        "⚠️ ESTE É O GRÁFICO QUE MAIS EXIGE EXPLICAÇÃO: a planilha NÃO traz "
        "histórico de quadro. Ela é um retrato da data-base.\n\n"
        "O headcount de cada mês foi RECONSTRUÍDO das datas: a pessoa conta no "
        "mês M se foi admitida até M e (não tem data de desligamento OU saiu "
        "depois de M). Sem isso só existe 'hoje', e não dá para falar em tendência."
    ),
    leitura="445 → 1.252 pessoas. O crescimento não causa turnover, mas define onde ele aparece.",
    recorte='Nenhum além dos filtros da tela. Usa a série inteira, inclusive os meses em que a janela de 12 meses ainda não fechou — aqui não há taxa, só contagem.',
    passos=[
        'Mesma reconstrução mensal do gráfico anterior: quem estava na empresa no fim de cada mês.',
        'Conta admissões e saídas dentro de cada mês.',
        'Nenhuma divisão é feita: este gráfico mostra CONTAGEM, não percentual.',
    ],
    visual=[
        ['Eixo X', 'mês'],
        ['Eixo Y esquerdo', 'quadro no fim do mês (barra larga e clara)'],
        ['Eixo Y direito', 'desligamentos no mês (barra estreita)'],
        ['Magenta', 'pedidos de demissão'],
        ['Laranja', 'desligamentos pela empresa'],
    ],
    tags=["contexto"],
))

registrar(Grafico(
    id="tempo_de_casa", chave="tempo_de_casa", tipo="barras",
    titulo="Turnover voluntário por tempo de casa",
    pergunta="Onde o turnover se concentra?",
    slide="Slide 6 · O problema tem endereço",
    fonte="rh.colaboradores → agregado por faixa",
    colunas="tempo_empresa_anos, exposicao_ltm, saiu_vol_ltm",
    funcao="rh_analise.segmentos()",
    metodo=(
        "Saídas voluntárias da faixa ÷ exposição em FTE-ano da MESMA faixa.\n\n"
        "A exposição é quanto tempo, em anos, a pessoa esteve na empresa dentro "
        "da janela de 12 meses. Usar exposição — e não número de pessoas — é o "
        "que impede uma faixa pequena de parecer pior só por ter pouca gente.\n\n"
        "A faixa '0-12 meses' é tempo de casa MENOR que 1 ano: quem completou 12 "
        "meses já atravessou o primeiro ano."
    ),
    leitura="39,3% no primeiro ano contra 2% a 7% depois. Uma razão de 7,8 vezes.",
    recorte="Agrupa por faixa de tempo de casa. A faixa '0-12 meses' é tempo MENOR que 1 ano — quem completou 12 meses já entrou na faixa seguinte.",
    passos=[
        'Exposição de cada pessoa na janela = dias entre max(admissão, início da janela) e min(desligamento ou data-base, data-base), dividido por 365,25.',
        'Marca saiu_vol quando o desligamento caiu na janela E o status é Voluntário.',
        'Agrupa por faixa e soma exposição e saídas voluntárias.',
        'turnover = saídas voluntárias ÷ exposição × 100.',
    ],
    visual=[
        ['Eixo X', 'faixa de tempo de casa'],
        ['Eixo Y', 'turnover voluntário anualizado'],
        ['Barra magenta', 'faixa acima de 1,5× a média do recorte'],
        ['Barra cinza', 'demais faixas'],
        ['Linha tracejada', 'turnover voluntário médio do recorte atual'],
        ['Clique na barra', 'filtra a página inteira por aquela faixa'],
    ],
    tags=["achado central"],
))

registrar(Grafico(
    id="sobrevivencia", chave="sobrevivencia", tipo="passo",
    titulo="Risco acumulado de pedido de demissão (Kaplan-Meier)",
    pergunta="De cada 100 pessoas contratadas, quantas pedem demissão — e quando?",
    slide="Slide 6 · O problema tem endereço",
    fonte="rh.colaboradores → coortes admitidas a partir de 2024",
    colunas="data_admissao, data_desligamento, status_desligamento",
    funcao="rh_analise.sobrevivencia()",
    metodo=(
        "Kaplan-Meier com CENSURA À DIREITA. A cada mês: risco do mês = eventos "
        "÷ pessoas ainda em observação. A sobrevivência acumulada multiplica "
        "(1 − risco), e o risco acumulado é 1 − sobrevivência.\n\n"
        "Por que não uma porcentagem simples: quem foi admitido há 4 meses ainda "
        "não teve chance de completar 12. Contá-la como 'ficou' subestimaria o "
        "risco — o estimador a tira do denominador nos horizontes que ela não "
        "alcançou.\n\n"
        "Só entram coortes de 2024 em diante: a base não traz desligamento "
        "anterior, então coortes mais antigas chegariam truncadas."
    ),
    leitura="12,0% pedem demissão até o primeiro aniversário. Um em cada oito.",
    recorte='⚠️ Só admitidos a partir de 01/01/2024 — a base não traz desligamento anterior, então coortes mais antigas chegariam truncadas (só teria sobrado quem ficou). Além disso, um mês só entra na curva se houver pelo menos 60 pessoas ainda em observação.',
    passos=[
        'Para cada pessoa: tempo observado = até a saída, se pediu demissão; até a data-base, se continua na empresa ou se foi desligada por decisão dela.',
        'Evento = pedido de demissão. Desligamento pela empresa NÃO é evento: entra como censura.',
        'A cada mês m: risco do mês = eventos em m ÷ pessoas ainda em observação no início de m.',
        'Sobrevivência acumulada S = produto de (1 − risco). Risco acumulado = (1 − S) × 100.',
    ],
    visual=[
        ['Eixo X', 'meses de casa'],
        ['Eixo Y', '% que já pediu demissão até ali'],
        ['Degrau', 'cada mês é um patamar; a altura do degrau é o risco daquele mês'],
        ['Pontos destacados', 'os horizontes de 3, 6, 12 e 18 meses'],
    ],
    tags=["achado central", "método"],
))

registrar(Grafico(
    id="coortes", chave="coortes", tipo="barras_agrupadas",
    titulo="Saída precoce por coorte de admissão",
    pergunta="As contratações recentes estão aguentando mais ou menos que as antigas?",
    slide="Slide 7 · A coorte 2026Q1 rompeu o padrão",
    fonte="rh.colaboradores → agrupado por trimestre de admissão",
    colunas="data_admissao, data_desligamento, status_desligamento",
    funcao="rh_analise.coortes()",
    metodo=(
        "Agrupa por trimestre de admissão e calcula, em cada horizonte, quantos "
        "por cento pediram demissão até ali. Só entram as pessoas que TIVERAM "
        "CHANCE de atingir o horizonte.\n\n"
        "Barras ausentes são coortes ainda sem horizonte observável. Lê-las como "
        "zero seria erro: significam 'ainda não dá para saber'."
    ),
    leitura="10,8% na coorte 2026Q1 contra 1% a 3% nas anteriores. É ruptura, não oscilação.",
    recorte='Só admitidos a partir de 2024. Em cada horizonte entram apenas as pessoas que TIVERAM CHANCE de atingi-lo: quem entrou há 2 meses não conta no cálculo de 3 meses. Coorte com menos de 20 elegíveis fica sem barra naquele horizonte.',
    passos=[
        'Agrupa por trimestre de admissão.',
        'Para cada horizonte h (3, 6 e 12 meses): elegíveis = quem já foi observado por h meses.',
        'Eventos = quantos desses elegíveis pediram demissão até h meses de casa.',
        'taxa = eventos ÷ elegíveis × 100.',
    ],
    visual=[
        ['Eixo X', 'trimestre de admissão'],
        ['Eixo Y', '% que saiu até o horizonte'],
        ['Barra magenta', 'saíram até 3 meses'],
        ['Barra azul', 'saíram até 6 meses'],
        ['Rótulo aparece', 'quando a coorte passa de 2,5× a mediana das demais'],
        ['Barra ausente', 'horizonte ainda não observável — não é zero'],
    ],
    tags=["alerta"],
))

registrar(Grafico(
    id="prioridade", chave="prioridade", tipo="bolhas",
    titulo="Onde agir primeiro — área × tempo de casa",
    pergunta="Por onde começar, considerando impacto e tamanho ao mesmo tempo?",
    slide="Slide 8 · Área ou tempo de casa?",
    fonte="rh.colaboradores → cruzamento área × faixa",
    colunas="area, faixa_tempo_casa, exposicao_ltm, saiu_vol_ltm",
    funcao="rh_analise.prioridade()",
    metodo=(
        "EXCESSO = saídas observadas − saídas esperadas, onde o esperado é a "
        "exposição do segmento × a taxa média da empresa.\n\n"
        "Por que excesso e não taxa: ranquear por taxa favorece o grupo pequeno "
        "(uma saída em dez vira 10%); por volume, favorece o grande. O excesso "
        "cruza impacto e tamanho automaticamente.\n\n"
        "Segmentos com menos de 8 FTE-ano de exposição ficam fora: com pouca "
        "exposição, uma única saída vira 200%."
    ),
    leitura="Três segmentos concentram 73% do excesso, e somam 271 pessoas.",
    recorte="⚠️ Dois recortes próprios, além dos filtros da tela: segmentos com menos de 8 FTE-ano de exposição são descartados (com pouca exposição, uma única saída vira 200%), e o gráfico exibe SOMENTE a faixa '0-12 meses'.",
    passos=[
        'Cruza área × faixa de tempo de casa e soma exposição e saídas voluntárias.',
        'Taxa média do recorte = total de saídas voluntárias ÷ exposição total.',
        'Esperado do segmento = exposição do segmento × taxa média do recorte.',
        'Excesso = saídas observadas − esperado.',
        'Custo evitável = excesso × custo médio de uma saída voluntária.',
    ],
    visual=[
        ['Eixo X', 'excesso de saídas sobre o esperado'],
        ['Eixo Y', 'turnover voluntário do segmento'],
        ['Tamanho da bolha', 'pessoas no segmento'],
        ['Magenta', 'excesso de 6 saídas ou mais'],
        ['Laranja', 'excesso entre 2,5 e 6'],
        ['Cinza', 'excesso abaixo de 2,5'],
        ['Clique na bolha', 'filtra a página inteira por aquela área'],
    ],
    tags=["priorização"],
))

registrar(Grafico(
    id="motivos", chave="motivos", tipo="barras_h",
    titulo="Motivos declarados nas saídas voluntárias",
    pergunta="O que as pessoas dizem quando pedem demissão?",
    slide="Slide 9 · Por que saem",
    fonte="rh.colaboradores → saídas voluntárias da janela",
    colunas="motivo_desligamento, engajamento, indice_lideranca, compa_ratio",
    funcao="rh_analise.motivos()",
    metodo=(
        "Contagem por motivo — mas o gráfico sozinho ENGANA. Passe o mouse: "
        "cada barra traz o engajamento e o índice de liderança médios de quem "
        "declarou aquele motivo.\n\n"
        "É a comparação desses números com a média de quem FICOU — e não a "
        "contagem — que mostra que o motivo declarado é gatilho, não causa."
    ),
    leitura="'Proposta externa' lidera — mas esse grupo já tinha engajamento 6,5 pontos abaixo.",
    recorte='Só saídas VOLUNTÁRIAS ocorridas dentro da janela de 12 meses. Desligamento por decisão da empresa não entra — o motivo declarado ali é outro fenômeno.',
    passos=[
        'Filtra as saídas voluntárias da janela.',
        'Agrupa por motivo declarado e conta.',
        'Para cada motivo, calcula as médias de engajamento, índice de liderança, compa-ratio e tempo de casa de quem o declarou.',
    ],
    visual=[
        ['Eixo X', 'número de saídas'],
        ['Eixo Y', 'motivo declarado'],
        ['Magenta', 'motivo mais frequente'],
        ['Azul', '2º e 3º mais frequentes'],
        ['Passe o mouse', 'traz o PERFIL de quem declarou — é aí que está o argumento, não na contagem'],
    ],
    tags=["causa"],
))

registrar(Grafico(
    id="fatores", chave="fatores", tipo="intervalo",
    titulo="Fatores associados à saída voluntária",
    pergunta="Que características aparecem junto com o pedido de demissão?",
    slide="Slide 9 · Por que saem",
    fonte="rh.colaboradores → ativos + saídas voluntárias",
    colunas="tempo_empresa_anos, engajamento, indice_lideranca, compa_ratio, horas_extras_mes",
    funcao="rh_analise.fatores()  ·  statsmodels.Logit",
    metodo=(
        "Regressão logística sobre ativos + quem pediu demissão. Quem foi "
        "DESLIGADO pela empresa fica de fora: é outro fenômeno, e misturar "
        "produz um modelo que não explica nenhum dos dois.\n\n"
        "A razão de chances vale mantendo os demais fatores constantes. A barra "
        "é o intervalo de confiança de 95%: se ela cruza o 1,0, o fator não é "
        "estatisticamente significativo.\n\n"
        "⚠️ Isto é ASSOCIAÇÃO. Num recorte pequeno a regressão pode não "
        "convergir — e aí o gráfico fica vazio, o que é honesto."
    ),
    leitura="Estar no primeiro ano multiplica por 4,5 — e sobrevive ao controle pelos demais.",
    recorte='⚠️ Universo = colaboradores ATIVOS + quem pediu demissão na janela. Quem foi desligado pela empresa fica FORA: sair por decisão do gestor é outro fenômeno, e misturar produz um modelo que não explica nenhum dos dois. Colunas sem variação no recorte são descartadas antes do ajuste.',
    passos=[
        "Monta as variáveis na direção do RISCO (ex.: 'engajamento 10 pontos MENOR'), para a razão de chances sair acima de 1 quando o fator aumenta o risco.",
        "Ajusta uma regressão logística (statsmodels.Logit) com 'pediu demissão' como desfecho.",
        'Razão de chances = exponencial do coeficiente; o IC 95% é a mesma transformação aplicada aos limites do intervalo.',
        'Marca como significativo quem tem p-valor abaixo de 0,05.',
    ],
    visual=[
        ['Eixo X', 'razão de chances de pedir demissão'],
        ['Ponto', 'a estimativa'],
        ['Barra horizontal', 'intervalo de confiança de 95%'],
        ['Linha vertical em 1,0', 'ausência de associação'],
        ['Magenta', 'significativo a 5%'],
        ['Cinza', 'o intervalo cruza o 1,0 — não é significativo'],
    ],
    tags=["causa", "método"],
))

registrar(Grafico(
    id="custo", chave="custo", tipo="custo",
    titulo="Custo estimado do turnover",
    pergunta="Quanto isso custa — e onde o dinheiro está?",
    slide="Slide 12 · R$ 10,4 milhões pela porta de entrada",
    fonte="rh.colaboradores → recalculado com as premissas da tela",
    colunas="salario_base, nivel_cargo, saiu_vol_ltm, saiu_invol_ltm, faixa_tempo_casa",
    funcao="rh_analise.preparar()  ·  custo_reposicao",
    metodo=(
        "remuneração anual = salário-base × meses/ano × encargos\n"
        "custo de reposição = remuneração anual × fator do nível\n\n"
        "⚠️ AS PREMISSAS SÃO EDITÁVEIS NESTA PÁGINA, e o custo é RECALCULADO do "
        "salário e do nível a cada mudança — não é lido de uma coluna já somada. "
        "Se alguém achar o fator alto, é só mexer e ver o número mudar.\n\n"
        "Os padrões usam a parte BAIXA do intervalo de mercado (0,5x a 2,0x a "
        "remuneração anual) e NÃO incluem perda de receita, retrabalho nem custo "
        "da vaga aberta. É conservador por construção."
    ),
    leitura="R$ 10,4 milhões — 68% do total — vêm de quem saiu com menos de um ano.",
    recorte='Só as saídas ocorridas na janela de 12 meses. A quarta barra é um SUBCONJUNTO das anteriores: dentre essas saídas, as de quem tinha menos de 1 ano de casa. Por isso ela não soma com as outras.',
    passos=[
        'remuneração anual = salário-base × meses/ano × encargos.',
        'custo de reposição da pessoa = remuneração anual × fator do nível do cargo.',
        'Soma por grupo: voluntárias, involuntárias, total e o subconjunto de 0-12 meses.',
        '⚠️ Os três parâmetros vêm dos campos editáveis no topo desta página. Alterar qualquer um recalcula tudo a partir do salário e do nível — nada é lido de coluna já somada no banco.',
    ],
    visual=[
        ['Eixo X', 'grupo de saídas'],
        ['Eixo Y', 'R$ em milhões, na janela de 12 meses'],
        ['Magenta', 'saídas voluntárias'],
        ['Laranja', 'desligamentos pela empresa'],
        ['Azul', 'custo total'],
        ['Magenta claro', 'subconjunto: quem saiu com menos de 1 ano de casa'],
    ],
    tags=["valor", "editável"],
))
