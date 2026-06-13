$ErrorActionPreference = "Stop"

$npm = Get-Command "npm.cmd" -ErrorAction SilentlyContinue
if (-not $npm) {
  $npm = Get-Command "npm" -ErrorAction Stop
}
$python = Get-Command "python" -ErrorAction Stop

& $npm.Source run build
$staticPath = (Resolve-Path "backend/static").Path
$iconPath = (Resolve-Path "logo/logo.ico").Path
& $python.Source -m PyInstaller `
  --noconfirm `
  --clean `
  --onedir `
  --name "YCY-Bililive-EMS" `
  --workpath ".build-cache" `
  --specpath ".build-cache" `
  --distpath "dist" `
  --windowed `
  --icon "$iconPath" `
  --add-data "$staticPath;backend/static" `
  --add-data "$iconPath;logo" `
  --hidden-import "bleak.backends.winrt" `
  --collect-all "bleak" `
  --collect-all "bilibili_api" `
  --collect-all "webview" `
  run.py

Copy-Item "README.md" "dist/YCY-Bililive-EMS/README.md" -Force
Copy-Item "CHANGELOG.md" "dist/YCY-Bililive-EMS/CHANGELOG.md" -Force

$releaseDir = Join-Path (Get-Location) "release"
New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null
$releaseZip = Join-Path $releaseDir "YCY-Bililive-EMS-Windows-x64.zip"
if (Test-Path -LiteralPath $releaseZip) {
  Remove-Item -LiteralPath $releaseZip -Force
}
Compress-Archive `
  -Path "dist/YCY-Bililive-EMS" `
  -DestinationPath $releaseZip `
  -CompressionLevel Optimal

Write-Host "Build complete:"
Write-Host "  App:     dist/YCY-Bililive-EMS/YCY-Bililive-EMS.exe"
Write-Host "  Release: release/YCY-Bililive-EMS-Windows-x64.zip"
