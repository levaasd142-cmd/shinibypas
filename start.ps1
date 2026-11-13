# Same logic as START.cmd: find Python >= 3.12 (not the Microsoft Store stub) and open the launcher.
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$check = 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'
$launcher = Join-Path $PSScriptRoot 'launcher.py'

function Test-Python([string]$exe, [string[]]$prefix) {
    try {
        & $exe @prefix -c $check *> $null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

$py = Get-Command py.exe -ErrorAction SilentlyContinue
if ($py -and (Test-Python $py.Source @('-3'))) {
    $pyw = Get-Command pyw.exe -ErrorAction SilentlyContinue
    if ($pyw) { Start-Process -FilePath $pyw.Source -ArgumentList @('-3', "`"$launcher`"") -WorkingDirectory $PSScriptRoot; exit 0 }
    & $py.Source -3 $launcher; exit $LASTEXITCODE
}

foreach ($candidate in @(Get-Command python.exe -All -ErrorAction SilentlyContinue)) {
    if ($candidate.Source -like '*\WindowsApps\*') { continue }
    if (-not (Test-Python $candidate.Source @())) { continue }
    $windowed = Join-Path (Split-Path -Parent $candidate.Source) 'pythonw.exe'
    if (Test-Path -LiteralPath $windowed) {
        Start-Process -FilePath $windowed -ArgumentList @("`"$launcher`"") -WorkingDirectory $PSScriptRoot; exit 0
    }
    & $candidate.Source $launcher; exit $LASTEXITCODE
}

Write-Host 'Python 3.12 or newer is required. Install it from https://www.python.org/downloads/ (with tcl/tk and the py launcher).'
Write-Host 'The Microsoft Store shortcut "python" is not enough.'
Read-Host 'Press Enter to close'
exit 1
