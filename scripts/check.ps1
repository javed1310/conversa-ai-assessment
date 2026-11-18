[CmdletBinding()]
param([switch]$SkipFrontendBuild)

$ErrorActionPreference = 'Stop'
$assessmentRoot = Split-Path -Parent $PSScriptRoot
$uvRunner = Join-Path $PSScriptRoot 'uv.ps1'
$frontend = Join-Path $assessmentRoot 'frontend'

function Invoke-BackendCheck {
    param([string[]]$ToolArgs)
    & powershell -NoProfile -File $uvRunner run python -m @ToolArgs
    if ($LASTEXITCODE -ne 0) { throw "Backend check failed: $($ToolArgs[0])" }
}

function Invoke-FrontendCheck {
    param([string]$Script)
    & npm.cmd --prefix $frontend run $Script
    if ($LASTEXITCODE -ne 0) { throw "Frontend check failed: $Script" }
}

Invoke-BackendCheck -ToolArgs @('ruff', 'check', 'app', 'tests')
Invoke-BackendCheck -ToolArgs @('ruff', 'format', '--check', 'app', 'tests')
Invoke-BackendCheck -ToolArgs @('mypy', 'app')
Invoke-BackendCheck -ToolArgs @('pytest', '-q')
Invoke-BackendCheck -ToolArgs @('app.export_contracts', '--check')
Invoke-FrontendCheck -Script 'lint'
Invoke-FrontendCheck -Script 'typecheck'
Invoke-FrontendCheck -Script 'test'
if (-not $SkipFrontendBuild) { Invoke-FrontendCheck -Script 'build' }
Write-Output 'PASS: all selected development checks and contract exports.'
