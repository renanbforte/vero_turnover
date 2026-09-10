# =============================================================================
# memoria.py — o checkpointer persistente (PostgreSQL) que funciona com async
# -----------------------------------------------------------------------------
# ESTE ARQUIVO EXISTE POR CAUSA DE UM PROBLEMA REAL, E VALE ENTENDER O PROBLEMA
# ANTES DO CODIGO.
#
# 1) Nosso agente e ASYNC (Passo 01), entao chamamos `await agente.ainvoke(...)`.
#    Um agente async exige um checkpointer que tenha metodos async
#    (`aget_tuple`, `aput`, ...). O `PostgresSaver` normal so tem os sincronos ->
#    da `NotImplementedError`.
#
# 2) Existe um `AsyncPostgresSaver`, que resolveria isso. Mas ele usa o psycopg
#    em modo assincrono, e o psycopg async NO WINDOWS exige o `SelectorEventLoop`.
#    Ja os servidores MCP (Bloco 4) sobem SUBPROCESSOS, e subprocesso no Windows
#    exige o `ProactorEventLoop`. Os dois nao convivem: escolher um quebra o
#    outro. (Erro real: "Psycopg cannot use the 'ProactorEventLoop'".)
#
# A SAIDA. O conflito e sobre o psycopg viver DENTRO do event loop. Entao a
# gente tira ele de la: usamos o `PostgresSaver` SINCRONO e rodamos cada chamada
# dele numa THREAD separada, com `asyncio.to_thread`. O event loop continua
# livre (e continua sendo o Proactor, que o MCP precisa), o psycopg roda fora
# dele, e ninguem briga.
#
# Resultado: memoria persistente de verdade + MCP + async, os tres juntos.
# O guia antigo desistia aqui e trocava o banco por memoria RAM no Passo 21 --
# ou seja, perdia a persistencia conquistada la no Passo 04. Nao precisa.
#
# E SEGURO RODAR EM THREADS? Sim: o proprio `PostgresSaver` tem um
# `threading.Lock` interno e serializa o acesso a conexao. (Confira voce mesmo:
# `inspect.getsource(PostgresSaver)` mostra o `self.lock`.)
# =============================================================================

import asyncio
from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from config import exigir


def _delegar(nome_sync):
    """Fabrica um metodo ASYNC que roda o metodo SINCRONO numa thread.

    `asyncio.to_thread(f, *args)` entrega `f` para uma thread do pool, devolve
    na hora o controle ao event loop, e o `await` acorda quando a thread termina.
    """
    async def metodo(self, *args, **kwargs):
        return await asyncio.to_thread(getattr(self, nome_sync), *args, **kwargs)
    return metodo


def _delegar_gerador(nome_sync):
    """Igual ao _delegar, mas para metodos que devolvem um GERADOR (o `list`).

    Nao da para "passear" por um gerador de dentro de outra thread aos poucos,
    entao materializamos tudo de uma vez (`list(...)`) dentro da thread e depois
    entregamos item a item. Sao listas de checkpoints -- pequenas.
    """
    async def metodo(self, *args, **kwargs):
        itens = await asyncio.to_thread(
            lambda: list(getattr(self, nome_sync)(*args, **kwargs))
        )
        for item in itens:
            yield item
    return metodo


class PostgresSaverEmThread(PostgresSaver):
    """PostgresSaver com os metodos async faltantes, delegando para threads."""

    # Os 10 metodos async que o LangGraph pode chamar. Escrevemos assim, em
    # tabela, em vez de 10 funcoes iguais: a ideia e UMA so (delegar para thread)
    # e fica obvio que nenhum ficou de fora. Se uma versao futura do LangGraph
    # acrescentar um metodo, voce so adiciona o nome aqui.
    aget = _delegar("get")
    aget_tuple = _delegar("get_tuple")
    aput = _delegar("put")
    aput_writes = _delegar("put_writes")
    adelete_thread = _delegar("delete_thread")
    adelete_for_runs = _delegar("delete_for_runs")
    acopy_thread = _delegar("copy_thread")
    aget_delta_channel_history = _delegar("get_delta_channel_history")
    aprune = _delegar("prune")
    alist = _delegar_gerador("list")


@contextmanager
def abrir_checkpointer(criar_tabelas=True):
    """Abre a conexao do checkpointer e a fecha certinho no fim.

    Uso:
        with abrir_checkpointer() as checkpointer:
            agente = create_agent(..., checkpointer=checkpointer)

    O `with` garante que a conexao com o banco seja FECHADA mesmo se der erro
    no meio. Regra: tudo que usa o checkpointer fica DENTRO do `with`.

    `criar_tabelas=True` chama o `setup()`, que cria as tabelas internas do
    checkpointer se ainda nao existirem (e idempotente: rodar de novo nao
    estraga nada).
    EM PRODUCAO: passe `criar_tabelas=False` e rode o `setup()` UMA vez, no
    deploy. Aplicacao que mexe no schema toda vez que sobe e receita de duas
    instancias criando a mesma tabela ao mesmo tempo.
    """
    url = exigir(
        "DATABASE_URL",
        "Rode `uv run python scripts/preparar.py` para criar o banco e o usuario.",
    )
    # POOL, e nao uma conexao unica. O atalho seria
    # `PostgresSaverEmThread.from_conn_string(url)`, que abre UMA conexao com uma
    # trava interna: funciona, mas faz requisicoes simultaneas entrarem em fila
    # umas atras das outras so para ler a memoria. Com um pool, cada uma pega a
    # sua conexao. Isso passa a importar de verdade no Bloco 2, onde o servidor
    # atende varios usuarios ao mesmo tempo.
    #
    # Os tres `kwargs` NAO sao opcionais -- e o que o `from_conn_string` fazia
    # por baixo dos panos e que agora somos nos a fornecer:
    #   autocommit=True      -> o checkpointer grava sem transacao explicita
    #   prepare_threshold=0  -> desliga prepared statements (atrapalham no pool)
    #   row_factory=dict_row -> devolve linhas como dicionario, formato que o
    #                           LangGraph espera
    pool = ConnectionPool(
        url,
        min_size=1,
        max_size=5,
        open=False,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
    )
    pool.open()
    try:
        checkpointer = PostgresSaverEmThread(pool)
        if criar_tabelas:
            checkpointer.setup()
        yield checkpointer
    finally:
        pool.close()
