!include "FileFunc.nsh"
!include "LogicLib.nsh"
!include "nsDialogs.nsh"
!include "x64.nsh"

Var StockBotPowerShellExitCode
Var StockBotPowerShellOutput

!macro customHeader
!macroend

!ifndef BUILD_UNINSTALLER
Var StockBotExistingServicePath
Var StockBotInstallRootValidationError
Var StockBotLiveServiceAuthorized
Var StockBotServiceConsentCheckbox
Var StockBotServiceInstallError

Function StockBotFailInstallRootValidation
  ${If} ${Silent}
    DetailPrint "$StockBotInstallRootValidationError"
  ${Else}
    MessageBox MB_ICONSTOP|MB_OK "$StockBotInstallRootValidationError"
  ${EndIf}
  SetErrorLevel 3
  Quit
FunctionEnd

Function StockBotFailServiceInstall
  ${If} ${Silent}
    DetailPrint "$StockBotServiceInstallError"
  ${Else}
    MessageBox MB_ICONSTOP|MB_OK "$StockBotServiceInstallError"
  ${EndIf}
  SetErrorLevel 4
  Quit
FunctionEnd

Function StockBotValidateInstallRoot
  ${If} $INSTDIR != "$PROGRAMFILES64\StockBot"
    StrCpy $StockBotInstallRootValidationError "StockBot must be installed in the fixed Program Files location."
    Call StockBotFailInstallRootValidation
  ${EndIf}
  InitPluginsDir
  nsExec::ExecToStack /TIMEOUT=60000 '"$SYSDIR\icacls.exe" "$PLUGINSDIR" /inheritance:r /remove:g "*S-1-1-0" /grant:r "*S-1-5-18:F" "*S-1-5-32-544:F" /T /C /Q'
  Pop $StockBotPowerShellExitCode
  Pop $StockBotPowerShellOutput
  ${If} $StockBotPowerShellExitCode != "0"
    StrCpy $StockBotInstallRootValidationError "StockBot installer validation workspace could not be secured."
    Call StockBotFailInstallRootValidation
  ${EndIf}
  nsExec::ExecToStack /TIMEOUT=60000 '"$SYSDIR\icacls.exe" "$PLUGINSDIR" /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" /Q'
  Pop $StockBotPowerShellExitCode
  Pop $StockBotPowerShellOutput
  ${If} $StockBotPowerShellExitCode != "0"
    StrCpy $StockBotInstallRootValidationError "StockBot installer validation workspace could not be secured."
    Call StockBotFailInstallRootValidation
  ${EndIf}
  File /oname=$PLUGINSDIR\validate-stockbot-install-root.ps1 "${PROJECT_DIR}\..\..\tools\validate_stockbot_install_root.ps1"
  ${DisableX64FSRedirection}
  nsExec::ExecToStack /TIMEOUT=600000 '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "$PLUGINSDIR\validate-stockbot-install-root.ps1" -InstallRoot "$INSTDIR"'
  ${EnableX64FSRedirection}
  Pop $StockBotPowerShellExitCode
  Pop $StockBotPowerShellOutput
  ${If} $StockBotPowerShellExitCode == "0"
    ${If} $StockBotPowerShellOutput != "SBIRV1:00"
      StrCpy $StockBotInstallRootValidationError "StockBot 설치 경로 검증 결과를 확인할 수 없습니다. (SBIRV1-93)"
      Call StockBotFailInstallRootValidation
    ${EndIf}
  ${ElseIf} $StockBotPowerShellExitCode == "20"
    StrCpy $StockBotInstallRootValidationError "StockBot 설치에 관리자 권한이 필요합니다. (SBIRV1-20)"
    Call StockBotFailInstallRootValidation
  ${ElseIf} $StockBotPowerShellExitCode == "21"
    StrCpy $StockBotInstallRootValidationError "StockBot은 지정된 Program Files 경로에만 설치할 수 있습니다. (SBIRV1-21)"
    Call StockBotFailInstallRootValidation
  ${ElseIf} $StockBotPowerShellExitCode == "22"
    StrCpy $StockBotInstallRootValidationError "Windows Program Files 경로를 신뢰할 수 없습니다. (SBIRV1-22)"
    Call StockBotFailInstallRootValidation
  ${ElseIf} $StockBotPowerShellExitCode == "23"
    StrCpy $StockBotInstallRootValidationError "StockBot 설치 경로를 만들거나 읽을 수 없습니다. (SBIRV1-23)"
    Call StockBotFailInstallRootValidation
  ${ElseIf} $StockBotPowerShellExitCode == "24"
    StrCpy $StockBotInstallRootValidationError "StockBot 설치 경로에 허용되지 않는 링크가 있습니다. (SBIRV1-24)"
    Call StockBotFailInstallRootValidation
  ${ElseIf} $StockBotPowerShellExitCode == "25"
    StrCpy $StockBotInstallRootValidationError "StockBot 설치 파일 소유권을 초기화할 수 없습니다. (SBIRV1-25)"
    Call StockBotFailInstallRootValidation
  ${ElseIf} $StockBotPowerShellExitCode == "26"
    StrCpy $StockBotInstallRootValidationError "StockBot 설치 경로 권한을 초기화할 수 없습니다. (SBIRV1-26)"
    Call StockBotFailInstallRootValidation
  ${ElseIf} $StockBotPowerShellExitCode == "27"
    StrCpy $StockBotInstallRootValidationError "StockBot 설치 경로를 안전하게 보호할 수 없습니다. (SBIRV1-27)"
    Call StockBotFailInstallRootValidation
  ${ElseIf} $StockBotPowerShellExitCode == "28"
    StrCpy $StockBotInstallRootValidationError "권한 적용 후 설치 경로에서 허용되지 않는 링크가 발견됐습니다. (SBIRV1-28)"
    Call StockBotFailInstallRootValidation
  ${ElseIf} $StockBotPowerShellExitCode == "29"
    StrCpy $StockBotInstallRootValidationError "StockBot 설치 파일 소유자를 검증할 수 없습니다. (SBIRV1-29)"
    Call StockBotFailInstallRootValidation
  ${ElseIf} $StockBotPowerShellExitCode == "30"
    StrCpy $StockBotInstallRootValidationError "StockBot 설치 경로에 안전하지 않은 쓰기 권한이 있습니다. (SBIRV1-30)"
    Call StockBotFailInstallRootValidation
  ${ElseIf} $StockBotPowerShellExitCode == "90"
    StrCpy $StockBotInstallRootValidationError "StockBot 설치 경로 검증 중 예상하지 못한 오류가 발생했습니다. (SBIRV1-90)"
    Call StockBotFailInstallRootValidation
  ${ElseIf} $StockBotPowerShellExitCode == "error"
    StrCpy $StockBotInstallRootValidationError "StockBot 설치 경로 검증기를 실행할 수 없습니다. (SBIRV1-91)"
    Call StockBotFailInstallRootValidation
  ${ElseIf} $StockBotPowerShellExitCode == "timeout"
    StrCpy $StockBotInstallRootValidationError "StockBot 설치 경로 검증 시간이 초과됐습니다. (SBIRV1-92)"
    Call StockBotFailInstallRootValidation
  ${Else}
    StrCpy $StockBotInstallRootValidationError "StockBot 설치 경로 검증 결과가 올바르지 않습니다. (SBIRV1-99)"
    Call StockBotFailInstallRootValidation
  ${EndIf}
FunctionEnd

Function StockBotPreflightPageCreate
  Call StockBotValidateInstallRoot
  Abort
FunctionEnd

Function StockBotServiceConsentPageCreate
  ReadRegStr $StockBotExistingServicePath HKLM "SYSTEM\CurrentControlSet\Services\StockBotLive" "ImagePath"
  ${If} $StockBotExistingServicePath != ""
    StrCpy $StockBotLiveServiceAuthorized "1"
    Abort
  ${EndIf}
  ${If} ${Silent}
    Abort
  ${EndIf}

  nsDialogs::Create 1018
  Pop $0
  ${If} $0 == error
    Abort
  ${EndIf}

  ${NSD_CreateLabel} 0 0 100% 34u "StockBot 자동매매 서비스 설치"
  Pop $0
  ${NSD_CreateLabel} 0 38u 100% 42u "Windows 시작 시 StockBotLive 서비스를 자동으로 실행합니다. 계좌 정보가 등록되기 전에는 주문하지 않으며, 실제 주문은 기존 계좌 및 위험 안전 게이트를 통과해야 합니다."
  Pop $0
  ${NSD_CreateCheckbox} 0 88u 100% 24u "실전 자동매매 서비스 설치 및 자동 시작에 동의합니다."
  Pop $StockBotServiceConsentCheckbox
  nsDialogs::Show
FunctionEnd

Function StockBotServiceConsentPageLeave
  ${NSD_GetState} $StockBotServiceConsentCheckbox $0
  ${If} $0 != ${BST_CHECKED}
    MessageBox MB_ICONSTOP|MB_OK "StockBot을 설치하려면 자동매매 서비스 설치에 동의해야 합니다."
    Abort
  ${EndIf}
  StrCpy $StockBotLiveServiceAuthorized "1"
FunctionEnd

!macro customWelcomePage
  !insertmacro MUI_PAGE_WELCOME
!macroend

!macro customInstallMode
  StrCpy $isForceMachineInstall "1"
!macroend

!macro customPageAfterChangeDir
  Page custom StockBotServiceConsentPageCreate StockBotServiceConsentPageLeave
  Page custom StockBotPreflightPageCreate
!macroend

!macro customInit
  StrCpy $INSTDIR "$PROGRAMFILES64\StockBot"
  StrCpy $StockBotLiveServiceAuthorized "0"
  ReadRegStr $StockBotExistingServicePath HKLM "SYSTEM\CurrentControlSet\Services\StockBotLive" "ImagePath"
  ${If} $StockBotExistingServicePath != ""
    StrCpy $StockBotLiveServiceAuthorized "1"
  ${ElseIf} ${Silent}
    ${GetParameters} $0
    ClearErrors
    ${GetOptions} $0 "/ALLOWLIVEORDERS" $1
    ${If} ${Errors}
      SetErrorLevel 2
      Quit
    ${EndIf}
    StrCpy $StockBotLiveServiceAuthorized "1"
  ${EndIf}
  ${If} ${Silent}
    !insertmacro setInstallModePerAllUsers
    ${If} ${UAC_IsAdmin}
      Call StockBotValidateInstallRoot
    ${ElseIf} $hasPerMachineInstallation != "1"
      StrCpy $StockBotInstallRootValidationError "Silent StockBot installation must be run as administrator."
      Call StockBotFailInstallRootValidation
    ${EndIf}
  ${EndIf}
!macroend

!macro customInstall
  Call StockBotValidateInstallRoot
  ${If} $StockBotLiveServiceAuthorized != "1"
    Abort "StockBot live service consent is required."
  ${EndIf}
  SetDetailsPrint none
  ${DisableX64FSRedirection}
  nsExec::ExecToStack /TIMEOUT=600000 '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "$INSTDIR\resources\stockbot-service\installer\install_stockbot_packaged_service.ps1" -PackageResourcesRoot "$INSTDIR\resources\stockbot-service" -AuthorizeLiveOrders'
  ${EnableX64FSRedirection}
  Pop $StockBotPowerShellExitCode
  Pop $StockBotPowerShellOutput
  ${If} $StockBotPowerShellExitCode == "0"
    DetailPrint "StockBot Windows service installation completed."
  ${ElseIf} $StockBotPowerShellExitCode == "40"
    StrCpy $StockBotServiceInstallError "StockBot 서비스 설치 리소스 또는 관리자 권한을 확인할 수 없습니다. (SBPSI1-40)"
    Call StockBotFailServiceInstall
  ${ElseIf} $StockBotPowerShellExitCode == "41"
    StrCpy $StockBotServiceInstallError "기존 StockBot 서비스 등록 또는 실행 경로를 신뢰할 수 없습니다. (SBPSI1-41)"
    Call StockBotFailServiceInstall
  ${ElseIf} $StockBotPowerShellExitCode == "42"
    StrCpy $StockBotServiceInstallError "기존 StockBot 실전 설정을 검증할 수 없습니다. (SBPSI1-42)"
    Call StockBotFailServiceInstall
  ${ElseIf} $StockBotPowerShellExitCode == "43"
    StrCpy $StockBotServiceInstallError "기존 StockBot 서비스 파일이 일부만 남아 있어 자동 복구할 수 없습니다. (SBPSI1-43)"
    Call StockBotFailServiceInstall
  ${ElseIf} $StockBotPowerShellExitCode == "44"
    StrCpy $StockBotServiceInstallError "StockBot 서비스 업데이트 복구 지점을 만들 수 없습니다. (SBPSI1-44)"
    Call StockBotFailServiceInstall
  ${ElseIf} $StockBotPowerShellExitCode == "45"
    StrCpy $StockBotServiceInstallError "StockBot 서비스 업데이트 또는 복구에 실패했습니다. (SBPSI1-45)"
    Call StockBotFailServiceInstall
  ${ElseIf} $StockBotPowerShellExitCode == "46"
    StrCpy $StockBotServiceInstallError "StockBot 서비스 업데이트가 실패하여 안전하게 중지했습니다. 설치기를 다시 실행하십시오. (SBPSI1-46)"
    Call StockBotFailServiceInstall
  ${ElseIf} $StockBotPowerShellExitCode == "90"
    StrCpy $StockBotServiceInstallError "StockBot 서비스 설치 중 예상하지 못한 오류가 발생했습니다. (SBPSI1-90)"
    Call StockBotFailServiceInstall
  ${ElseIf} $StockBotPowerShellExitCode == "error"
    StrCpy $StockBotServiceInstallError "StockBot 서비스 설치 프로세스를 실행할 수 없습니다. (SBPSI1-91)"
    Call StockBotFailServiceInstall
  ${ElseIf} $StockBotPowerShellExitCode == "timeout"
    StrCpy $StockBotServiceInstallError "StockBot 서비스 설치 시간이 초과됐습니다. (SBPSI1-92)"
    Call StockBotFailServiceInstall
  ${Else}
    StrCpy $StockBotServiceInstallError "StockBot 서비스 설치 중 예상하지 못한 오류가 발생했습니다. (SBPSI1-99)"
    Call StockBotFailServiceInstall
  ${EndIf}
!macroend
!endif

!macro customUnInstall
  ${If} ${isUpdated}
    DetailPrint "Preserving StockBotLive during application update."
  ${Else}
    SetDetailsPrint none
    ${DisableX64FSRedirection}
    nsExec::ExecToStack /TIMEOUT=600000 '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "$INSTDIR\resources\stockbot-service\installer\uninstall_stockbot_service.ps1"'
    ${EnableX64FSRedirection}
    Pop $StockBotPowerShellExitCode
    Pop $StockBotPowerShellOutput
    ${If} $StockBotPowerShellExitCode != "0"
      Abort "StockBot Windows service removal failed. Application files were preserved."
    ${EndIf}
  ${EndIf}
!macroend
