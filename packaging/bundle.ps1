# Produce a single sendable zip for a beta tester (Windows).
#   powershell -ExecutionPolicy Bypass -File packaging\bundle.ps1
# Output: dist\workflow-mapper-windows.zip  (exe + extension + instructions)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)   # repo root

if (-not (Test-Path "dist\workflow-mapper.exe")) {
    Write-Host "exe not found - building it first..."
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1
}

$stage = "dist\bundle"
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $stage | Out-Null

Copy-Item "dist\workflow-mapper.exe" $stage\
Copy-Item -Recurse "browser-extension" "$stage\browser-extension"
Copy-Item "packaging\INSTALL.txt" $stage\
Copy-Item "PRIVACY.md" "$stage\PRIVACY.txt"

$zip = "dist\workflow-mapper-windows.zip"
Remove-Item $zip -Force -ErrorAction SilentlyContinue
Compress-Archive -Path "$stage\*" -DestinationPath $zip

$size = "{0:N1} MB" -f ((Get-Item $zip).Length / 1MB)
Write-Host ""
Write-Host "Created $zip ($size)"
Write-Host "Send this one file to your beta tester."
