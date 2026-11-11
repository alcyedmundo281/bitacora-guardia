FROM node:22-bookworm-slim

# Instalar Python, Git, Curl, Chromium y dependencias de sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-venv \
    git \
    curl \
    ca-certificates \
    chromium \
    && rm -rf /var/lib/apt/lists/*

# Instalar Quarto CLI para compilación de reportes y PDFs en la nube
RUN curl -sL https://github.com/quarto-dev/quarto-cli/releases/download/v1.11.9/quarto-1.11.9-linux-amd64.tar.gz | tar -xz -C /usr/local --strip-components=1

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

# Puerto público por defecto: 8080 (estándar de Railway)
ENV PORT=8080
EXPOSE 8080 3000

CMD ["/app/scripts/cloud_entrypoint.sh"]
