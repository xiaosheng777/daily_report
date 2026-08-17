$ErrorActionPreference = "Stop"

$RootDir = Resolve-Path (Join-Path $PSScriptRoot "../..")
Set-Location $RootDir

if (-not (Test-Path "backend/requirements.txt")) {
  throw "backend/requirements.txt not found. Please run this script from inside the daily_report project."
}

New-Item -ItemType Directory -Force -Path "wheelhouse" | Out-Null
Get-ChildItem "wheelhouse" -File -Include *.whl,*.tar.gz,*.zip -ErrorAction SilentlyContinue | Remove-Item -Force

# This script does NOT require Docker Desktop.
# It downloads Linux x86_64 wheels for the backend Docker image: python:3.12-slim.
$pythonCmd = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
  $pythonCmd = "py"
  $pythonArgs = @("-3", "-m", "pip")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
  $pythonCmd = "python"
  $pythonArgs = @("-m", "pip")
} else {
  throw "Python was not found. Install Python first, then rerun this script."
}

& $pythonCmd @pythonArgs install -U pip

& $pythonCmd @pythonArgs download `
  --dest wheelhouse `
  --only-binary=:all: `
  --platform manylinux2014_x86_64 `
  --implementation cp `
  --python-version 312 `
  --abi cp312 `
  -r backend/requirements.txt

$wheels = Get-ChildItem wheelhouse -Filter *.whl -File
if ($wheels.Count -eq 0) {
  throw "No .whl files were downloaded. Check your internet connection or pip error output above."
}

Write-Host "Prepared offline Linux Python wheels in: wheelhouse/"
Get-ChildItem wheelhouse
