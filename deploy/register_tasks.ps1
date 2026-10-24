# 注册 Windows 计划任务：三个 stage 在交易日自动跑。
# 用法（服务器上，管理员 PowerShell）：
#   powershell -ExecutionPolicy Bypass -File C:\AppData\a_share_quant\deploy\register_tasks.ps1
# 卸载：
#   powershell -ExecutionPolicy Bypass -File ...register_tasks.ps1 -Remove

param(
    [switch]$Remove,
    [string]$Root = "C:\AppData\a_share_quant",
    [string]$PyData = "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
)

$ErrorActionPreference = "Stop"

if ($Remove) {
    foreach ($n in @("a_share_quant_data","a_share_quant_trade","a_share_quant_settle")) {
        Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue
        Write-Output "removed $n"
    }
    exit 0
}

# 周一~周五 08:30 / 09:15 / 15:35
$jobs = @(
    @{Name="a_share_quant_data";   Stage="data";   At="08:30"},
    @{Name="a_share_quant_trade";  Stage="trade";  At="09:15"},
    @{Name="a_share_quant_settle"; Stage="settle"; At="15:35"}
)

foreach ($j in $jobs) {
    $arg = "scripts\live_auto.py --stage " + $j.Stage
    $action = New-ScheduledTaskAction -Execute $PyData -Argument $arg -WorkingDirectory $Root
    $t = [datetime]::ParseExact($j.At, "HH:mm", $null)
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $t
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 60) -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId "Administrator" -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName $j.Name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    Write-Output ("registered " + $j.Name + "  " + $j.At + "  stage=" + $j.Stage)
}

Write-Output ""
Write-Output "已注册 3 个任务。查看：Get-ScheduledTask -TaskName a_share_quant_* | Format-Table TaskName,State"
Write-Output "手动触发一次：Start-ScheduledTask -TaskName a_share_quant_trade"
