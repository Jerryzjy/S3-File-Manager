; S3 File Manager Windows installer.
; Build the desktop executable first with build-desktop.ps1, then run
; build-installer.ps1. The release directory is intentionally kept next to
; the installer output so the installer never reaches into a developer's
; home directory.

#ifndef AppVersion
#define AppVersion "0.0.0"
#endif

[Setup]
AppId={{B4F7D7D6-AC33-4B2D-9D42-DB2D0FDCE73B}
AppName=S3 File Manager
AppVersion={#AppVersion}
AppVerName=S3 File Manager {#AppVersion}
AppPublisher=Jerryzjy
AppPublisherURL=https://github.com/Jerryzjy/S3-File-Manager
AppSupportURL=https://github.com/Jerryzjy/S3-File-Manager/issues
AppUpdatesURL=https://github.com/Jerryzjy/S3-File-Manager/releases
DefaultDirName={localappdata}\Programs\S3 File Manager
DefaultGroupName=S3 File Manager
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
UsePreviousAppDir=yes
OutputDir=..\dist\release
OutputBaseFilename=S3FileManager-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\r2_batch_download\preview_assets\s3-file-manager.ico
UninstallDisplayIcon={app}\S3FileManager.exe
UninstallDisplayName=S3 File Manager {#AppVersion}
CloseApplications=yes
CloseApplicationsFilter=S3FileManager.exe
RestartApplications=no
AllowNoIcons=yes
ArchitecturesInstallIn64BitMode=x64compatible
ChangesAssociations=no
VersionInfoDescription=S3 File Manager installer
VersionInfoProductName=S3 File Manager
VersionInfoProductTextVersion={#AppVersion}
VersionInfoTextVersion={#AppVersion}

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; This copies the already verified release bundle, including the executable,
; notices, license files, checksums, and dependency licenses.
Source: "..\dist\release\{#AppVersion}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\S3 File Manager"; Filename: "{app}\S3FileManager.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\S3 File Manager"; Filename: "{app}\S3FileManager.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\S3FileManager.exe"; Description: "{cm:LaunchProgram,S3 File Manager}"; WorkingDir: "{app}"; Flags: postinstall nowait skipifsilent
