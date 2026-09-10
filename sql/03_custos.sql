-- =============================================================================
-- 03_custos.sql — quanto cada requisicao consumiu
--   psql -U agente_rh_app -h localhost -d agente_rh -f sql/03_custos.sql
-- -----------------------------------------------------------------------------
-- POR QUE GRAVAR ISTO NO BANCO, e nao so somar na memoria?
-- Porque a memoria some quando o servidor reinicia. Um teto DIARIO que zera a
-- cada `Ctrl+C` nao protege nada: bastaria o servidor reiniciar para o gasto
-- "voltar do zero". No banco, o numero sobrevive.
-- E porque so assim voce consegue responder depois: "quanto o usuario X me
-- custou este mes?" -- pergunta que nao da para responder olhando a fatura da
-- OpenAI, que vem num numero so.
-- =============================================================================

CREATE TABLE IF NOT EXISTS custos (
    id              BIGSERIAL PRIMARY KEY,

    -- De quem foi o gasto. Permite ver o custo POR USUARIO.
    owner           TEXT NOT NULL,
    thread_id       TEXT NOT NULL,

    -- Os dois numeros pelos quais a OpenAI cobra. Guardamos SEPARADOS porque
    -- eles tem precos MUITO diferentes: a saida costuma custar 4x a entrada.
    -- Somar os dois num campo so perderia essa informacao para sempre.
    tokens_entrada  INTEGER NOT NULL,
    tokens_saida    INTEGER NOT NULL,

    -- Qual modelo respondeu. Guardamos porque o preco muda por modelo -- e
    -- porque no dia em que voce trocar de modelo, o historico antigo continua
    -- calculavel com o preco certo.
    modelo          TEXT NOT NULL,

    -- Quantas vezes o modelo foi chamado NESTA requisicao. Um numero alto aqui
    -- e sinal de laco de ferramentas -- o gasto que o limite por minuto nao ve.
    chamadas        INTEGER NOT NULL DEFAULT 1,

    criada_em       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indice para a pergunta mais frequente: "quanto gastei HOJE?"
CREATE INDEX IF NOT EXISTS idx_custos_data ON custos (criada_em);

-- Indice para "quanto o usuario X gastou no periodo?"
CREATE INDEX IF NOT EXISTS idx_custos_owner ON custos (owner, criada_em);
