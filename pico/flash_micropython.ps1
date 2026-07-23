[CmdletBinding()]
param(
    [ValidateSet('PicoW', 'Pico2W')]
    [string]$Board = 'Pico2W',
    [ValidatePattern('^[A-Za-z]$')]
    [string]$BootDrive = 'D',
    [string]$Uf2Path
)

$ErrorActionPreference = 'Stop'
$boardProfiles = @{
    PicoW = @{
        BootLabel = 'RPI-RP2'
        DefaultUf2 = 'C:\tmp\RPI_PICO_W-20260406-v1.28.0.uf2'
    }
    Pico2W = @{
        BootLabel = 'RP2350'
        DefaultUf2 = 'C:\tmp\RPI_PICO2_W-20260406-v1.28.0.uf2'
    }
}
$profile = $boardProfiles[$Board]
if (-not $Uf2Path) {
    $Uf2Path = $profile.DefaultUf2
}

$destination = "${BootDrive}:\\"
if (-not (Test-Path -LiteralPath $destination)) {
    throw "Pico BOOTSEL drive is not mounted at $destination"
}
if (-not (Test-Path -LiteralPath $Uf2Path -PathType Leaf)) {
    throw "MicroPython UF2 not found: $Uf2Path"
}
$volume = Get-Volume -DriveLetter $BootDrive
$expectedLabel = $profile.BootLabel
if ($volume.FileSystemLabel -ne $expectedLabel) {
    throw "Refusing to flash $destination for $Board because its label is '$($volume.FileSystemLabel)', not $expectedLabel"
}
Copy-Item -LiteralPath $Uf2Path -Destination $destination -Force
Write-Host "$Board UF2 copied. The Pico will reset automatically; wait for its COM port to appear."
