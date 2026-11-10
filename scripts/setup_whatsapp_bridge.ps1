# Script para configurar y ejecutar el puente de WhatsApp en el disco local C:
# (Evita problemas de bloqueo de archivos en la unidad virtual de Google Drive)

$TargetDir = Join-Path $env:USERPROFILE "bitacora_whatsapp_bridge"
Write-Host "Configurando WhatsApp Bridge en: $TargetDir" -ForegroundColor Cyan

if (-not (Test-Path $TargetDir)) {
    New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
}

# Copiar archivos fuente del bridge
$SourceBridge = Join-Path $PSScriptRoot "..\bridge"
Copy-Item -Path (Join-Path $SourceBridge "package.json") -Destination $TargetDir -Force
Copy-Item -Path (Join-Path $SourceBridge "index.js") -Destination $TargetDir -Force

Set-Location $TargetDir
Write-Host "Instalando dependencias de WhatsApp en el disco local..." -ForegroundColor Yellow
npm install

Write-Host "Iniciando Bridge de WhatsApp..." -ForegroundColor Green
Write-Host "A continuacion vera el codigo QR para vincular el numero dedicado:" -ForegroundColor Green
node index.js

