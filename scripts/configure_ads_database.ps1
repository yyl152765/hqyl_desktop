$ErrorActionPreference = 'Stop'

# Run on the recharge PC as the Windows user who runs HQYLAutomation.
# No password is embedded, accepted on the command line, or printed.
Write-Host 'Configure the Shopee recharge guard database password for this Windows user.'
Write-Host 'Enter the approved new PostgreSQL connection from your administrator.'
function Read-Setting([string]$Name, [string]$Prompt, [string]$Default = '') {
    $value = [Environment]::GetEnvironmentVariable($Name, 'User')
    if (-not $value) { $value = $Default }
    $typed = Read-Host "$Prompt [$value]"
    if ($typed) { return $typed.Trim() }
    return $value
}
$adsHost = Read-Setting 'HQYL_GUARD_DB_HOST' 'Approved database or secure tunnel host'
$adsPort = Read-Setting 'HQYL_GUARD_DB_PORT' 'Port' '5432'
$adsDbName = 'hrms'
$adsSchema = 'shopee_ads'
$adsUser = Read-Setting 'HQYL_GUARD_DB_USER' 'Database user'
if (-not $adsHost -or $adsHost -match '[,\s/]' -or $adsHost.TrimEnd('.') -in @('47.112.20.68', 'rpa.whhqyl.com.cn') -or
    -not $adsUser -or $adsPort -notmatch '^\d{1,5}$' -or [int]$adsPort -lt 1 -or [int]$adsPort -gt 65535) { throw 'Invalid configuration for the new database.' }
$adsNetwork = Read-Setting 'HQYL_GUARD_DB_PRIVATE_NETWORK' 'Use approved private VPN or encrypted local tunnel? 1=yes, 0=use DB TLS' '0'
if ($adsNetwork -notin @('0','1')) { throw 'Invalid transport choice.' }
$adsSslMode = if ($adsNetwork -eq '1') { 'disable' } else { 'require' }
$adsSecurePassword = Read-Host 'Database password (hidden)' -AsSecureString
if ($adsSecurePassword.Length -eq 0) {
    throw 'Password is empty. No configuration was changed.'
}

$adsPasswordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($adsSecurePassword)
try {
    $adsPasswordText = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($adsPasswordPointer)
    foreach ($entry in @{HQYL_GUARD_DB_HOST=$adsHost; HQYL_GUARD_DB_PORT=$adsPort; HQYL_GUARD_DB_NAME=$adsDbName; HQYL_GUARD_DB_USER=$adsUser; HQYL_GUARD_DB_SCHEMA=$adsSchema; HQYL_GUARD_DB_SSLMODE=$adsSslMode; HQYL_GUARD_DB_PRIVATE_NETWORK=$adsNetwork}.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'User')
    }
    [Environment]::SetEnvironmentVariable('HQYL_GUARD_DB_PASSWORD', $adsPasswordText, 'User')
    $env:HQYL_GUARD_DB_PASSWORD = $adsPasswordText
    if ([Environment]::GetEnvironmentVariable('HQYL_GUARD_DB_PASSWORD', 'User') -cne $adsPasswordText) {
        throw 'The saved configuration could not be verified.'
    }
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($adsPasswordPointer)
    $adsPasswordText = $null
    $adsSecurePassword.Dispose()
}

# Notify Explorer so applications opened afterwards receive the updated environment.
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class HqylAdsEnvironmentNotification {
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr SendMessageTimeout(IntPtr hwnd, uint msg, UIntPtr wparam,
        string lparam, uint flags, uint timeout, out UIntPtr result);
}
'@
$adsBroadcastResult = [UIntPtr]::Zero
$adsBroadcastStatus = [HqylAdsEnvironmentNotification]::SendMessageTimeout(
    [IntPtr]0xffff, 0x001a, [UIntPtr]::Zero, 'Environment', 2, 3000, [ref]$adsBroadcastResult)
Write-Host 'Saved and verified. No payment or database write was performed.'
Write-Host 'Fully exit HQYLAutomation (including the tray icon), then reopen it.'
if ($adsBroadcastStatus -eq [IntPtr]::Zero) {
    Write-Host 'Windows did not acknowledge the environment update. Sign out and sign in before reopening the app.'
}
