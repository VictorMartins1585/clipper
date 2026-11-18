# Baixa o ffmpeg e o deno (Windows x64) e coloca em dist\Clipper\bin.
# Usado pelo GitHub Actions e também serve para quem for gerar o .exe na própria máquina:
#   powershell -ExecutionPolicy Bypass -File scripts\fetch_tools.ps1
param([string]$Dest = "dist\Clipper\bin")

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # deixa o Invoke-WebRequest muito mais rápido

New-Item -ItemType Directory -Force -Path $Dest | Out-Null
$tmp = Join-Path $env:TEMP "clipper-tools"
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

# ffmpeg: build GPL da BtbN (inclui libx264 e libass, necessários para cortar e legendar)
Write-Host "Baixando ffmpeg..."
Invoke-WebRequest "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip" -OutFile "$tmp\ffmpeg.zip"
Expand-Archive "$tmp\ffmpeg.zip" -DestinationPath "$tmp\ffmpeg"
$ff = Get-ChildItem "$tmp\ffmpeg" -Recurse -Filter ffmpeg.exe | Select-Object -First 1
if (-not $ff) { throw "ffmpeg.exe não encontrado dentro do zip" }
Copy-Item $ff.FullName "$Dest\ffmpeg.exe"
$lic = Get-ChildItem "$tmp\ffmpeg" -Recurse -Filter LICENSE.txt | Select-Object -First 1
if ($lic) { Copy-Item $lic.FullName "$Dest\FFMPEG-LICENSE.txt" }

# deno: o yt-dlp precisa de um runtime de JavaScript para resolver os desafios do YouTube
Write-Host "Baixando deno..."
Invoke-WebRequest "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip" -OutFile "$tmp\deno.zip"
Expand-Archive "$tmp\deno.zip" -DestinationPath "$tmp\deno"
if (-not (Test-Path "$tmp\deno\deno.exe")) { throw "deno.exe não encontrado dentro do zip" }
Copy-Item "$tmp\deno\deno.exe" "$Dest\deno.exe"

Write-Host "Pronto: ffmpeg.exe e deno.exe em $Dest"
