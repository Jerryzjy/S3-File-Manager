param(
    [string]$IsccPath,
    [switch]$BuildDesktop
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Resolve-Iscc {
    param([string]$RequestedPath)

    if ($RequestedPath) {
        $candidate = (Resolve-Path -LiteralPath $RequestedPath -ErrorAction Stop).Path
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            throw "The specified Inno Setup compiler was not found: $candidate"
        }
        return $candidate
    }

    $fromPath = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($fromPath) { return $fromPath.Source }

    $knownLocations = @(
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 7\ISCC.exe'),
        (Join-Path ${env:ProgramFiles} 'Inno Setup 7\ISCC.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path ${env:ProgramFiles} 'Inno Setup 6\ISCC.exe')
    )
    foreach ($candidate in $knownLocations) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return $candidate
        }
    }
    throw 'Inno Setup compiler not found. Install Inno Setup 6.5 or newer, or pass -IsccPath C:\Path\To\ISCC.exe.'
}

if ($BuildDesktop) {
    & (Join-Path $PSScriptRoot 'build-desktop.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'The desktop build failed.' }
}

$releaseVersion = (python -c 'from r2_batch_download import __version__; print(__version__)').Trim()
if ($LASTEXITCODE -ne 0 -or -not $releaseVersion) {
    throw 'Failed to read the desktop version.'
}

$releasePath = Join-Path $PSScriptRoot "dist\release\$releaseVersion"
$executable = Join-Path $releasePath 'S3FileManager.exe'
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "Release executable not found at $executable. Run .\build-desktop.ps1 first or pass -BuildDesktop."
}

$compiler = Resolve-Iscc -RequestedPath $IsccPath
$iss = Join-Path $PSScriptRoot 'installer\S3FileManager.iss'
$arguments = @(
    "/DAppVersion=$releaseVersion",
    '/Qp',
    $iss
)
& $compiler @arguments
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE." }

$installer = Join-Path $PSScriptRoot "dist\release\S3FileManager-$releaseVersion-windows-x64-setup.exe"
if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
    throw "Inno Setup completed without creating $installer"
}
Write-Host "Built: $installer"
