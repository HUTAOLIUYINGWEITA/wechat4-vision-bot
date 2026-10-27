# 小耳朵 开关面板（2026-10-26：on=真上岗/off=真停车）
$ErrorActionPreference = 'Continue'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$toggle = Join-Path $dir 'toggle.ps1'
$lock = Join-Path $dir 'watcher.lock'

function Invoke-Toggle([string]$act) {
    if (-not (Test-Path $toggle)) { Write-Host '  [X] 找不到 toggle.ps1' -ForegroundColor Red; return }
    & powershell -NoProfile -ExecutionPolicy Bypass -File $toggle $act
    if ($LASTEXITCODE -ne 0) { Write-Host ("  [!] toggle.ps1 {0} 退出码={1}" -f $act, $LASTEXITCODE) -ForegroundColor Yellow }
}

function Get-RunStateText {
    $pidNum = 0
    if (Test-Path $lock) {
        try {
            $t = (Get-Content -LiteralPath $lock -Raw -Encoding UTF8).Trim()
            if ($t -match '^\d+$') { $pidNum = [int]$t }
        } catch { }
    }
    if ($pidNum -gt 0) {
        try {
            $p = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $pidNum) -ErrorAction Stop
            if ($p -and $p.CommandLine -and ($p.CommandLine -match 'watcher\.py')) {
                return ("运行中 pid=" + $pidNum)
            }
        } catch { }
    }
    return '未运行'
}

while ($true) {
    Clear-Host
    $state = Get-RunStateText
    Write-Host '============================================' -ForegroundColor Cyan
    Write-Host ("  小耳朵 开关面板          [进程：{0}]" -f $state) -ForegroundColor Cyan
    Write-Host '============================================' -ForegroundColor Cyan
    Invoke-Toggle 'status'
    Write-Host ''
    Write-Host '   [1] 打开（真上岗）        [2] 关闭（真停车）' -ForegroundColor Yellow
    Write-Host '   [3] 急停                  [4] 状态' -ForegroundColor Yellow
    Write-Host '   [5] 退出' -ForegroundColor Yellow
    $c = Read-Host ' 请选择'
    switch ($c) {
        '1' {
            Write-Host ''
            Write-Host '--- 执行：打开（真上岗）---' -ForegroundColor Cyan
            Invoke-Toggle 'on'
            Write-Host ''
            Write-Host '--- 操作后状态 ---' -ForegroundColor Cyan
            Invoke-Toggle 'status'
        }
        '2' {
            Write-Host ''
            Write-Host '--- 执行：关闭（真停车）---' -ForegroundColor Cyan
            Invoke-Toggle 'off'
            Write-Host ''
            Write-Host '--- 操作后状态 ---' -ForegroundColor Cyan
            Invoke-Toggle 'status'
        }
        '3' {
            Write-Host ''
            Write-Host '--- 执行：急停 ---' -ForegroundColor Cyan
            Invoke-Toggle 'panic'
            Write-Host ''
            Write-Host '--- 操作后状态 ---' -ForegroundColor Cyan
            Invoke-Toggle 'status'
        }
        '4' {
            Write-Host ''
            Write-Host '--- 当前状态 ---' -ForegroundColor Cyan
            Invoke-Toggle 'status'
        }
        '5' { return }
        default { }
    }
    if (@('1', '2', '3', '4') -contains $c) {
        Write-Host ''
        Read-Host ' 按回车继续' | Out-Null
    }
}