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
Write-Host "绿航智算 · 本地演示启动器" -ForegroundColor Cyan

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Stop-WithMessage "未找到项目虚拟环境：.venv\Scripts\python.exe"
}

$occupied = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if ($occupied) {
    Stop-WithMessage "端口 8765 已被占用。请关闭对应程序后重试；启动器不会自动结束其他进程。"
}

Write-Host "[1/4] 检查依赖..."
& $python -m pip check
if ($LASTEXITCODE -ne 0) {
    Stop-WithMessage "依赖检查失败，请先在项目虚拟环境中安装依赖。"
}
& $python -c "import fastapi, langgraph, uvicorn"
if ($LASTEXITCODE -ne 0) {
    Stop-WithMessage "缺少本地 Web 依赖，请先执行：.venv\Scripts\python.exe -m pip install -e .[dev]"
}

Write-Host "[2/4] 执行 DeepSeek 最小连通测试（只输出安全状态摘要）..."
& $python -m core.llm_layer --smoke-test
if ($LASTEXITCODE -ne 0) {
    Write-Host "[WARN] DeepSeek 连通测试未通过；服务仍会启动，并在调用失败时使用模板回退。" -ForegroundColor Yellow
}

Write-Host "[3/4] 启动本地服务..."
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
        Stop-WithMessage "服务未在预期时间内进入可用状态。"
    }

    Write-Host "[4/4] 服务已就绪：$url（模型模式：$($health.llm_mode)）" -ForegroundColor Green
    Start-Process $url
    Write-Host "浏览器关闭不会停止服务。返回此窗口并按 Enter 可安全停止本次服务。"
    Read-Host | Out-Null
} finally {
    if ($serverProcess -and -not $serverProcess.HasExited) {
        Stop-Process -Id $serverProcess.Id
        Write-Host "本地演示服务已停止。"
    }
}
