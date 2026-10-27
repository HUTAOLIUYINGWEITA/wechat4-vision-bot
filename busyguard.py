# -*- coding: utf-8 -*-
"""小耳朵「你忙就别动」忙闲守护 —— 只读判断，绝不注入任何键鼠事件。

动作（发送/唤醒）前调用 evaluate()：
  1) 前台窗口进程属于游戏（如 WorldOfTanks.exe / cs2.exe）  -> busy reason=foreground-game
  2) 处于 quietHours 静默时段                               -> busy reason=quiet-hours
  3) 最近 1 小时动作数超过 maxRepliesPerHour                -> busy reason=rate-limit

⛔ 2026-10-25 第四轮整改（用户实测「它一切到前台就秒退」）：
   - **foreground-wechat 已删除**（上一轮刚加就删：它就是「秒退」元凶）
   - **fullscreen 覆盖比判定已停用**（fullscreenMinCoverRatio / fullscreenAllowProcesses 不再生效）
   - **user-active 判定保持停用**（不回头）
   ⭐ 只保留「游戏进程硬拦截」这一条前台判断：用户明确要求「前台看着是什么都不拦」。
      红线不变：游戏在前台绝不注入任何键鼠。

本次事故根因：游戏前台时上一个 reply.py 仍真实点击/注入键盘抢走左键。
本模块只做只读查询（GetForegroundWindow / GetLastInputInfo / 窗口矩形），
不点击、不按键、不切前台、不移动光标。

命令行自检: E:\\anaconda\\python.exe busyguard.py
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import time

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MONITOR_DEFAULTTONEAREST = 2
SW_RESTORE = 9

# busyGuard 默认值（config.json 的 "busyGuard" 会覆盖）
DEFAULT_BUSY_GUARD = {
    "enabled": True,
    # 游戏进程名（2026-10-26 修：**只按进程名匹配，窗口标题绝不参与**；
    # 含 .exe 的项 = exe 文件名精确匹配，不含 .exe 的项 = 进程名前缀匹配）
    "gameProcesses": [
        "WorldOfTanks.exe", "WorldOfTanks", "world_of_tanks.exe",
        "WoT.exe", "wot.exe",
        "Tanks.exe", "Tank.exe",
    ],
    # 全屏也放行的进程（浏览器 / 资源管理器；游戏绝不在此列）
    # ⛔ 已停用（2026-10-25 第四轮整改）：fullscreen 覆盖比判定不再生效，保留字段仅为兼容旧 config.json
    "fullscreenAllowProcesses": [
        "explorer.exe",
        "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe",
        "opera.exe", "vivaldi.exe", "iexplore.exe",
        "360se.exe", "360chrome.exe", "qqbrowser.exe",
    ],
    # ⛔ 已停用（2026-10-25）：保留字段仅为兼容旧 config.json，不再生效
    "userActiveSeconds": 60,
    # ⛔ 已停用（2026-10-25 第四轮整改）：全屏覆盖比不再参与判定
    "fullscreenMinCoverRatio": 0.92,
}


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG),
                ("right", wt.LONG), ("bottom", wt.LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", wt.DWORD)]


# ───────────────────────── 低层查询（只读） ─────────────────────────


def last_input_tick():
    """最近一次键鼠输入的 GetTickCount 时刻；失败返回 None。"""
    li = LASTINPUTINFO()
    li.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not _user32.GetLastInputInfo(ctypes.byref(li)):
        return None
    return int(li.dwTime) & 0xFFFFFFFF


def tick_now():
    return int(_kernel32.GetTickCount()) & 0xFFFFFFFF


def tick_delta_ms(later, earlier):
    """32 位 tick 回绕安全的差值（毫秒）。"""
    return (int(later) - int(earlier)) & 0xFFFFFFFF


def last_input_age_seconds():
    """最近一次键鼠输入距今多少秒（>=0；失败返回 0 = 视为用户在用）。"""
    t = last_input_tick()
    if t is None:
        return 0.0
    return tick_delta_ms(tick_now(), t) / 1000.0


def _process_image_name(pid):
    """取进程可执行文件全路径（只读；失败返回空串）。"""
    h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        ok = _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        return buf.value if ok else ""
    finally:
        _kernel32.CloseHandle(h)


def window_rect(hwnd):
    r = RECT()
    try:
        _user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(r))
    except Exception:
        return (0, 0, 0, 0)
    return (int(r.left), int(r.top), int(r.right), int(r.bottom))


def monitor_rect(hwnd):
    """窗口所在显示器的完整矩形 (l,t,r,b)；失败返回 None。"""
    try:
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        mon = _user32.MonitorFromWindow(wt.HWND(hwnd), MONITOR_DEFAULTTONEAREST)
        if not mon or not _user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return None
        r = mi.rcMonitor
        return (int(r.left), int(r.top), int(r.right), int(r.bottom))
    except Exception:
        return None


def foreground_window_info():
    """前台窗口信息（只读）。"""
    hwnd = int(_user32.GetForegroundWindow() or 0)
    if not hwnd:
        return {"hwnd": 0, "title": "", "class": "", "pid": 0, "exe": "",
                "exe_base": "", "rect": (0, 0, 0, 0), "monitor": None}
    buf = ctypes.create_unicode_buffer(512)
    _user32.GetWindowTextW(wt.HWND(hwnd), buf, 512)
    cls = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(wt.HWND(hwnd), cls, 256)
    pid = wt.DWORD(0)
    _user32.GetWindowThreadProcessId(wt.HWND(hwnd), ctypes.byref(pid))
    exe = _process_image_name(pid.value)
    return {
        "hwnd": hwnd,
        "title": buf.value,
        "class": cls.value,
        "pid": int(pid.value),
        "exe": exe,
        "exe_base": os.path.basename(exe).lower(),
        "rect": window_rect(hwnd),
        "monitor": monitor_rect(hwnd),
    }


def cursor_pos():
    p = wt.POINT()
    if _user32.GetCursorPos(ctypes.byref(p)):
        return (int(p.x), int(p.y))
    return None


# ───────────────────────── 各条判断 ─────────────────────────


def _match_game(cfg, fg):
    """前台进程是否命中游戏名单（**只看进程名，绝不匹配窗口标题**）。

    2026-10-26 修（真 bug）：旧版把 `fg["title"]` 也拿去撞名单，导致「Edge 里看坦克世界视频」
    被判成 foreground-game（日志：exe='msedge.exe'、title='【坦克世界】…' 命中裸中文词 '坦克世界'），
    一条都不回。现在：
      * 名单项带 .exe -> 与前台「进程 exe 文件名」精确匹配（不区分大小写）；
      * 名单项不带 .exe -> 只按「进程名前缀」匹配（如 WorldOfTanks -> worldoftanks.exe）；
      * **窗口标题永远不参与判定**（标题是用户内容：浏览器/播放器/编辑器里都可能出现游戏名）。
    返回 (命中的名单项, 匹配字段, 匹配值)；未命中返回 ("", "", "")。
    """
    names = [str(x).strip().lower() for x in (cfg.get("gameProcesses") or []) if str(x).strip()]
    exe_base = str(fg.get("exe_base") or "").strip().lower()
    exe_path = str(fg.get("exe") or "").strip().lower()
    proc = os.path.basename(exe_base) if exe_base else os.path.basename(exe_path)
    if not proc:
        return "", "", ""
    for n in names:
        if n.endswith(".exe"):
            if proc == n:
                return n, "进程名(exe 文件名精确匹配)", proc
        else:
            if proc.startswith(n):
                return n, "进程名前缀匹配", proc
    return "", "", ""


def _fullscreen_coverage(fg):
    """窗口覆盖显示器面积比例；无显示器信息返回 0。"""
    mon = fg.get("monitor")
    if not mon:
        return 0.0
    ml, mt, mr, mb = mon
    ma = float(max(1, (mr - ml) * (mb - mt)))
    l, t, r, b = fg.get("rect") or (0, 0, 0, 0)
    # 取窗口与显示器的交集面积
    ix0, iy0 = max(l, ml), max(t, mt)
    ix1, iy1 = min(r, mr), min(b, mb)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    return float((ix1 - ix0) * (iy1 - iy0)) / ma


def _is_fullscreen_blocked(cfg, fg):
    ratio = _fullscreen_coverage(fg)
    if ratio < float(cfg.get("fullscreenMinCoverRatio") or 0.92):
        return False, ratio
    allow = [str(x).lower() for x in (cfg.get("fullscreenAllowProcesses") or [])]
    if (fg.get("exe_base") or "") in allow:
        return False, ratio
    return True, ratio


def _in_quiet_hours(quiet_hours, now=None):
    if not (isinstance(quiet_hours, (list, tuple)) and len(quiet_hours) == 2):
        return False
    try:
        start, end = int(quiet_hours[0]), int(quiet_hours[1])
    except Exception:
        return False
    hour = (now or time.localtime()).tm_hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _recent_action_count(wake_times, seconds=3600):
    cutoff = time.time() - seconds
    return sum(1 for t in (wake_times or []) if isinstance(t, (int, float)) and t >= cutoff)


def merge_config(config):
    """把 config.json 的 busyGuard 与默认值合并。"""
    cfg = dict(DEFAULT_BUSY_GUARD)
    raw = (config or {}).get("busyGuard") or {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if v is not None:
                cfg[k] = v
    return cfg


def _busy(reason, detail, checks):
    return {"busy": True, "reason": reason, "detail": detail, "checks": checks}


# ───────────────────────── 对外主入口 ─────────────────────────


def evaluate(config, wake_times=None, now=None, ignore_user_active=False):
    """动作前判断：任一命中 -> {"busy": True, "reason": ..., "detail": ...}。

    只读，不注入任何输入。

    ⭐ 2026-10-25 第四轮整改后只剩 3 条：
       ① foreground-game（游戏进程硬拦截，红线）
       ② quiet-hours（静默时段）
       ③ rate-limit（每小时上限）
    user-active / foreground-wechat / foreground-fullscreen 全部停用（参数 ignore_user_active 保留兼容）。
    """
    cfg = merge_config(config)
    checks = {"busyGuard": dict((k, v) for k, v in cfg.items() if k != "fullscreenAllowProcesses"),
              "userActiveDisabled": True,
              "foregroundWechatDisabled": True,
              "foregroundFullscreenDisabled": True,
              "note": ("busyGuard: user-active / foreground-wechat / foreground-fullscreen 判定已停用；"
                       "只保留 foreground-game 硬拦截 + quiet-hours + rate-limit")}
    if not cfg.get("enabled", True):
        checks["disabled"] = True
        return {"busy": False, "reason": "", "detail": "busyGuard.enabled=false", "checks": checks}

    fg = foreground_window_info()
    checks["foreground"] = fg
    # idle 秒数 / 全屏覆盖比只做诊断记录，不再参与 busy 判定
    checks["idleSeconds"] = round(last_input_age_seconds(), 3)
    checks["fullscreenCoverRatio"] = round(_fullscreen_coverage(fg), 3)

    # 1) 游戏前台（唯一保留的前台硬拦截，红线：游戏在前台绝不注入任何键鼠）
    #    2026-10-26：只按进程名匹配，窗口标题不参与（修 Edge 看坦克世界视频被误杀）
    game, game_field, game_value = _match_game(cfg, fg)
    if game:
        return _busy("foreground-game",
                     "前台进程=%r；命中游戏名单 %r（匹配字段=%s，匹配值=%r；窗口标题=%r 不参与判定）"
                     "；游戏在前台绝不注入任何键鼠" % (
                         fg.get("exe_base") or "?", game, game_field, game_value,
                         (fg.get("title") or "")[:40]),
                     checks)

    # 2) 静默时段
    quiet = (config or {}).get("quietHours")
    if _in_quiet_hours(quiet, now):
        return _busy("quiet-hours", "当前处于静默时段 quietHours=%r" % (quiet,), checks)

    # 3) 频率限制
    limit = int((config or {}).get("maxRepliesPerHour") or 0)
    used = _recent_action_count(wake_times)
    checks["actionsLastHour"] = used
    if limit > 0 and used >= limit:
        return _busy("rate-limit", "最近 1 小时已动作 %d 次 >= maxRepliesPerHour=%d" % (used, limit), checks)

    return {"busy": False, "reason": "", "detail": "", "checks": checks}


def foreground_blocked(config):
    """发送过程中用：**只查「前台是否是游戏进程」**（唯一保留的前台硬拦截）。

    ⚠ 2026-10-25 第四轮整改：fullscreen / foreground-wechat / user-active 全部停用。
       reply.py 自己要操作微信，必然会切前台；任何「前台类」判断都会把自己挡住。
       这里只剩游戏硬拦截（红线：游戏在前台绝不注入任何键鼠）。

    返回 (blocked: bool, reason: str, detail: str)。
    """
    cfg = merge_config(config)
    if not cfg.get("enabled", True):
        return False, "", ""
    fg = foreground_window_info()
    game, game_field, game_value = _match_game(cfg, fg)
    if game:
        return True, "foreground-game", (
            "前台进程=%r 命中游戏名单 %r（匹配字段=%s，匹配值=%r；窗口标题不参与判定）" % (
                fg.get("exe_base") or "?", game, game_field, game_value))
    return False, "", ""


def describe(config=None):
    """给状态页/自检用：打印当前快照（只读）。"""
    cfg = merge_config(config or {})
    fg = foreground_window_info()
    return {
        "busyGuard": cfg,
        "userActiveDisabled": True,
        "foregroundWechatDisabled": True,
        "foregroundFullscreenDisabled": True,
        "note": ("busyGuard: user-active / foreground-wechat / foreground-fullscreen 判定已停用；"
                 "只保留 foreground-game 硬拦截 + quiet-hours + rate-limit"),
        "foreground": fg,
        "idleSeconds": round(last_input_age_seconds(), 3),
        "cursor": cursor_pos(),
        "fullscreenCoverRatio": round(_fullscreen_coverage(fg), 3),
        "verdict": evaluate(config or {}),
    }


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(json.dumps(describe(), ensure_ascii=False, indent=2, default=str))
