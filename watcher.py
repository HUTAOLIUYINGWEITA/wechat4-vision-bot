# -*- coding: utf-8 -*-
"""微信消息看门狗 (WeChat -> 小耳朵自动回复) —— 方案A：进程内直连官方 API

⭐ 新流程（不再唤醒 Agent：无 RPC / 无 session/prompt / 无 wake 通道）:
    检测白名单未读（纯本地像素，0 token）
    -> 截图（已有）+ 可选 OCR 出对方最近消息文本
    -> 进程内 HTTPS 调 https://api.deepseek.com/chat/completions（genreply.py）
    -> 发送护栏（sendguard.py：白名单 / 30s 间隔 / 连续 5 条停 / 不重复 / 落 sent-*.jsonl）
    -> 调现成的 reply.py 真发送（复用，不改它的发送逻辑）

检测（0 token，纯本地像素分析）:
  L1 窗口层：PrintWindow 抓微信窗口（被遮挡也能抓，不抢焦点）
             -> 会话列表「未读红点」检测 + 列表文本区稳定基线差分（新会话置顶/时间戳变化）
  L2 任务栏层：PrintWindow 抓任务栏 -> 绿色微信图标定位 -> 图标角标红点（窗口最小化时兜底）
命中后保存截图 + 写 signal.json + 生成回复 + 护栏 + reply.py 发送

安全边界:
  * enabled=false 时完全不动作（连截图都不做）
  * autoSend=false 时只生成草稿，绝不发送
  * apiKey 只从 .credentials.json 读，日志/异常里一律 sk-***（红线）
  * API 失败 -> 记 "[GEN] ERROR ..."，不发送，保留待处理可重试
  * STOP 文件存在 -> 立即退出（toggle.ps1 panic）
  * 日志写入 logs/，含判定原因、生成请求/回复原文、发送结果；发送明细写 logs/sent-YYYYMMDD.jsonl
  * config.wake.enabled=false（默认）：唤醒 Agent 这条链不在流程里（代码保留但不调用）

依赖：系统 Python 已有 numpy / Pillow / opencv / pywin32 / PyYAML（不安装任何包，HTTP 用 urllib）
用法: python watcher.py [--once] [--selftest] [--status]
      python watcher.py [--test-gen "会话" "对方消息"]   # 只测生成，不发
      python watcher.py [--inject-unread --chat "文件传输助手" --incoming "..."]
                                                                          # 造一条白名单未读（测试钩子）
      python watcher.py [--guard-selftest]               # 发送护栏自检
"""

import ctypes
import ctypes.wintypes as wt
import base64
import difflib
import hashlib
import hmac
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

try:
    from PIL import Image, ImageGrab
except Exception as exc:  # pragma: no cover
    raise SystemExit("需要 Pillow: %s" % exc)

try:
    import win32gui
    import win32ui
    import win32con
    import win32process
except Exception as exc:  # pragma: no cover
    raise SystemExit("需要 pywin32: %s" % exc)

# 「你忙就别动」忙闲守护（只读判断：前台游戏/全屏/用户活跃/静默/限速）
try:
    import busyguard
except Exception:  # pragma: no cover
    busyguard = None

# 方案A：进程内直接调官方 API 生成回复 + 发送护栏（都不依赖第三方包）
try:
    import genreply
except Exception as exc:  # pragma: no cover
    raise SystemExit("需要 genreply.py（进程内调 API 生成回复）: %s" % exc)

try:
    import sendguard
except Exception as exc:  # pragma: no cover
    raise SystemExit("需要 sendguard.py（发送护栏）: %s" % exc)

# ─────────────────────────── paths / constants ───────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STOP_PATH = os.path.join(BASE_DIR, "STOP")
LOG_DIR = os.path.join(BASE_DIR, "logs")
SHOT_DIR = os.path.join(BASE_DIR, "screenshots")
SIGNAL_PATH = os.path.join(BASE_DIR, "signal.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LOCK_PATH = os.path.join(BASE_DIR, "watcher.lock")
# reply.py 真正发送成功后才会追加 sent 记录；watcher 靠它确认「真发出去了」
REPLY_STATE_PATH = os.path.join(BASE_DIR, "reply_state.json")
# 方案A：发送护栏状态 / 人设 / 预生成凭据 / 测试注入文件 / reply.py 路径
GUARD_STATE_PATH = os.path.join(BASE_DIR, "guard_state.json")
PERSONA_PATH = os.path.join(BASE_DIR, "persona.txt")
CREDENTIALS_PATH = os.path.join(BASE_DIR, ".credentials.json")
REPLY_PY_PATH = os.path.join(BASE_DIR, "reply.py")
SEND_PY_PATH = os.path.join(BASE_DIR, "sendfile.py")
INJECT_PATH = os.path.join(BASE_DIR, "tmp-inject-unread.json")
# ── 熔断 / 急停（2026-10-27 第一阶段）──
#   熔断状态：{consecutiveFailures,lastFailureAt,lastReason}（发送成功一次即清零）
BREAKER_STATE_PATH = os.path.join(BASE_DIR, "breaker_state.json")
#   熔断记录：达标触发时写（含触发时间与原因），熔断后不自启（靠 STOP + 人工 toggle.ps1 on）
BREAKER_TRIPPED_PATH = os.path.join(BASE_DIR, "breaker_tripped.json")
#   全局急停热键：Ctrl + Alt + Shift + S（四键同按，GetAsyncKeyState 每轮检测）
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_SHIFT = 0x10
VK_S = 0x53
PANIC_HOTKEY_LABEL = "Ctrl+Alt+Shift+S"

WECHAT_CLASS = "Qt51514QWindowIcon"
WECHAT_TITLE = "微信"
VERSION = "2.0.0"

DEFAULT_CONFIG = {
    # 公开版安全默认：没有 config.json 时不自动上岗（要真上岗必须显式 enabled=true）
    "enabled": False,
    "autoSend": False,
    "intervalSeconds": 3,
    "quietHours": [23, 8],
    "whitelist": [],
    "blacklist": [],
    "maxRepliesPerHour": 30,
    "sound": True,
    "notifyExistingOnStart": False,
    # 连着两条消息时，第二条不该被长冷却吞掉（2026-10-25 第四次整改：30 -> 10）
    "triggerCooldownSeconds": 10,
    # 失败（busy / 冷却 / wake 失败 / 等不到发送确认）后多久重试一次；同一条未读最多重试 3 次
    "retryAfterSeconds": 60,
    "maxRetryAttempts": 3,
    # wake 成功后，等 reply.py 把成功记录写进 reply_state.json 的最长时间。
    # ⚠ 只有真发出去（reply_state.json 新增 sent 记录）才写 processed_fingerprint
    "replyConfirmTimeoutSeconds": 180,
    # 「你忙就别动」忙闲守护（2026-10-25 第四轮整改）
    #   ⭐ 只剩游戏进程硬拦截 foreground-game（红线：游戏前台绝不注入键鼠）
    #   ② quiet-hours / ③ rate-limit 保持
    #   user-active / foreground-wechat / foreground-fullscreen 全部停用
    "busyGuard": {
        "enabled": True,
        "gameProcesses": [
            # 2026-10-26：只按进程名匹配，窗口标题不参与（删掉裸中文词「坦克世界」，
            # 否则 Edge 里看坦克世界视频会被误判成正在打游戏）
            "WorldOfTanks.exe", "WorldOfTanks", "WoT.exe", "wot.exe",
            "Tanks.exe", "Tank.exe",
            "cs2.exe", "csgo.exe",
        ],
        "fullscreenAllowProcesses": [
            "explorer.exe", "chrome.exe", "msedge.exe", "firefox.exe",
            "brave.exe", "opera.exe", "vivaldi.exe", "iexplore.exe",
            "360se.exe", "360chrome.exe", "qqbrowser.exe",
        ],
        # ⛔ 已停用（2026-10-25 第四轮整改）：保留字段仅为兼容旧 config.json，不再生效
        "userActiveSeconds": 60,
        "fullscreenMinCoverRatio": 0.92,
    },
    "dedupe": {"settleScans": 2},
    "ocr": {
        "enabled": True,
        "tesseract": "",
        "lang": "chi_sim",
        "timeoutMs": 10000,
    },
    "whitelistMatch": {
        "coverageThreshold": 0.70,
        "coverageThresholdLoose": 0.55,
        "looseMinMatched": 6,
    },
    "detect": {
        "windowClass": WECHAT_CLASS,
        "capture": "auto",
        "rowDiffThreshold": 0.03,
        "minBadgeArea": 40,
        "taskbarEnabled": True,
    },
    # ── 方案A：进程内直接调官方 API 生成回复（不唤醒 Agent）──
    #   凭据读 .credentials.json（provider / baseUrl / model / apiKey），apiKey 绝不进日志
    "gen": {
        "enabled": True,
        "credentials": ".credentials.json",
        "personaFile": "persona.txt",
        "maxTokens": 120,          # 硬上限 120（代码里再 clamp 一次）
        "temperature": 0.8,
        "timeoutSeconds": 25,
        "maxReplyChars": 80,
        "ocrPreview": True,        # 可选：OCR 出会话行里的最近消息文本喂给模型
        "sendTimeoutSeconds": 120, # 等 reply.py 真发送的上限
        "forbid": [
            "转账", "汇款", "付款", "红包", "密码", "验证码", "银行卡",
            "http://", "https://", "www.", "二维码", "扫码", "点击链接", "点链接",
            "sk-", "密钥", "api key", "apikey", "发文件", "附件给你",
        ],
        # 仅测试用：tmp-inject-unread.json 可以造一条白名单未读（端到端自测，不发真实消息）
        "testHooks": True,
    },
    # ── 发送护栏（用户明确要求：只限制「别乱发消息」）──
    "guard": {
        "enabled": True,
        "whitelistOnly": True,              # ① 白名单外绝不发
        "perChatMinIntervalSeconds": 30,    # ② 同一会话 30 秒内只发 1 条
        "perChatMaxBurst": 5,               # ③ 同一会话连续 <=5 条 -> 停了等用户发话
        "burstPauseSeconds": 1800,          #    连续用满后暂停多久
        "burstResetSeconds": 600,           #    和上一条隔这么久算新一段对话（连续计数清零）
        "dedupeSameText": True,             # ④ 同一句话不重复发
        "dedupeWindowSeconds": 600,         #    去重窗口
        "sentLogEnabled": True,             # ⑤ 每条落 logs/sent-YYYYMMDD.jsonl
    },
    # ── 熔断（2026-10-27 第一阶段，用户拍板：连续失败 3 次自动急停，不再往微信发告警）──
    #   达标 -> 写 breaker_tripped.json -> 写 STOP -> 结束 watcher 自身进程 -> Windows 气泡通知
    #   成功发送一次即清零；熔断后不自启（STOP 存在，必须人工 toggle.ps1 on）
    "breaker": {
        "enabled": True,
        "consecutiveFailures": 3,   # 连续失败阈值
        "resetOnSuccess": True,     # 成功发送一次即清零
    },
    # ── 急停（2026-10-27 第一阶段，三处入口）──
    #   ① 桌面快捷方式 小耳朵急停.lnk -> toggle.ps1 panic（写 STOP + 立即杀进程，由 toggle.ps1 记 [PANIC] 日志）
    #   ② 全局热键 Ctrl+Alt+Shift+S（watcher 每轮 GetAsyncKeyState 检测）
    #   ③ 微信口令：白名单来信同时含「小耳朵」+「急停/停止」-> 立即写 STOP + 退出
    "panic": {
        "enabled": True,
        "hotkey": "ctrl+alt+shift+s",
        "wechatKeywords": ["急停", "停止"],
        "toast": True,              # 急停时弹 Windows 气泡（写 STOP 后进程就退，气泡由子进程弹）
    },
    # ⚠ 唤醒 Agent 这条链已停用（config.wake.enabled=false）：代码保留，默认不调用
    "wake": {
        "enabled": False,
        "channel": "off",
        "baseUrl": "http://127.0.0.1:19387",
        "dshHome": "",
        "sessionId": "",
        "sessionTitle": "微信看门狗",
        "cwd": BASE_DIR,
        "promptTemplate": (
            "【微信看门狗】{time} 检测到微信新消息提醒（判定依据：{reason}；有效未读会话 {badges} 个）。\n"
            "截图：{screenshot}\n"
            "信号：{signal}\n\n"
            "安全策略（来自 config.json）：autoSend={autoSend}，白名单={whitelist}，黑名单={blacklist}，静默时段={quietHours}。\n"
            "规则：\n"
            "1) 只有白名单命中的会话才允许回复；黑名单/未命中会话只记录。\n"
            "2) autoSend=true 时，请用下面这条命令**实际发送**回复（脚本会自动找会话、输入、发送，\n"
            "   并保存发送前后截图和 OCR 校验结果；只用一次，不要重复发）：\n"
            "   python \"reply.py\" --chat \"<命中的会话显示名>\" --text \"<回复内容>\"\n"
            "   不要点「标记已读」、不要删除/清理任何消息。\n"
            "3) autoSend=false：只拟回复草稿，等用户确认，绝不自己发送。\n"
            "输出：① 谁发的 ② 要点 ③ 是否已发送（贴 reply.py 打印的截图路径与 OCR 校验结果）。"
        ),
    },
    # ── 远程指令（微信 → Agent，2026-10-26 两级密码 + 挑战-应答）──
    #    来信含触发词「小耳朵」+ 正确密码（L1/L2）才会转告本机 Agent；
    #    只存 sha256(salt+password)，config/日志/审计里不出现明文密码。
    "remote": {
        # 🔴 公开版安全默认：远程指令（微信消息 -> 操作电脑）默认关闭。
        #    开启前必须先用 tools/setup_password.py 生成两级密码，并只把可信会话放进白名单。
        "enabled": False,
        "triggerWord": "小耳朵",
        "levels": {},                    # config.json 里填 {L1:{salt,sha256,label,allow}, L2:{...}}
        "pendingTtlSeconds": 180,
        "maxWrongAttempts": 3,
        "lockoutSeconds": 900,
        "targetSessionId": "",
        "targetSessionTitle": "",
        # 2026-10-26 23:4x 用户要求：**默认不再发微信告警**（空字符串 = 只写本地日志，不发微信）。
        # 以后想开微信提醒再填会话名（同样受 notifyCooldownSeconds 限流）。
        "fallbackNotifyChat": "",
        # 可选 Windows 气泡通知（默认关闭；开启后失败时弹系统通知，不占微信）
        "notifyToast": False,
        # 同一 chat+reason 的提醒冷却（秒）：冷却内只写日志、不再提醒
        "notifyCooldownSeconds": 600,
        "auditFile": "logs/remote-YYYYMMDD.jsonl",
    },
}

# ─────────────────────────── logging ───────────────────────────


def _now():
    return datetime.now()


def _ts():
    return _now().strftime("%Y-%m-%d %H:%M:%S")


class Logger:
    def __init__(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        self.path = os.path.join(LOG_DIR, "watchdog-%s.log" % _now().strftime("%Y%m%d"))

    def write(self, level, msg):
        # 2026-10-26 需求：logs/watchdog-*.log 里凡打印来信原文处，密码形态一律打 ******
        try:
            msg = mask_sensitive(msg)
        except Exception:
            pass
        line = "%s [%s] %s" % (_ts(), level, msg)
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        print(line, flush=True)

    def info(self, msg):
        self.write("INFO", msg)

    def warn(self, msg):
        self.write("WARN", msg)

    def error(self, msg):
        self.write("ERROR", msg)


LOG = None


def log_info(msg):
    (LOG or _console_logger()).info(msg)


def log_warn(msg):
    (LOG or _console_logger()).warn(msg)


def log_error(msg):
    (LOG or _console_logger()).error(msg)


_CONSOLE = None


def _console_logger():
    global _CONSOLE
    if _CONSOLE is None:
        _CONSOLE = Logger()
    return _CONSOLE


# ─────────────────────────── config / state ───────────────────────────


def _merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
        return _merge(DEFAULT_CONFIG, raw)
    except FileNotFoundError:
        return _merge(DEFAULT_CONFIG, {})
    except Exception as exc:
        log_warn("config.json 读取失败（用默认配置）: %s" % exc)
        return _merge(DEFAULT_CONFIG, {})


def save_config(cfg):
    with open(CONFIG_PATH + ".tmp", "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(CONFIG_PATH + ".tmp", CONFIG_PATH)


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception as exc:
        log_warn("state.json 写入失败: %s" % exc)


# ───────────────────── 熔断 / 急停（2026-10-27 第一阶段） ─────────────────────
# 日志口径：三处急停一律 `[PANIC] source=…`；熔断是 `[PANIC] breaker …`。
# 审计文件：logs/panic-YYYYMMDD.jsonl（每行一条：at/source/detail），不写任何密码/密钥。
def append_panic_log(source, detail="", extra=None):
    """急停/熔断统一审计：logs/panic-YYYYMMDD.jsonl 追加一条，返回记录 dict。"""
    rec = {"at": _ts(), "ts": round(time.time(), 3),
           "source": str(source or "?"), "detail": str(detail or "")[:500]}
    if isinstance(extra, dict):
        for k, v in extra.items():
            if k not in rec:
                rec[k] = v
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, "panic-%s.jsonl" % _now().strftime("%Y%m%d"))
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as exc:
        log_warn("[PANIC] 急停审计写入失败（忽略）: %s" % exc)
    return rec


def write_stop_file(reason):
    """写 STOP 急停文件（UTF-8 无 BOM；toggle.ps1 panic 也是同一个文件）。返回是否成功。"""
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        with open(STOP_PATH, "w", encoding="utf-8", newline="\n") as f:
            f.write("panic at %s reason=%s\n" % (_ts(), str(reason or "?")[:300]))
        return True
    except Exception as exc:
        log_error("[PANIC] 写 STOP 文件失败: %s" % exc)
        return False


def breaker_state_load():
    """读熔断状态（缺省/损坏时返回全零），字段：consecutiveFailures / lastFailureAt / lastReason。"""
    st = {}
    try:
        with open(BREAKER_STATE_PATH, "r", encoding="utf-8-sig") as f:
            st = json.load(f)
        if not isinstance(st, dict):
            st = {}
    except Exception:
        st = {}
    st.setdefault("consecutiveFailures", 0)
    st.setdefault("lastFailureAt", "")
    st.setdefault("lastReason", "")
    return st


def breaker_state_save(state):
    try:
        with open(BREAKER_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except Exception as exc:
        log_warn("[BREAKER] 状态写入失败（忽略）: %s" % exc)


# ─────────────────────────── DPI / win32 helpers ───────────────────────────
_user32 = ctypes.windll.user32


def set_dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor v2
    except Exception:
        try:
            _user32.SetProcessDPIAware()
        except Exception:
            pass


def dpi_scale_of(hwnd):
    try:
        dpi = _user32.GetDpiForWindow(hwnd)
        if dpi:
            return dpi / 96.0
    except Exception:
        pass
    try:
        return ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100.0
    except Exception:
        return 1.0


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WECHAT_EXE_NAMES = ("weixin.exe", "wechat.exe")
_kernel32 = ctypes.windll.kernel32


def _process_image_name(pid):
    """取进程可执行文件全路径（失败返回空串）。"""
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


def _is_cloaked(hwnd):
    """DWM cloak 状态（Windows 虚拟桌面/挂起时窗口会被 cloak，视觉上不可见）。"""
    try:
        dwmapi = ctypes.windll.dwmapi
        val = ctypes.c_int(0)
        hr = dwmapi.DwmGetWindowAttribute(wt.HWND(hwnd), ctypes.c_uint(14),  # DWMWA_CLOAKED
                                          ctypes.byref(val), ctypes.sizeof(val))
        return hr == 0 and val.value != 0
    except Exception:
        return False


def enumerate_weixin_windows():
    """枚举所有属于 Weixin.exe 的顶层窗口（含隐藏 / 最小化 / 托盘）。"""
    out = []
    exe_cache = {}

    def cb(h, _):
        try:
            _tid, pid = win32process.GetWindowThreadProcessId(h)
            if pid not in exe_cache:
                exe_cache[pid] = _process_image_name(pid)
            exe = exe_cache[pid] or ""
            if os.path.basename(exe).lower() not in WECHAT_EXE_NAMES:
                return True
            l, t, r, b = win32gui.GetWindowRect(h)
            out.append({
                "hwnd": h,
                "pid": pid,
                "exe": exe,
                "class": win32gui.GetClassName(h),
                "title": win32gui.GetWindowText(h),
                "visible": bool(_user32.IsWindowVisible(h)),
                "iconic": bool(_user32.IsIconic(h)),
                "cloaked": _is_cloaked(h),
                "rect": (l, t, r, b),
                "size": (max(0, r - l), max(0, b - t)),
            })
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return out


def describe_window_candidates(cands):
    if not cands:
        return "（没有枚举到任何 Weixin.exe 顶层窗口）"
    parts = []
    for c in cands:
        parts.append("hwnd=%s class=%r title=%r visible=%s iconic=%s cloaked=%s size=%s" % (
            hex(c["hwnd"]), c["class"], c["title"], c["visible"], c["iconic"],
            c["cloaked"], c["size"]))
    return " | ".join(parts)


def select_wechat_window(cands):
    """在候选窗口里选可用主窗口。

    规则：只认「可见 + 非最小化 + 非 cloak + 尺寸 >= 200x200」；被其它窗口遮挡不影响（PrintWindow 能抓）。
    返回 (window_info, reason, detail)：选不到时 reason 说明准确原因。
    """
    usable = []
    for c in cands:
        w, h = c["size"]
        if not c["visible"] or c["iconic"] or c["cloaked"]:
            continue
        if w < 200 or h < 200:
            continue
        score = w * h
        if c["title"] == WECHAT_TITLE:
            score += 10 ** 9
        if c["class"] == WECHAT_CLASS:
            score += 10 ** 8
        usable.append((score, c))
    if usable:
        usable.sort(key=lambda x: x[0], reverse=True)
        return usable[0][1], "", ""

    # 主窗口候选（微信 4.x 主窗口类 Qt51514QWindowIcon / 标题 微信）
    main_cands = [c for c in cands if c["class"] == WECHAT_CLASS or c["title"] == WECHAT_TITLE]
    main_cands = main_cands or cands

    # 1) 最小化：可见 + IsIconic=True（注意最小化后 GetWindowRect 会变成 ~199x34 的虚框，不能要求尺寸）
    visible_iconic = [c for c in main_cands if c["visible"] and c["iconic"]]
    if visible_iconic:
        c = visible_iconic[0]
        return None, "wechat-window-minimized", \
            "微信主窗口处于最小化状态（hwnd=%s IsIconic=True，最小化后窗口矩形=%s），" \
            "最小化不可抓图（与「被其它窗口遮挡」不同）" % (hex(c["hwnd"]), c["size"])

    # 2) 隐藏到托盘：主窗口 IsWindowVisible=False
    hidden = [c for c in main_cands if not c["visible"]]
    if hidden:
        c = hidden[0]
        return None, "wechat-window-hidden-to-tray", \
            "微信主窗口被隐藏（hwnd=%s IsWindowVisible=False，最小化到托盘/关闭到托盘），无法抓图" % hex(c["hwnd"])

    # 3) 可见但尺寸过小
    small = [c for c in main_cands if c["visible"] and (c["size"][0] < 200 or c["size"][1] < 200)]
    if small:
        c = small[0]
        return None, "wechat-window-too-small", \
            "微信主窗口尺寸异常（hwnd=%s size=%s），无法抓图" % (hex(c["hwnd"]), c["size"])

    # 4) 只有辅助窗口 / 完全找不到
    big = [c for c in cands if c["size"][0] > 200 and c["size"][1] > 200 and c["visible"]]
    if big:
        return None, "wechat-window-not-usable", \
            "枚举到 Weixin 可见窗口但没有可用主窗口（可能正在切换账号/登录窗口）"
    return None, "wechat-window-not-found", "没有枚举到 Weixin.exe 的顶层窗口"


def find_wechat_window_simple():
    """兼容旧调用：只返回 (hwnd, title) 或 (None, "")。"""
    info, _reason, _detail = select_wechat_window(enumerate_weixin_windows())
    if info is None:
        return None, ""
    return info["hwnd"], info["title"]


def window_rect(hwnd):
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    return l, t, r, b


def print_window(hwnd, w, h):
    """PrintWindow 抓取窗口内容（被遮挡也有效，PW_RENDERFULLCONTENT=2）。"""
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc_dc, w, h)
    save_dc.SelectObject(bmp)
    ok = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
    info = bmp.GetInfo()
    buf = bmp.GetBitmapBits(True)
    img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), buf, "raw", "BGRX", 0, 1)
    win32gui.DeleteObject(bmp.GetHandle())
    save_dc.DeleteDC()
    mfc_dc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwnd_dc)
    if not ok or img.size[0] == 0 or img.size[1] == 0:
        return None
    return img


def screen_grab(bbox):
    try:
        return ImageGrab.grab(bbox=bbox)
    except Exception:
        return None


def bitblt_window(hwnd, w, h):
    """从窗口 DC BitBlt 抓图（PrintWindow 失败时的降级）。"""
    hwnd_dc = None
    src_dc = None
    mem_dc = None
    bmp = None
    try:
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        src_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        mem_dc = src_dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(src_dc, w, h)
        mem_dc.SelectObject(bmp)
        mem_dc.BitBlt((0, 0), (w, h), src_dc, (0, 0), win32con.SRCCOPY)
        info = bmp.GetInfo()
        buf = bmp.GetBitmapBits(True)
        return Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), buf, "raw", "BGRX", 0, 1)
    except Exception:
        return None
    finally:
        try:
            if bmp is not None:
                win32gui.DeleteObject(bmp.GetHandle())
        except Exception:
            pass
        try:
            if mem_dc is not None:
                mem_dc.DeleteDC()
        except Exception:
            pass
        try:
            if src_dc is not None:
                src_dc.DeleteDC()
        except Exception:
            pass
        try:
            if hwnd_dc is not None:
                win32gui.ReleaseDC(hwnd, hwnd_dc)
        except Exception:
            pass


def img_to_bgr(img):
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def near_black(img_bgr, thresh=8.0):
    return float(img_bgr.mean()) < thresh


def validate_capture(img):
    """校验抓到的图是否「像一张真窗口」：非空/非全黑/有内容方差。返回 (ok, reason)。"""
    if img is None:
        return False, "无图像"
    try:
        bgr = img_to_bgr(img)
    except Exception as exc:
        return False, "转换失败: %s" % exc
    if bgr.size == 0 or bgr.shape[0] < 50 or bgr.shape[1] < 50:
        return False, "尺寸异常: %s" % (getattr(img, "size", None),)
    mean = float(bgr.mean())
    if mean < 8.0:
        return False, "全黑(mean=%.1f)" % mean
    std = float(bgr.std())
    if std < 3.0:
        return False, "纯色(std=%.1f)" % std
    return True, ""


def capture_wechat_window(hwnd, rect, mode="auto"):
    """按 PrintWindow -> BitBlt -> 屏幕区域 的顺序降级抓图。

    返回 (img, source, attempts)；attempts 记录每层尝试与失败原因，便于日志定位。
    """
    l, t, r, b = rect
    w, h = r - l, b - t
    attempts = []
    if mode in ("auto", "printwindow"):
        img = print_window(hwnd, w, h)
        ok, why = validate_capture(img)
        attempts.append("printwindow=%s" % ("ok" if ok else "fail:" + why))
        if ok:
            return img, "printwindow", attempts
    if mode in ("auto", "bitblt"):
        img = bitblt_window(hwnd, w, h)
        ok, why = validate_capture(img)
        attempts.append("bitblt=%s" % ("ok" if ok else "fail:" + why))
        if ok:
            return img, "bitblt", attempts
    if mode in ("auto", "screen"):
        # 屏幕区域抓取：被遮挡时会抓到「盖在上面的窗口」，仅作最后兜底
        img = screen_grab((l, t, r, b))
        ok, why = validate_capture(img)
        attempts.append("screen=%s(可能抓到遮挡窗口)" % ("ok" if ok else "fail:" + why))
        if ok:
            return img, "screen", attempts
    return None, None, attempts


# ─────────────────────────── detection geometry ───────────────────────────
# 微信 4.x PC 版布局（逻辑像素 @125% 缩放下实测）：
#   左侧导航栏 67 + 会话列表 240（会话行 64 高，从 y=80 开始）
#   未读徽标样式（4.0 实测）：会话行「头像右上角」红底白字数字徽标；
#   左侧「聊天」图标上另有聚合徽标（总未读数）。两者都可作为检测信号。
GEOM = {
    "rail_w": 67.0,
    "list_w": 240.0,
    "rows_top": 80.0,
    "row_h": 64.0,
    "rows_max": 7,
    # 左侧「聊天」图标聚合徽标区（窗口逻辑坐标，绝对）
    "rail_chat_x": (20.0, 72.0),
    "rail_chat_y": (74.0, 124.0),
    # 会话行头像区（相对 list_left）：头像 44 宽，徽标在头像右上角
    "avatar_x": (12.0, 100.0),
    # 会话行右侧时间列（相对 list 右边）
    "right_badge_w": 66.0,
    "text_left_off": 52.0,
    "text_right_off": 70.0,
}


def detect_list_bounds(bgr, scale):
    """自动校准会话列表左右边界（找不到时回退到公式）。逻辑坐标 dict。"""
    h, w = bgr.shape[:2]
    # 用列表下方空白带的列颜色变化找列表/聊天区分界
    y0 = int(h * 0.80)
    y1 = int(h * 0.92)
    band = bgr[y0:y1].mean(axis=0)
    best_x, best_diff = None, 0.0
    for x in range(int(w * 0.15), int(w * 0.55)):
        left = band[max(0, x - 3):x].mean(axis=0)
        right = band[x:x + 3].mean(axis=0)
        diff = float(np.abs(left - right).sum())
        if diff > best_diff:
            best_diff, best_x = diff, x
    if best_x is not None and best_diff > 25:
        list_right = best_x / scale
    else:
        list_right = GEOM["rail_w"] + GEOM["list_w"]
    return {
        "scale": scale,
        "list_left": GEOM["rail_w"],
        "list_right": max(GEOM["rail_w"] + 120, list_right),
        "rows_top": GEOM["rows_top"],
        "row_h": GEOM["row_h"],
    }


def _red_mask(bgr):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    m1 = cv2.inRange(hsv, (0, 110, 110), (12, 255, 255))
    m2 = cv2.inRange(hsv, (168, 110, 110), (180, 255, 255))
    return cv2.bitwise_or(m1, m2)


def _white_mask(bgr):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return cv2.bitwise_and(cv2.inRange(hsv, (0, 0, 190), (180, 60, 255)), 255)


def _green_mask(bgr):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return cv2.inRange(hsv, (55, 90, 90), (100, 255, 255))


def _blobs(mask, min_area, min_w, max_w, min_h, max_h):
    if cv2 is None:
        return []
    n, _labels, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
    out = []
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        if area < min_area:
            continue
        if not (min_w <= w <= max_w and min_h <= h <= max_h):
            continue
        out.append({"x": x, "y": y, "w": w, "h": h, "area": area,
                    "cx": float(cents[i][0]), "cy": float(cents[i][1])})
    return out


def find_badge_blobs(bgr_zone, scale):
    """在区域内找「未读徽标」：红底 + 近方形 + 内部有白色数字。"""
    if bgr_zone is None or bgr_zone.size == 0 or cv2 is None:
        return []
    mask = _red_mask(bgr_zone)
    white = _white_mask(bgr_zone)
    min_area = int(110 * scale * scale)
    blobs = _blobs(mask, min_area, int(10 * scale), int(34 * scale),
                   int(10 * scale), int(34 * scale))
    out = []
    for bl in blobs:
        ar = bl["w"] / float(max(1, bl["h"]))
        if ar < 0.5 or ar > 2.2:
            continue
        x, y, w, h = bl["x"], bl["y"], bl["w"], bl["h"]
        white_count = int((white[y:y + h, x:x + w] > 0).sum())
        # 数字或数字+白边：至少几个白像素；纯色红块（头像 logo）会被排除
        if white_count < int(6 * scale * scale):
            continue
        bl = dict(bl)
        bl["white"] = white_count
        out.append(bl)
    return out


def pixel_changed_ratio(cur, base):
    if cur is None or base is None or cur.shape != base.shape or cur.size == 0:
        return 1.0
    diff = cv2.absdiff(cur, base)
    changed = int((diff > 28).sum())
    return changed / float(cur.size)


def _dhash_bgr(bgr):
    """行/区域感知哈希（dhash，64bit hex）。用于未读指纹：同一画面稳定、内容变化则变。"""
    if bgr is None or getattr(bgr, "size", 0) == 0 or cv2 is None:
        return ""
    try:
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
        bits = 0
        for row in small:
            for i in range(8):
                bits = (bits << 1) | (1 if row[i + 1] > row[i] else 0)
        return format(bits, "016x")
    except Exception:
        return ""


# ─────────────────────────── OCR / whitelist matching ───────────────────────────
_ELLIPSIS_TAILS = ("…", "...", "。。。")
_OCR_TMP_DIR = os.path.join(BASE_DIR, "tmp-ocr")


def find_tesseract(explicit=""):
    """定位已安装的 tesseract（本机已装，不引入新包）。"""
    cands = []
    if explicit:
        cands.append(explicit)
    env = os.environ.get("TESSERACT_BIN", "")
    if env:
        cands.append(env)
    # 常见安装位置（用户级 / 系统级），也支持 TESSERACT_BIN 环境变量或 PATH 里的 tesseract
    for base, sub in ((os.environ.get("LOCALAPPDATA"), os.path.join("Programs", "Tesseract-OCR")),
                      (os.environ.get("ProgramFiles"), "Tesseract-OCR"),
                      (os.environ.get("ProgramFiles(x86)"), "Tesseract-OCR")):
        if base:
            cands.append(os.path.join(base, sub, "tesseract.exe"))
    which = shutil.which("tesseract")
    if which:
        cands.append(which)
    for p in cands:
        try:
            if p and os.path.isfile(p):
                return p
        except Exception:
            pass
    return ""


def _tess_run(arr, psm, tesseract, lang="chi_sim", timeout_ms=10000):
    if not tesseract:
        return ""
    try:
        os.makedirs(_OCR_TMP_DIR, exist_ok=True)
        path = os.path.join(_OCR_TMP_DIR, "ocr-%s.png" % uuid.uuid4().hex[:8])
        Image.fromarray(arr).save(path)
        try:
            p = subprocess.run(
                [tesseract, path, "stdout", "-l", lang, "--psm", str(psm), "--oem", "1", "--dpi", "300"],
                capture_output=True, timeout=max(3.0, timeout_ms / 1000.0),
                creationflags=0x08000000)
            return " ".join(p.stdout.decode("utf-8", "replace").split())
        finally:
            try:
                os.remove(path)
            except Exception:
                pass
    except Exception:
        return ""


def _ocr_text_bbox(gray, thresh=105):
    mask = (gray > thresh).astype(np.uint8)
    ys, xs = np.where(mask > 0)
    if len(xs) == 0:
        return None
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    if x1 - x0 < 8 or y1 - y0 < 6:
        return None
    return x0, y0, x1, y1


def _ocr_prep(crop_rgb, mode, use_bbox):
    gray = cv2.cvtColor(np.array(crop_rgb), cv2.COLOR_RGB2GRAY)
    if use_bbox:
        bb = _ocr_text_bbox(gray)
        if bb:
            x0, y0, x1, y1 = bb
            pad = 5
            gray = gray[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad]
    if mode == "plain2x":
        g = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_LANCZOS4)
        return cv2.copyMakeBorder(g, 16, 16, 16, 16, cv2.BORDER_CONSTANT, value=0)
    if mode == "norm3x":
        g = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
        g = cv2.resize(g, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        return cv2.copyMakeBorder(g, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=0)
    inv = cv2.bitwise_not(gray)
    if mode == "bin3x":
        _, th = cv2.threshold(inv, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        th = cv2.resize(th, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST)
        return cv2.copyMakeBorder(th, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255)
    if mode == "bin4x":
        _, th = cv2.threshold(inv, 45, 255, cv2.THRESH_BINARY)
        th = cv2.resize(th, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST)
        return cv2.copyMakeBorder(th, 24, 24, 24, 24, cv2.BORDER_CONSTANT, value=255)
    return None


def _ocr_row_name_candidates(pil_img, geom, row_idx, tesseract, ocr_cfg):
    """对会话行「名字行」做多预处理多 PSM OCR，返回候选文本列表。

    名字区右侧留出时间列，避免把「15:49」这类时间戳混进名字。
    """
    s = geom["scale"]
    W, H = pil_img.size
    lx0 = int(round(geom["list_left"] * s))
    lx1 = min(W, int(round(geom["list_right"] * s)))
    rows_top = int(round(geom["rows_top"] * s))
    row_h = int(round(geom["row_h"] * s))
    top = rows_top + row_idx * row_h
    box_fixed = (lx0 + int(62 * s), top + int(12 * s), lx1 - int(56 * s), top + int(40 * s))
    box_wide = (lx0 + int(56 * s), top + int(8 * s), lx1 - int(50 * s), top + int(44 * s))
    plans = [
        (box_fixed, "plain2x", False, 7),
        (box_fixed, "bin3x", True, 6),
        (box_wide, "bin3x", True, 6),
        (box_wide, "norm3x", True, 6),
        (box_wide, "norm3x", True, 7),
        (box_wide, "bin4x", True, 6),
    ]
    out = []
    lang = ocr_cfg.get("lang", "chi_sim")
    timeout_ms = int(ocr_cfg.get("timeoutMs", 10000))
    for box, mode, use_bbox, psm in plans:
        bx0 = max(0, min(W, box[0]))
        by0 = max(0, min(H, box[1]))
        bx1 = max(0, min(W, box[2]))
        by1 = max(0, min(H, box[3]))
        if bx1 - bx0 < 10 or by1 - by0 < 8:
            continue
        try:
            arr = _ocr_prep(pil_img.crop((bx0, by0, bx1, by1)), mode, use_bbox)
        except Exception:
            continue
        if arr is None:
            continue
        txt = _tess_run(arr, psm, tesseract, lang, timeout_ms)
        if txt:
            out.append(txt)
    return out


def _ocr_row_preview(pil_img, geom, row_idx, tesseract, ocr_cfg):
    """OCR 会话行「最近消息预览」（名字下面那一行），给生成回复当「对方说了什么」。

    可选功能：OCR 失败/取不到就返回空串，由调用方退化成「只知道有新消息」。
    """
    s = geom["scale"]
    W, H = pil_img.size
    lx0 = int(round(geom["list_left"] * s))
    lx1 = min(W, int(round(geom["list_right"] * s)))
    rows_top = int(round(geom["rows_top"] * s))
    row_h = int(round(geom["row_h"] * s))
    top = rows_top + row_idx * row_h
    box = (lx0 + int(62 * s), top + int(40 * s), lx1 - int(14 * s), top + int(62 * s))
    plans = [(box, "plain2x", 7), (box, "bin3x", 7), (box, "norm3x", 7)]
    out = ""
    lang = ocr_cfg.get("lang", "chi_sim")
    timeout_ms = int(ocr_cfg.get("timeoutMs", 10000))
    for bx, mode, psm in plans:
        bx0 = max(0, min(W, bx[0]))
        by0 = max(0, min(H, bx[1]))
        bx1 = max(0, min(W, bx[2]))
        by1 = max(0, min(H, bx[3]))
        if bx1 - bx0 < 10 or by1 - by0 < 8:
            continue
        try:
            arr = _ocr_prep(pil_img.crop((bx0, by0, bx1, by1)), mode, True)
        except Exception:
            continue
        if arr is None:
            continue
        txt = _tess_run(arr, psm, tesseract, lang, timeout_ms)
        if txt:
            out = txt.strip()
            if out:
                break
    # 去掉常见的纯符号/时间噪声
    if not re.search(r"[\w\u4e00-\u9fff]", out or ""):
        return ""
    return out[:120]


def normalize_name(text):
    """归一化显示名：去首尾空白、去掉结尾 …/...、全角转半角、统一小写。"""
    if not text:
        return ""
    s = str(text).strip()
    for tail in _ELLIPSIS_TAILS:
        while s.endswith(tail):
            s = s[:-len(tail)].strip()
    out = []
    for ch in s:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        elif code == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    return " ".join("".join(out).strip().lower().split())


def _clean_candidate(text):
    s = (text or "").strip()
    s = re.sub(r"^[\s\W_]+", "", s)
    s = re.sub(r"[\s\W_]+$", "", s)
    return s.strip()


def _coverage(cand, target):
    """candidate 的字符按顺序出现在 target 中的比例（容忍 OCR 错字/截断）。"""
    if not cand or not target:
        return 0.0, 0
    try:
        sm = difflib.SequenceMatcher(None, cand, target, autojunk=False)
        matched = sum(b.size for b in sm.get_matching_blocks())
    except Exception:
        return 0.0, 0
    return matched / float(len(cand)), matched


def name_match(candidates, names, match_cfg=None):
    """名单匹配（白名单/黑名单通用，任一命中即算），返回 dict。规则：
    ① 完全相等；② 名单项是显示名前缀（≥4 字）；③ 显示名是名单项前缀（≥4 字）；
    ④ OCR 容错：候选与名单项（或其前缀）字符覆盖率达标 → 仍算 matched by prefix；
    ⑤ 短名容错（≤5 字，OCR 错 1 个字也能认，如 改信团队 ≈ 微信团队）。
    """
    cfg = match_cfg or {}
    thr = float(cfg.get("coverageThreshold", 0.70))
    loose = float(cfg.get("coverageThresholdLoose", 0.55))
    loose_min = int(cfg.get("looseMinMatched", 6))
    wl = [str(w).strip() for w in (names or []) if str(w).strip()]
    res = {"hit": False, "matched": "", "reason": "", "display": "", "normalized": "",
           "candidates": list(candidates or []), "whitelistEmpty": not wl}
    best_display = ""
    for raw in (candidates or []):
        cand = _clean_candidate(raw)
        norm_c = normalize_name(cand)
        if len(norm_c) < 2:
            continue
        if len(norm_c) > len(best_display):
            best_display = cand
        res["display"] = best_display
        res["normalized"] = normalize_name(best_display)
        for item in wl:
            norm_i = normalize_name(item)
            if not norm_i:
                continue
            if norm_c == norm_i:
                res.update({"hit": True, "matched": item, "reason": "matched by exact",
                            "display": cand, "normalized": norm_c})
                return res
            if len(norm_i) >= 4 and norm_c.startswith(norm_i):
                res.update({"hit": True, "matched": item, "reason": "matched by prefix",
                            "display": cand, "normalized": norm_c})
                return res
            if len(norm_c) >= 4 and norm_i.startswith(norm_c):
                res.update({"hit": True, "matched": item, "reason": "matched by prefix",
                            "display": cand, "normalized": norm_c})
                return res
        # ⑤ 短名容错：4~5 字的显示名（如「微信团队」）OCR 错一个字也要能认
        if len(norm_c) <= 5:
            for item in wl:
                norm_i = normalize_name(item)
                if not norm_i or len(norm_i) > 8:
                    continue
                cov, matched = _coverage(norm_c, norm_i)
                if matched >= 3 and cov >= 0.6:
                    res.update({"hit": True, "matched": item, "reason": "matched by prefix",
                                "display": cand, "normalized": norm_c})
                    return res
        for item in wl:
            norm_i = normalize_name(item)
            if not norm_i:
                continue
            cov, matched = _coverage(norm_c, norm_i)
            if matched >= 4 and cov >= thr:
                res.update({"hit": True, "matched": item, "reason": "matched by prefix",
                            "display": cand, "normalized": norm_c})
                return res
            if matched >= loose_min and cov >= loose:
                res.update({"hit": True, "matched": item, "reason": "matched by prefix",
                            "display": cand, "normalized": norm_c})
                return res
            for plen in range(4, len(norm_i)):
                cov, matched = _coverage(norm_c, norm_i[:plen])
                if (matched >= 4 and cov >= thr) or (matched >= loose_min and cov >= loose):
                    res.update({"hit": True, "matched": item, "reason": "matched by prefix",
                                "display": cand, "normalized": norm_c})
                    return res
    return res


# 兼容旧调用名
whitelist_match = name_match


def analyze_window(bgr, scale):
    """分析微信窗口截图：未读徽标（左栏聚合 + 行头像角标 + 右列）+ 行变化比例。"""
    h, w = bgr.shape[:2]
    geom = detect_list_bounds(bgr, scale)
    s = scale
    lx0 = int(round(geom["list_left"] * s))
    lx1 = min(w, int(round(geom["list_right"] * s)))
    ly0 = int(round(geom["rows_top"] * s))
    ly1 = min(h, int(round((geom["rows_top"] + GEOM["rows_max"] * GEOM["row_h"]) * s)))
    if ly1 <= ly0:
        ly1 = min(h, ly0 + int(240 * s))
    result = {
        "geom": geom,
        "list_region": (lx0, ly0, lx1, ly1),
        "rail_unread": False,
        "rail_blobs": [],
        "rail_zone": None,
        "row_badges": 0,
        "row_blob_list": [],
        "unread": False,
        "active_rows": [],
        "text_region": None,
        "text_bgr": None,
        "green_active_ratio": 0.0,
        "fingerprint": {"digest": "", "count": 0, "rows": [], "rail": ""},
    }
    if cv2 is None or lx1 <= lx0 or ly1 <= ly0:
        return result

    # 1) 左栏「聊天」图标聚合未读徽标
    rx0 = int(round(GEOM["rail_chat_x"][0] * s))
    rx1 = int(round(GEOM["rail_chat_x"][1] * s))
    ry0 = int(round(GEOM["rail_chat_y"][0] * s))
    ry1 = int(round(GEOM["rail_chat_y"][1] * s))
    rail_zone = bgr[ry0:ry1, rx0:rx1]
    result["rail_zone"] = rail_zone
    rail_blobs = find_badge_blobs(rail_zone, s)
    result["rail_unread"] = len(rail_blobs) > 0
    result["rail_white"] = int(sum(b.get("white", 0) for b in rail_blobs))
    result["rail_blobs"] = [dict(b, zone_origin=(rx0, ry0)) for b in rail_blobs]

    # 2) 会话行头像右上角的数字徽标
    ax0 = lx0 + int(round(GEOM["avatar_x"][0] * s))
    ax1 = lx0 + int(round(GEOM["avatar_x"][1] * s))
    avatar_zone = bgr[ly0:ly1, ax0:ax1]
    if avatar_zone.size:
        row_blobs = find_badge_blobs(avatar_zone, s)
        result["row_badges"] = len(row_blobs)
        result["row_blob_list"] = [dict(b, zone_origin=(ax0, ly0)) for b in row_blobs]

    # 3) 会话行右侧时间列（其他样式/兜底）
    bx0 = lx1 - int(round(GEOM["right_badge_w"] * s))
    right_zone = bgr[ly0:ly1, max(lx0, bx0):lx1]
    if right_zone.size:
        right_blobs = find_badge_blobs(right_zone, s)
        if right_blobs:
            result["row_badges"] += len(right_blobs)
            result["row_blob_list"].extend(
                [dict(b, zone_origin=(max(lx0, bx0), ly0), where="right") for b in right_blobs])

    result["unread"] = bool(result["rail_unread"] or result["row_badges"] > 0)

    # 4) 当前选中（绿色高亮）行：差分时排除，避免自己发消息/输入引起误报
    list_region = bgr[ly0:ly1, lx0:lx1]
    gm = _green_mask(list_region)
    if gm.size:
        row_px = int(GEOM["row_h"] * s)
        masked = []
        for row in range(GEOM["rows_max"]):
            ya = row * row_px
            yb = min(ya + row_px, list_region.shape[0])
            if yb <= ya:
                break
            frac = float((gm[ya:yb] > 0).mean())
            if frac > 0.25:
                masked.append((row, frac))
        result["active_rows"] = [m[0] for m in masked]
        result["green_active_ratio"] = max([m[1] for m in masked], default=0.0)

    # 5) 文本区（标题+摘要+时间）：稳定基线差分
    tx0 = lx0 + int(round(GEOM["text_left_off"] * s))
    tx1 = lx0 + int(round((geom["list_right"] - geom["list_left"] - GEOM["text_right_off"]) * s))
    if tx1 > tx0:
        result["text_region"] = (tx0, ly0, min(tx1, w), ly1)
        result["text_bgr"] = bgr[ly0:ly1, tx0:min(tx1, w)]

    # 6) 未读指纹：未读徽标数量 + 每个未读会话行的内容 dhash + 左栏聚合徽标 dhash
    #    同一画面 re-scan 指纹不变 → 去重；内容/数量变化 → 指纹变化。
    try:
        row_h_px = int(round(GEOM["row_h"] * s))
        name_x0 = lx0 + int(round(52 * s))
        fp_rows = []
        seen_rows = set()
        for b in result["row_blob_list"]:
            if b.get("where") == "right":
                continue
            cy = b["zone_origin"][1] + b["y"] + b["h"] // 2
            row_idx = int((cy - ly0) // max(1, row_h_px))
            if row_idx in seen_rows:
                continue
            seen_rows.add(row_idx)
            top = ly0 + row_idx * row_h_px
            bottom = min(ly1, top + row_h_px)
            if bottom - top < 8:
                continue
            strip = bgr[max(0, top + int(4 * s)):max(0, bottom - int(4 * s)), name_x0:lx1]
            name_strip = bgr[max(0, top + int(10 * s)):max(0, bottom - int(28 * s)),
                             name_x0:max(name_x0 + 8, lx1 - int(50 * s))]
            fp_rows.append({"row": int(row_idx), "dh": _dhash_bgr(strip),
                            "name_dh": _dhash_bgr(name_strip)})
        fp_rows.sort(key=lambda r: r["row"])
        rail_dh = _dhash_bgr(rail_zone) if rail_zone is not None else ""
        fp = {"count": len(fp_rows), "rows": fp_rows, "rail": rail_dh}
        digest = hashlib.sha1(
            json.dumps(fp, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
        result["fingerprint"] = {"digest": digest, "count": fp["count"],
                                 "rows": fp_rows, "rail": rail_dh}
    except Exception as exc:
        log_warn("未读指纹计算失败: %s" % exc)
    return result


def row_diff_ratio(cur, base, active_rows, geom):
    """文本区差分比例；排除当前选中行。返回 (ratio, changed)"""
    if cur is None or base is None or cur.shape != base.shape:
        return 1.0, True
    s = geom["scale"]
    row_px = int(geom["row_h"] * s)
    hh = cur.shape[0]
    mask = np.zeros(hh, dtype=bool)
    for row in range(hh // max(1, row_px) + 1):
        ya = row * row_px
        yb = min(ya + row_px, hh)
        if yb > ya:
            mask[ya:yb] = True
    for row in active_rows:
        ya = row * row_px
        yb = min(ya + row_px, hh)
        if yb > ya:
            mask[ya:yb] = False
    if not mask.any():
        return 0.0, False
    cur_g = cv2.cvtColor(cur[mask], cv2.COLOR_BGR2GRAY)
    base_g = cv2.cvtColor(base[mask], cv2.COLOR_BGR2GRAY)
    diff = cv2.absdiff(cur_g, base_g)
    changed = int((diff > 30).sum())
    total = diff.size
    ratio = changed / float(total) if total else 0.0
    return ratio, ratio > 0.0


# ─────────────────────────── taskbar layer ───────────────────────────


def find_taskbar_wechat_icon(scale):
    """在任务栏截图中找微信绿色图标，返回 (bgr, icon_rect) 或 (None, None)。"""
    hwnd = ctypes.windll.user32.FindWindowW("Shell_TrayWnd", None)
    if not hwnd:
        return None, None
    l, t, r, b = window_rect(hwnd)
    w, h = r - l, b - t
    if w <= 0 or h <= 0:
        return None, None
    img = print_window(hwnd, w, h)
    if img is None:
        return None, None
    bgr = img_to_bgr(img)
    gm = _green_mask(bgr)
    blobs = _blobs(gm, int(80 * scale * scale), int(14 * scale), int(40 * scale), int(14 * scale), int(40 * scale))
    if not blobs:
        return bgr, None
    # 选最接近正方形、面积最大的绿色块
    def score(bl):
        ar = bl["w"] / float(bl["h"])
        return -abs(ar - 1.0) * 1000 + bl["area"]
    blobs.sort(key=score, reverse=True)
    icon = blobs[0]
    return bgr, icon


def taskbar_unread(bgr, icon, scale):
    """检测任务栏微信图标角标红点。"""
    if bgr is None or icon is None:
        return False, 0
    h, w = bgr.shape[:2]
    pad = int(10 * scale)
    x0 = max(0, icon["x"] - pad)
    y0 = max(0, icon["y"] - pad)
    x1 = min(w, icon["x"] + icon["w"] + pad)
    y1 = min(h, icon["y"] + icon["h"] + pad)
    zone = bgr[y0:y1, x0:x1]
    if zone.size == 0:
        return False, 0
    mask = _red_mask(zone)
    blobs = _blobs(mask, int(30 * scale * scale), int(4 * scale), int(22 * scale), int(4 * scale), int(22 * scale))
    return len(blobs) > 0, len(blobs)


# ─────────────────────────── DSH wake channel ───────────────────────────


def resolve_dsh_home(config):
    home = os.environ.get("DSH_HOME") or config.get("wake", {}).get("dshHome") or ""
    if home:
        return home
    default = os.path.join(os.path.expanduser("~"), ".dsh")
    if os.path.isdir(default):
        return default
    return default


def _b64url(data):
    return base64.b64encode(data).decode().replace("+", "-").replace("/", "_").rstrip("=")


def read_browser_session_secret(dsh_home):
    """读取 DSH 持久化的 browser-session 签名密钥（32 字节）。"""
    path = os.path.join(dsh_home, ".credentials.yaml")
    if not os.path.isfile(path):
        return None, "credentials 文件不存在: %s" % path
    text = None
    try:
        import yaml  # PyYAML（系统 Python 已有）
        with open(path, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        rec = (doc or {}).get("records", {}).get("client-connection/browser-session")
        version = ((rec or {}).get("payload") or {}).get("version")
        secret = ((rec or {}).get("payload") or {}).get("secret")
        if (rec or {}).get("kind") != "grant" or version != 1 or not isinstance(secret, str):
            return None, "credentials 中无 browser-session 记录"
        raw = base64.b64decode(secret.replace("-", "+").replace("_", "/") + "=" * (-len(secret) % 4))
        if len(raw) != 32:
            return None, "browser-session 密钥长度异常"
        return raw, ""
    except Exception as exc:
        return None, "读取 credentials 失败: %s" % exc


def mint_browser_cookie(base_url, secret, lifetime_ms=3600_000):
    """离线铸造 DSH browser-session Cookie（与 dsh-client-connection 的校验格式一致）。"""
    authority = urllib.parse.urlsplit(base_url).netloc
    issued = int(time.time() * 1000)
    payload = {"version": 1, "authority": authority,
               "issuedAt": issued, "expiresAt": issued + lifetime_ms}
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64url(hmac.new(secret, body.encode(), hashlib.sha256).digest())
    name = "dsh-auth-" + _b64url(hashlib.sha256(authority.encode()).digest())
    return "%s=v1.%s.%s" % (name, body, sig)


class DshRpc:
    """通过 DSH Web API（slash RPC + 铸造 Cookie）唤醒 agent。"""

    def __init__(self, config):
        self.config = config
        self.wake_cfg = config.get("wake", {})
        self.base_url = str(self.wake_cfg.get("baseUrl") or "http://127.0.0.1:19387").rstrip("/")
        self.dsh_home = resolve_dsh_home(config)
        self._cookie = None
        self._cookie_exp = 0

    def available(self):
        secret, err = read_browser_session_secret(self.dsh_home)
        if secret is None:
            return False, err
        try:
            urllib.request.urlopen(urllib.request.Request(self.base_url + "/"), timeout=3)
            return True, ""
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return True, ""  # 有鉴权是正常的，能连上即可
            return False, "HTTP %s" % exc.code
        except Exception as exc:
            return False, "连不上 %s: %s" % (self.base_url, exc)

    def _get_cookie(self):
        """每次调用都重新读 .credentials.yaml 并重新铸造 Cookie（禁用缓存）。

        原因：DSH 重启/重建凭据后 browser-session 密钥可能变化，缓存旧 Cookie 会得到 401；
        铸造是纯本地 HMAC，成本可忽略，唤醒频率也低。
        """
        secret, err = read_browser_session_secret(self.dsh_home)
        if secret is None:
            raise RuntimeError("读取 browser-session 签名密钥失败: %s" % err)
        cookie = mint_browser_cookie(self.base_url, secret)
        self._cookie = cookie
        return cookie

    def rpc(self, method, args, timeout=60):
        endpoint = method.replace(".", "/")
        rpc_id = str(uuid.uuid4())
        body = json.dumps({"type": "client-request", "rpcId": rpc_id,
                           "method": endpoint, "payload": {"args": args}}).encode()
        req = urllib.request.Request(
            self.base_url + "/api/" + endpoint, data=body, method="POST",
            headers={"Content-Type": "application/json", "Cookie": self._get_cookie()})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                full = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError("HTTP %s: %s" % (exc.code, exc.read()[:300].decode("utf-8", "replace")))
        if not isinstance(full, dict) or full.get("type") != "server-response" or full.get("rpcId") != rpc_id:
            raise RuntimeError("DSH 返回了非预期信封: %s" % str(full)[:300])
        return full.get("result") or {}

    @staticmethod
    def _unwrap(method, result):
        if result.get("ok"):
            return result.get("value")
        err = result.get("error") or {}
        raise RuntimeError("%s 失败: %s: %s" % (method, err.get("code"), err.get("message")))

    def list_sessions(self):
        value = self._unwrap("session/list", self.rpc("session/list", {"_request": {}}))
        return (value or {}).get("items") or []

    def find_session(self, session_id="", title=""):
        items = self.list_sessions()
        if session_id:
            for it in items:
                if it.get("sessionId") == session_id:
                    return it
        if title:
            for it in items:
                proj = ((it.get("projections") or {}).get("values") or {})
                if proj.get("title") == title:
                    return it
        return None

    def find_or_create_session(self, state):
        cfg = self.wake_cfg
        session_id = str(cfg.get("sessionId") or "").strip()
        title = str(cfg.get("sessionTitle") or "微信看门狗").strip()
        if session_id:
            found = self.find_session(session_id=session_id)
            if found:
                return session_id
            log_warn("wake.sessionId=%s 在 DSH 中不存在，改为按标题查找" % session_id)
        found = self.find_session(title=title)
        if found:
            session_id = found["sessionId"]
            state["wake_session_id"] = session_id
            save_state(state)
            return session_id
        if not cfg.get("allowCreateSession", True):
            raise RuntimeError("找不到标题为 %r 的会话，且 wake.allowCreateSession=false" % title)
        # 创建新会话（cwd 指向本目录，便于 agent 读 signal.json / 截图）
        cwd = str(cfg.get("cwd") or BASE_DIR)
        os.makedirs(cwd, exist_ok=True)
        value = self._unwrap("session/create", self.rpc("session/create", {"request": {"cwd": cwd}}))
        session_id = value["sessionId"]
        try:
            self._unwrap("session/rename", self.rpc("session/rename",
                                                    {"request": {"sessionId": session_id, "title": title}}))
        except Exception as exc:
            log_warn("会话改名失败（不影响唤醒）: %s" % exc)
        state["wake_session_id"] = session_id
        save_state(state)
        log_info("已创建唤醒会话: %s (%s)" % (session_id, title))
        return session_id

    def prompt(self, session_id, text):
        value = self._unwrap("session/prompt", self.rpc("session/prompt", {"request": {
            "requestId": str(uuid.uuid4()),
            "sessionId": session_id,
            "mode": "queue",
            "content": [{"type": "text", "text": text}],
        }}))
        return value


# ─────────────────────────── 远程指令（微信 → Agent，2026-10-26 两级密码 + 挑战-应答） ───────────────────────────
# 链路：白名单来信含触发词「小耳朵」+ 正确密码（L1/L2）-> 不生成闲聊回复，改用 DSH session/prompt
#       把指令转告给本机会话里的 Agent -> Agent 干活后用 reply.py / sendfile.py 把结果发回微信。
#
# 两级密码（只存哈希，config/日志/审计/备份里都不出现明文）：
#   L1 = 只读/低风险（allow: send_file/screenshot/report_status/read_text）
#   L2 = 高危（额外 allow: run_command/modify_file/delete_file/install/start_stop_app）
#   哈希算法（与 config.remote.levels.*.sha256 一致，别自己另发明）：
#       sha256_hex = sha256(bytes.fromhex(salt) + password.encode("utf-8")).hexdigest()  # 小写 hex
#
# 🔴 取舍（务必保留）：**绝不发"收到我去办"这种回执** —— watcher 的 sendguard 同一会话
#    30 秒限流会把 Agent 随后发出的真实结果拦掉；宁可少一句回执，也要保证最终结果能发回。
REMOTE_STATE_PATH = os.path.join(BASE_DIR, "remote_pending.json")
# 2026-10-26 23:4x：失败提醒的去重/限流状态（同一 chat+reason 冷却期内只提醒一次）
REMOTE_NOTIFY_STATE_PATH = os.path.join(BASE_DIR, "remote_notify_state.json")

REMOTE_PROMPT_TEMPLATE = (
    "【微信远程指令｜口令已校验】\n"
    "授权级别：{level}（{label}）\n"
    "允许动作（allow 列表，原样）：{allow}\n"
    "来信会话：{chat}\n"
    "来信时间：{time}\n"
    "用户原话：{text}\n"
    "\n"
    "你现在被微信唤起来当\"远程操作员\"。请在这台电脑上完成上面的请求，然后把结果发回微信。\n"
    "发回方式（只能用这两个脚本，不要自己开微信窗口乱点）：\n"
    "  文本： python \"{reply_py}\" --chat \"{chat}\" --text \"<内容>\"\n"
    "  文件： python \"{send_py}\" --chat \"{chat}\" --file \"<绝对路径>\"\n"
    "硬性要求：\n"
    "1. 只发用户点名要的东西；绝不发 .credentials*、*key*、密码/令牌类文件。\n"
    "2. 不要批量外发、不要发整个磁盘目录。\n"
    "3. 发送失败重试一次，仍失败就在审计日志里写清原因。\n"
    "4. 做完必须用上面的脚本给用户一个明确回音（成功发了什么 / 为什么没办成）。\n"
    "5. 本次授权只覆盖上面 allow 列表里的动作；超出范围的动作一律拒绝，并回音说明「需要二级密码」。\n"
)

# L1（只读/低风险）请求里出现这些词 -> 判定为高危动作，不转告、要求补 L2
REMOTE_HIGH_RISK_WORDS = (
    "删除", "删掉", "删了", "清空", "格式化", "覆盖", "重命名", "改名", "移动到", "剪切",
    "执行", "运行", "命令行", "cmd", "powershell", "脚本", "注册表", "服务", "进程",
    "安装", "卸载", "装个", "启动", "关闭", "停止", "杀掉", "结束进程", "写入", "新建", "修改",
)

_AUTHCODE_RE = re.compile(r"[A-Za-z0-9]{4,}")


def sha256_pwd(salt_hex, password):
    """sha256(bytes.fromhex(salt) + password_utf8) -> 小写 hex（与 config.remote.levels.*.sha256 一致）。"""
    return hashlib.sha256(bytes.fromhex(str(salt_hex)) + str(password).encode("utf-8")).hexdigest()


def verify_level_password(password, level_cfg):
    """校验某个密码是否等于该级别的密码（只比较哈希，不存明文）。"""
    if not password or not isinstance(level_cfg, dict):
        return False
    salt = str(level_cfg.get("salt") or "")
    expect = str(level_cfg.get("sha256") or "").lower()
    if not salt or not expect:
        return False
    try:
        return sha256_pwd(salt, password) == expect
    except Exception:
        return False


def remote_levels(remote_cfg):
    """取 config.remote.levels（只保留 dict 项）。"""
    lv = (remote_cfg or {}).get("levels") or {}
    return {str(k): v for k, v in lv.items() if isinstance(v, dict)}


_REDACT_CACHE = {"cfg_mtime": None, "levels": {}}


def _redact_levels():
    """日志脱敏用：缓存 levels 哈希；config.json 变更时自动重载。"""
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
        if _REDACT_CACHE["cfg_mtime"] != mtime:
            cfg = load_config()
            _REDACT_CACHE["levels"] = remote_levels(cfg.get("remote") or {})
            _REDACT_CACHE["cfg_mtime"] = mtime
        return _REDACT_CACHE["levels"]
    except Exception:
        return _REDACT_CACHE.get("levels") or {}


def redact_known_password(text):
    """日志/审计统一脱敏：把 6 位连续数字串、以及能通过哈希校验的密码候选替换成 ******。

    2026-10-26 需求：日志里凡打印来信原文处，6 位连号与已知密码形态都要打码。
    这里不知道明文密码，所以对候选 token 现场做哈希校验（只比较哈希，安全）。
    """
    s = str(text if text is not None else "")
    if not s:
        return s
    try:
        levels = _redact_levels()
    except Exception:
        levels = {}
    if levels:
        # 1) 先按 token 做哈希校验，遮掉「完整密码」（必须在 6 位数字打码之前！
        #    否则「6位数字+字母」形态的密码会先被打成 "******abc"，后半段就漏出去了）
        def _sub(m):
            token = m.group(0)
            for lv in levels.values():
                if verify_level_password(token, lv):
                    return "******"
            return token
        s = _AUTHCODE_RE.sub(_sub, s)
    # 2) 再把剩余 6 位连续数字（L1 形态 / 验证码 / 密码形态）遮掉
    #    注意：文件名/时间戳/hash 里的 6 位数字（左右紧邻字母/数字/-_./\:）不算密码形态，不遮，
    #    否则日志里的截图路径（reply-20261026-194736-before.png）会被打烂。
    _path_neighbor = set("0123456789abcdefghijklmnopqrstuvwxyz"
                         "ABCDEFGHIJKLMNOPQRSTUVWXYZ-_./\\:")

    def _mask_six(m):
        left = s[m.start() - 1] if m.start() > 0 else ""
        right = s[m.end()] if m.end() < len(s) else ""
        if left in _path_neighbor or right in _path_neighbor:
            return m.group(0)
        return "******"

    return re.sub(r"(?<!\d)\d{6}(?!\d)", _mask_six, s)


# 日志统一脱敏入口（Logger.write / reply.py / sendfile.py 都会调用）
mask_sensitive = redact_known_password


def find_password_in_text(text, remote_cfg):
    """在文本里找「恰好等于某个级别密码」的候选。返回 (level_name, token) 或 ("", "")。"""
    levels = remote_levels(remote_cfg)
    if not levels:
        return "", ""
    s = str(text or "")
    for m in _AUTHCODE_RE.finditer(s):
        token = m.group(0)
        for name, lv in levels.items():
            if verify_level_password(token, lv):
                return name, token
    for m in re.finditer(r"(?<!\d)(\d{6})(?!\d)", s):
        token = m.group(1)
        for name, lv in levels.items():
            if verify_level_password(token, lv):
                return name, token
    return "", ""


def redact_token(text, token):
    if not token:
        return str(text or "")
    return str(text or "").replace(token, "******")


def is_high_risk_text(text):
    """L1 授权下的高危动作探测（命中返回关键词，否则空串）。"""
    low = str(text or "").lower()
    for w in REMOTE_HIGH_RISK_WORDS:
        if w.lower() in low:
            return w
    return ""


def build_remote_prompt(remote_cfg, chat, text, level):
    """组装转告 prompt：必须写明授权级别 + allow 清单（原样），命令原文已脱敏。"""
    levels = remote_levels(remote_cfg)
    lv = levels.get(level) or {}
    allow = lv.get("allow") or []
    label = str(lv.get("label") or "")
    prompt = REMOTE_PROMPT_TEMPLATE.format(
        chat=chat, time=time.strftime("%Y-%m-%d %H:%M:%S"),
        text=redact_known_password(text), level=level, label=label,
        reply_py=REPLY_PY_PATH, send_py=SEND_PY_PATH,
        allow=", ".join(str(a) for a in allow))
    if level == "L2":
        prompt += ("\n本次为二级授权（L2）：允许执行命令与文件修改/删除/安装/启动停止程序等 "
                   "allow 列表内动作。\n")
    else:
        prompt += ("\n本次为一级授权（L1，只读/低风险）：不得执行命令、修改/删除文件、安装软件或"
                   "启动/停止程序；遇到超出 allow 列表的请求，直接回音说明「需要二级密码」。\n")
    return prompt


# ── 待办 / 锁死状态（持久化到 remote_pending.json，进程重启不丢） ──
def remote_state_load(path=None):
    path = path or REMOTE_STATE_PATH
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            st = json.load(f)
        if not isinstance(st, dict):
            st = {}
    except Exception:
        st = {}
    st.setdefault("pending", {})
    st.setdefault("lockouts", {})
    if not isinstance(st["pending"], dict):
        st["pending"] = {}
    if not isinstance(st["lockouts"], dict):
        st["lockouts"] = {}
    return st


def remote_state_save(state, path=None):
    path = path or REMOTE_STATE_PATH
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
        return True
    except Exception as exc:
        log_warn("[REMOTE] 待办状态写入失败: %s" % exc)
        return False


def remote_plan(chat, text, remote_cfg, state, now=None):
    """远程指令状态机（纯决策，不执行 RPC、不发消息）。返回 plan dict。

    plan.action:
      chat_only      -> 不含触发词（或纯密码但无待办）：完全当普通聊天，走 genreply
      need_password  -> 触发词命中但没密码：建待办 + 回「先核对密码」
      wrong_password -> 有待办但应答不是正确密码：累计次数 + 回「密码不对，还有 N 次」
      lockout        -> 错满 maxWrongAttempts：清待办 + 锁死 lockoutSeconds + 回「错误次数太多」
      wake           -> 密码通过：转告 Agent（plan.prompt 已带授权级别 + allow 清单）
    """
    now = float(now if now is not None else time.time())
    cfg = remote_cfg or {}
    levels = remote_levels(cfg)
    trigger = str(cfg.get("triggerWord") or "小耳朵")
    ttl = float(cfg.get("pendingTtlSeconds") or 180)
    max_wrong = int(cfg.get("maxWrongAttempts") or 3)
    lock_sec = float(cfg.get("lockoutSeconds") or 900)
    pending_map = state.setdefault("pending", {})
    lock_map = state.setdefault("lockouts", {})
    raw = str(text or "").strip()
    plan = {"action": "chat_only", "level": "", "passwordOk": False, "attempts": 0,
            "command": raw, "reply_text": "", "prompt": "", "audit": False}
    if not cfg.get("enabled"):
        return plan
    # 0) 锁死检查
    lock = lock_map.get(chat) or {}
    if lock and float(lock.get("until") or 0) > now:
        left = int(max(0.0, float(lock["until"]) - now))
        plan.update({"action": "lockout", "attempts": -1, "audit": True,
                     "reply_text": "错误次数太多，先停了（约 %d 分钟内不再接受指令）"
                                   % max(1, (left + 59) // 60)})
        return plan
    if lock:
        lock_map.pop(chat, None)
    # 1) 过期待办清理
    pend = pending_map.get(chat) or {}
    if pend and (now - float(pend.get("createdAt") or 0)) > ttl:
        pending_map.pop(chat, None)
        pend = {}
    has_trigger = bool(trigger and trigger in raw)
    # 2) 一条到位：含触发词 + 含任一级别密码
    if has_trigger:
        level, token = find_password_in_text(raw, cfg)
        if level:
            cmd = redact_token(raw, token)
            risk = is_high_risk_text(raw)
            if level == "L1" and risk:
                pending_map[chat] = {"text": cmd, "createdAt": now, "attempts": 0,
                                     "levelHint": "L2", "needLevelReason": risk}
                plan.update({"action": "need_password", "level": "L1", "passwordOk": True,
                             "attempts": 0, "command": cmd, "audit": True,
                             "reply_text": "这个要二级密码（L2）才能做（命中高危动作：%s）。"
                                           "把二级密码单独发我（3 分钟内有效）" % risk})
                return plan
            pending_map.pop(chat, None)
            lock_map.pop(chat, None)
            plan.update({"action": "wake", "level": level, "passwordOk": True, "attempts": 0,
                         "command": cmd, "audit": True,
                         "prompt": build_remote_prompt(cfg, chat, cmd, level)})
            return plan
    # 3) 有未过期待办：这条消息当「密码应答」
    if pend:
        attempt_before = int(pend.get("attempts") or 0)
        body = raw
        candidate_level = ""
        for name, lv in levels.items():
            if verify_level_password(body, lv):
                candidate_level = name
                break
        if candidate_level:
            cmd = redact_known_password(str(pend.get("text") or ""))
            risk = is_high_risk_text(cmd)
            if candidate_level == "L1" and risk:
                pend["attempts"] = attempt_before
                pending_map[chat] = pend
                plan.update({"action": "need_password", "level": "L1", "passwordOk": True,
                             "attempts": attempt_before, "command": cmd, "audit": True,
                             "reply_text": "这个要二级密码（L2）才能做（命中高危动作：%s）。"
                                           "把二级密码单独发我（3 分钟内有效）" % risk})
                return plan
            pending_map.pop(chat, None)
            lock_map.pop(chat, None)
            plan.update({"action": "wake", "level": candidate_level, "passwordOk": True,
                         "attempts": attempt_before, "command": cmd, "audit": True,
                         "prompt": build_remote_prompt(cfg, chat, cmd, candidate_level)})
            return plan
        # 不是密码（或密码不对）-> 累计错误次数
        attempts = attempt_before + 1
        if attempts >= max_wrong:
            pending_map.pop(chat, None)
            lock_map[chat] = {"until": now + lock_sec, "reason": "wrong-password",
                              "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            plan.update({"action": "lockout", "attempts": attempts, "audit": True,
                         "reply_text": "错误次数太多，先停了（约 %d 分钟内不再接受指令）"
                                       % int(lock_sec // 60)})
            return plan
        pend["attempts"] = attempts
        pending_map[chat] = pend
        plan.update({"action": "wrong_password", "attempts": attempts, "audit": True,
                     "reply_text": "密码不对，还有 %d 次机会" % (max_wrong - attempts)})
        return plan
    # 4) 无待办：有触发词没密码 -> 建待办；否则完全普通聊天
    if has_trigger:
        cmd = redact_known_password(raw)
        pending_map[chat] = {"text": cmd, "createdAt": now, "attempts": 0, "levelHint": ""}
        plan.update({"action": "need_password", "attempts": 0, "command": cmd, "audit": True,
                     "reply_text": "要做这个得先核对密码，把密码发我（3 分钟内有效）"})
        return plan
    looks_password = bool(re.fullmatch(r"[A-Za-z0-9]{4,}", raw or ""))
    plan.update({"action": "chat_only", "audit": looks_password, "command": raw})
    return plan


# ── 审计（logs/remote-YYYYMMDD.jsonl，每行一条 JSON） ──
def remote_audit_path(remote_cfg):
    """审计文件路径（默认 logs/remote-YYYYMMDD.jsonl，YYYYMMDD 替换成当天日期）。"""
    pattern = str((remote_cfg or {}).get("auditFile") or "logs/remote-YYYYMMDD.jsonl")
    pattern = pattern.replace("YYYYMMDD", time.strftime("%Y%m%d"))
    if not os.path.isabs(pattern):
        pattern = os.path.join(BASE_DIR, pattern)
    return pattern


def append_remote_audit(remote_cfg, record):
    """写远程指令审计 JSONL（UTF-8 无 BOM）。

    🔴 绝不写密码明文、也不写密码哈希；调用方传进来的 incoming 已脱敏。
    必须包含：at/chat/level/passwordOk/attempts/incoming/action/targetSessionId/promptChars
    """
    path = remote_audit_path(remote_cfg)
    try:
        os.makedirs(os.path.dirname(path) or BASE_DIR, exist_ok=True)
        item = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "ts": time.time()}
        item.update(record or {})
        item.setdefault("chat", "")
        item.setdefault("level", "-")
        item.setdefault("passwordOk", False)
        item.setdefault("attempts", 0)
        item.setdefault("incoming", "")
        item.setdefault("action", "")
        item.setdefault("targetSessionId", "")
        item.setdefault("promptChars", 0)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
        return path
    except Exception as exc:
        log_warn("[REMOTE] 审计写入失败: %s" % exc)
        return ""


# ─────────────────────────── watchdog core ───────────────────────────


class Watchdog:
    def __init__(self):
        set_dpi_aware()
        self.config = load_config()
        self.state = load_state()
        self.baseline_text = None
        self.baseline_geom = None
        self.baseline_ts = 0.0
        self.last_trigger_ts = 0.0
        self.prev_badges = 0
        self.prev_unread = False
        self.rail_baseline = None
        self.prev_rail_white = 0
        self.first_scan = True
        # 未读指纹去重状态（防同一条未读反复唤醒/回复）
        self.last_fp = None
        self.last_fp_info = None
        self.pending_fp = None
        self.pending_count = 0
        self.processed_fp = str(self.state.get("processed_fingerprint") or "")
        self._tesseract = None
        self._last_black_raw = None
        self._last_rowwarn = 0.0
        self._taskbar_scale = None
        self.wake_times = [t for t in self.state.get("wake_times", []) if isinstance(t, (int, float))]
        self.rpc = DshRpc(self.config)
        self.stop_requested = False
        self._cfg_mtime = self._file_mtime(CONFIG_PATH)
        self._last_win_missing_log = 0.0
        self._warned_no_window = False
        # 窗口不可用状态（同状态只报一次，之后每 5 分钟复述）
        self._unavail_reason = None
        self._unavail_detail = ""
        self._unavail_since = 0.0
        self._unavail_last_log = 0.0
        # 候选窗口枚举日志（状态变化时打印）
        self._last_cand_sig = None
        self._last_cand_dump_ts = 0.0
        # 抓取方式（变化时打印）
        self._last_capture_source = None
        # 忙闲守护：同一 (指纹, 原因) 只记一次日志，避免游戏期间刷屏
        self._last_busy_key = None
        # 【第四次整改】只有 reply.py 真发送成功才写 processed_fingerprint：
        #   awaiting_send = 已唤醒、正在等 reply.py 往 reply_state.json 追加 sent 记录
        #   retry_state   = 失败后保留的待处理状态（重试次数 / 下次重试时间）
        self.awaiting_send = None
        self.retry_state = self._load_retry_state()
        self.gave_up = list(self.state.get("gave_up") or [])
        # 重启前若正处「已唤醒、等 reply.py 发送确认」状态：转成待重试，绝不猜成已处理
        aw = self.state.get("awaiting_send")
        if isinstance(aw, dict) and aw.get("fp") and self.retry_state is None:
            self.retry_state = {"fp": str(aw.get("fp")),
                                "retries": 1,
                                "next_try_at": time.time() + self._retry_after_seconds(),
                                "reason": "restart-before-send-confirmed",
                                "displays": []}
            self.state["retry_pending"] = dict(self.retry_state)
            self.state.pop("awaiting_send", None)
            save_state(self.state)
            log_warn("启动时发现上次唤醒还没等到 reply.py 发送确认 -> 转为待重试"
                     "（不写 processed_fingerprint）fp=%s" % self.retry_state["fp"])

    @staticmethod
    def _file_mtime(path):
        try:
            return os.path.getmtime(path)
        except Exception:
            return None

    # ---- stop / config ----
    def stop_file_present(self):
        return os.path.exists(STOP_PATH)

    # ---- 熔断（2026-10-27 第一阶段） ----
    def _breaker_cfg(self):
        return (self.config or {}).get("breaker") or {}

    def breaker_register_failure(self, reason, kill=True):
        """记一次「真发送失败」（update breaker_state.json）。

        连续失败达到 breaker.consecutiveFailures -> 熔断：写 breaker_tripped.json -> 写 STOP
        -> 气泡通知 -> 结束 watcher 自身进程（熔断后不自启，须人工 toggle.ps1 on）。
        返回 {"tripped": bool, "count": n, "limit": n}。
        """
        br = self._breaker_cfg()
        st = breaker_state_load()
        st["consecutiveFailures"] = int(st.get("consecutiveFailures") or 0) + 1
        st["lastFailureAt"] = _ts()
        st["lastFailureTs"] = round(time.time(), 3)
        st["lastReason"] = str(reason or "")[:300]
        breaker_state_save(st)
        try:
            limit = max(1, int(br.get("consecutiveFailures") or 3))
        except Exception:
            limit = 3
        count = int(st["consecutiveFailures"])
        log_warn("[BREAKER] 发送失败计数 +1 -> 连续失败 %d/%d（原因=%s）" % (
            count, limit, st["lastReason"][:160]))
        if not br.get("enabled", True):
            log_info("[BREAKER] 熔断已禁用（breaker.enabled=false）-> 只计数、不熔断")
            return {"tripped": False, "count": count, "limit": limit}
        if count >= limit:
            self._trip_breaker(st, limit, kill=kill)
            return {"tripped": True, "count": count, "limit": limit}
        return {"tripped": False, "count": count, "limit": limit}

    def breaker_register_success(self):
        """成功真发一次 -> 连续失败计数清零（resetOnSuccess=false 则保留）。"""
        br = self._breaker_cfg()
        if not br.get("resetOnSuccess", True):
            return
        st = breaker_state_load()
        prev = int(st.get("consecutiveFailures") or 0)
        if prev <= 0:
            return
        st["consecutiveFailures"] = 0
        st["lastSuccessAt"] = _ts()
        breaker_state_save(st)
        log_info("[BREAKER] 发送成功 -> 连续失败计数清零（此前连续失败 %d 次；lastFailureAt=%s 保留备查）"
                 % (prev, st.get("lastFailureAt") or "-"))

    def breaker_reset_after_manual_enable(self):
        """人工恢复检测：上次熔断后有人执行了 toggle.ps1 on（STOP 已删、进程重新起）-> 计数清零。

        否则熔断态（consecutiveFailures>=阈值）会残留，重现后的第 1 次失败就又熔断。
        熔断记录 breaker_tripped.json 保留备查（不删）。
        """
        try:
            if not os.path.exists(BREAKER_TRIPPED_PATH):
                return
            if os.path.exists(STOP_PATH):
                return
            st = breaker_state_load()
            if int(st.get("consecutiveFailures") or 0) <= 0:
                return
            log_info("[BREAKER] 检测到上次熔断已被人工恢复（breaker_tripped.json 在、STOP 已删除）"
                     "-> 连续失败计数清零（此前 %s），重新计数" % st.get("consecutiveFailures"))
            st["consecutiveFailures"] = 0
            st["lastManualResetAt"] = _ts()
            breaker_state_save(st)
        except Exception as exc:
            log_warn("[BREAKER] 人工恢复检测异常（忽略）: %s" % exc)

    def _trip_breaker(self, st, limit, kill=True):
        """连续失败达标 -> 熔断（写记录 + STOP + 气泡 + 结束自身进程）。"""
        rec = {"trippedAt": _ts(), "trippedTs": round(time.time(), 3),
               "reason": str(st.get("lastReason") or "")[:300],
               "consecutiveFailures": int(st.get("consecutiveFailures") or 0),
               "threshold": int(limit),
               "source": "reply-send-failure",
               "stopFile": STOP_PATH,
               "breakerStateFile": BREAKER_STATE_PATH,
               "note": "熔断后不自启：STOP 存在 + 熔断记录保留，恢复须人工排查后执行 toggle.ps1 on"}
        try:
            with open(BREAKER_TRIPPED_PATH, "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=2)
                f.write("\n")
        except Exception as exc:
            log_error("[PANIC] breaker 写 breaker_tripped.json 失败: %s" % exc)
        append_panic_log("breaker",
                         "连续发送失败 %d 次达阈值 %d，最后原因=%s" % (
                             rec["consecutiveFailures"], limit, rec["reason"]),
                         extra=rec)
        log_error("[PANIC] breaker 连续发送失败 %d 次（阈值 %d）-> 已写 %s + STOP，"
                  "watcher 进程即将退出；最后原因=%s" % (
                      rec["consecutiveFailures"], limit, BREAKER_TRIPPED_PATH,
                      rec["reason"][:160]))
        write_stop_file("breaker: %s" % rec["reason"][:160])
        if (self.config.get("panic") or {}).get("toast", True):
            try:
                self._notify_toast(
                    "小耳朵已熔断急停",
                    "连续 %d 次发送失败，已写 STOP 并退出；请检查微信窗口/日志，处理后重新打开"
                    % rec["consecutiveFailures"])
            except Exception as exc:
                log_warn("[PANIC] 熔断气泡提示失败（忽略）: %s" % exc)
        if kill:
            try:
                self._release_lock()
            except Exception:
                pass
            log_error("[PANIC] 熔断后不自启：watcher 进程 pid=%s 立即退出（恢复须人工 toggle.ps1 on）"
                      % os.getpid())
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except Exception:
                pass
            os._exit(0)

    # ---- 急停（2026-10-27 第一阶段，三处入口：快捷方式 / 热键 / 微信口令） ----
    def panic_hotkey_down(self):
        """只读检测全局急停热键 Ctrl+Alt+Shift+S 是否四键同按（GetAsyncKeyState 最高位）。"""
        try:
            u = ctypes.windll.user32
            return all(bool(int(u.GetAsyncKeyState(v)) & 0x8000)
                       for v in (VK_CONTROL, VK_MENU, VK_SHIFT, VK_S))
        except Exception:
            return False

    def hotkey_watch_sleep(self, seconds):
        """休眠期间仍按 <=0.2s 轮询急停热键，保证 Ctrl+Alt+Shift+S 不会被 3 秒采样间隔漏掉。"""
        end = time.time() + max(0.0, float(seconds))
        while True:
            if (self.config.get("panic") or {}).get("enabled", True) and self.panic_hotkey_down():
                self.do_panic("hotkey", "检测到全局热键 %s 四键同按（休眠轮询期间）"
                              % PANIC_HOTKEY_LABEL)
            left = end - time.time()
            if left <= 0:
                return
            time.sleep(min(0.2, max(0.01, left)))

    def do_panic(self, source, detail="", kill=True):
        """急停统一出口：[PANIC] 日志 + 审计 + 写 STOP + 气泡 -> 结束 watcher 自身进程。"""
        append_panic_log(source, detail)
        log_error("[PANIC] source=%s %s -> 写 STOP 急停文件" % (source, detail))
        ok = write_stop_file("%s %s" % (source, detail))
        if (self.config.get("panic") or {}).get("toast", True):
            try:
                self._notify_toast("小耳朵已急停", "来源=%s，已写 STOP 文件，watcher 退出" % source)
            except Exception as exc:
                log_warn("[PANIC] 急停气泡提示失败（忽略）: %s" % exc)
        if kill:
            try:
                self._release_lock()
            except Exception:
                pass
            log_error("[PANIC] 急停生效：source=%s -> watcher 进程 pid=%s 立即退出（STOP 已写=%s）"
                      % (source, os.getpid(), ok))
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except Exception:
                pass
            os._exit(0)
        return {"ok": bool(ok), "source": source}

    def reload_config_if_changed(self):
        mtime = self._file_mtime(CONFIG_PATH)
        if mtime != self._cfg_mtime:
            self._cfg_mtime = mtime
            old = self.config
            self.config = load_config()
            self.rpc = DshRpc(self.config)
            wake_on = bool((self.config.get("wake") or {}).get("enabled"))
            log_info("config.json 已变更: enabled=%s autoSend=%s interval=%ss 唤醒Agent=%s "
                     "（方案A：进程内直接调 API，默认不等于唤醒）" % (
                         self.config["enabled"], self.config["autoSend"],
                         self.config["intervalSeconds"],
                         "enabled(旧通道，未在流程中使用)" if wake_on else "disabled"))
            return True
        return False

    # ---- quiet hours / rate limit ----
    def in_quiet_hours(self):
        qh = self.config.get("quietHours") or []
        if not (isinstance(qh, (list, tuple)) and len(qh) == 2):
            return False
        try:
            start, end = int(qh[0]), int(qh[1])
        except Exception:
            return False
        hour = _now().hour
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def wakes_last_hour(self):
        cutoff = time.time() - 3600
        self.wake_times = [t for t in self.wake_times if t >= cutoff]
        return len(self.wake_times)

    # ---- 发送确认 / 失败重试（2026-10-25 第四次整改：只有真发出去才算已处理） ----
    def _load_retry_state(self):
        rs = self.state.get("retry_pending")
        if not isinstance(rs, dict) or not rs.get("fp"):
            return None
        try:
            return {"fp": str(rs.get("fp")),
                    "retries": int(rs.get("retries") or 0),
                    "next_try_at": float(rs.get("next_try_at") or 0.0),
                    "reason": str(rs.get("reason") or ""),
                    "displays": list(rs.get("displays") or [])}
        except Exception:
            return None

    def _retry_after_seconds(self):
        try:
            return max(1.0, float(self.config.get("retryAfterSeconds", 60) or 60))
        except Exception:
            return 60.0

    def _max_retry_attempts(self):
        try:
            return max(0, int(self.config.get("maxRetryAttempts", 3) or 3))
        except Exception:
            return 3

    def _reply_confirm_timeout(self):
        try:
            return max(5.0, float(self.config.get("replyConfirmTimeoutSeconds", 180) or 180))
        except Exception:
            return 180.0

    def _clear_retry(self, why=""):
        if self.retry_state:
            if why:
                log_info("%s -> 清空待重试状态 fp=%s" % (why, self.retry_state.get("fp")))
            self.retry_state = None
            self.state.pop("retry_pending", None)
            save_state(self.state)

    def _schedule_retry(self, filt, rows, reason, info=None, delay=None, count_retry=True):
        """失败（busy / 冷却 / wake 失败 / 等不到发送确认）-> 不写 processed_fingerprint。

        保留待处理状态，retryAfterSeconds 后重试；同一条未读最多重试 maxRetryAttempts 次，
        用满后标记 gave-up（只记日志 + state.json 留痕，绝不写 processed_fingerprint）。
        count_retry=False 用于「静默时段」这类纯延后（不计入重试次数、不会 gave-up）。
        """
        fp = str((filt or {}).get("digest") or "")
        if not fp:
            return
        max_retry = self._max_retry_attempts()
        used = 0
        if self.retry_state and self.retry_state.get("fp") == fp:
            used = int(self.retry_state.get("retries") or 0)
        displays = [r.get("display") or "?" for r in (rows or [])]
        if count_retry and used >= max_retry:
            # 重试次数用满 -> gave-up
            item = {"fp": fp, "at": _ts(), "reason": reason,
                    "retries": used, "displays": displays}
            self.gave_up.insert(0, item)
            self.state["gave_up"] = self.gave_up[:20]
            self.state.pop("retry_pending", None)
            self.retry_state = None
            save_state(self.state)
            log_warn("gave-up: 同一条未读已重试 %d 次仍未成功（最后原因=%s）-> 停止自动重试；"
                     "未写 processed_fingerprint（未读不变则不再处理，等未读变化或人工处理）fp=%s displays=%s"
                     % (used, reason, fp, displays))
            return
        if delay is None:
            delay = self._retry_after_seconds()
        delay = max(0.5, float(delay))
        next_try = time.time() + delay
        retries = used + 1 if count_retry else used
        self.retry_state = {"fp": fp, "retries": retries, "next_try_at": next_try,
                            "reason": reason, "displays": displays}
        self.state["retry_pending"] = dict(self.retry_state)
        save_state(self.state)
        if count_retry:
            log_warn("保留待处理状态（不写 processed_fingerprint）: reason=%s fp=%s，%.0fs 后重试"
                     "（第 %d/%d 次重试）" % (reason, fp, delay, used + 1, max_retry))
        else:
            log_warn("保留待处理状态（不写 processed_fingerprint）: reason=%s fp=%s，延后 %.0fs（%.1f 小时）"
                     "再试，不计入重试次数" % (reason, fp, delay, delay / 3600.0))

    def _quiet_seconds_left(self):
        """当前静默时段还剩多少秒（不在静默时段返回 0）。"""
        qh = self.config.get("quietHours") or []
        if not (isinstance(qh, (list, tuple)) and len(qh) == 2):
            return 0.0
        try:
            start, end = int(qh[0]), int(qh[1])
        except Exception:
            return 0.0
        if start == end:
            return 0.0
        now = _now()
        hour = now.hour
        in_quiet = (start <= hour < end) if start < end else (hour >= start or hour < end)
        if not in_quiet:
            return 0.0
        target = now.replace(minute=0, second=0, microsecond=0)
        if start < end:
            target = target.replace(hour=end)
        elif hour >= start:
            target = (target + timedelta(days=1)).replace(hour=end)
        else:
            target = target.replace(hour=end)
        return max(0.0, (target - now).total_seconds())

    def _retry_due(self, digest, now=None):
        rs = self.retry_state
        if not rs or not digest or rs.get("fp") != digest:
            return False
        if self.awaiting_send:
            return False
        return float(now if now is not None else time.time()) >= float(rs.get("next_try_at") or 0.0)

    @staticmethod
    def _reply_sent_records():
        """读 reply_state.json 的 sent 列表（reply.py 真发送成功后才追加）。"""
        try:
            with open(REPLY_STATE_PATH, "r", encoding="utf-8") as f:
                doc = json.load(f)
            return list((doc or {}).get("sent") or [])
        except Exception:
            return []

    def _find_confirmed_send(self, since, rows):
        """在 reply_state.json 里找 trigger 之后「真的发出去」的记录；没有返回 None。"""
        chats = set()
        for r in (rows or []):
            for key in ("display", "matched"):
                v = normalize_name(str(r.get(key) or ""))
                if v:
                    chats.add(v)
        for item in self._reply_sent_records():
            try:
                ts = float(item.get("ts") or 0)
            except Exception:
                continue
            if ts < float(since) - 2.0:
                continue  # 只认本次唤醒之后新增的发送记录（2 秒容差）
            if chats:
                chat = normalize_name(str(item.get("chat") or ""))
                if chat and not any(chat == c or chat in c or c in chat for c in chats):
                    continue
            return item
        return None

    def _begin_awaiting_send(self, filt, rows, payload, info, wake_res):
        """wake 成功 -> 进入「等 reply.py 确认发送」状态，成功前绝不写 processed_fingerprint。"""
        timeout = self._reply_confirm_timeout()
        now = time.time()
        self.awaiting_send = {"fp": str((filt or {}).get("digest") or ""),
                              "since": now, "deadline": now + timeout,
                              "rows": list(rows or []), "filtered": filt,
                              "payload": payload, "info": info, "wake": wake_res}
        self.state["awaiting_send"] = {"fp": self.awaiting_send["fp"],
                                       "since": int(now), "timeout": timeout}
        save_state(self.state)
        log_info("wake 成功但【尚未确认发送】-> 先不写 processed_fingerprint；"
                 "等 reply.py 把成功记录写进 reply_state.json（最多 %.0fs）fp=%s"
                 % (timeout, self.awaiting_send["fp"]))

    def _check_awaiting_send(self):
        """每轮扫描调用：确认 reply.py 真的发出去了，才把指纹写成已处理。"""
        aw = self.awaiting_send
        if not aw:
            return
        item = self._find_confirmed_send(aw.get("since"), aw.get("rows"))
        if item:
            log_info("[发送确认] reply.py 真的发出去了（reply_state.json 新增 sent: "
                     "chat=%r text=%r at=%s）-> 现在才写 processed_fingerprint fp=%s"
                     % (item.get("chat"), (item.get("text") or "")[:80],
                        item.get("at"), aw.get("fp")))
            try:
                self._persist_processed(aw.get("info") or {}, aw.get("rows") or [],
                                        aw.get("payload") or {}, aw.get("filtered") or {},
                                        send_record=item)
            finally:
                self.awaiting_send = None
                self.state.pop("awaiting_send", None)
                self._clear_retry("发送已确认")
            return
        now = time.time()
        if now >= float(aw.get("deadline") or 0):
            log_warn("等待 reply.py 发送确认超时（%.0fs 内 reply_state.json 没有新增成功发送记录）"
                     "-> 不算已处理，按失败重试；fp=%s"
                     % (now - float(aw.get("since") or now), aw.get("fp")))
            rowz = aw.get("rows") or []
            filt = aw.get("filtered") or {"digest": aw.get("fp")}
            self.awaiting_send = None
            self.state.pop("awaiting_send", None)
            self._schedule_retry(filt, rowz, "send-not-confirmed", aw.get("info"))

    # ---- signal ----
    def write_signal(self, payload):
        tmp = SIGNAL_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, SIGNAL_PATH)

    def save_snapshot(self, img):
        os.makedirs(SHOT_DIR, exist_ok=True)
        stamp = _now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(SHOT_DIR, "wechat-%s.png" % stamp)
        try:
            img.save(path)
            return path
        except Exception as exc:
            log_warn("截图保存失败: %s" % exc)
            return ""

    # ---- sound ----
    def play_sound(self):
        if not self.config.get("sound", True):
            return
        try:
            import winsound
            winsound.Beep(880, 120)
            time.sleep(0.08)
            winsound.Beep(1175, 140)
        except Exception:
            pass

    # ---- trigger ----
    def trigger(self, img, reason, badges, extra=None, whitelist_info=None, digest=""):
        now = _now()
        screenshot = self.save_snapshot(img)
        cfg = self.config
        quiet = self.in_quiet_hours()
        wl_rows = list((whitelist_info or {}).get("rows") or [])
        payload = {
            "version": VERSION,
            "detectedAt": int(time.time() * 1000),
            "detectedAtLocal": now.strftime("%Y-%m-%d %H:%M:%S"),
            "reason": reason,
            "unreadBadges": badges,
            "screenshot": screenshot,
            "windowTitle": (extra or {}).get("window_title", ""),
            "autoSend": bool(cfg.get("autoSend")),
            "enabled": bool(cfg.get("enabled")),
            "quietHours": cfg.get("quietHours"),
            "whitelist": cfg.get("whitelist") or [],
            "blacklist": cfg.get("blacklist") or [],
            "quietMode": quiet,
            "pid": os.getpid(),
            "fingerprint": digest or ((extra or {}).get("fingerprint") or {}).get("digest", ""),
            "whitelistMatch": {
                "anyHit": bool((whitelist_info or {}).get("any_hit")),
                "rows": wl_rows,
            },
            # 方案A：唤醒 Agent 这条链已停用（保留字段方便老消费者辨认）
            "wake": {"enabled": False, "skipped": "wake-disabled",
                     "note": "方案A：进程内直接调 API 生成回复，不再唤醒 Agent"},
        }
        # 1) signal.json 始终先落盘
        self.write_signal(payload)
        log_info("检测到新消息提醒: reason=%s badges=%s screenshot=%s signal=%s" % (
            reason, badges, screenshot or "(无)", SIGNAL_PATH))
        self.play_sound()

        # 2) 静默时段 / 速率限制 -> 只记录不发送（保留待处理，到点再试）
        if quiet:
            payload["reply"] = {"ok": False, "skipped": "quiet-hours", "retry": True}
            self.write_signal(payload)
            log_info("处于静默时段 %s，仅记录不发送（保留待处理）" % (cfg.get("quietHours"),))
            return payload
        limit = int(cfg.get("maxRepliesPerHour") or 0)
        if limit > 0 and self.wakes_last_hour() >= limit:
            payload["reply"] = {"ok": False, "skipped": "rate-limit", "retry": True}
            self.write_signal(payload)
            log_warn("超过 maxRepliesPerHour=%d（最近 1 小时已发送 %d 次），仅记录不发送" % (
                limit, self.wakes_last_hour()))
            return payload
        # 3) 白名单门禁：未命中 → 只记录，绝不发送
        if whitelist_info is not None and not whitelist_info.get("any_hit"):
            payload["reply"] = {"ok": False, "skipped": "whitelist-miss", "retry": False}
            self.write_signal(payload)
            log_info("skip: whitelist-miss -> 只记录不发送（截图与 signal.json 已保存；不会回复）")
            return payload

        # 4) 方案A：进程内调 API 生成回复 -> 发送护栏 -> reply.py 真发送
        result = self.generate_and_send(payload, wl_rows, extra or {}, digest)
        payload["reply"] = result
        self.write_signal(payload)
        return payload

    # ---------- 方案A：生成 + 护栏 + 发送 ----------
    def _recent_message_text(self, info, row):
        """可选：OCR 会话行里的「最近消息」预览，返回 (text, source)。"""
        gen_cfg = self.config.get("gen") or {}
        if not gen_cfg.get("ocrPreview", True):
            return "", "disabled"
        if not info or info.get("img") is None or not info.get("geom"):
            return "", "no-image"
        ocr_cfg = self.config.get("ocr") or {}
        if not ocr_cfg.get("enabled", True):
            return "", "ocr-disabled"
        if self._tesseract is None:
            self._tesseract = find_tesseract(ocr_cfg.get("tesseract", ""))
        if not self._tesseract:
            log_warn("%s OCR 不可用（找不到 tesseract）-> 只把会话名告诉模型" % genreply.GEN_TAG)
            return "", "no-tesseract"
        row_idx = int(row.get("row", 0) or 0)
        try:
            txt = _ocr_row_preview(info["img"], info["geom"], row_idx,
                                   self._tesseract, ocr_cfg)
        except Exception as exc:
            log_warn("%s 预览 OCR 失败: %s" % (genreply.GEN_TAG, exc))
            return "", "ocr-error"
        if txt:
            log_info("%s OCR 对方最近消息: %r（row=%s）" % (genreply.GEN_TAG, txt, row_idx))
            return txt, "ocr"
        log_info("%s OCR 未取到对方消息文本（row=%s）-> 只告诉模型「对方发来了新消息」"
                 % (genreply.GEN_TAG, row_idx))
        return "", "empty"

    def _call_reply_py(self, chat, text):
        """调用现成的 reply.py 真发送（只调用，不改它的发送逻辑）。"""
        gen_cfg = self.config.get("gen") or {}
        timeout = float(gen_cfg.get("sendTimeoutSeconds") or 120)
        python = sys.executable or "python"
        cmd = [python, REPLY_PY_PATH, "--chat", chat, "--text", text]
        since = time.time()
        log_info("%s 调用 reply.py: chat=%r text=%r timeout=%.0fs" % (
            "[REPLY]", chat, text, timeout))
        t0 = time.time()
        try:
            proc = subprocess.run(cmd, cwd=BASE_DIR, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            log_error("[REPLY] reply.py 超时 %.0fs（已强杀）-> 不确认发送" % timeout)
            return {"ok": False, "error": "reply-timeout", "elapsed": time.time() - t0}
        out = (proc.stdout or "").strip()
        lines = out.splitlines()
        if lines:
            log_info("[REPLY] reply.py 输出尾部(%d 行):\n%s" % (
                min(len(lines), 14), "\n".join(lines[-14:])))
        err = (proc.stderr or "").strip()
        if err:
            log_warn("[REPLY] reply.py stderr: %s" % err[-800:])
        parsed = None
        for line in reversed(lines):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    parsed = json.loads(line)
                    break
                except Exception:
                    continue
        confirmed = self._find_confirmed_send(
            since - 2.0, [{"display": chat, "matched": chat}])
        rc = proc.returncode
        ok = bool(rc == 0 and (parsed or {}).get("ok", True) and confirmed)
        if ok:
            log_info("[REPLY] stage=done 已发送 chat=%r text=%r（reply.py rc=%s）" % (
                chat, text, rc))
        else:
            log_error("[REPLY] 未确认发送: rc=%s parsed=%s confirmed=%s" % (
                rc, json.dumps(parsed or {}, ensure_ascii=False)[:300],
                bool(confirmed)))
            user_msg = (parsed or {}).get("userMessage")
            if user_msg:
                # 2026-10-26：会话行选择歧义/近似 -> reply.py 已安全 abort，给了要回给用户的话；
                # 这里只记日志，由上层 generate_and_send 做一次 best-effort 转告。
                log_warn("[REPLY] 行选择安全 abort，需要回用户: %r" % user_msg)
        return {"ok": ok, "rc": rc, "parsed": parsed or {}, "confirmed": confirmed,
                "elapsed": round(time.time() - t0, 2)}

    def _remote_forward(self, chat, incoming, incoming_src=""):
        """远程指令转告（微信 -> 本机 Agent，两级密码 + 挑战-应答）。不是远程指令返回 None。

        决策由 remote_plan() 完成（纯状态机，便于 --remote-selftest 离线验证）；这里负责执行：
          * need_password / wrong_password / lockout -> 用 reply.py 回一条文本（不转告）
          * wake -> 组装带「授权级别 + allow 清单」的 prompt，DSH session/prompt 转告；
                    成功返回 sendConfirmed=True（watcher 记 processed），**不再走 genreply、不自己发文本**
          * chat_only -> 返回 None，维持原普通闲聊回复流程
        🔴 绝不发"收到我去办"回执：sendguard 同会话 30 秒限流会拦掉 Agent 随后发的真实结果。
        """
        cfg = self.config
        # 🔴 2026-10-27 第一阶段（用户拍板）：微信口令急停 —— 白名单来信同时含触发词「小耳朵」
        #    和「急停/停止」-> **立即写 STOP + 退出**（不等密码/转告；第一阶段只写逻辑，不发"已急停"回执，
        #    回执的真机验证留第二阶段）。放在最前面，避免「停止」这类高危词先被 remote_plan 判成聊天。
        incoming_s = str(incoming or "")
        panic_cfg = cfg.get("panic") or {}
        remote_cfg = cfg.get("remote") or {}
        trigger_word = str(remote_cfg.get("triggerWord") or "小耳朵")
        if panic_cfg.get("enabled", True) and trigger_word and trigger_word in incoming_s:
            for kw in (panic_cfg.get("wechatKeywords") or ["急停", "停止"]):
                kw = str(kw or "")
                if kw and kw in incoming_s:
                    self.do_panic(
                        "wechat-command",
                        "微信口令急停 chat=%r 命中关键词=%r 来信=%r（来源=白名单来信）"
                        % (chat, kw, incoming_s[:100]))
                    return {"ok": False, "skipped": "panic", "retry": False}
        if not remote_cfg.get("enabled"):
            return None
        state = remote_state_load()
        plan = remote_plan(chat, incoming, remote_cfg, state)
        action = str(plan.get("action") or "chat_only")
        level = str(plan.get("level") or "-")
        attempts = int(plan.get("attempts") if plan.get("attempts") is not None else 0)
        target_sid = str(remote_cfg.get("targetSessionId") or "").strip()
        target_title = str(remote_cfg.get("targetSessionTitle") or "").strip()
        base_audit = {
            "chat": chat,
            "level": level,
            "passwordOk": bool(plan.get("passwordOk")),
            "attempts": attempts,
            "incoming": redact_known_password(str(incoming)[:1000]),
            "targetSessionId": target_sid,
            "promptChars": len(str(plan.get("prompt") or "")),
        }
        if action == "chat_only":
            if plan.get("audit"):
                append_remote_audit(remote_cfg, dict(base_audit, action="chat_only"))
            return None
        # 状态机产生了变化（建待办/累计次数/锁死/清待办）-> 持久化
        remote_state_save(state)
        if action in ("need_password", "wrong_password", "lockout"):
            reply_text = str(plan.get("reply_text") or "")
            log_info("[REMOTE] action=%s level=%s attempts=%s -> 回文本、不转告：%r" % (
                action, level, attempts, reply_text[:160]))
            send = self._call_reply_py(chat, reply_text)
            append_remote_audit(remote_cfg, dict(base_audit, action=action,
                                                 replyOk=bool(send.get("ok"))))
            return {"ok": False, "skipped": "remote-" + action, "retry": False,
                    "text": reply_text,
                    "remote": {"action": action, "level": level, "attempts": attempts,
                               "replyOk": bool(send.get("ok"))}}
        # action == wake：转告 Agent
        prompt = str(plan.get("prompt") or "")
        cmd_text = str(plan.get("command") or "")
        log_info("[REMOTE] action=wake level=%s -> 转告 Agent: chat=%r target=%s(%r) 指令=%r "
                 "allow=%s" % (level, chat, target_sid or "-", target_title, cmd_text[:160],
                               ",".join(str(a) for a in ((remote_levels(remote_cfg).get(level) or {})
                                                          .get("allow") or []))))
        t0 = time.time()
        try:
            if not target_sid:
                raise RuntimeError("config.remote.targetSessionId 未配置")
            if getattr(self, "rpc", None) is None:
                self.rpc = DshRpc(self.config)
            found = self.rpc.find_session(session_id=target_sid)
            if not found and target_title:
                found = self.rpc.find_session(title=target_title)
            if not found:
                raise RuntimeError("目标会话不存在（sessionId=%s title=%r）" % (target_sid, target_title))
            if found.get("sessionId") and found.get("sessionId") != target_sid:
                log_warn("[REMOTE] targetSessionId 未命中，按标题找到 %s -> 用它" % found.get("sessionId"))
                target_sid = str(found.get("sessionId"))
            self.rpc.prompt(target_sid, prompt)
            rpc_ok, rpc_err = True, ""
        except Exception as exc:
            rpc_ok = False
            rpc_err = redact_known_password(str(exc)[:500])
            log_error("[REMOTE] 转告失败 target=%s: %s" % (target_sid or "-", rpc_err))
        latency_ms = int((time.time() - t0) * 1000)
        if rpc_ok:
            append_remote_audit(remote_cfg, dict(base_audit, action="wake_ok",
                                                 targetSessionId=target_sid,
                                                 latencyMs=latency_ms))
            log_info("[REMOTE] wake_ok level=%s（session/prompt ok，%dms）-> 结果由 Agent 用 "
                     "reply.py/sendfile.py 发回；本进程不再自己发文本（避免 sendguard 30s "
                     "限流拦掉结果）" % (level, latency_ms))
            return {"ok": True, "sendConfirmed": True, "retry": False,
                    "remote": {"action": "wake_ok", "level": level,
                               "targetSessionId": target_sid, "latencyMs": latency_ms}}
        err_text = "我这边叫不动执行端：%s" % (rpc_err or "未知错误")
        log_warn("[REMOTE] wake_fail -> 回错误提示 chat=%r text=%r" % (chat, err_text[:200]))
        send = self._call_reply_py(chat, err_text)
        append_remote_audit(remote_cfg, dict(base_audit, action="wake_fail",
                                             targetSessionId=target_sid,
                                             rpcError=rpc_err, latencyMs=latency_ms,
                                             errorReplyOk=bool(send.get("ok"))))
        return {"ok": False, "skipped": "remote-forward-failed", "retry": False,
                "text": err_text,
                "remote": {"action": "wake_fail", "level": level, "error": rpc_err,
                           "errorReplyOk": bool(send.get("ok"))}}

    def _notify_cooldown_check(self, key, cooldown):
        """同一 chat+reason 的提醒冷却检查（状态存 remote_notify_state.json）。返回 (limited, remain, state)。"""
        now = time.time()
        st = {}
        try:
            with open(REMOTE_NOTIFY_STATE_PATH, "r", encoding="utf-8-sig") as f:
                st = json.load(f)
        except Exception:
            st = {}
        if not isinstance(st, dict):
            st = {}
        last = float(st.get(key) or 0)
        if cooldown and cooldown > 0 and last and (now - last) < cooldown:
            return True, int(cooldown - (now - last)), st
        st[key] = now
        # 顺带清掉 7 天前的记录，避免文件无限增长
        try:
            st = {k: v for k, v in st.items()
                  if isinstance(v, (int, float)) and (now - float(v)) < 7 * 86400}
        except Exception:
            pass
        try:
            with open(REMOTE_NOTIFY_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=2)
                f.write("\n")
        except Exception as exc:
            log_warn("[ALERT] 提醒限流状态写入失败（忽略）: %s" % exc)
        return False, 0, st

    def _notify_toast(self, title, message):
        """可选 Windows 气泡通知（默认不发微信；notifyToast=true 才走这里）。"""
        def _q(s):
            return "'" + str(s or "").replace("'", "''") + "'"
        try:
            ps = ("Add-Type -AssemblyName System.Windows.Forms; "
                  "Add-Type -AssemblyName System.Drawing; "
                  "$n = New-Object System.Windows.Forms.NotifyIcon; "
                  "$n.Icon = [System.Drawing.SystemIcons]::Information; "
                  "$n.BalloonTipTitle = %s; $n.BalloonTipText = %s; "
                  "$n.Visible = $true; $n.ShowBalloonTip(5000); "
                  "Start-Sleep -Milliseconds 1200; $n.Dispose()" % (_q(title), _q(message)))
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-Command", ps],
                           capture_output=True, timeout=10,
                           creationflags=0x08000000)
            return True
        except Exception as exc:
            log_warn("[ALERT] toast 提示失败（忽略）: %s" % exc)
            return False

    def _notify_reply_failure(self, chat, abort_reason, user_msg="", display=""):
        """reply.py 发送失败时的兜底处理。

        2026-10-26 23:4x 用户反馈「她老是发消息到她自己的文件传输助手」-> 默认不再发微信告警：
          1) **永远**写 logs/reply-failures-YYYYMMDD.jsonl（本地记录，不丢信息）
          2) **永远**在 logs/watchdog-*.log 打一条醒目 [ALERT]
          3) 提醒通道（微信降级 / Windows toast）默认全关，且受「同一 chat+reason 冷却」限流
             （状态 remote_notify_state.json，冷却 remote.notifyCooldownSeconds 默认 600s）：
             - fallbackNotifyChat 非空 -> 发微信（默认已改为空字符串 = 不发）
             - notifyToast=true -> 弹 Windows 气泡通知（默认 false）
        返回 {"ok": 是否真的提醒了, "record":…, "log": 路径, "notifyChannel": …}
        """
        cfg = self.config or {}
        remote_cfg = cfg.get("remote") or {}
        fallback_chat = str(remote_cfg.get("fallbackNotifyChat") or "").strip()
        use_toast = bool(remote_cfg.get("notifyToast", False))
        try:
            cooldown = float(remote_cfg.get("notifyCooldownSeconds") or 600)
        except Exception:
            cooldown = 600.0
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        reason_kind = "reply-failed"
        # 2026-10-27 第一阶段：wechat-window-lost（Esc/点击后微信主窗口消失）也单独归类写 reply-failures
        for k in ("row-ambiguous", "row-approximate", "open-chat-failed",
                  "wechat-window-lost", "search-layer-not-opened", "conversation-list",
                  "dropdown-gone", "type-verify-failed", "foreground-changed"):
            if k in str(abort_reason or ""):
                reason_kind = k
                break
        # 简化标点（不用『』（）这类标点）
        if reason_kind in ("row-ambiguous", "row-approximate"):
            suggestion = ("找不到会话 %s，请检查 config.whitelist 里的名字是否和微信当前会话名完全一致，"
                          "微信改名或 OCR 读错都会导致打不开，可手工在微信搜一次核对" % chat)
        elif reason_kind == "open-chat-failed":
            suggestion = ("搜到了会话行但没打开或标题校验没通过，可能微信 UI 改版或窗口被遮挡，"
                          "看 reply.py 日志 stage 与截图")
        elif reason_kind in ("type-verify-failed", "foreground-changed"):
            suggestion = ("输入/注入阶段校验没通过，已放弃发送（不会误发），检查微信窗口是否在前台、"
                          "剪贴板是否可用、输入法状态")
        elif reason_kind == "wechat-window-lost":
            suggestion = ("Esc/点击后微信主窗口不见了（可能被收进托盘/误关）。第一阶段不自动拉起微信，"
                          "请人工确认微信窗口是否还在；窗口恢复后可重新打开小耳朵")
        elif reason_kind in ("search-layer-not-opened", "conversation-list", "dropdown-gone"):
            suggestion = ("Ctrl+F 搜索层没打开或下拉面板不对（把会话列表当成了搜索结果），"
                          "已安全 abort 不点击；检查微信窗口是否在前台、焦点是否在微信")
        else:
            suggestion = "查看 logs/watchdog-*.log 与 reply.py 的 stage 日志"
        notify_key = "%s|%s" % (chat, reason_kind)
        limited, remain, _st = self._notify_cooldown_check(notify_key, cooldown)
        notice = {"ok": False}
        notify_channel = ""
        notice_text = ""
        if limited:
            log_info("[ALERT] 同一 chat+reason 在提醒冷却期内（剩余 %ds，冷却 %ss）-> 只写日志不提醒 key=%r"
                     % (remain, int(cooldown), notify_key))
        elif fallback_chat:
            notice_text = str(user_msg or "").strip() or (
                "小耳朵提醒，没打开会话 %s，这条没发出去，原因 %s" % (chat, reason_kind))
            if reason_kind in ("row-ambiguous", "row-approximate"):
                notice_text = ("小耳朵提醒，找不到会话 %s，请检查白名单名字或微信是否改名，原因 %s"
                               % (chat, reason_kind))
            elif reason_kind in ("type-verify-failed", "foreground-changed") and not user_msg:
                notice_text = ("小耳朵提醒，想给 %s 发的消息没能完整输入，已放弃发送，原因 %s"
                               % (chat, reason_kind))
            notify_channel = "wechat"
            try:
                notice = self._call_reply_py(fallback_chat, notice_text)
            except Exception as exc:
                log_warn("[ALERT] 微信降级提醒发送异常（忽略）: %s" % exc)
                notice = {"ok": False, "error": str(exc)[:200]}
            log_info("[ALERT] 已提醒（wechat -> %r）ok=%s" % (fallback_chat, bool(notice.get("ok"))))
        elif use_toast:
            notice_text = str(user_msg or "").strip() or (
                "小耳朵提醒，%s 发送失败，原因 %s，详情见 logs" % (chat, reason_kind))
            notify_channel = "toast"
            notice = {"ok": self._notify_toast("小耳朵提醒", notice_text)}
            log_info("[ALERT] 已提示（toast）ok=%s" % bool(notice.get("ok")))
        else:
            log_info("[ALERT] 未配置提醒通道（fallbackNotifyChat 为空 且 notifyToast=false）-> 仅本地记录")
        record = {
            "at": now, "ts": time.time(), "chat": chat, "display": display,
            "reason": reason_kind, "abort": str(abort_reason)[:600],
            "userMessage": user_msg, "suggestion": suggestion,
            "notifyKey": notify_key, "notifyCooldownSeconds": cooldown,
            "cooldownLimited": bool(limited), "notifyChannel": notify_channel,
            "fallbackChat": fallback_chat, "fallbackNotice": notice_text,
            "fallbackOk": bool(notice.get("ok")),
            "fallbackError": str(notice.get("error") or "")[:300],
        }
        log_path = os.path.join(LOG_DIR, "reply-failures-%s.jsonl" % time.strftime("%Y%m%d"))
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            log_warn("[ALERT] 失败记录写入失败: %s" % exc)
            log_path = ""
        log_error("[ALERT] 回复发送失败（%s）chat=%r display=%r -> 已写 %s；提醒通道=%s ok=%s 限流=%s；建议: %s"
                  % (reason_kind, chat, display, log_path or "(写失败)",
                     notify_channel or "无", bool(notice.get("ok")), bool(limited), suggestion))
        return {"ok": bool(notice.get("ok")), "record": record, "log": log_path,
                "notifyChannel": notify_channel, "notice": notice}

    def generate_and_send(self, payload, rows, info, digest=""):
        """进程内生成回复 -> 发送护栏 -> reply.py 发送。返回 payload["reply"]。"""
        cfg = self.config
        gen_cfg = cfg.get("gen") or {}
        guard_cfg = cfg.get("guard") or {}
        targets = [r for r in (rows or []) if r.get("hit") and not r.get("black")]
        if not targets:
            log_info("skip: whitelist-miss -> 只记录不发送（未命中白名单）")
            return {"ok": False, "skipped": "whitelist-miss", "retry": False}
        row = targets[0]
        # 真名优先（reply.py 用键盘搜索；OCR 显示名可能带错字）
        chat = str(row.get("matched") or row.get("display") or "").strip()
        display = str(row.get("display") or "")
        if not chat:
            return {"ok": False, "skipped": "no-chat-name", "retry": False}
        if not gen_cfg.get("enabled", True):
            log_info("skip: gen.enabled=false -> 不生成、不发送")
            return {"ok": False, "skipped": "gen-disabled", "retry": False}

        # 1) 对方说了什么（可选 OCR；测试注入可覆盖）
        test_in = str((info or {}).get("test_incoming") or "").strip()
        if test_in:
            incoming, incoming_src = test_in, "test-injected"
            log_info("%s 对方消息（测试注入）: %r" % (genreply.GEN_TAG, incoming))
        else:
            incoming, incoming_src = self._recent_message_text(info, row)

        # 1.5) 远程指令闸门（2026-10-26）：来信含口令 -> 转告 Agent（session/prompt），
        #      **绝不走 genreply、绝不自己发文本**；不含口令才继续下面的普通闲聊回复。
        remote_res = self._remote_forward(chat, incoming, incoming_src)
        if remote_res is not None:
            payload["remote"] = remote_res.get("remote") or {}
            payload["gen"] = {"ok": False, "text": "", "incoming": incoming,
                              "incomingSource": incoming_src, "skipped": "remote-command"}
            return remote_res

        # 2) 进程内 HTTPS 调官方 API 生成回复
        gen_res = genreply.generate(gen_cfg, BASE_DIR, chat, incoming,
                                    log_info=log_info, log_warn=log_warn, log_error=log_error)
        payload["gen"] = {
            "ok": bool(gen_res.get("ok")),
            "text": gen_res.get("text") or "",
            "incoming": incoming,
            "incomingSource": incoming_src,
            "model": gen_res.get("model") or (gen_cfg.get("model") or ""),
            "latencyMs": gen_res.get("latencyMs"),
            "attempts": gen_res.get("attempts"),
            "error": gen_res.get("error") or "",
        }
        if not gen_res.get("ok"):
            log_error("%s ERROR %s -> 不发送，保留待处理可重试（chat=%r）" % (
                genreply.GEN_TAG, gen_res.get("error") or "generation-failed", chat))
            return {"ok": False, "error": gen_res.get("error") or "generation-failed",
                    "retry": True, "text": ""}
        text = gen_res.get("text") or ""
        fingerprint = digest or payload.get("fingerprint") or ""

        # 3) autoSend 总闸
        if not cfg.get("autoSend"):
            log_info("skip: autoSend=false -> 只生成草稿，不发送 text=%r" % text)
            return {"ok": False, "skipped": "autoSend-false", "retry": False,
                    "text": text, "draft": True}
        if getattr(self, "dry_run_no_send", False):
            log_info("skip: dry-run -> 已生成草稿但不发送 text=%r" % text)
            return {"ok": False, "skipped": "dry-run", "retry": False, "text": text}

        # 4) 发送护栏（5 条）
        self.guard_state = sendguard.load_state(GUARD_STATE_PATH)
        verdict = sendguard.check(cfg, self.guard_state, chat, text,
                                  whitelisted=True)
        payload["guard"] = verdict
        sendguard.save_state(GUARD_STATE_PATH, self.guard_state)
        if not verdict.get("ok"):
            log_warn("%s skip: %s（%s）-> 不发送 chat=%r text=%r" % (
                sendguard.GUARD_TAG, verdict.get("skip"), verdict.get("detail"),
                chat, text))
            if verdict.get("stop"):
                log_warn("%s stop: 连续 %s 条达上限 -> 停了等用户发话（pausedUntil=%s）"
                         % (sendguard.GUARD_TAG, guard_cfg.get("perChatMaxBurst"),
                            verdict.get("detail")))
            return {"ok": False, "skipped": verdict.get("skip"), "retry": False,
                    "text": text, "guard": verdict}

        # 5) 调 reply.py 真发送
        send = self._call_reply_py(chat, text)
        if not send.get("ok"):
            # 2026-10-26 晚（用户真机实测失败反馈）：reply.py 因「行选择/打开会话失败」没发出去时，
            # 旧逻辑把 userMessage 又发回**同一个打不开的会话**（必然再失败、用户啥也看不到）。
            # 现在改为：写 logs/reply-failures-*.jsonl + [ALERT] 日志 + 降级提醒发到易达会话
            # （config.remote.fallbackNotifyChat，默认「文件传输助手」）。
            parsed = send.get("parsed") or {}
            abort_reason = str(parsed.get("abort") or send.get("error") or "")
            user_msg = str(parsed.get("userMessage") or "")
            # 🔴 2026-10-27 第一阶段：熔断 —— 每次「真发送失败」计数 +1；连续达标 -> 急停（写 STOP + 退出）
            try:
                self.breaker_register_failure(abort_reason or "send-not-confirmed")
            except SystemExit:
                raise
            except Exception as exc:
                log_error("[BREAKER] 失败计数异常（忽略）: %s" % exc)
            row_fail = any(k in abort_reason for k in
                           ("row-ambiguous", "row-approximate", "open-chat-failed",
                            "search-layer-not-opened", "conversation-list",
                            "dropdown-gone", "wechat-window-lost"))
            if row_fail or user_msg:
                try:
                    res = self._notify_reply_failure(chat, abort_reason, user_msg, display=display)
                except Exception as exc:
                    log_error("[ALERT] 失败兜底处理异常: %s" % exc)
                    res = {"ok": False, "record": {}, "log": ""}
                log_info("[REPLY] 转告结果: 降级提醒 ok=%s（原因=%s）" % (
                    bool(res.get("ok")), abort_reason[:120]))
                return {"ok": False, "error": "row-selection-abort-notified",
                        "retry": False, "text": text, "send": send,
                        "noticeOk": bool(res.get("ok")),
                        "failureRecord": res.get("record"),
                        "failureLog": res.get("log")}
            return {"ok": False, "error": send.get("error") or "send-not-confirmed",
                    "retry": True, "text": text, "send": send}
        record = send.get("confirmed") or {}

        # 6) 护栏记账 + 落 logs/sent-YYYYMMDD.jsonl
        extra = {
            "display": display,
            "fingerprint": fingerprint,
            "reason": payload.get("reason"),
            "gen": payload.get("gen"),
            "model": (payload.get("gen") or {}).get("model"),
            "latencyMs": (payload.get("gen") or {}).get("latencyMs"),
            "before": record.get("before"),
            "after": record.get("after"),
            "elapsed": send.get("elapsed"),
            "mouse_events": record.get("mouse_events"),
            "key_events": record.get("key_events"),
        }
        sent_item = sendguard.record(cfg, self.guard_state, chat, text,
                                     fingerprint=fingerprint, extra=extra)
        sendguard.save_state(GUARD_STATE_PATH, self.guard_state)
        sent_log = ""
        if guard_cfg.get("sentLogEnabled", True):
            sent_log = sendguard.append_sent_log(LOG_DIR, sent_item)
        try:
            self.wake_times.append(time.time())
            self.state["wake_times"] = self.wake_times
            save_state(self.state)
        except Exception:
            pass
        log_info("[SENT] 已真发出去 chat=%r text=%r 指纹=%s -> 发送记录 %s" % (
            chat, text, fingerprint or "(无)", sent_log or "(写失败)"))
        # 🔴 2026-10-27 第一阶段：成功真发一次 -> 熔断连续失败计数清零（breaker.resetOnSuccess）
        try:
            self.breaker_register_success()
        except Exception as exc:
            log_warn("[BREAKER] 成功计数清零异常（忽略）: %s" % exc)
        return {"ok": True, "sendConfirmed": True, "text": text, "chat": chat,
                "display": display, "send_record": record, "sent_log": sent_log,
                "gen": payload.get("gen"), "guard": verdict, "retry": False}

    def wake(self, payload, channel):
        """⚠ 已停用：方案A 不再唤醒 Agent（保留代码仅为兼容，默认流程不调用）。"""
        log_warn("wake disabled（方案A：进程内直接调 API 生成回复，不再唤醒 Agent / 不调用 RPC）"
                 " -> 忽略 wake(channel=%r)" % (channel,))
        return {"enabled": False, "channel": "off", "ok": False,
                "reason": "wake-disabled"}

    def _dsh_wake_legacy(self, payload, channel):
        """旧的 RPC 唤醒实现（历史代码保留，默认不调用）。"""
        requested = str(channel)
        session_title = (self.config.get("wake", {}) or {}).get("sessionTitle")
        log_info("legacy wake channel=%s baseUrl=%s sessionTitle=%r" % (
            requested, self.rpc.base_url, session_title))
        if requested in ("log", "none", "off"):
            return {"requestedChannel": requested, "channel": requested, "ok": False,
                    "reason": "channel=log"}
        if requested == "aura":
            return {"requestedChannel": requested, "channel": "aura", "ok": False,
                    "reason": "waiting for aura-scheduler sensor"}
        if requested not in ("auto", "rpc"):
            log_warn("未知 legacy wake channel=%r，按 auto 处理" % requested)
        ok, why = self.rpc.available()
        if not ok:
            log_error("legacy wake 不可用: %s（signal.json 已落盘）" % why)
            return {"requestedChannel": requested, "channel": "rpc", "ok": False, "error": why}
        try:
            session_id = self.rpc.find_or_create_session(self.state)
            cfg = self.config
            prompt = (cfg.get("wake", {}).get("promptTemplate") or DEFAULT_CONFIG["wake"]["promptTemplate"]).format(
                time=payload["detectedAtLocal"],
                reason=payload["reason"],
                badges=payload["unreadBadges"],
                screenshot=payload["screenshot"] or "(截图保存失败)",
                signal=SIGNAL_PATH,
                autoSend="true" if payload["autoSend"] else "false",
                whitelist=", ".join(payload["whitelist"]) if payload["whitelist"] else "(空=不回复任何人)",
                blacklist=", ".join(payload.get("blacklist") or []) if payload.get("blacklist") else "(空)",
                quietHours=payload["quietHours"],
            )
            wl = payload.get("whitelistMatch") or {}
            if wl.get("rows"):
                lines = ["", "【白名单匹配结果】"]
                for r in wl["rows"]:
                    if r.get("black"):
                        lines.append("- 黑名单（忽略，不触发）：display='%s'" % r.get("display"))
                    elif r.get("hit"):
                        lines.append("- 命中：display='%s' → 白名单项 '%s'（%s）" % (
                            r.get("display"), r.get("matched"), r.get("reason")))
                    else:
                        lines.append("- 未命中（只记录，不回复）：display='%s'" % r.get("display"))
                prompt = prompt + "\n".join(lines)
            self.rpc.prompt(session_id, prompt)
            log_info("legacy wake ok=True session_id=%s（session/prompt 已被 DSH 接受）" % session_id)
            return {"requestedChannel": requested, "channel": "rpc", "ok": True,
                    "sessionId": session_id}
        except Exception as exc:
            log_error("legacy wake ok=False 失败原因: %s" % exc)
            return {"requestedChannel": requested, "channel": "rpc", "ok": False,
                    "error": str(exc)}


    # ---- window candidates diagnostics ----
    def _maybe_dump_candidates(self, cands, unavailable_reason):
        """微信主窗口候选状态变化时打印一次（忽略 IME/辅助窗口，避免刷屏）。"""
        relevant = [c for c in cands if c["class"] == WECHAT_CLASS or c["title"] == WECHAT_TITLE]
        sig = json.dumps([(c["hwnd"], c["visible"], c["iconic"], c["cloaked"], c["size"])
                          for c in relevant], ensure_ascii=False, default=str)
        now = time.time()
        if sig != self._last_cand_sig or now - self._last_cand_dump_ts > 300:
            self._last_cand_sig = sig
            self._last_cand_dump_ts = now
            msg = "微信主窗口候选 %d 个: %s（Weixin 顶层窗口共 %d 个，其余为 IME/托盘消息窗）" % (
                len(relevant),
                describe_window_candidates(relevant) if relevant else "无",
                len(cands))
            if unavailable_reason:
                log_warn(msg)
            else:
                log_info(msg)

    def _note_unavailable(self, reason, detail):
        """窗口不可用告警：同状态只报一次，之后每 300 秒复述一次（不刷屏）。"""
        now = time.time()
        if reason != self._unavail_reason:
            self._unavail_reason = reason
            self._unavail_detail = detail or ""
            self._unavail_since = now
            self._unavail_last_log = now
            log_warn("微信窗口不可用: %s（%s）" % (reason, detail or "无详情"))
            return
        if now - self._unavail_last_log >= 300:
            self._unavail_last_log = now
            log_warn("微信窗口仍不可用（已持续 %d 秒）: %s（%s）" % (
                int(now - self._unavail_since), reason, self._unavail_detail or "无详情"))

    def _note_available(self):
        if self._unavail_reason is not None:
            log_info("微信窗口恢复可用（此前不可用 %d 秒，原因=%s）" % (
                int(time.time() - self._unavail_since), self._unavail_reason))
            self._unavail_reason = None
            self._unavail_detail = ""

    # ---- one scan ----
    def scan_once(self):
        """抓一帧并分析；返回 dict（窗口不可用时 available=False 且带准确原因）。"""
        cfg = self.config
        detect = cfg.get("detect", {})
        cands = enumerate_weixin_windows()
        win, reason, detail = select_wechat_window(cands)
        self._maybe_dump_candidates(cands, reason)
        if win is None:
            return {"available": False, "reason": reason, "detail": detail,
                    "candidates": cands,
                    "taskbar": self._taskbar_check()}
        hwnd = win["hwnd"]
        title = win["title"]
        scale = dpi_scale_of(hwnd)
        # 抓之前再确认一次状态（避免刚好在枚举后的一瞬间被最小化/隐藏）
        if not _user32.IsWindowVisible(hwnd) or _user32.IsIconic(hwnd) or _is_cloaked(hwnd):
            return {"available": False, "reason": "wechat-window-became-unavailable",
                    "detail": "抓图前复检发现窗口刚被最小化/隐藏",
                    "candidates": cands,
                    "taskbar": self._taskbar_check(scale)}
        l, t, r, b = win["rect"]
        w, h = r - l, b - t
        if w <= 100 or h <= 100:
            return {"available": False, "reason": "wechat-window-too-small",
                    "detail": "窗口尺寸 %sx%s 太小" % (w, h),
                    "taskbar": self._taskbar_check(scale)}
        mode = detect.get("capture", "auto")
        img, source, attempts = capture_wechat_window(hwnd, (l, t, r, b), mode)
        if img is None:
            return {"available": False, "reason": "capture-failed",
                    "detail": "三层抓图均失败: %s" % "; ".join(attempts),
                    "taskbar": self._taskbar_check(scale)}
        if source != self._last_capture_source:
            log_info("抓图方式: %s（逐层尝试: %s）" % (source, "; ".join(attempts)))
            self._last_capture_source = source
        bgr = img_to_bgr(img)
        info = analyze_window(bgr, scale)
        info.update({"available": True, "source": source, "window_title": title,
                     "hwnd": hwnd, "capture_attempts": attempts,
                     "img": img, "bgr": bgr, "scale": scale})
        info["taskbar"] = self._taskbar_check(scale)
        return info

    def _taskbar_check(self, scale=None):
        if not self.config.get("detect", {}).get("taskbarEnabled", True):
            return {"supported": False}
        try:
            if scale is None:
                hwnd, _ = find_wechat_window_simple()
                scale = dpi_scale_of(hwnd) if hwnd else 1.0
            bgr, icon = find_taskbar_wechat_icon(scale)
            if icon is None:
                return {"supported": True, "icon_found": False, "unread": False}
            unread, count = taskbar_unread(bgr, icon, scale)
            return {"supported": True, "icon_found": True, "unread": unread,
                    "count": count, "icon": {k: icon[k] for k in ("x", "y", "w", "h")}}
        except Exception as exc:
            return {"supported": True, "error": str(exc), "unread": False}

    # ---- 未读指纹：剔除「已确认黑名单」的行 ----
    def _row_cache(self, row):
        name_dh = (row or {}).get("name_dh") or ""
        if not name_dh:
            return {}
        return (self.state.get("row_name_cache") or {}).get(name_dh) or {}

    def _filtered_fp(self, info):
        """只把「非黑名单行」算进指纹；黑名单行（官方号刷未读）不会造成假新消息。"""
        fp = info.get("fingerprint") or {}
        rows = fp.get("rows") or []
        kept, black = [], []
        for r in rows:
            c = self._row_cache(r)
            if c.get("black"):
                black.append(r)
            else:
                kept.append(r)
        digest = ""
        if kept:
            digest = hashlib.sha1(
                json.dumps({"count": len(kept), "rows": kept}, sort_keys=True,
                           separators=(",", ":")).encode()).hexdigest()[:16]
        return {"digest": digest, "count": len(kept), "rows": kept,
                "raw_count": len(rows), "raw_digest": fp.get("digest") or "",
                "black_rows": black}

    def _cache_row_name(self, row, display, is_black, is_white):
        name_dh = (row or {}).get("name_dh") or ""
        if not name_dh:
            return
        cache = self.state.setdefault("row_name_cache", {})
        cache[name_dh] = {"display": display, "black": bool(is_black),
                          "white": bool(is_white), "at": _ts()}
        if len(cache) > 200:
            items = sorted(cache.items(), key=lambda kv: kv[1].get("at", ""))
            self.state["row_name_cache"] = dict(items[-200:])

    def _log_blacklisted_skip(self, filt, rows=None):
        raw_digest = filt.get("raw_digest") or ""
        if raw_digest == getattr(self, "_last_black_raw", None):
            return
        self._last_black_raw = raw_digest
        displays = [self._row_cache(r).get("display", "?") for r in (filt.get("black_rows") or [])]
        if rows:
            displays = [r.get("display") or "?" for r in rows if r.get("black")] or displays
        log_info("blacklist: 当前未读行全部命中黑名单（%d 行: %s）" % (
            len(filt.get("black_rows") or rows or []), ", ".join(displays) or "?"))
        log_info("skip: all-unread-blacklisted（黑名单不触发唤醒，不消耗 wake_times）")

    def process_scan(self, info):
        """根据一帧分析结果决定是否触发唤醒（黑名单优先 -> 白名单 -> 指纹去重）。"""
        # ---------- 先看上一轮唤醒是否已被 reply.py 确认发送（真发出去才写 processed_fp） ----------
        self._check_awaiting_send()

        # ---------- 窗口不可用 ----------
        if not info or not info.get("available"):
            reason = (info or {}).get("reason", "unavailable")
            tb = (info or {}).get("taskbar") or {}
            now = time.time()
            if reason in ("wechat-window-minimized", "wechat-window-hidden-to-tray") and tb.get("unread"):
                if now - self.last_trigger_ts > 60:
                    self.last_trigger_ts = now
                    log_warn("%s，任务栏显示未读，但窗口不可抓图 -> 无法做名单校验，只记录不唤醒/不回复" % reason)
                return
            self._note_unavailable(reason, (info or {}).get("detail", ""))
            return

        self._note_available()
        geom = info["geom"]
        unread_raw = bool(info.get("unread"))
        now = time.time()

        # ---------- 行变化（仅日志/诊断用） ----------
        row_changed = False
        ratio = 0.0
        text = info.get("text_bgr")
        if text is not None and self.baseline_text is not None and self.baseline_geom is not None:
            if self.baseline_text.shape == text.shape and abs(self.baseline_geom["scale"] - geom["scale"]) < 0.01:
                ratio, _ = row_diff_ratio(text, self.baseline_text, info.get("active_rows") or [], geom)
                row_changed = ratio > float(self.config["detect"].get("rowDiffThreshold", 0.03))
        if text is not None and (ratio < 0.003 or self.baseline_text is None):
            self.baseline_text = text.copy()
            self.baseline_geom = dict(geom)

        filt = self._filtered_fp(info)
        digest = filt["digest"]
        fp_count = filt["count"]

        # ---------- 未读归零 ----------
        if not unread_raw:
            if self.first_scan:
                self.first_scan = False
                log_info("已建立基线：当前没有未读，正在监视新消息")
            if self.last_fp is not None or self.prev_unread:
                log_info("未读归零 → 指纹清空，重新武装（此前 fp=%s）" % (self.last_fp or "-"))
            self.last_fp = None
            self.last_fp_info = None
            self.pending_fp = None
            self.pending_count = 0
            self.prev_unread = False
            self.prev_badges = 0
            self._last_black_raw = None
            self._last_busy_key = None
            # 未读归零：没有待处理的消息了 -> 丢弃「失败重试」状态（未读消失说明不需要再发）
            # ⚠ awaiting_send 保留：reply.py 发送成功后会把该会话读掉，此时仍要等发送确认
            if self.retry_state and not self.awaiting_send:
                self._clear_retry("未读归零")
            if row_changed and (now - self._last_rowwarn) > 60:
                log_info("会话列表有变化但没有未读徽标（判定为自己操作/预览刷新，比例 %.3f），不唤醒" % ratio)
                self._last_rowwarn = now
            return

        # ---------- 全部未读都是黑名单：完全不唤醒 ----------
        if fp_count == 0:
            if self.first_scan:
                self.first_scan = False
                self._evaluate_rows(info)          # OCR 识别并缓存黑名单名字
                filt = self._filtered_fp(info)
                self._log_init_or_black(filt)
            self._log_blacklisted_skip(filt)
            self.last_fp = None
            self.last_fp_info = None
            self.pending_fp = None
            self.pending_count = 0
            self.prev_unread = True
            self.prev_badges = 0
            return

        # ---------- 首次扫描：初始化指纹（只记录，不唤醒） ----------
        if self.first_scan:
            self.first_scan = False
            # 先 OCR 一次，把已知官方号/联系人归类进缓存（若某些行还是 unknown，保留在指纹里）
            rows, any_black, any_white = self._evaluate_rows(info)
            filt = self._filtered_fp(info)
            if filt["count"] == 0:
                self._log_init_or_black(filt, rows)
                self._log_blacklisted_skip(filt, rows)
                self.last_fp = None
                self.last_fp_info = None
                self.prev_unread = True
                self.prev_badges = 0
                return
            digest = filt["digest"]
            fp_count = filt["count"]
            self.last_fp = digest
            self.last_fp_info = {"digest": digest, "count": fp_count, "rows": filt["rows"]}
            self.pending_fp = None
            log_info("启动初始化未读指纹: fp=%s count=%d（已排除黑名单 %d 行；只记录，不唤醒）" % (
                digest, fp_count, len(filt["black_rows"])))
            if digest and self.processed_fp and digest == self.processed_fp:
                log_info("重启后未读指纹与 state.json 已处理记录一致 -> 不重复回复旧消息")
            if self.config.get("notifyExistingOnStart"):
                log_info("notifyExistingOnStart=true -> 对启动时已有未读执行一次处理")
                self._handle_new_unread(info, "existing-unread-on-start")
            return

        # ---------- 指纹未变 ----------
        if digest and self.last_fp and digest == self.last_fp:
            # 【第四次整改】上次 busy/冷却/等确认失败时保留了指纹：
            # 到重试时间就重新判定，绝不把「没发出去」当成「已处理」吞掉。
            if self._retry_due(digest, now):
                rs = self.retry_state or {}
                log_info("retry: 同一条未读到重试时间（reason=%s，已重试 %s/%s 次）-> 重新判定 fp=%s"
                         % (rs.get("reason") or "-", rs.get("retries"), self._max_retry_attempts(), digest))
                self._handle_new_unread(info, "retry-after-skip", is_retry=True)
                return
            log_info("skip: same-unread-fingerprint fp=%s（未读未变化，不唤醒）" % digest)
            self.pending_fp = None
            self.pending_count = 0
            self.prev_unread = True
            self.prev_badges = fp_count
            return

        # ---------- 有效未读数减少 ----------
        prev_count = int((self.last_fp_info or {}).get("count") or 0)
        if fp_count < prev_count:
            self.last_fp = digest
            self.last_fp_info = {"digest": digest, "count": fp_count, "rows": filt["rows"]}
            self.pending_fp = None
            self.pending_count = 0
            log_info("有效未读数减少（%d -> %d，部分消息被读掉/被拉黑）→ 指纹更新，不唤醒 fp=%s" % (
                prev_count, fp_count, digest))
            self.prev_unread = True
            self.prev_badges = fp_count
            return

        # ---------- 新指纹：连续稳定确认 ----------
        settle = max(1, int((self.config.get("dedupe") or {}).get("settleScans", 2) or 2))
        if self.pending_fp != digest:
            self.pending_fp = digest
            self.pending_count = 1
            log_info("未读指纹变化（待稳定确认）: fp=%s count=%d 需要连续 %d 次一致" % (
                digest, fp_count, settle))
            return
        self.pending_count += 1
        if self.pending_count < settle:
            log_info("未读指纹变化（稳定确认中 %d/%d）: fp=%s" % (
                self.pending_count, settle, digest))
            return

        # ---------- 稳定确认：处理 ----------
        self.pending_fp = None
        self.pending_count = 0
        self._handle_new_unread(info, "unread-fingerprint-changed")

    def _log_init_or_black(self, filt, rows=None):
        log_info("启动初始化：未读行全部命中黑名单（%d 行），有效未读=0" % (
            len(filt.get("black_rows") or rows or [])))

    # ---- 新未读事件：OCR -> 黑名单 -> 白名单 -> 唤醒 or 只记录 ----
    def _handle_new_unread(self, info, reason, is_retry=False):
        filt = self._filtered_fp(info)
        digest = filt["digest"]
        fp_count = filt["count"]
        now = time.time()
        if digest and self.processed_fp and digest == self.processed_fp:
            self.last_fp = digest
            self.last_fp_info = {"digest": digest, "count": fp_count, "rows": filt["rows"]}
            self._clear_retry("该指纹已确认发送过")
            log_info("skip: fingerprint-already-processed fp=%s（state.json 中已确认处理过，不重复回复）" % digest)
            return

        # OCR 识别所有未读行 + 黑/白名单判定（缓存到 state.row_name_cache）
        rows, any_black, any_white = self._evaluate_rows(info)
        filt2 = self._filtered_fp(info)
        digest2 = filt2["digest"]
        count2 = filt2["count"]

        # 全部黑名单 -> 完全不唤醒
        if count2 == 0:
            self._log_blacklisted_skip(filt2, rows)
            self.last_fp = None
            self.last_fp_info = None
            self.pending_fp = None
            self.pending_count = 0
            self.prev_unread = True
            self.prev_badges = 0
            self._clear_retry("全部未读是黑名单")
            return

        # ---------- 忙闲守护「你忙就别动」（动作前判断，只记日志不动作） ----------
        # 2026-10-25 第四次整改：user-active 判定已停用（不再看「最近 N 秒有没有键鼠输入」）。
        # 现在只拦：游戏前台 / 全屏 / 微信前台 / 静默时段 / 限速。
        # 命中 -> 不唤醒 agent、不注入任何键鼠，且【不写 processed_fingerprint】，保留待处理状态重试。
        if busyguard is not None:
            try:
                guard = busyguard.evaluate(self.config, wake_times=self.wake_times)
            except Exception as exc:
                guard = {"busy": False, "reason": "", "detail": "busyguard-error: %s" % exc}
                log_warn("忙闲守护检查异常（按不拦截处理）: %s" % exc)
            if guard.get("busy"):
                reason_b = guard.get("reason") or "busy"
                key = (digest2, reason_b)
                if reason_b == "quiet-hours":
                    # 静默时段：纯延后，不计入重试次数（避免半小时就把未读 gave-up 掉）
                    left = self._quiet_seconds_left()
                    log_warn("skip: busy (reason=quiet-hours) detail=%s（不唤醒、不注入任何键鼠；"
                             "本次【不写 processed_fingerprint】，静默时段还剩 %.0f 秒（%.1f 小时），"
                             "结束后自动再试）" % (guard.get("detail") or "", left, left / 3600.0))
                    self.last_fp = digest2
                    self.last_fp_info = {"digest": digest2, "count": count2, "rows": filt2["rows"]}
                    self.pending_fp = None
                    self.pending_count = 0
                    self.prev_unread = True
                    self.prev_badges = count2
                    self._schedule_retry(filt2, rows, reason_b, info,
                                         delay=left + 2.0, count_retry=False)
                    return
                if key != self._last_busy_key:
                    self._last_busy_key = key
                    log_warn("skip: busy (reason=%s) detail=%s（不唤醒、不注入任何键鼠；"
                             "本次【不写 processed_fingerprint】，%.0fs 后重试）"
                             % (reason_b, guard.get("detail") or "", self._retry_after_seconds()))
                else:
                    log_info("skip: busy (reason=%s)（同一条未读持续忙，已记录过）" % reason_b)
                self.last_fp = digest2
                self.last_fp_info = {"digest": digest2, "count": count2, "rows": filt2["rows"]}
                self.pending_fp = None
                self.pending_count = 0
                self.prev_unread = True
                self.prev_badges = count2
                self._schedule_retry(filt2, rows, reason_b, info)
                return
            self._last_busy_key = None

        # ---------- 冷却 ----------
        # 2026-10-25 第四次整改：30s -> 10s；冷却也【不写 processed_fingerprint】，冷却结束立即重试。
        debounce = float(self.config.get("triggerCooldownSeconds", 10) or 0)
        if (now - self.last_trigger_ts) <= debounce:
            remain = debounce - (now - self.last_trigger_ts)
            log_info("skip: trigger-cooldown（距上次 %.0fs < %.0fs），【不写 processed_fingerprint】，"
                     "%.1fs 后重试 fp=%s" % (now - self.last_trigger_ts, debounce, remain + 0.5, digest2))
            self.last_fp = digest2
            self.last_fp_info = {"digest": digest2, "count": count2, "rows": filt2["rows"]}
            self._schedule_retry(filt2, rows, "trigger-cooldown", info, delay=remain + 0.5)
            return

        self.last_trigger_ts = now
        log_info("触发回复判定: reason=%s fingerprint=%s count=%d white_hit=%s black=%d" % (
            reason, digest2, count2, any_white, sum(1 for r in rows if r.get("black"))))
        payload = self.trigger(info["img"], reason, max(1, count2), extra=info,
                               whitelist_info={"any_hit": any_white, "rows": rows},
                               digest=digest2)
        self.last_fp = digest2
        self.last_fp_info = {"digest": digest2, "count": count2, "rows": filt2["rows"]}
        reply_res = (payload or {}).get("reply") or {}
        if reply_res.get("sendConfirmed"):
            # reply.py 真发出去了（reply_state.json 新增 sent 记录）-> 才写 processed_fingerprint
            self._persist_processed(info, rows, payload, filt2,
                                    send_record=reply_res.get("send_record"), note="sent")
            return
        if reply_res.get("skipped"):
            skipped = str(reply_res.get("skipped"))
            if reply_res.get("retry"):
                # 静默时段/限速：延后重试（不计入已处理）
                log_warn("skip: %s -> 【不写 processed_fingerprint】，保留待处理状态重试" % skipped)
                self._schedule_retry(filt2, rows, skipped, info)
                return
            # 护栏/白名单/autoSend 关闭：明确不发送 -> 记 processed 防反复生成（state.json 留痕）
            log_warn("%s skip 已记录（%s）-> 不重试、不发送；写 processed_fingerprint 防止反复生成"
                     % (sendguard.GUARD_TAG, skipped))
            self._persist_processed(info, rows, payload, filt2, send_record=None,
                                    note="skip:%s" % skipped)
            return
        # 生成/发送失败（API 错误 / reply.py 未确认）-> 不写 processed，保留待处理重试
        err = reply_res.get("error") or "generate-or-send-failed"
        log_warn("生成/发送未成功（%s）-> 【不写 processed_fingerprint】，保留待处理状态以便重试" % err)
        self._schedule_retry(filt2, rows, str(err), info)


    # ---------- 测试钩子：造一条白名单未读（端到端自测） ----------
    def _row_name_dhash(self, bgr, geom, row_idx):
        """按 analyze_window 的 name_strip 几何算某一行的名字 dhash（用于注入行的指纹）。"""
        try:
            s = geom["scale"]
            h, w = bgr.shape[:2]
            lx0 = int(round(geom["list_left"] * s))
            lx1 = min(w, int(round(geom["list_right"] * s)))
            ly0 = int(round(geom["rows_top"] * s))
            row_h_px = int(round(GEOM["row_h"] * s))
            top = ly0 + row_idx * row_h_px
            bottom = min(h, top + row_h_px)
            name_x0 = lx0 + int(round(52 * s))
            name_strip = bgr[max(0, top + int(10 * s)):max(0, bottom - int(28 * s)),
                             name_x0:max(name_x0 + 8, lx1 - int(50 * s))]
            return _dhash_bgr(name_strip)
        except Exception:
            return ""

    def _find_row_by_hint(self, rows, hint):
        """在 OCR 出来的会话行里找 hint（支持 OCR 容错匹配）。"""
        if not hint:
            return None
        mcfg = self.config.get("whitelistMatch") or {}
        for r in rows or []:
            cands = list(r.get("candidates") or []) + [r.get("display") or ""]
            m = name_match(cands, [hint], mcfg)
            if m.get("hit"):
                return r
        return None

    def _handle_inject_file(self):
        """读 tmp-inject-unread.json 并造一条未读（只用于自测；默认值由一次真实截图 + OCR 支撑）。"""
        raw = ""
        try:
            with open(INJECT_PATH, "r", encoding="utf-8-sig") as f:
                raw = f.read()
        finally:
            try:
                os.remove(INJECT_PATH)
            except Exception:
                pass
        try:
            doc = json.loads(raw or "{}")
        except Exception as exc:
            log_error("[TEST] 注入文件不是合法 JSON: %s" % exc)
            return
        hint = str(doc.get("chatHint") or doc.get("chat") or "").strip()
        incoming = str(doc.get("incoming") or "").strip()
        info = self.scan_once()
        if not info or not info.get("available"):
            log_error("[TEST] 注入未读失败：微信窗口不可用（%s）"
                      % ((info or {}).get("reason") or "?"))
            return
        row = None
        explicit_row = doc.get("row")
        if explicit_row is not None:
            try:
                idx = int(explicit_row)
                row = {"row": idx, "display": "(inject-row-%d，OCR 在后续名单判定里做)" % idx,
                       "hit": True, "black": False, "matched": hint}
                log_info("[TEST] 按指定行注入（跳过全列表 OCR，加快链路）: row=%d hint=%r"
                         % (idx, hint))
            except Exception:
                row = None
        if row is None:
            rows, any_black, any_white = self._evaluate_rows(
                info, row_indices=list(range(int(GEOM.get("rows_max") or 7))))
            row = self._find_row_by_hint(rows, hint) if hint else None
            if row is None:
                row = next((r for r in rows if r.get("hit")), None)
        if row is None:
            log_error("[TEST] 注入未读失败：会话列表里找不到 %r" % hint)
            return
        idx = int(row.get("row") or 0)
        fake = {"row": idx, "dh": "inject-%d" % int(time.time() * 1000),
                "name_dh": self._row_name_dhash(info["bgr"], info["geom"], idx)}
        info2 = dict(info)
        info2["unread"] = True
        info2["row_badges"] = 1
        info2["fingerprint"] = {"digest": "", "count": 1, "rows": [fake],
                                "rail": (info.get("fingerprint") or {}).get("rail", "")}
        if incoming:
            info2["test_incoming"] = incoming
        log_warn("[TEST] 造一条白名单未读（检测层注入，非真实来信）: chatHint=%r → row=%d display=%r "
                 "incoming=%r；目的：端到端验证「进程内生成 → 发送护栏 → reply.py 真发送」"
                 % (hint, idx, row.get("display"), incoming))
        self._handle_new_unread(info2, "test-injected-unread")

    def _evaluate_rows(self, info, row_indices=None):
        """对当前所有未读行做 OCR + 黑名单（优先）-> 白名单匹配；日志写 hit/miss；更新名字缓存。

        row_indices 只给测试钩子用（没有未读时也 OCR 指定行，用来找注入目标行）。
        """
        fp = info.get("fingerprint") or {}
        if row_indices is None:
            row_infos = list(fp.get("rows") or [])
        else:
            row_infos = [{"row": int(i)} for i in row_indices]
        whitelist = self.config.get("whitelist") or []
        blacklist = self.config.get("blacklist") or []
        ocr_cfg = self.config.get("ocr") or {}
        match_cfg = self.config.get("whitelistMatch") or {}
        tesseract = ""
        if ocr_cfg.get("enabled", True):
            if self._tesseract is None:
                self._tesseract = find_tesseract(ocr_cfg.get("tesseract", ""))
                if not self._tesseract:
                    log_error("名单判定 unknown: 找不到 tesseract（OCR 不可用）-> 本次不发送")
            tesseract = self._tesseract
        rows = []
        any_white = False
        any_black = False
        for r in row_infos:
            row_idx = int(r.get("row", 0))
            candidates = []
            if tesseract:
                try:
                    candidates = _ocr_row_name_candidates(
                        info["img"], info["geom"], row_idx, tesseract, ocr_cfg)
                except Exception as exc:
                    log_warn("OCR 第 %d 行失败: %s" % (row_idx, exc))
            m_white = name_match(candidates, whitelist, match_cfg)
            m_black = name_match(candidates, blacklist, match_cfg) if blacklist else None
            display = m_white.get("display") or (m_black or {}).get("display") or "(OCR无结果)"
            is_white = bool(m_white.get("hit"))
            is_black = bool(m_black and m_black.get("hit"))
            if is_black and is_white:
                log_info("blacklist hit ignored: display='%s' 同时命中白名单 -> 按白名单处理" % display)
                is_black = False
            if is_black:
                any_black = True
                log_info("blacklist hit: display='%s' matched='%s' reason=%s (row=%d)" % (
                    display, m_black.get("matched"), m_black.get("reason"), row_idx))
            elif is_white:
                any_white = True
                log_info("whitelist hit: display='%s' matched='%s' reason=%s (row=%d, ocr候选=%s)" % (
                    display, m_white.get("matched"), m_white.get("reason"), row_idx,
                    " | ".join(candidates[:3])))
            else:
                log_info("whitelist miss: display='%s' normalized='%s' (row=%d, ocr候选=%s)" % (
                    display, m_white.get("normalized") or "", row_idx,
                    " | ".join(candidates[:3]) or "无"))
            self._cache_row_name(r, display, is_black, is_white)
            rows.append({
                "row": row_idx,
                "hit": is_white,
                "black": is_black,
                "matched": m_white.get("matched") or (m_black or {}).get("matched") or "",
                "reason": m_white.get("reason") or (m_black or {}).get("reason") or "",
                "display": display,
                "normalized": m_white.get("normalized") or "",
                "candidates": candidates[:6],
            })
        try:
            save_state(self.state)
        except Exception:
            pass
        return rows, any_black, any_white

    def _persist_processed(self, info, rows, payload, filtered=None, send_record=None, note="sent"):
        """把「已处理」的指纹写进 state.json（重启不重复回复）。

        ⚠ 方案A：只有两种情况下才调用本函数
          ① note="sent"：reply.py 真的发出去（reply_state.json 新增 sent 记录）
          ② note="skip:xxx"：发送护栏/白名单/autoSend 明确不发送（记录留痕，不反复生成）
          API 失败 / reply.py 未确认发送 / 静默 / 限速 -> 绝不写 processed_fingerprint（保留待处理重试）。
        """
        filt = filtered or self._filtered_fp(info)
        digest = filt.get("digest") or ""
        if not digest:
            log_warn("要写入已处理指纹但 digest 为空（不入库）")
            return
        try:
            self.processed_fp = digest
            self.state["processed_fingerprint"] = digest
            self.state["processed_at"] = int(time.time())
            self.state["processed_unread"] = {
                "count": int(filt.get("count") or 0),
                "blackRows": int(len(filt.get("black_rows") or [])),
                "rows": [{"row": r.get("row"), "display": r.get("display"),
                          "hit": r.get("hit"), "black": r.get("black"),
                          "matched": r.get("matched"), "reason": r.get("reason")}
                         for r in (rows or [])],
                "reason": str(note),
                "wake": (payload or {}).get("wake"),
                "gen": (payload or {}).get("gen"),
                "reply": (payload or {}).get("reply"),
                "confirmed_send": ({"chat": send_record.get("chat"),
                                    "text": (send_record.get("text") or "")[:200],
                                    "at": send_record.get("at"),
                                    "before": send_record.get("before"),
                                    "after": send_record.get("after"),
                                    "ts": send_record.get("ts")}
                                   if send_record else None),
            }
            hist = self.state.get("processed_history") or []
            hist.insert(0, {
                "fingerprint": digest,
                "at": _ts(),
                "reason": str(note),
                "displays": [r.get("display") for r in (rows or [])],
                "hit": any(r.get("hit") for r in (rows or [])),
                "black": [r.get("display") for r in (rows or []) if r.get("black")],
                "confirmed_send": bool(send_record),
                "sent_chat": (send_record or {}).get("chat"),
                "sent_text": (send_record or {}).get("text")
                              or (((payload or {}).get("gen") or {}).get("text")),
            })
            self.state["processed_history"] = hist[:20]
            self.state.pop("awaiting_send", None)
            self.state.pop("retry_pending", None)
            self.retry_state = None
            save_state(self.state)
            if send_record:
                log_info("已确认真的发出去 -> 写 processed_fingerprint 到 state.json（重启后不重复回复）: "
                         "fp=%s（reply_state.json chat=%r at=%s）"
                         % (digest, (send_record or {}).get("chat"), (send_record or {}).get("at")))
            else:
                log_info("已记录「未发送」（%s）-> 写 processed_fingerprint 防反复生成: fp=%s"
                         % (note, digest))
        except Exception as exc:
            log_warn("state.json 持久化失败: %s" % exc)

    # ---- main loop ----
    def run(self):
        self._acquire_lock()
        log_info("=" * 70)
        log_info("微信看门狗 v%s 启动 (pid=%s)" % (VERSION, os.getpid()))
        log_info("配置: enabled=%s autoSend=%s interval=%ss quietHours=%s" % (
            self.config["enabled"], self.config["autoSend"],
            self.config["intervalSeconds"], self.config["quietHours"]))
        # 方案A 启动声明：唤醒 Agent 这条链已停用（不写 RPC/session 字样，保证日志扫描干净）
        log_info("Agent 唤醒通道已停用（方案A：进程内直接调官方 API 生成回复 + reply.py 发送；"
                 "不唤醒 Agent、不调远程唤醒接口）")
        gen_cfg = self.config.get("gen") or {}
        try:
            creds = genreply.load_credentials(
                os.path.join(BASE_DIR, str(gen_cfg.get("credentials") or ".credentials.json")))
            log_info("[GEN] 凭据 OK: provider=%s model=%s baseUrl=%s apiKey=sk-***（人设=%s）" % (
                creds.get("provider") or "-", creds.get("model"), creds.get("baseUrl"),
                str(gen_cfg.get("personaFile") or "persona.txt")))
        except Exception as exc:
            log_error("[GEN] 凭据读取失败（生成会失败，不会发送）: %s" % genreply.mask_key(exc))
        log_info("[GEN] 生成参数: maxTokens<=%s temperature=%s timeout=%ss maxReplyChars=%s ocrPreview=%s" % (
            gen_cfg.get("maxTokens"), gen_cfg.get("temperature"),
            gen_cfg.get("timeoutSeconds"), gen_cfg.get("maxReplyChars"),
            gen_cfg.get("ocrPreview")))
        gcfg = self.config.get("guard") or {}
        log_info("[GUARD] 发送护栏: whitelistOnly=%s perChatMinIntervalSeconds=%s perChatMaxBurst=%s "
                 "burstPauseSeconds=%s dedupeSameText=%s（发送记录 logs/sent-YYYYMMDD.jsonl）" % (
                     gcfg.get("whitelistOnly"), gcfg.get("perChatMinIntervalSeconds"),
                     gcfg.get("perChatMaxBurst"), gcfg.get("burstPauseSeconds"),
                     gcfg.get("dedupeSameText")))
        log_info("去重: settleScans=%s processed_fp=%s；白名单: %s" % (
            (self.config.get("dedupe") or {}).get("settleScans", 2),
            self.processed_fp or "(无)",
            self.config.get("whitelist") or "(空=不回复任何人)"))
        log_info("发送确认: 只有 reply.py 真的发出去（reply_state.json 新增 sent）才写 processed_fingerprint；"
                 "API/发送失败保留待处理重试 retryAfterSeconds=%ss maxRetryAttempts=%s；"
                 "triggerCooldownSeconds=%s" % (
                     self.config.get("retryAfterSeconds", 60),
                     self.config.get("maxRetryAttempts", 3),
                     self.config.get("triggerCooldownSeconds", 10)))
        log_info("信号文件: %s" % SIGNAL_PATH)
        log_info("发送记录: %s" % os.path.join(LOG_DIR, "sent-YYYYMMDD.jsonl"))
        if gen_cfg.get("testHooks"):
            log_warn("[TEST] gen.testHooks=true：写 tmp-inject-unread.json 可造一条白名单未读做端到端自测"
                     "（不影响真实消息处理；用完请删除该文件）")
        if busyguard is not None:
            bg = busyguard.merge_config(self.config)
            log_info("忙闲守护: enabled=%s【busyGuard: user-active / foreground-wechat / foreground-fullscreen "
                     "判定均已停用（userActiveSeconds=%ss、fullscreenMinCoverRatio=%s 保留但不再生效）】" % (
                         bg.get("enabled"), bg.get("userActiveSeconds"),
                         bg.get("fullscreenMinCoverRatio")))
            log_info("忙闲守护拦截条件（只剩 3 条）: ① foreground-game 游戏名单=%s | ② quiet-hours=%s | ③ rate-limit" % (
                bg.get("gameProcesses"), self.config.get("quietHours")))
        else:
            log_warn("忙闲守护模块 busyguard.py 不可用 -> 本次没有「你忙就别动」保护，请检查文件")
        log_info("急停: 创建 STOP 文件（或 toggle.ps1 panic）立即退出")
        # 2026-10-27 第一阶段：三处急停 + 熔断的启动日志（按用户要求加保护再上岗）
        _br = self.config.get("breaker") or {}
        _br_state = breaker_state_load()
        log_info("熔断: enabled=%s consecutiveFailures=%s resetOnSuccess=%s（当前连续失败=%s；"
                 "达标 -> breaker_tripped.json + STOP + 退出 + 气泡，熔断后不自启）" % (
                     _br.get("enabled"), _br.get("consecutiveFailures"), _br.get("resetOnSuccess"),
                     _br_state.get("consecutiveFailures")))
        log_info("急停入口: ① 桌面快捷方式『小耳朵急停』-> toggle.ps1 panic ② 全局热键 %s"
                 "（每轮 GetAsyncKeyState 检测） ③ 微信口令：白名单来信含『小耳朵』+『%s』-> 立即 STOP+退出" % (
                     PANIC_HOTKEY_LABEL,
                     "/".join(str(w) for w in ((self.config.get("panic") or {}).get("wechatKeywords")
                                               or ["急停", "停止"]))))
        log_info("=" * 70)
        if self._file_mtime(STOP_PATH) is not None:
            log_warn("启动时发现 STOP 文件，立即退出。删除 STOP 或 toggle.ps1 on 后重新启动。")
            self._release_lock()
            return 0
        # 熔断后被人为恢复（toggle.ps1 on 会删 STOP）-> 连续失败计数清零，重新给一次机会
        self.breaker_reset_after_manual_enable()
        disabled_logged = False
        while True:
            try:
                if self.stop_file_present():
                    log_warn("检测到 STOP 文件 -> 看门狗立即退出（急停生效）")
                    break
                self.reload_config_if_changed()
                cfg = self.config
                # 🔴 2026-10-27 第一阶段：全局急停热键 Ctrl+Alt+Shift+S（每轮 GetAsyncKeyState 检测；
                #    放在 enabled 判断之前，休眠时也能急停）。命中 -> 写 STOP + 气泡 + 结束自身进程。
                if (cfg.get("panic") or {}).get("enabled", True) and self.panic_hotkey_down():
                    self.do_panic("hotkey", "检测到全局热键 %s 四键同按（GetAsyncKeyState）"
                                  % PANIC_HOTKEY_LABEL)
                if not cfg.get("enabled", True):
                    if not disabled_logged:
                        log_info("总开关 enabled=false：看门狗休眠中（不截图、不检测、不唤醒）")
                        disabled_logged = True
                    # 休眠也按 0.2s 轮询急停热键（不被 3s 间隔漏掉）
                    self.hotkey_watch_sleep(float(cfg.get("intervalSeconds", 3)))
                    continue
                if disabled_logged:
                    log_info("总开关 enabled=true：看门狗恢复检测")
                    disabled_logged = False
                # 测试钩子：tmp-inject-unread.json 造一条白名单未读（只走一遍完整 生成->护栏->发送 链路）
                if ((self.config.get("gen") or {}).get("testHooks", True)
                        and os.path.exists(INJECT_PATH)):
                    try:
                        self._handle_inject_file()
                    except Exception as exc:
                        log_error("[TEST] 注入未读处理异常: %s\n%s" % (
                            exc, traceback.format_exc(limit=3)))
                    self.hotkey_watch_sleep(0.5)
                    continue
                interval = float(cfg.get("intervalSeconds", 3))
                info = self.scan_once()
                if info and info.get("available"):
                    if not self._warned_no_window:
                        log_info("微信窗口已就绪: %r，抓取方式=%s，未读=%s，行徽标=%s" % (
                            info.get("window_title", ""), info.get("source"),
                            info.get("unread"), info.get("row_badges")))
                        self._warned_no_window = True
                self.process_scan(info)
                self.hotkey_watch_sleep(max(0.5, interval))
            except KeyboardInterrupt:
                log_info("收到 Ctrl-C，退出")
                break
            except Exception as exc:
                log_error("主循环异常: %s\n%s" % (exc, traceback.format_exc(limit=3)))
                self.hotkey_watch_sleep(3)
        self._release_lock()
        return 0

    # ---- lock ----
    def _acquire_lock(self):
        try:
            if os.path.exists(LOCK_PATH):
                with open(LOCK_PATH, "r", encoding="utf-8") as f:
                    old_pid = int((f.read() or "0").strip() or 0)
                if old_pid and self._pid_alive(old_pid):
                    log_error("已有看门狗在运行 (pid=%d)，本次退出。如确认无进程，请删除 %s" % (old_pid, LOCK_PATH))
                    raise SystemExit(2)
            with open(LOCK_PATH, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
        except SystemExit:
            raise
        except Exception as exc:
            log_warn("锁文件处理失败: %s" % exc)

    @staticmethod
    def _pid_alive(pid):
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            code = wt.DWORD()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            kernel32.CloseHandle(handle)
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        except Exception:
            return False

    def _release_lock(self):
        try:
            os.remove(LOCK_PATH)
        except Exception:
            pass


# ─────────────────────────── CLI helpers ───────────────────────────


def print_status():
    cfg = load_config()
    state = load_state()
    print("微信看门狗 v%s 状态" % VERSION)
    print("  config : %s" % CONFIG_PATH)
    print("  enabled: %s     autoSend: %s     interval: %ss" % (
        cfg["enabled"], cfg["autoSend"], cfg["intervalSeconds"]))
    print("  quietHours: %s   maxRepliesPerHour: %s" % (cfg["quietHours"], cfg["maxRepliesPerHour"]))
    print("  triggerCooldownSeconds: %s   retryAfterSeconds: %s   maxRetryAttempts: %s" % (
        cfg.get("triggerCooldownSeconds"), cfg.get("retryAfterSeconds"),
        cfg.get("maxRetryAttempts")))
    print("  published_fp: %s   awaiting_send: %s   retry_pending: %s" % (
        state.get("processed_fingerprint") or "(无)",
        (state.get("awaiting_send") or {}).get("fp") or "(无)",
        (state.get("retry_pending") or {}).get("fp") or "(无)"))
    if busyguard is not None:
        bg = busyguard.merge_config(cfg)
        print("  忙闲守护: enabled=%s【只剩游戏硬拦截；user-active / foreground-wechat / "
              "foreground-fullscreen 判定均已停用】  gameProcesses=%s" % (
                  bg.get("enabled"), bg.get("gameProcesses")))
        try:
            g = busyguard.evaluate(cfg)
            fg = (g.get("checks") or {}).get("foreground") or {}
            idle = (g.get("checks") or {}).get("idleSeconds")
            print("  当前前台: %r（%r）idle=%.1fs（仅诊断，不参与判定）-> %s" % (
                fg.get("exe_base") or "?", (fg.get("title") or "")[:40], float(idle or 0),
                ("busy=%s reason=%s" % (True, g.get("reason"))) if g.get("busy") else "不忙（可动作）"))
        except Exception as exc:
            print("  忙闲守护自检失败: %s" % exc)
    else:
        print("  忙闲守护: busyguard.py 不可用（没有「你忙就别动」保护）")
    gen_cfg = cfg.get("gen") or {}
    gcfg = cfg.get("guard") or {}
    print("  回复生成: 进程内直连官方 API（方案A）credentials=%s persona=%s maxTokens<=%s temperature=%s" % (
        gen_cfg.get("credentials"), gen_cfg.get("personaFile"),
        gen_cfg.get("maxTokens"), gen_cfg.get("temperature")))
    try:
        creds = genreply.load_credentials(
            os.path.join(BASE_DIR, str(gen_cfg.get("credentials") or ".credentials.json")))
        print("  [GEN] provider=%s model=%s baseUrl=%s apiKey=sk-***" % (
            creds.get("provider") or "-", creds.get("model"), creds.get("baseUrl")))
    except Exception as exc:
        print("  [GEN] 凭据读取失败: %s" % genreply.mask_key(exc))
    print("  [GUARD] whitelistOnly=%s perChatMinIntervalSeconds=%s perChatMaxBurst=%s "
          "burstPauseSeconds=%s dedupeSameText=%s" % (
              gcfg.get("whitelistOnly"), gcfg.get("perChatMinIntervalSeconds"),
              gcfg.get("perChatMaxBurst"), gcfg.get("burstPauseSeconds"),
              gcfg.get("dedupeSameText")))
    print("  Agent 唤醒通道: disabled（方案A：不再唤醒 Agent；进程内直接调 API 生成回复）")
    gst = sendguard.load_state(GUARD_STATE_PATH)
    sent_chats = len(gst.get("chats") or {})
    today = sendguard.sent_log_path(LOG_DIR)
    print("  发送记录: %s（%s，已记录会话 %d 个）" % (
        today, "存在" if os.path.exists(today) else "今日暂无", sent_chats))
    print("  STOP 文件: %s" % ("存在（急停中）" if os.path.exists(STOP_PATH) else "不存在（可运行）"))
    # 2026-10-27 第一阶段：熔断 / 急停状态（离线可读）
    _br = cfg.get("breaker") or {}
    _bst = breaker_state_load()
    print("  熔断 breaker: enabled=%s 阈值=%s 当前连续失败=%s 上次失败=%s 上次原因=%r" % (
        _br.get("enabled"), _br.get("consecutiveFailures"),
        _bst.get("consecutiveFailures"), _bst.get("lastFailureAt") or "-",
        (_bst.get("lastReason") or "")[:60]))
    print("  熔断记录 breaker_tripped.json: %s" % (
        "存在（曾熔断，需人工处理）" if os.path.exists(BREAKER_TRIPPED_PATH) else "不存在"))
    print("  急停入口: 桌面「小耳朵急停」lnk / 全局热键 %s / 微信口令『小耳朵+急停|停止』; "
          "急停审计 logs/panic-YYYYMMDD.jsonl" % PANIC_HOTKEY_LABEL)
    print("  signal.json: %s" % ("存在" if os.path.exists(SIGNAL_PATH) else "不存在"))
    lock = None
    if os.path.exists(LOCK_PATH):
        try:
            lock = int(open(LOCK_PATH, encoding="utf-8").read().strip() or 0)
        except Exception:
            lock = None
    alive = bool(lock and Watchdog._pid_alive(lock))
    print("  运行状态: %s" % (("运行中 pid=%s" % lock) if alive else "未运行"))
    print("  logs: %s" % LOG_DIR)


def cmd_selftest():
    set_dpi_aware()
    global LOG
    LOG = Logger()
    wd = Watchdog()
    if busyguard is not None:
        try:
            g = busyguard.evaluate(wd.config)
            fg = (g.get("checks") or {}).get("foreground") or {}
            print("[selftest] 忙闲守护: busy=%s reason=%r 前台=%r idle=%.1fs 游戏名单=%s" % (
                g.get("busy"), g.get("reason"), fg.get("exe_base") or "?",
                float((g.get("checks") or {}).get("idleSeconds") or 0),
                busyguard.merge_config(wd.config).get("gameProcesses")))
        except Exception as exc:
            print("[selftest] 忙闲守护自检失败: %s" % exc)
    # 1) 枚举所有 Weixin.exe 顶层窗口（打印 class / title / visible / iconic / cloaked / 尺寸）
    cands = enumerate_weixin_windows()
    print("[selftest] Weixin 顶层窗口 %d 个：" % len(cands))
    for c in cands:
        print("  hwnd=%s class=%r title=%r visible=%s iconic=%s cloaked=%s size=%s" % (
            hex(c["hwnd"]), c["class"], c["title"], c["visible"], c["iconic"],
            c["cloaked"], c["size"]))
    win, reason, detail = select_wechat_window(cands)
    if win is None:
        print("[selftest] 没有可用窗口: reason=%s detail=%s" % (reason, detail))
        return 1
    print("[selftest] 选中窗口: hwnd=%s class=%r title=%r size=%s（遮挡与否不影响）" % (
        hex(win["hwnd"]), win["class"], win["title"], win["size"]))
    # 2) 三层抓图降级链
    img, source, attempts = capture_wechat_window(
        win["hwnd"], win["rect"], wd.config.get("detect", {}).get("capture", "auto"))
    print("[selftest] 抓图尝试: %s -> 采用 %s" % ("; ".join(attempts), source))
    if img is None:
        print("[selftest] 抓图全部失败"); return 1
    scale = dpi_scale_of(win["hwnd"])
    bgr = img_to_bgr(img)
    info = analyze_window(bgr, scale)
    print("[selftest] 列表区域=%s scale=%.3f  未读=%s  行徽标=%d  左栏聚合=%s  active_rows=%s" % (
        info["list_region"], scale, info.get("unread"), info.get("row_badges"),
        info.get("rail_unread"), info.get("active_rows")))
    print("[selftest] 行徽标: %s" % json.dumps(info.get("row_blob_list"), ensure_ascii=False))
    print("[selftest] 未读指纹: %s" % json.dumps(info.get("fingerprint"), ensure_ascii=False))
    tb = wd._taskbar_check(scale)
    print("[selftest] 任务栏层: %s" % json.dumps(tb, ensure_ascii=False))
    shot = os.path.join(BASE_DIR, "selftest-wechat.png")
    img.save(shot)
    print("[selftest] 截图: %s" % shot)
    return 0


def cmd_test_gen(argv):
    """只测「进程内直连 API 生成回复」，不发送、不碰微信（方案A 单测入口）。"""
    import argparse
    global LOG
    LOG = Logger()
    ap = argparse.ArgumentParser(prog="watcher.py --test-gen")
    ap.add_argument("--test-gen", action="store_true")
    ap.add_argument("--chat", default="文件传输助手")
    ap.add_argument("--incoming", default="你好，在吗？")
    args, _unknown = ap.parse_known_args(argv)
    cfg = load_config()
    gen_cfg = cfg.get("gen") or {}
    print("=== [test-gen] 进程内调官方 API（不发送）chat=%r incoming=%r ===" % (
        args.chat, args.incoming))
    res = genreply.generate(gen_cfg, BASE_DIR, args.chat, args.incoming,
                            log_info=lambda m: print(m, flush=True),
                            log_warn=lambda m: print(m, flush=True),
                            log_error=lambda m: print(m, flush=True))
    print("=== [test-gen] 结果: ok=%s text=%r error=%s latencyMs=%s ===" % (
        res.get("ok"), res.get("text"), res.get("error"), res.get("latencyMs")))
    return 0 if res.get("ok") else 1


def cmd_inject_unread(argv):
    """造一条白名单未读（写 tmp-inject-unread.json，由运行中的看门狗消费）。

    目的：桌面当前没有真实来信时，端到端验证「生成 -> 护栏 -> reply.py 真发送」。
    """
    import argparse
    global LOG
    LOG = Logger()
    ap = argparse.ArgumentParser(prog="watcher.py --inject-unread")
    ap.add_argument("--inject-unread", action="store_true")
    ap.add_argument("--chat", default="文件传输助手")
    ap.add_argument("--incoming", default="")
    ap.add_argument("--row", type=int, default=None,
                    help="直接指定会话列表行号（跳过全列表 OCR，测试更快）")
    args, _unknown = ap.parse_known_args(argv)
    doc = {"chatHint": args.chat, "incoming": args.incoming,
           "createdAt": _ts(), "note": "test-inject-unread（方案A 端到端自测）"}
    if args.row is not None:
        doc["row"] = int(args.row)
    tmp = INJECT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, INJECT_PATH)
    print("[inject] 已写 %s" % INJECT_PATH)
    print("[inject] 内容: %s" % json.dumps(doc, ensure_ascii=False))
    lock_pid = 0
    try:
        lock_pid = int(open(LOCK_PATH, encoding="utf-8").read().strip() or 0)
    except Exception:
        lock_pid = 0
    if not (lock_pid and Watchdog._pid_alive(lock_pid)):
        print("[inject] ⚠ 看门狗未运行：文件不会被消费（可先启动 watcher.py，或下次启动时消费）")
        return 1
    print("[inject] 看门狗 pid=%s 正在运行，等待消费…" % lock_pid)
    for _ in range(60):
        if not os.path.exists(INJECT_PATH):
            print("[inject] 看门狗已消费注入文件（完整链路见 logs/watchdog-*.log）")
            return 0
        time.sleep(0.5)
    print("[inject] 30s 内未被消费（看门狗可能 enabled=false / 窗口不可用 / 正在发送上一条）")
    return 1


def _selftest_print_plan(tag, text_shown, plan):
    """打印一条自测判定（所有输出都脱敏，不落审计、不发消息、不碰微信）。"""
    print("[remote-selftest] %s" % tag)
    print("  输入(已脱敏): %r" % text_shown)
    print("  判定: action=%s level=%s passwordOk=%s attempts=%s" % (
        plan.get("action"), plan.get("level") or "-", plan.get("passwordOk"),
        plan.get("attempts")))
    if plan.get("reply_text"):
        print("  将回微信: %r" % plan.get("reply_text"))
    if plan.get("prompt"):
        print("  将转告 Agent 的 prompt 前 200 字: %r"
              % redact_known_password(plan.get("prompt")[:200]))
    print("")


def cmd_remote_selftest(argv):
    """离线验证远程指令状态机（不碰微信、不发 RPC、不发消息、不写审计）。

    用法:
      watcher.py --remote-selftest                       # 跑内置 6 组场景
      watcher.py --remote-selftest "<模拟来信>" [...]     # 逐条判定（英文场景名请用引号）
    正确密码不硬编码在源码里：通过环境变量 REMOTE_SELFTEST_PWD1 / REMOTE_SELFTEST_PWD2 传入，
    这样本文件/日志/报告里都不会出现明文密码。没传时只跑负向场景。
    """
    import argparse
    global LOG
    LOG = Logger()
    ap = argparse.ArgumentParser(prog="watcher.py --remote-selftest")
    ap.add_argument("--remote-selftest", action="store_true")
    ap.add_argument("texts", nargs="*")
    args, _unknown = ap.parse_known_args(argv)
    cfg = load_config()
    remote_cfg = cfg.get("remote") or {}
    levels = remote_levels(remote_cfg)
    p1 = str(os.environ.get("REMOTE_SELFTEST_PWD1") or "").strip()
    p2 = str(os.environ.get("REMOTE_SELFTEST_PWD2") or "").strip()
    print("=== [remote-selftest] enabled=%s triggerWord=%r pendingTtl=%ss maxWrong=%s lockout=%ss ===" % (
        remote_cfg.get("enabled"), remote_cfg.get("triggerWord"),
        remote_cfg.get("pendingTtlSeconds"), remote_cfg.get("maxWrongAttempts"),
        remote_cfg.get("lockoutSeconds")))
    for name in sorted(levels):
        lv = levels[name]
        print("  级别 %s: label=%r allow=%s sha256=%s..." % (
            name, lv.get("label"), ",".join(str(a) for a in (lv.get("allow") or [])),
            str(lv.get("sha256") or "")[:16]))
    print("  REMOTE_SELFTEST_PWD1/PWD2 是否提供: %s / %s" % (bool(p1), bool(p2)))

    if args.texts:
        state = {"pending": {}, "lockouts": {}}
        for t in args.texts:
            plan = remote_plan("自测会话", t, remote_cfg, state)
            _selftest_print_plan("输入", redact_known_password(t), plan)
        print("=== 自测结束（单条模式，未发消息/未转告）===")
        return 0

    state = {"pending": {}, "lockouts": {}}

    def run(tag, chat, text):
        plan = remote_plan(chat, text, remote_cfg, state)
        _selftest_print_plan(tag, redact_known_password(text), plan)
        return plan

    print("---------- 场景 1：触发词 + 无密码 -> 需要密码（产生 pending）----------")
    run("S1 触发词+无密码", "自测S1", "小耳朵 把桌面报告发我")
    print("---------- 场景 2：pending 存在 + 一级密码 -> L1 通过 ----------")
    if p1:
        run("S2 待办+一级密码", "自测S1", p1)
    else:
        print("  跳过（未提供 REMOTE_SELFTEST_PWD1）\n")
    print("---------- 场景 3：无 pending + 一级密码 -> 普通聊天 ----------")
    if p1:
        run("S3 无待办+一级密码", "自测S3", p1)
    else:
        print("  跳过（未提供 REMOTE_SELFTEST_PWD1）\n")
    print("---------- 场景 4：二级密码 -> L2 通过（挑战-应答 / 一条到位）----------")
    run("S4a 触发词+无密码（准备待办）", "自测S4", "小耳朵 帮我执行一个命令")
    if p2:
        run("S4b 待办+二级密码", "自测S4", p2)
        run("S4c 一条到位二级密码", "自测S4c",
            "小耳朵 " + p2 + " 帮我把最新截图发到文件传输助手")
    else:
        print("  跳过（未提供 REMOTE_SELFTEST_PWD2）\n")
    print("---------- 场景 5：连续 3 次错误密码 -> lockout ----------")
    run("S5a 触发词+无密码（准备待办）", "自测S5", "小耳朵 帮我做点事")
    run("S5b 错密码 1", "自测S5", "061111")
    run("S5c 错密码 2", "自测S5", "061111")
    run("S5d 错密码 3 -> 锁死", "自测S5", "061111")
    print("---------- 场景 6：L1 密码 + 高危动作 -> 二级密码拦截 ----------")
    if p1:
        run("S6 一级密码+删盘", "自测S6", "小耳朵 " + p1 + " 帮我删掉 D 盘所有文件")
    else:
        print("  跳过（未提供 REMOTE_SELFTEST_PWD1）\n")
    print("---------- 场景 7：普通聊天 ----------")
    run("S7 普通聊天", "自测S7", "今天天气不错")
    print("=== 自测结束（全部为离线判定，未发消息、未转告、未写审计）===")
    return 0


def cmd_guard_selftest():
    """发送护栏自检：用真实 sendguard.check 跑 4 个场景，打印的 skip 行与线上日志一致。"""
    global LOG
    LOG = Logger()
    cfg = load_config()
    now = time.time()
    results = []

    def show(tag, verdict, expect):
        ok = str(verdict.get("skip")) == expect
        results.append((tag, ok, verdict))
        print("[GUARD] 场景%s -> %s（预期 %s）：skip=%r detail=%s" % (
            tag, "PASS" if ok else "FAIL", expect, verdict.get("skip"),
            verdict.get("detail")))

    st = {"chats": {}}
    v = sendguard.check(cfg, st, "某个非白名单会话", "在吗", whitelisted=False, now=now)
    show("① 白名单外", v, "whitelist-miss")

    sendguard.record(cfg, st, "文件传输助手", "收到啦", fingerprint="fp-selftest",
                     now=now - 10)
    v = sendguard.check(cfg, st, "文件传输助手", "好呀", whitelisted=True, now=now)
    show("② 同一会话 30s 内第二条", v, "per-chat-interval")

    v = sendguard.check(cfg, st, "文件传输助手", "收到啦", whitelisted=True, now=now + 40)
    show("③ 同一句话重复（隔 50s）", v, "duplicate-text")

    st2 = {"chats": {}}
    for i in range(5):
        sendguard.record(cfg, st2, "连续测试群", "第%d条" % i, now=now - 100 + i * 40)
    v = sendguard.check(cfg, st2, "连续测试群", "第六条", whitelisted=True, now=now + 100)
    show("④ 同一会话连续 >5 条", v, "per-chat-burst-limit")
    if v.get("stop"):
        until = time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(float(v.get("nextAllowedAt") or 0)))
        print("[GUARD] -> 已触发 stop：停了等你发话（pausedUntil=%s）" % until)

    tmp_guard = os.path.join(BASE_DIR, "tmp-guard-selftest.json")
    sendguard.save_state(tmp_guard, st)
    bad = [t for t, ok, _v in results if not ok]
    print("[GUARD] 自检结果: %d/%d 通过%s" % (
        len(results) - len(bad), len(results), "" if not bad else "，失败: %s" % bad))
    print("[GUARD] 自检用的是隔离状态文件 %s（不污染线上 guard_state.json）" % tmp_guard)
    return 0 if not bad else 1


def main(argv):
    global LOG
    LOG = Logger()
    os.makedirs(BASE_DIR, exist_ok=True)
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0
    if "--status" in argv:
        print_status(); return 0
    if "--selftest" in argv:
        return cmd_selftest()
    if "--test-gen" in argv:
        return cmd_test_gen(argv)
    if "--inject-unread" in argv:
        return cmd_inject_unread(argv)
    if "--guard-selftest" in argv:
        return cmd_guard_selftest()
    if "--remote-selftest" in argv:
        return cmd_remote_selftest(argv)
    if "--test-wake" in argv:
        print("[test-wake] 已停用：方案A 不再唤醒 Agent（进程内直接调 API 生成回复）。"
              "生成自测请用 --test-gen")
        return 1
    if "--once" in argv:
        set_dpi_aware()
        wd = Watchdog()
        info = wd.scan_once()
        if not info or not info.get("available"):
            print(json.dumps({k: v for k, v in (info or {}).items() if k != "img"}, ensure_ascii=False, indent=2))
            return 1
        print(json.dumps({
            "window": info.get("window_title"),
            "source": info.get("source"),
            "unread": info.get("unread"),
            "rail_unread": info.get("rail_unread"),
            "row_badges": info.get("row_badges"),
            "row_blob_list": info.get("row_blob_list"),
            "active_rows": info.get("active_rows"),
            "list_region": info.get("list_region"),
            "scale": info["geom"]["scale"],
            "taskbar": info.get("taskbar"),
        }, ensure_ascii=False, indent=2, default=str))
        wd.process_scan(info)
        return 0
    try:
        return Watchdog().run()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        log_error("致命错误: %s\n%s" % (exc, traceback.format_exc()))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
