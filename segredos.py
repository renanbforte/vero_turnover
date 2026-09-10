# =============================================================================
# segredos.py — transformar segredo de VARIAVEL em ARQUIVO temporario
# -----------------------------------------------------------------------------
# O PROBLEMA. Alguns programas de terceiros so aceitam credencial em ARQUIVO
# (o servidor MCP do Google Calendar e um deles: ele le um CAMINHO, nao um JSON).
# Mas o nosso projeto tem uma regra desde o Passo 04: configuracao e segredo vem
# de VARIAVEL DE AMBIENTE, nunca de arquivo no repositorio -- senao o deploy na
# AWS nao funciona, porque la os segredos vem do Secrets Manager e nao existe
# `.env` nem arquivo solto no disco.
#
# A PONTE. Guardamos o CONTEUDO do segredo numa variavel de ambiente e, na hora
# de subir, escrevemos um arquivo TEMPORARIO com ele. O programa de terceiros
# recebe o caminho desse arquivo. Ao encerrar, o arquivo e apagado.
#
#   .env / Secrets Manager  ->  variavel  ->  arquivo temporario  ->  o programa
#
# ⚠️ SEJA HONESTO SOBRE O QUE ISTO E. O segredo passa a existir em disco
# enquanto o processo roda. Nao da para evitar: o programa exige arquivo. O que
# a gente controla:
#   - fica na pasta temporaria do SEU usuario, nao no repositorio;
#   - e apagado quando o processo termina;
#   - nunca entra no Git;
#   - na AWS, vem do Secrets Manager e morre com o container.
# =============================================================================

import atexit
import json
import logging
import os
import tempfile

log = logging.getLogger("segredos")

# Guardamos os caminhos criados para apagar tudo no fim.
_criados = []


def _limpar():
    """Apaga os arquivos temporarios quando o processo termina."""
    for caminho in _criados:
        try:
            os.unlink(caminho)
        except OSError:
            pass


# `atexit` roda no encerramento normal do processo. Nao roda num `kill -9` --
# por isso os arquivos ficam na pasta temporaria do sistema, que e limpa
# periodicamente de qualquer forma.
atexit.register(_limpar)


def arquivo_de_segredo(nome, conteudo, obrigatorio=True):
    """Escreve `conteudo` num arquivo temporario e devolve o caminho.

    `nome` e so para identificar o arquivo e as mensagens de erro.
    Devolve None se o conteudo estiver vazio e `obrigatorio=False`.
    """
    conteudo = (conteudo or "").strip()
    if not conteudo:
        if obrigatorio:
            raise ValueError(f"O segredo {nome!r} esta vazio.")
        return None

    # Conferimos que e JSON valido AQUI, e nao depois. Um JSON quebrado gera um
    # erro obscuro la dentro do programa de terceiros ("Invalid credentials
    # file format"), e voce perde meia hora procurando no lugar errado.
    try:
        json.loads(conteudo)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"O segredo {nome!r} nao e um JSON valido: {e}. "
            "Confira se ele esta numa LINHA SO no .env, sem quebras."
        ) from e

    # `delete=False` porque quem vai LER o arquivo e outro processo -- se o
    # Python apagasse ao fechar, o programa nao acharia nada.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=f"-{nome}.json", delete=False, encoding="utf-8"
    ) as arquivo:
        arquivo.write(conteudo)
        caminho = arquivo.name

    # Permissao restrita: so o dono le e escreve. No Linux (e na AWS) isto vale
    # de verdade. No Windows tem efeito limitado -- mas a pasta temporaria ja e
    # por usuario, entao o resultado pratico e o mesmo.
    try:
        os.chmod(caminho, 0o600)
    except OSError:
        pass

    _criados.append(caminho)
    log.debug("segredo %r materializado em arquivo temporario", nome)
    return caminho


def de_variavel(nome_da_variavel, obrigatorio=True):
    """Le uma variavel de ambiente e devolve o CAMINHO de um arquivo com ela."""
    return arquivo_de_segredo(
        nome_da_variavel.lower().replace("_", "-"),
        os.environ.get(nome_da_variavel, ""),
        obrigatorio=obrigatorio,
    )
