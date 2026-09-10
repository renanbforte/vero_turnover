# =============================================================================
# rag.py — buscar nos SEUS documentos
# -----------------------------------------------------------------------------
# O PROBLEMA. O modelo sabe muita coisa geral e NADA sobre voce: seu contrato,
# seu manual, sua tabela de precos. E nao adianta "mandar tudo junto" na
# pergunta -- um manual de 200 paginas nao cabe no contexto, e se coubesse
# custaria caro em toda mensagem.
#
# A IDEIA DO RAG (Retrieval-Augmented Generation, "geracao aumentada por
# busca"): antes de responder, BUSQUE os poucos trechos relevantes e mande so
# eles junto com a pergunta.
#
#   pergunta -> busca os 4 trechos mais parecidos -> modelo responde com eles
#
# COMO A BUSCA FUNCIONA, sem misticismo:
#   1. Um modelo de EMBEDDING transforma texto num vetor de 1536 numeros.
#   2. Textos com sentido PARECIDO viram vetores PROXIMOS -- mesmo sem
#      compartilhar palavra nenhuma. ("garantia de 12 meses" e "o produto tem
#      1 ano de cobertura" ficam perto; "receita de bolo" fica longe.)
#   3. Buscar = achar os vetores mais proximos do vetor da pergunta.
#
# O `pgvector` faz o passo 3 com o operador `<=>` (distancia de cosseno):
#     0 = identico   1 = sem relacao   2 = oposto
# =============================================================================

import asyncio
import json
import logging
import os
from contextlib import contextmanager

from langchain_openai import OpenAIEmbeddings
from psycopg_pool import ConnectionPool

from config import exigir

log = logging.getLogger("rag")

# O modelo que transforma texto em vetor.
# ⚠️ TROCAR ESTE MODELO EXIGE RECRIAR A TABELA: cada modelo produz vetores de
# tamanho diferente, e vetores de modelos diferentes NAO sao comparaveis entre
# si -- mesmo que tivessem o mesmo tamanho. Trocar = reprocessar tudo.
# ESCOLHA MEDIDA, nao opiniao. Comparando os modelos num teste de sinonimos e
# de armadilhas ("cancelar assinatura" x "assinar contrato"):
#     3-small (1536d)  -> separacao 0,114
#     3-large (3072d)  -> separacao 0,306   (2,7x melhor)
#     3-large (1536d)  -> separacao 0,298   (97% da qualidade, metade do espaco)
#
# E o preco NAO e argumento para escolher o pior: vetorizar um manual de 200
# paginas custa US$ 0,013 com o 3-large, UMA VEZ. Cada pergunta custa
# 1/300.000 de dolar. Uma unica resposta do agente custa 130x mais que o
# embedding que a alimentou. Economizar aqui e economizar no lugar errado.
MODELO_EMBEDDING = os.environ.get("MODELO_EMBEDDING", "text-embedding-3-large")

# 1536 e o tamanho da coluna no banco. O 3-large produz 3072 por padrao, mas
# aceita truncar -- e truncado ele ainda ganha do 3-small com folga.
DIMENSOES = 1536


class RAG:
    """Guarda trechos com seus vetores, e busca os mais parecidos."""

    def __init__(self, pool):
        self._pool = pool
        exigir("OPENAI_API_KEY", "Os embeddings usam a mesma chave do modelo.")
        self._embeddings = OpenAIEmbeddings(
            model=MODELO_EMBEDDING,
            dimensions=DIMENSOES,   # trunca o vetor para caber na coluna
        )

    # -- parte sincrona (SQL) --------------------------------------------------

    def _inserir(self, owner, fonte, trechos, metadados, vetores):
        with self._pool.connection() as conn, conn.cursor() as cur:
            # Apaga o que ja existia DESTA fonte antes de inserir. Sem isso,
            # recarregar um documento corrigido deixaria as duas versoes no
            # banco -- e a busca traria a antiga metade das vezes.
            cur.execute(
                "DELETE FROM documentos WHERE owner = %s AND fonte = %s",
                (owner, fonte),
            )
            # `executemany` manda tudo de uma vez, em vez de uma ida ao banco
            # por trecho. Com 300 trechos, a diferenca e de segundos.
            cur.executemany(
                "INSERT INTO documentos"
                " (owner, fonte, trecho, posicao, embedding, metadata)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                [
                    (owner, fonte, trecho, i, str(vetor), json.dumps(meta))
                    for i, (trecho, meta, vetor) in enumerate(
                        zip(trechos, metadados, vetores)
                    )
                ],
            )
            return len(trechos)

    def _buscar(self, owner, vetor, k, distancia_maxima):
        with self._pool.connection() as conn, conn.cursor() as cur:
            # `<=>` e a distancia de cosseno do pgvector.
            # O `WHERE owner = %s` NAO e detalhe: sem ele, a busca acharia os
            # documentos de todos os usuarios. Um vazamento silencioso, porque
            # a resposta pareceria perfeitamente normal.
            cur.execute(
                """
                SELECT trecho, fonte, posicao, metadata,
                       embedding <=> %s AS distancia
                  FROM documentos
                 WHERE owner = %s
                   AND embedding <=> %s < %s
                 ORDER BY distancia
                 LIMIT %s
                """,
                (str(vetor), owner, str(vetor), distancia_maxima, k),
            )
            return cur.fetchall()

    def _contar(self, owner):
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT fonte, count(*) FROM documentos WHERE owner = %s"
                " GROUP BY fonte ORDER BY 1",
                (owner,),
            )
            return cur.fetchall()

    def _apagar(self, owner, fonte):
        with self._pool.connection() as conn, conn.cursor() as cur:
            if fonte:
                cur.execute(
                    "DELETE FROM documentos WHERE owner = %s AND fonte = %s",
                    (owner, fonte),
                )
            else:
                cur.execute("DELETE FROM documentos WHERE owner = %s", (owner,))
            return cur.rowcount

    # -- parte async: o que o resto do projeto chama ---------------------------

    async def guardar(self, owner, fonte, documentos):
        """Calcula os vetores e guarda. `documentos` sao objetos `Document`.

        Aceita tambem uma lista de strings, por conveniencia -- elas viram
        Document sem metadata.
        """
        if not documentos:
            return 0

        # Normaliza: aceita Document ou string.
        trechos, metadados = [], []
        for d in documentos:
            if isinstance(d, str):
                trechos.append(d)
                metadados.append({})
            else:
                trechos.append(d.page_content)
                metadados.append(d.metadata or {})

        # UMA chamada para todos os trechos, e nao uma por trecho: a API aceita
        # lote, e em lote e muito mais rapido e barato.
        vetores = await self._embeddings.aembed_documents(trechos)
        n = await asyncio.to_thread(
            self._inserir, owner, fonte, trechos, metadados, vetores
        )
        log.info("guardados %d trechos de %r para %r", n, fonte, owner)
        return n

    async def buscar(self, owner, pergunta, k=4, distancia_maxima=0.75):
        """Devolve os `k` trechos mais parecidos com a pergunta.

        `distancia_maxima` corta os resultados ruins. Sem esse corte, uma
        pergunta sobre um assunto que NAO esta nos documentos ainda traria os 4
        trechos "menos distantes" -- e o modelo responderia com base em lixo,
        parecendo confiante. Melhor devolver nada e deixar o agente dizer que
        nao sabe.
        """
        vetor = await self._embeddings.aembed_query(pergunta)
        linhas = await asyncio.to_thread(
            self._buscar, owner, vetor, k, distancia_maxima
        )
        return [
            {
                "trecho": t,
                "fonte": f,
                "posicao": p,
                "metadata": meta,
                "distancia": float(d),
            }
            for t, f, p, meta, d in linhas
        ]

    async def apagar(self, owner, fonte=None):
        """Apaga os documentos de um dono (ou so de uma fonte dele).

        Existe por dois motivos: limpar testes, e atender pedido de exclusao de
        dados (LGPD) -- alguem pede para apagar o que e dele, e voce apaga.
        """
        return await asyncio.to_thread(self._apagar, owner, fonte)

    async def inventario(self, owner):
        """O que ja foi carregado: [(fonte, quantos_trechos), ...]"""
        return await asyncio.to_thread(self._contar, owner)


@contextmanager
def abrir_rag():
    url = exigir("DATABASE_URL", "Rode sql/00_banco_e_usuario.sql.")
    pool = ConnectionPool(url, min_size=1, max_size=3, open=False)
    pool.open()
    try:
        yield RAG(pool)
    finally:
        pool.close()
