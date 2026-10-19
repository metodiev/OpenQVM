# Download a small Alpine Linux "virt" ISO for the end-to-end test.
# The ISO is not bundled with OpenQVM and must be verified by the user.
param(
    [string]$Destination = "$env:TEMP\openqvm-alpine-virt.iso"
)

$ErrorActionPreference = "Stop"
$url = "https://dl-cdn.alpinelinux.org/alpine/v3.24/releases/x86_64/alpine-virt-3.24.2-x86_64.iso"
Write-Host "Downloading $url"
curl.exe -L --fail --retry 2 -o $Destination $url
Write-Host "Saved to $Destination"
Write-Host "Run: `$env:OPENQVM_TEST_ISO='$Destination'; pytest tests/end_to_end -m e2e -q -s"
