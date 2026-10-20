# Windows PC を TeloCut のレンダーノード（レンダリング用 PC）にする（ログオン時に自動起動・ウィンドウは出さない）。
# TeloCut フォルダ（TeloCutCLI.exe がある所）をこの PC にコピーし、その中で PowerShell を開いて:
#   powershell -ExecutionPolicy Bypass -File install_render_node.ps1 -Server http://192.168.0.20:8770
# 止める:  powershell -ExecutionPolicy Bypass -File install_render_node.ps1 -Uninstall
# 素材は編集 PC と同じパス（\\NAS\video など）で見えるようにしておく
param(
    [string]$Server = "",
    [string]$Name = $env:COMPUTERNAME,
    [string]$Kinds = "",            # 受ける仕事: render,merge,full,transcode（空 = 全部）
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$task = "TeloCut Render Node"
$exe = Join-Path $PSScriptRoot "TeloCutCLI.exe"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='TeloCutCLI.exe'" | Where-Object { $_.CommandLine -like "*--render-node*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Write-Host "レンダーノードを停止・削除しました"
    return
}
if (-not (Test-Path $exe)) { throw "TeloCutCLI.exe が見つかりません。TeloCut フォルダに置いて実行してください: $exe" }
if (-not $Server) { throw "-Server にアセットサーバーの URL（例 http://192.168.0.20:8770）を指定してください" }
if ($Server -notlike "http*") { $Server = "http://$Server" }
try { Invoke-RestMethod "$Server/api/info" -TimeoutSec 5 | Out-Null } catch { throw "アセットサーバー $Server に接続できません" }

$inner = "& '$exe' --render-node --server '$Server' --name '$Name'"
if ($Kinds) { $inner += " --kinds '$Kinds'" }
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -Command `"$inner`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Priority 7
Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $task
Start-Sleep 12
$online = Invoke-RestMethod "$Server/api/render/nodes" -TimeoutSec 5 | Where-Object { $_.name -eq $Name -and $_.online }
if ($online) {
    Write-Host "完了: レンダーノード「$Name」が $Server に接続しました（ログオン時に自動で起動します）"
} else {
    Write-Host "登録しました。接続の確認は $Server/ の「ファーム」タブで（数秒かかることがあります）"
}
