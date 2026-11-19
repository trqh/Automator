# Windows x64 development build. Requires Python 3.14, internet for pip, and
# a separately downloaded OFFICIAL CPython 3.14 embeddable x64 ZIP.
# Do not run as Administrator; never bundle existing app data or .env files.
param(
    [Parameter(Mandatory=$true)][string]$EmbedZip,
    [string]$BuildPython = "py -3.14"
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $repo
# Prevent deleted packages from re-entering through stale metadata or build output.
$obsolete = Join-Path $repo 'src\automator\integrations\dataops'
if (Test-Path $obsolete) { throw 'Obsolete dataops package exists in the source tree.' }
$source = Join-Path $repo 'src'
$buildDir = Join-Path $repo 'build'
$eggInfo = Join-Path $source 'automator.egg-info'
if (Test-Path $buildDir) { Remove-Item $buildDir -Recurse -Force }
if (Test-Path $eggInfo) { Remove-Item $eggInfo -Recurse -Force }
$distDir = Join-Path $repo 'dist'
if (!(Test-Path $distDir)) { New-Item -ItemType Directory -Force -Path $distDir | Out-Null }
Get-ChildItem $distDir -Filter 'automator-*.whl' -File | Remove-Item -Force
$zip = (Resolve-Path $EmbedZip).Path
if ($zip -notmatch '(?i)python-3\.14(?:\.\d+)?-embed-amd64\.zip$') {
    throw 'Expected the official Python 3.14 Windows x64 embeddable ZIP (python-3.14.x-embed-amd64.zip).'
}
$venv = Join-Path $repo '.venv-release'
if (!(Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
    & py -3.14 -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Unable to create release build venv.' }
}
$python = Join-Path $venv 'Scripts\python.exe'
& $python -m pip install --upgrade pip build wheel pyinstaller
if ($LASTEXITCODE -ne 0) { throw 'Unable to install build tooling.' }
& $python -m pip install -e .
if ($LASTEXITCODE -ne 0) { throw 'Unable to install Automator in the build venv.' }
& $python -m build --wheel --outdir (Join-Path $repo 'dist')
if ($LASTEXITCODE -ne 0) { throw 'Wheel build failed.' }
$wheel = Get-ChildItem (Join-Path $repo 'dist') -Filter 'automator-*.whl' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (!$wheel) { throw 'Wheel artifact missing.' }
# Inspect the actual wheel, not the source tree or editable install.
$wheelEntries = & $python -m zipfile -l $wheel.FullName
if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect the freshly built wheel.' }
$dataopsEntries = @($wheelEntries | Where-Object { $_ -match '(?i)automator/integrations/dataops/' })
if ($dataopsEntries.Count -gt 0) {
    $dataopsEntries | ForEach-Object { Write-Host $_ }
    throw 'Obsolete dataops package is present in the freshly built wheel.'
}
Write-Host 'WHEEL DATAOPS: []'
$entry = Join-Path $repo 'scripts\automator_launcher.py'
& $python -m PyInstaller --noconfirm --clean --onedir --windowed --name Automator --paths (Join-Path $repo 'src') --collect-all automator --collect-submodules automator $entry
if ($LASTEXITCODE -ne 0) { throw 'GUI freeze failed.' }
$out = Join-Path $repo 'dist\Automator'
$runtime = Join-Path $out 'runtime'
if (Test-Path $runtime) { Remove-Item $runtime -Recurse -Force }
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
Expand-Archive -Path $zip -DestinationPath $runtime -Force
$pth = Get-ChildItem $runtime -Filter 'python3*._pth' | Select-Object -First 1
if (!$pth) { throw 'Python embeddable ._pth was not found.' }
$lines = @(Get-Content -Path $pth.FullName | Where-Object { $_.Trim() -ne 'import site' })
if ($lines -notcontains 'Lib\site-packages') { $lines += 'Lib\site-packages' }
$lines += 'import site'
Set-Content -Path $pth.FullName -Value $lines -Encoding ascii
$site = Join-Path $runtime 'Lib\site-packages'
New-Item -ItemType Directory -Force -Path $site | Out-Null
& $python -m pip install --upgrade --target $site $wheel.FullName
if ($LASTEXITCODE -ne 0) { throw 'Bundled helper dependencies installation failed.' }
# Finalize before static checks so the actual distributable is tested.
$finalizer = Join-Path $repo 'scripts\finalize_windows_release.py'
if (!(Test-Path $finalizer)) { throw 'Release finalizer is missing.' }
& $python $finalizer --stage $repo --mode prepare
if ($LASTEXITCODE -ne 0) { throw 'Release finalization failed; do not publish.' }
& $python (Join-Path $repo 'scripts\check_distribution.py') $out
if ($LASTEXITCODE -ne 0) { throw 'Bundled runtime checks failed.' }
Write-Host "I15C BUILD PASS: $out" -ForegroundColor Green
Write-Host 'Do not publish yet: test Feature 900003 end-to-end in a fresh isolated data directory, including SQLite comparison and history.'
