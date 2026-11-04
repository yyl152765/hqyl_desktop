[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$DriverDirectory,
    [Parameter(Mandatory = $true)][ValidateRange(1, 9999)][int]$CoreMajor
)
# Read one local driver and run only --version. Never repair or download files.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$report = [ordered]@{
    schema = 1; status = 'failed'; code = ''; requested_major = $CoreMajor
    driver_file = "chromedriver$CoreMajor.exe"; file_exists = $false; file_bytes = $null
    pe_signature_valid = $false; machine = $null; process_started = $false
    exit_code = $null; driver_version = $null; major_matches = $null
    winerror = $null; exception_type = $null; terminated_own_process = $false
}
$process = $null
$reader = $null
try {
    if (-not [System.IO.Directory]::Exists($DriverDirectory)) {
        $report.code = 'driver_directory_missing'
    } else {
        $driverPath = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($DriverDirectory, $report.driver_file))
        if (-not [System.IO.File]::Exists($driverPath)) {
            $report.code = 'driver_file_missing'
        } else {
            $report.file_exists = $true
            $report.file_bytes = ([System.IO.FileInfo]::new($driverPath)).Length
            $reader = [System.IO.BinaryReader]::new([System.IO.File]::OpenRead($driverPath))
            if ($reader.BaseStream.Length -ge 64 -and $reader.ReadUInt16() -eq 0x5A4D) {
                $reader.BaseStream.Position = 60
                $offset = $reader.ReadInt32()
                if ($offset -ge 64 -and $offset -le $reader.BaseStream.Length - 24) {
                    $reader.BaseStream.Position = $offset
                    if ($reader.ReadUInt32() -eq 0x00004550) {
                        $report.pe_signature_valid = $true
                        $machine = $reader.ReadUInt16()
                        $report.machine = ('0x{0:X4}' -f $machine)
                    }
                }
            }
            $reader.Dispose(); $reader = $null
            if (-not $report.pe_signature_valid) {
                $report.code = 'invalid_pe_signature_not_executed'
            } else {
                $info = [System.Diagnostics.ProcessStartInfo]::new()
                $info.FileName = $driverPath
                $info.Arguments = '--version'
                $info.UseShellExecute = $false
                $info.CreateNoWindow = $true
                $info.RedirectStandardOutput = $true
                $info.RedirectStandardError = $true
                $process = [System.Diagnostics.Process]::new()
                $process.StartInfo = $info
                $report.code = 'driver_start_failed'
                [void]$process.Start()
                $report.process_started = $true
                $stdout = $process.StandardOutput.ReadToEndAsync()
                $stderr = $process.StandardError.ReadToEndAsync()
                if (-not $process.WaitForExit(5000)) {
                    $report.code = 'driver_version_timeout'
                    $process.Kill()
                    $report.terminated_own_process = $process.WaitForExit(1000)
                } else {
                    $report.exit_code = $process.ExitCode
                    $report.code = 'driver_version_unrecognized'
                    if ($stdout.Wait(500) -and $stderr.Wait(500)) {
                        # Return only a parsed numeric version, never arbitrary output.
                        $match = [regex]::Match($stdout.Result.Trim(), '\AChromeDriver ([0-9]{1,4}(?:\.[0-9]{1,6}){3})(?:\s+[^\r\n]*)?\z')
                        if ($report.exit_code -eq 0 -and $match.Success) {
                            $report.driver_version = $match.Groups[1].Value
                            $report.major_matches = [int]($report.driver_version.Split('.')[0]) -eq $CoreMajor
                            $report.code = 'driver_major_mismatch'
                            if ($report.major_matches) { $report.status = 'passed'; $report.code = 'driver_version_ok' }
                        }
                    }
                }
            }
        }
    }
} catch {
    if (-not $report.code) { $report.code = 'driver_inspection_failed' }
    $exception = $_.Exception
    for ($depth = 0; $null -ne $exception -and $depth -lt 10; $depth++) {
        $report.exception_type = $exception.GetType().Name
        if ($exception -is [System.ComponentModel.Win32Exception]) {
            $report.winerror = $exception.NativeErrorCode
            break
        }
        $exception = $exception.InnerException
    }
} finally {
    if ($null -ne $reader) { $reader.Dispose() }
    if ($null -ne $process) {
        try {
            if ($report.process_started -and -not $process.HasExited) {
                $process.Kill()
                $report.terminated_own_process = $process.WaitForExit(1000)
            }
        } catch { $report.code = 'own_driver_process_cleanup_failed'; $report.status = 'failed' }
        $process.Dispose()
    }
}
$report | ConvertTo-Json -Depth 3 -Compress
if ($report.status -eq 'passed') { exit 0 }
exit 1
