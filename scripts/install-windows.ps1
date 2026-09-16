# Instala o Zapy como tarefa agendada do Windows (auto-start no boot, roda
# sem usuário logado, reinicia sozinha se cair) — mesmo padrão usado no
# agente-local do projeto-z-edu. Usa só o módulo ScheduledTasks nativo do
# Windows (Win10/Server 2016+), sem instalar nenhuma ferramenta de terceiro.
#
# Sem GPIO real no Windows: relés e sensores caem em modo mock (ver
# app.py/_MockRelay) — o painel e a integração com o ZAccess funcionam
# normalmente, só não aciona relé físico.
#
# Uso (PowerShell como Administrador):
#   .\scripts\install-windows.ps1

$ErrorActionPreference = "Stop"
$TaskName = "Zapy"

function Write-Step($msg) { Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Die($msg) { Write-Error $msg; exit 1 }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Die "roda este script num PowerShell aberto como Administrador (botão direito no ícone > Executar como administrador)."
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallDir = Split-Path -Parent $ScriptDir

Write-Step "checando Python"
$pyCmd = Get-Command py -ErrorAction SilentlyContinue
$PythonExe = $null
if ($pyCmd) {
    $PythonExe = "py"
    $pyArgs = "-3"
} else {
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCmd) { Die "Python não encontrado — instale o Python 3.10+ (https://python.org, marque 'Add to PATH') antes de rodar este script." }
    $PythonExe = $pythonCmd.Source
    $pyArgs = ""
}
$verOut = if ($pyArgs) { & $PythonExe $pyArgs -c "import sys; print(sys.version_info[0], sys.version_info[1])" } else { & $PythonExe -c "import sys; print(sys.version_info[0], sys.version_info[1])" }
$verParts = $verOut -split ' '
if ([int]$verParts[0] -lt 3 -or ([int]$verParts[0] -eq 3 -and [int]$verParts[1] -lt 10)) {
    Die "Python $($verParts -join '.') encontrado, mas o Zapy precisa de 3.10+."
}
Write-Host "Python $($verParts -join '.') encontrado ($PythonExe $pyArgs)"

Push-Location $InstallDir
try {
    Write-Step "criando .env (a partir de .env.example)"
    $EnvPath = Join-Path $InstallDir ".env"
    if (Test-Path $EnvPath) {
        Write-Host ".env já existe — não mexendo (apague o arquivo se quiser gerar de novo)."
    } else {
        Copy-Item (Join-Path $InstallDir ".env.example") $EnvPath
    }
    # Windows não tem GPIO real: garante pin factory mock (senão gpiozero
    # falha ao achar um backend e só se recupera pelo fallback interno do
    # app.py, gerando avisos no log à toa).
    if (-not (Select-String -Path $EnvPath -Pattern '^GPIOZERO_PIN_FACTORY=' -Quiet)) {
        Add-Content -Path $EnvPath -Value "`n# Sem GPIO real no Windows: usa pin factory mock`nGPIOZERO_PIN_FACTORY=mock"
    }

    Write-Step "criando ambiente virtual (.venv)"
    if (-not (Test-Path (Join-Path $InstallDir ".venv"))) {
        if ($pyArgs) { & $PythonExe $pyArgs -m venv .venv } else { & $PythonExe -m venv .venv }
        if ($LASTEXITCODE -ne 0) { Die "criação do venv falhou (código $LASTEXITCODE)." }
    } else {
        Write-Host ".venv já existe."
    }

    $VenvPython = Join-Path $InstallDir ".venv\Scripts\python.exe"
    $VenvPip = Join-Path $InstallDir ".venv\Scripts\pip.exe"

    Write-Step "instalando dependências"
    & $VenvPip install -q --upgrade pip
    & $VenvPip install -q -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Die "pip install falhou (código $LASTEXITCODE)." }
} finally {
    Pop-Location
}

$portLine = Get-Content $EnvPath | Where-Object { $_ -match '^PORT=' } | Select-Object -First 1
$Port = if ($portLine) { ($portLine -split '=')[1].Trim() } else { "" }
if (-not $Port) { $Port = "3080" }

Write-Step "liberando a porta $Port/TCP no Firewall do Windows"
$fwRuleName = "Zapy - painel de relés"
if (-not (Get-NetFirewallRule -DisplayName $fwRuleName -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName $fwRuleName -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
} else {
    Write-Host "regra de firewall já existia."
}

Write-Step "registrando a tarefa agendada ($TaskName)"
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    $overwrite = "s"
    if ([Environment]::UserInteractive) {
        $overwrite = Read-Host "tarefa '$TaskName' já existe — sobrescrever? [s/N]"
    }
    if ($overwrite -match '^[sSyY]') {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    } else {
        Write-Step "mantendo tarefa existente — só reiniciando"
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "`npronto — confira em Agendador de Tarefas (taskschd.msc)."
        exit 0
    }
}

$action = New-ScheduledTaskAction -Execute $VenvPython -Argument "app.py" -WorkingDirectory $InstallDir
$trigger = New-ScheduledTaskTrigger -AtStartup
# SYSTEM + ServiceAccount: roda sem precisar de ninguém logado na máquina —
# igual um serviço de verdade, sem depender de sessão de usuário.
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
# ExecutionTimeLimit zero é essencial: o padrão do Agendador mata a tarefa
# depois de 72h rodando — inaceitável pra um processo que precisa ficar de
# pé indefinidamente.

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description "Zapy - painel de relés e cliente ZAccess" | Out-Null
Start-ScheduledTask -TaskName $TaskName

$detectedIp = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
    Select-Object -First 1).IPAddress

Write-Host "`npronto — confira em Agendador de Tarefas (taskschd.msc) ou 'Get-ScheduledTask $TaskName'." -ForegroundColor Green
Write-Host "painel: http://${detectedIp}:${Port}"
