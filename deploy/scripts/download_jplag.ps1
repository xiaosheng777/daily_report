param(
  [string]$Version = "6.0.0"
)

$RootDir = Resolve-Path (Join-Path $PSScriptRoot "../..")
$OutDir = Join-Path $RootDir "vendor/jplag"
$OutFile = Join-Path $OutDir "jplag.jar"
$Url = "https://github.com/jplag/JPlag/releases/download/v$Version/jplag-$Version-jar-with-dependencies.jar"

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
Invoke-WebRequest -Uri $Url -OutFile $OutFile
java -version
java -jar $OutFile --help | Out-Null
Write-Host "Downloaded and verified: $OutFile"
