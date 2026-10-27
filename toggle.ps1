# 微信消息看门狗 - 总开关（真上岗 / 真停车）/ 急停 / 状态
# 2026-10-26 改造：on = 起进程 + autoSend；off = 杀进程；stop = 只杀进程；panic = 立即杀
# 用法（在项目目录）:
#   .\toggle.ps1 on      真上岗：enabled=true + autoSend=true + 删 STOP + 确保 watcher 进程在跑
#   .\toggle.ps1 off     真停车：enabled=false + autoSend=false + 杀掉 watcher 进程 + 清 lock
#   .\toggle.ps1 stop    只杀 watcher 进程（不动开关配置）
#   .\toggle.ps1 panic   急停：写 STOP + 立即杀进程 + 清 lock
#   .\toggle.ps1 start   后台启动 watcher（与 on 共用同一段启动逻辑）
#   .\toggle.ps1 status  状态（含进程命令行校验 + 开关/进程一致性检查）
param(
    [Parameter(Position = 0)]
    [string]$Action = "status"
)

$ErrorActionPreference = "Stop"
$Base = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $Base "config.json"
$StopFile = Join-Path $Base "STOP"
$LockFile = Join-Path $Base "watcher.lock"
$LogDir = Join-Path $Base "logs"
$WatcherPy = Join-Path $Base "watcher.py"
$Python = "python"
if (-not (Test-Path $Python)) { $Python = "python" }
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Read-Cfg {
    if (-not (Test-Path $ConfigPath)) { throw "找不到 config.json: $ConfigPath" }
    return (Get-Content -Path $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json)
}

function Set-CfgBool {
    # 硬约束：原地只改「顶层」布尔值（保留原格式/中文），UTF-8 无 BOM；写前断 BOM、写后 ConvertFrom-Json + 值校验
    # 说明：config.json 可能被别的工具以 2/4 空格缩进重排过，所以按「所有同名键里缩进最小 = 顶层键」定位，
    #       而不是写死缩进；嵌套的 gen.enabled / guard.enabled / busyGuard.enabled 缩进更深，不会被误改。
    param([string]$Key, [bool]$Value)
    $bytes = [System.IO.File]::ReadAllBytes($ConfigPath)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        throw "config.json 写前发现 BOM，已中止（请先修）"
    }
    $text = [System.IO.File]::ReadAllText($ConfigPath)
    $nl = "`n"
    if ($text.Contains("`r`n")) { $nl = "`r`n" }
    $lines = $text -split "`r?`n"
    $linePat = '^(\s*)"' + [regex]::Escape($Key) + '"\s*:\s*(true|false)(.*)$'
    $best = -1
    $bestIndent = 999999
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $m = [regex]::Match($lines[$i], $linePat)
        if ($m.Success) {
            $indent = $m.Groups[1].Value.Length
            if ($indent -lt $bestIndent) { $bestIndent = $indent; $best = $i }
        }
    }
    if ($best -lt 0) { throw ('config.json 里找不到配置项 "' + $Key + '"，已中止') }
    if ($bestIndent -gt 8) { throw ('配置项 "' + $Key + '" 最小缩进 ' + $bestIndent + ' 空格，疑似不是顶层键，已中止') }
    $val = "false"
    if ($Value) { $val = "true" }
    $line = $lines[$best]
    $line2 = [regex]::Replace($line, '("' + [regex]::Escape($Key) + '"\s*:\s*)(true|false)', ('${1}' + $val))
    $lines[$best] = $line2
    $text2 = $lines -join $nl
    [System.IO.File]::WriteAllText($ConfigPath, $text2, $Utf8NoBom)
    $bytes2 = [System.IO.File]::ReadAllBytes($ConfigPath)
    if ($bytes2.Length -ge 3 -and $bytes2[0] -eq 0xEF -and $bytes2[1] -eq 0xBB -and $bytes2[2] -eq 0xBF) {
        throw "config.json 写后出现 BOM，已中止"
    }
    $check = Get-Content -Path $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($null -eq $check) { throw "config.json 写后 ConvertFrom-Json 校验失败" }
    $actual = $check.PSObject.Properties[$Key].Value
    if ([bool]$actual -ne $Value) { throw ('config.json 写后值校验失败: "' + $Key + '"=' + $actual + ' 期望=' + $Value) }
    return $check
}

function Get-WatcherPidFromLock {
    if (-not (Test-Path $LockFile)) { return 0 }
    try {
        $text = (Get-Content -Path $LockFile -Raw -Encoding UTF8).Trim()
        if ($text -match '^\d+$') { return [int]$text }
    } catch { }
    return 0
}

function Get-ProcInfo {
    param([int]$ProcId)
    if ($ProcId -le 0) { return $null }
    try {
        $p = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $ProcId) -ErrorAction Stop
        if ($null -ne $p) { return @{ pid = [int]$p.ProcessId; cmd = [string]$p.CommandLine } }
    } catch { }
    return $null
}

function Test-WatcherCmd {
    param([string]$Cmd)
    if (-not $Cmd) { return $false }
    return ($Cmd -match 'watcher\.py')
}

function Test-PidAlive {
    param([int]$ProcId)
    if ($ProcId -le 0) { return $false }
    try { $null = Get-Process -Id $ProcId -ErrorAction Stop; return $true } catch { return $false }
}

function Get-WatcherProcess {
    # 返回 @{pid; cmd; source='lock'|'scan'} 或 $null；只认「命令行含 watcher.py」的进程
    $lockPid = Get-WatcherPidFromLock
    if ($lockPid -gt 0) {
        $info = Get-ProcInfo -ProcId $lockPid
        if ($info -and (Test-WatcherCmd -Cmd $info.cmd)) {
            return @{ pid = $lockPid; cmd = $info.cmd; source = "lock" }
        }
    }
    try {
        $procs = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
            ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and $_.CommandLine -and ($_.CommandLine -match 'watcher\.py')
        })
        if ($procs.Count -gt 0) {
            $p = $procs[0]
            return @{ pid = [int]$p.ProcessId; cmd = [string]$p.CommandLine; source = "scan" }
        }
    } catch { }
    return $null
}

function Write-Lock {
    param([int]$ProcId)
    [System.IO.File]::WriteAllText($LockFile, [string]$ProcId, $Utf8NoBom)
}

function Clear-Lock {
    if (Test-Path $LockFile) { Remove-Item -Path $LockFile -Force -ErrorAction SilentlyContinue }
}

function Write-PanicLog {
    # 2026-10-27 第一阶段：三处急停都要留 [PANIC] 日志说明来源。
    #   来源=shortcut-toggle.ps1 的由本函数写；hotkey / wechat-text 由 watcher.py 写（同一目录 logs/panic-*.jsonl）。
    param([string]$Source, [string]$Detail)
    try {
        $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
        $line = "{0} [PANIC] source={1} {2}" -f $stamp, $Source, $Detail
        $audit = Join-Path $LogDir ("panic-" + (Get-Date -Format "yyyyMMdd") + ".jsonl")
        $rec = @{ at = $stamp; source = $Source; detail = $Detail } | ConvertTo-Json -Compress
        [System.IO.File]::AppendAllText($audit, $rec + "`n", $Utf8NoBom)
        $wdlog = Join-Path $LogDir ("watchdog-" + (Get-Date -Format "yyyyMMdd") + ".log")
        [System.IO.File]::AppendAllText($wdlog, $line + "`n", $Utf8NoBom)
        Write-Host $line -ForegroundColor Red
    } catch {
        Write-Host ("[PANIC] 日志写入失败: " + $_) -ForegroundColor Yellow
    }
}

function Start-Watcher {
    # 启动/复用 watcher（on 与 start 共用）。成功返回 @{pid; cmd; source}，失败返回 $null
    $running = Get-WatcherProcess
    if ($running) {
        if ($running.source -eq "scan") { Write-Lock -ProcId $running.pid }
        Write-Host ("[上岗] 看门狗已在运行 pid={0}（命令行已校验含 watcher.py），不重复启动。" -f $running.pid)
        return $running
    }
    Clear-Lock
    $pythonw = Join-Path (Split-Path -Parent $Python) "pythonw.exe"
    if (-not (Test-Path $pythonw)) { $pythonw = $Python }
    Start-Process -FilePath $pythonw -ArgumentList "`"$WatcherPy`"" -WorkingDirectory $Base -WindowStyle Hidden
    # 等 2~3 秒回读 watcher.lock 确认进程真的起来了
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 100
        $pidNum = Get-WatcherPidFromLock
        if ($pidNum -gt 0) {
            $info = Get-ProcInfo -ProcId $pidNum
            if ($info -and (Test-WatcherCmd -Cmd $info.cmd)) {
                return @{ pid = $pidNum; cmd = $info.cmd; source = "lock" }
            }
        }
    }
    $running = Get-WatcherProcess
    if ($running) {
        if ($running.source -eq "scan") { Write-Lock -ProcId $running.pid }
        return $running
    }
    return $null
}

function Stop-Watcher {
    # 真停车：只杀「命令行含 watcher.py」的进程；kill 前校验 pid 命令行，防 pid 复用误杀
    $lockPid = Get-WatcherPidFromLock
    $killed = @()
    if ($lockPid -gt 0) {
        $info = Get-ProcInfo -ProcId $lockPid
        if ($info -and (Test-WatcherCmd -Cmd $info.cmd)) {
            try { Stop-Process -Id $lockPid -Force -ErrorAction Stop; $killed += $lockPid } catch { }
            for ($i = 0; $i -lt 25; $i++) {
                if (-not (Test-PidAlive -ProcId $lockPid)) { break }
                Start-Sleep -Milliseconds 100
            }
        } else {
            Write-Host ("[保护] watcher.lock 里的 pid={0} 命令行不含 watcher.py（疑似 pid 复用/残留）→ 不杀它，只清 lock。" -f $lockPid) -ForegroundColor Yellow
        }
    }
    # lock 不可靠时兜底：扫描真实 watcher 进程并停掉（仍然只认命令行含 watcher.py 的进程）
    $found = Get-WatcherProcess
    if ($found -and (@($killed) -notcontains $found.pid)) {
        try { Stop-Process -Id $found.pid -Force -ErrorAction Stop; $killed += $found.pid } catch { }
        for ($i = 0; $i -lt 25; $i++) {
            if (-not (Test-PidAlive -ProcId $found.pid)) { break }
            Start-Sleep -Milliseconds 100
        }
    }
    Clear-Lock
    return @{ killed = $killed }
}

function Show-Status {
    $cfg = Read-Cfg
    $lockPid = Get-WatcherPidFromLock
    $running = Get-WatcherProcess
    $stopExists = Test-Path $StopFile
    Write-Host "===== 微信看门狗状态 ====="
    Write-Host ("目录          : {0}" -f $Base)
    Write-Host ("总开关 enabled: {0}" -f $(if ($cfg.enabled) { "true  (上岗)" } else { "false (停车)" }))
    Write-Host ("autoSend      : {0}" -f $(if ($cfg.autoSend) { "true  (允许自动发送)" } else { "false (只拟稿，绝不自动发)" }))
    $qh = "无（不静默）"
    if (@($cfg.quietHours).Count -gt 0) { $qh = (@($cfg.quietHours) -join " -> ") }
    Write-Host ("检测间隔      : {0} 秒   静默时段: {1}" -f $cfg.intervalSeconds, $qh)
    Write-Host ("STOP 急停文件 : {0}" -f $(if ($stopExists) { "存在（已急停）" } else { "不存在" }))
    if ($running) {
        Write-Host ("进程          : 运行中 pid={0}（命令行已校验含 watcher.py，来源={1}）" -f $running.pid, $running.source)
    } elseif ($lockPid -gt 0) {
        Write-Host ("进程          : 未运行（watcher.lock 残留 pid={0}，疑似僵尸/pid 复用）" -f $lockPid)
    } else {
        Write-Host "进程          : 未运行"
    }
    Write-Host "Agent 唤醒通道: disabled（方案A：进程内直接调 API 生成回复，不唤醒 Agent）"
    if ($running -and $cfg.enabled -and $cfg.autoSend -and (-not $stopExists)) {
        Write-Host ("[结论] 真上岗中 ✅  进程 pid={0} + enabled=true + autoSend=true + 无 STOP" -f $running.pid) -ForegroundColor Green
    } elseif ((-not $running) -and (-not $cfg.enabled)) {
        Write-Host "[结论] 真停车 ✅  进程未运行 + enabled=false" -ForegroundColor Green
    } else {
        if ($cfg.enabled -and (-not $running)) { Write-Host "[结论] ⚠ 开关开着但进程没跑 → 执行 .\toggle.ps1 on" -ForegroundColor Yellow }
        if ($running -and (-not $cfg.enabled)) { Write-Host ("[结论] ⚠ 进程在空转（pid={0} 但 enabled=false）→ 执行 .\toggle.ps1 off（真停车）" -f $running.pid) -ForegroundColor Yellow }
        if ($running -and $cfg.enabled -and (-not $cfg.autoSend)) { Write-Host "[结论] ⚠ 进程在跑、enabled=true 但 autoSend=false（只拟稿不发送）→ 执行 .\toggle.ps1 on" -ForegroundColor Yellow }
        if ($stopExists -and $running) { Write-Host "[结论] ⚠ STOP 存在但进程仍在跑 → 执行 .\toggle.ps1 panic（立即停）" -ForegroundColor Yellow }
    }
    $signal = Join-Path $Base "signal.json"
    if (Test-Path $signal) {
        try {
            $sig = Get-Content -Path $signal -Raw -Encoding UTF8 | ConvertFrom-Json
            Write-Host ("最近信号      : {0}  reason={1}" -f $sig.detectedAtLocal, $sig.reason)
        } catch { Write-Host "最近信号      : (解析失败)" }
    } else {
        Write-Host "最近信号      : 尚无"
    }
    if (Test-Path $LogDir) {
        $latest = Get-ChildItem -Path $LogDir -Filter "watchdog-*.log" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($latest) {
            Write-Host ("日志(最新)    : {0}" -f $latest.FullName)
            Get-Content -Path $latest.FullName -Tail 5 -Encoding UTF8 | ForEach-Object { Write-Host ("  | {0}" -f $_) }
        }
    }
    Write-Host "=========================="
}

switch ($Action.ToLowerInvariant()) {
    "on" {
        Set-CfgBool -Key "enabled" -Value $true | Out-Null
        Set-CfgBool -Key "autoSend" -Value $true | Out-Null
        if (Test-Path $StopFile) {
            Remove-Item -Path $StopFile -Force
            Write-Host "[on] 已清除 STOP 急停文件。"
        }
        $running = Start-Watcher
        Write-Host ""
        if ($running) {
            Write-Host ("[on] ✅ 已上岗：进程 pid={0}、enabled=true、autoSend=true、STOP 已清除" -f $running.pid) -ForegroundColor Green
            Write-Host ("[on] 日志: {0}" -f (Join-Path $LogDir ("watchdog-" + (Get-Date -Format "yyyyMMdd") + ".log")))
        } else {
            Write-Host "[on] ❌ 启动失败：配置已写 enabled=true/autoSend=true，但 3 秒内没等到 watcher 进程（watcher.lock 未出现）。" -ForegroundColor Red
            Write-Host "[on] 前台排障（直接跑会打印启动/退出原因）:" -ForegroundColor Yellow
            Write-Host ("     {0} `"{1}`"" -f $Python, $WatcherPy) -ForegroundColor Yellow
            Write-Host "[on] 常见原因：STOP 未删 / 微信窗口不可用 / Python 或依赖缺失 / config 解析失败（看 logs 目录）。" -ForegroundColor Yellow
            exit 1
        }
    }
    "off" {
        Set-CfgBool -Key "enabled" -Value $false | Out-Null
        Set-CfgBool -Key "autoSend" -Value $false | Out-Null
        $r = Stop-Watcher
        $killed = @($r.killed)
        if ($killed.Count -gt 0) {
            Write-Host ("[off] ✅ 已停车：已杀掉 watcher 进程 pid={0}、enabled=false、autoSend=false、watcher.lock 已清" -f ($killed -join ',')) -ForegroundColor Green
        } else {
            Write-Host "[off] ✅ 已停车：enabled=false、autoSend=false；当前没有运行中的 watcher 进程（watcher.lock 已清）" -ForegroundColor Green
        }
    }
    "stop" {
        $r = Stop-Watcher
        $killed = @($r.killed)
        if ($killed.Count -gt 0) {
            Write-Host ("[stop] ✅ 已停止 watcher 进程 pid={0}（只停进程，未改开关配置）" -f ($killed -join ',')) -ForegroundColor Green
        } else {
            Write-Host "[stop] 当前没有运行中的 watcher 进程（watcher.lock 已清，未改开关配置）"
        }
    }
    "panic" {
        $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        [System.IO.File]::WriteAllText($StopFile, "panic at $stamp`r`n", $Utf8NoBom)
        Write-Host ("[panic] 已写 STOP 急停文件（{0}）。" -f $stamp)
        $r = Stop-Watcher
        $killed = @($r.killed)
        if ($killed.Count -gt 0) {
            Write-Host ("[panic] ✅ 已立即杀掉 watcher 进程 pid={0}、watcher.lock 已清（不再等下一轮循环）" -f ($killed -join ',')) -ForegroundColor Green
        } else {
            Write-Host "[panic] 当前没有运行中的 watcher 进程；STOP 文件保留（下次启动会立即退出，直到执行 .\toggle.ps1 on）。"
        }
        $killedText = $(if ($killed.Count -gt 0) { ($killed -join ',') } else { "无(未在运行)" })
        Write-PanicLog -Source "shortcut-toggle.ps1" -Detail ("action=panic 已写 STOP 文件；杀掉 watcher pid=" + $killedText)
    }
    "start" {
        $running = Get-WatcherProcess
        if ($running) {
            Write-Host ("[start] 看门狗已在运行 pid={0}（命令行已校验），不重复启动。" -f $running.pid)
        } else {
            $cfg = Read-Cfg
            if (-not $cfg.enabled) { Write-Host "[start] 注意：enabled=false，进程启动后会休眠；要真上岗请用 .\toggle.ps1 on" -ForegroundColor Yellow }
            if (Test-Path $StopFile) { Write-Host "[start] 注意：STOP 文件存在，watchdog 启动后会立即退出；要真上岗请用 .\toggle.ps1 on" -ForegroundColor Yellow }
            $running = Start-Watcher
            if ($running) {
                Write-Host ("[start] ✅ 已后台启动 watcher（pid={0}，命令行已校验）" -f $running.pid) -ForegroundColor Green
                Write-Host ("[start] 日志: {0}" -f (Join-Path $LogDir ("watchdog-" + (Get-Date -Format "yyyyMMdd") + ".log")))
            } else {
                Write-Host "[start] ❌ 启动失败，请前台运行排障:" -ForegroundColor Red
                Write-Host ("     {0} `"{1}`"" -f $Python, $WatcherPy) -ForegroundColor Yellow
                exit 1
            }
        }
    }
    "status" {
        Show-Status
    }
    default {
        Write-Host "用法: .\toggle.ps1 [on|off|stop|panic|start|status]"
        Write-Host "  on      真上岗（enabled=true + autoSend=true + 删 STOP + 后台起 watcher 进程）"
        Write-Host "  off     真停车（enabled=false + autoSend=false + 杀掉 watcher 进程 + 清 lock）"
        Write-Host "  stop    只杀 watcher 进程（不动开关配置）"
        Write-Host "  panic   急停（写 STOP + 立即杀进程 + 清 lock）"
        Write-Host "  start   后台启动 watcher（与 on 同一段启动逻辑）"
        Write-Host "  status  查看状态（含命令行校验与一致性检查）"
        exit 1
    }
}