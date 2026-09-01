# Build the one-file workflow-mapper.exe (Windows).
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
# Output: dist\workflow-mapper.exe

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)   # repo root

Write-Host "Regenerating icons..."
python packaging\make_icons.py

Write-Host "Building exe with PyInstaller..."
python -m PyInstaller --noconfirm --clean --onefile --noconsole `
  --name workflow-mapper `
  --icon packaging\icon.ico `
  --add-data "ui/prototype.html;ui" `
  --add-data "ui/admin.html;ui" `
  --hidden-import sensors_win --hidden-import sensors_null --hidden-import screen --hidden-import auth `
  tray.py

Write-Host ""
Write-Host "Done. Artifact: dist\workflow-mapper.exe"
Write-Host "The browser extension (browser-extension\) is distributed separately."
