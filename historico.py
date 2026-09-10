# =============================================================================
# historico.py — grava a conversa em texto limpo nas SUAS tabelas
# -----------------------------------------------------------------------------
# DUAS DECISOES DE PRODUCAO MORAM AQUI. Vale entender as duas.
#
# DECISAO 1 — POOL DE CONEXOES, e nao uma conexao unica.
# O jeito facil e abrir UMA conexao no inicio e usar sempre a mesma. Funciona
# perfeitamente ate o dia em que duas requisicoes chegam ao mesmo tempo -- e ai
# as duas mexem na mesma conexao e se atropelam ("another operation is in
# progress"). Um POOL e um conjunto de conexoes prontas: cada uso pega uma
# emprestada e devolve. Requisicoes simultaneas usam conexoes DIFERENTES.
# O guia antigo usava conexao unica e so mencionava o pool na secao "producao",
# no fim -- ou seja, entregava o bug e o conserto em lugares separados.
#
# DECISAO 2 — RODAR O BANCO FORA DO EVENT LOOP (asyncio.to_thread).
# O psycopg aqui e SINCRONO: enquanto ele espera o banco, ele TRAVA a thread.
# Se isso acontecer dentro do event loop, TODAS as outras requisicoes do
# servidor param junto -- inclusive as que nem tocam no banco. Por isso toda
# operacao de banco vai para uma thread, com `asyncio.to_thread` -- o mesmo
# padrao ja usado no `memoria.py`.
# =============================================================================

import asyncio
from contextlib import contextmanager

from psycopg_pool import ConnectionPool

from config import exigir


class Historico:
    """Grava perguntas e respostas nas tabelas `conversas` e `mensagens`."""

    def __init__(self, pool):
        self._pool = pool

    # -- Parte sincrona: o SQL de verdade -------------------------------------
    # Note o `with self._pool.connection() as conn`: pega uma conexao emprestada
    # e DEVOLVE ao sair do bloco, mesmo se der erro. Sem o `with`, uma excecao
    # deixaria a conexao presa fora do pool -- e depois de algumas, o pool seca
    # e a aplicacao trava inteira.

    def _garantir_conversa(self, thread_id, owner):
        """Devolve o id da conversa, criando-a se ainda nao existir."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            # UPSERT em UMA instrucao. O `DO UPDATE SET owner = conversas.owner`
            # parece inutil (grava o valor que ja estava la), e e mesmo -- o
            # truque existe so porque `DO NOTHING` NAO devolve o id da linha que
            # ja existia, e ai seriam necessarias duas idas ao banco.
            cur.execute(
                """
                INSERT INTO conversas (thread_id, owner)
                VALUES (%s, %s)
                ON CONFLICT (thread_id) DO UPDATE SET owner = conversas.owner
                RETURNING id
                """,
                (thread_id, owner),
            )
            return cur.fetchone()[0]

    def _inserir_mensagem(self, conversa_id, papel, conteudo, owner):
        with self._pool.connection() as conn, conn.cursor() as cur:
            # `%s` + tupla de valores = QUERY PARAMETRIZADA. O texto do SQL e os
            # dados viajam SEPARADOS, e o driver garante que o dado nunca vire
            # comando. Se voce montasse com f-string, um conteudo malicioso
            # poderia fechar a string e escrever SQL proprio -- SQL injection.
            # A entrada aqui vem do usuario. NUNCA use f-string em SQL.
            cur.execute(
                "INSERT INTO mensagens (conversa_id, papel, conteudo, owner)"
                " VALUES (%s, %s, %s, %s)",
                (conversa_id, papel, conteudo, owner),
            )

    def _listar(self, thread_id, limite):
        with self._pool.connection() as conn, conn.cursor() as cur:
            # JOIN: as mensagens guardam so o `conversa_id` (um numero). Para
            # filtrar pelo `thread_id` legivel, costuramos as duas tabelas pela
            # coluna em comum. Isso e um JOIN: juntar duas tabelas por um elo.
            cur.execute(
                """
                SELECT m.papel, m.conteudo, m.criada_em
                  FROM mensagens AS m
                  JOIN conversas AS c ON c.id = m.conversa_id
                 WHERE c.thread_id = %s
                 ORDER BY m.criada_em DESC, m.id DESC
                 LIMIT %s
                """,
                (thread_id, limite),
            )
            # Buscamos do mais novo para o mais antigo (o indice esta nessa
            # ordem, entao e rapido) e invertemos aqui, para exibir em ordem
            # cronologica. Inverter uma lista de 50 itens em Python e de graca;
            # fazer o banco varrer a tabela toda nao e.
            return list(reversed(cur.fetchall()))

    # -- Parte async: o que o nucleo chama -------------------------------------

    async def registrar_pergunta(self, thread_id, owner, texto):
        """Grava a PERGUNTA e devolve o id da conversa.

        POR QUE GRAVAR ANTES DE CHAMAR O MODELO?
        Porque se o modelo falhar (API fora do ar, timeout, limite de uso), a
        pergunta do usuario ja esta salva. Voce consegue ver o que foi pedido,
        depurar e ate reprocessar. Se gravassemos so no fim, toda falha
        apagaria a evidencia de que ela existiu.
        """
        conversa_id = await asyncio.to_thread(
            self._garantir_conversa, thread_id, owner
        )
        await asyncio.to_thread(
            self._inserir_mensagem, conversa_id, "user", texto, owner
        )
        return conversa_id

    async def registrar_resposta(self, conversa_id, owner, texto):
        """Grava a RESPOSTA do agente."""
        await asyncio.to_thread(
            self._inserir_mensagem, conversa_id, "assistant", texto, owner
        )

    async def checar(self):
        """Prova que o banco responde. Usado pelo /health do webhook.

        `SELECT 1` e a consulta mais barata que existe: nao le tabela nenhuma.
        O que ela testa de verdade e a CADEIA toda -- pegar conexao do pool,
        falar com o servidor, receber resposta. Se o banco caiu, o pool secou ou
        a senha mudou, esta linha falha.
        """
        def _ping():
            with self._pool.connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
                return cur.fetchone()[0]

        return await asyncio.to_thread(_ping)

    async def listar_mensagens(self, thread_id, limite=50):
        """Devolve as ultimas mensagens da conversa, em ordem cronologica.

        Cada item e uma tupla (papel, conteudo, criada_em).
        """
        return await asyncio.to_thread(self._listar, thread_id, limite)


@contextmanager
def abrir_historico():
    """Abre o pool de conexoes e o fecha no fim."""
    url = exigir("DATABASE_URL", "Rode sql/00_banco_e_usuario.sql.")
    # min_size=1  -> mantem 1 conexao sempre pronta (a 1a requisicao nao espera)
    # max_size=5  -> teto. Conexao e recurso caro no servidor; nao peca infinito.
    # open=False + pool.open() -> abrir no construtor e desaconselhado pelo
    #   psycopg_pool (esconde erro de conexao dentro da criacao do objeto).
    pool = ConnectionPool(url, min_size=1, max_size=5, open=False)
    pool.open()
    try:
        yield Historico(pool)
    finally:
        pool.close()
