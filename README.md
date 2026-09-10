# vero_turnover

Painel de retenção e agente de diretoria, sobre uma base **fictícia** de 1.500
colaboradores. Duas telas, um banco, nenhuma imagem estática:

| Rota | O que é | Precisa de senha? |
|---|---|---|
| `/painel` | 9 gráficos interativos em SVG, com filtros e premissas de custo editáveis ao vivo | não |
| `/chat` | agente que responde perguntas de diretoria em linguagem natural | sim |

---

## O que o painel faz que um print não faz

Cada gráfico traz um bloco **"de onde veio este número"** com quatro camadas:

1. **A consulta SQL que reproduz aquele gráfico** — com o recorte próprio dele
   no `WHERE`, a agregação, e as premissas que estão na tela naquele instante.
   Não é ilustração: copie, cole no `psql` e o resultado é o mesmo da tela.
2. **O recorte próprio**, em português — a restrição que só aquele gráfico
   aplica. Dois gráficos podem sair da mesma tabela e falar de populações
   diferentes; essa é a camada que costuma faltar.
3. **Os passos do cálculo**, em ordem, nomeando a função que faz a conta.
4. **A leitura visual** — o que cada eixo, cor e tamanho representam.

As premissas de custo (salários/ano, encargos, fator de reposição por nível)
são **campos editáveis**. Mudar qualquer uma recalcula os nove gráficos a
partir do salário e do nível — nada é lido de coluna já somada no banco. A
consulta SQL exibida se reescreve junto.

## Como os números são calculados

As decisões que mudam o resultado estão todas em `rh_analise.py`:

- **Denominador em exposição (FTE-ano)**, não headcount médio. O quadro cresceu
  181% na janela; com denominador mal escolhido o crescimento sozinho "melhora"
  a taxa.
- **Headcount reconstruído mês a mês** das datas de admissão e desligamento —
  a base é um retrato, não uma série histórica.
- **Kaplan-Meier com censura à direita**, e não porcentagem simples: quem foi
  admitido há 4 meses ainda não teve chance de completar 12.
- **Coortes só de 2024 em diante**, por truncamento à esquerda.
- **Priorização por excesso sobre o baseline**, não por taxa (favorece o grupo
  pequeno) nem por volume (favorece o grande).
- **Regressão logística** só sobre ativos + quem pediu demissão. Quem foi
  desligado pela empresa fica fora: é outro fenômeno.

## Privacidade

O painel e o agente **nunca devolvem grupo com menos de 5 pessoas**. A trava
está no banco (as views `rh.vw_*` têm `HAVING count(*) >= 5`) e repetida no
código de leitura — não no prompt do modelo, que qualquer um contorna.

Os dados são **sintéticos**, criados para um processo seletivo. Nenhum registro
representa pessoa real, e nenhuma conclusão deve ser transposta para pessoas.

---

## Subir no Easypanel

### 1. Um serviço de PostgreSQL **com pgvector**

Crie um serviço de banco usando a imagem `pgvector/pgvector:pg16`. A extensão é
obrigatória: a memória semântica do agente guarda vetores. Um Postgres comum
falha ao aplicar `sql/04_rag.sql`.

Anote o nome interno do serviço (algo como `vero_postgres`), o banco, o usuário
e a senha.

### 2. Um serviço de App a partir deste repositório

- **Source:** este repositório, branch `main`
- **Build:** Dockerfile (o Easypanel detecta sozinho)
- **Port:** `8000`
- **Environment:** cole o conteúdo de [`.env.example`](.env.example) e preencha:

```
DATABASE_URL=postgresql://usuario:senha@vero_postgres:5432/vero
OPENAI_API_KEY=sk-...
RH_CHAT_SENHA=uma-senha-longa
RH_PERFIS_AUTORIZADOS=banca,diretoria
TETO_DIARIO_USD=5
```

Use o hostname **interno** do Postgres no `DATABASE_URL` — assim o tráfego não
sai da rede do projeto.

### 3. Deploy

Na primeira subida o `entrada.py` espera o banco responder, cria as tabelas e
carrega a planilha de `dados/`. Nas seguintes ele vê que a base já tem linhas e
não recarrega nada.

Depois, opcionalmente, abra o Console do serviço e rode uma vez:

```bash
python indexar_cartoes.py
```

Isso indexa 20 cartões de análise na memória semântica — o agente passa a
responder "o que isso significa", e não só "quanto é". O painel funciona sem
isso.

### 4. Domínio

Aponte o domínio para o serviço na porta 8000. O painel fica em `/painel` e o
chat em `/chat`.

---

## Rodar na sua máquina

```bash
uv sync
cp .env.example .env        # preencha DATABASE_URL e OPENAI_API_KEY
python entrada.py           # cria as tabelas e carrega a base
uvicorn rh_web:app --port 8000
```

Abra http://127.0.0.1:8000/painel.

---

## O que tem aqui

```
rh_analise.py        AS REGRAS DE CÁLCULO — exposição, faixas, Kaplan-Meier, custo
rh_carga.py          planilha → PostgreSQL, numa transação só
rh_consultas.py      o lado da leitura + a trava de k-anonimato
rh_exploracao.py     o motor do painel: recalcula tudo em pandas por recorte
rh_graficos.py       o catálogo — o que cada gráfico é (não desenha nada)
rh_sql_graficos.py   a consulta SQL que reproduz cada gráfico
rh_insights.py       os 20 cartões de análise que viram memória semântica
rh_agente.py         o agente de diretoria: prompt e contexto
rh_web.py            as rotas: /painel, /chat
tools/rh.py          as 9 ferramentas que o agente pode chamar
web/painel.html      os 9 gráficos, em SVG, sem biblioteca
sql/                 o schema, incluindo as views com k-anonimato
entrada.py           prepara o banco na subida do container
```

Os módulos sem prefixo (`nucleo.py`, `memoria.py`, `rag.py`, `limites.py`,
`custos.py`…) são a infraestrutura do agente: memória em Postgres, limites de
uso, teto de gasto, RAG. Vêm de um projeto maior de estudo, e aqui está apenas
o subconjunto que este servidor usa.

## Uso de IA generativa

O código e o texto foram escritos com apoio de IA generativa. As decisões
metodológicas — indicador, denominador, descarte de variáveis inconsistentes,
premissas de custo, priorização e limites do uso do modelo — foram tomadas e
revisadas por pessoa, e cada número é reproduzível pelos scripts.
