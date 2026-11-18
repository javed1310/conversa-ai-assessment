[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$UvArgs)

$ErrorActionPreference = 'Stop'
$assessmentRoot = Split-Path -Parent $PSScriptRoot
$uvPath = Join-Path $assessmentRoot '.tools/uv/bin/uv.exe'
if (-not (Test-Path -LiteralPath $uvPath)) {
    throw 'Workspace uv is missing. Follow the bootstrap instructions in README.md.'
}
$names = @('UV_CACHE_DIR', 'UV_PYTHON_INSTALL_DIR', 'UV_PYTHON_BIN_DIR', 'UV_PROJECT_ENVIRONMENT', 'UV_PYTHON', 'UV_LINK_MODE')
$previous = @{}
foreach ($name in $names) { $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
try {
    $env:UV_CACHE_DIR = Join-Path $assessmentRoot '.cache/uv'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $assessmentRoot '.tools/python'
    $env:UV_PYTHON_BIN_DIR = Join-Path $assessmentRoot '.tools/python-bin'
    $env:UV_LINK_MODE = 'copy'
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $assessmentRoot 'backend/.venv-system'
    # Use the installed/qualified interpreter, not an unapproved downloaded runtime.
    $env:UV_PYTHON = (Get-Command python -ErrorAction Stop).Source
    Push-Location (Join-Path $assessmentRoot 'backend')
    try {
        & $uvPath @UvArgs
        $uvExitCode = $LASTEXITCODE
    }
    finally { Pop-Location }
}
finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process') }
}
exit $uvExitCode
