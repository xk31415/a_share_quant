# 注册 Windows 计划任务：三个 stage 在交易日自动跑 + 看门狗每 30 分钟巡检。
# 用法（服务器上，管理员 PowerShell）：
#   powershell -ExecutionPolicy Bypass -File C:\AppData\a_share_quant\deploy\register_tasks.ps1
# 卸载：
#   powershell -ExecutionPolicy Bypass -File ...register_tasks.ps1 -Remove
#
# 两个踩过的坑，改这个文件前先读：
# 1. 必须用 schtasks /ru /rp 存密码，任务才是「无论是否登录都运行」。
#    Register-ScheduledTask + ServiceAccount 会退化成「只使用交互方式」，
#    RDP 一注销就不跑了。
# 2. schtasks **没有工作目录参数**，/tr 里的脚本必须写绝对路径。
#    2026-10-26/27 三天相对路径 + 默认 cwd=System32，Python 报
#    can't open file 退出码 2，脚本一行没跑、一条告警都没有。

param(
    [switch]$Remove,
    [string]$Root = "C:\AppData\a_share_quant",
    [string]$PyData = "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
)

$ErrorActionPreference = "Stop"

if ($Remove) {
    foreach ($n in @("a_share_quant_data","a_share_quant_trade","a_share_quant_settle","a_share_quant_watchdog")) {
        schtasks /delete /tn $n /f | Out-Null
        Write-Output "removed $n"
    }
    exit 0
}

# 交互式运行时提示输入一次密码，用于「无论是否登录都运行」
$pw = Read-Host -AsSecureString "Administrator 密码（用于计划任务后台运行）"
$BSTR = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw)
$Plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto($BSTR)

$Script = Join-Path $Root "scripts\live_auto.py"     # 绝对路径，别用相对
$Watchdog = Join-Path $Root "scripts\watchdog.py"

$jobs = @(
    @{Name="a_share_quant_data";   Tr=('"'+$PyData+'" "'+$Script+'" --stage data');   At="08:30"},
    # trade 用 09:29 启动（提前 1 分钟）：stage_trade 跑完「拉持仓 + 算信号」（约 25 秒）
    # 后会等到 live.yaml 的 order_time（09:30:05）才下单。为什么不能 09:15：那只是集合
    # 竞价的申报窗口，09:25 才撮合出开盘价、09:30 才连续竞价，在 09:15 下单只能拿信号日
    # 收盘价挂限价——2026-10-28 实测 3 笔里 2 笔真实买不到。提前 1 分钟是为了让订单时刻
    # 稳定落在 09:30:05，不被前面步骤的耗时波动推后（若写 09:30，实际下单会是 09:30:25）。
    @{Name="a_share_quant_trade";  Tr=('"'+$PyData+'" "'+$Script+'" --stage trade');  At="09:29"},
    @{Name="a_share_quant_settle"; Tr=('"'+$PyData+'" "'+$Script+'" --stage settle'); At="15:35"}
)

foreach ($j in $jobs) {
    schtasks /create /tn $j.Name /tr $j.Tr `
        /sc weekly /d MON,TUE,WED,THU,FRI /st $j.At `
        /ru Administrator /rp $Plain /rl HIGHEST /f | Out-Null
    Write-Output ("registered " + $j.Name + "  " + $j.At)
}

# 看门狗：每 30 分钟巡检一次心跳（周末逻辑里自己跳过）。
# 它是"任务没跑起来"时唯一的告警来源——进程内推送那时是沉默的。
$wd = '"' + $PyData + '" "' + $Watchdog + '"'
schtasks /create /tn a_share_quant_watchdog /tr $wd `
    /sc minute /mo 30 /ru Administrator /rp $Plain /rl HIGHEST /f | Out-Null
Write-Output "registered a_share_quant_watchdog  every 30min"

Write-Output ""
Write-Output "已注册 4 个任务（无论是否登录都运行，脚本均为绝对路径）。"
Write-Output "查看：schtasks /query /tn a_share_quant_trade /fo LIST /v"
Write-Output "手动触发：schtasks /run /tn a_share_quant_trade"
Write-Output "验证任务真的会跑：触发后看 live\logs\<今天>.log 是否更新、上次结果是否为 0"
