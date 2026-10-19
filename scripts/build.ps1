param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    if ($Clean) {
        Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
    }
    python -m pip install --upgrade build
    python -m build
    Write-Host "Artifacts written to $root\dist"
} finally {
    Pop-Location
}
