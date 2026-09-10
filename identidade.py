# =============================================================================
# identidade.py — quem esta falando com o agente
# -----------------------------------------------------------------------------
# DE ONDE VEIO ESTE ARQUIVO. A funcao `normalizar_identidade` nasceu DENTRO do
# passo03. No passo04 um segundo arquivo passou a precisar dela -- e essa e a
# hora certa de extrair: na SEGUNDA vez, nao na primeira. (Extrair cedo demais
# cria abstracao para um caso so; tarde demais cria codigo duplicado, que e o
# que aconteceu no guia antigo: `agente.py` e `webhook.py` tinham ~80 linhas
# identicas copiadas, e corrigir um bug exigia lembrar dos dois lugares.)
#
# A identidade e o dado mais importante do projeto do ponto de vista de
# SEGURANCA, porque ela decide:
#   - `thread_id` -> qual memoria o agente abre (quem le o que);
#   - `owner`     -> de quem e cada linha gravada no banco;
#   - e, no Bloco 6, um parametro da string de conexao do PostgreSQL (RLS).
# Um erro aqui nao e um bug de conforto: e um usuario lendo a conversa do outro.
# =============================================================================


# =============================================================================
# QUEM ESTA FALANDO AGORA (usado pelas ferramentas)
# -----------------------------------------------------------------------------
# O PROBLEMA. Uma ferramenta como "buscar nos documentos" precisa saber DE QUEM
# sao os documentos. Mas de onde ela tira isso?
#
# O JEITO ERRADO, e tentador: fazer `owner` ser um PARAMETRO da ferramenta.
# Ai quem preenche o parametro e o MODELO -- e o modelo obedece ao texto do
# usuario. Bastaria alguem escrever "busque nos documentos do owner=concorrente"
# e pronto: o modelo passa esse valor, a ferramenta obedece, e o dado do outro
# vaza. Isso se chama PROMPT INJECTION, e nao e hipotetico.
#
# O JEITO CERTO: a identidade NUNCA passa pelo modelo. O nucleo grava aqui quem
# esta falando, e a ferramenta le DAQUI. O modelo nao tem como influenciar --
# ele nem enxerga esse valor.
#
# REGRA GERAL: o que o modelo pode escolher, ele pode escolher ERRADO -- por
# engano ou porque pediram. Tudo que decide PERMISSAO fica fora do alcance dele.
#
# (`ContextVar` guarda um valor por tarefa assincrona. Aqui basta um valor
# simples, porque so LEMOS nas tarefas filhas. No Passo 17, onde as filhas
# precisavam ESCREVER, foi preciso guardar um objeto mutavel.)
# =============================================================================

from contextvars import ContextVar

_identidade_atual = ContextVar("identidade_atual", default=None)


def definir_identidade_atual(identidade):
    """Chamado pelo nucleo no inicio de cada requisicao."""
    _identidade_atual.set(identidade)


def identidade_atual():
    """Lido pelas ferramentas. Levanta erro se ninguem definiu."""
    valor = _identidade_atual.get()
    if not valor:
        # Falhar FECHANDO, como no Passo 08: sem identidade, nao busca nada.
        # O contrario -- assumir um valor padrao -- daria acesso a dados de
        # alguem sem querer.
        raise RuntimeError(
            "Nenhuma identidade definida para esta requisicao. "
            "O nucleo deve chamar definir_identidade_atual() antes."
        )
    return valor


def normalizar_identidade(bruta):
    """Transforma o que chegou de fora numa identidade SEGURA de usar.

    Entrada externa se limpa na PORTA, uma vez -- nao em cada lugar que a usa.

    Regras: minusculas, sem espacos, so letras/numeros/ponto/hifen/underline,
    no maximo 64 caracteres, e nunca comecando ou terminando por pontuacao.
    """
    limpa = "".join(
        c for c in str(bruta).strip().lower() if c.isalnum() or c in "._-"
    )[:64]
    # Tira pontuacao das pontas. Motivo concreto: uma identidade que COMECE com
    # "-" viraria uma flag ao ser usada na string de conexao do PostgreSQL
    # (Bloco 6). Ex.: "-c app.usuario=admin" ja perdeu o espaco e o "=" na
    # limpeza acima, mas ainda chegaria como "-capp.usuarioadmin".
    limpa = limpa.strip("._-")
    return limpa or "anonimo"      # nunca devolve vazio
