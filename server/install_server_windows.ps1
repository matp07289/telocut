# Windows PC を TeloCut アセットサーバーにする（ログオン時に自動起動・ウィンドウは出さない）。
# TeloCut フォルダ（TeloCutCLI.exe がある所）に置いて、管理者の PowerShell で:
#   powershell -ExecutionPolicy Bypass -File install_server_windows.ps1 -Root "\\192.168.0.10\video"
# 止める:  powershell -ExecutionPolicy Bypass -File install_server_windows.ps1 -Uninstall
param(
    [string]$Root = "",
    [int]$Port = 8770,
    [string]$Data = "$env:USERPROFILE\TeloCutServer",
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$task = "TeloCut Asset Server"
$exe = Join-Path $PSScriptRoot "TeloCutCLI.exe"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
    Get-Process TeloCutCLI -ErrorAction SilentlyContinue | Stop-Process -Force
    Remove-NetFirewallRule -DisplayName $task -ErrorAction SilentlyContinue
    Write-Host "アセットサーバーを停止・削除しました"
    return
}
if (-not (Test-Path $exe)) { throw "TeloCutCLI.exe が見つかりません。TeloCut フォルダに置いて実行してください: $exe" }
if (-not $Root) { throw "-Root に素材フォルダ（例 \\192.168.0.10\video）を指定してください" }
if (-not (Test-Path $Root)) { throw "素材フォルダが見えません: $Root（NAS にエクスプローラーで一度ログインしてください）" }

# 起動コマンド（powershell を隠して起動すると、コンソールのウィンドウも出ない）
$inner = "& '$exe' --asset-server --root '$Root' --data '$Data' --port $Port"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -Command `"$inner`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null

Remove-NetFirewallRule -DisplayName $task -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName $task -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -Profile Private,Domain | Out-Null

Get-Process TeloCutCLI -ErrorAction SilentlyContinue | Stop-Process -Force
Start-ScheduledTask -TaskName $task
Start-Sleep 4
$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and $_.PrefixOrigin -ne "WellKnown" } | Select-Object -First 1).IPAddress
try {
    Invoke-RestMethod "http://127.0.0.1:$Port/api/info" -TimeoutSec 5 | Out-Null
    Write-Host ""
    Write-Host "======================================================"
    Write-Host " 完了: http://${ip}:$Port/"
    Write-Host " 素材フォルダ: $Root   データ: $Data"
    Write-Host " この PC にログオンすると自動で起動します"
    Write-Host "======================================================"
} catch {
    Write-Host "起動を確認できませんでした。手動で試す: & '$exe' --asset-server --root '$Root' --port $Port"
}
