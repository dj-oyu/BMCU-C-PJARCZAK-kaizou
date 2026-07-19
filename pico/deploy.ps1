[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^COM[0-9]+$')]
    [string]$Port,
    [string]$SecretsPath,
    [switch]$NoReset
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw "mpremote Python environment not found: $python"
}

$files = @('bmcu_link.py', 'wifi.py', 'web_ui.py', 'main.py')
foreach ($name in $files) {
    $source = Join-Path $PSScriptRoot $name
    & $python -m mpremote connect $Port fs cp $source (':' + $name)
    if ($LASTEXITCODE -ne 0) { throw "Failed to upload $name to $Port" }
}

if ($SecretsPath) {
    if (-not (Test-Path -LiteralPath $SecretsPath -PathType Leaf)) {
        throw "Secrets file not found: $SecretsPath"
    }
    & $python -m mpremote connect $Port fs cp $SecretsPath ':secrets.py'
    if ($LASTEXITCODE -ne 0) { throw "Failed to upload secrets.py to $Port" }
}

if (-not $NoReset) {
    & $python -m mpremote connect $Port soft-reset
    if ($LASTEXITCODE -ne 0) { throw "Failed to soft-reset $Port" }
}

Write-Host "Pico deployment complete on $Port."
