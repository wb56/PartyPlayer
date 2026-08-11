$ErrorActionPreference = "Stop"

$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Virtuelle Umgebung fehlt: $Python"
}

$PythonRoot = & $Python -c "import sys; print(sys.base_prefix)"
$InstalledTcl = Join-Path $PythonRoot "tcl\tcl8.6"
$InstalledTk = Join-Path $PythonRoot "tcl\tk8.6"
$BuildTcl = Join-Path $PSScriptRoot "build\tcl8.6"
if (-not (Test-Path (Join-Path $InstalledTcl "init.tcl"))) {
    throw "Tcl/Tk-Laufzeit der Python-Installation fehlt."
}

# Die lokale Python-Installation enthaelt in init.tcl eine doppelte CR-Markierung,
# die Tcl und damit auch PyInstaller an der Initialisierung hindert. Fuer den Build
# wird eine normalisierte Kopie verwendet; die Python-Installation bleibt unberuehrt.
Copy-Item $InstalledTcl $BuildTcl -Recurse -Force
$InitFile = Join-Path $BuildTcl "init.tcl"
$InitContent = [System.IO.File]::ReadAllText($InitFile)
$InitContent = $InitContent.Replace("`r`r`n", "`r`n")
[System.IO.File]::WriteAllText($InitFile, $InitContent, [System.Text.Encoding]::ASCII)
$env:TCL_LIBRARY = $BuildTcl
$env:TK_LIBRARY = $InstalledTk

& $Python -m PyInstaller --noconfirm --clean (Join-Path $PSScriptRoot "DeckRelay.spec")

$Runtime = Join-Path $PSScriptRoot "dist\DeckRelay"
New-Item -ItemType Directory -Force (Join-Path $Runtime "data") | Out-Null
New-Item -ItemType Directory -Force (Join-Path $Runtime "logs") | Out-Null
Copy-Item (Join-Path $PSScriptRoot "LAUFZEIT-README.txt") $Runtime -Force
Copy-Item (Join-Path $PSScriptRoot "LICENSE") $Runtime -Force
Copy-Item (Join-Path $PSScriptRoot "THIRD_PARTY_LICENSES.md") $Runtime -Force

& $Python (Join-Path $PSScriptRoot "scripts\check_release_artifact.py") $Runtime
if ($LASTEXITCODE -ne 0) {
    throw "Releaseprüfung auf externe VLC-/FFmpeg-Laufzeitdateien fehlgeschlagen."
}

Write-Host "Laufzeitverzeichnis erstellt: $Runtime"
Write-Host "Smoke-Test: & $Python scripts\release_smoke_test.py $Runtime"
