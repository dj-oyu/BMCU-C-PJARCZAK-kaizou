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

# Derived from the directory rather than hardcoded: a hardcoded list silently
# went stale when bmcu_control.py, bambuddy_session.py, bambuddy_https.py and
# bambuddy_tls.py were added, and every one of them is imported at module level
# by main.py or bambuddy_ws.py. Deploying without them leaves the bridge unable
# to import itself, i.e. a Pico that no longer boots after an OTA update.
# ci/test_pico_deploy_manifest.py fails if this ever drifts again.
$excluded = @('secrets.py', 'secrets_example.py', 'config_example.py')
$modules = Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.py' |
    Where-Object { $excluded -notcontains $_.Name } |
    Select-Object -ExpandProperty Name
# main.py last: it is the entry point, so a run that dies partway leaves the
# device with the old main.py rather than a new one calling absent modules.
$files = @($modules | Where-Object { $_ -ne 'main.py' } | Sort-Object)
if ($modules -contains 'main.py') { $files += 'main.py' }
Write-Host ("Uploading {0} modules: {1}" -f $files.Count, ($files -join ', '))
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
    & $python -m mpremote connect $Port reset
    if ($LASTEXITCODE -ne 0) { throw "Failed to reset $Port" }
}

Write-Host "Pico deployment complete on $Port."
