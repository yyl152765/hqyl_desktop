$ErrorActionPreference = 'Stop'

# Run on the recharge PC as the Windows user who runs HQYLAutomation.
# No password is embedded, accepted on the command line, or printed.
Write-Host 'Configure the Shopee recharge guard database password for this Windows user.'
Write-Host 'Use the database password provided by your administrator, not the shop login password.'
$adsSecurePassword = Read-Host 'Database password (hidden)' -AsSecureString
if ($adsSecurePassword.Length -eq 0) {
    throw 'Password is empty. No configuration was changed.'
}

$adsPasswordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($adsSecurePassword)
try {
    $adsPasswordText = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($adsPasswordPointer)
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
