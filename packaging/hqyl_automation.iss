#define MyAppName "寰球云联自动化平台"
#define MyAppVersion "0.2.72"
#define MyAppPublisher "HQYL"
#define MyAppExeName "HQYLAutomation.exe"

[Setup]
AppId={{83CF1201-A403-45B4-9C5E-3A8C37B7DA36}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\HQYLAutomation
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\release
OutputBaseFilename=HQYLAutomationSetup_{#MyAppVersion}
SetupIconFile=HQYLAutomation.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标："; Flags: unchecked

[Files]
Source: "..\dist\HQYLAutomation\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs notimestamp

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

; ---------------------------------------------------------------------------
;  WebView2 Evergreen Runtime — detect, download & silent-install
; ---------------------------------------------------------------------------
;  pywebview 6.x requires the EdgeChromium (WebView2) rendering engine.
;  Windows 11 ships WebView2 as a non-removable OS component, but Windows 10
;  does not.  The code below runs between the "Ready to Install" page and the
;  actual file extraction:
;
;    1. Query three registry locations (per-machine 64-bit, 32-bit, per-user)
;       for the WebView2 client GUID.
;    2. If none has a non-empty / non-zero version → download the Evergreen
;       Bootstrapper (~1.7 MB) from Microsoft's official CDN and run it with
;       "/silent /install".
;    3. Re-check the registry to confirm the install succeeded.
;    4. On download or install failure, show an actionable error with a manual
;       download link and block the installer from proceeding.
;
;  Because PrivilegesRequired=lowest, the bootstrapper installs WebView2
;  per-user, which is the recommended path for non-admin installers.
; ---------------------------------------------------------------------------

[Code]
const
  { Microsoft's stable "Evergreen Bootstrapper" redirect — always resolves to
    the latest bootstrapper binary (~1.7 MB). }
  WebView2BootstrapperUrl =
    'https://go.microsoft.com/fwlink/p/?LinkId=2124703';
  WebView2BootstrapperFile =
    'MicrosoftEdgeWebview2Setup.exe';

  { The well-known client GUID shared by every Evergreen WebView2 install. }
  WV2_GUID =
    '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

  ManualDownloadHint =
    '您也可以手动下载安装 WebView2 Runtime 后重新运行本安装程序：'#13#10 +
    'https://developer.microsoft.com/microsoft-edge/webview2/';

var
  DownloadPage: TDownloadWizardPage;
  NeedWebView2: Boolean;

// ---- registry helpers ---------------------------------------------------

function HasValidVersion(RootKey: Integer; const SubKey: String): Boolean;
var
  Ver: String;
begin
  Result := RegQueryStringValue(RootKey, SubKey, 'pv', Ver)
            and (Ver <> '') and (Ver <> '0.0.0.0');
end;

function IsWebView2Installed: Boolean;
begin
  { Per-machine, 64-bit view (WOW6432Node) }
  Result := HasValidVersion(
    HKLM,
    'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\' + WV2_GUID);
  if Result then Exit;

  { Per-machine, native 32-bit key }
  Result := HasValidVersion(
    HKLM,
    'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WV2_GUID);
  if Result then Exit;

  { Per-user install }
  Result := HasValidVersion(
    HKCU,
    'Software\Microsoft\EdgeUpdate\Clients\' + WV2_GUID);
end;

// ---- download progress callback -----------------------------------------

function OnDownloadProgress(const Url, FileName: String;
  const Progress, ProgressMax: Int64): Boolean;
begin
  { Return True to keep downloading; False would cancel. }
  if Progress = ProgressMax then
    Log('WebView2 bootstrapper downloaded to {tmp}: ' + FileName);
  Result := True;
end;

// ---- wizard integration -------------------------------------------------

procedure InitializeWizard;
begin
  NeedWebView2 := not IsWebView2Installed;
  if NeedWebView2 then
    Log('WebView2 detected: False — will download during install')
  else
    Log('WebView2 detected: True — no download needed');

  { Create the download page unconditionally so the object is always valid;
    it is only shown when NeedWebView2 is True. }
  DownloadPage := CreateDownloadPage(
    '正在准备运行环境',
    '首次安装需要下载 WebView2 Runtime，请稍候…',
    @OnDownloadProgress);
  DownloadPage.ShowBaseNameInsteadOfUrl := True;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  ExitCode: Integer;
  Bootstrapper: String;
begin
  Result := True;

  { Hook the transition from "Ready to Install" → file extraction. }
  if (CurPageID <> wpReady) or (not NeedWebView2) then
    Exit;

  // ----- step 1: download ------------------------------------------------
  DownloadPage.Clear;
  DownloadPage.Add(
    WebView2BootstrapperUrl,
    WebView2BootstrapperFile,
    '');  { no SHA-256 pinning — the bootstrapper is a tiny stub that
            downloads the real payload from Microsoft's CDN and verifies
            it with its own embedded signature; pinning the stub hash
            would break whenever Microsoft refreshes the bootstrapper. }
  DownloadPage.Show;
  try
    try
      DownloadPage.Download;
    except
      Log('WebView2 bootstrapper download failed');
      SuppressibleMsgBox(
        'WebView2 Runtime 下载失败，请检查网络连接后重试。'#13#10#13#10 +
        ManualDownloadHint,
        mbCriticalError, MB_OK, IDOK);
      Result := False;
      Exit;
    end;
  finally
    DownloadPage.Hide;
  end;

  // ----- step 2: silent install ------------------------------------------
  Bootstrapper := ExpandConstant('{tmp}\' + WebView2BootstrapperFile);

  if not FileExists(Bootstrapper) then
  begin
    Log('WebView2 bootstrapper not found after download');
    SuppressibleMsgBox(
      'WebView2 安装程序下载后未找到，请重试。'#13#10#13#10 +
      ManualDownloadHint,
      mbCriticalError, MB_OK, IDOK);
    Result := False;
    Exit;
  end;

  Log('Launching WebView2 bootstrapper: ' + Bootstrapper);
  if not Exec(Bootstrapper, '/silent /install', '', SW_HIDE,
              ewWaitUntilTerminated, ExitCode) then
  begin
    Log('Failed to launch WebView2 bootstrapper');
    SuppressibleMsgBox(
      '无法启动 WebView2 安装程序。'#13#10#13#10 +
      ManualDownloadHint,
      mbCriticalError, MB_OK, IDOK);
    Result := False;
    Exit;
  end;

  Log('WebView2 bootstrapper exited with code: ' + IntToStr(ExitCode));

  // ----- step 3: verify installation ------------------------------------
  if IsWebView2Installed then
  begin
    Log('WebView2 is now installed — continuing');
    NeedWebView2 := False;
    Exit;                   { Result is already True }
  end;

  { The bootstrapper returned, but the runtime still isn't registered.
    This covers non-zero exit codes AND the rare case where a zero exit
    did not actually persist a registry entry. }
  SuppressibleMsgBox(
    'WebView2 Runtime 安装未成功'
    + ' (退出代码 ' + IntToStr(ExitCode) + ')。'#13#10#13#10
    + ManualDownloadHint,
    mbCriticalError, MB_OK, IDOK);
  Result := False;
end;
