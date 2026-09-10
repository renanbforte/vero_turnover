-- =============================================================================
-- 05_rh.sql — as tabelas do agente de RH (schema `rh`)
--   psql -U agente_rh_app -h localhost -d agente_rh -f sql/05_rh.sql
-- -----------------------------------------------------------------------------
-- POR QUE UM SCHEMA SEPARADO, E NAO TABELAS SOLTAS NO `public`?
-- Porque estes dados sao de NATUREZA diferente do resto do banco. `conversas`,
-- `mensagens` e `documentos` sao do agente. `rh.*` e dado de PESSOAS -- o tipo
-- de dado que um dia alguem vai auditar, e que precisa de uma fronteira visivel.
-- Um schema e essa fronteira: da para conceder ou revogar acesso a `rh` inteiro
-- numa linha (`REVOKE USAGE ON SCHEMA rh FROM ...`), o que e impossivel quando
-- as tabelas estao misturadas com as outras.
--
-- A DECISAO MAIS IMPORTANTE DESTE ARQUIVO ESTA NO FIM: as VIEWS `rh.vw_*`.
-- O agente NUNCA le `rh.colaboradores` diretamente. Ele le views que so
-- devolvem AGREGADO, e que suprimem qualquer grupo com menos de 5 pessoas.
-- Isso nao e enfeite: e o que impede a pergunta "quem esta pensando em sair?"
-- de ter resposta. Uma regra que pode ser garantida pelo banco deve ser
-- garantida pelo banco -- se depender do prompt, um dia alguem contorna.
--
-- POR QUE AS METRICAS JA VEM CALCULADAS, E NAO SAO CALCULADAS NA PERGUNTA?
-- Porque turnover tem MUITAS definicoes possiveis, e a diretoria precisa que
-- duas perguntas parecidas devolvam o MESMO numero. Se a formula ficasse a
-- cargo do modelo montar na hora, "qual o turnover do Comercial?" e "quanto o
-- Comercial perdeu de gente?" poderiam divergir -- e ai o painel perde a
-- confianca, que e a unica coisa que ele tem. A formula mora no `rh_analise.py`,
-- roda UMA vez na carga, e o resultado vira linha aqui.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS rh;


-- =============================================================================
-- 1. O RETRATO — uma linha por colaborador
-- -----------------------------------------------------------------------------
-- Esta e a unica tabela com dado individual. Ela existe porque as agregacoes
-- precisam de uma origem, e porque recarregar a base tem que ser possivel sem
-- depender do arquivo original estar por perto.
--
-- REPARE NAS COLUNAS DERIVADAS (faixa_tempo_casa, exposicao_ltm, saiu_vol_ltm,
-- custo_reposicao). Elas poderiam ser calculadas no SELECT. Estao gravadas de
-- proposito: sao REGRAS DE NEGOCIO (o que conta como saida voluntaria, como se
-- mede exposicao, quanto custa repor um nivel). Regra em coluna e regra que
-- todo mundo le igual. Regra em SELECT e regra que cada consulta escreve de um
-- jeito -- e um dia duas ficam diferentes sem ninguem perceber.
-- =============================================================================
CREATE TABLE IF NOT EXISTS rh.colaboradores (
    id_colaborador          TEXT PRIMARY KEY,

    -- Recortes organizacionais.
    area                    TEXT NOT NULL,
    localidade              TEXT NOT NULL,
    nivel_cargo             TEXT NOT NULL,
    tipo_contrato           TEXT NOT NULL,

    -- DATE e nao TIMESTAMPTZ aqui: admissao e desligamento sao datas de
    -- calendario, nao instantes. Quem tem hora tem fuso, e fuso em data de
    -- contrato so gera confusao ("admitido dia 1 ou dia 31?").
    data_admissao           DATE NOT NULL,
    data_desligamento       DATE,          -- NULL = ativo. E informacao, nao falha.

    -- CHECK no banco: a ultima linha de defesa contra um bug na carga gravar
    -- 'Ativo ' com espaco, ou 'ativo' minusculo. Depois que entra torto, toda
    -- agregacao fica torta junto.
    status_desligamento     TEXT NOT NULL
        CHECK (status_desligamento IN ('Ativo', 'Voluntario', 'Involuntario')),
    motivo_desligamento     TEXT,

    -- Medidas do arquivo original.
    tempo_empresa_anos      NUMERIC(6,2) NOT NULL,
    salario_base            NUMERIC(12,2) NOT NULL,
    compa_ratio             NUMERIC(5,3) NOT NULL,
    performance             TEXT,
    engajamento             SMALLINT CHECK (engajamento BETWEEN 0 AND 100),
    indice_lideranca        SMALLINT CHECK (indice_lideranca BETWEEN 0 AND 100),
    horas_extras_mes        NUMERIC(6,2),
    absenteismo_dias_12m    SMALLINT,
    -- Fica NULL de proposito: na base fornecida, 60% dos valores preenchidos
    -- eram MAIORES que o tempo de casa da propria pessoa (impossivel). A carga
    -- rejeita os incoerentes em vez de grava-los. Ver `rh.qualidade`.
    meses_desde_promocao    SMALLINT,
    horas_treinamento_12m   NUMERIC(6,2),
    movimentos_internos_24m SMALLINT,

    -- -- derivadas na carga (regra de negocio congelada) ---------------------
    faixa_tempo_casa        TEXT NOT NULL,     -- '0-12 meses' | '1-2 anos' | ...
    -- Exposicao em FTE-ano dentro da janela de 12 meses. E o DENOMINADOR do
    -- indicador. Guardado por pessoa para que qualquer recorte novo (uma area,
    -- um cruzamento) possa somar a exposicao do proprio grupo -- e nao herdar
    -- um denominador de outro lugar.
    exposicao_ltm           NUMERIC(8,4) NOT NULL DEFAULT 0,
    saiu_ltm                BOOLEAN NOT NULL DEFAULT FALSE,
    saiu_vol_ltm            BOOLEAN NOT NULL DEFAULT FALSE,
    saiu_invol_ltm          BOOLEAN NOT NULL DEFAULT FALSE,
    custo_reposicao         NUMERIC(12,2) NOT NULL DEFAULT 0,

    carregado_em            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Os indices seguem os recortes que o agente realmente pede. Indice que
-- ninguem usa custa escrita e nao devolve nada.
CREATE INDEX IF NOT EXISTS idx_rh_colab_area    ON rh.colaboradores (area, faixa_tempo_casa);
CREATE INDEX IF NOT EXISTS idx_rh_colab_local   ON rh.colaboradores (localidade);
CREATE INDEX IF NOT EXISTS idx_rh_colab_status  ON rh.colaboradores (status_desligamento);
CREATE INDEX IF NOT EXISTS idx_rh_colab_desl    ON rh.colaboradores (data_desligamento)
    WHERE data_desligamento IS NOT NULL;


-- =============================================================================
-- 2. A SERIE MENSAL — headcount reconstruido mes a mes
-- -----------------------------------------------------------------------------
-- A base e um RETRATO (uma foto da data-base), nao um historico. Mas as datas
-- de admissao e desligamento permitem reconstruir quantas pessoas havia em
-- qualquer mes: quem entrou antes e ainda nao saiu. Sem esta tabela, o agente
-- so consegue falar de "agora" -- e diretoria pergunta tendencia.
-- =============================================================================
CREATE TABLE IF NOT EXISTS rh.metricas_mensais (
    mes                     DATE PRIMARY KEY,      -- ultimo dia do mes
    headcount_inicio        INTEGER NOT NULL,
    headcount_fim           INTEGER NOT NULL,
    headcount_medio         NUMERIC(10,2) NOT NULL,
    admissoes               INTEGER NOT NULL,
    desligamentos           INTEGER NOT NULL,
    voluntarios             INTEGER NOT NULL,
    involuntarios           INTEGER NOT NULL,
    -- NULL nos 11 primeiros meses: a janela movel de 12 meses ainda nao fechou.
    -- NULL aqui e honesto; 0 seria mentira, e o agente leria como "nao houve
    -- turnover" em vez de "ainda nao da para calcular".
    turnover_12m            NUMERIC(6,2),
    turnover_vol_12m        NUMERIC(6,2),
    turnover_invol_12m      NUMERIC(6,2)
);


-- =============================================================================
-- 3. SEGMENTOS — formato LONGO, e nao uma tabela por dimensao
-- -----------------------------------------------------------------------------
-- POR QUE LONGO (dimensao, valor) EM VEZ DE rh.por_area, rh.por_localidade...?
-- Porque assim UMA ferramenta atende todas as dimensoes. Com uma tabela por
-- dimensao, cada recorte novo exigiria uma ferramenta nova -- e cada ferramenta
-- ocupa contexto em TODA mensagem que o agente processa. Formato longo troca
-- seis ferramentas por uma com um parametro.
-- =============================================================================
CREATE TABLE IF NOT EXISTS rh.segmentos (
    dimensao                TEXT NOT NULL,   -- 'area','localidade','nivel','contrato','performance','tempo_de_casa'
    valor                   TEXT NOT NULL,
    headcount_ativo         INTEGER NOT NULL,
    exposicao               NUMERIC(10,2) NOT NULL,
    desl_voluntarios        INTEGER NOT NULL,
    desl_involuntarios      INTEGER NOT NULL,
    turnover_vol            NUMERIC(6,2) NOT NULL,
    turnover_total          NUMERIC(6,2) NOT NULL,
    share_saidas_vol        NUMERIC(6,2) NOT NULL,
    -- Quantas saidas o segmento teve A MAIS do que teria com a taxa media da
    -- empresa. E este numero -- e nao a taxa -- que responde "onde agir
    -- primeiro": taxa alta em grupo de 10 pessoas nao move o ponteiro.
    excesso_saidas          NUMERIC(8,2) NOT NULL,
    custo_excesso           NUMERIC(14,2) NOT NULL,
    PRIMARY KEY (dimensao, valor)
);


-- =============================================================================
-- 4. MATRIZ DE PRIORIDADE — area x faixa de tempo de casa
-- -----------------------------------------------------------------------------
-- O cruzamento existe separado porque ele responde uma pergunta que nenhuma
-- dimensao sozinha responde: "a area X tem turnover alto por ser mal
-- gerenciada, ou porque contratou muita gente nova?". So o cruzamento separa
-- efeito de COMPOSICAO de efeito de GESTAO.
-- =============================================================================
CREATE TABLE IF NOT EXISTS rh.prioridade (
    area                    TEXT NOT NULL,
    faixa_tempo_casa        TEXT NOT NULL,
    headcount               INTEGER NOT NULL,
    exposicao               NUMERIC(10,2) NOT NULL,
    saidas_vol              INTEGER NOT NULL,
    turnover_vol            NUMERIC(6,2) NOT NULL,
    esperado_no_baseline    NUMERIC(8,2) NOT NULL,
    excesso_saidas          NUMERIC(8,2) NOT NULL,
    custo_excesso           NUMERIC(14,2) NOT NULL,
    PRIMARY KEY (area, faixa_tempo_casa)
);


-- =============================================================================
-- 5. COORTES E SOBREVIVENCIA — a analise da ENTRADA
-- =============================================================================
CREATE TABLE IF NOT EXISTS rh.coortes (
    coorte                  TEXT PRIMARY KEY,      -- '2025Q4'
    contratacoes            INTEGER NOT NULL,
    -- NULL quando a coorte ainda nao teve tempo de ser observada naquele
    -- horizonte. Quem entrou ha 2 meses NAO pode contar como "ficou 12 meses".
    saida_vol_3m            NUMERIC(6,2),
    saida_vol_6m            NUMERIC(6,2),
    saida_vol_12m           NUMERIC(6,2)
);

CREATE TABLE IF NOT EXISTS rh.sobrevivencia (
    mes_de_casa             SMALLINT PRIMARY KEY,
    em_risco                INTEGER NOT NULL,
    risco_acumulado         NUMERIC(6,2) NOT NULL   -- Kaplan-Meier, em %
);


-- =============================================================================
-- 6. FATORES ASSOCIADOS — a regressao, com o intervalo de confianca junto
-- -----------------------------------------------------------------------------
-- O IC vem gravado porque um numero de razao de chances SEM intervalo e um
-- numero que finge certeza. O agente e instruido a nunca citar a razao sem
-- dizer se ela e significativa -- e isso so e possivel se o dado estiver aqui.
-- =============================================================================
CREATE TABLE IF NOT EXISTS rh.fatores (
    fator                   TEXT PRIMARY KEY,
    odds_ratio              NUMERIC(8,3) NOT NULL,
    ic_inferior             NUMERIC(8,3) NOT NULL,
    ic_superior             NUMERIC(8,3) NOT NULL,
    p_valor                 NUMERIC(8,5) NOT NULL,
    significativo           BOOLEAN NOT NULL
);


-- =============================================================================
-- 7. MOTIVOS DECLARADOS — com o perfil de quem declarou cada um
-- -----------------------------------------------------------------------------
-- As colunas de perfil (engajamento, lideranca) existem para permitir a leitura
-- mais util desta tabela: o motivo declarado costuma ser o GATILHO, nao a causa.
-- Sem elas, o agente so consegue repetir o que a entrevista de desligamento
-- registrou -- que e justamente o dado mais enviesado do RH.
-- =============================================================================
CREATE TABLE IF NOT EXISTS rh.motivos (
    motivo                  TEXT PRIMARY KEY,
    quantidade              INTEGER NOT NULL,
    share                   NUMERIC(6,2) NOT NULL,
    engajamento_medio       NUMERIC(6,2),
    lideranca_media         NUMERIC(6,2),
    compa_medio             NUMERIC(6,3),
    tempo_casa_medio        NUMERIC(6,2)
);


-- =============================================================================
-- 8. PREMISSAS E QUALIDADE — o que o agente precisa para se explicar
-- -----------------------------------------------------------------------------
-- Estas duas tabelas parecem administrativas e sao as mais importantes para a
-- CONFIANCA. Um diretor que recebe "R$ 15,3 milhoes" vai perguntar "calculado
-- como?". Se o agente nao souber responder, o numero morre ali.
-- =============================================================================
CREATE TABLE IF NOT EXISTS rh.premissas (
    chave                   TEXT PRIMARY KEY,
    valor                   TEXT NOT NULL,
    descricao               TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rh.qualidade (
    teste                   TEXT PRIMARY KEY,
    resultado               TEXT NOT NULL,
    -- 'OK' | 'ATENCAO' | 'DESCARTAR' | 'LIMITACAO' | 'SEM USO'
    decisao                 TEXT NOT NULL
);


-- =============================================================================
-- 9. AS VIEWS DO AGENTE — a fronteira que impede resposta individual
-- -----------------------------------------------------------------------------
-- ⚠️ ESTA E A PARTE QUE PROTEGE PESSOAS. Leia antes de mexer.
--
-- O agente so enxerga o que esta aqui. `rh.colaboradores` fica fora do alcance
-- das ferramentas de proposito: a pergunta "quem esta em risco de sair?" NAO
-- pode ter resposta, e a forma de garantir isso nao e pedir por favor no
-- prompt -- e nao existir consulta que devolva linha de pessoa.
--
-- O CORTE DE 5 PESSOAS (`HAVING count(*) >= 5`) fecha a porta dos fundos:
-- sem ele, bastaria filtrar ate sobrar um ("area = Juridico E localidade =
-- Campinas E nivel = Lideranca") para reidentificar alguem a partir de uma
-- media. Grupo pequeno demais simplesmente nao aparece.
-- =============================================================================

-- 9a. O indicador da empresa inteira, na janela de 12 meses.
CREATE OR REPLACE VIEW rh.vw_indicador AS
SELECT
    count(*) FILTER (WHERE status_desligamento = 'Ativo')      AS headcount_ativo,
    round(sum(exposicao_ltm), 1)                               AS exposicao_fte_ano,
    count(*) FILTER (WHERE saiu_vol_ltm)                       AS desl_voluntarios,
    count(*) FILTER (WHERE saiu_invol_ltm)                     AS desl_involuntarios,
    round(100.0 * count(*) FILTER (WHERE saiu_vol_ltm)
          / nullif(sum(exposicao_ltm), 0), 1)                  AS turnover_vol,
    round(100.0 * count(*) FILTER (WHERE saiu_ltm)
          / nullif(sum(exposicao_ltm), 0), 1)                  AS turnover_total,
    round(sum(custo_reposicao) FILTER (WHERE saiu_ltm), 0)     AS custo_saidas_12m,
    count(*) FILTER (WHERE status_desligamento = 'Ativo'
                       AND tempo_empresa_anos < 1)             AS ativos_em_rampa
FROM rh.colaboradores;

-- 9b. O cubo pre-agregado, para exploracao MANUAL (psql, BI, planilha).
-- ⚠️ A ferramenta flexivel do agente NAO usa esta view: ela agrupa direto na
-- tabela, no nivel que a pergunta pediu, aplicando o MESMO corte de 5 pessoas
-- (ver MINIMO_POR_GRUPO em rh_consultas.py). O motivo e aritmetico: esta view
-- ja agrupa pelas 6 dimensoes, entao reagregar a partir dela PERDERIA as
-- pessoas das celulas suprimidas e os totais nao fechariam.
-- Ela existe para quem for olhar o dado na mao com a mesma protecao.
CREATE OR REPLACE VIEW rh.vw_cubo AS
SELECT
    area, localidade, nivel_cargo, tipo_contrato, performance, faixa_tempo_casa,
    count(*)                                                   AS pessoas,
    count(*) FILTER (WHERE status_desligamento = 'Ativo')      AS headcount_ativo,
    round(sum(exposicao_ltm), 2)                               AS exposicao,
    count(*) FILTER (WHERE saiu_vol_ltm)                       AS desl_voluntarios,
    count(*) FILTER (WHERE saiu_invol_ltm)                     AS desl_involuntarios,
    round(avg(engajamento), 1)                                 AS engajamento_medio,
    round(avg(indice_lideranca), 1)                            AS lideranca_media,
    round(avg(compa_ratio), 3)                                 AS compa_medio,
    round(avg(horas_extras_mes), 1)                            AS horas_extras_medias,
    round(avg(salario_base), 0)                                AS salario_medio,
    round(sum(custo_reposicao) FILTER (WHERE saiu_ltm), 0)     AS custo_saidas
FROM rh.colaboradores
GROUP BY area, localidade, nivel_cargo, tipo_contrato, performance, faixa_tempo_casa
HAVING count(*) >= 5;          -- <- a trava de k-anonimato. Nao remova.

-- 9c. A serie mensal ja formatada para leitura.
CREATE OR REPLACE VIEW rh.vw_serie AS
SELECT to_char(mes, 'YYYY-MM')  AS competencia,
       headcount_fim, admissoes, desligamentos, voluntarios, involuntarios,
       turnover_12m, turnover_vol_12m, turnover_invol_12m
  FROM rh.metricas_mensais
 ORDER BY mes;


-- =============================================================================
-- 10. CONFERENCIA — rode depois da carga
-- -----------------------------------------------------------------------------
-- Uma tabela vazia nao da erro: da resposta errada em silencio, que e pior.
-- Este SELECT existe para voce ver, com os proprios olhos, que a carga entrou.
-- =============================================================================
-- SELECT 'colaboradores' t, count(*) FROM rh.colaboradores
-- UNION ALL SELECT 'metricas_mensais', count(*) FROM rh.metricas_mensais
-- UNION ALL SELECT 'segmentos',        count(*) FROM rh.segmentos
-- UNION ALL SELECT 'prioridade',       count(*) FROM rh.prioridade
-- UNION ALL SELECT 'coortes',          count(*) FROM rh.coortes
-- UNION ALL SELECT 'sobrevivencia',    count(*) FROM rh.sobrevivencia
-- UNION ALL SELECT 'fatores',          count(*) FROM rh.fatores
-- UNION ALL SELECT 'motivos',          count(*) FROM rh.motivos
-- UNION ALL SELECT 'premissas',        count(*) FROM rh.premissas
-- UNION ALL SELECT 'qualidade',        count(*) FROM rh.qualidade;
