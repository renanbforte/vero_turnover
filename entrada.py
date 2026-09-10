# =============================================================================
# entrada.py — o que acontece ANTES do servidor atender a primeira requisicao
# -----------------------------------------------------------------------------
# Num container, o codigo sobe sozinho e ninguem esta olhando. Este arquivo faz,
# em ordem, as tres coisas que uma pessoa faria a mao na primeira instalacao:
#
#   1. ESPERA O BANCO. O Postgres e o app sobem juntos, e o app quase sempre
#      fica pronto primeiro. Sem esta espera o primeiro deploy falha, o
#      orquestrador reinicia, e o log fica cheio de erro de conexao que nao era
#      erro nenhum -- era pressa.
#   2. CRIA AS TABELAS. Roda os arquivos de `sql/` em ordem. Todos sao
#      `IF NOT EXISTS`, entao rodar de novo nao quebra nada.
#   3. CARREGA A BASE, SE ESTIVER VAZIA. Le a planilha de `dados/`, calcula
#      tudo e grava. Se ja houver linhas, NAO recarrega -- um deploy nao pode
#      apagar o que esta no ar so porque o container reiniciou.
#
# Depois disso ele sai e o `uvicorn` assume (ver Dockerfile).
#
# ⚠️ POR QUE ISSO NAO E UMA MIGRACAO DE VERDADE. Nao ha versionamento de schema
# aqui: e um seed para uma demonstracao. Se este projeto virar coisa seria, o
# lugar deste arquivo e um Alembic, e a carga vira um job separado -- nao algo
# que roda a cada subida de container.
# =============================================================================

import logging
import os
import sys
import time
from pathlib import Path

import psycopg

# `config` carrega o .env ao ser importado. No container isso nao muda nada --
# as variaveis chegam pelo ambiente, e nao ha .env. Serve para rodar igual na
# sua maquina, com o mesmo arquivo que o resto do projeto ja usa.
import config  # noqa: F401
from logs import configurar

configurar()
log = logging.getLogger("entrada")

RAIZ = Path(__file__).parent
SQL = ["01_tabelas.sql", "02_idempotencia.sql", "03_custos.sql",
       "04_rag.sql", "05_rh.sql"]
PLANILHA = RAIZ / "dados" / "Base_Ficticia_Turnover_Case.xlsx"
DATA_BASE = os.environ.get("RH_DATA_BASE", "2026-06-30")


def esperar_banco(url, tentativas=60, intervalo=2):
    """Tenta conectar ate o Postgres responder. Falha com mensagem util."""
    for n in range(1, tentativas + 1):
        try:
            with psycopg.connect(url, connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT version()")
                    log.info("banco respondeu: %s", cur.fetchone()[0][:40])
            return
        except Exception as e:
            if n == tentativas:
                log.error("o banco nao respondeu depois de %d tentativas", n)
                raise
            if n == 1 or n % 10 == 0:
                log.info("esperando o banco (%d/%d): %s", n, tentativas, e)
            time.sleep(intervalo)


def criar_tabelas(url):
    """Roda os arquivos de sql/ em ordem, cada um na sua transacao.

    `autocommit` porque `CREATE EXTENSION` (o pgvector, em 04_rag.sql) nao gosta
    de rodar dentro de bloco em algumas versoes -- e porque um arquivo que falha
    nao deve levar os anteriores junto.
    """
    with psycopg.connect(url, autocommit=True) as conn:
        for nome in SQL:
            caminho = RAIZ / "sql" / nome
            if not caminho.exists():
                log.warning("sql ausente, pulando: %s", nome)
                continue
            with conn.cursor() as cur:
                cur.execute(caminho.read_text(encoding="utf-8"))
            log.info("sql aplicado: %s", nome)


def base_vazia(url):
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM rh.colaboradores")
        return cur.fetchone()[0] == 0


def carregar():
    """A mesma carga do passo25, sem o RAG.

    O RAG fica de fora daqui de proposito: indexar os cartoes chama a API de
    embeddings, que custa dinheiro e pode falhar por chave errada. Uma falha ali
    impediria o servidor de subir -- e o painel, que e o que a maioria vai
    acessar, nao depende de RAG nenhum. Os cartoes sao indexados na primeira
    conversa do chat, ou a mao com `python -m indexar_cartoes`.
    """
    import pandas as pd

    from rh_analise import analisar
    from rh_carga import abrir_carga, gravar

    if not PLANILHA.exists():
        log.error("planilha nao encontrada em %s -- base fica vazia", PLANILHA)
        return
    resultado = analisar(str(PLANILHA), pd.Timestamp(DATA_BASE))
    with abrir_carga() as pool:
        gravar(pool, resultado)
    log.info("base carregada: %d colaboradores", len(resultado["colaboradores"]))


def main():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        log.error("DATABASE_URL nao definida. Configure no painel do Easypanel.")
        return 1

    esperar_banco(url)
    criar_tabelas(url)
    if base_vazia(url):
        log.info("rh.colaboradores esta vazia -- carregando a planilha")
        carregar()
    else:
        log.info("base ja carregada -- nada a fazer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
