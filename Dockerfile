# =============================================================================
# Dockerfile — a imagem que o Easypanel vai construir
# -----------------------------------------------------------------------------
# DUAS ETAPAS (multi-stage), e a razao e concreta: a primeira instala as
# dependencias, a segunda so copia o resultado. Assim as ferramentas de
# compilacao nao viajam para a imagem final -- ela fica menor e com menos
# software instalado, que e menos coisa para ter vulnerabilidade.
# =============================================================================

FROM python:3.12-slim AS dependencias

# `uv` instala as dependencias muito mais rapido que o pip, e le o pyproject
# direto. Vem da imagem oficial: nada e baixado de script solto na internet.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml ./
# `--no-install-project`: instala SO as dependencias. O codigo entra depois, na
# outra camada -- assim mexer no codigo nao invalida o cache das dependencias, e
# o deploy seguinte leva segundos em vez de minutos.
RUN uv venv /opt/venv \
 && VIRTUAL_ENV=/opt/venv uv pip install -r pyproject.toml


FROM python:3.12-slim

# Roda como usuario comum, e nao como root. Se alguem escapar do processo, sai
# num usuario que nao pode escrever no sistema de arquivos da imagem.
RUN useradd --create-home --uid 10001 vero
COPY --from=dependencias /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY --chown=vero:vero . .
USER vero

EXPOSE 8000

# `entrada.py` prepara o banco e sai; so depois o servidor sobe. O `&&` garante
# que um erro na preparacao NAO deixa o servidor subir servindo tela vazia --
# melhor o container reiniciar com log de erro do que atender com base vazia.
CMD ["sh", "-c", "python entrada.py && exec uvicorn rh_web:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]
