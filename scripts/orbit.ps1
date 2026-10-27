# Orbit CLI 启动器（PowerShell）。与 orbit.cmd 等价，见那边的注释。
#
# 注意：Windows 默认执行策略会拒绝未签名脚本。两种用法：
#   1) 当前会话放行：  Set-ExecutionPolicy -Scope Process Bypass
#   2) 直接调用解释器：powershell -ExecutionPolicy Bypass -File .\scripts\orbit.ps1 status
# 日常建议用 orbit.cmd（不受执行策略限制）。

$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $root "backend"
$env:PYTHONIOENCODING = "utf-8"
$python = Join-Path $root "backend\.venv\Scripts\python.exe"
& $python -m app.cli @args
exit $LASTEXITCODE
