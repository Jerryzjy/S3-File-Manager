$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$releaseVersion = python -c 'from r2_batch_download import __version__; print(__version__)'
if ($LASTEXITCODE -ne 0 -or -not $releaseVersion) { throw 'Failed to read the desktop version.' }
$releasePath = Join-Path $PSScriptRoot "dist/release/$releaseVersion"
python -m PyInstaller --noconfirm --distpath $releasePath S3FileManager.spec
if ($LASTEXITCODE -ne 0) { throw 'Failed to build the desktop executable.' }
python tools/package_release.py $releasePath
if ($LASTEXITCODE -ne 0) { throw 'Failed to package release archive.' }
Write-Host "Built: $(Join-Path $releasePath 'S3FileManager.exe')"
