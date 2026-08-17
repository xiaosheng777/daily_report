<#+
.SYNOPSIS
Builds a complete Linux/amd64 offline deployment bundle from Windows PowerShell.

.DESCRIPTION
Requires Docker Desktop to be running in Linux-container mode. The resulting
tar.gz includes the backend and nginx runtime images, application files, and
deployment scripts. It does not include Docker itself or config/llm_api_key.
#>
[CmdletBinding()]
param(
    [string]$OutputDir,
    [string]$Version = (Get-Date -Format 'yyyyMMddHHmmss'),
    [string]$BackendImage = 'daily-report-backend:latest',
    [string]$NginxImage = 'nginx:alpine',
    [string]$TargetPlatform = 'linux/amd64'
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $PSCommandPath
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir '../..')).Path
if (-not $OutputDir) { $OutputDir = Join-Path $ProjectRoot 'dist' }
$OutputDir = [IO.Path]::GetFullPath($OutputDir)
$BundleName = "daily-report-offline-$Version"
$BundleDir = Join-Path $OutputDir $BundleName
$ArchivePath = Join-Path $OutputDir "$BundleName.tar.gz"

function Require-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command not found: $Name"
    }
}

Require-Command docker
Require-Command tar

foreach ($Image in @('python:3.12-slim', $NginxImage)) {
    & docker image inspect $Image *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Required local Docker image is missing: $Image. Pull it on this connected build machine, then run this script again."
    }
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
if (Test-Path $BundleDir) { throw "Bundle path already exists: $BundleDir. Use a different -Version." }
New-Item -ItemType Directory -Path (Join-Path $BundleDir 'images'), (Join-Path $BundleDir 'config') | Out-Null

Write-Host "Building $BackendImage for $TargetPlatform without pulling from the network..."
& docker build --platform=$TargetPlatform --pull=false -f (Join-Path $ProjectRoot 'deploy/Dockerfile.backend') -t $BackendImage $ProjectRoot
if ($LASTEXITCODE -ne 0) { throw 'Docker build failed.' }

Write-Host 'Exporting runtime images...'
& docker save -o (Join-Path $BundleDir 'images/daily-report-images.tar') $BackendImage $NginxImage
if ($LASTEXITCODE -ne 0) { throw 'Docker image export failed.' }
Set-Content -Path (Join-Path $BundleDir 'images/images.txt') -Value @($BackendImage, $NginxImage) -Encoding utf8

Write-Host 'Collecting runtime files...'
function Copy-RuntimeDirectory([string]$Name) {
    $Source = Join-Path $ProjectRoot $Name
    $Destination = Join-Path $BundleDir $Name
    foreach ($File in Get-ChildItem -Path $Source -Recurse -File -Force) {
        $Relative = $File.FullName.Substring($Source.Length).TrimStart([char]'\', [char]'/')
        if ($File.Name -eq 'llm_api_key' -or
            $File.Extension -eq '.pyc' -or
            $Relative -match '(^|[\\/])(__pycache__|\.pytest_cache)([\\/]|$)') {
            continue
        }
        $Target = Join-Path $Destination $Relative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Target) | Out-Null
        Copy-Item -LiteralPath $File.FullName -Destination $Target -Force
    }
}

foreach ($Directory in @('backend', 'frontend', 'wheelhouse', 'deploy')) {
    Copy-RuntimeDirectory $Directory
}
foreach ($File in @('docker-compose.yml', 'DEPLOY.md', 'OFFLINE_UPGRADE_GUIDE.md', 'README.md', 'install.sh')) {
    Copy-Item -Path (Join-Path $ProjectRoot $File) -Destination $BundleDir -Force
}
Copy-Item -Path (Join-Path $ProjectRoot 'config/config.yaml') -Destination (Join-Path $BundleDir 'config/config.yaml.example') -Force

@(
    'Daily Report offline deployment bundle',
    "Version: $Version",
    "Runtime images: $BackendImage, $NginxImage",
    "Platform: $TargetPlatform",
    "Generated at: $([DateTime]::UtcNow.ToString('o'))",
    '',
    'Target prerequisites: Linux x86_64, Docker Engine, and Docker Compose.',
    'No registry/network access is needed by the installer.'
) | Set-Content -Path (Join-Path $BundleDir 'MANIFEST.txt') -Encoding utf8

& tar -C $OutputDir -czf $ArchivePath $BundleName
if ($LASTEXITCODE -ne 0) { throw 'Creating the compressed bundle failed.' }
Write-Host "Bundle created: $ArchivePath"
Write-Host "Transfer it to the offline server, extract it, then run: sudo bash ./$BundleName/deploy/scripts/install.sh /opt/daily-report"
