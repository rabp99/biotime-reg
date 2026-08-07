<#
.SYNOPSIS
    Configura el Firewall de Windows para los dos puertos del servicio.

.DESCRIPTION
    Los dos puertos tienen exposiciones opuestas y se tratan distinto:

      83   Ingesta ADMS. Abierto a internet por necesidad: los terminales están
           detrás de NAT y solo ellos pueden iniciar la conexión. Se restringe a
           las IPs públicas de las dos sedes.

      8083 API de consulta. NO debe ser alcanzable desde internet. Se restringe
           a la IP del servidor de Horario360.

    La restricción por IP también está en la aplicación (API_ALLOWED_IPS), de
    modo que perder estas reglas no deja el endpoint de credenciales abierto.

.PARAMETER Horario360Ip
    IP del servidor de Horario360, desde la que consulta el API.

.PARAMETER SiteIps
    IPs públicas de las sedes donde están los terminales.

.EXAMPLE
    .\configure-firewall.ps1

.EXAMPLE
    .\configure-firewall.ps1 -WhatIf
#>

[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidatePattern('^\d{1,3}(\.\d{1,3}){3}$')]
    [string]$Horario360Ip = '161.132.68.120',

    # Sedes conocidas, obtenidas de iclock_terminal.real_ip:
    #   190.119.162.162 → BIO_05, BIO_06, BIO_07
    #   38.56.214.116   → BIO_08
    [string[]]$SiteIps = @('190.119.162.162', '38.56.214.116'),

    [int]$IngestPort = 83,
    [int]$ApiPort = 8083
)

$ErrorActionPreference = 'Stop'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Ejecutar esta consola como Administrador.'
}

function Set-Rule {
    param([string]$Name, [int]$Port, [string[]]$RemoteAddresses, [string]$Description)

    if (Get-NetFirewallRule -DisplayName $Name -ErrorAction SilentlyContinue) {
        Write-Host "  Reemplazando regla existente: $Name" -ForegroundColor Yellow
        Remove-NetFirewallRule -DisplayName $Name
    }

    if ($PSCmdlet.ShouldProcess($Name, "Crear regla para el puerto $Port")) {
        New-NetFirewallRule `
            -DisplayName $Name `
            -Description $Description `
            -Direction Inbound `
            -Action Allow `
            -Protocol TCP `
            -LocalPort $Port `
            -RemoteAddress $RemoteAddresses `
            -Profile Any | Out-Null

        Write-Host "  $Name -> puerto $Port desde: $($RemoteAddresses -join ', ')" -ForegroundColor Green
    }
}

Write-Host 'Configurando firewall' -ForegroundColor Cyan

Set-Rule -Name 'biotime-reg ingesta (terminales)' `
         -Port $IngestPort `
         -RemoteAddresses $SiteIps `
         -Description 'Marcaciones ZKTeco. Solo las sedes con terminales.'

Set-Rule -Name 'biotime-reg API (Horario360)' `
         -Port $ApiPort `
         -RemoteAddresses @($Horario360Ip) `
         -Description 'API de marcaciones. Solo el servidor de Horario360.'

Write-Host @"

Reglas aplicadas. Verificar:
    Get-NetFirewallRule -DisplayName 'biotime-reg*' |
        Format-Table DisplayName, Enabled, Action

    Get-NetFirewallRule -DisplayName 'biotime-reg*' |
        Get-NetFirewallAddressFilter | Format-Table RemoteAddress

Poner la misma IP en el .env para la segunda barrera:
    API_ALLOWED_IPS=$Horario360Ip

AVISO: si las sedes cambian de IP publica, los terminales dejan de entregar
datos en silencio. Comprobar con:
    SELECT sn, real_ip FROM iclock_terminal WHERE state = 1;
"@ -ForegroundColor Cyan
