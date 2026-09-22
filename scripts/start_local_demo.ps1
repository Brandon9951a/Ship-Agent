$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$url = "http://127.0.0.1:8765"

function Stop-WithMessage([string]$Message) {
    Write-Host "[FAILED] $Message" -ForegroundColor Red
    exit 1
}

Set-Location -LiteralPath $projectRoot
Write-Host "Green Shipping Intelligence - Local Demo Launcher" -ForegroundColor Cyan

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Stop-WithMessage "Project virtual environment was not found: .venv\Scripts\python.exe"
}

$portProbe = New-Object System.Net.Sockets.TcpClient
try {
    $portProbe.Connect("127.0.0.1", 8765)
    $portOccupied = $true
} catch {
    $portOccupied = $false
} finally {
    $portProbe.Dispose()
}
if ($portOccupied) {
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

Write-Host "[3/4] Starting the local service in this window..."
Write-Host "[4/4] Keep this window open. Use Ctrl+C to stop the service." -ForegroundColor Green
Write-Host "If the browser does not open automatically, open $url manually." -ForegroundColor Yellow
& $python -m ui.web_server --host 127.0.0.1 --port 8765 --llm --open-browser
if ($LASTEXITCODE -ne 0) {
    Stop-WithMessage "The local service exited with an error."
}
Write-Host "Local demo service stopped."
