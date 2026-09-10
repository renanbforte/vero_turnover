# =============================================================================
# custos.py — saber quanto se gasta, e avisar quando passa do teto
# -----------------------------------------------------------------------------
# O QUE ESTE ARQUIVO RESOLVE, em ordem de importancia:
#
#   1. VISIBILIDADE. Hoje voce so descobre o gasto pela fatura, num numero so.
#      Aqui fica gravado quanto CADA requisicao consumiu, de QUEM.
#   2. TETO DIARIO. O limite do Passo 09 e por MINUTO: ele impede o pico, mas
#      nao o acumulo. 120/min mantidos 24h dao ~173 mil requisicoes por dia.
#      Um teto diario e o que fecha essa conta.
#
# ⚠️ ISTO NAO SUBSTITUI O LIMITE DE GASTO NO PAINEL DA OPENAI.
# Aquele e o unico que funciona mesmo se este codigo tiver bug, se o servidor
# ficar sem banco, ou se a sua chave vazar e for usada FORA daqui. Configure em
# platform.openai.com -> Settings -> Limits. Este arquivo e a primeira barreira;
# o painel da OpenAI e o freio de mao.
# =============================================================================

import asyncio
import logging
import os
import time
from contextlib import contextmanager
from datetime import date

from psycopg_pool import ConnectionPool

from config import exigir

log = logging.getLogger("custos")

# PRECOS por 1 MILHAO de tokens, em dolares.
# ⚠️ CONFIRA os valores atuais em openai.com/api/pricing -- preco muda, e um
# numero errado aqui nao quebra nada: so faz voce confiar num calculo errado.
# A busca e por PREFIXO porque a API devolve o nome com data
# ("gpt-4o-mini-2024-07-18"), e nao o nome curto.
PRECOS = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o":      (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1":     (2.00, 8.00),
    "gpt-3.5-turbo": (0.50, 1.50),
}
PRECO_PADRAO = (1.00, 4.00)   # chute conservador para modelo desconhecido


def calcular(modelo, entrada, saida):
    """Converte tokens em dolares.

    O nome do modelo chega de dois jeitos diferentes, e os DOIS precisam casar:
      - do nosso .env:  "openai:gpt-4o-mini"          (com o provedor na frente)
      - da API:         "gpt-4o-mini-2024-07-18"      (com a data no fim)
    Por isso tiramos o prefixo do provedor e comparamos por PREFIXO do resto.
    (Sem isso, o preco do .env caia no valor estimado -- foi um bug real,
    descoberto quando o chat mostrou "sem preco na tabela".)
    """
    modelo = modelo.split(":")[-1]
    for nome, (p_ent, p_sai) in PRECOS.items():
        if modelo.startswith(nome):
            return entrada / 1e6 * p_ent + saida / 1e6 * p_sai
    log.warning("modelo %r sem preco na tabela; usando estimativa", modelo)
    p_ent, p_sai = PRECO_PADRAO
    return entrada / 1e6 * p_ent + saida / 1e6 * p_sai


class Custos:
    def __init__(self, pool):
        self._pool = pool
        # Cache do teto. Sem ele, TODA requisicao faria uma consulta de
        # soma no banco -- barato, mas desnecessario: o gasto do dia nao
        # muda o suficiente em 30 segundos para valer uma ida ao banco por
        # mensagem. Em troca, o teto pode ser ultrapassado por ate 30s de
        # trafego. E uma troca consciente, e o numero e ajustavel.
        self._cache = None
        self._cache_ate = 0.0

    # -- parte sincrona --------------------------------------------------------

    def _registrar(self, owner, thread_id, entrada, saida, modelo, chamadas):
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO custos (owner, thread_id, tokens_entrada,"
                " tokens_saida, modelo, chamadas) VALUES (%s,%s,%s,%s,%s,%s)",
                (owner, thread_id, entrada, saida, modelo, chamadas),
            )

    def _gasto_do_dia(self):
        """Soma os tokens de HOJE, agrupados por modelo, e converte em dolares.

        Agrupamos por modelo porque cada um tem preco proprio -- somar tokens de
        modelos diferentes e depois multiplicar por um preco so daria errado.
        """
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT modelo, sum(tokens_entrada), sum(tokens_saida), count(*)"
                "  FROM custos WHERE criada_em::date = CURRENT_DATE"
                " GROUP BY modelo"
            )
            total, requisicoes = 0.0, 0
            for modelo, entrada, saida, n in cur.fetchall():
                total += calcular(modelo, entrada or 0, saida or 0)
                requisicoes += n
            return total, requisicoes

    def _por_usuario(self, dias):
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT owner, modelo, sum(tokens_entrada), sum(tokens_saida),"
                " count(*), max(chamadas)"
                "  FROM custos"
                " WHERE criada_em > now() - make_interval(days => %s)"
                " GROUP BY owner, modelo ORDER BY 3 DESC",
                (dias,),
            )
            linhas = []
            for owner, modelo, ent, sai, n, max_ch in cur.fetchall():
                linhas.append({
                    "owner": owner,
                    "requisicoes": n,
                    "tokens": (ent or 0) + (sai or 0),
                    "custo": calcular(modelo, ent or 0, sai or 0),
                    "max_chamadas": max_ch,
                })
            return linhas

    # -- parte async: o que o nucleo chama --------------------------------------

    async def registrar(self, owner, thread_id, entrada, saida, modelo, chamadas=1):
        await asyncio.to_thread(
            self._registrar, owner, thread_id, entrada, saida, modelo, chamadas
        )

    async def gasto_do_dia(self):
        """Devolve (dolares_gastos_hoje, numero_de_requisicoes)."""
        return await asyncio.to_thread(self._gasto_do_dia)

    async def por_usuario(self, dias=7):
        return await asyncio.to_thread(self._por_usuario, dias)

    async def verificar_teto(self):
        """Confere se o gasto de hoje passou do teto.

        Devolve (passou, gasto, teto). NAO bloqueia nada -- quem decide o que
        fazer com essa informacao e quem chamou. Um modulo que so mede e mais
        facil de testar e de reaproveitar do que um que tambem decide.
        """
        teto = float(os.environ.get("TETO_DIARIO_USD", 5))
        agora = time.monotonic()
        if self._cache is None or agora > self._cache_ate:
            gasto, _ = await self.gasto_do_dia()
            self._cache = gasto
            self._cache_ate = agora + float(os.environ.get("CACHE_TETO_SEG", 30))
        return self._cache > teto, self._cache, teto


@contextmanager
def abrir_custos():
    url = exigir("DATABASE_URL", "Rode sql/00_banco_e_usuario.sql.")
    pool = ConnectionPool(url, min_size=1, max_size=3, open=False)
    pool.open()
    try:
        yield Custos(pool)
    finally:
        pool.close()
