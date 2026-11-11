#!/bin/sh
set -e

echo "=========================================================="
echo "  INICIANDO SISTEMA CLINICO DE GUARDIA 24/7 EN LA NUBE"
echo "=========================================================="

# 1. Iniciar el Backend FastAPI de Bitácora en segundo plano
echo "Iniciando Agente FastAPI en puerto 8000..."
if [ -x "/app/.venv/bin/bitacora" ]; then
    /app/.venv/bin/bitacora start-server --host 127.0.0.1 --port 8000 &
else
    uv run bitacora start-server --host 127.0.0.1 --port 8000 &
fi

# Esperar 2 segundos a que el backend esté listo
sleep 2

# 2. Iniciar el WhatsApp Bridge en el puerto público ($PORT o 8080)
cd /app/bridge
echo "Iniciando WhatsApp Bridge en puerto ${PORT:-8080}..."
exec node index.js

