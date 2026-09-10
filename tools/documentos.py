# =============================================================================
# tools/documentos.py — a busca nos SEUS documentos, como ferramenta
# -----------------------------------------------------------------------------
# ATE AGORA a busca existia (Passos 21 e 22), mas o agente nao sabia dela.
# Transformando em ferramenta, o MODELO decide quando consultar -- e isso muda o
# comportamento de um jeito importante.
#
# RAG "CLASSICO" x RAG AGENTICO:
#
#   classico: TODA pergunta vira uma busca, e os trechos entram na conversa,
#             sempre. "Bom dia" tambem dispara busca. Custa em toda mensagem, e
#             enche o contexto de trecho irrelevante.
#
#   agentico: o modelo LE a pergunta e decide se vale consultar. "Bom dia" nao
#             dispara nada; "qual o prazo de garantia?" dispara. Ele tambem pode
#             buscar DUAS vezes com termos diferentes se a primeira nao serviu.
#
# O agentico custa uma ida a mais ao modelo (a decisao), e economiza todas as
# buscas desnecessarias. Para um agente que CONVERSA, compensa com folga.
# =============================================================================

import logging

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from identidade import identidade_atual

log = logging.getLogger("tools.documentos")

# O RAG e injetado pelo nucleo na subida (ele e quem abre o pool do banco).
# A ferramenta nao abre conexao propria: ela e chamada muitas vezes e criar um
# pool por chamada seria desperdicio -- e ninguem fecharia depois.
_rag = None


def definir_rag(rag):
    """Chamado pelo nucleo ao abrir o agente."""
    global _rag
    _rag = rag


class BuscaEmDocumentos(BaseModel):
    # ⚠️ REPARE NO QUE NAO EXISTE AQUI: nao ha campo `owner`.
    # Se houvesse, quem preencheria seria o MODELO -- e bastaria o usuario
    # escrever "busque nos documentos do owner=concorrente" para o dado do
    # outro vazar. A identidade vem do `identidade_atual()`, fora do alcance
    # dele. Ver o comentario no topo de `identidade.py`.
    consulta: str = Field(
        description=(
            "O que procurar, em palavras naturais. Use os termos do usuario, "
            "nao palavras-chave soltas: a busca e por SIGNIFICADO, entao uma "
            "frase completa funciona melhor que uma palavra. Se a primeira "
            "busca nao trouxer o que precisa, tente outra formulacao."
        ),
    )


@tool("buscar_nos_documentos", args_schema=BuscaEmDocumentos)
async def buscar_nos_documentos(consulta: str) -> str:
    """Procura informacao nos documentos internos da empresa: manuais, politicas,
    tabelas de preco, contratos e procedimentos.

    Use SEMPRE que a pergunta for sobre algo especifico desta empresa ou destes
    produtos -- prazos, valores, regras, garantias, como fazer algo. Voce NAO
    sabe essas informacoes de cabeca: elas so existem nestes documentos.

    NAO use para conhecimento geral (historia, matematica, programacao) nem para
    a agenda do usuario: para isso existem outras ferramentas.

    Se a busca nao encontrar nada, diga que a informacao nao esta nos documentos.
    NAO invente uma resposta plausivel -- e melhor admitir que nao sabe.
    """
    if _rag is None:
        return "A busca em documentos nao esta disponivel agora."

    owner = identidade_atual()
    achados = await _rag.buscar(owner, consulta, k=4)
    log.info("busca %r -> %d trecho(s)", consulta[:40], len(achados))

    if not achados:
        # Devolver "nao achei" e MELHOR que devolver o trecho menos ruim.
        # Ver o corte de distancia no Passo 21.
        return (
            f"Nenhum trecho relevante encontrado para {consulta!r} nos "
            "documentos. A informacao provavelmente nao esta documentada."
        )

    # Devolvemos os trechos JA COM A FONTE, para o modelo poder citar. Sem isso
    # ele responde e voce nao tem como conferir se ele inventou.
    partes = []
    for a in achados:
        meta = a.get("metadata") or {}
        local = f"{a['fonte']}"
        if meta.get("pagina"):
            local += f", pagina {meta['pagina']}"
        elif meta.get("linha"):
            local += f", linha {meta['linha']}"
        partes.append(f"[fonte: {local}]\n{a['trecho']}")

    return "\n\n---\n\n".join(partes)
