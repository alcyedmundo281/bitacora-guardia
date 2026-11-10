FROM node:22-bookworm-slim

# Instalar Python, curl y dependencias de sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Instalar uv para gestión ultrarrápida de Python
ADD https://astral.sh/uv/install.sh /uv-installer.sh
RUN sh /uv-installer.sh && rm /uv-installer.sh
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

# 1. Copiar y preparar dependencias de Python
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY data/ ./data/
COPY reports/ ./reports/

RUN uv venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
RUN uv pip install -e .

# 2. Copiar y preparar el puente de WhatsApp en Node.js
WORKDIR /app/bridge
COPY bridge/package.json ./
RUN npm install --omit=dev
COPY bridge/index.js ./

WORKDIR /app

# Puerto público para la interfaz web de vinculación QR y Webhooks
ENV PORT=3000
EXPOSE 3000 8000

# Script de arranque unificado
COPY scripts/cloud_entrypoint.sh /app/cloud_entrypoint.sh
RUN chmod +x /app/cloud_entrypoint.sh

CMD ["/app/cloud_entrypoint.sh"]

