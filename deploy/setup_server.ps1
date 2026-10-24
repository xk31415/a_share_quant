# setup_server.ps1 — 云服务器一次性初始化（开 SSH，之后全自动部署由本地执行）
# 用法：以「管理员身份」运行 PowerShell，执行本脚本
#   powershell -ExecutionPolicy Bypass -File setup_server.ps1

$ErrorActionPreference = 'Stop'

Write-Host "== a_share_quant 实盘服务器初始化 ==" -ForegroundColor Cyan

# ---- 0. 必须是管理员 ----
$isAdmin = ([Security.Principal.WindowsPrincipal] `
        [Security.Principal.WindowsIdentity]::GetCurrent() `
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "请用「以管理员身份运行」的 PowerShell 执行本脚本。" -ForegroundColor Red
    exit 1
}

# ---- 1. 安装 OpenSSH Server ----
Write-Host "`n[1/3] 安装 OpenSSH Server..." -ForegroundColor Yellow
$cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' -ErrorAction SilentlyContinue
if ($cap -and $cap.State -eq 'NotPresent') {
    Add-WindowsCapability -Online -Name $cap.Name | Out-Null
    Write-Host "  已安装"
} elseif ($cap -and $cap.State -eq 'Installed') {
    Write-Host "  已存在，跳过"
} else {
    # 部分精简镜像没有该 Capability，改用官方 MSI/GitHub 包
    Write-Host "  系统内无 OpenSSH Capability，尝试下载 Win32-OpenSSH..." -ForegroundColor Yellow
    $zip = "$env:TEMP\OpenSSH-Win64.zip"
    Invoke-WebRequest -Uri 'https://github.com/PowerShell/Win32-OpenSSH/releases/download/v9.5.0.0p1-Beta/OpenSSH-Win64.zip' -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath 'C:\Program Files\OpenSSH' -Force
    & 'C:\Program Files\OpenSSH\install-sshd.ps1'
}

# ---- 2. 启动服务并设为开机自启 ----
Write-Host "`n[2/3] 启动 sshd 并设为自动..." -ForegroundColor Yellow
Set-Service -Name sshd -StartupType Automatic
Start-Service sshd
Get-Service sshd | Format-Table -AutoSize

# ---- 3. 防火墙放行 22 ----
Write-Host "`n[3/3] 防火墙放行 TCP/22..." -ForegroundColor Yellow
if (-not (Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' `
        -DisplayName 'OpenSSH Server (sshd)' -Enabled True `
        -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
}
Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' | Format-Table -AutoSize

# ---- 验证 ----
Write-Host "`n== 完成 ==" -ForegroundColor Green
Write-Host "sshd 状态: $((Get-Service sshd).Status)"
Write-Host "监听端口:"
Get-NetTCPConnection -LocalPort 22 -State Listen -ErrorAction SilentlyContinue |
    Select-Object LocalAddress, LocalPort, State | Format-Table -AutoSize

Write-Host "现在可以从本地用 SSH 连接了：" -ForegroundColor Cyan
Write-Host "  ssh Administrator@8.134.172.15"
