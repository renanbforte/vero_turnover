# =============================================================================
# tools/__init__.py — a "vitrine" do pacote de ferramentas
# -----------------------------------------------------------------------------
# Uma pasta com um arquivo `__init__.py` dentro vira um PACOTE em Python: um
# conjunto de arquivos que se importa como se fosse um so.
#
# ⚠️ ESTE PACOTE E MENOR QUE O DO REPOSITORIO DE ESTUDO, DE PROPOSITO.
# La existem tambem ferramentas de CEP, CNPJ, cotacao e hora, alem do conector
# de MCP. Nenhuma delas e usada pelo agente de diretoria (ver `rh_agente.py`,
# que monta o agente so com `TOOLS_RH`), e cada uma traria dependencia e
# superficie de ataque para um servidor que existe para falar de turnover.
#
# O que sobrou:
#   - `rh.py`         as 9 ferramentas do agente de diretoria
#   - `documentos.py` a busca na memoria semantica (RAG), usada pelo `nucleo`
# =============================================================================

from .documentos import buscar_nos_documentos, definir_rag
# O conjunto de RH NAO entra em TOOLS: ele e fechado e so o agente de
# diretoria usa (ver rh_agente.py). Ferramenta a mais custa contexto em
# TODA mensagem, mesmo quando o assunto nao tem nada a ver.
from .rh import TOOLS_RH, definir_rh

TOOLS = [
    buscar_nos_documentos,
]

# `__all__` diz o que sai daqui quando alguem faz `from tools import *`.
__all__ = [
    "TOOLS",
    "TOOLS_RH",
    "definir_rh",
    "buscar_nos_documentos",
    "definir_rag",
]
