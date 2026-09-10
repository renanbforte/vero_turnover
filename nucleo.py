# =============================================================================
# nucleo.py — O AGENTE, separado de QUEM fala com ele
# -----------------------------------------------------------------------------
# ESTE E O ARQUIVO MAIS IMPORTANTE DO PROJETO. Vale ler devagar.
#
# O PROBLEMA QUE ELE RESOLVE (um erro real, do guia antigo):
# La, existiam dois arquivos -- `agente.py` (terminal) e `webhook.py` (HTTP).
# Cada um montava o agente, normalizava a identidade, montava o config, gravava
# no banco... e as MESMAS ~80 linhas estavam COPIADAS nos dois. Consequencias:
#   - corrigir um bug exigia lembrar de corrigir em dois lugares;
#   - os dois foram divergindo (um tinha tratamento de erro que o outro nao);
#   - uma regra de seguranca aplicada num canal ficava faltando no outro.
# Esse ultimo item e o perigoso: seguranca que depende de voce lembrar nao e
# seguranca.
#
# A SOLUCAO: separar NUCLEO de CANAL.
#   NUCLEO (este arquivo) = o agente e as regras. Nao sabe se quem chamou foi um
#                           terminal, um webhook, o n8n ou o WhatsApp.
#   CANAL  (passos/, e depois o webhook) = so traduz. Recebe do mundo, chama o
#                           nucleo, devolve para o mundo.
#
# Toda regra que precisa valer SEMPRE (limpar identidade, limitar tamanho da
# entrada, isolar por thread) mora aqui. Assim, um canal novo -- amanha um bot
# do Telegram -- ja nasce com todas elas, de graca.
#
#          n8n / WhatsApp  ─┐
#          chat web        ─┼─>  webhook.py  ─┐
#          terminal        ─────────────────  ┼─>  nucleo.py  ─>  modelo + banco
#                                             ┘
# =============================================================================

import asyncio
import logging
import os
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass

from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    SummarizationMiddleware,
)
from langchain_core.callbacks import UsageMetadataCallbackHandler

from config import MODELO, exigir
from custos import abrir_custos
from historico import abrir_historico
from identidade import definir_identidade_atual, normalizar_identidade
from memoria import abrir_checkpointer
from middleware_erros import (
    limite_de_ferramentas,
    tratar_erros_de_tool,
    zerar_contador,
)
from rag import abrir_rag
from rh_consultas import abrir_rh
from tools import TOOLS, definir_rag, definir_rh

log = logging.getLogger("nucleo")

# Limite de tamanho da mensagem de entrada.
# POR QUE LIMITAR? Voce paga por token. Sem limite, uma unica requisicao com um
# livro inteiro colado dentro custa caro e ainda pode estourar a janela de
# contexto do modelo. 4000 caracteres cobre com folga uma mensagem de WhatsApp
# (que vai ate ~4096). Quem manda texto e a internet: sempre limite.
MAX_CARACTERES = 4000

# Tempo maximo, em segundos, esperando o modelo responder.
# POR QUE ISTO E OBRIGATORIO EM PRODUCAO. Sem timeout, uma chamada travada da
# OpenAI trava a requisicao PARA SEMPRE. O n8n fica pendurado esperando, a
# conexao do banco fica ocupada, e o problema so aparece quando o servidor nao
# responde mais nada. "Esperar para sempre" nao e paciencia, e um bug.
# 60s e generoso: uma resposta normal leva ~1s aqui. Quem passa disso travou.
def _timeout():
    try:
        return float(os.environ.get("TIMEOUT_MODELO", 60))
    except ValueError:
        return 60.0


SYSTEM_PROMPT_PADRAO = (
    "Voce e um assistente prestativo que responde em portugues do Brasil. "
    "Seja claro e direto. "
    # A linha abaixo entra no Passo 13. Ela NAO lista as ferramentas uma a uma
    # (isso o LangChain ja faz por baixo, usando a docstring de cada uma); ela
    # so instrui o comportamento: prefira consultar a chutar.
    "Quando uma ferramenta puder responder com precisao, use a ferramenta em "
    "vez de responder de memoria. "
    # A instrucao de CITAR entra no Passo 24. Ela vale para a resposta ao
    # usuario, e e o que permite alguem CONFERIR o que o agente disse. Sem
    # citacao, uma resposta correta e uma inventada sao indistinguiveis.
    "Ao usar informacao vinda dos documentos internos, CITE a fonte no fim da "
    "resposta, no formato: (fonte: nome-do-arquivo). Se a informacao veio de "
    "mais de um documento, cite todos. Nunca cite uma fonte que a ferramenta "
    "nao devolveu."
)


def _limite_de_chamadas():
    """Limita quantas vezes o modelo pode ser chamado numa UNICA requisicao.

    O BURACO QUE ISTO TAPA. O rate limit do Passo 09 conta REQUISICOES por
    minuto. Mas uma unica requisicao pode custar por 40: basta o modelo entrar
    num laco de ferramentas.
        usuario: "consulte estes 40 CNPJs"
          -> chama buscar_cnpj, volta ao modelo
          -> chama buscar_cnpj, volta ao modelo   (x40)
    Isso conta como 1 no rate limit e custa como 40.

    `run_limit` conta as chamadas ao modelo DENTRO de uma execucao.
    `exit_behavior="end"` faz o agente parar e responder com o que tem, em vez
    de estourar um erro -- o usuario recebe uma resposta parcial, que e melhor
    que nenhuma.
    """
    return ModelCallLimitMiddleware(
        run_limit=int(os.environ.get("MAX_CHAMADAS_MODELO", 8)),
        exit_behavior="end",
    )


def _sumarizacao():
    """Monta o middleware que RESUME a conversa quando ela fica grande.

    O PROBLEMA QUE ELE RESOLVE. O modelo nao lembra de nada sozinho: a cada
    pergunta, o historico INTEIRO e reenviado. Numa conversa com 60 mensagens,
    voce paga pelas 60 -- toda vez. E, passando do limite de contexto do modelo,
    a chamada simplesmente falha.
    (Veja isso acontecendo: `uv run python -m passos.revisao_fluxo renan "ok"`.)

    A SOLUCAO. Quando o historico passa de um tamanho, um middleware pede ao
    modelo para RESUMIR as mensagens antigas num paragrafo, joga fora o detalhe
    e mantem as ultimas N intactas. O agente continua sabendo do que se falou,
    sem carregar cada palavra.

    ⚠️ A ARMADILHA: `keep` PRECISA ser menor que o que o `trigger` acumula.
    O padrao do `keep` e 20 MENSAGENS. Se o gatilho disparar quando houver o
    equivalente a 10 mensagens, nao ha nada para descartar -- ele dispara e nao
    compacta nada. E a causa n1 de "liguei a sumarizacao e nao vi diferenca".
    Por isso nosso padrao e 6, bem abaixo do que 3000 tokens costumam acumular.
    """
    return SummarizationMiddleware(
        # Quem ESCREVE o resumo. Pode ser um modelo mais barato que o principal:
        # resumir e tarefa simples, e assim voce economiza no proprio economizar.
        model=MODELO,
        # QUANDO resumir. Em tokens, porque token e o que voce paga -- contar
        # mensagens engana (uma resposta de ferramenta pode valer 20 mensagens
        # curtas).
        trigger=("tokens", int(os.environ.get("SUMARIZAR_ACIMA_DE", 3000))),
        # QUANTO manter intacto depois de resumir. As mais recentes, que sao as
        # que importam para a proxima resposta.
        keep=("messages", int(os.environ.get("SUMARIZACAO_MANTER", 6))),
    )


class EntradaInvalida(ValueError):
    """A mensagem recebida nao passou na validacao.

    Existe como classe PROPRIA para o canal poder distinguir 'o usuario mandou
    algo errado' (culpa do cliente -> HTTP 400) de 'deu ruim aqui dentro'
    (culpa do servidor -> HTTP 500). No Bloco 2 isso vira codigo de status.
    """


class TetoDiarioAtingido(Exception):
    """O gasto de hoje passou do teto configurado.

    So e levantada quando TETO_DIARIO_MODO=bloquear. No modo "avisar" (padrao),
    o teto vira apenas um alerta no log.
    """


class TempoEsgotado(TimeoutError):
    """O modelo demorou demais.

    Excecao propria (e nao o TimeoutError cru) para o canal poder devolver o
    codigo HTTP certo: 504 Gateway Timeout. Isso diz ao n8n "eu nao consegui a
    tempo, pode tentar de novo" -- diferente de 400 ("sua requisicao esta
    errada, nao adianta insistir").
    """


@dataclass
class Resposta:
    """O que o nucleo devolve.

    POR QUE UM OBJETO E NAO SO UMA STRING? Porque o webhook vai precisar
    devolver mais do que o texto (quem era, qual conversa), e o n8n vai querer
    esses campos no JSON. Devolver um objeto agora evita mudar a assinatura da
    funcao depois -- e mudanca de assinatura quebra todo mundo que chama.
    """
    texto: str
    identidade: str
    thread_id: str
    # O consumo desta mensagem. Ja media vamos no Passo 17 para gravar no banco;
    # devolver junto e de graca e permite mostrar o custo na hora (o chat usa).
    tokens_entrada: int = 0
    tokens_saida: int = 0


class Agente:
    """O agente pronto para responder. Monte UMA vez, use MUITAS."""

    def __init__(self, checkpointer, historico, custos, tools=None, system_prompt=None):
        exigir("OPENAI_API_KEY", "Pegue em https://platform.openai.com/api-keys")
        self._historico = historico
        self._custos = custos
        self._agente = create_agent(
            model=MODELO,
            # As ferramentas que o modelo PODE escolher chamar.
            # `tools or []` permite criar um Agente SEM ferramenta nenhuma
            # (passando tools=[]), o que e util para comparar os dois no teste.
            tools=tools or [],
            system_prompt=system_prompt or SYSTEM_PROMPT_PADRAO,
            checkpointer=checkpointer,
            # A rede de seguranca das ferramentas (Passo 15). Fica AQUI, no
            # nucleo, e nao em cada ferramenta: assim toda ferramenta nova ja
            # nasce protegida, sem ninguem precisar lembrar.
            # A ORDEM IMPORTA: o tratamento de erro envolve as ferramentas;
            # a sumarizacao mexe no historico antes de falar com o modelo.
            middleware=[
                tratar_erros_de_tool,
                limite_de_ferramentas,
                _limite_de_chamadas(),
                _sumarizacao(),
            ],
        )

    async def checar_banco(self):
        """Repassa a checagem de saude ao historico.

        O canal (webhook) nao conhece o `historico` -- ele so conhece o Agente.
        Manter essa porta aqui evita que o webhook precise importar e abrir
        recursos por conta propria, o que quebraria a separacao nucleo x canal.
        """
        return await self._historico.checar()

    async def responder(self, identidade_bruta, texto, callbacks=None):
        """Recebe QUEM falou e O QUE falou. Devolve a resposta.

        Esta e a UNICA porta de entrada do agente. Toda validacao acontece aqui,
        antes de qualquer coisa cara (chamada de modelo) acontecer.
        """
        # -- 1. Limpar a identidade -------------------------------------------
        # Entrada externa se limpa na porta, uma vez.
        identidade = normalizar_identidade(identidade_bruta)

        # -- 2. Validar a mensagem --------------------------------------------
        # Validar ANTES de gastar: nenhuma dessas situacoes chega a chamar a API.
        texto = str(texto).strip()
        if not texto:
            raise EntradaInvalida("A mensagem esta vazia.")
        if len(texto) > MAX_CARACTERES:
            raise EntradaInvalida(
                f"A mensagem tem {len(texto)} caracteres; o limite e {MAX_CARACTERES}."
            )

        # -- 3. Isolar a conversa ---------------------------------------------
        # thread_id derivado da identidade -- NUNCA fixo. Ver Passo 03.
        thread_id = identidade
        config = {"configurable": {"thread_id": thread_id}}

        # -- 3b. O gasto de hoje passou do teto? -------------------------------
        # Checado ANTES da chamada cara, e depois da validacao (que e de graca).
        passou, gasto, teto = await self._custos.verificar_teto()
        if passou:
            modo = os.environ.get("TETO_DIARIO_MODO", "avisar").lower()
            log.warning(
                "TETO DIARIO ULTRAPASSADO: US$ %.4f de US$ %.2f (modo=%s)",
                gasto, teto, modo,
            )
            if modo == "bloquear":
                raise TetoDiarioAtingido(
                    f"O limite de gasto diario (US$ {teto:.2f}) foi atingido."
                )

        # -- 4. Gravar a PERGUNTA (antes de chamar o modelo) -------------------
        # Se o modelo falhar depois desta linha, a pergunta ja esta salva: da
        # para ver o que foi pedido, depurar e ate reprocessar. Gravar so no fim
        # apagaria a evidencia de toda requisicao que falhou.
        conversa_id = await self._historico.registrar_pergunta(
            thread_id, identidade, texto
        )

        # -- 5. Perguntar ao agente (com prazo) --------------------------------
        # `asyncio.wait_for` cancela a tarefa se ela passar do tempo. Sem isso,
        # uma chamada travada segura a requisicao indefinidamente.
        # A pergunta JA foi gravada no passo 4, entao mesmo estourando o prazo
        # fica o registro -- e ela aparece na consulta "perguntas sem resposta".
        # O contador acompanha TODAS as chamadas ao modelo desta execucao --
        # inclusive as do laco de ferramentas e a da sumarizacao. Somar o
        # `usage_metadata` das mensagens nao funcionaria: a lista traz o
        # historico inteiro, e voce contaria as chamadas antigas de novo.
        # Zera o contador de ferramentas DESTA requisicao (ver middleware_erros).
        zerar_contador()

        # Diz as ferramentas QUEM esta falando. A busca nos documentos usa isso
        # para nao vazar dado de outro usuario -- e o modelo nao tem como mexer
        # neste valor, que e exatamente o ponto. Ver `identidade.py`.
        definir_identidade_atual(identidade)

        contador = UsageMetadataCallbackHandler()
        try:
            resultado = await asyncio.wait_for(
                self._agente.ainvoke(
                    {"messages": [{"role": "user", "content": texto}]},
                    # `callbacks` extras permitem que quem chamou acompanhe a
                    # execucao ao vivo -- e o que o chat de terminal usa para
                    # mostrar as ferramentas sendo chamadas.
                    {**config, "callbacks": [contador, *(callbacks or [])]},
                ),
                timeout=_timeout(),
            )
        except (TimeoutError, asyncio.TimeoutError) as e:
            raise TempoEsgotado(
                f"O modelo nao respondeu em {_timeout():.0f}s."
            ) from e

        texto_resposta = resultado["messages"][-1].content

        # -- 6. Gravar a RESPOSTA ---------------------------------------------
        await self._historico.registrar_resposta(
            conversa_id, identidade, texto_resposta
        )

        # -- 7. Gravar o CONSUMO ----------------------------------------------
        # `usage_metadata` vem como {modelo: {input_tokens, output_tokens...}}.
        # Pode haver mais de um modelo numa execucao (o principal e o que
        # escreve o resumo), entao gravamos uma linha por modelo.
        for modelo, uso in (contador.usage_metadata or {}).items():
            await self._custos.registrar(
                identidade,
                thread_id,
                uso.get("input_tokens", 0),
                uso.get("output_tokens", 0),
                modelo,
                chamadas=sum(
                    1 for m in resultado["messages"]
                    if type(m).__name__ == "AIMessage"
                ),
            )

        entrada = sum(u.get("input_tokens", 0) for u in (contador.usage_metadata or {}).values())
        saida = sum(u.get("output_tokens", 0) for u in (contador.usage_metadata or {}).values())
        return Resposta(
            texto=texto_resposta,
            identidade=identidade,
            thread_id=thread_id,
            tokens_entrada=entrada,
            tokens_saida=saida,
        )


@asynccontextmanager
async def abrir_agente(tools=None, system_prompt=None, usar_mcp=None, usar_rh=False):
    """Abre o agente com as ferramentas do pacote `tools/` MAIS as do MCP.

    ⚠️ VIROU ASSINCRONO no Passo 19, e por um motivo so: carregar as ferramentas
    de MCP exige `await` (elas vem de subprocessos que precisam subir). Quem
    chama passa a usar `async with` em vez de `with`.

    Passe `tools=[]` para abrir sem as ferramentas proprias, e `usar_mcp=False`
    para abrir sem as de MCP -- e o que os testes fazem para comparar.

    `usar_rh=True` abre TAMBEM o pool de leitura dos dados de RH (schema `rh`).
    Fica OPT-IN de proposito: quem nao rodou `sql/05_rh.sql` continua subindo o
    agente normalmente. Ligar por padrao faria o projeto inteiro passar a exigir
    tabelas que a maior parte dos passos do guia nem usa.
    """
    """Abre o agente (com a conexao do banco) e fecha tudo no fim.

    Uso, igual em qualquer canal:

        with abrir_agente() as agente:
            resposta = await agente.responder("renan", "ola")

    No webhook (Bloco 2) este mesmo `with` vai ficar no "lifespan" do FastAPI:
    abre quando o servidor sobe, fecha quando ele desce.
    """
    # Dois recursos, dois `with` aninhados. Ambos sao fechados na ordem inversa
    # da abertura, mesmo se der erro no meio.
    # `if tools is None` e diferente de `if not tools`: o primeiro distingue
    # "nao me disseram nada" (usa o padrao) de "me disseram lista vazia"
    # (respeita: nenhuma ferramenta). Com `not tools`, uma lista vazia seria
    # confundida com ausencia e o padrao entraria sem querer.
    if tools is None:
        tools = TOOLS
    ferramentas = list(tools)

    # MCP pode ser desligado por variavel de ambiente: util quando voce nao tem
    # internet, ou quando quer subir rapido sem esperar os servidores.
    if usar_mcp is None:
        usar_mcp = os.environ.get("MCP_ATIVO", "true").strip().lower() == "true"
    if usar_mcp:
        # Carregado UMA vez, na abertura do agente -- e nao a cada mensagem.
        # No webhook isso acontece na subida do servidor (o `lifespan`).
        # Import preguicoso: `tools/mcp.py` nao existe neste repositorio (ver
        # tools/__init__.py). Deixar o import aqui dentro faz o modulo carregar
        # sem ele, e so quem chamar o agente GERAL descobre que falta -- e
        # ninguem chama: este servidor sobe o agente de diretoria.
        from tools.mcp import criar_tools_mcp
        ferramentas += await criar_tools_mcp()

    # ExitStack em vez de `with` aninhado: com ele da para abrir um recurso
    # CONDICIONALMENTE (o de RH) sem duplicar o `yield` num if/else. Todos sao
    # fechados na ordem inversa da abertura, igual ao `with` aninhado -- inclusive
    # se der erro no meio.
    with ExitStack() as pilha:
        checkpointer = pilha.enter_context(abrir_checkpointer())
        historico = pilha.enter_context(abrir_historico())
        custos = pilha.enter_context(abrir_custos())
        rag = pilha.enter_context(abrir_rag())

        # As ferramentas nao abrem conexao propria: recebem o que ja foi aberto
        # aqui. Assim existe UM pool de cada, fechado no fim do `with`.
        definir_rag(rag)
        if usar_rh:
            definir_rh(pilha.enter_context(abrir_rh()), rag)

        yield Agente(
            checkpointer, historico, custos,
            tools=ferramentas, system_prompt=system_prompt,
        )
