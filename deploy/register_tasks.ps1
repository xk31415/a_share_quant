# 注册 Windows 计划任务：三个 stage 在交易日自动跑。
# 用法（服务器上，管理员 PowerShell）：
#   powershell -ExecutionPolicy Bypass -File C:\AppData\a_share_quant\deploy\register_tasks.ps1
# 卸载：
#   powershell -ExecutionPolicy Bypass -File ...register_tasks.ps1 -Remove
#
# 注意：必须用 schtasks /ru /rp 存密码，任务才是「无论是否登录都运行」。
# 用 Register-ScheduledTask + ServiceAccount 注册出来的会退化成
# 「只使用交互方式」，RDP 一注销就不跑了 —— 这是踩过的坑。

param(
    [switch]$Remove,
    [string]$Root = "C:\AppData\a_share_quant",
    [string]$PyData = "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
)

$ErrorActionPreference = "Stop"

if ($Remove) {
    foreach ($n in @("a_share_quant_data","a_share_quant_trade","a_share_quant_settle")) {
        schtasks /delete /tn $n /f | Out-Null
        Write-Output "removed $n"
    }
    exit 0
}

# 交互式运行时提示输入一次密码，用于「无论是否登录都运行」
$pw = Read-Host -AsSecureString "Administrator 密码（用于计划任务后台运行）"
$BSTR = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw)
$Plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto($BSTR)

$jobs = @(
    @{Name="a_share_quant_data";   Stage="data";   At="08:30"},
    @{Name="a_share_quant_trade";  Stage="trade";  At="09:15"},
    @{Name="a_share_quant_settle"; Stage="settle"; At="15:35"}
)

foreach ($j in $jobs) {
    $tr = '"' + $PyData + '" scripts\live_auto.py --stage ' + $j.Stage
    schtasks /create /tn $j.Name /tr $tr `
        /sc weekly /d MON,TUE,WED,THU,FRI /st $j.At `
        /ru Administrator /rp $Plain /rl HIGHEST /f | Out-Null
    Write-Output ("registered " + $j.Name + "  " + $j.At)
}

Write-Output ""
Write-Output "已注册 3 个任务（无论是否登录都运行）。"
Write-Output "查看：schtasks /query /tn a_share_quant_trade /fo LIST /v"
Write-Output "手动触发：schtasks /run /tn a_share_quant_trade"
