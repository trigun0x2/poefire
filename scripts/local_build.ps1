# Local Windows build for testing on this PC; nothing it writes is tracked by git (dist/ is ignored).
#
#   scripts/local_build.ps1            build into dist\staging\poefire, then swap it into dist\poefire
#   scripts/local_build.ps1 -SwapOnly  only swap a finished staging build in (after closing the app)
#
# A running poefire.exe locks its files, so the build never writes to dist\poefire directly: it
# builds into staging, and the swap happens only when no poefire process runs from dist\poefire.
# Exit codes: 0 done, 2 built but the app is running (close it, then run with -SwapOnly), 1 error.
param([switch]$SwapOnly)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

$pyinstaller = "pyinstaller==6.22.3"  # same as .github/workflows/release.yml
$staging = Join-Path $root "dist\staging\poefire"
$target = Join-Path $root "dist\poefire"
$old = Join-Path $root "dist\poefire-old"

function Invoke-Native([string]$what, [scriptblock]$run) {
  & $run
  if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" }
}

if (-not $SwapOnly) {
  Invoke-Native "uv sync" { uv sync --locked --extra window }
  Invoke-Native "PyInstaller" {
    uv run --with $pyinstaller pyinstaller packaging/windows/poefire.spec --noconfirm --clean `
      --distpath dist/staging --workpath dist/.pyinstaller-work
  }
  Invoke-Native "Smoke test (poefire-cli --help)" { & (Join-Path $staging "poefire-cli.exe") --help > $null }
}

if (-not (Test-Path (Join-Path $staging "poefire.exe"))) {
  throw "No staging build at $staging. Run without -SwapOnly first."
}

$running = Get-Process -Name poefire, poefire-cli -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -and $_.Path.StartsWith("$target\", [StringComparison]::OrdinalIgnoreCase) }
if ($running) {
  $ids = ($running | ForEach-Object { $_.Id }) -join ", "
  Write-Host "poefire is running from $target (PID $ids). The new build is in $staging."
  Write-Host "Close poefire, then run: scripts/local_build.ps1 -SwapOnly"
  exit 2
}

if (Test-Path $old) { Remove-Item -Recurse -Force $old }
if (Test-Path $target) { Rename-Item $target $old }
Move-Item $staging $target
if (Test-Path $old) { Remove-Item -Recurse -Force $old }
Write-Host "Built: $target\poefire.exe"
