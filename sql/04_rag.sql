-- =============================================================================
-- 04_rag.sql — a tabela dos seus documentos, com vetores
--   A extensao precisa de SUPERUSUARIO (roda como postgres):
--     psql -U postgres -h localhost -d agente_rh -c "CREATE EXTENSION IF NOT EXISTS vector;"
--   O resto roda como agente_app:
--     psql -U agente_rh_app -h localhost -d agente_rh -f sql/04_rag.sql
-- =============================================================================

-- Se voce for superusuario, esta linha ja resolve. Senao, rode o comando
-- separado acima antes deste arquivo.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documentos (
    id          BIGSERIAL PRIMARY KEY,

    -- De quem e o documento. Mesma coluna `owner` das outras tabelas, pelo
    -- mesmo motivo: um usuario nao pode achar o documento do outro. Sem isso,
    -- perguntar "quanto custa o plano?" traria o contrato de outra empresa.
    owner       TEXT NOT NULL,

    -- De onde o trecho veio (nome do arquivo, URL, id do registro). E o que
    -- permite CITAR a fonte na resposta -- sem isso o agente responde e voce
    -- nao tem como conferir se ele inventou.
    fonte       TEXT NOT NULL,

    -- O pedaco de texto. Guardamos o texto junto com o vetor porque, na hora de
    -- responder, e o TEXTO que vai para o modelo. O vetor serve so para achar.
    trecho      TEXT NOT NULL,

    -- A ordem do trecho dentro da fonte. Permite reconstruir o documento e dar
    -- contexto ("este trecho vem depois daquele").
    posicao     INTEGER NOT NULL,

    -- O VETOR. 1536 numeros, que e o tamanho do text-embedding-3-small.
    -- ⚠️ O numero e FIXO na tabela: trocar de modelo de embedding muda a
    -- dimensao e exige recriar a tabela e reprocessar tudo. Nao e uma troca
    -- barata -- escolha o modelo pensando nisso.
    embedding   vector(1536) NOT NULL,

    -- O `metadata` do Document: de onde veio, pagina, linha, o que voce quiser.
    -- JSONB (e nao JSON) aqui de proposito: este a gente CONSULTA por dentro
    -- (ex.: "so os trechos da pagina 12"), e o jsonb indexa e filtra rapido.
    -- Compare com a tabela `idempotencia`, onde usamos `json` justamente porque
    -- la a gente so guarda e devolve.
    metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,

    criada_em   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- INDICE DE VETOR (HNSW). Sem ele, buscar significa comparar a pergunta com
-- TODOS os trechos, um por um. Com 100 trechos ninguem nota; com 100 mil, a
-- busca fica lenta demais para uma conversa.
--
-- `vector_cosine_ops` diz ao indice que a comparacao e por COSSENO -- tem que
-- casar com o operador usado na consulta (`<=>`). Indice com uma metrica e
-- consulta com outra = o indice e ignorado em silencio, e voce nao percebe.
CREATE INDEX IF NOT EXISTS idx_documentos_embedding
    ON documentos USING hnsw (embedding vector_cosine_ops);

-- Indice comum, para as consultas administrativas ("o que ja foi carregado
-- desta fonte?") e para apagar/recarregar um documento.
CREATE INDEX IF NOT EXISTS idx_documentos_owner_fonte
    ON documentos (owner, fonte);
