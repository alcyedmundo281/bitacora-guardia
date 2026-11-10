@echo off
title Conectar WhatsApp - Bitacora Medica
echo ========================================================
echo   CONEXION DE NUMERO DEDICADO A WHATSAPP
echo ========================================================
echo.
echo 1. Verificando Servidor Backend de Bitacora...
start /B uv run bitacora start-server --port 8000

echo.
echo 2. Iniciando Puente WhatsApp en http://localhost:3000...
echo Se abrira una ventana en su navegador con el Codigo QR.
echo.
powershell -ExecutionPolicy Bypass -File "%~dp0scripts\setup_whatsapp_bridge.ps1"
pause
