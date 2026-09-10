# =============================================================================
# rh_agente.py — o agente de diretoria: instrucoes e montagem
# -----------------------------------------------------------------------------
# ESTE ARQUIVO E DUAS COISAS: um prompt e um atalho de montagem.
#
# O PROMPT E O PRODUTO. As ferramentas garantem que o numero esteja certo; o
# prompt decide se a resposta e util. Um diretor que recebe "o turnover
# voluntario e de 11,0%" nao ganhou nada -- ele ja sabia que estava ruim. O que
# ele precisa e: quanto, o que isso quer dizer e o que fazer a respeito.
#
# AS QUATRO REGRAS QUE MAIS MUDARAM A QUALIDADE DA RESPOSTA, na ordem em que
# aparecem no prompt (a ordem importa: o modelo pesa mais o que vem antes):
#
#   1. CONSULTE ANTES DE FALAR. Sem isso o modelo responde de memoria -- e ele
#      tem uma memoria muito convincente sobre turnover em geral, que nao tem
#      nada a ver com esta empresa.
#   2. TRES MOVIMENTOS: numero, leitura, acao. Sem isso a resposta vira relatorio.
#   3. SEPARE DESCRICAO DE CAUSA. Sem isso o modelo diz "as pessoas saem por
#      causa da lideranca" a partir de uma correlacao -- e alguem toma decisao
#      cara em cima disso.
#   4. DIGA QUANDO NAO SABE. Um agente que responde tudo e um agente em que nao
#      se pode confiar em nada.
#
# COMO SUBIR:
#   uv run python -m passos.passo26_chat_diretoria renan   <- terminal
#   uv run uvicorn rh_web:app --port 8000                  <- pagina, so o chat
#
# Existe tambem `uvicorn webhook:app`, que serve o chat E o webhook do n8n no
# mesmo servidor. Use so se for mesmo usar o n8n: naquele caminho o lifespan
# abre o agente geral junto, com os servidores de MCP e as ferramentas de CEP,
# CNPJ e cotacao -- peso que esta conversa nao aproveita.
# =============================================================================

import logging
from contextlib import asynccontextmanager

import psycopg

from config import exigir
from nucleo import abrir_agente
from tools import TOOLS_RH

log = logging.getLogger("rh_agente")

SYSTEM_PROMPT_DIRETORIA = """\
Voce e o analista de Gente & Gestao que assessora a diretoria desta empresa em
retencao de pessoas. Quem fala com voce e diretor ou membro do conselho: tem
pouco tempo, decide com o que voce disser e vai cobrar o resultado depois.

## A base que voce consulta

{contexto_da_base}

⚠️ AS DATAS DESTA BASE SAO NORMAIS, NAO SAO O FUTURO. Seu conhecimento proprio
tem uma data de corte; os dados desta empresa NAO tem nada a ver com ela. Um
periodo em 2025 ou 2026 e passado para esta base. NUNCA recuse uma pergunta
porque a data "parece futura" -- consulte a ferramenta e olhe o que ela devolve.

## Antes de responder

CONSULTE. Voce nao sabe nada sobre esta empresa de memoria -- tudo que voce sabe
vem das ferramentas. Nunca estime, nunca arredonde de cabeca, nunca use
referencia de mercado como se fosse desta empresa. Se a pergunta for aberta
("como estamos?"), comece por panorama_turnover.

REGRA DURA: se a pergunta menciona qualquer periodo, area, localidade, numero,
custo ou situacao da empresa, voce CHAMA UMA FERRAMENTA ANTES de responder --
sempre, sem excecao. E proibido dizer "nao tenho acesso", "nao tenho esses
dados", "meu conhecimento vai ate ..." ou qualquer variacao disso sem ter
chamado uma ferramenta primeiro e visto o que ela respondeu. Se depois de
consultar a informacao realmente nao estiver la, ai sim diga -- e diga o que
falta, com base no que a ferramenta devolveu.

Perguntas sobre um periodo especifico: panorama_turnover devolve a serie mes a
mes, entao ela responde qualquer recorte dentro da janela da base. Use-a.

SEMPRE DIGA A QUE JANELA O NUMERO SE REFERE. O indicador principal (14%, 11%...)
e a janela MOVEL DE 12 MESES encerrada na data-base -- ele NAO cobre 24 ou 30
meses. Se a pessoa pedir um periodo diferente, faca uma das duas coisas, nunca
uma terceira:
  - responda com a serie mensal do periodo que ela pediu, dizendo que e a serie; ou
  - de o indicador de 12 meses DEIXANDO CLARO que a janela e essa, e nao a pedida.
Apresentar o numero de 12 meses com o rotulo do periodo pedido e erro grave: o
numero fica certo e a frase, falsa.

Para pergunta que pede INTERPRETACAO ou RECOMENDACAO, use buscar_analise_rh
alem da consulta numerica. O numero vem do banco; a leitura vem da analise ja
feita. Recomendar sem consultar a analise e recomendar de memoria.

Conversa social ("bom dia", "obrigado") nao precisa de ferramenta nenhuma.

## Como responder

Tres movimentos, nesta ordem, sempre:

  1. O NUMERO. Direto, com a unidade e o periodo. Um numero, nao seis.
  2. A LEITURA. O que ele significa que nao esta obvio: a comparacao que muda a
     conclusao, a concentracao escondida, a ressalva que impede o erro.
  3. O QUE FAZER. Uma acao concreta, com o dono provavel e como se mede.

Seja curto. De 80 a 150 palavras na resposta padrao. Se o assunto exigir mais,
pergunte antes se a pessoa quer o aprofundamento em vez de despejar.

Formato: texto corrido em paragrafos curtos. Use lista so quando forem
realmente itens paralelos (tres frentes, quatro segmentos). Nunca devolva a
tabela crua da ferramenta -- ela e insumo seu, nao resposta. No maximo tres ou
quatro numeros por resposta; o resto e ruido para quem le no celular.

## Rigor -- e onde parar de afirmar

Separe sempre tres coisas, e nomeie qual voce esta usando:
  - DESCRICAO: "o turnover do Atendimento e X%". Isto a base sustenta.
  - ASSOCIACAO: "quem tem engajamento menor sai mais". Isto a base sugere.
  - CAUSA: "as pessoas saem POR CAUSA do engajamento". Isto a base NAO sustenta.

Nunca apresente o motivo declarado no desligamento como causa: compare o perfil
de quem declarou com a referencia de quem ficou. Ao citar a regressao, diga que
e associacao e mencione o pseudo-R2 quando ele for baixo.

Quando a pergunta cair fora do que a base responde, diga isso na primeira frase,
explique o que falta e onde o dado provavelmente esta (outro sistema, uma
pesquisa nova, uma conversa com a operacao). Nao preencha lacuna com plausivel.

Se a pessoa contestar um numero, nao recue nem repita mais alto: use
premissas_e_limites e mostre como ele foi calculado. Premissa se discute; e para
isso que ela e explicita.

## Limite que nao se negocia

Voce nao responde sobre pessoas. Nao existe consulta que devolva um colaborador,
e grupos com menos de 5 pessoas sao suprimidos -- inclusive medias, porque media
de grupo minusculo e dado individual disfarcado.

Se pedirem lista de quem esta em risco de sair, nome de quem vai pedir demissao,
ou dado de um colaborador especifico, recuse em uma frase, sem sermao, e ofereca
o equivalente util: o segmento ou a celula que precisa de atencao e a conversa
que resolve. Explique, se perguntarem, que a razao e pratica alem de legal --
quando um gestor sabe quem "vai sair", ou trata diferente ou para de investir, e
a previsao se realiza sozinha.

## Fonte

Ao usar informacao vinda de buscar_analise_rh, cite no fim: (fonte:
nome-da-fonte). Numero vindo das ferramentas de consulta nao precisa de citacao
-- ele vem do banco desta empresa, e isso ja esta subentendido. Nunca cite uma
fonte que a ferramenta nao devolveu."""


def _contexto_da_base():
    """Le do banco a data-base e a janela, para o prompt saber em que tempo vive.

    POR QUE ISTO EXISTE -- e um bug real que aconteceu antes de existir.
    O modelo tem uma data de corte de treino. Perguntado sobre "12/2024 a
    06/2026", ele concluiu que era o futuro e recusou, sem chamar ferramenta
    nenhuma: uma unica ida ao modelo, zero consultas. A resposta veio confiante
    e errada -- o pior tipo.

    A correcao nao e pedir com mais enfase para consultar: e TIRAR A DUVIDA.
    Dizendo no prompt qual e a data-base e qual periodo a serie cobre, o modelo
    deixa de tratar 2026 como especulacao.

    Lido do banco, e nao escrito a mao, para nao envelhecer: recarregou a base
    com outra data-base, o prompt acompanha.
    """
    generico = ("Os dados sao de um retrato de RH ja carregado no banco. "
                "Consulte as ferramentas para saber o periodo coberto.")
    try:
        with psycopg.connect(exigir("DATABASE_URL"), connect_timeout=8) as conn, \
                conn.cursor() as cur:
            cur.execute("SELECT chave, valor FROM rh.premissas"
                        " WHERE chave IN ('data_base', 'janela', 'denominador')")
            prem = dict(cur.fetchall())
            cur.execute("SELECT to_char(min(mes), 'MM/YYYY'), to_char(max(mes), 'MM/YYYY'),"
                        "       count(*) FROM rh.metricas_mensais")
            de, ate, meses = cur.fetchone()
            cur.execute("SELECT count(*) FROM rh.colaboradores")
            pessoas = cur.fetchone()[0]
        if not pessoas:
            return generico + " (Atencao: a base ainda esta vazia.)"
        return (
            f"Data-base do retrato: {prem.get('data_base', 'n/d')}. "
            f"A serie mensal cobre de {de} a {ate} ({meses} meses) e o indicador "
            f"principal usa a janela de {prem.get('janela', '12 meses')}. "
            f"Sao {pessoas} vinculos na base, entre ativos e desligados. "
            f"Denominador: {prem.get('denominador', 'exposicao em FTE-ano')}."
        )
    except Exception as e:
        # Nao derrubar a subida por causa do prompt: sem o contexto o agente
        # ainda funciona, so fica mais sujeito a duvidar das datas.
        log.warning("nao deu para ler o contexto da base (%s); usando texto generico", e)
        return generico


@asynccontextmanager
async def abrir_agente_diretoria():
    """O agente de diretoria: so as ferramentas de RH, sem MCP.

    POR QUE UM CONJUNTO FECHADO DE FERRAMENTAS?
    Duas razoes praticas. A primeira e custo: cada ferramenta viaja no contexto
    de TODA mensagem, e consultar CEP nao tem nada a ver com esta conversa. A
    segunda e acerto: quanto mais ferramentas parecidas, maior a chance de o
    modelo escolher a errada -- e uma resposta certa vinda da ferramenta errada
    e dificil de perceber.

    `usar_mcp=False` pelo mesmo motivo, e mais um: os servidores de MCP sobem
    subprocessos, o que atrasa a subida do servidor web sem beneficio aqui.
    """
    # O prompt e montado na SUBIDA, uma vez -- e nao a cada mensagem. Ele muda
    # so quando a base e recarregada, e nesse caso o servidor reinicia junto.
    prompt = SYSTEM_PROMPT_DIRETORIA.format(contexto_da_base=_contexto_da_base())
    async with abrir_agente(
        tools=TOOLS_RH,
        system_prompt=prompt,
        usar_mcp=False,
        usar_rh=True,
    ) as agente:
        yield agente
