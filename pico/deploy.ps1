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
# went stale as transport modules were added. Deploying without any imported
# module leaves the bridge unable to boot after an OTA update.
# ci/test_pico_deploy_manifest.py fails if this ever drifts again.
$excluded = @('secrets.py', 'secrets_example.py', 'config_example.py')
$modules = Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.py' |
    Where-Object { $excluded -notcontains $_.Name } |
    Select-Object -ExpandProperty Name
# main.py last: it is the entry point, so a run that dies partway leaves the
# device with the old main.py rather than a new one calling absent modules.
$files = @($modules | Where-Object { $_ -ne 'main.py' } | Sort-Object)
if ($modules -contains 'main.py') { $files += 'main.py' }

# The web UI is a build artifact, not a module: web_ui.py streams it off
# littlefs. Upload it before any module so a failure here leaves the device
# untouched rather than half-updated.
$assets = @('www/index.html.gz', 'www/schema.json')
# mkdir fails when the directory already exists, which is the normal case.
try { & $python -m mpremote connect $Port fs mkdir ':www' 2>$null } catch { }
$global:LASTEXITCODE = 0
foreach ($asset in $assets) {
    $source = Join-Path $PSScriptRoot $asset
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Missing web UI asset: $source. Run tools/build_web_ui.py first."
    }
    & $python -m mpremote connect $Port fs cp $source (':' + $asset)
    if ($LASTEXITCODE -ne 0) { throw "Failed to upload $asset to $Port" }
}

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
