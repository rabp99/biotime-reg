<#
.SYNOPSIS
    Instala la ingesta y el API como servicios de Windows usando NSSM.

.DESCRIPTION
    Crea dos servicios independientes a propósito:

      biotime-reg-ingest : puerto 83,   abierto a internet, habla con los 4 terminales
      biotime-reg-api    : puerto 8083, restringido,        sirve a Horario360

    Separarlos permite reiniciar o revertir uno sin tocar el otro, que es lo que
    hace posible la migración en dos fases descrita en el README.

    NSSM: https://nssm.cc/download  (dejar nssm.exe en el PATH)

.EXAMPLE
    .\install-services.ps1
    .\install-services.ps1 -Only api
#>

[CmdletBinding()]
param(
    [ValidateSet('both', 'ingest', 'api')]
    [string]$Only = 'both',

    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),

    # Cuenta bajo la que corren los servicios. LocalSystem funciona, pero una
    # cuenta dedicada sin privilegios es preferible si la política lo permite.
    [string]$ServiceAccount = ''
)

$ErrorActionPreference = 'Stop'

function Assert-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]$identity
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Ejecutar esta consola como Administrador.'
    }
}

function Install-One {
    param([string]$Name, [string]$Mode, [string]$Python, [string]$Root, [string]$LogDir)

    if (Get-Service -Name $Name -ErrorAction SilentlyContinue) {
        Write-Host "  $Name ya existe: deteniendo y reconfigurando" -ForegroundColor Yellow
        & nssm stop $Name confirm | Out-Null
        & nssm remove $Name confirm | Out-Null
        Start-Sleep -Seconds 1
    }

    & nssm install $Name $Python '-m' 'app.main' $Mode
    & nssm set $Name AppDirectory      $Root
    & nssm set $Name DisplayName       "biotime-reg ($Mode)"
    & nssm set $Name Description       "Marcaciones ZKTeco - $Mode"
    & nssm set $Name Start             SERVICE_AUTO_START

    # Reinicio automático ante caída. Sin esto, un fallo transitorio deja de
    # recibir marcaciones sin que nadie se entere: exactamente lo que le pasó a
    # BioTime, que estuvo cinco días sin ingerir.
    & nssm set $Name AppExit Default   Restart
    & nssm set $Name AppRestartDelay   5000

    & nssm set $Name AppStdout         (Join-Path $LogDir "$Name.log")
    & nssm set $Name AppStderr         (Join-Path $LogDir "$Name.err.log")
    & nssm set $Name AppRotateFiles    1
    & nssm set $Name AppRotateBytes    10485760   # 10 MB

    if ($ServiceAccount) {
        & nssm set $Name ObjectName $ServiceAccount
    }

    Write-Host "  $Name instalado" -ForegroundColor Green
}

Assert-Admin

if (-not (Get-Command nssm -ErrorAction SilentlyContinue)) {
    throw 'nssm.exe no está en el PATH. Descargar de https://nssm.cc/download'
}

$python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw "No se encontró el entorno virtual en $python. Crear con: python -m venv .venv"
}

if (-not (Test-Path (Join-Path $ProjectRoot '.env'))) {
    throw "Falta $ProjectRoot\.env. Copiar de .env.example y completar PG_DSN y JWT_SECRET."
}

$logDir = Join-Path $ProjectRoot 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

Write-Host "Instalando servicios desde $ProjectRoot" -ForegroundColor Cyan

if ($Only -in @('both', 'api')) {
    Install-One -Name 'biotime-reg-api' -Mode 'api' -Python $python -Root $ProjectRoot -LogDir $logDir
}
if ($Only -in @('both', 'ingest')) {
    Install-One -Name 'biotime-reg-ingest' -Mode 'ingest' -Python $python -Root $ProjectRoot -LogDir $logDir
}

Write-Host @"

Instalado. Siguientes pasos:

  Arrancar solo el API (fase 1 de la migracion):
      Start-Service biotime-reg-api
      curl http://127.0.0.1:8083/health

  Arrancar la ingesta (fase 2, tras detener BioTime y liberar el puerto 83):
      netstat -ano | findstr :83
      Start-Service biotime-reg-ingest

  Estado y logs:
      Get-Service biotime-reg-*
      Get-Content $logDir\biotime-reg-ingest.log -Tail 50 -Wait

  Desinstalar:
      nssm stop biotime-reg-ingest confirm; nssm remove biotime-reg-ingest confirm
      nssm stop biotime-reg-api confirm;    nssm remove biotime-reg-api confirm
"@ -ForegroundColor Cyan
