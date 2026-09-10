# =============================================================================
# config.py — o unico lugar que le o .env
# -----------------------------------------------------------------------------
# POR QUE ISTO EXISTE?
# O jeito ingenuo de ler um segredo e `os.environ["DATABASE_URL"]`. O problema:
# se a variavel nao estiver no .env, o Python explode com um `KeyError` seco,
# que nao diz o que fazer. Pior: se o arquivo pedir a variavel LOGO NO TOPO,
# ele quebra mesmo em passos que nem usam banco de dados.
#
# A solucao aqui tem duas partes:
#   1. `exigir(...)` e uma FUNCAO, nao uma constante. Ela so e chamada no
#      momento em que a variavel e realmente necessaria. Passo 01 nao precisa
#      de banco -> Passo 01 nunca chama exigir("DATABASE_URL").
#   2. Quando falta algo, a mensagem diz O QUE falta e ONDE arrumar.
# =============================================================================

import os
from dotenv import load_dotenv

# Le o arquivo .env e joga as variaveis para dentro do os.environ.
# Chamado UMA vez aqui; todos os passos importam deste arquivo.
load_dotenv()


# O modelo padrao do guia. `os.environ.get(nome, padrao)` devolve o padrao se a
# variavel nao existir -- ou seja, isto nunca quebra, mesmo com .env vazio.
MODELO = os.environ.get("MODELO", "openai:gpt-4o-mini")


def exigir(nome, dica=""):
    """Le uma variavel OBRIGATORIA do .env. Se faltar, para com mensagem clara.

    `SystemExit` encerra o programa exibindo so a mensagem, sem aquele bloco
    gigante de 'Traceback' que assusta e nao ajuda em nada aqui.
    """
    valor = os.environ.get(nome, "").strip()
    if not valor or valor.startswith("coloque-sua"):
        raise SystemExit(
            f"\n[.env] Falta preencher a variavel: {nome}\n"
            f"       {dica}\n"
            f"       Abra o arquivo .env na raiz do projeto e preencha.\n"
        )
    return valor
