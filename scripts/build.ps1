param(
    [string]$OutputDirectory = [Environment]::GetFolderPath('Desktop'),
    [string]$Notes = '',
    [switch]$SkipDependencyInstall,
    [switch]$SkipInternalDependencies,
    [switch]$Check
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$ReleaseDir = Join-Path $Root "release"
$OriginalSourceDateEpoch = $env:SOURCE_DATE_EPOCH
$OriginalPythonHashSeed = $env:PYTHONHASHSEED
$OriginalBuildProfile = $env:HQYL_BUILD_PROFILE
$BuildProfile = if ($SkipInternalDependencies) { 'limited' } else { 'full' }

if (-not (Test-Path $Python)) {
    $Python = "python"
}

# Fail before building when the complete upload package cannot be produced.
$Version = & $Python (Join-Path $PSScriptRoot 'package_release.py') --print-version
if ($LASTEXITCODE -ne 0) { throw 'Release version validation failed.' }
& $Python (Join-Path $PSScriptRoot 'package_release.py') --check
if ($LASTEXITCODE -ne 0) { throw 'WinRAR preflight failed.' }

$IsccCommand = Get-Command 'ISCC.exe' -ErrorAction SilentlyContinue
$IsccCandidates = @(
    if ($IsccCommand) { $IsccCommand.Source }
    Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'
    Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe'
    Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'
)
$Iscc = $IsccCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1
$Missing = @()
if (-not $Iscc) { $Missing += 'Inno Setup 6 (ISCC.exe)' }
$DependencyEntryFiles = @{
    'superbrowser_process' = 'main\shopee\ads_recharge_guard.py'
    'mabang_process' = 'util\bigseller_request_util.py'
}
foreach ($Dependency in @('superbrowser_process', 'mabang_process') | Where-Object { -not $SkipInternalDependencies }) {
    $DependencyPath = Join-Path (Join-Path $Root 'runtime') $Dependency
    $EntryFile = Join-Path $DependencyPath $DependencyEntryFiles[$Dependency]
    if (-not (Test-Path -LiteralPath $EntryFile -PathType Leaf)) {
        $Missing += "Internal source: $EntryFile"
    }
}
if ($Missing.Count -gt 0) {
    throw ("Missing build prerequisites:`n - " + ($Missing -join "`n - ") +
        "`nThe runtime directory must contain the bundled source and public configuration files." +
        "`nCreating empty folders does not supply these dependencies. See README.md (internal dependencies).")
}
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) { throw 'Specify -OutputDirectory for the RAR archive.' }
if ($Check) {
    Write-Host "Build prerequisites are ready for version $Version (profile: $BuildProfile)."
    return
}

# Keep Python bytecode archives and executable headers reproducible across builds.
$env:SOURCE_DATE_EPOCH = "946684800"
$env:PYTHONHASHSEED = "0"
$env:HQYL_BUILD_PROFILE = $BuildProfile

Push-Location $Root
try {
    if (-not $SkipDependencyInstall) {
        & $Python -m pip install -r requirements.txt
        if ($LASTEXITCODE -ne 0) {
            throw "pip install failed with exit code $LASTEXITCODE"
        }
    }

    if (-not $SkipInternalDependencies) {
        & $Python scripts\build_public_runtime_configs.py
        if ($LASTEXITCODE -ne 0) {
            throw "public runtime config generation failed with exit code $LASTEXITCODE"
        }

        & $Python scripts\stage_superbrowser_source.py
        if ($LASTEXITCODE -ne 0) {
            throw "Public runtime source staging failed with exit code $LASTEXITCODE"
        }
    } else {
        Write-Host 'Building LIMITED edition. Features requiring the two internal projects will be unavailable.'
    }
    & $Python -m PyInstaller --clean --noconfirm HQYLAutomation.spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }

    $FrozenExe = Join-Path $Root 'dist\HQYLAutomation\HQYLAutomation.exe'
    $CheckReport = Join-Path $Root "build\self_check_$(Get-Date -Format 'yyyyMMdd_HHmmss_fffffff').json"
    $CheckProcess = Start-Process -FilePath $FrozenExe -ArgumentList @('--self-check', "`"--self-check-report=$CheckReport`"") -PassThru -Wait -WindowStyle Hidden
    if ($CheckProcess.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $CheckReport)) {
        throw "Frozen application self-check failed. See $CheckReport"
    }
    $CheckResult = Get-Content -LiteralPath $CheckReport -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($CheckResult.status -ne 'passed' -or $CheckResult.version -ne $Version -or $CheckResult.build_profile.name -ne $BuildProfile) {
        throw "Frozen application self-check report does not match this build: $CheckReport"
    }

    if (-not (Test-Path $ReleaseDir)) {
        New-Item -ItemType Directory -Path $ReleaseDir | Out-Null
    }

    $Installer = Join-Path $ReleaseDir "HQYLAutomationSetup_$Version.exe"
    if (Test-Path -LiteralPath $Installer) {
        $Backup = "$Installer.$(Get-Date -Format 'yyyyMMdd_HHmmss_fffffff').bak"
        Copy-Item -LiteralPath $Installer -Destination $Backup
    }
    & $Iscc (Join-Path $Root "packaging\hqyl_automation.iss")
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup failed with exit code $LASTEXITCODE"
    }

    $PackageArgs = @('scripts\package_release.py', $Version, '--fresh-build', '--rar-output-dir', $OutputDirectory, '--self-check-report', $CheckReport)
    if ($Notes) { $PackageArgs += @('--notes', $Notes) }
    & $Python @PackageArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Release packaging failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location

    if ($null -eq $OriginalSourceDateEpoch) {
        Remove-Item Env:\SOURCE_DATE_EPOCH -ErrorAction SilentlyContinue
    } else {
        $env:SOURCE_DATE_EPOCH = $OriginalSourceDateEpoch
    }

    if ($null -eq $OriginalPythonHashSeed) {
        Remove-Item Env:\PYTHONHASHSEED -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONHASHSEED = $OriginalPythonHashSeed
    }
    if ($null -eq $OriginalBuildProfile) {
        Remove-Item Env:\HQYL_BUILD_PROFILE -ErrorAction SilentlyContinue
    } else {
        $env:HQYL_BUILD_PROFILE = $OriginalBuildProfile
    }
}
