# Para e remove a tarefa agendada do Zapy — NÃO apaga .env nem .venv, pra
# dar pra reinstalar sem perder configuração. Roda install-windows.ps1 de
# novo depois se quiser recriar a tarefa.
#
# Uso (PowerShell como Administrador):
#   .\scripts\uninstall-windows.ps1 [-Purge]
#   -Purge  remove também o ambiente virtual (.venv)

param(
    [switch]$Purge
)

$ErrorActionPreference = "Stop"
$TaskName = "Zapy"

function Write-Step($msg) { Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Die($msg) { Write-Error $msg; exit 1 }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Die "roda este script num PowerShell aberto como Administrador."
}

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existingTask) {
    Write-Step "tarefa '$TaskName' não existe — nada instalado por aqui."
} else {
    Write-Step "parando e removendo a tarefa $TaskName"
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

if ($Purge) {
    $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $InstallDir = Split-Path -Parent $ScriptDir
    $VenvDir = Join-Path $InstallDir ".venv"
    if (Test-Path $VenvDir) {
        Write-Step "removendo .venv"
        Remove-Item -Recurse -Force $VenvDir
        Write-Host ".venv removido."
    }
}

Write-Host "`ndesinstalação concluída — .env continua no lugar. Roda install-windows.ps1 de novo pra reinstalar a tarefa." -ForegroundColor Green
