# =============================================================================
# indexar_cartoes.py — a memoria semantica do agente, uma vez so
# -----------------------------------------------------------------------------
# O agente responde "quanto e" consultando o banco pelas ferramentas. Isso
# funciona sem nada disto aqui.
#
# O que ESTE script acrescenta e a resposta para "o que isso significa": 20
# cartoes de analise -- o achado, a leitura e a limitacao de cada um -- indexados
# como memoria semantica (RAG). Com eles o agente relaciona a pergunta com o
# raciocinio que ja foi feito, em vez de reinventar interpretacao a cada vez.
#
# POR QUE NAO RODA SOZINHO NA SUBIDA DO CONTAINER: indexar chama a API de
# embeddings da OpenAI. Custa centavos, mas pode falhar -- chave errada, cota
# estourada, rede. Se isso acontecesse dentro do `entrada.py`, o servidor nao
# subiria, e o painel, que nao depende de RAG nenhum, ficaria fora do ar junto.
# Separar e o que garante que a falha do opcional nao derruba o essencial.
#
# RODAR DE NOVO E SEGURO: o RAG apaga a fonte antes de inserir, entao os cartoes
# sao substituidos, nunca duplicados.
#
# COMO RODAR:
#   local:      python indexar_cartoes.py
#   Easypanel:  abra o Console do servico e rode a mesma linha
# =============================================================================

import asyncio
import logging
import sys
from pathlib import Path

import pandas as pd

import config  # noqa: F401  (carrega o .env)
from logs import configurar
from rag import abrir_rag
from rh_analise import analisar
from rh_insights import indexar

configurar()
log = logging.getLogger("indexar")

# Os cartoes sao gerados a partir do MESMO calculo da carga -- e nao lidos das
# tabelas ja gravadas. Refazer a analise leva segundos e garante que o texto do
# cartao e o numero do banco vieram da mesma conta.
PLANILHA = Path(__file__).parent / "dados" / "Base_Ficticia_Turnover_Case.xlsx"
DATA_BASE = "2026-06-30"


async def main():
    if not PLANILHA.exists():
        print(f"\n  Planilha nao encontrada em {PLANILHA}\n")
        return 1
    print("\n  Recalculando a analise para gerar os cartoes...")
    resultado = analisar(str(PLANILHA), pd.Timestamp(DATA_BASE))
    with abrir_rag() as rag:
        quantos = await indexar(rag, resultado)
    print(f"  {quantos} cartoes indexados na memoria semantica.\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
