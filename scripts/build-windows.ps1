$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$frontendDist = Join-Path $root 'frontend\dist'
if (-not (Test-Path (Join-Path $frontendDist 'index.html'))) {
    throw '找不到 frontend/dist/index.html。请先运行 npm --prefix frontend ci 和 npm --prefix frontend run build。'
}

Push-Location (Join-Path $root 'backend')
try {
    if (-not (Test-Path '.venv-build\Scripts\python.exe')) {
        py -3.12 -m venv .venv-build
    }
    & '.venv-build\Scripts\python.exe' -m pip install --upgrade pip
    & '.venv-build\Scripts\python.exe' -m pip install -r requirements-dev.txt
    & '.venv-build\Scripts\python.exe' -m PyInstaller --clean --noconfirm --onedir --name VacuumInterlock --collect-all uvicorn --collect-all fastapi --collect-all cryptography --add-data '..\frontend\dist;frontend\dist' run.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败，退出码 $LASTEXITCODE" }
    Write-Host "打包完成：$(Join-Path (Get-Location) 'dist\VacuumInterlock')"
} finally {
    Pop-Location
}
