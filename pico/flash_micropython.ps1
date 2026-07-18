[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z]$')]
    [string]$BootDrive = 'D',
    [string]$Uf2Path = 'C:\tmp\RPI_PICO2_W-20260406-v1.28.0.uf2'
)

$ErrorActionPreference = 'Stop'
$destination = "${BootDrive}:\\"
if (-not (Test-Path -LiteralPath $destination)) {
    throw "Pico BOOTSEL drive is not mounted at $destination"
}
if (-not (Test-Path -LiteralPath $Uf2Path -PathType Leaf)) {
    throw "MicroPython UF2 not found: $Uf2Path"
}
$volume = Get-Volume -DriveLetter $BootDrive
if ($volume.FileSystemLabel -ne 'RP2350') {
    throw "Refusing to flash $destination because its label is '$($volume.FileSystemLabel)', not RP2350"
}
Copy-Item -LiteralPath $Uf2Path -Destination $destination -Force
Write-Host 'UF2 copied.  The Pico will reset automatically; wait for its COM port to appear.'
