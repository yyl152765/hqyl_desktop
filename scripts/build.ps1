$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$ReleaseDir = Join-Path $Root "release"

if (-not (Test-Path $Python)) {
    $Python = "python"
}

Push-Location $Root
try {
    & $Python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        throw "pip install failed with exit code $LASTEXITCODE"
    }

    & $Python -m PyInstaller --noconfirm HQYLAutomation.spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }

    if (-not (Test-Path $ReleaseDir)) {
        New-Item -ItemType Directory -Path $ReleaseDir | Out-Null
    }

    $IsccCommand = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    $IsccCandidates = @(
        if ($IsccCommand) { $IsccCommand.Source }
        Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
        Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"
        Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"
    )
    $Iscc = $IsccCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if ($Iscc) {
        & $Iscc (Join-Path $Root "packaging\hqyl_automation.iss")
        if ($LASTEXITCODE -ne 0) {
            throw "Inno Setup failed with exit code $LASTEXITCODE"
        }
    } else {
        Write-Host "Inno Setup ISCC.exe not found. EXE build is ready under dist\HQYLAutomation."
    }
}
finally {
    Pop-Location
}
