# =============================================================================
# passo27_testar_chat.py — provar que as travas do agente de RH funcionam
# -----------------------------------------------------------------------------
# POR QUE ESTE TESTE EXISTE, e por que ele nao usa banco nem modelo.
#
# As protecoes deste agente sao de dois tipos, e os dois falham em SILENCIO:
#   - AUTENTICACAO: se ela parar de funcionar, tudo continua respondendo. Voce
#     so descobre quando alguem que nao devia ler ja leu.
#   - LISTA BRANCA de colunas: se ela cair, o agente passa a aceitar nome de
#     coluna vindo do texto do usuario. Tambem sem erro visivel.
#
# Bug que nao levanta excecao precisa de teste, senao nao existe. E como estes
# testes nao dependem de banco nem de chave de API, eles rodam em qualquer
# maquina, em segundos, inclusive numa esteira de CI.
#
# COMO RODAR (da raiz do projeto):
#   uv run python -m passos.passo27_testar_chat
# =============================================================================

import asyncio
import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

SENHA = "senha-so-para-o-teste"
resultados = []


def checar(nome, obtido, esperado):
    ok = obtido == esperado
    resultados.append(ok)
    marca = " ok " if ok else "FALHA"
    print(f"  [{marca}] {nome}")
    if not ok:
        print(f"          obtido: {obtido!r}   esperado: {esperado!r}")


# =============================================================================
# 1. AS ROTAS E A PORTA DE ENTRADA
# =============================================================================
def testar_rotas():
    print("\n1. ROTAS E AUTENTICACAO\n")
    os.environ["RH_CHAT_SENHA"] = SENHA
    os.environ["RH_PERFIS_AUTORIZADOS"] = "renan, ana.diretoria"

    from rh_web import router

    app = FastAPI()
    app.include_router(router)
    # O agente nao subiu de proposito: quem passar pela autenticacao deve chegar
    # ate aqui e receber 503. Isso PROVA que a requisicao atravessou as travas --
    # um 401 no lugar do 503 significaria que ela parou antes, e o teste passaria
    # pelo motivo errado.
    app.state.agente_rh = None
    c = TestClient(app)
    cabecalho = {"X-Chat-Senha": SENHA}

    checar("GET /chat devolve a pagina", c.get("/chat").status_code, 200)
    checar("POST sem senha e recusado (401)",
           c.post("/chat/mensagem", json={"de": "renan", "texto": "ola"}).status_code, 401)
    checar("POST com senha errada e recusado (401)",
           c.post("/chat/mensagem", json={"de": "renan", "texto": "ola"},
                  headers={"X-Chat-Senha": "chute"}).status_code, 401)
    checar("POST com login fora da lista e recusado (403)",
           c.post("/chat/mensagem", json={"de": "estagiario", "texto": "ola"},
                  headers=cabecalho).status_code, 403)
    checar("POST autorizado atravessa as travas (503, agente nao subiu)",
           c.post("/chat/mensagem", json={"de": "renan", "texto": "ola"},
                  headers=cabecalho).status_code, 503)
    checar("POST com campo desconhecido e recusado (422)",
           c.post("/chat/mensagem", json={"de": "renan", "texto": "ola", "x": 1},
                  headers=cabecalho).status_code, 422)
    checar("POST com texto vazio e recusado (422)",
           c.post("/chat/mensagem", json={"de": "renan", "texto": ""},
                  headers=cabecalho).status_code, 422)

    # FALHA FECHANDO: variavel esquecida deve BLOQUEAR, nunca liberar.
    os.environ["RH_PERFIS_AUTORIZADOS"] = ""
    checar("sem perfis configurados, ate o autorizado e recusado (403)",
           c.post("/chat/mensagem", json={"de": "renan", "texto": "ola"},
                  headers=cabecalho).status_code, 403)
    os.environ["RH_CHAT_SENHA"] = ""
    checar("sem senha configurada, tudo e recusado (401)",
           c.post("/chat/mensagem", json={"de": "renan", "texto": "ola"},
                  headers=cabecalho).status_code, 401)


# =============================================================================
# 2. A LISTA BRANCA DE COLUNAS
# -----------------------------------------------------------------------------
# Estes testes nao tocam no banco: a validacao acontece ANTES de montar o SQL,
# e e exatamente isso que a gente quer provar. Se um nome invalido chegasse a
# virar comando, o erro apareceria no banco -- tarde demais.
# =============================================================================
def testar_lista_branca():
    print("\n2. LISTA BRANCA DE COLUNAS (sem tocar no banco)\n")
    from rh_consultas import ConsultasRH

    rh = ConsultasRH(pool=None)      # nenhuma consulta chega a ser executada

    def erro_de(corrotina):
        try:
            asyncio.run(corrotina)
            return None
        except ValueError as e:
            return type(e).__name__
        except Exception as e:
            return f"{type(e).__name__} (esperavamos ValueError)"

    checar("dimensao inexistente e recusada",
           erro_de(rh.segmento("salario_do_ceo")), "ValueError")
    checar("dimensao inventada no cubo e recusada",
           erro_de(rh.cubo(["nome_completo"])), "ValueError")
    checar("SQL no nome da dimensao e recusado",
           erro_de(rh.cubo(["area; DROP TABLE rh.colaboradores"])), "ValueError")
    checar("agrupar por 3 dimensoes e recusado",
           erro_de(rh.cubo(["area", "localidade", "nivel"])), "ValueError")
    checar("nenhuma dimensao e recusado",
           erro_de(rh.cubo([])), "ValueError")
    checar("campo de filtro fora da lista e recusado",
           erro_de(rh.cubo(["area"], [{"campo": "cpf", "valor": "x"}])), "ValueError")
    checar("operador invalido e recusado",
           erro_de(rh.cubo(["area"], [{"campo": "area", "operador": "regex",
                                       "valor": "x"}])), "ValueError")
    checar("reducao fora de 0-100 e recusada",
           erro_de(rh.cenario(0)), "ValueError")


# =============================================================================
# 3. AS FERRAMENTAS RECUSAM QUEM NAO ESTA AUTORIZADO
# -----------------------------------------------------------------------------
# A checagem se repete aqui de proposito: no `rh_web.py` ela protege a rota; nas
# ferramentas ela protege QUALQUER canal -- inclusive um que alguem acrescente
# amanha e esqueca de proteger. Defesa em profundidade so vale se as duas
# camadas forem testadas.
# =============================================================================
def testar_ferramentas():
    print("\n3. AS FERRAMENTAS CHECAM AUTORIZACAO POR CONTA PROPRIA\n")
    from identidade import definir_identidade_atual
    from tools.rh import TOOLS_RH, panorama_turnover

    os.environ["RH_PERFIS_AUTORIZADOS"] = "renan"
    definir_identidade_atual("intruso")
    resposta = asyncio.run(panorama_turnover.ainvoke({}))
    checar("ferramenta recusa identidade nao autorizada",
           "nao tem autorizacao" in resposta.lower(), True)

    os.environ["RH_PERFIS_AUTORIZADOS"] = ""
    definir_identidade_atual("renan")
    resposta = asyncio.run(panorama_turnover.ainvoke({}))
    checar("sem lista configurada, a ferramenta recusa (falha fechando)",
           "autorizado" in resposta.lower(), True)

    checar("nenhuma ferramenta aceita 'owner' ou 'identidade' como argumento",
           not any(campo in (t.args_schema.model_json_schema().get("properties", {})
                             if t.args_schema else {})
                   for t in TOOLS_RH
                   for campo in ("owner", "identidade", "login", "usuario")),
           True)


def main():
    testar_rotas()
    testar_lista_branca()
    testar_ferramentas()

    total, ok = len(resultados), sum(resultados)
    print(f"\n{'=' * 66}")
    print(f"  {ok} de {total} verificacoes passaram")
    print(f"{'=' * 66}\n")
    raise SystemExit(0 if ok == total else 1)


if __name__ == "__main__":
    main()
