# =============================================================================
# rh_web.py — o canal WEB: a pagina de chat que a diretoria abre no navegador
# -----------------------------------------------------------------------------
# Este e o TERCEIRO canal do projeto (terminal, webhook, e agora a pagina). Ele
# segue a mesma regra dos outros: NAO tem regra de negocio. Le do mundo, entrega
# ao nucleo, devolve ao mundo. Toda a decisao sobre o que o agente pode fazer
# esta no `rh_agente.py` e no `tools/rh.py`.
#
# DUAS FORMAS DE SUBIR, e a diferenca importa.
#
#   uv run uvicorn rh_web:app --port 8000        <- SO o chat (recomendado)
#   uv run uvicorn webhook:app --port 8000       <- chat + webhook do n8n
#
# O router e o mesmo nos dois; muda o que sobe junto. No `webhook:app`, o
# lifespan abre TAMBEM o agente geral -- que carrega os servidores de MCP em
# subprocessos e as ferramentas de CEP, CNPJ e cotacao. Para uma pagina que so
# fala de turnover, isso e subida mais lenta, um segundo conjunto de pools e o
# endpoint /webhook exposto sem necessidade.
#
# Suba pelo `webhook:app` so se voce REALMENTE for usar o n8n no mesmo servidor.
# Para a diretoria, `rh_web:app` e o certo: um agente, um conjunto de
# ferramentas, uma porta.
#
# ⚠️ AUTENTICACAO: LEIA ANTES DE EXPOR ISTO NA INTERNET.
# A pagina pede uma senha de acesso (RH_CHAT_SENHA) e o login de quem esta
# falando. Isso e suficiente para uma rede interna e NAO e suficiente para a
# internet aberta, por um motivo simples: a senha e compartilhada, entao ela
# prova que a pessoa pertence ao grupo, nao QUEM ela e. O login e declarado pelo
# proprio usuario.
#
# O caminho certo para producao e SSO (Entra ID, Google Workspace) devolvendo um
# token assinado, com o login vindo do token e nao do formulario. A estrutura ja
# esta pronta para isso: troque `_exigir_acesso` por uma dependencia que valide
# o token, e nada mais neste arquivo muda.
#
# Por que uma senha SEPARADA da WEBHOOK_API_KEY: a chave do webhook e de
# maquina (n8n) e vive num cofre. A senha do chat vive no navegador de varias
# pessoas. Misturar as duas faria o vazamento de uma comprometer a outra.
# =============================================================================

import hmac
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse,
                               Response)
from pydantic import BaseModel, ConfigDict, Field

from identidade import normalizar_identidade
from limites import limitador
from logs import configurar, id_requisicao
from nucleo import EntradaInvalida, MAX_CARACTERES, TempoEsgotado, TetoDiarioAtingido
from rh_agente import abrir_agente_diretoria

configurar()          # liga o logging antes de qualquer coisa

log = logging.getLogger("rh_web")

router = APIRouter(tags=["chat"])
PAGINA = Path(__file__).parent / "web" / "chat.html"


def chat_ativo():
    """O chat so sobe se alguem ligar explicitamente. Ver o lifespan do webhook."""
    return os.environ.get("RH_CHAT_ATIVO", "false").strip().lower() == "true"


def _perfis_autorizados():
    bruto = os.environ.get("RH_PERFIS_AUTORIZADOS", "")
    return {p.strip().lower() for p in bruto.split(",") if p.strip()}


def _exigir_acesso(senha_recebida, identidade):
    """Duas checagens, e as duas falham FECHANDO.

    1. A senha confere? `compare_digest` compara em tempo constante -- comparar
       com `==` vaza, pelo tempo da resposta, quantos caracteres iniciais estao
       certos, e isso e suficiente para adivinhar a senha caractere a caractere.
    2. O login declarado esta na lista de autorizados? Esta checagem se repete
       dentro das ferramentas de proposito. Aqui ela economiza a chamada ao
       modelo; la ela vale para QUALQUER canal, inclusive um que alguem
       acrescente amanha e esqueca de proteger.
    """
    esperada = os.environ.get("RH_CHAT_SENHA", "").strip()
    if not esperada:
        log.warning("401: RH_CHAT_SENHA nao configurada")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Acesso nao configurado.")
    if not senha_recebida or not hmac.compare_digest(senha_recebida, esperada):
        log.warning("401: senha do chat invalida (login=%s)", identidade)
        # Mensagem generica: nunca diga QUAL das duas checagens falhou.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Acesso negado.")
    if identidade not in _perfis_autorizados():
        log.warning("403: login sem autorizacao de RH (login=%s)", identidade)
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Este login nao tem acesso aos dados de Gente & Gestao.",
        )


class Pergunta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    de: str = Field(min_length=1, max_length=64,
                    pattern=r"^[A-Za-z0-9._+@-]+$",
                    description="Login de quem esta perguntando.")
    texto: str = Field(min_length=1, max_length=MAX_CARACTERES)


@router.get("/chat", include_in_schema=False)
async def pagina():
    """A pagina em si. HTML estatico -- toda a logica esta no endpoint abaixo."""
    if not PAGINA.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pagina do chat nao encontrada.")
    return FileResponse(PAGINA, media_type="text/html; charset=utf-8")


@router.post("/chat/mensagem")
async def mensagem(
    pergunta: Pergunta,
    request: Request,
    x_chat_senha: str = Header(default="", alias="X-Chat-Senha"),
):
    """Recebe a pergunta do navegador e devolve a resposta do agente."""
    identidade = normalizar_identidade(pergunta.de)
    _exigir_acesso(x_chat_senha, identidade)

    # Limite por identidade, igual ao webhook. Protege contra o F5 nervoso e
    # contra um script deixado rodando -- e uma requisicao recusada aqui nao
    # chega a custar uma chamada de modelo.
    espera = limitador.checar(identidade)
    if espera is not None:
        limitador.esquecer_inativos()
        resposta = JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"ok": False, "erro": {
                "codigo": "excesso_de_requisicoes",
                "mensagem": f"Muitas perguntas seguidas. Tente em {espera}s."}},
        )
        resposta.headers["Retry-After"] = str(espera)
        return resposta

    agente = getattr(request.app.state, "agente_rh", None)
    if agente is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"ok": False, "erro": {
                "codigo": "chat_indisponivel",
                "mensagem": "O agente de Gente & Gestao nao esta ativo neste servidor."}},
        )

    # Metadado no log, nunca o conteudo: a pergunta de um diretor sobre pessoas
    # e justamente o tipo de texto que nao deve viver em ferramenta de
    # monitoramento. O conteudo esta na tabela `mensagens`, que e protegida e
    # pode ser apagada quando alguem pedir.
    log.info("chat de=%s chars=%d", identidade, len(pergunta.texto))
    try:
        resposta = await agente.responder(identidade, pergunta.texto)
    except EntradaInvalida as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    except TetoDiarioAtingido as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(e)) from e
    except TempoEsgotado as e:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, str(e)) from e
    except Exception:
        log.exception("falha no chat (de=%s)", identidade)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Nao foi possivel responder agora.",
        ) from None

    return {
        "ok": True,
        "resposta": resposta.texto,
        "tokens_entrada": resposta.tokens_entrada,
        "tokens_saida": resposta.tokens_saida,
    }


# =============================================================================
# O PAINEL — dados para analise, nao figuras prontas
# -----------------------------------------------------------------------------
# UMA rota entrega tudo: o recorte pedido, ja recalculado em pandas, com todas
# as series que a pagina desenha. O navegador desenha em SVG, entao da para
# passar o mouse e ver o valor exato, e clicar numa barra para filtrar por ela.
#
# POR QUE UMA ROTA SO, E NAO UMA POR GRAFICO. Porque todos os graficos vem do
# MESMO recorte: uma consulta ao banco e um passe de pandas produzem as nove
# series de uma vez. Nove rotas fariam nove consultas iguais e correriam o risco
# de responder com recortes ligeiramente diferentes se o usuario mexesse num
# filtro no meio do caminho.
# =============================================================================

FILTROS_DA_TELA = ("area", "localidade", "nivel", "contrato", "performance",
                   "tempo_de_casa")


def _pool_rh():
    """O pool que o nucleo ja abriu. None se o agente nao subiu."""
    from tools.rh import _rh
    return _rh.pool if _rh is not None else None


def _json_seguro(df):
    """DataFrame -> lista de dicionarios que sobrevive ao json.dumps.

    Tres conversoes que, faltando, quebram em producao e nao no teste:
    NaN vira None (JSON nao tem NaN), datas viram texto ISO, e Decimal --
    que e como o PostgreSQL devolve NUMERIC -- vira float.
    """
    import math
    from datetime import date, datetime
    from decimal import Decimal

    if df is None or not len(df):
        return []
    saida = []
    for linha in df.to_dict(orient="records"):
        limpa = {}
        for k, v in linha.items():
            if isinstance(v, Decimal):
                v = float(v)
            elif isinstance(v, (datetime, date)):
                v = v.isoformat()[:10]
            elif isinstance(v, float) and not math.isfinite(v):
                # ⚠️ NaN E INFINITO. O inf aparece de verdade: num recorte
                # pequeno a regressao pode ter separacao perfeita, e ai a razao
                # de chances vai para infinito. `json.dumps` recusa os dois, e a
                # rota devolvia 200 com corpo quebrado -- erro dificil de achar.
                v = None
            elif hasattr(v, "item"):        # numpy int64/float64/bool_
                v = v.item()
                if isinstance(v, float) and not math.isfinite(v):
                    v = None
            limpa[str(k)] = v
        saida.append(limpa)
    return saida


@router.get("/painel", include_in_schema=False)
async def painel():
    caminho = Path(__file__).parent / "web" / "painel.html"
    if not caminho.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Página do painel não encontrada.")
    return FileResponse(caminho, media_type="text/html; charset=utf-8")


@router.get("/painel/api/contexto", include_in_schema=False)
async def painel_contexto(request: Request):
    """Recalcula tudo para o recorte pedido e devolve as series em JSON.

    Os filtros e as premissas vem da query string. Nada e montado com o texto do
    usuario: os nomes de campo passam pela lista branca de `rh_exploracao`, e os
    numeros das premissas tem limite de sanidade.
    """
    import asyncio

    from rh_exploracao import (Premissas, RecorteVazio, carregar, opcoes,
                               sql_do_recorte)
    from rh_graficos import CATALOGO
    from rh_sql_graficos import DEVOLVE, sql_do_grafico

    pool = _pool_rh()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Banco indisponível.")

    p = dict(request.query_params)
    filtros = [{"campo": c, "operador": "igual", "valor": p[c]}
               for c in FILTROS_DA_TELA if p.get(c) and p[c] != "todos"]
    premissas = Premissas.de_parametros(p)

    try:
        # pandas e a regressao sao sincronos e pesados: vao para uma thread,
        # senao travam o event loop e o chat para de responder enquanto calcula.
        ctx = await asyncio.to_thread(carregar, pool, filtros, premissas)
        menus = await asyncio.to_thread(opcoes, pool)
    except RecorteVazio as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    seg = ctx.segmentos
    faixas = seg[seg.dimensao == "tempo_de_casa"] if len(seg) else seg
    return {
        "pessoas": ctx.pessoas,
        "filtros": {c: p.get(c, "todos") for c in FILTROS_DA_TELA},
        "opcoes": menus,
        "premissas": premissas.como_dict(),
        "indicador": ctx.indicador,
        "serie": _json_seguro(ctx.serie),
        "tempo_de_casa": _json_seguro(faixas),
        "segmentos": _json_seguro(seg),
        "prioridade": _json_seguro(ctx.prioridade),
        "coortes": _json_seguro(ctx.coortes),
        "sobrevivencia": _json_seguro(ctx.sobrevivencia),
        "motivos": _json_seguro(ctx.motivos),
        "fatores": _json_seguro(ctx.fatores),
        "custo": (_json_seguro(ctx.custo) or [{}])[0],
        "catalogo": [
            {"id": g.id, "chave": g.chave, "tipo": g.tipo, "titulo": g.titulo,
             "pergunta": g.pergunta, "slide": g.slide, "fonte": g.fonte,
             "colunas": g.colunas, "metodo": g.metodo, "funcao": g.funcao,
             "recorte": g.recorte, "passos": g.passos, "visual": g.visual,
             "leitura": g.leitura, "tags": g.tags,
             # A consulta que reproduz ESTE grafico -- com o recorte proprio
             # dele dentro do WHERE e as premissas da tela no texto. E o que
             # responde "de onde veio este numero" sem repetir o mesmo SELECT
             # nove vezes.
             "sql": sql_do_grafico(g.id, filtros, premissas.como_dict(),
                                   ctx.data_base),
             "devolve": DEVOLVE.get(g.id, "")}
            for g in CATALOGO.values()
        ],
        # A consulta REALMENTE executada nesta requisicao, com o WHERE que os
        # filtros da tela produziram. Nao e um exemplo: e o que rodou.
        "sql_executado": sql_do_recorte(filtros),
    }


@router.get("/chat/saude", include_in_schema=False)
async def saude(request: Request):
    """A base de RH esta carregada? Diferente de "o servidor esta no ar".

    Vale a distincao: um banco vazio nao da erro -- ele devolve zero, e zero
    parece um numero. Aqui a gente conta as linhas e diz.
    """
    agente = getattr(request.app.state, "agente_rh", None)
    if agente is None:
        return {"ok": False, "motivo": "agente de RH nao ativo"}
    try:
        # Reaproveita o pool que o nucleo abriu na subida -- e o MESMO objeto que
        # as ferramentas usam. Abrir um pool novo a cada checagem de saude e
        # justamente o tipo de vazamento que so aparece quando o monitoramento
        # bate de 30 em 30 segundos por semanas.
        from tools.rh import _rh
        if _rh is None:
            return {"ok": False, "motivo": "conexao de RH nao inicializada"}
        n = await _rh.checar()
        return {"ok": n > 0, "colaboradores": n,
                "aviso": None if n else "a carga ainda nao foi feita "
                                        "(passos/passo25_carregar_rh.py)"}
    except Exception as e:
        log.exception("saude do chat falhou")
        return {"ok": False, "motivo": str(e)}


# =============================================================================
# O APP PROPRIO — para subir SO o chat
# -----------------------------------------------------------------------------
# Tudo abaixo existe para `uv run uvicorn rh_web:app` funcionar sozinho, sem
# arrastar o webhook do n8n junto. O router acima continua exportado, entao o
# `webhook.py` segue podendo monta-lo -- as duas formas convivem, e nenhuma
# duplica regra de negocio: o agente e o mesmo, montado pelo `rh_agente.py`.
#
# REPARE NO QUE NAO TEM AQUI: nenhuma checagem de RH_CHAT_ATIVO. Aquela variavel
# existe para o `webhook.py` decidir se acrescenta o chat ao servidor dele. Quem
# roda `uvicorn rh_web:app` ja disse o que queria ao escolher o comando --
# pedir a confirmacao de novo, numa variavel, so criaria um jeito silencioso de
# o servidor subir sem fazer nada.
# =============================================================================

@asynccontextmanager
async def _ciclo_de_vida(app):
    """Abre o agente UMA vez, na subida. Ver o mesmo padrao em webhook.py."""
    async with abrir_agente_diretoria() as agente:
        app.state.agente_rh = agente
        log.info("assistente de retencao pronto em /chat")
        yield
    log.info("assistente de retencao encerrado")


app = FastAPI(
    title="Assistente de Retencao",
    version="0.1.0",
    lifespan=_ciclo_de_vida,
    # A documentacao automatica fica DESLIGADA aqui de proposito. Este servidor
    # atende diretor, nao integrador: /docs so entregaria o desenho da API para
    # quem abrisse por curiosidade. No `webhook:app` ela continua ligada, porque
    # la existe alguem (o n8n) que precisa dela.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.include_router(router)


@app.middleware("http")
async def _registrar(request: Request, call_next):
    """Da a cada requisicao um id que aparece em todas as linhas de log dela.

    E o mesmo middleware do `webhook.py`, e a duplicacao aqui e deliberada e
    pequena: importar do webhook faria este app carregar o agente geral junto,
    que e exatamente o que ele existe para evitar.
    """
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:8]
    marca = id_requisicao.set(rid)
    inicio = time.perf_counter()
    try:
        resposta = await call_next(request)
        log.info("%s %s -> %s em %.0fms", request.method, request.url.path,
                 resposta.status_code, (time.perf_counter() - inicio) * 1000)
        resposta.headers["X-Request-Id"] = rid
        return resposta
    finally:
        id_requisicao.reset(marca)


@app.exception_handler(HTTPException)
async def _erro(request, exc):
    """Erro no MESMO formato do webhook, para a pagina ler de um jeito so."""
    codigos = {401: "nao_autorizado", 403: "proibido", 404: "nao_encontrado",
               429: "excesso_de_requisicoes", 503: "chat_indisponivel",
               504: "tempo_esgotado"}
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "erro": {"codigo": codigos.get(exc.status_code, "erro"),
                                       "mensagem": exc.detail}},
    )


@app.get("/", include_in_schema=False)
async def _raiz():
    """Abrir a raiz no navegador leva para o chat, em vez de dar 404."""
    return RedirectResponse("/chat")
