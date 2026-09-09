$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'frontend/dist/index.html'))) {
    throw 'Frontend build missing. Run npm ci and npm run build in frontend first.'
}
Push-Location (Join-Path $projectRoot 'backend')
try {
    uv run uvicorn app.main:app --host 127.0.0.1 --port 6185
} finally {
    Pop-Location
}
