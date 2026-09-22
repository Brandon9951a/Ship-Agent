$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$url = "http://127.0.0.1:8765"
$serverProcess = $null

function Stop-WithMessage([string]$Message) {
    Write-Host "[FAILED] $Message" -ForegroundColor Red
    exit 1
}

Set-Location -LiteralPath $projectRoot
Write-Host "Green Shipping Intelligence - Local Demo Launcher" -ForegroundColor Cyan

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Stop-WithMessage "Project virtual environment was not found: .venv\Scripts\python.exe"
}

$occupied = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if ($occupied) {
    Stop-WithMessage "Port 8765 is already in use. Close the owning program and retry. No process was stopped."
}

Write-Host "[1/4] Checking dependencies..."
& $python -m pip check
if ($LASTEXITCODE -ne 0) {
    Stop-WithMessage "Dependency check failed. Install the project dependencies first."
}
& $python -c "import fastapi, langgraph, uvicorn"
if ($LASTEXITCODE -ne 0) {
    Stop-WithMessage "Web dependencies are missing. Run: .venv\Scripts\python.exe -m pip install -e .[dev]"
}

Write-Host "[2/4] Running the safe DeepSeek connectivity test..."
& $python -m core.llm_layer --smoke-test
if ($LASTEXITCODE -ne 0) {
    Write-Host "[WARN] DeepSeek test failed. The deterministic workflow will still start with template fallback." -ForegroundColor Yellow
}

Write-Host "[3/4] Starting the local service..."
try {
    $serverProcess = Start-Process -FilePath $python `
        -ArgumentList @("-m", "ui.web_server", "--host", "127.0.0.1", "--port", "8765", "--llm") `
        -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru

    $healthy = $false
    foreach ($attempt in 1..30) {
        if ($serverProcess.HasExited) { break }
        try {
            $health = Invoke-RestMethod -Uri "$url/healthz" -TimeoutSec 2
            if ($health.status -eq "ok") {
                $healthy = $true
                break
            }
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $healthy) {
        Stop-WithMessage "The service did not become healthy in time."
    }

    Write-Host "[4/4] Service ready: $url (LLM mode: $($health.llm_mode))" -ForegroundColor Green
    try {
        Start-Process $url
    } catch {
        Write-Host "[WARN] Browser could not be opened automatically. Open $url manually." -ForegroundColor Yellow
    }
    Write-Host "Closing the browser does not stop the service. Return here and press Enter to stop it."
    Read-Host | Out-Null
} finally {
    if ($serverProcess -and -not $serverProcess.HasExited) {
        Stop-Process -Id $serverProcess.Id
        Write-Host "Local demo service stopped."
    }
}
