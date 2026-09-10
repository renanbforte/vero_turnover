# =============================================================================
# rh_consultas.py — o LADO DA LEITURA: tudo que o agente pode perguntar ao banco
# -----------------------------------------------------------------------------
# ⚠️ ESTE ARQUIVO E O PONTO DE ESTRANGULAMENTO DE SEGURANCA DOS DADOS DE PESSOAS.
# Nenhuma ferramenta monta SQL. Todas chamam um metodo daqui. Isso e proposital:
#
#   - Se o MODELO montasse SQL, bastaria um pedido bem escrito ("liste os
#     colaboradores com maior risco") para o agente devolver linha de pessoa.
#     Prompt nao e trava de seguranca: e um pedido educado.
#   - Com as consultas escritas AQUI, "listar pessoas" nao e algo que o agente
#     faz mal -- e algo que ele NAO CONSEGUE fazer, porque nao existe metodo.
#
# AS DUAS TRAVAS, e por que sao duas:
#   1. LISTA BRANCA de colunas (DIMENSOES / FILTROS). O nome da coluna nunca vem
#      do texto do usuario direto para o SQL: ele e procurado num dicionario, e
#      o que nao esta la vira erro. Isto tambem elimina injecao de SQL por
#      construcao -- nao ha string de usuario dentro do comando.
#   2. MINIMO POR GRUPO (k-anonimato). Grupo com menos de 5 pessoas nao e
#      devolvido. Sem isso, bastaria filtrar ate sobrar um ("Juridico + Campinas
#      + Lideranca") para uma MEDIA virar o dado de uma pessoa so.
#
# POR QUE ASYNC EM CIMA DE SQL SINCRONO? Mesma razao do `memoria.py`: o psycopg
# assincrono no Windows exige o SelectorEventLoop, e os servidores MCP exigem o
# ProactorEventLoop. Rodando o SQL em thread (`asyncio.to_thread`), o event loop
# fica livre e os dois convivem.
# =============================================================================

import asyncio
import logging
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from config import exigir

log = logging.getLogger("rh_consultas")

# O k do k-anonimato. Mudar este numero e uma decisao de privacidade, nao de
# conveniencia: 1 devolveria pessoas, e nenhum prompt conserta isso depois.
MINIMO_POR_GRUPO = 5

# LISTA BRANCA. Chave = o nome que o modelo escreve; valor = a coluna real.
# O modelo nunca ve o nome da coluna, e a coluna nunca vem do modelo.
DIMENSOES = {
    "area": "area",
    "localidade": "localidade",
    "nivel": "nivel_cargo",
    "contrato": "tipo_contrato",
    "performance": "performance",
    "tempo_de_casa": "faixa_tempo_casa",
    "situacao": "status_desligamento",
}

# Filtros aceitam os mesmos campos das dimensoes MAIS os intervalos numericos,
# que sao tratados a parte porque comparam (>=, <) em vez de igualar.
FILTROS_NUMERICOS = {
    "engajamento": "engajamento",
    "lideranca": "indice_lideranca",
    "compa_ratio": "compa_ratio",
    "horas_extras": "horas_extras_mes",
    "salario": "salario_base",
    "tempo_anos": "tempo_empresa_anos",
}


class ConsultasRH:
    """Leitura do schema `rh`. Um metodo por pergunta que o agente pode fazer."""

    def __init__(self, pool):
        self._pool = pool

    @property
    def pool(self):
        """O pool, para quem precisa consultar fora deste modulo.

        Existe para o painel de graficos (`rh_graficos.py`) nao abrir um pool
        proprio: ele roda no mesmo servidor e usaria conexoes a toa. Nao e uma
        porta dos fundos -- quem usa continua passando pelas consultas
        DECLARADAS no catalogo, e nenhuma delas devolve linha de pessoa.
        """
        return self._pool

    # -- infraestrutura -------------------------------------------------------

    def _ler(self, sql, parametros=()):
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, parametros)
            return cur.fetchall()

    async def _consultar(self, sql, parametros=()):
        return await asyncio.to_thread(self._ler, sql, parametros)

    async def checar(self):
        """O banco responde E a carga foi feita? Duas coisas diferentes."""
        linhas = await self._consultar("SELECT count(*) AS n FROM rh.colaboradores")
        return linhas[0]["n"]

    # -- 1. o panorama ---------------------------------------------------------

    async def panorama(self):
        """O indicador da empresa + a tendencia. E o que abre qualquer conversa."""
        indicador = (await self._consultar("SELECT * FROM rh.vw_indicador"))[0]
        serie = await self._consultar(
            """
            SELECT to_char(mes, 'YYYY-MM') AS competencia,
                   headcount_fim, admissoes, desligamentos,
                   voluntarios, involuntarios,
                   turnover_12m, turnover_vol_12m
              FROM rh.metricas_mensais
             WHERE turnover_12m IS NOT NULL
             ORDER BY mes
            """
        )
        premissas = await self._consultar(
            "SELECT chave, valor FROM rh.premissas"
            " WHERE chave IN ('janela','denominador','data_base','folha_anual')"
        )
        return {
            "indicador": indicador,
            "serie": serie,
            "premissas": {p["chave"]: p["valor"] for p in premissas},
        }

    # -- 2. segmentos ---------------------------------------------------------

    async def segmento(self, dimensao):
        if dimensao not in DIMENSOES:
            raise ValueError(
                f"Dimensao {dimensao!r} nao existe. Disponiveis: "
                + ", ".join(sorted(DIMENSOES))
            )
        return await self._consultar(
            """
            SELECT valor, headcount_ativo, exposicao, desl_voluntarios,
                   desl_involuntarios, turnover_vol, turnover_total,
                   share_saidas_vol, excesso_saidas, custo_excesso
              FROM rh.segmentos
             WHERE dimensao = %s
             ORDER BY turnover_vol DESC
            """,
            (dimensao,),
        )

    async def prioridade(self, limite=8):
        """Area x faixa, do maior excesso para o menor. Responde 'onde comecar'."""
        return await self._consultar(
            """
            SELECT area, faixa_tempo_casa, headcount, saidas_vol, turnover_vol,
                   esperado_no_baseline, excesso_saidas, custo_excesso
              FROM rh.prioridade
             ORDER BY excesso_saidas DESC
             LIMIT %s
            """,
            (limite,),
        )

    # -- 3. a consulta flexivel (o "cubo") ------------------------------------

    async def cubo(self, dimensoes, filtros=None, limite=25):
        """Agrega por 1 ou 2 dimensoes, com filtros. NUNCA devolve pessoas.

        `dimensoes`: lista de nomes da lista branca DIMENSOES.
        `filtros`:   lista de dicionarios {campo, operador, valor}.
                     operador: 'igual' | 'maior_que' | 'menor_que'

        A montagem do SQL usa SOMENTE nomes vindos dos dicionarios da lista
        branca. Os valores vao como PARAMETRO (%s), nunca concatenados. As duas
        coisas juntas fecham a porta da injecao: o que o usuario escreve nunca
        vira comando, so vira dado.
        """
        if not dimensoes:
            raise ValueError("Informe pelo menos uma dimensao para agrupar.")
        if len(dimensoes) > 2:
            raise ValueError(
                "No maximo 2 dimensoes. Mais que isso fragmenta os grupos ate "
                f"quase todos ficarem abaixo de {MINIMO_POR_GRUPO} pessoas e "
                "serem suprimidos -- a tabela viria quase vazia."
            )
        colunas = []
        for d in dimensoes:
            if d not in DIMENSOES:
                raise ValueError(
                    f"Dimensao {d!r} nao existe. Disponiveis: "
                    + ", ".join(sorted(DIMENSOES))
                )
            colunas.append(DIMENSOES[d])

        onde, parametros = [], []
        for f in filtros or []:
            campo = f.get("campo")
            operador = f.get("operador", "igual")
            valor = f.get("valor")
            if campo in DIMENSOES and operador == "igual":
                onde.append(f"{DIMENSOES[campo]} = %s")
                parametros.append(str(valor))
            elif campo in FILTROS_NUMERICOS and operador in ("maior_que", "menor_que"):
                sinal = ">=" if operador == "maior_que" else "<"
                onde.append(f"{FILTROS_NUMERICOS[campo]} {sinal} %s")
                parametros.append(float(valor))
            else:
                raise ValueError(
                    f"Filtro invalido: campo={campo!r} operador={operador!r}. "
                    "Campos de texto aceitam 'igual'; numericos aceitam "
                    "'maior_que' e 'menor_que'. Campos disponiveis: "
                    + ", ".join(sorted(set(DIMENSOES) | set(FILTROS_NUMERICOS)))
                )

        grupo = ", ".join(colunas)
        filtro_sql = ("WHERE " + " AND ".join(onde)) if onde else ""
        sql = f"""
            SELECT {grupo},
                   count(*)                                              AS pessoas,
                   count(*) FILTER (WHERE status_desligamento = 'Ativo') AS headcount_ativo,
                   round(sum(exposicao_ltm), 1)                          AS exposicao,
                   count(*) FILTER (WHERE saiu_vol_ltm)                  AS saidas_voluntarias,
                   count(*) FILTER (WHERE saiu_invol_ltm)                AS saidas_involuntarias,
                   round(100.0 * count(*) FILTER (WHERE saiu_vol_ltm)
                         / nullif(sum(exposicao_ltm), 0), 1)             AS turnover_vol,
                   round(avg(engajamento), 1)                            AS engajamento_medio,
                   round(avg(indice_lideranca), 1)                       AS lideranca_media,
                   round(avg(compa_ratio), 2)                            AS compa_medio,
                   round(avg(horas_extras_mes), 1)                       AS horas_extras_medias,
                   round(avg(salario_base), 0)                           AS salario_medio,
                   round(sum(custo_reposicao) FILTER (WHERE saiu_ltm), 0) AS custo_saidas
              FROM rh.colaboradores
              {filtro_sql}
             GROUP BY {grupo}
            HAVING count(*) >= {MINIMO_POR_GRUPO}
             ORDER BY turnover_vol DESC NULLS LAST
             LIMIT %s
        """
        linhas = await self._consultar(sql, (*parametros, limite))

        # Quantos grupos sumiram por serem pequenos demais. Devolver isso e uma
        # questao de honestidade: sem o aviso, o agente apresentaria uma tabela
        # incompleta como se fosse completa.
        sql_suprimidos = f"""
            SELECT count(*) AS grupos, coalesce(sum(n), 0) AS pessoas
              FROM (SELECT count(*) AS n FROM rh.colaboradores {filtro_sql}
                     GROUP BY {grupo} HAVING count(*) < {MINIMO_POR_GRUPO}) g
        """
        suprimidos = (await self._consultar(sql_suprimidos, tuple(parametros)))[0]
        return {"linhas": linhas, "suprimidos": suprimidos}

    # -- 4. a entrada: coortes e sobrevivencia --------------------------------

    async def entrada(self):
        coortes = await self._consultar(
            "SELECT coorte, contratacoes, saida_vol_3m, saida_vol_6m, saida_vol_12m"
            "  FROM rh.coortes ORDER BY coorte"
        )
        km = await self._consultar(
            "SELECT mes_de_casa, em_risco, risco_acumulado"
            "  FROM rh.sobrevivencia WHERE mes_de_casa IN (0,3,6,9,12,15,18)"
            " ORDER BY mes_de_casa"
        )
        return {"coortes": coortes, "sobrevivencia": km}

    # -- 5. explicacao: fatores, motivos, premissas, qualidade ----------------

    async def fatores(self):
        linhas = await self._consultar(
            "SELECT fator, odds_ratio, ic_inferior, ic_superior, p_valor, significativo"
            "  FROM rh.fatores ORDER BY odds_ratio DESC"
        )
        r2 = await self._consultar(
            "SELECT valor FROM rh.premissas WHERE chave = 'pseudo_r2_regressao'"
        )
        return {"fatores": linhas, "pseudo_r2": r2[0]["valor"] if r2 else "n/d"}

    async def motivos(self):
        linhas = await self._consultar(
            "SELECT motivo, quantidade, share, engajamento_medio, lideranca_media,"
            "       compa_medio, tempo_casa_medio"
            "  FROM rh.motivos ORDER BY quantidade DESC"
        )
        # As medias dos ATIVOS sao a referencia. Sem elas, "engajamento 66" nao
        # diz nada -- e com elas vira "6,5 pontos abaixo de quem ficou", que e a
        # leitura que muda a conclusao.
        referencia = (await self._consultar(
            """
            SELECT round(avg(engajamento), 1)      AS engajamento_medio,
                   round(avg(indice_lideranca), 1) AS lideranca_media,
                   round(avg(compa_ratio), 2)      AS compa_medio
              FROM rh.colaboradores WHERE status_desligamento = 'Ativo'
            """
        ))[0]
        return {"motivos": linhas, "referencia_ativos": referencia}

    async def premissas(self):
        return await self._consultar(
            "SELECT chave, valor, descricao FROM rh.premissas ORDER BY chave"
        )

    async def qualidade(self):
        return await self._consultar(
            "SELECT teste, resultado, decisao FROM rh.qualidade"
            " ORDER BY CASE decisao WHEN 'DESCARTAR' THEN 1 WHEN 'LIMITACAO' THEN 2"
            "                       WHEN 'ATENCAO' THEN 3 ELSE 4 END, teste"
        )

    # -- 6. o business case ----------------------------------------------------

    async def cenario(self, reducao_pct, faixa="0-12 meses"):
        """Quanto vale reduzir X% das saidas voluntarias de uma faixa.

        A conta e simples de proposito: saidas evitadas x custo medio de
        reposicao. Nao inclui perda de receita nem custo de vaga aberta -- e o
        agente e instruido a dizer isso, porque um numero de economia sem as
        premissas do lado vira promessa.
        """
        if not 0 < reducao_pct <= 100:
            raise ValueError("A reducao deve estar entre 1 e 100 por cento.")
        dados = (await self._consultar(
            """
            SELECT count(*) FILTER (WHERE saiu_vol_ltm)   AS saidas_vol,
                   count(*) FILTER (WHERE saiu_invol_ltm) AS saidas_invol,
                   round(avg(custo_reposicao) FILTER (WHERE saiu_vol_ltm), 0) AS custo_medio
              FROM rh.colaboradores WHERE faixa_tempo_casa = %s
            """,
            (faixa,),
        ))[0]
        total = (await self._consultar("SELECT * FROM rh.vw_indicador"))[0]
        evitadas = (dados["saidas_vol"] or 0) * reducao_pct / 100
        economia = evitadas * float(dados["custo_medio"] or 0)
        novo_vol = ((total["desl_voluntarios"] - evitadas)
                    / float(total["exposicao_fte_ano"]) * 100)
        return {
            "faixa": faixa,
            "reducao_pct": reducao_pct,
            "saidas_na_faixa": dados["saidas_vol"],
            "saidas_evitadas": round(evitadas, 1),
            "custo_medio_por_saida": float(dados["custo_medio"] or 0),
            "economia_12m": round(economia, 0),
            "turnover_vol_hoje": float(total["turnover_vol"]),
            "turnover_vol_projetado": round(novo_vol, 1),
        }


@contextmanager
def abrir_rh():
    """Abre o pool de leitura dos dados de RH e fecha no fim.

    Aberto pelo `nucleo.py` junto com o RAG, pelo mesmo motivo: as ferramentas
    sao chamadas muitas vezes e nao podem abrir conexao a cada chamada.
    """
    url = exigir("DATABASE_URL", "Rode `uv run python scripts/preparar.py`.")
    pool = ConnectionPool(url, min_size=1, max_size=3, open=False)
    pool.open()
    try:
        yield ConsultasRH(pool)
    finally:
        pool.close()
