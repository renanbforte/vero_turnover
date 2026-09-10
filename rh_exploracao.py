# =============================================================================
# rh_exploracao.py — recalcular TUDO em pandas, com filtros e premissas do usuario
# -----------------------------------------------------------------------------
# O QUE MUDA EM RELACAO AO `rh_graficos.py` ORIGINAL
#
# Antes: cada grafico tinha um SELECT que lia uma tabela JA AGREGADA
# (`rh.segmentos`, `rh.coortes`...). Rapido, mas engessado -- aquelas tabelas
# foram calculadas na carga, para a empresa inteira. Nao ha como pedir "o mesmo
# grafico, so para o Comercial de Sao Paulo": a linha simplesmente nao existe.
#
# Agora: carrega-se `rh.colaboradores` FILTRADO e reprocessa-se em pandas, com
# as MESMAS funcoes que geraram os numeros do deck (`rh_analise.py`). O grafico
# passa a responder a qualquer recorte, e continua sendo a mesma conta.
#
# ISSO E O QUE PERMITE ANALISAR AO VIVO. Numa banca, "e se olharmos so o
# Atendimento?" deixa de ser uma pergunta para depois e vira um clique.
#
# ⚠️ AS PREMISSAS TAMBEM SAO EDITAVEIS, E ISSO E DE PROPOSITO.
# O deck afirma que o custo de reposicao usa a parte baixa da faixa de mercado e
# que "se acharem o fator alto, o modelo recalcula". Aqui essa frase deixa de ser
# promessa: os fatores viram campo, e o numero muda na tela. E a forma mais
# honesta de apresentar uma premissa -- deixando quem duvida mexer nela.
#
# O QUE **NAO** MUDA: a trava de privacidade. Todo recorte que resultar em menos
# de 5 pessoas e recusado, aqui como nas ferramentas do agente. Filtrar ate
# sobrar uma pessoa nao e uma analise -- e uma reidentificacao.
# =============================================================================

import logging
from dataclasses import dataclass, field

import pandas as pd

import rh_analise
from rh_consultas import DIMENSOES, FILTROS_NUMERICOS, MINIMO_POR_GRUPO

log = logging.getLogger("rh_exploracao")

# As colunas do banco tem nomes longos; as funcoes de `rh_analise` esperam os
# nomes curtos da planilha. Este mapa e a ponte -- e existir UM mapa, aqui, e o
# que permite reaproveitar aquelas funcoes sem tocar numa linha delas.
DE_PARA = {
    "id_colaborador": "id", "nivel_cargo": "nivel", "tipo_contrato": "contrato",
    "data_admissao": "admissao", "data_desligamento": "desligamento",
    "status_desligamento": "status", "motivo_desligamento": "motivo",
    "tempo_empresa_anos": "tempo_anos", "salario_base": "salario",
    "compa_ratio": "compa", "indice_lideranca": "lideranca",
    "horas_extras_mes": "horas_extras", "absenteismo_dias_12m": "absenteismo",
    "meses_desde_promocao": "meses_promocao", "horas_treinamento_12m": "treinamento",
    "movimentos_internos_24m": "movimentos", "performance": "performance",
    "engajamento": "engajamento", "area": "area", "localidade": "localidade",
}


@dataclass
class Premissas:
    """As premissas de custo, que o usuario pode alterar na tela.

    Os padroes sao os do deck. Alterar aqui muda o custo em todos os graficos e
    numeros da pagina de uma vez, porque tudo e recalculado a partir do retrato
    -- e nao lido de uma tabela ja somada.
    """
    meses_ano: float = rh_analise.MESES_ANO
    encargos: float = rh_analise.ENCARGOS
    fatores: dict = field(default_factory=lambda: dict(rh_analise.FATOR_REPOSICAO))

    @classmethod
    def de_parametros(cls, p: dict):
        """Le as premissas da query string, com limites de sanidade.

        Os limites nao sao burocracia: sem eles, um encargo de 900 devolveria um
        custo de bilhoes e a pagina exibiria isso com toda a seriedade.
        """
        def num(chave, padrao, minimo, maximo):
            try:
                v = float(str(p.get(chave, padrao)).replace(",", "."))
            except (TypeError, ValueError):
                return padrao
            return max(minimo, min(maximo, v))

        fatores = dict(rh_analise.FATOR_REPOSICAO)
        for nivel in fatores:
            fatores[nivel] = num(f"fator_{nivel.lower()}", fatores[nivel], 0.05, 3.0)
        return cls(
            meses_ano=num("meses_ano", rh_analise.MESES_ANO, 12.0, 16.0),
            encargos=num("encargos", rh_analise.ENCARGOS, 1.0, 2.5),
            fatores=fatores,
        )

    def como_dict(self):
        return {"meses_ano": self.meses_ano, "encargos": self.encargos,
                **{f"fator_{k.lower()}": v for k, v in self.fatores.items()}}


class RecorteVazio(Exception):
    """O filtro deixou gente de menos. Ver MINIMO_POR_GRUPO."""


def _montar_filtro(filtros):
    """(WHERE, parametros) a partir da lista branca. Mesma trava do agente.

    Nenhum nome de coluna vem do texto do usuario: ele e PROCURADO nos
    dicionarios `DIMENSOES`/`FILTROS_NUMERICOS`. O que nao esta la vira erro, e
    os valores viajam como parametro -- nunca concatenados.
    """
    onde, valores = [], []
    for f in filtros or []:
        campo, operador, valor = f.get("campo"), f.get("operador", "igual"), f.get("valor")
        if valor in (None, "", "todos"):
            continue
        if campo in DIMENSOES and operador == "igual":
            onde.append(f"{DIMENSOES[campo]} = %s")
            valores.append(str(valor))
        elif campo in FILTROS_NUMERICOS and operador in ("maior_que", "menor_que"):
            onde.append(f"{FILTROS_NUMERICOS[campo]} {'>=' if operador == 'maior_que' else '<'} %s")
            valores.append(float(valor))
        else:
            raise ValueError(f"Filtro inválido: {campo!r} / {operador!r}")
    return (("WHERE " + " AND ".join(onde)) if onde else ""), valores


def sql_do_recorte(filtros, para_exibir=True):
    """O SELECT que carregou o recorte -- o de verdade, nao um exemplo.

    `para_exibir=True` embute os valores no texto, para dar para copiar e rodar
    no psql. NA EXECUCAO eles nao vao embutidos: viajam como parametro (%s), que
    e o que impede injecao. Sao coisas diferentes de proposito, e a pagina diz
    isso ao lado da consulta.
    """
    onde, valores = _montar_filtro(filtros)
    if not onde:
        return "SELECT *\n  FROM rh.colaboradores"
    if not para_exibir:
        return f"SELECT *\n  FROM rh.colaboradores\n {onde}"
    texto = onde
    for v in valores:
        texto = texto.replace("%s", (f"'{v}'" if isinstance(v, str) else str(v)), 1)
    return ("SELECT *\n  FROM rh.colaboradores\n "
            + texto.replace(" AND ", "\n   AND "))


@dataclass
class Contexto:
    """Tudo que a pagina precisa, ja calculado para o recorte pedido."""
    pessoas: int
    filtros: list
    premissas: Premissas
    data_base: pd.Timestamp
    colaboradores: pd.DataFrame
    indicador: dict
    serie: pd.DataFrame
    segmentos: pd.DataFrame
    prioridade: pd.DataFrame
    coortes: pd.DataFrame
    sobrevivencia: pd.DataFrame
    motivos: pd.DataFrame
    fatores: pd.DataFrame
    custo: pd.DataFrame


def carregar(pool, filtros=None, premissas=None, data_base=None):
    """Le o recorte, recalcula tudo em pandas e devolve o Contexto.

    O CAMINHO, em quatro passos:
      1. SELECT em `rh.colaboradores` com os filtros (lista branca).
      2. Renomeia as colunas para o vocabulario do `rh_analise`.
      3. `preparar()` recalcula exposicao, faixa e custo -- este ultimo com as
         PREMISSAS DO USUARIO, e nao com as gravadas na carga.
      4. As mesmas funcoes do deck produzem serie, segmentos, coortes, etc.

    O passo 3 e o que faz a premissa editavel funcionar de verdade: o custo NAO
    e lido da coluna `custo_reposicao` gravada no banco, e sim recalculado do
    salario e do nivel. Ler a coluna seria mais rapido e ignoraria o que o
    usuario acabou de digitar.
    """
    premissas = premissas or Premissas()
    onde, valores = _montar_filtro(filtros)

    with pool.connection() as conn, conn.cursor() as cur:
        if data_base is None:
            cur.execute("SELECT valor FROM rh.premissas WHERE chave = 'data_base'")
            linha = cur.fetchone()
            data_base = pd.Timestamp(
                pd.to_datetime(linha[0], dayfirst=True) if linha else "2026-06-30")
        cur.execute(f"SELECT * FROM rh.colaboradores {onde}", valores)
        colunas = [c.name for c in cur.description]
        bruto = pd.DataFrame(cur.fetchall(), columns=colunas)

    if len(bruto) < MINIMO_POR_GRUPO:
        raise RecorteVazio(
            f"O recorte tem {len(bruto)} pessoa(s). São necessárias pelo menos "
            f"{MINIMO_POR_GRUPO} — abaixo disso, uma média já é dado individual."
        )

    df = bruto.rename(columns=DE_PARA)
    for c in ("admissao", "desligamento"):
        df[c] = pd.to_datetime(df[c])
    # NUMERIC do PostgreSQL chega como Decimal; as contas precisam de float.
    for c in ("tempo_anos", "salario", "compa", "engajamento", "lideranca",
              "horas_extras", "absenteismo", "meses_promocao", "treinamento",
              "movimentos"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["elegivel"] = "Sim"

    # As premissas do usuario entram AQUI, sobrescrevendo as do modulo -- e por
    # isso o custo muda na tela quando alguem edita um fator.
    antigos = (rh_analise.MESES_ANO, rh_analise.ENCARGOS, dict(rh_analise.FATOR_REPOSICAO))
    rh_analise.MESES_ANO = premissas.meses_ano
    rh_analise.ENCARGOS = premissas.encargos
    rh_analise.FATOR_REPOSICAO.update(premissas.fatores)
    try:
        preparado, _ = rh_analise.preparar(df, data_base)
        indicador = rh_analise.indicador(preparado)
        serie = rh_analise.serie_mensal(preparado, data_base, "2024-01-31")
        segmentos = rh_analise.segmentos(preparado)
        coortes = rh_analise.coortes(preparado, data_base)
        sobrevivencia = rh_analise.sobrevivencia(preparado, data_base)
        motivos = rh_analise.motivos(preparado)
        try:
            prioridade = rh_analise.prioridade(preparado)
        except Exception:
            prioridade = pd.DataFrame()
        try:
            fatores = rh_analise.fatores(preparado)
        except Exception:
            # Recorte pequeno costuma deixar a regressao sem variacao suficiente.
            # Um grafico a menos e melhor que a pagina inteira falhando.
            log.info("regressao não convergiu neste recorte")
            fatores = pd.DataFrame()
    finally:
        rh_analise.MESES_ANO, rh_analise.ENCARGOS = antigos[0], antigos[1]
        rh_analise.FATOR_REPOSICAO.clear()
        rh_analise.FATOR_REPOSICAO.update(antigos[2])

    saiu = preparado[preparado.saiu]
    entrada = preparado[(preparado.faixa == "0-12 meses") & preparado.saiu]
    custo = pd.DataFrame([{
        "custo_vol": float(preparado.loc[preparado.saiu_vol, "custo_reposicao"].sum()),
        "custo_invol": float(preparado.loc[preparado.saiu_invol, "custo_reposicao"].sum()),
        "custo_entrada": float(entrada.custo_reposicao.sum()),
        "custo_total": float(saiu.custo_reposicao.sum()),
        "custo_medio_vol": float(preparado.loc[preparado.saiu_vol, "custo_reposicao"].mean() or 0),
        "folha_anual": float(preparado.loc[preparado.status.eq("Ativo"), "remuneracao_anual"].sum()),
    }])

    log.info("recorte: %d pessoas, %d filtros", len(preparado), len(filtros or []))
    return Contexto(
        pessoas=len(preparado), filtros=filtros or [], premissas=premissas,
        data_base=data_base,
        colaboradores=preparado, indicador=indicador, serie=serie,
        segmentos=segmentos, prioridade=prioridade, coortes=coortes,
        sobrevivencia=sobrevivencia, motivos=motivos, fatores=fatores, custo=custo,
    )


def opcoes(pool):
    """Os valores disponiveis em cada filtro, lidos do banco.

    Lidos, e nao escritos a mao: se a base mudar de areas, os menus acompanham
    sozinhos. Menu com opcao que nao existe mais e um jeito silencioso de
    devolver tela vazia.
    """
    consultas = {
        "area": "SELECT DISTINCT area FROM rh.colaboradores ORDER BY 1",
        "localidade": "SELECT DISTINCT localidade FROM rh.colaboradores ORDER BY 1",
        "nivel": "SELECT DISTINCT nivel_cargo FROM rh.colaboradores ORDER BY 1",
        "contrato": "SELECT DISTINCT tipo_contrato FROM rh.colaboradores ORDER BY 1",
        "performance": "SELECT DISTINCT performance FROM rh.colaboradores"
                       " WHERE performance IS NOT NULL ORDER BY 1",
        "tempo_de_casa": "SELECT DISTINCT faixa_tempo_casa FROM rh.colaboradores ORDER BY 1",
    }
    saida = {}
    with pool.connection() as conn, conn.cursor() as cur:
        for chave, sql in consultas.items():
            cur.execute(sql)
            saida[chave] = [r[0] for r in cur.fetchall()]
    return saida
