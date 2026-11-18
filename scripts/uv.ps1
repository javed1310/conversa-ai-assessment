[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$UvArgs)

# Compatibility entry point. The Python runner also works where scripts are restricted.
$ErrorActionPreference = 'Stop'
& python (Join-Path $PSScriptRoot 'uv.py') @UvArgs
exit $LASTEXITCODE
