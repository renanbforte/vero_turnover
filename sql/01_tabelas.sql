-- =============================================================================
-- 01_tabelas.sql — as SUAS tabelas de historico (texto limpo, legivel)
--   psql -U agente_rh_app -h localhost -d agente_rh -f sql/01_tabelas.sql
-- -----------------------------------------------------------------------------
-- POR QUE ESTAS TABELAS, SE O CHECKPOINTER JA GRAVA TUDO?
-- Porque o checkpointer grava no formato INTERNO dele: colunas `jsonb` com o
-- estado serializado do grafo. Aquilo e para a maquina reconstruir a conversa,
-- nao para voce ler. Tente um `SELECT * FROM checkpoints` e veja.
--
-- Estas tabelas sao SUAS: quem falou, o que falou, quando. Servem para auditar,
-- depurar, gerar relatorio, calcular custo e responder "o que esse usuario
-- perguntou ontem?" -- coisas que voce vai precisar em producao.
--
-- Rode conectado como `agente_app` (o dono do banco), NAO como postgres. Assim
-- as tabelas nascem pertencendo a aplicacao.
-- =============================================================================

CREATE TABLE IF NOT EXISTS conversas (
    id          BIGSERIAL PRIMARY KEY,

    -- O mesmo valor usado como thread_id na memoria do agente.
    -- UNIQUE: uma conversa por thread. E o que permite o "upsert" do historico.py.
    thread_id   TEXT NOT NULL UNIQUE,

    -- O DONO da linha. Hoje e igual ao thread_id; existe desde ja porque toda a
    -- RLS do Bloco 6 depende desta coluna. Adicionar coluna de dono depois, com
    -- a tabela cheia, da muito mais trabalho do que criar junto.
    -- NOT NULL de proposito: linha sem dono e linha que ninguem consegue ver
    -- depois que a RLS ligar -- e um bug silencioso.
    owner       TEXT NOT NULL,

    -- TIMESTAMPTZ (com fuso), nao TIMESTAMP. O TIMESTAMP puro guarda "14:30"
    -- sem dizer 14:30 ONDE -- e no dia em que o servidor rodar em UTC e voce
    -- ler em Sao Paulo, todos os horarios ficam 3 horas errados, sem aviso.
    criada_em   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mensagens (
    id          BIGSERIAL PRIMARY KEY,

    -- REFERENCES = chave estrangeira: toda mensagem pertence a uma conversa que
    -- EXISTE. O banco recusa mensagem orfa.
    -- ON DELETE CASCADE: apagar a conversa apaga as mensagens dela junto. Sem
    -- isso, apagar uma conversa da erro (ha filhos apontando para ela) -- e isso
    -- importa de verdade quando alguem pedir "apague meus dados" (LGPD).
    conversa_id BIGINT NOT NULL REFERENCES conversas(id) ON DELETE CASCADE,

    -- CHECK: o banco so aceita estes dois valores. E a ultima linha de defesa
    -- contra um bug no codigo gravar 'assistente', 'bot' ou vazio. Regra que
    -- pode ser garantida pelo banco deve ser garantida pelo banco.
    papel       TEXT NOT NULL CHECK (papel IN ('user', 'assistant')),

    conteudo    TEXT NOT NULL,
    owner       TEXT NOT NULL,
    criada_em   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- INDICE. Sem ele, "as ultimas mensagens desta conversa" faz o banco varrer a
-- tabela inteira. Com 100 linhas nao muda nada; com 1 milhao, muda tudo.
-- A ordem das colunas segue a da consulta: filtra por conversa, ordena por data.
CREATE INDEX IF NOT EXISTS idx_mensagens_conversa_data
    ON mensagens (conversa_id, criada_em DESC);

-- Indice por dono: "tudo que o usuario X falou", e a consulta que a RLS vai
-- fazer em toda linha no Bloco 6.
CREATE INDEX IF NOT EXISTS idx_mensagens_owner
    ON mensagens (owner);
