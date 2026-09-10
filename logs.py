# =============================================================================
# logs.py — o diario de bordo do servidor
# -----------------------------------------------------------------------------
# POR QUE NAO USAR `print`? Porque `print` nao tem:
#   - HORA      -> "isso aconteceu ontem ou agora?"
#   - GRAVIDADE -> nao da para filtrar so os erros no meio de milhares de linhas
#   - ORIGEM    -> qual parte do sistema falou?
#   - DESLIGAR  -> print voce so desliga editando o codigo
# O modulo `logging`, que vem com o Python, resolve os quatro.
#
# AS GRAVIDADES (do menos para o mais grave):
#   DEBUG    detalhe util so quando voce esta cacando um problema
#   INFO     o fluxo normal ("requisicao recebida", "agente pronto")
#   WARNING  algo torto, mas o sistema seguiu ("chave errada", "limite atingido")
#   ERROR    deu errado de verdade ("nao consegui responder")
# Em producao voce roda em INFO. Quando o problema aparece, baixa para DEBUG
# sem tocar no codigo -- so mudando LOG_NIVEL no .env.
# =============================================================================

import logging
import os
import sys
from contextvars import ContextVar

# -- O ID DE REQUISICAO -------------------------------------------------------
# O PROBLEMA QUE ELE RESOLVE: com tres usuarios falando ao mesmo tempo, as
# linhas de log se misturam e voce nao sabe qual pertence a qual conversa:
#
#   10:31:02 INFO  requisicao recebida
#   10:31:02 INFO  requisicao recebida
#   10:31:03 WARN  limite atingido        <- de QUEM?
#
# Com um id, cada linha se identifica:
#
#   10:31:02 INFO  [a1b2c3d4] requisicao recebida
#   10:31:02 INFO  [9f8e7d6c] requisicao recebida
#   10:31:03 WARN  [9f8e7d6c] limite atingido      <- agora da para seguir
#
# `ContextVar` e uma variavel que tem um valor DIFERENTE para cada tarefa
# assincrona em andamento. E o que permite que duas requisicoes rodando ao
# mesmo tempo tenham, cada uma, o seu proprio id -- sem uma pisar na outra.
id_requisicao = ContextVar("id_requisicao", default="-")


class _FiltroId(logging.Filter):
    """Injeta o id da requisicao em toda linha de log.

    Um `Filter` do logging pode fazer duas coisas: barrar linhas, ou ENRIQUECE-
    las. Usamos a segunda: acrescentamos o campo `req`, que o formato abaixo
    usa. Assim nenhum `log.info(...)` espalhado pelo codigo precisa lembrar de
    passar o id -- ele entra sozinho.
    """

    def filter(self, record):
        record.req = id_requisicao.get()
        return True     # True = deixa a linha passar


def configurar():
    """Liga o logging do projeto inteiro. Chamado uma vez, na subida."""
    nivel = os.environ.get("LOG_NIVEL", "INFO").upper()

    manipulador = logging.StreamHandler(sys.stdout)
    manipulador.addFilter(_FiltroId())
    manipulador.setFormatter(
        logging.Formatter(
            # asctime=hora, levelname=gravidade, req=id da requisicao,
            # name=quem falou (webhook, seguranca...), message=o texto.
            "%(asctime)s %(levelname)-7s [%(req)s] %(name)-11s %(message)s",
            datefmt="%H:%M:%S",
        )
    )

    raiz = logging.getLogger()
    # Tira manipuladores antigos: o uvicorn instala os dele, e sem esta linha
    # cada mensagem sairia DUAS vezes (uma por manipulador).
    raiz.handlers.clear()
    raiz.addHandler(manipulador)
    raiz.setLevel(nivel)

    # O log de acesso do uvicorn ja diz "POST /webhook 200". A gente registra a
    # mesma coisa com MAIS informacao (duracao, id), entao desligamos o dele
    # para nao ver tudo em dobro.
    logging.getLogger("uvicorn.access").disabled = True

    # As mensagens restantes do uvicorn (subiu, desceu, erro) passam a usar o
    # NOSSO formato. `handlers.clear()` tira o manipulador proprio dele e
    # `propagate = True` manda as mensagens subirem para a raiz, que e nossa.
    # Sem isso, metade do log sai num formato e metade em outro.
    for nome in ("uvicorn", "uvicorn.error"):
        registrador = logging.getLogger(nome)
        registrador.handlers.clear()
        registrador.propagate = True

    # Bibliotecas falantes. O cliente HTTP registra uma linha INFO para CADA
    # chamada a OpenAI ("HTTP Request: POST ... 200 OK") -- informacao que a
    # nossa propria linha ja da, com mais contexto. Subimos o nivel delas para
    # WARNING: continuam avisando quando algo der errado, e calam no resto.
    # (o "httpx2" nao e engano: o SDK da OpenAI v3 traz uma copia propria do
    # httpx com esse nome. Descobrimos listando logging.root.manager.loggerDict.)
    for ruidosa in ("httpx", "httpx2", "httpcore", "openai", "urllib3"):
        logging.getLogger(ruidosa).setLevel(logging.WARNING)
