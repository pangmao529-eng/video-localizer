Unicode true
ManifestDPIAware true
RequestExecutionLevel user
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"

Name "Young Tiger Video Localizer"
OutFile "${OUTPUT}"
InstallDir "$LOCALAPPDATA\YoungTiger\VideoLocalizer"
SetCompressor /FINAL zlib
CRCCheck force
ShowInstDetails show
ShowUninstDetails show
!define ARP "Software\Microsoft\Windows\CurrentVersion\Uninstall\YoungTigerVideoLocalizer"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\video-localizer.exe"
!define MUI_FINISHPAGE_RUN_PARAMETERS "setup --gui"
!define MUI_FINISHPAGE_RUN_TEXT "$(Configure)"
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"
!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "Korean"

LangString Configure ${LANG_ENGLISH} "Configure your model connection"
LangString Configure ${LANG_SIMPCHINESE} "配置模型 API 连接"
LangString Configure ${LANG_KOREAN} "모델 API 연결 설정"

Function .onInit
  ${Unless} ${RunningX64}
    MessageBox MB_ICONSTOP "Windows x64 is required."
    Abort
  ${EndUnless}
  SetShellVarContext current
  SetRegView 64
  System::Call 'kernel32::CreateMutexW(p 0, i 0, w "Local\YoungTiger.VideoLocalizer.Install") p.r0 ?e'
  Pop $1
  ${If} $1 = 183
    MessageBox MB_ICONSTOP "Another installation is running."
    Abort
  ${EndIf}
FunctionEnd

Section
  ; Use a fixed account-level path even when an old shortcut or /D is supplied.
  StrCpy $INSTDIR "$LOCALAPPDATA\YoungTiger\VideoLocalizer"
  StrCpy $0 "$INSTDIR\versions\${REVISION}"
  CreateDirectory "$INSTDIR\versions"
  ClearErrors
  SetOutPath "$0"
  File /r "${PAYLOAD}\*"
  IfErrors failed
  SetOutPath "$INSTDIR"
  ; The launcher reads active.txt once on startup. Keep retained versions usable
  ; by running jobs and switch the pointer only after all files are present.
  IfFileExists "$INSTDIR\video-localizer.exe" 0 write_launcher
  IfFileExists "$INSTDIR\launcher.id" 0 write_launcher
  FileOpen $1 "$INSTDIR\launcher.id" r
  FileRead $1 $2
  FileClose $1
  StrCmp $2 "${LAUNCHER_ID}" launcher_present
  write_launcher:
    ClearErrors
    File "${LAUNCHER}"
    IfErrors failed
    FileOpen $1 "$INSTDIR\launcher.id" w
    FileWrite $1 "${LAUNCHER_ID}"
    FileClose $1
    IfErrors failed
  launcher_present:
  File "${CMD_SHIM}"
  File "/oname=README.md" "${PRODUCT_README}"
  WriteUninstaller "$INSTDIR\uninstall.exe"
  IfErrors failed
  FileOpen $1 "$INSTDIR\active.next" w
  IfErrors failed
  FileWrite $1 "${REVISION}"
  FileClose $1
  System::Call 'kernel32::MoveFileExW(w "$INSTDIR\active.next", w "$INSTDIR\active.txt", i 9) i.r1'
  IntCmp $1 0 failed
  CreateShortCut "$SMPROGRAMS\Young Tiger Video Localizer.lnk" "$INSTDIR\video-localizer.exe" "setup --gui"
  WriteRegStr HKCU "${ARP}" "DisplayName" "Young Tiger Video Localizer"
  WriteRegStr HKCU "${ARP}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${ARP}" "Publisher" "Young Tiger"
  WriteRegStr HKCU "${ARP}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${ARP}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegDWORD HKCU "${ARP}" "NoModify" 1
  WriteRegDWORD HKCU "${ARP}" "NoRepair" 1
  Goto done
  failed:
    SetErrorLevel 1
    Abort "Installation failed while writing program files. Close running video tasks and rerun setup."
  done:
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  SetRegView 64
  StrCpy $0 "$LOCALAPPDATA\YoungTiger\VideoLocalizer"
  StrCmp $INSTDIR $0 +2
  Abort "Unexpected installation path."
  IfFileExists "$INSTDIR\active.txt" +2
  Abort "Installation metadata is missing."
  Delete "$SMPROGRAMS\Young Tiger Video Localizer.lnk"
  DeleteRegKey HKCU "${ARP}"
  Delete "$INSTDIR\video-localizer.cmd"
  Delete "$INSTDIR\video-localizer.exe"
  Delete "$INSTDIR\launcher.id"
  Delete "$INSTDIR\README.md"
  Delete "$INSTDIR\active.txt"
  Delete "$INSTDIR\active.next"
  Delete "$INSTDIR\uninstall.exe"
  ; Delete only files the release installer explicitly embedded. Private API
  ; settings, jobs, exports and third-party files never enter this list.
  FindFirst $2 $3 "$INSTDIR\versions\*"
  next_version:
    StrCmp $3 "" finished_versions
    StrCmp $3 "." read_next
    StrCmp $3 ".." read_next
    StrCpy $4 "$INSTDIR\versions\$3"
    IfFileExists "$4\video-localizer-release.txt" 0 read_next
    !include "${UNINSTALL_FILES}"
    RMDir "$4"
  read_next:
    FindNext $2 $3
    Goto next_version
  finished_versions:
    FindClose $2
    RMDir "$INSTDIR\versions"
    RMDir "$INSTDIR"
SectionEnd
