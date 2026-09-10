# =============================================================================
# middleware_erros.py — nenhuma ferramenta pode derrubar o agente
# -----------------------------------------------------------------------------
# O PROBLEMA. As ferramentas do `tools/` tratam os erros que a gente PREVIU:
# timeout, rede fora, resposta estranha. Mas e o erro que ninguem previu? Um
# `KeyError` porque a API mudou um campo, uma divisao por zero, uma biblioteca
# que estourou. Sem protecao, essa excecao sobe pelo agente e derruba a
# requisicao inteira -- o usuario recebe HTTP 500 e perde a conversa.
#
# A REDE DE SEGURANCA. Um MIDDLEWARE que envolve TODA chamada de ferramenta:
#
#     agente -> [middleware] -> ferramenta
#                    |
#              se estourar, vira uma mensagem de erro em vez de subir
#
# Ele transforma a excecao numa `ToolMessage` -- que e como a ferramenta
# "responde" ao modelo. Ou seja: o modelo LE o erro, entende que a ferramenta
# nao funcionou, e responde ao usuario com jeito ("nao consegui consultar
# agora"). O agente continua vivo.
#
# ISTO NAO SUBSTITUI o tratamento de erro dentro de cada ferramenta. Sao dois
# niveis: a ferramenta trata o que ela CONHECE (e da mensagem util); o
# middleware pega o que ninguem conhecia. Rede de seguranca de circo nao
# substitui o trapezista saber cair -- ela existe para quando ele erra.
# =============================================================================

import logging

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

log = logging.getLogger("tools")


class TratarErrosDeTool(AgentMiddleware):
    """Captura qualquer excecao de ferramenta e devolve uma mensagem ao modelo.

    POR QUE UMA SUBCLASSE, e nao um decorador?
    Porque o nosso agente e ASSINCRONO (Passo 01) e chama `ainvoke`. O LangGraph
    procura o metodo `awrap_tool_call` -- a versao async. Se voce so escrever a
    versao sincrona, o agente estoura com
    `NotImplementedError: awrap_tool_call ... not available`.
    A subclasse permite escrever as DUAS, e ai o mesmo middleware serve tanto
    para um agente sincrono quanto para o nosso.
    """

    def _virar_mensagem(self, request, erro):
        """Transforma a excecao numa resposta que o modelo consegue ler."""
        nome = request.tool_call.get("name", "desconhecida")

        # `log.exception` grava a PILHA COMPLETA no servidor -- e onde voce vai
        # depurar depois. Sem esta linha, o erro sumiria: o modelo receberia um
        # aviso educado e voce nunca saberia que algo quebrou.
        log.exception("a ferramenta %r falhou", nome)

        # Para o MODELO vai so o essencial: qual ferramenta e que tipo de erro.
        # Nada de pilha nem de caminho de arquivo -- o modelo pode repetir isso
        # ao usuario, e detalhe tecnico vazado e material de reconhecimento.
        return ToolMessage(
            content=(
                f"A ferramenta '{nome}' falhou ({type(erro).__name__}). "
                "Explique ao usuario que nao foi possivel consultar agora e "
                "sugira tentar mais tarde. Nao invente o resultado."
            ),
            # `tool_call_id` AMARRA esta resposta ao pedido certo. Sem ele, o
            # modelo fica com um pedido de ferramenta sem resposta -- e a OpenAI
            # RECUSA a proxima chamada com "tool_calls sem resposta".
            tool_call_id=request.tool_call["id"],
        )

    def wrap_tool_call(self, request, handler):
        """Versao SINCRONA (usada por um agente com `.invoke`)."""
        try:
            return handler(request)
        except Exception as e:
            return self._virar_mensagem(request, e)

    async def awrap_tool_call(self, request, handler):
        """Versao ASSINCRONA -- e a que o NOSSO agente usa."""
        try:
            return await handler(request)
        except Exception as e:
            return self._virar_mensagem(request, e)


# Uma instancia pronta, para o nucleo so importar e usar.
tratar_erros_de_tool = TratarErrosDeTool()


# =============================================================================
# LIMITE DE FERRAMENTAS POR REQUISICAO
# -----------------------------------------------------------------------------
# DESCOBERTO TESTANDO O PASSO 17. O `ModelCallLimitMiddleware` conta CHAMADAS AO
# MODELO, e a gente imaginava que isso limitasse o gasto de uma requisicao. Mas
# medindo, o modelo fez isto ao receber 6 CNPJs:
#
#   [1] AI: pediu 6 chamadas DE UMA VEZ   <- chamada paralela de ferramentas
#   [2..7] Tool: as 6 respostas
#   [8] AI: a resposta final
#
# Duas chamadas ao modelo. Seis execucoes de ferramenta. Ou seja:
#   `run_limit` limita a PROFUNDIDADE (quantas idas e voltas)
#   e NAO limita a LARGURA (quantas ferramentas de uma vez).
#
# A largura importa porque cada execucao bate numa API externa (que pode ter
# limite de uso proprio) e cada resposta entra no contexto -- e voce paga por
# ela. Um pedido com 200 CNPJs viraria 200 requisicoes a BrasilAPI numa tacada.
#
# Este middleware conta as execucoes DENTRO de uma requisicao e recusa o excesso.
# =============================================================================

import os
from contextvars import ContextVar

# Contador por requisicao.
#
# ⚠️ REPARE QUE O ContextVar GUARDA UMA LISTA, e nao um numero. Isso nao e
# firula -- a primeira versao guardava um `int` e NAO FUNCIONOU. O motivo:
# as ferramentas paralelas rodam em TAREFAS separadas, e cada tarefa herda uma
# COPIA do contexto. Um `_usadas.set(n)` feito dentro de uma tarefa filha nao
# aparece nas irmas, entao cada uma contava do zero e o teto nunca era atingido.
#
# Guardando uma LISTA, o que e herdado e a REFERENCIA ao mesmo objeto. Mutar a
# lista (`contador[0] += 1`) e visto por todas as tarefas -- que e o que a gente
# precisa. Regra geral: para compartilhar estado entre tarefas filhas, o
# ContextVar tem que guardar um objeto MUTAVEL, nao um valor.
_usadas = ContextVar("ferramentas_usadas")


def zerar_contador():
    """Chamado pelo nucleo no inicio de cada requisicao."""
    _usadas.set([0])


class LimiteDeFerramentas(AgentMiddleware):
    """Recusa execucoes de ferramenta acima do teto, dentro de uma requisicao."""

    def _checar(self, request):
        """Devolve uma ToolMessage de recusa, ou None se pode executar."""
        teto = int(os.environ.get("MAX_FERRAMENTAS_POR_REQUISICAO", 12))
        try:
            contador = _usadas.get()
        except LookupError:
            # Ninguem chamou zerar_contador() -- acontece se a ferramenta for
            # usada fora do nucleo. Cria um contador local em vez de quebrar.
            contador = [0]
            _usadas.set(contador)

        contador[0] += 1
        if contador[0] <= teto:
            return None

        nome = request.tool_call.get("name", "desconhecida")
        log.warning("limite de ferramentas atingido (%d) ao chamar %r", teto, nome)
        # Recusamos devolvendo uma mensagem, e nao levantando erro: assim o
        # modelo LE a recusa e consegue responder ao usuario com o que ja tem.
        return ToolMessage(
            content=(
                f"Limite de {teto} consultas por mensagem atingido. "
                "Responda ao usuario com o que ja foi consultado e peca para "
                "ele dividir o pedido em partes menores."
            ),
            tool_call_id=request.tool_call["id"],
        )

    def wrap_tool_call(self, request, handler):
        return self._checar(request) or handler(request)

    async def awrap_tool_call(self, request, handler):
        recusa = self._checar(request)
        return recusa if recusa is not None else await handler(request)


limite_de_ferramentas = LimiteDeFerramentas()
