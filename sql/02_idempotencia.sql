-- =============================================================================
-- 02_idempotencia.sql — a tabela de "crachas" ja vistos
--   psql -U agente_rh_app -h localhost -d agente_rh -f sql/02_idempotencia.sql
-- -----------------------------------------------------------------------------
-- PARA QUE SERVE. Guardar quais pedidos ja foram processados, e qual foi a
-- resposta de cada um. Quando o mesmo cracha chega de novo (porque o n8n
-- re-tentou), devolvemos a resposta guardada em vez de processar outra vez.
--
-- POR QUE NO BANCO E NAO NA MEMORIA? Porque o servidor pode reiniciar. Se a
-- lista de crachas vivesse na memoria do processo, um `Ctrl+C` apagaria tudo e
-- a proxima re-tentativa seria processada de novo -- exatamente o problema que
-- estamos tentando resolver. Alem disso, no dia em que houver duas copias do
-- servidor, as duas precisam enxergar a MESMA lista.
-- =============================================================================

CREATE TABLE IF NOT EXISTS idempotencia (
    -- O cracha, escolhido por quem chama (o n8n). E a CHAVE PRIMARIA: o banco
    -- garante que nao existem dois iguais. E essa garantia -- feita pelo banco,
    -- nao pelo nosso codigo -- que faz tudo funcionar mesmo se duas
    -- re-tentativas chegarem no MESMO instante.
    chave       TEXT PRIMARY KEY,

    -- De quem era o pedido. Serve para auditoria e, principalmente, para
    -- impedir que um usuario leia a resposta guardada de OUTRO usuario caso
    -- adivinhe (ou reuse) o cracha dele.
    owner       TEXT NOT NULL,

    -- 'processando' = alguem pegou este cracha e esta trabalhando nele agora.
    -- 'concluida'   = terminou; a resposta esta guardada na coluna abaixo.
    estado      TEXT NOT NULL CHECK (estado IN ('processando', 'concluida')),

    -- A resposta que foi devolvida. Fica NULL enquanto o estado e 'processando'.
    --
    -- POR QUE `json` E NAO `jsonb`? Os dois guardam JSON, mas o `jsonb`
    -- NORMALIZA: ele desmonta o JSON e o remonta na ordem que preferir. Ou seja,
    -- o que sai NAO e byte a byte o que entrou -- as chaves trocam de lugar.
    -- Confira voce mesmo:
    --   SELECT '{"a":1,"bb":2}'::jsonb::text;   -- pode voltar reordenado
    --   SELECT '{"a":1,"bb":2}'::json::text;    -- volta igualzinho
    -- Como aqui a gente so GUARDA e DEVOLVE (nunca consulta dentro do JSON),
    -- queremos a resposta repetida identica a original. Entao: `json`.
    -- Se um dia voce precisar filtrar por dentro do JSON, ai `jsonb` compensa.
    resposta    JSON,

    criada_em   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indice por data: usado para a limpeza (apagar crachas velhos). Sem ele, a
-- faxina teria que varrer a tabela inteira toda vez.
CREATE INDEX IF NOT EXISTS idx_idempotencia_data
    ON idempotencia (criada_em);
