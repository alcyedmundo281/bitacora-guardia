FROM node:22-bookworm-slim

# Instalar Python, Git y dependencias de sistema (Git es requerido por Baileys para libsignal)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-venv \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Configurar git para clonar dependencias siempre por HTTPS
RUN git config --global url."https://github.com/".insteadOf "git@github.com:" && \
    git config --global url."https://github.com/".insteadOf "ssh://git@github.com/"

# Instalar uv directamente desde la imagen oficial de Astral en GitHub Container Registry
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# 1. Copiar código y dependencias de Python
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY data/ ./data/
COPY reports/ ./reports/
COPY scripts/ ./scripts/

# Configurar entorno virtual con uv
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"
RUN uv venv /app/.venv && uv pip install --no-cache -e .

# 2. Copiar y preparar el puente de WhatsApp en Node.js
WORKDIR /app/bridge
COPY bridge/package*.json ./
RUN npm install --omit=dev
COPY bridge/index.js ./

WORKDIR /app

# Script de arranque
RUN chmod +x /app/scripts/cloud_entrypoint.sh

# Puerto público por defecto (soporta 3000 y 8080)
ENV PORT=3000
EXPOSE 3000 8080

CMD ["/app/scripts/cloud_entrypoint.sh"]
