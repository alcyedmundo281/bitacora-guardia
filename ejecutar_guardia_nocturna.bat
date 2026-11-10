@echo off
title Agente de Guardia Medica Antigravity
echo ========================================================
echo   AGENTE ANTIGRAVITY - BITACORA DE GUARDIA MEDICA
echo ========================================================
echo.
echo Ejecutando Quality Gates y Generando Reportes...
call uv run python "%~dp0scripts\run_night_round_agent.py"
echo.
echo Presione cualquier tecla para abrir el reporte en el navegador...
pause >nul
start "" "%~dp0reports\bitacora_guardia.html"
exit
