#ifndef AppVersion
  #define AppVersion "1.6.5"
#endif
#ifndef DistributionDir
  #define DistributionDir "..\dist\PaperReader"
#endif

[Setup]
AppId={{D0B88327-AD78-4F29-A996-EA9C64B80B07}
AppName=PaperReader
AppVersion={#AppVersion}
AppPublisher=PaperReader
DefaultDirName={localappdata}\Programs\PaperReader
DefaultGroupName=PaperReader
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\release
OutputBaseFilename=PaperReader-{#AppVersion}-Setup-x64
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\PaperReader.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
MinVersion=10.0
CloseApplications=yes
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "{#DistributionDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\PaperReader"; Filename: "{app}\PaperReader.exe"
Name: "{group}\PaperReader (Browser)"; Filename: "{app}\PaperReader.exe"; Parameters: "--browser"
Name: "{autodesktop}\PaperReader"; Filename: "{app}\PaperReader.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\PaperReader.exe"; Description: "Launch PaperReader"; Flags: nowait postinstall skipifsilent
