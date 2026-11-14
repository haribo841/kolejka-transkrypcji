param([string]$Interpreter = '')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\Python-runtime.ps1')
$translationEnvironment = Join-Path $PSScriptRoot '.venv-translation'
New-AppPythonEnvironment -Destination $translationEnvironment -PythonVersion '3.12' -Interpreter $Interpreter
$translationPython = Join-Path $translationEnvironment 'Scripts\python.exe'
& $translationPython -m pip install -r (Join-Path $PSScriptRoot 'requirements-translation.lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Instalacja srodowiska tlumaczen nie powiodla sie.' }
& $translationPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Srodowisko tlumaczen ma niespojne zaleznosci.' }
Write-Host 'Srodowisko gotowe. W karcie Lokalizacje YouTube otworz kreator i pobierz model.'
