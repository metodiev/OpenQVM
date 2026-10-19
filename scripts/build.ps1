param(
    [switch]$Clean,
    [switch]$Bundle
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
    Write-Host "Wheels written to $root\dist"

    if ($Bundle) {
        python -m pip install --upgrade pyinstaller
        python -m PyInstaller --noconfirm packaging/openqvm.spec
        $bundleDir = Join-Path $root "dist\OpenQVM"
        & (Join-Path $bundleDir "openqvm-cli.exe") --version
        if ($LASTEXITCODE -ne 0) {
            throw "The bundled CLI failed its version check."
        }
        # A GUI-subsystem executable is not awaited by `&`, so use Start-Process.
        $outFile = Join-Path $env:TEMP "openqvm-selftest.out"
        $errFile = Join-Path $env:TEMP "openqvm-selftest.err"
        $selfTest = Start-Process -FilePath (Join-Path $bundleDir "OpenQVM.exe") `
            -ArgumentList "--self-test" -Wait -PassThru `
            -RedirectStandardOutput $outFile -RedirectStandardError $errFile
        Get-Content $outFile -ErrorAction SilentlyContinue | Write-Host
        if ($selfTest.ExitCode -ne 0) {
            Get-Content $errFile -ErrorAction SilentlyContinue | Write-Host
            throw "The portable bundle failed its self-test (exit $($selfTest.ExitCode))."
        }
        $version = (python -c "import openqvm; print(openqvm.PRODUCT_VERSION)").Trim()
        $zip = Join-Path $root "dist\OpenQVM-$version-win64.zip"
        Compress-Archive -Path $bundleDir -DestinationPath $zip -Force
        Write-Host "Portable bundle: $bundleDir (archived at $zip)"
    }
} finally {
    Pop-Location
}
