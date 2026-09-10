# =============================================================================
# limites.py — quanto cada um pode usar
# -----------------------------------------------------------------------------
# O PROBLEMA. O Passo 08 fechou a porta: so entra quem tem a chave. Mas quem
# tem a chave pode entrar QUANTAS VEZES QUISER. Tres jeitos de isso te machucar,
# nenhum deles hipotetico:
#   1. Um fluxo do n8n mal configurado entra em laco e dispara mil requisicoes.
#   2. Um usuario nervoso manda 40 mensagens seguidas no WhatsApp.
#   3. A chave vaza e alguem resolve usar a sua cota da OpenAI.
# Em qualquer um deles voce so descobre pela fatura.
#
# A DEFESA: contar requisicoes por janela de tempo e recusar o excesso com
# HTTP 429. Contamos em DOIS niveis:
#   - por IDENTIDADE -> um usuario abusando nao atrapalha os outros;
#   - GLOBAL         -> um teto para o servico inteiro, que protege a fatura
#                       mesmo se as requisicoes vierem espalhadas.
#
# ⚠️ LIMITE HONESTO DESTA IMPLEMENTACAO. A contagem vive na MEMORIA do processo.
# Isso funciona perfeitamente com UM servidor. No dia em que voce rodar duas
# copias atras de um balanceador, cada uma tera a propria contagem, e o limite
# real vira o dobro. A solucao de verdade e guardar a contagem num lugar
# compartilhado (Redis). Enquanto for uma instancia so, isto aqui basta -- e e
# muito melhor que nao ter limite nenhum.
# =============================================================================

import os
import time
from collections import deque

JANELA_SEGUNDOS = 60


def _inteiro(nome, padrao):
    try:
        return int(os.environ.get(nome, padrao))
    except ValueError:
        return int(padrao)


class Limitador:
    """Janela deslizante: quantas requisicoes nos ultimos 60 segundos?

    POR QUE JANELA DESLIZANTE e nao "zera todo minuto cheio"? Porque o contador
    que zera as 10:01:00 permite 20 requisicoes as 10:00:59 e mais 20 as
    10:01:01 -- o dobro do limite em dois segundos. A janela deslizante olha
    sempre os ultimos 60 segundos a partir de AGORA, e nao tem essa brecha.
    """

    def __init__(self):
        # deque = fila com remocao rapida nas duas pontas. Guardamos so os
        # HORARIOS das requisicoes; nada de conteudo (memoria e privacidade).
        self._por_identidade = {}
        self._global = deque()

    def _registrar(self, fila, limite, agora):
        # Joga fora o que ja saiu da janela. Como a fila esta em ordem
        # cronologica, basta remover da frente ate achar algo recente.
        limite_inferior = agora - JANELA_SEGUNDOS
        while fila and fila[0] < limite_inferior:
            fila.popleft()

        if len(fila) >= limite:
            # Quanto falta para a requisicao mais antiga sair da janela: e o
            # tempo exato que o cliente precisa esperar.
            return max(1, int(fila[0] + JANELA_SEGUNDOS - agora))

        fila.append(agora)
        return None

    def checar(self, identidade):
        """Devolve None se pode passar, ou os SEGUNDOS para tentar de novo."""
        agora = time.monotonic()   # relogio que nunca anda para tras
        por_usuario = _inteiro("LIMITE_POR_IDENTIDADE", 20)
        global_ = _inteiro("LIMITE_GLOBAL", 120)

        # O global primeiro: se o servico como um todo estourou, nao adianta
        # gastar memoria registrando a tentativa na fila do usuario.
        espera = self._registrar(self._global, global_, agora)
        if espera:
            return espera

        fila = self._por_identidade.setdefault(identidade, deque())
        return self._registrar(fila, por_usuario, agora)

    def esquecer_inativos(self, maximo=10_000):
        """Evita que o dicionario cresca para sempre.

        Cada identidade nova cria uma entrada. Num servico publico, isso e um
        vazamento de memoria lento: alguem mandando requisicoes com identidades
        aleatorias enche a memoria do processo. Aqui limpamos as filas vazias
        quando o dicionario passa do teto.
        """
        if len(self._por_identidade) <= maximo:
            return
        vazias = [k for k, v in self._por_identidade.items() if not v]
        for k in vazias:
            del self._por_identidade[k]


limitador = Limitador()
