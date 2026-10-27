# -*- coding: utf-8 -*-
"""微信看门狗 · 回复发送器 v3「键盘为主 + 必要时鼠标点会话行 + 忙闲守护 + 20 秒硬超时」

用法:
  python reply.py --chat "文件传输助手" --text "回复内容"
  可选: --dry-run  只验证「定位会话 + 清空输入框 + 输入内容」，不按 Enter 发送
  可选: --match "备用会话名"（v3 忽略，仅兼容旧参数）

动作序列（默认走键盘；必要时用鼠标点会话行——用户已明确同意，游戏运行时一律不动）:
  AttachThreadInput/ALT 切前台 -> Ctrl+F 搜索 -> 输入联系人名
  -> OCR 搜索下拉面板，定位「真正的会话行」（跳过 💥搜索网络结果 / 🔍命令行）
  -> 受控单击该行 -> 校验聊天标题 == 目标会话名（不匹配绝不输入正文、绝不按 Enter）
  -> Ctrl+A + Delete 清空输入框 -> 输入内容 -> OCR 校验输入框
  -> 发送前复检（前台不是游戏、用户没动键鼠、标题没变）-> Enter 发送
  -> 前后截图 + 像素/OCR 校验 -> 日志写「已发送」+ 点击坐标/标题校验结果

退路（首选搜索找不到时）：直接 OCR 会话列表，点目标行，同样校验标题。
⚠ 2026-10-25 实测（GUI agent 在真实窗口逐键验证）：微信搜索「文件传输助手」时
  第 1 行是「💥 搜索网络结果」，直接按 Enter 会打开「搜一搜」网页（新进程
  WeChatAppEx.exe 抢前台），会话根本不会切换；纯键盘 ↓+Enter 到会话行也没反应。
  唯一可靠路径 = 单击搜索结果里「功能/群聊」分组下的会话行。

安全（对应 2026-10-25「打游戏控制我左键」事故）:
  * 动作前 busyguard.evaluate()：游戏前台（硬拦截）/静默时段/限速
    -> 只写日志 `skip: busy (reason=…)` 后退出，不切窗口、不按键、不点鼠标。
  * 发送过程中持续复检：前台变游戏 -> abort；用户动键鼠/光标被移动 -> abort。
  * 每次动作整体硬超时 20 秒：到点看门狗线程立即释放所有按键并中止。
  * 鼠标只点微信窗口矩形内（越界一律 abort）；绝不点「搜索网络结果/搜一搜/网页条目」。
  * 不标已读、不删除消息；只发一条；120 秒内同会话同内容不重复发。
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import difflib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import busyguard  # noqa: E402
import watcher  # noqa: E402

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import win32clipboard  # noqa: E402

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

LOG_DIR = os.path.join(BASE_DIR, "logs")
SHOT_DIR = os.path.join(BASE_DIR, "screenshots")
REPLY_STATE = os.path.join(BASE_DIR, "reply_state.json")

# 整体动作硬超时（秒）——用户要求 20 秒
HARD_TIMEOUT_SECONDS = 20.0

# ─────────────── 键盘 VK ───────────────
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
VK_DELETE = 0x2E
VK_TAB = 0x09
VK_A = 0x41
VK_V = 0x56
VK_F = 0x46
VK_DOWN = 0x28

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

SW_RESTORE = 9

SCREENSHOT_EXPIRE_HOURS = 168


# ─────────────── 日志 / 截图 ───────────────
def log(msg):
    # 2026-10-26 需求：日志里的密码形态一律打 ******（走 watcher 的统一脱敏入口）
    try:
        _mask = getattr(watcher, "mask_sensitive", None)
        if _mask:
            msg = _mask(msg)
    except Exception:
        pass
    line = "%s [REPLY] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, "watchdog-%s.log" % time.strftime("%Y%m%d"))
        with io.open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def log_stage(stage, msg=""):
    """阶段日志：stage=guard/activate/search/type/send/done（哪一步退出都能看出来）。"""
    log("stage=%s %s" % (stage, msg))


def log_error(tag, exc=None):
    """异常/失败统一出口：[REPLY] ERROR <类型> <信息> + 完整 traceback 落日志（不静默 exit）。"""
    if exc is None:
        log("ERROR %s" % (tag,))
    else:
        log("ERROR %s %s: %s" % (tag, type(exc).__name__, exc))
    try:
        tb = traceback.format_exc()
        if tb and "NoneType: None" not in tb:
            log("TRACEBACK:\n%s" % tb)
    except Exception:
        pass


def fg_desc():
    """当前前台窗口的一句话描述（诊断用；只读）。"""
    try:
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            return "hwnd=0"
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(wt.HWND(hwnd), buf, 256)
        pid = wt.DWORD(0)
        user32.GetWindowThreadProcessId(wt.HWND(hwnd), ctypes.byref(pid))
        exe = os.path.basename(busyguard._process_image_name(pid.value) or "")
        return "hwnd=%s exe=%r title=%r" % (hex(hwnd), exe, buf.value[:40])
    except Exception as exc:
        return "fg_desc-失败: %s" % exc


def save_img(img, tag):
    if img is None:
        return ""
    try:
        os.makedirs(SHOT_DIR, exist_ok=True)
        path = os.path.join(SHOT_DIR, "reply-%s-%s.png" % (time.strftime("%Y%m%d-%H%M%S"), tag))
        img.save(path)
        return path
    except Exception:
        return ""


# ───────── 微信主窗口存活校验 + Esc 禁令（2026-10-27 第一阶段新增） ─────────
# 🔴 高怀疑真凶：Ctrl+F 没打开搜索层时按 Esc，Esc 会落在微信主窗口上 -> 微信 4.x 把窗口
#   收进托盘 = 用户看到的「她把微信窗口关了」。现在：
#   * **只有「已确证搜索层打开」时才允许按 Esc**（esc_if_search_confirmed）；
#   * 任何 Esc / 点击之后都校验微信主窗口仍在（IsWindow + IsWindowVisible + 尺寸合理），
#     消失 -> 日志 [ALERT] wechat-window-lost -> abort（watcher 侧据 abort 原因写 reply-failures）。
#   * 本阶段**不做**「自动重新拉起微信」的真机动作（代码意图留日志，真机验证留第二阶段）。
WECHAT_MIN_W = 400   # 「尺寸合理」下限（物理像素；1371x870 实测窗口远大于此）
WECHAT_MIN_H = 300


def wechat_window_alive(hwnd):
    """只读校验微信主窗口仍在（IsWindow + IsWindowVisible + 未最小化 + 尺寸合理）。返回 (ok, detail)。"""
    try:
        hwnd = int(hwnd or 0)
    except Exception:
        hwnd = 0
    if not hwnd:
        return False, "hwnd=0（没有目标窗口句柄）"
    try:
        if not user32.IsWindow(wt.HWND(hwnd)):
            return False, "IsWindow=false（窗口句柄已失效/窗口被销毁）"
        if not user32.IsWindowVisible(wt.HWND(hwnd)):
            return False, "IsWindowVisible=false（窗口被隐藏/收进托盘）"
        if user32.IsIconic(wt.HWND(hwnd)):
            return False, "IsIconic=true（窗口被最小化）"
    except Exception as exc:
        return False, "窗口状态校验异常: %s" % exc
    try:
        l, t, r, b = watcher.window_rect(hwnd)
        w, h = int(r - l), int(b - t)
    except Exception as exc:
        return False, "window_rect 异常: %s" % exc
    if w < WECHAT_MIN_W or h < WECHAT_MIN_H:
        return False, "尺寸异常 %dx%d（< %dx%d）" % (w, h, WECHAT_MIN_W, WECHAT_MIN_H)
    return True, "hwnd=%s size=%dx%d" % (hex(hwnd), w, h)


def check_wechat_window_alive(hwnd, phase=""):
    """Esc / 点击之后必须调用：窗口丢了 -> [ALERT] wechat-window-lost + abort（不连点、不继续注入）。"""
    ok, detail = wechat_window_alive(hwnd)
    if ok:
        return True, detail
    msg = "[ALERT] wechat-window-lost（阶段=%s；%s）" % (phase or "-", detail)
    log(msg)
    log_error(msg)
    exc = ReplyAbort("wechat-window-lost（阶段=%s；%s；已立即 abort，不再做任何键鼠注入、不自动拉起微信[留第二阶段]）"
                     % (phase or "-", detail))
    exc.user_message = "小耳朵的微信窗口不见了，这条没发出去，原因 wechat-window-lost"
    raise exc


def esc_if_search_confirmed(ctx, where=""):
    """🔴 只有「已确证搜索层打开」时才允许按 Esc；否则一个 Esc 都不发。

    搜索层确认来源：① 输入目标名后聊天输入框为空（verify_search_layer_after_typing 标 confirmed）；
    ② Ctrl+F 后像素差确证。确认记录超过 8 秒视为过期（防止拿旧证据去按 Esc）。
    返回是否真的按了 Esc。
    """
    if not getattr(ctx, "search_open", False):
        log("  不按 Esc（%s：搜索层未确证打开 -> 一个 Esc 都不发；Esc 落到微信主窗口会把窗口收进托盘）"
            % (where or "-"))
        return False
    ts = float(getattr(ctx, "search_open_ts", 0) or 0)
    age = time.time() - ts if ts else 999.0
    if age > 8.0:
        log("  不按 Esc（%s：搜索层确证已过期 %.1fs -> 一个 Esc 都不发）" % (where or "-", age))
        return False
    hwnd = int(_INJECT_TARGET.get("hwnd") or 0)
    if hwnd:
        ok, detail = wechat_window_alive(hwnd)
        if not ok:
            log("  不按 Esc（%s：微信窗口已不可用 %s -> 一个 Esc 都不发）" % (where or "-", detail))
            return False
    try:
        ctx.guard.tap(VK_ESCAPE)
    except ReplyAbort:
        raise
    log("  已按 Esc（%s：搜索层确证打开 + 前台=微信；Esc 后才做的窗口存活校验在 InputGuard 里）"
        % (where or "-"))
    return True


class ReplyAbort(Exception):
    pass


# ───────── 注入前「前台必须是目标微信窗口」强制校验（2026-10-26 23:3x 紧急修复） ─────────
# 事故：输入阶段把 Ctrl+A/Delete/Ctrl+V 注入了当时的前台 msedge（用户浏览器），并把微信窗口弄丢。
# 现在所有键鼠注入都必须先过这里：① ALT 残留先释放；② 前台 hwnd+pid 必须 = 目标微信窗口；
# 不通过 -> 抛 ReplyAbort(foreground-changed)，**一个字符都不注入**。
_INJECT_TARGET = {"hwnd": 0, "pid": 0, "exe": "", "label": "", "last_log": 0.0}


def _fg_hwnd():
    try:
        return int(user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def _hwnd_pid(hwnd):
    pid = wt.DWORD(0)
    try:
        user32.GetWindowThreadProcessId(wt.HWND(int(hwnd)), ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def _exe_base_of_pid(pid):
    try:
        p = watcher._process_image_name(int(pid))
        return os.path.basename(p).lower() if p else ""
    except Exception:
        return ""


def release_stuck_alt(where=""):
    """检测 ALT 是否残留按下；若按下则补一次 key_up 并记日志（不注入其它任何键）。"""
    try:
        state = int(user32.GetAsyncKeyState(VK_MENU) or 0)
    except Exception:
        state = 0
    if state & 0x8000:
        try:
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        except Exception:
            pass
        log("  [注入保护] %s 检测到 ALT 残留按下 -> 已补发 key_up(VK_MENU)" % (where or "-"))
        return True
    return False


def arm_injection_guard(hwnd, label=""):
    """启动注入保护：此后所有键鼠注入前都要求前台 = 这个微信窗口（hwnd+pid/exe）。"""
    pid = _hwnd_pid(hwnd)
    _INJECT_TARGET.update({"hwnd": int(hwnd or 0), "pid": pid,
                           "exe": _exe_base_of_pid(pid), "label": label, "last_log": 0.0})


def disarm_injection_guard():
    _INJECT_TARGET.update({"hwnd": 0, "pid": 0, "exe": "", "label": ""})


def check_injection_foreground(phase="", log_ok=False):
    """注入前强制校验。失败抛 ReplyAbort(foreground-changed)，之前绝不注入任何键鼠。"""
    release_stuck_alt(phase)
    tgt = int(_INJECT_TARGET.get("hwnd") or 0)
    if not tgt:
        return True
    fg = _fg_hwnd()
    if fg != tgt:
        hint = ""
        try:
            hint = "、当前前台 %s" % busyguard.foreground_window_info().get("exe_base")
        except Exception:
            pass
        raise ReplyAbort("foreground-changed（%s；注入前前台校验失败：前台 hwnd=%s%s，目标微信 hwnd=%s；"
                         "未注入任何键鼠）" % (phase or "-", hex(fg), hint, hex(tgt)))
    fg_pid = _hwnd_pid(fg)
    want_pid = int(_INJECT_TARGET.get("pid") or 0)
    want_exe = str(_INJECT_TARGET.get("exe") or "")
    if want_pid and fg_pid != want_pid:
        raise ReplyAbort("foreground-changed（%s；前台 hwnd 相同但 pid=%s != 微信 pid=%s；"
                         "未注入任何键鼠）" % (phase or "-", fg_pid, want_pid))
    if want_exe and _exe_base_of_pid(fg_pid) not in ("", want_exe):
        raise ReplyAbort("foreground-changed（%s；前台进程 exe=%r != %r；未注入任何键鼠）"
                         % (phase or "-", _exe_base_of_pid(fg_pid), want_exe))
    if phase:
        now = time.time()
        if (phase != _INJECT_TARGET.get("label")
                or now - float(_INJECT_TARGET.get("last_log") or 0) > 10.0):
            _INJECT_TARGET["label"] = phase
            _INJECT_TARGET["last_log"] = now
            log("  [注入保护] 前台校验 ok（hwnd=%s pid=%s exe=%s label=%s）" % (
                hex(fg), fg_pid, _exe_base_of_pid(fg_pid) or "?", phase))
    return True


# ─────────────── 输入注入（键盘 only；鼠标只保留成对 API 且永不使用） ───────────────
class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_ulonglong)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_ulonglong)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


class InputGuard:
    """所有键鼠注入的唯一出口：记录按下的键、成对 down/up、finally 全释放。"""

    def __init__(self):
        self.pressed = []
        self.mouse_down = False
        self.cursor0 = busyguard.cursor_pos()
        self.cursor_moved_by_us = False
        self.key_events = 0
        self.mouse_events = 0
        # 受控点击次数：打开会话时点搜索结果行/会话列表行（用户已同意用鼠标；游戏时被 busyGuard 拦死）
        self.clicks = 0
        t = busyguard.last_input_tick()
        self.last_synthetic_tick = t if t is not None else busyguard.tick_now()

    def _mark(self):
        t = busyguard.last_input_tick()
        now = busyguard.tick_now()
        self.last_synthetic_tick = max(now, t) if t is not None else now

    def key_down(self, vk):
        check_injection_foreground()
        user32.keybd_event(int(vk), 0, 0, 0)
        if int(vk) not in self.pressed:
            self.pressed.append(int(vk))
        self.key_events += 1
        self._mark()

    def key_up(self, vk):
        check_injection_foreground()
        user32.keybd_event(int(vk), 0, KEYEVENTF_KEYUP, 0)
        try:
            self.pressed.remove(int(vk))
        except ValueError:
            pass
        self.key_events += 1
        self._mark()

    def tap(self, vk, mods=()):
        for m in mods:
            self.key_down(m)
        self.key_down(vk)
        self.key_up(vk)
        for m in reversed(mods):
            self.key_up(m)
        # 2026-10-27 第一阶段：Esc 之后必须校验微信主窗口仍存在（非 Esc 按键不改窗口状态，跳过）
        self._after_inject("key", int(vk))

    def _after_inject(self, kind, vk=None):
        """Esc / 点击之后的微信主窗口存活校验（窗口丢了直接 abort，防止继续在错状态里注入）。"""
        tgt = int(_INJECT_TARGET.get("hwnd") or 0)
        if not tgt:
            return
        if kind == "key" and vk != VK_ESCAPE:
            return
        try:
            time.sleep(0.12 if kind == "key" else 0.06)
            check_wechat_window_alive(tgt, "Esc 后" if kind == "key" else "点击后")
        except ReplyAbort:
            raise
        except Exception:
            pass

    def type_unicode(self, text):
        """SendInput 逐字符注入 unicode（不改剪贴板）。每个字符注入前都复查前台。"""
        data = text.encode("utf-16-le")
        units = [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]
        for unit in units:
            check_injection_foreground()
            arr = (INPUT * 1)()
            arr[0].type = 1  # INPUT_KEYBOARD
            arr[0].u.ki = _KEYBDINPUT(0, unit, KEYEVENTF_UNICODE, 0, 0)
            user32.SendInput(1, ctypes.byref(arr), ctypes.sizeof(INPUT))
            arr2 = (INPUT * 1)()
            arr2[0].type = 1
            arr2[0].u.ki = _KEYBDINPUT(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)
            user32.SendInput(1, ctypes.byref(arr2), ctypes.sizeof(INPUT))
            self.key_events += 2
        self._mark()

    # ---- 鼠标接口：DOWN/UP 成对 + finally 兜底 UP ----
    def mouse_down_left(self):
        if self.mouse_down:
            return
        check_injection_foreground()
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        self.mouse_down = True
        self.mouse_events += 1
        self._mark()

    def mouse_up_left(self):
        if not self.mouse_down:
            return
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        self.mouse_down = False
        self.mouse_events += 1
        self._mark()

    def click_at(self, screen_x, screen_y):
        """受控单击一个屏幕坐标（用户已同意使用鼠标；游戏运行时走不到这里）。

        仅用于点微信窗口内的会话行；DOWN/UP 成对，finally 保 UP。
        不再强制还原光标（用户明确表态不介意鼠标被移动）。
        🔴 注入保护：点击前必须确认前台仍是目标微信窗口，否则 abort（不移动光标、不点击）。
        """
        check_injection_foreground()
        user32.SetCursorPos(int(screen_x), int(screen_y))
        # 2026-10-26 验收修复：把「我们自己造成的新光标位置」立刻同步给 cursor0，
        # 否则 ActivityMonitor 会把自己这一次受控点击误判成「用户动了鼠标」-> abort。
        self.cursor0 = busyguard.cursor_pos() or (int(screen_x), int(screen_y))
        self._mark()
        time.sleep(0.06)
        self.mouse_down_left()
        try:
            time.sleep(0.05)
        finally:
            self.mouse_up_left()
        self.clicks += 1
        self._mark()
        # 2026-10-27 第一阶段：任何点击之后都校验微信主窗口仍存在
        self._after_inject("click")
        return int(screen_x), int(screen_y)

    # ---- finally 兜底 ----
    def release_all(self):
        for vk in reversed(list(self.pressed)):
            try:
                user32.keybd_event(int(vk), 0, KEYEVENTF_KEYUP, 0)
            except Exception:
                pass
        self.pressed = []
        if self.mouse_down:
            try:
                user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            except Exception:
                pass
            self.mouse_down = False
        self._mark()

    def restore_cursor(self):
        """只在「本程序移动过光标」时还原（本版本 0 次鼠标，永远不会动）。"""
        if not self.cursor_moved_by_us or not self.cursor0:
            return
        try:
            user32.SetCursorPos(int(self.cursor0[0]), int(self.cursor0[1]))
        except Exception:
            pass


class ActivityMonitor:
    """识别「发送过程中用户真的动了键鼠」：光标位置变化 / 出现比本程序最后一次注入更新的输入。"""

    def __init__(self, guard):
        self.guard = guard
        self.cursor0 = guard.cursor0

    def user_active(self, tolerance_ms=25):
        cur = busyguard.cursor_pos()
        # 2026-10-26 验收修复：受控点击会移动光标，cursor0 必须跟着更新（见 InputGuard.click_at），
        # 否则我们自己点的那一下会被当成「用户动了鼠标」-> 下一个 checkpoint 误 abort。
        base = getattr(self.guard, "cursor0", None) or self.cursor0
        if cur and base and cur != base:
            return True, "光标位置从 %s 变到 %s（用户移动了鼠标）" % (base, cur)
        t = busyguard.last_input_tick()
        synth = self.guard.last_synthetic_tick
        if t is not None:
            delta = busyguard.tick_delta_ms(t, synth)
            if 0 < delta <= 0x7FFFFFFF and delta > tolerance_ms:
                return True, "最近键鼠输入比本程序最后一次注入新 %dms（用户有真实键鼠操作）" % delta
        return False, ""


# ─────────────── 整动作硬超时 ───────────────
class Ctx:
    def __init__(self, config, timeout=HARD_TIMEOUT_SECONDS):
        self.config = config
        self.timeout = float(timeout)
        self.t0 = time.monotonic()
        self.deadline = self.t0 + self.timeout
        self.guard = InputGuard()
        self.activity = ActivityMonitor(self.guard)
        self.done = False
        self.abort = threading.Event()
        self.abort_reason = ""
        self.abort_logged = False
        self.step = "init"
        self.search_open = False
        self.search_uncertain = False
        # 2026-10-27 第一阶段：搜索层「确证」时间戳（只有确证后才允许按 Esc）
        self.search_open_ts = 0.0
        self.search_confirmed_by = ""
        self.prev_fg = int(user32.GetForegroundWindow() or 0)
        self._thread = threading.Thread(target=self._watchdog, name="reply-hard-timeout", daemon=True)
        self._thread.start()

    def elapsed(self):
        return time.monotonic() - self.t0

    def remaining(self):
        """距硬超时截止还剩多少秒（用于发送前安全余量判断）。"""
        return max(0.0, self.deadline - time.monotonic())

    def _watchdog(self):
        while not self.done:
            if time.monotonic() >= self.deadline:
                self.guard.release_all()
                if not self.abort_logged:
                    self.abort_logged = True
                    self.abort_reason = "hard-timeout"
                    log("abort: hard-timeout（整体动作 > %.0f 秒硬超时，已立即释放所有按键并中止）"
                        % self.timeout)
                self.abort.set()
                t_end = time.monotonic() + 1.5
                while time.monotonic() < t_end and not self.done:
                    time.sleep(0.03)
                if not self.done:
                    log("硬超时后主流程未在 1.5 秒内退出 -> 强制结束进程（输入已释放）")
                    os._exit(9)
                return
            time.sleep(0.03)

    def sleep(self, seconds):
        end = time.monotonic() + max(0.0, float(seconds))
        while True:
            self.check()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.02, left))

    def check(self):
        if self.abort.is_set():
            raise ReplyAbort(self.abort_reason or "abort")
        if time.monotonic() >= self.deadline:
            raise ReplyAbort("hard-timeout")

    def checkpoint(self, phase, check_user=True):
        """发送过程中的安全检查点：硬超时 / 前台变游戏全屏 / 用户动键鼠。"""
        self.step = phase
        self.check()
        blocked, reason, detail = busyguard.foreground_blocked(self.config)
        if blocked:
            raise ReplyAbort("%s（阶段=%s；%s；未按 Enter）" % (reason, phase, detail))
        if check_user:
            active, why = self.activity.user_active()
            if active:
                raise ReplyAbort("user-active（阶段=%s；%s；未按 Enter）" % (phase, why))

    def mark_search_open(self, confirmed, by=""):
        """记录搜索层是否**确证**打开（决定后续能不能按 Esc）。"""
        self.search_open = bool(confirmed)
        self.search_uncertain = not bool(confirmed)
        self.search_open_ts = time.time() if confirmed else 0.0
        self.search_confirmed_by = str(by or "") if confirmed else ""
        if confirmed:
            log("  [搜索层] 已确证打开（依据=%s，ts=%.3f）" % (by or "-", self.search_open_ts))

    def finish(self):
        self.done = True
        try:
            self.guard.release_all()
        finally:
            self.guard.restore_cursor()


# ─────────────── 剪贴板（仅 SendInput 注入被吞时兜底） ───────────────
class Clip:
    def __init__(self):
        self.saved_text = None
        self.saved_ok = False
        self.used = False

    def save_text(self):
        try:
            win32clipboard.OpenClipboard()
            try:
                self.saved_text = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                self.saved_ok = True
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            self.saved_text = None
            self.saved_ok = False

    def set_text(self, text):
        for _ in range(3):
            try:
                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
                finally:
                    win32clipboard.CloseClipboard()
                return True
            except Exception:
                time.sleep(0.08)
        return False

    def restore(self):
        if self.saved_ok and self.saved_text is not None:
            try:
                self.set_text(self.saved_text)
                log("剪贴板已还原（本次用过剪贴板兜底）")
            except Exception:
                pass


# ─────────────── 微信窗口 / 抓图 / OCR ───────────────
def ensure_wechat_ready(ctx):
    """确保微信主窗口可用：最小化/隐藏时用 ShowWindow(SW_RESTORE) 恢复（窗口操作，不是鼠标）。"""
    log_stage("activate", "枚举 Weixin.exe 顶层窗口（只读）")
    cands = watcher.enumerate_weixin_windows()
    win, reason, detail = watcher.select_wechat_window(cands)
    if win is not None:
        return win
    main = [c for c in cands if c["class"] == watcher.WECHAT_CLASS or c["title"] == watcher.WECHAT_TITLE]
    main.sort(key=lambda c: (0 if (c["visible"] and not c["iconic"] and not c["cloaked"]) else 1,
                             -(c["size"][0] * c["size"][1])))
    if not main:
        raise ReplyAbort("wechat-not-found（没有枚举到 Weixin.exe 的主窗口；%s）" % (detail or reason))
    icon = main[0]
    if icon["iconic"] or not icon["visible"]:
        log("微信窗口当前=%s，用 ShowWindow(SW_RESTORE) 恢复（窗口操作，0 鼠标、0 键盘）"
            % ("最小化" if icon["iconic"] else "隐藏到托盘"))
        user32.ShowWindow(icon["hwnd"], SW_RESTORE)
    for _ in range(12):
        ctx.sleep(0.1)
        cands = watcher.enumerate_weixin_windows()
        win, reason, detail = watcher.select_wechat_window(cands)
        if win is not None:
            return win
    raise ReplyAbort("wechat-unavailable（%s；%s；未做任何按键/鼠标）" % (reason, detail))


def capture_window(ctx, hwnd):
    l, t, r, b = watcher.window_rect(hwnd)
    w, h = r - l, b - t
    if w <= 100 or h <= 100:
        raise ReplyAbort("capture-failed（窗口尺寸异常 %sx%s）" % (w, h))
    img = watcher.print_window(hwnd, w, h)
    ok, why = watcher.validate_capture(img)
    if ok:
        return img
    img2 = watcher.bitblt_window(hwnd, w, h)
    ok2, why2 = watcher.validate_capture(img2)
    if ok2:
        return img2
    raise ReplyAbort("capture-failed（printwindow=%s；bitblt=%s）" % (why, why2 or "无"))


def _same_process(hwnd_a, hwnd_b):
    """两个窗口是否属于同一进程（用于确认「前台确实是微信」）。"""
    try:
        pa, pb = wt.DWORD(0), wt.DWORD(0)
        user32.GetWindowThreadProcessId(wt.HWND(hwnd_a), ctypes.byref(pa))
        user32.GetWindowThreadProcessId(wt.HWND(hwnd_b), ctypes.byref(pb))
        return bool(pa.value and pa.value == pb.value)
    except Exception:
        return False


def capture_screen_window(ctx, hwnd):
    """整窗「屏幕」截图（ImageGrab）：微信搜索下拉是 Qt popup，PrintWindow 抓不到，
    必须用屏幕抓图（且抓图 bbox 正好 = 窗口矩形，所以图像坐标 == 窗口矩形坐标）。

    返回 PIL Image（RGB），尺寸必须与窗口矩形一致；失败一律 abort（不冒险在错图上点击）。
    """
    ctx.check()
    l, t, r, b = watcher.window_rect(hwnd)
    w, h = r - l, b - t
    if w <= 100 or h <= 100:
        raise ReplyAbort("capture-failed（窗口尺寸异常 %sx%s）" % (w, h))
    img = watcher.screen_grab((l, t, r, b))
    if img is None:
        raise ReplyAbort("screen-grab-failed（抓不到屏幕区域 %s；未做任何点击、未注入正文）"
                         % ((l, t, r, b),))
    if img.size != (w, h):
        raise ReplyAbort("screen-grab-size-mismatch（截图 %s != 窗口 %sx%s；未做任何点击）"
                         % (img.size, w, h))
    # 🔴 遮挡保护（2026-10-26 验收加固）：屏幕截图会被其它窗口（游戏/浏览器全屏）盖住。
    # 只有「前台窗口属于微信同一进程」时屏幕图才真的拍到微信；否则一律 abort ——
    # 绝不在一张别的窗口的画面上定位/点击（点击坐标是相对微信窗口的，点到别的窗口就是事故）。
    fg = int(user32.GetForegroundWindow() or 0)
    if fg and not _same_process(fg, hwnd):
        raise ReplyAbort("screen-covered（前台 %s 不属于微信进程，屏幕截图可能被其它窗口覆盖；"
                         "未做任何点击、未输入正文）" % fg_desc())
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img


def _force_foreground_noinput(hwnd):
    """零键鼠注入地把窗口切到前台（AttachThreadInput + SetForegroundWindow）。

    2026-10-25 23:0x 实测：WeChat 4.x 在「窗口可见但在后台」时，ALT 键技巧
    会显示 GetForegroundWindow()==微信，但微信后续收不到 Ctrl+F（列表区像素差
    恒为 0.0021）——换成本函数后 Ctrl+F 正常打开搜索层（像素差 0.0x~1）。"""
    if int(user32.GetForegroundWindow() or 0) == hwnd:
        return True
    fg_hwnd = int(user32.GetForegroundWindow() or 0)
    tid_fg = wt.DWORD(0)
    if fg_hwnd:
        user32.GetWindowThreadProcessId(wt.HWND(fg_hwnd), ctypes.byref(tid_fg))
    tid_me = kernel32.GetCurrentThreadId()
    attached = False
    try:
        if tid_fg.value and tid_fg.value != tid_me:
            attached = bool(user32.AttachThreadInput(tid_me, tid_fg.value, True))
        if user32.IsIconic(wt.HWND(hwnd)):
            user32.ShowWindow(wt.HWND(hwnd), SW_RESTORE)
        user32.BringWindowToTop(wt.HWND(hwnd))
        user32.SetForegroundWindow(wt.HWND(hwnd))
    finally:
        if attached:
            user32.AttachThreadInput(tid_me, tid_fg.value, False)
    end = time.time() + 1.0
    while time.time() < end:
        if int(user32.GetForegroundWindow() or 0) == hwnd:
            return True
        time.sleep(0.05)
    # 2026-10-26 用户要求「能不按 ALT 就不按」：AttachThreadInput 常失败，先多试一次
    # 纯 API 方案 SwitchToThisWindow（同样零键鼠注入；失败无副作用）。
    try:
        user32.SwitchToThisWindow(wt.HWND(hwnd), True)
    except Exception:
        pass
    end = time.time() + 0.6
    while time.time() < end:
        if int(user32.GetForegroundWindow() or 0) == hwnd:
            return True
        time.sleep(0.05)
    return int(user32.GetForegroundWindow() or 0) == hwnd


def activate_window(ctx, hwnd):
    """零鼠标切前台：先 AttachThreadInput 无输入技巧，失败再退回 ALT 键技巧（0 次鼠标）。"""
    log_stage("activate", "开始切前台: target=%s 当前前台 %s" % (hex(hwnd), fg_desc()))
    if int(user32.GetForegroundWindow() or 0) == hwnd:
        log_stage("activate", "微信已经在前台，无需切换")
        return True
    if user32.IsIconic(wt.HWND(hwnd)):
        user32.ShowWindow(wt.HWND(hwnd), SW_RESTORE)
        log_stage("activate", "微信窗口原来是最小化 -> ShowWindow(SW_RESTORE)（0 鼠标 0 键盘）")
    ctx.check()
    # 方式 1：AttachThreadInput + SetForegroundWindow（不注入任何键鼠；实测对微信更可靠）
    try:
        if _force_foreground_noinput(hwnd):
            ctx.sleep(0.25)   # 等微信处理完 WM_ACTIVATE，再发 Ctrl+F
            if int(user32.GetForegroundWindow() or 0) == hwnd:
                log_stage("activate", "AttachThreadInput 切前台成功（0 键鼠注入）; 现在前台 %s" % fg_desc())
                return True
    except ReplyAbort:
        raise
    except Exception as exc:
        log("  AttachThreadInput 切前台异常（改用 ALT 键技巧兜底）: %s" % exc)
    ctx.check()
    # 方式 2：ALT 键技巧兜底（只有 AttachThreadInput 不可用时才按 ALT）
    # 🔴 2026-10-26 加固：key_down(VK_MENU) -> SetForegroundWindow -> key_up(VK_MENU)
    #   外层再包 try/finally 双保险，保证任何异常路径下 ALT 都已释放（否则下一次
    #   Enter 可能变成 Alt+Enter 之类）。日志里明确写「本次使用 ALT / ALT 已释放」。
    if user32.IsIconic(wt.HWND(hwnd)):
        user32.ShowWindow(wt.HWND(hwnd), SW_RESTORE)
    release_stuck_alt("ALT 切前台前")
    alt_used = True
    alt_released = False
    try:
        ctx.guard.key_down(VK_MENU)
        try:
            user32.SetForegroundWindow(wt.HWND(hwnd))
        finally:
            ctx.guard.key_up(VK_MENU)
    finally:
        # 双保险：万一 key_up 本身抛异常，补一次 UP
        if int(VK_MENU) in getattr(ctx.guard, "pressed", []):
            try:
                ctx.guard.key_up(VK_MENU)
            except Exception:
                pass
        try:
            alt_released = not (int(user32.GetAsyncKeyState(VK_MENU) or 0) & 0x8000)
        except Exception:
            alt_released = int(VK_MENU) not in getattr(ctx.guard, "pressed", [])
    log_stage("activate", "AttachThreadInput 未成功 -> 用 ALT 键技巧兜底（0 鼠标；"
              "本次使用 ALT=%s；ALT 已释放=%s（GetAsyncKeyState 复核））"
              % (alt_used, alt_released))
    for _ in range(10):
        ctx.sleep(0.1)
        if int(user32.GetForegroundWindow() or 0) == hwnd:
            ctx.sleep(0.15)
            log_stage("activate", "ALT 键技巧切前台成功; 现在前台 %s" % fg_desc())
            return True
    ok = int(user32.GetForegroundWindow() or 0) == hwnd
    log_stage("activate", "切前台失败 ok=%s 当前前台 %s" % (ok, fg_desc()))
    return ok


_TESS = None


def tesseract_bin():
    global _TESS
    if _TESS is None:
        _TESS = watcher.find_tesseract("")
    if not _TESS:
        raise ReplyAbort("tesseract-missing（OCR 不可用；未做任何发送动作）")
    return _TESS


def geom_of(img, scale):
    bgr = watcher.img_to_bgr(img)
    info = watcher.analyze_window(bgr, scale)
    g = info["geom"]
    W, H = img.size
    s = scale
    lx0 = int(round(g["list_left"] * s))
    lx1 = min(W, int(round(g["list_right"] * s)))
    # 聊天标题区：只裁「聊天区」顶部（y 按窗口高度比例 0.045~0.105H），
    # x 从会话列表右侧开始、最多到聊天区的 ~62% —— 绝不能框进左侧会话列表/搜索框，
    # 也不能框到右侧可能弹出的「搜一搜」面板（旧代码裁到 W-120s，实测把两列都框进去，
    # 导致明明切过去了却判标题不匹配；2026-10-25 GUI agent 实测确认）。
    hx0 = min(W - 8, lx1 + int(6 * s))
    hx1 = min(W - int(8 * s), lx1 + max(int(140 * s), int(round(0.62 * (W - lx1)))))
    return {
        "s": s, "W": W, "H": H, "lx0": lx0, "lx1": lx1,
        "list": (max(0, lx0), int(44 * s), min(W, lx1), max(0, H - int(60 * s))),
        "header": (hx0, max(0, int(round(0.045 * H))), hx1, max(0, int(round(0.105 * H)))),
        # 2026-10-26 23:2x 修正：旧 input 区域上沿=H-180s（870 高时≈645），而聊天区下沿=H-150s（≈682），
        # 两者重叠 37px —— 消息多/最后一条气泡贴近输入框时，会把「气泡文字」OCR 进输入框校验，
        # 导致 post-click-validation-failed 误 abort（实测 23:26 打不开文件传输助手）。
        # 输入框实际内区上沿按 H-145s 取（≈689），与聊天区脱开。
        "input": (max(0, lx1 + int(10 * s)), max(0, H - int(145 * s)),
                  min(W, W - int(120 * s)), max(0, H - int(70 * s))),
        "search": (max(0, lx0 + int(8 * s)), int(44 * s), min(W, lx1 - int(25 * s)), int(96 * s)),
        "chat": (max(0, lx1 + int(13 * s)), int(44 * s), min(W, W - int(8 * s)),
                 max(0, H - int(150 * s))),
        # 搜索下拉面板（Qt 弹出层，PrintWindow 抓不到，必须整屏截图）：
        # 按窗口宽高比例，约为 x 0.06~0.47W、y 0.05~0.80H（2026-10-25 实测校准）
        "dropdown": (max(0, int(round(0.06 * W))), max(0, int(round(0.05 * H))),
                     min(W, int(round(0.47 * W))), min(H, int(round(0.80 * H)))),
    }


DEFAULT_PLANS = (
    ("plain2x", False, 7),
    ("bin3x", True, 6),
    ("norm3x", True, 6),
)


def ocr_candidates(img, box, ctx, max_plans=2):
    tess = tesseract_bin()
    out = []
    x0, y0, x1, y1 = [int(v) for v in box]
    W, H = img.size
    x0 = max(0, min(W, x0)); x1 = max(0, min(W, x1))
    y0 = max(0, min(H, y0)); y1 = max(0, min(H, y1))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return out
    crop = img.crop((x0, y0, x1, y1))
    for mode, use_bbox, psm in DEFAULT_PLANS:
        ctx.check()
        if len(out) >= max_plans:
            break
        try:
            arr = watcher._ocr_prep(crop, mode, use_bbox)
            if arr is None:
                continue
            txt = watcher._tess_run(arr, psm, tess, "chi_sim", 8000)
            if txt:
                out.append(txt)
        except ReplyAbort:
            raise
        except Exception:
            continue
    return out


def name_match_region(img, box, target, ctx, match_cfg, max_plans=2):
    texts = ocr_candidates(img, box, ctx, max_plans=max_plans)
    m = watcher.name_match(texts, [target], match_cfg)
    return bool(m.get("hit")), texts, m.get("display") or ""


def text_match(texts, want):
    """回复正文校验：OCR 文本与待发文本做归一化 + 覆盖率匹配。"""
    w = watcher.normalize_name(want)
    if not w:
        return False
    for raw in texts:
        n = watcher.normalize_name(raw)
        if not n:
            continue
        if w == n or w in n or (len(n) >= 2 and n in w):
            return True
        try:
            cov, matched = watcher._coverage(n, w)
        except Exception:
            continue
        if matched >= max(2, int(len(w) * 0.35)) and cov >= 0.35:
            return True
    return False


def text_match_region(img, box, want, ctx, max_plans=2):
    texts = ocr_candidates(img, box, ctx, max_plans=max_plans)
    return text_match(texts, want), texts


def text_recall(texts, want):
    """OCR 文本对 want 的「字符召回率」（matched / len(want)）。

    用途：搜索框区域的 OCR 常常把放大镜图标/滚动条/按钮一起识别进来，
    使 name_match 的 coverage（matched/len(candidate)）永远达不到阈值（实测
    '〇文件传输助手 四 中 XXX' 这类结果）。此时用召回率判断「目标名字到底有没有输进去」更合适。
    返回 (best_cov, best_matched)。
    """
    w = watcher.normalize_name(want)
    if not w:
        return 0.0, 0
    best_cov, best_matched = 0.0, 0
    for raw in texts or []:
        n = watcher.normalize_name(raw)
        if not n:
            continue
        try:
            sm = difflib.SequenceMatcher(None, n, w, autojunk=False)
            matched = sum(b.size for b in sm.get_matching_blocks())
        except Exception:
            continue
        cov = matched / float(len(w))
        if cov > best_cov:
            best_cov, best_matched = cov, matched
    return best_cov, best_matched


# ─────────────── 搜索下拉面板：OCR 找「真正的会话行」并单击 ───────────────
# 2026-10-25/26 实测（真实窗口）：微信 4.x 的 Ctrl+F 下拉面板里既有「真正的会话行」，
# 也有「💥 搜索网络结果 / 🔍xx打开 / 恢复数据 / 已读功能 / 并粘贴 …」这类命令条目：
#   * 按 Enter 会打开「搜一搜」网页（新进程 WeChatAppEx.exe 抢前台），会话根本不切换；
#   * 纯键盘 ↓+Enter 到会话行也没反应；
#   * 唯一可靠路径 = 在「整窗屏幕截图（含 Qt popup，PrintWindow 抓不到）」里 OCR 定位会话行
#     -> 受控单击该行 -> 校验聊天标题 == 目标会话名；
#   * 命令/网页条目一律排除，宁可 abort 也不误点（2026-10-26 验收要求）。
GROUP_HEADERS = ("功能", "联系人", "群聊", "聊天记录", "公众号", "服务号",
                 "最近使用", "最常使用", "搜索历史",
                 "小程序", "视频号", "全部")

# 命令/网页条目特征：出现即视为「命令行」，绝不当会话行点击
COMMAND_MARKS = ("搜索网络结果", "搜一搜", "打开", "恢复数据", "已读", "粘贴",
                 "扫描功能", "怎么", "只有自己", "开启", "设置", "帮助", "更多",
                 "清理", "翻译", "收藏")


def _clean_row_text(text):
    """会话行 OCR 文本归一化：只留中文/字母/数字，去掉 | - 空格 与图标噪声。"""
    out = []
    for ch in str(text or ""):
        if ch.isalnum() or ("\u4e00" <= ch <= "\u9fff"):
            out.append(ch)
    return "".join(out)


def _is_command_row(text, target_clean):
    """命令/网页条目判定（宁可 abort 也不误点）：是目标名本身之外还带命令关键词的，一律算命令。"""
    clean = _clean_row_text(text)
    if not clean or clean == target_clean:
        return False
    for mark in COMMAND_MARKS:
        if mark in clean:
            return True
    return False


def mouse_desc(ctx):
    """日志口径：点了几个坐标 / 底层鼠标事件数（用户已同意用鼠标点会话行，游戏时被拦死）。"""
    g = getattr(ctx, "guard", None)
    clicks = getattr(g, "clicks", 0) if g is not None else 0
    events = getattr(g, "mouse_events", 0) if g is not None else 0
    return "受控点击 %d 次（底层鼠标事件 %d）" % (clicks, events)


def _ocr_tsv_lines(img, box, ctx, psm=6):
    """对区域做 tesseract TSV OCR，按视觉行聚合成 [{y,top,bottom,x0,x1,text,norm}]。

    2026-10-26 增强（会话行选择精度改造）：
      * 两遍预处理再合并：
          ① 灰度反色：普通浅色文字（命令行/分组标题/普通会话行）
          ② 绿色掩膜：只留「绿通道显著高于红/蓝」的绿色文字（微信把搜索命中的关键词/高亮会话名
             渲染成绿色；旧版灰度预处理会把高亮会话行 OCR 成乱码，进而误点命令行 → 打开搜一搜）
      * 绿色稀疏文字用 psm=11 才能读全整行（psm=6 会截断成「文件 传输」）
      * 输出带 top/bottom（原图坐标），供面板定位 / 行高估算 / 图标区域裁切使用
    """
    tess = tesseract_bin()
    W, H = img.size
    x0, y0, x1, y1 = [int(v) for v in box]
    x0 = max(0, min(W, x0)); x1 = max(0, min(W, x1))
    y0 = max(0, min(H, y0)); y1 = max(0, min(H, y1))
    if x1 - x0 < 40 or y1 - y0 < 40:
        return []
    pad = 20
    crop = img.crop((x0, y0, x1, y1))
    rgb = np.array(crop)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    inv = cv2.bitwise_not(gray)      # 深色界面 + 浅色文字 -> 反色给 tesseract
    arrays = [("gray", inv, psm)]
    try:
        rgb16 = rgb.astype(np.int16)
        g_excess = rgb16[:, :, 1] - np.maximum(rgb16[:, :, 0], rgb16[:, :, 2])
        green_mask = np.where(g_excess > 25, 0, 255).astype(np.uint8)  # 绿字->黑，其它->白
        if float((green_mask < 128).mean()) > 0.004:   # 有明显绿色文字才跑第二遍
            arrays.append(("green", green_mask, 11))
    except Exception:
        pass
    tmp_dir = os.path.join(BASE_DIR, "tmp-ocr")
    all_lines = []
    for tag, arr, use_psm in arrays:
        big = cv2.resize(arr, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        big = cv2.copyMakeBorder(big, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)
        path = ""
        try:
            os.makedirs(tmp_dir, exist_ok=True)
            fd, path = tempfile.mkstemp(prefix="reply-ocr-", suffix=".png", dir=tmp_dir)
            os.close(fd)
            from PIL import Image as _Image
            _Image.fromarray(big).save(path)
            p = subprocess.run(
                [tess, path, "stdout", "-l", "chi_sim", "--psm", str(use_psm),
                 "--oem", "1", "--dpi", "300", "tsv"],
                capture_output=True, timeout=8.0, creationflags=0x08000000)
        except Exception as exc:
            log("  [下拉] TSV OCR 失败(%s): %s" % (tag, exc))
            continue
        finally:
            try:
                if path:
                    os.remove(path)
            except Exception:
                pass
        groups = {}
        for line in p.stdout.decode("utf-8", "replace").splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) < 12 or parts[0] != "5":
                continue
            try:
                left, top, ww, hh = int(parts[6]), int(parts[7]), int(parts[8]), int(parts[9])
                conf = float(parts[10])
            except Exception:
                continue
            txt = (parts[11] or "").strip()
            if not txt or conf < 30:
                continue
            groups.setdefault((parts[2], parts[3], parts[4]), []).append(
                (left, top, ww, hh, txt))
        for words in groups.values():
            words.sort(key=lambda x: x[0])
            txt = " ".join(w[4] for w in words)
            y = sum(w[1] + w[3] / 2.0 for w in words) / len(words)
            yt = min(w[1] for w in words)
            yb = max(w[1] + w[3] for w in words)
            xa = min(w[0] for w in words)
            xb = max(w[0] + w[2] for w in words)
            all_lines.append({
                "text": txt,
                # 2x 放大 + pad -> 还原到原图坐标
                "y": y / 2.0 + y0 - pad / 2.0,
                "top": yt / 2.0 + y0 - pad / 2.0,
                "bottom": yb / 2.0 + y0 - pad / 2.0,
                "x0": xa / 2.0 + x0 - pad / 2.0,
                "x1": xb / 2.0 + x0 - pad / 2.0,
            })
    # 按 y 邻近合并两遍结果（同一行：text 保留候选，用 " | " 连接）
    merged = []
    for r in sorted(all_lines, key=lambda x: x["y"]):
        hit = None
        for m in merged:
            if abs(m["y"] - r["y"]) > 16:
                continue
            # 只有 x 范围有重叠才是「同一行的两遍 OCR 结果」；否则是聊天区/面板外的另一行文字，
            # 不能合并（否则会把会话行文本和后台聊天列表文本拼成乱码，导致 exact 匹配失败）
            if r["x1"] < m["x0"] - 40 or r["x0"] > m["x1"] + 40:
                continue
            hit = m
            break
        if hit is None:
            merged.append(dict(r, texts=[r["text"]]))
        else:
            if r["text"] not in hit["texts"]:
                hit["texts"].append(r["text"])
            hit["x0"] = min(hit["x0"], r["x0"])
            hit["x1"] = max(hit["x1"], r["x1"])
            hit["top"] = min(hit["top"], r["top"])
            hit["bottom"] = max(hit["bottom"], r["bottom"])
    out = []
    for m in merged:
        txt = " | ".join(m["texts"])
        out.append({"text": txt, "norm": watcher.normalize_name(txt),
                    "y": m["y"], "top": m["top"], "bottom": m["bottom"],
                    "x0": m["x0"], "x1": m["x1"]})
    out.sort(key=lambda r: r["y"])
    return out


def _sim_text(a, b):
    if not a or not b:
        return 0.0
    try:
        return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    except Exception:
        return 0.0


# ───────── 2026-10-26 会话行选择精度改造（用户反馈：误触聊天记录 / 面板位置 / 误点群聊） ─────────
# 总原则：宁可 abort 不发送，也绝不能点进错会话、绝不能发到错的人那里。
#
# 图标标定值（用 screenshots\*-dropdown.png 真实截图标定，selftest_rows.py 可复现）：
#   真会话行（左侧=会话头像：彩色方块/圆）：colorful=0.27~0.45  satMean=69~102  satStd=96~113
#   聊天记录/命令条目（左侧=灰色放大镜）：colorful=0.01~0.05 satMean=8~18    satStd=19~39
# 判定：colorful>=0.15 且 satMean>=45 且 satStd>=60 -> avatar（头像）
#       colorful<=0.05 且 satMean<=30            -> gray（灰色放大镜/非头像）
#       其它                                      -> unknown（一律不当会话行）
ICON_AVATAR_MIN_COLORFUL = 0.15
ICON_AVATAR_MIN_SATMEAN = 45.0
ICON_AVATAR_MIN_SATSTD = 60.0
ICON_GRAY_MAX_COLORFUL = 0.05
ICON_GRAY_MAX_SATMEAN = 30.0


def _icon_stats(img, y_center, row_h, s=1.0, anchor_x0=None):
    """取一行左侧「图标区域」的颜色统计（头像彩色高饱和，放大镜近灰低饱和）。

    锚点 anchor_x0 = 该行中「与目标名最相似的那段文字」的 x0（由 analyze_dropdown 传进来）。
    图标区域 = 锚点左侧一个 row_h 宽的窗口（头像方块/灰色放大镜就在文字前面）。
    这样不会把微信搜索关键词的绿色高亮文字（在文字区）误当成头像。
    """
    W, H = img.size
    icon_w = max(24, int(round(row_h * 0.95)))
    if anchor_x0 is None:
        anchor_x0 = 0.10 * W
    x1 = int(round(float(anchor_x0) - 8.0 * s))
    x0 = int(round(x1 - icon_w))
    x0 = max(0, min(W - 2, x0)); x1 = max(x0 + 6, min(W, x1))
    y0 = int(round(y_center - row_h * 0.34))
    y1 = int(round(y_center + row_h * 0.34))
    y0 = max(0, min(H - 2, y0)); y1 = max(y0 + 4, min(H, y1))
    try:
        arr = np.array(img.crop((x0, y0, x1, y1)))
        hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
        S = hsv[:, :, 1].astype(float)
        V = hsv[:, :, 2].astype(float)
        colorful = float(((S >= 70) & (V >= 60)).mean())
        return {"box": (x0, y0, x1, y1), "colorful": colorful,
                "satMean": float(S.mean()), "satStd": float(S.std()),
                "rgbStd": float(arr.reshape(-1, 3).std(axis=0).mean())}
    except Exception:
        return {"box": (x0, y0, x1, y1), "colorful": 0.0, "satMean": 0.0,
                "satStd": 0.0, "rgbStd": 0.0}


def _classify_icon(stats):
    """根据图标颜色统计判定行类型：avatar / gray / unknown。"""
    if not stats:
        return "unknown"
    if (stats["colorful"] >= ICON_AVATAR_MIN_COLORFUL
            and stats["satMean"] >= ICON_AVATAR_MIN_SATMEAN
            and stats["satStd"] >= ICON_AVATAR_MIN_SATSTD):
        return "avatar"
    if (stats["colorful"] <= ICON_GRAY_MAX_COLORFUL
            and stats["satMean"] <= ICON_GRAY_MAX_SATMEAN):
        return "gray"
    return "unknown"


def _is_group_header(text):
    """该行是否是搜索下拉的「分组标题」（功能/联系人/群聊/聊天记录/公众号/服务号…）。"""
    c = _clean_row_text(text)
    return bool(c) and any(c == h for h in GROUP_HEADERS)


def _ocr_words(img, box, ctx, psm=6, zoom=2.0, psms=None, include_orig=True):
    """对区域做 tesseract TSV OCR，返回词级列表 [{text,x0,x1,top,bottom,y,src}]。

    2026-10-26：从「按 tesseract 的 block/par/line 聚合」改为「拿词级坐标自己聚类」——
    tesseract 会把同一 y 的面板文字和右侧聊天区文字归进同一 line，导致拼出 x0=0/x1=1371 的乱行。
    2026-10-26 晚（真机 row-approximate 修复·治本）：
      * zoom 可控（默认 2.0x INTER_CUBIC）：下拉小字号中文放大后识别明显更准；
      * 同一张放大图跑 psm（默认 6）+ 额外 psms（默认 11）两遍，行级结果在 `_ocr_rows` 里合并；
      * include_orig=True 时再跑一遍「原始未放大」结果作对照（日志里打印放大版 / 原始版）。
    """
    tess = tesseract_bin()
    W, H = img.size
    x0, y0, x1, y1 = [int(v) for v in box]
    x0 = max(0, min(W, x0)); x1 = max(0, min(W, x1))
    y0 = max(0, min(H, y0)); y1 = max(0, min(H, y1))
    if x1 - x0 < 40 or y1 - y0 < 40:
        return []
    pad = 20
    crop = img.crop((x0, y0, x1, y1))
    rgb = np.array(crop)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    gray_inv = cv2.bitwise_not(gray)
    z = float(zoom or 2.0)
    # 同一个放大图的多 PSM（psm 6 段落 / psm 11 稀疏文字）；多遍结果在 _ocr_rows 里做行级并集
    arrays = [("gray-z%.1f-p%d" % (z, psm), gray_inv, z, psm)]
    for extra in (psms or ()):
        if int(extra) != int(psm):
            arrays.append(("gray-z%.1f-p%d" % (z, int(extra)), gray_inv, z, int(extra)))
    # 原始（未放大）对照：只为日志对比 + 候选变体兜底
    if include_orig and abs(z - 1.0) > 1e-6:
        arrays.append(("gray-orig-p%d" % psm, gray_inv, 1.0, psm))
    try:
        rgb16 = rgb.astype(np.int16)
        g_excess = rgb16[:, :, 1] - np.maximum(rgb16[:, :, 0], rgb16[:, :, 2])
        green_mask = np.where(g_excess > 25, 0, 255).astype(np.uint8)
        if float((green_mask < 128).mean()) > 0.004:
            arrays.append(("green-z%.1f-p11" % z, green_mask, z, 11))
    except Exception:
        pass
    tmp_dir = os.path.join(BASE_DIR, "tmp-ocr")
    words = []
    for tag, arr, use_zoom, use_psm in arrays:
        big = arr if abs(use_zoom - 1.0) < 1e-6 else cv2.resize(
            arr, None, fx=use_zoom, fy=use_zoom, interpolation=cv2.INTER_CUBIC)
        big = cv2.copyMakeBorder(big, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)
        path = ""
        try:
            os.makedirs(tmp_dir, exist_ok=True)
            fd, path = tempfile.mkstemp(prefix="reply-ocr-", suffix=".png", dir=tmp_dir)
            os.close(fd)
            from PIL import Image as _Image
            _Image.fromarray(big).save(path)
            p = subprocess.run(
                [tess, path, "stdout", "-l", "chi_sim", "--psm", str(use_psm),
                 "--oem", "1", "--dpi", "300", "tsv"],
                capture_output=True, timeout=8.0, creationflags=0x08000000)
        except Exception as exc:
            log("  [下拉] TSV OCR 失败(%s): %s" % (tag, exc))
            continue
        finally:
            try:
                if path:
                    os.remove(path)
            except Exception:
                pass
        for line in p.stdout.decode("utf-8", "replace").splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) < 12 or parts[0] != "5":
                continue
            try:
                left, top, ww, hh = int(parts[6]), int(parts[7]), int(parts[8]), int(parts[9])
                conf = float(parts[10])
            except Exception:
                continue
            txt = (parts[11] or "").strip()
            if not txt or conf < 30:
                continue
            words.append({
                "text": txt, "src": tag,
                "x0": left / use_zoom + x0 - pad / use_zoom,
                "x1": (left + ww) / use_zoom + x0 - pad / use_zoom,
                "top": top / use_zoom + y0 - pad / use_zoom,
                "bottom": (top + hh) / use_zoom + y0 - pad / use_zoom,
                "y": (top + hh / 2.0) / use_zoom + y0 - pad / use_zoom,
            })
    return words


def _cluster_rows(words, s=1.0, y_tol=10.0, x_gap=60.0):
    """词级坐标 -> 行（按 y 邻近聚类；同一 y 内按 x 间隙切分成多段，避免跨面板/聊天区合并）。"""
    rows = []
    for w in sorted(words, key=lambda w: (w["y"], w["x0"])):
        hit = None
        for r in rows:
            if abs(r["y"] - w["y"]) > y_tol:
                continue
            # 必须在左右两侧都靠得近：否则同一 y 的「面板文字」和「右侧聊天区文字」会被拼成一行
            if w["x0"] <= r["x1"] + x_gap * s and w["x1"] >= r["x0"] - x_gap * s:
                hit = r
                break
        if hit is None:
            rows.append({"y": w["y"], "top": w["top"], "bottom": w["bottom"],
                         "x0": w["x0"], "x1": w["x1"], "words": [w]})
        else:
            hit["words"].append(w)
            hit["x0"] = min(hit["x0"], w["x0"])
            hit["x1"] = max(hit["x1"], w["x1"])
            hit["top"] = min(hit["top"], w["top"])
            hit["bottom"] = max(hit["bottom"], w["bottom"])
            hit["y"] = (hit["top"] + hit["bottom"]) / 2.0
    out = []
    for r in rows:
        ws = sorted(r["words"], key=lambda w: w["x0"])
        out.append({"text": " ".join(w["text"] for w in ws),
                    "y": r["y"], "top": r["top"], "bottom": r["bottom"],
                    "x0": r["x0"], "x1": r["x1"]})
    out.sort(key=lambda r: r["y"])
    return out


def _union_chars(variants):
    """把同一视觉行的多遍 OCR 文本做「字符并集（去重保序）」，作为一个额外候选变体。

    2026-10-26 晚：长中文名在下拉小字里经常被不同 PSM/缩放读丢不同的字（如「说起」），
    并集能把各遍读到的字拼回来，交给覆盖率匹配 + 点开后的标题闸门兜底。
    """
    seen = set()
    out = []
    for t in variants or []:
        for ch in _clean_row_text(t):
            if ch and ch not in seen:
                seen.add(ch)
                out.append(ch)
    return "".join(out)


def _ocr_rows(img, box, ctx, psm=6, zoom=2.0, psms=(6, 11), include_orig=True,
              log_compare=False):
    """多遍 OCR 词级聚类 -> 行，并按行合并不同预处理/PSM 的结果（variants 保留候选文本）。

    2026-10-26 晚（真机 row-approximate 修复·治本）：
      * 默认 2.0x 放大后再 OCR（小字号中文提升明显）；
      * 同一放大图跑 psm6 + psm11 两遍，行级 variants 保留各遍候选；
      * variants 额外加「字符并集（去重保序）」候选；
      * src_texts 记录每遍 OCR 的行文本；log_compare=True 时打印「放大版 / 原始版」对照。
    """
    words = _ocr_words(img, box, ctx, psm=psm, zoom=zoom, psms=psms,
                       include_orig=include_orig)
    s = 1.0
    by_src = {}
    for w in words:
        by_src.setdefault(w["src"], []).append(w)
    merged = []
    for tag, ws in by_src.items():
        for r in _cluster_rows(ws, s=s):
            hit = None
            for m in merged:
                if abs(m["y"] - r["y"]) > 16:
                    continue
                if r["x1"] < m["x0"] - 40 or r["x0"] > m["x1"] + 40:
                    continue
                hit = m
                break
            if hit is None:
                merged.append(dict(r, texts=[r["text"]], _src_rows=[r],
                                   src_texts={tag: r["text"]}))
            else:
                if r["text"] not in hit["texts"]:
                    hit["texts"].append(r["text"])
                    hit["_src_rows"].append(r)
                    hit["src_texts"][tag] = r["text"]
                hit["x0"] = min(hit["x0"], r["x0"])
                hit["x1"] = max(hit["x1"], r["x1"])
                hit["top"] = min(hit["top"], r["top"])
                hit["bottom"] = max(hit["bottom"], r["bottom"])
    out = []
    for m in merged:
        variants = [_clean_row_text(v) for v in m["texts"]]
        union = _union_chars(m["texts"])
        if union:
            m["src_texts"]["union"] = union
            if union not in variants:
                variants.append(union)
        txt = " | ".join(m["texts"])
        out.append({"text": txt, "variants": variants,
                    "variant_x0": [v.get("x0", m["x0"]) for v in m["_src_rows"]] + [m["x0"]],
                    "src_texts": dict(m["src_texts"]),
                    "norm": watcher.normalize_name(txt),
                    "y": m["y"], "top": m["top"], "bottom": m["bottom"],
                    "x0": m["x0"], "x1": m["x1"]})
    out.sort(key=lambda r: r["y"])
    if log_compare:
        ztag = "z%.1f" % float(zoom)
        for o in out[:24]:
            st = o.get("src_texts") or {}
            big = [v for k, v in st.items() if ztag in k and "orig" not in k]
            orig = [v for k, v in st.items() if "orig" in k]
            if orig and big and "".join(big) != "".join(orig):
                log("  [OCR对照] y=%.0f 放大版=%r 原始版=%r" % (
                    o["y"], " | ".join(big)[:100], " | ".join(orig)[:100]))
    return out


def _estimate_panel_right(img, panel_left, y_top, s):
    """用像素亮度突变自适应估计下拉面板右边界（不写死宽度）。

    面板背景通常比右侧聊天区背景略亮；在搜索框下方一段带里按列求平均亮度，
    找 `|col[x+1]-col[x]|` 最大的位置作为面板右边。找不到明显突变就退回 panel_left+430*s。
    """
    W, H = img.size
    y0 = int(min(H - 2, y_top + 10 * s))
    y1 = int(min(H - 1, max(y0 + 30, y_top + 240 * s)))
    try:
        arr = np.array(img.crop((0, y0, W, y1)).convert("L")).astype(np.float32)
    except Exception:
        return min(W - 2.0, panel_left + 430.0 * s)
    if arr.size == 0:
        return min(W - 2.0, panel_left + 430.0 * s)
    col = arr.mean(axis=0)
    lo = int(max(panel_left + 80 * s, 0))
    hi = int(min(W - 2, panel_left + 520 * s))
    best_x, best_d = None, 0.0
    for x in range(lo, hi):
        d = abs(float(col[x + 1]) - float(col[x]))
        if d > best_d:
            best_d, best_x = d, x
    if best_x is not None and best_d >= 1.2:
        return float(best_x + 1)
    return min(W - 2.0, panel_left + 430.0 * s)


def _find_search_line(lines, target_clean, W, H):
    """定位搜索框那一行（面板的锚点）。

    已知大致比例：搜索框在窗口顶部 y≈0.03~0.15H、x≈0.02~0.25W，且右上角有 ✕、右侧有 + 按钮。
    这里取该带内「与目标文本相似度最高」的文字行；找不到返回 None（无法定位面板 -> abort）。
    """
    band_y0, band_y1 = 0.03 * H, 0.15 * H
    cands = []
    for r in lines:
        if not (band_y0 <= r["y"] <= band_y1):
            continue
        if r["x0"] < 0.02 * W or r["x0"] > 0.30 * W:
            continue
        if not _clean_row_text(r["text"]):
            continue
        cands.append(r)
    if not cands:
        return None
    scored = [(_sim_text(_clean_row_text(r["text"]), target_clean), r) for r in cands]
    scored.sort(key=lambda t: (-t[0], t[1]["y"]))
    best_score, best = scored[0]
    if best_score >= 0.2:
        return best
    # OCR 认不清搜索框文字时（很常见）退化为「该带内最靠上的一行」——搜索框永远在面板上方
    return min(cands, key=lambda r: r["y"])


# ─────────────── 搜索层 / 面板判别守卫（2026-10-27 第一阶段新增） ───────────────
# 🔴 高怀疑真凶（2026-10-26 夜间交接单）：Ctrl+F 很多时候**根本没打开**搜索层，此时
#   ① 输入的「目标名」会打进**聊天输入框**；② 截图里看到的是**会话列表**而不是搜索下拉。
#   旧逻辑会把会话列表当成下拉 -> 选中会话行 -> 点击（点错会话），收尾还盲按 Esc
#   （Esc 落在微信主窗口上 -> 微信 4.x 把窗口收进托盘 = 用户看到的「她把微信窗口关了」）。
#   现在：输入后必须确认聊天输入框为空（否则清残留 + abort search-layer-not-opened）；
#         下拉候选里出现 >=2 行「时间戳」一律判定 conversation-list、不取候选、abort。
_TIMESTAMP_RE = re.compile(
    r"(?:昨天|前天|今天|"
    r"星期[一二三四五六日天]|周[一二三四五六日天]|"
    r"\d{1,2}月\d{1,2}日|\d{4}[-/]\d{1,2}[-/]\d{1,2}|"
    r"\d{1,2}[-/]\d{1,2})\s*"
    r"\d{1,2}:\d{2}(?::\d{2})?")
_BARE_TIME_RE = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?!\d)")
PANEL_CONVERSATION_TIMESTAMP_ROWS = 2   # 候选里 >=2 行带时间戳 -> 判定「这是会话列表，不是搜索下拉」
# 复核阈值：搜索框 OCR 里目标名字符覆盖率 >= 此值 -> 说明「真在搜索框里打了目标名」，是搜索层内部
SEARCH_BOX_TARGET_RECALL_MIN = 0.30


def _row_has_timestamp(*texts):
    """判断一行文本里是否带「聊天列表时间戳」（HH:MM / 昨天 HH:MM / 18:37 / 星期X HH:MM）。

    会话列表每行右侧都有时间戳（23:20 / 18:55 / 昨天 12:16），搜索下拉行**不显示时间**，
    所以这是区分「下拉面板」和「会话列表」最可靠的信号（图标判别区分不了：会话列表也有彩色头像）。
    """
    for t in texts or ():
        s = str(t or "")
        if not s:
            continue
        if _TIMESTAMP_RE.search(s) or _BARE_TIME_RE.search(s):
            return True
    return False


def classify_panel_kind(rows):
    """纯逻辑（离线可单测）：判断面板是「搜索下拉 dropdown」还是「会话列表 conversation-list」。

    返回 (kind, evidence)：
      "conversation-list" -> 候选行里出现 >=2 行带时间戳；**不取任何候选、上层 abort**。
      "dropdown"          -> 有候选行且时间戳行 < 2（真正的搜索下拉）。
      "unknown"           -> 没有可用候选行（面板没弹/OCR 全失败），同样不点击。
    """
    rows = list(rows or [])
    ts_rows = []
    for r in rows:
        texts = [r.get("text") or "", r.get("norm") or ""]
        texts.extend(r.get("variants") or [])
        if _row_has_timestamp(*texts):
            ts_rows.append(r)
    if len(ts_rows) >= PANEL_CONVERSATION_TIMESTAMP_ROWS:
        sample = "；".join("%r@y=%.0f" % (str(r.get("text") or "")[:16], float(r.get("y") or 0))
                           for r in ts_rows[:4])
        evidence = ("候选 %d 行中有 %d 行带时间戳（>=%d 行即判定为会话列表）：%s" % (
            len(rows), len(ts_rows), PANEL_CONVERSATION_TIMESTAMP_ROWS, sample))
        return "conversation-list", evidence
    if not rows:
        return "unknown", "没有可用候选行（面板没弹出来 / OCR 全失败）"
    evidence = ("候选 %d 行，时间戳行 %d 行（<%d 行）-> 是搜索下拉" % (
        len(rows), len(ts_rows), PANEL_CONVERSATION_TIMESTAMP_ROWS))
    return "dropdown", evidence


def analyze_dropdown(img, geom, ctx, target, lines=None):
    """自适应分析搜索下拉面板，返回：
        {"panel_left","panel_right","search_line","panel_top","panel_bottom",
         "rows":[{text,variants,norm,y,top,bottom,x0,x1,section,icon,kind}], "reason"}

    算法（2026-10-26 精度改造，替代旧的固定比例裁剪）：
      ① 整窗屏幕截图 -> 全窗多遍 OCR（词级坐标）
      ② 定位搜索框那一行（y≈0.03~0.15H、x≈0.02~0.30W，与目标文本相似度最高 / 退化为最靠上）
      ③ 面板右边界用像素亮度突变自适应估计（不写死宽度）
      ④ 面板 = 从搜索框下沿开始的连续行块（行距突然变大即截断；自适应阈值 = 行距中位数×1.8）
      ⑤ 块内按「分组标题（功能/联系人/群聊/聊天记录/公众号/服务号）」分段；
         每行左侧图标区域算饱和度/颜色方差 -> avatar（头像）/gray（灰色放大镜）/unknown
    """
    W, H = img.size
    s = float(geom.get("s") or 1.0)
    out = {"panel_left": 0.0, "panel_right": 0.0, "search_line": None,
           "panel_top": 0.0, "panel_bottom": 0.0, "rows": [], "reason": "",
           # 2026-10-27 第一阶段：面板类型判定（dropdown / conversation-list / unknown）
           "panel_kind": "unknown", "panel_evidence": ""}
    target_clean = _clean_row_text(target)
    if lines is None:
        lines = _ocr_rows(img, (0, 0, W, H), ctx)
    if not lines:
        out["reason"] = "no-ocr-lines"
        return out
    search = _find_search_line(lines, target_clean, W, H)
    if search is None:
        out["reason"] = "search-box-not-found"
        return out
    y_start = float(search.get("bottom") or (search["y"] + 10.0))
    # 面板左缘：只用于「排除明显不属于左栏文字区」的行（右栏聊天区/窗口边缘的乱行）。
    # 真正的面板行靠「词级聚类 + 行内 x 间隙切分」隔离，不依赖这个左缘做裁剪。
    panel_left = max(4.0, 0.05 * W)
    panel_right = min(W - 2.0, panel_left + 0.45 * W)
    out.update({"panel_left": panel_left, "panel_right": panel_right,
                "search_line": search})
    # 收集面板区域内的行（词级已聚类，x 已在面板内；用行 x0 再做一次保险过滤）
    text_x0_min = max(0.05 * W, panel_left + 8.0 * s)
    text_x0_max = panel_right + 20.0 * s
    cand = [r for r in lines
            if float(r.get("top") or r["y"]) >= y_start + 2.0
            and text_x0_min <= float(r["x0"]) <= text_x0_max]
    if not cand:
        out["reason"] = "panel-block-empty"
        return out
    cand.sort(key=lambda r: r["y"])
    gaps = sorted(float(r["top"]) - float(prev["bottom"] or prev["y"])
                  for prev, r in zip(cand, cand[1:])
                  if 20.0 * s <= float(r["top"]) - float(prev["bottom"] or prev["y"]) <= 90.0 * s)
    row_h = gaps[len(gaps) // 2] if gaps else 42.0 * s
    gap_limit = max(1.8 * row_h, 70.0 * s)
    block = []
    prev_bottom = y_start
    for r in cand:
        gap = float(r.get("top") or r["y"]) - prev_bottom
        if block and gap > gap_limit:
            break
        block.append(r)
        prev_bottom = float(r.get("bottom") or r["y"])
    if not block:
        out["reason"] = "panel-block-empty"
        return out
    panel_top = min(float(r.get("top") or r["y"]) for r in block)
    panel_bottom = max(float(r.get("bottom") or r["y"]) for r in block)
    section = ""
    rows = []
    for r in block:
        if _is_group_header(r["text"]):
            section = _clean_row_text(r["text"])
            continue
        # 图标锚点：优先用「与目标名最像的候选文本」的 x0（该行多遍 OCR 的候选各自有 x0）
        anchor_x0 = float(r.get("x0") or 0.0)
        try:
            variants = r.get("variants") or []
            vx0s = r.get("variant_x0") or []
            best_sim, best_i = 0.0, -1
            for i, v in enumerate(variants):
                sim = _sim_text(v or "", target_clean)
                if sim > best_sim:
                    best_sim, best_i = sim, i
            if best_i >= 0 and best_sim >= 0.82 and best_i < len(vx0s):
                anchor_x0 = float(vx0s[best_i])
        except Exception:
            pass
        stats = None
        try:
            stats = _icon_stats(img, float(r["y"]), row_h, s, anchor_x0=anchor_x0)
        except Exception:
            stats = None
        kind = _classify_icon(stats)
        variants = r.get("variants") or [_clean_row_text(r["text"])]
        rows.append({"text": r["text"], "variants": [v for v in variants if v],
                     "norm": _clean_row_text(r["text"]),
                     "y": float(r["y"]), "top": float(r.get("top") or r["y"]),
                     "bottom": float(r.get("bottom") or r["y"]),
                     "x0": float(r["x0"]), "x1": float(r["x1"]),
                     "src_texts": dict(r.get("src_texts") or {}),
                     "section": section, "icon": stats, "kind": kind})
    out.update({"panel_top": panel_top, "panel_bottom": panel_bottom, "rows": rows})
    # 2026-10-27 第一阶段：面板类型判定（必须打印「面板判定：dropdown / conversation-list / unknown + 依据」）
    kind, evidence = classify_panel_kind(rows)
    if kind == "conversation-list":
        # 复核（防误判）：时间戳行可能是「搜索下拉面板下方露出的会话列表」——
        #   真下拉只会盖住会话列表上半部分，下面的行仍会被 OCR 进候选块。用「搜索框 OCR 是否
        #   真的含目标名」区分：真在搜索框打了目标名 = 搜索层确实打开 = 这是搜索下拉，不是会话列表。
        #   （如果不做这步，短下拉 + 下方露出会话列表的合法截图会被误判 abort，正常发送会失败。）
        recall, sb_text = _search_box_target_recall(img, geom, ctx, target)
        ts_cnt = sum(1 for r in rows
                     if _row_has_timestamp(r.get("text") or "", *(r.get("variants") or [])))
        if recall >= SEARCH_BOX_TARGET_RECALL_MIN:
            evidence = ("候选 %d 行中有 %d 行带时间戳，但搜索框 OCR 命中目标名"
                        "（字符覆盖 %.0f%%：%r）-> 时间戳来自「下拉面板下方露出的会话列表」，"
                        "仍判定为搜索下拉" % (len(rows), ts_cnt, 100.0 * recall, sb_text[:40]))
            kind = "dropdown"
            log("[下拉] 面板判定复核：候选有时间戳，但搜索框里确实有目标名 -> 不判 conversation-list")
        else:
            evidence = ("候选 %d 行中有 %d 行带时间戳，且搜索框 OCR 未命中目标名"
                        "（字符覆盖 %.0f%%：%r）-> 判定为会话列表" % (
                            len(rows), ts_cnt, 100.0 * recall, sb_text[:40]))
    out.update({"panel_kind": kind, "panel_evidence": evidence})
    log("[下拉] 面板判定：%s（依据：%s）" % (kind, evidence))
    if kind == "conversation-list":
        # 会话列表被当成搜索下拉 -> 不取任何候选、不点击（上层 find_conversation_row 会 abort）
        out["rows"] = []
        out["reason"] = ("conversation-list（候选行里有 >=%d 行时间戳，判定这是会话列表不是搜索下拉；"
                         "不取任何候选、不点击）" % PANEL_CONVERSATION_TIMESTAMP_ROWS)
        return out
    return out


def _candidate_desc(row):
    """候选行的中文描述（给歧义提示用）。"""
    sect = str(row.get("section") or "")
    if sect == "群聊":
        return "群聊『%s』" % row.get("text", "")[:20]
    if sect in ("聊天记录",):
        return "聊天记录『%s』" % row.get("text", "")[:20]
    return "会话『%s』" % row.get("text", "")[:20]


APPROX_COVERAGE_MIN = 0.60          # 近似命中：字符集合覆盖率（召回）下限
APPROX_PRECISION_MIN = 0.75         # 近似命中：候选字符里属于目标的比例下限（防「别人名字是目标前缀」误点）
APPROX_SCORE_MARGIN = 0.15          # 近似命中：最佳与第二名综合得分的最小差距
TITLE_APPROX_COVERAGE_MIN = 0.55    # 点开后的标题校验：近似放行召回率下限
TITLE_APPROX_PRECISION_MIN = 0.75   # 点开后的标题校验：近似放行精确率下限


def _char_coverage(cand, target_clean):
    """字符集合召回率 = |候选字符集 ∩ 目标字符集| / |目标字符集|。"""
    cs = set(ch for ch in _clean_row_text(cand) if ch and ch.strip())
    ts = set(ch for ch in (target_clean or "") if ch and ch.strip())
    if not ts:
        return 0.0
    return len(cs & ts) / float(len(ts))


def _char_precision(cand, target_clean):
    """字符集合精确率 = |候选字符集 ∩ 目标字符集| / |候选字符集|（防目标名是别人名字的前缀）。"""
    cs = set(ch for ch in _clean_row_text(cand) if ch and ch.strip())
    ts = set(ch for ch in (target_clean or "") if ch and ch.strip())
    if not cs:
        return 0.0
    return len(cs & ts) / float(len(cs))


def _variant_scores(variants, target_clean):
    """返回 (最佳综合得分, 对应召回率, 对应精确率)。得分 = 0.6*召回率 + 0.4*difflib ratio。"""
    best_score, best_cov, best_prec = 0.0, 0.0, 0.0
    for v in variants or []:
        v = str(v or "")
        if not v:
            continue
        cov = _char_coverage(v, target_clean)
        prec = _char_precision(v, target_clean)
        score = 0.6 * cov + 0.4 * _sim_text(v, target_clean)
        if score > best_score or (score == best_score and cov > best_cov):
            best_score, best_cov, best_prec = score, cov, prec
    return best_score, best_cov, best_prec


def select_target_row(rows, target_clean):
    """纯选择逻辑（便于离线单测）。

    2026-10-26 晚（真机实测 row-approximate 修复）：tesseract 对下拉小字长中文名识别很差
    （实测某个较长的中文群名被 OCR 读错字/插竖线，永远过不了 exact
    -> 每次 abort、会话永远打不开）。改为分档：
      1) 头像行 + exact 唯一 -> ok（保持）
      2) 头像行 + exact 多个 -> row-ambiguous abort（保持"不瞎猜"）
      3) 无 exact：只在头像行里算「字符集合覆盖率 + difflib 加权得分」
         - 最佳覆盖率 >= 0.60 且比第二名得分高出 >= 0.15 -> ok_approx（先点，安全闸交给点开后的标题校验）
         - 最佳覆盖率 >= 0.60 但跟第二名接近 -> row-ambiguous abort
         - 最佳覆盖率 < 0.60 -> row-approximate abort（保持不点）
    返回 {"status":…, "row":…, "reason":…, "coverage":…, "score":…, "secondScore":…, "candidates":[…]}
    """
    avatar_rows = [r for r in (rows or []) if r.get("kind") == "avatar"]

    def _variants(r):
        vs = r.get("variants") or []
        if not vs:
            vs = [r.get("norm") or ""]
        return [v for v in vs if v]

    exact = [r for r in avatar_rows if target_clean in _variants(r)]
    if len(exact) == 1:
        return {"status": "ok", "row": exact[0], "reason": "exact-unique",
                "candidates": exact}
    if len(exact) > 1:
        return {"status": "abort", "row": None, "reason": "row-ambiguous",
                "candidates": exact}
    scored = []
    for r in avatar_rows:
        score, cov, prec = _variant_scores(_variants(r), target_clean)
        scored.append({"row": r, "score": score, "coverage": cov, "precision": prec})
    scored.sort(key=lambda x: (-x["score"], float(x["row"].get("y") or 0)))
    if not scored:
        return {"status": "abort", "row": None, "reason": "row-not-found",
                "candidates": [], "coverage": 0.0, "score": 0.0}
    best = scored[0]
    second_score = scored[1]["score"] if len(scored) > 1 else 0.0
    base_info = {"coverage": round(best["coverage"], 3),
                 "precision": round(best["precision"], 3),
                 "score": round(best["score"], 3),
                 "secondScore": round(second_score, 3)}
    ok_metrics = (best["coverage"] >= APPROX_COVERAGE_MIN
                  and best["precision"] >= APPROX_PRECISION_MIN)
    if ok_metrics:
        margin = best["score"] - second_score
        if margin >= APPROX_SCORE_MARGIN:
            return dict(base_info, status="ok_approx", row=best["row"],
                        reason="approx-unique", candidates=[best["row"]])
        cands = [best["row"]] + [c["row"] for c in scored[1:4]]
        return dict(base_info, status="abort", row=None,
                    reason="row-ambiguous", candidates=cands)
    cands = [c["row"] for c in scored[:4] if c["coverage"] >= 0.25]
    return dict(base_info, status="abort", row=None,
                reason="row-approximate", candidates=cands)


def _split_group_title(norm_text):
    """标题 -> (base_clean, has_group_number)。群聊标题形如「名称 (数字)」。"""
    s = str(norm_text or "")
    m = re.match(r"^(?P<base>.+?)\s*[（(]\s*(?P<n>\d+)\s*[)）]\s*$", s)
    if m:
        return _clean_row_text(m.group("base")), True
    return _clean_row_text(s), False


def classify_chat_title(title_texts, target, expected_group=False):
    """三重校验①标题 + ②群聊/私聊（2026-10-26 晚放宽：exact 或高覆盖率唯一）。

    title_texts: OCR 出来的标题候选文本列表（每项可能含多个 OCR 候选，用 | 分隔）。
    返回 (ok, detail)：
      - base 精确等于目标名 -> ok（群聊标题允许尾部「(数字)」）
      - 目标是私聊但标题带「(数字)」-> abort（疑似点进群聊）
      - 目标是群聊但标题不带「(数字)」-> 实测微信 4.x 群聊标题本来就不带人数，
        为避免误伤正常群聊发送，这里按 exact 名称通过并记录 warning（不是 abort）
      - 2026-10-26 晚（真机 row-approximate 修复）：exact 不中时，用「字符集合覆盖率 >= 0.55
        且唯一（只有一个候选达到、且与第二名得分差 >= 0.15）」放行。点开后的标题是大字、
        OCR 通常准确，所以安全闸仍在这里；**标题校验不过绝不会输入正文、绝不会按 Enter**。
    """
    target_clean = _clean_row_text(target)
    parts = []
    for raw in title_texts or []:
        for part in re.split(r"[|\n]", str(raw)):
            part = part.strip()
            if not part:
                continue
            base, has_n = _split_group_title(part)
            if not base:
                continue
            parts.append((part, base, has_n))
    # ① exact
    for part, base, has_n in parts:
        if base != target_clean:
            continue
        if has_n and not expected_group:
            return False, ("row-group-mismatch（目标是私聊，但标题『%s』带群聊编号，"
                           "疑似点进群聊；已 abort 不发送）" % part[:40])
        if expected_group and not has_n:
            log("  [校验] 目标群聊标题未带人数编号（微信 4.x 实测如此），按 exact 名称接受")
        return True, part[:60]
    # ② 近似兜底（唯一 + 高覆盖率；只用于「标题 OCR 读错字」）
    scored = []
    seen_clean = set()
    for part, base, has_n in parts:
        if base in seen_clean:
            continue
        seen_clean.add(base)
        cov = _char_coverage(base, target_clean)
        prec = _char_precision(base, target_clean)
        if cov < TITLE_APPROX_COVERAGE_MIN or prec < TITLE_APPROX_PRECISION_MIN:
            continue
        score = 0.6 * cov + 0.4 * _sim_text(base, target_clean)
        scored.append((score, cov, prec, part, base, has_n))
    scored.sort(key=lambda t: -t[0])
    if scored:
        best = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0
        if (len(scored) == 1) or (best[0] - second >= APPROX_SCORE_MARGIN):
            if best[5] and not expected_group:
                return False, ("row-group-mismatch（目标是私聊，但标题『%s』带群聊编号，"
                               "疑似点进群聊；已 abort 不发送）" % best[3][:40])
            log("  [校验] 标题近似通过（覆盖 %.0f%% / 精确 %.0f%% / 得分 %.2f，唯一候选）"
                "-> 继续输入框校验" % (100.0 * best[1], 100.0 * best[2], best[0]))
            return True, "title-approx(%s)" % best[3][:50]
    return False, "行标题不是精确的目标名（OCR=%r）" % (
        " | ".join(str(t)[:60] for t in (title_texts or []))[:160])


def _input_box_is_empty(img, geom, ctx):
    """三重校验③：点完会话后输入框区域应为空（避免落在聊天记录搜索结果页）。"""
    texts = ocr_candidates(img, geom["input"], ctx, max_plans=1)
    for raw in texts or []:
        for part in re.split(r"[|\n]", str(raw)):
            c = _clean_row_text(part)
            if not c:
                continue
            if c in ("输入", "输入消息", "消息", "发送"):
                continue
            # 🔴 2026-10-26 收尾验收修复：输入框区域是深色空白区，tesseract 会对空白/小图标
            #   hallucinate 单字（实测 '站'/'屈'/'一'），旧逻辑「任意非空文字即失败」会让
            #   每次点开会话后都 abort（真发 100% 发不出去）。真实「聊天记录搜索结果页」的
            #   输入框里是目标名/搜索词（≥2 字），所以只把「≥2 个字符」的 OCR 当证据。
            if len(c) < 2:
                log("  [校验] 输入框 OCR 单字噪声已忽略: %r（不作为 abort 依据）" % part[:12])
                continue
            return False, "行输入框里识别到文字『%s』（疑似停在搜索/聊天记录页）" % part[:40]
    return True, ""


# ───────── 搜索层守卫：输入目标名之后，聊天输入框必须为空（2026-10-27 第一阶段） ─────────
# 🔴 背景：Ctrl+F 很多时候根本没打开搜索层，输入的「目标名」直接打进了**聊天输入框**；
#   旧逻辑继续当搜索层用 -> 把会话列表当搜索结果 -> 点击 -> 收尾盲按 Esc 把微信窗口收进托盘。
#   现在输入后先用「输入框为空」确证；不通过 -> 前台=微信确证前提下清残留 -> abort。
SEARCH_GUARD_INPUT_TOP_RATIO = 0.62   # 只 OCR 输入框上半部（下半部是表情/文件/剪刀工具栏，会幻觉出噪声）
# 2026-10-27 第二阶段真机发现的误判：微信空输入框会显示 placeholder「长按鼠标 语音输入」，
# OCR 读成「长按电标 语音输入」时含 >=2 连续汉字，会被误判成「字打进了聊天框」。
# 这些 placeholder 短语必须先去掉，再判断输入框里是否真的有文字。
_INPUT_PLACEHOLDER_PATTERNS = (
    re.compile(r"长按.{0,6}输入"),      # 长按鼠标 语音输入 / 长按电标 语音输入 / 长按说话
    re.compile(r"语音输入"),
    re.compile(r"输入消息"),
    re.compile(r"发送消息"),
    re.compile(r"按\s*Enter.{0,8}发送", re.I),
    re.compile(r"Ctrl\s*\+\s*Enter", re.I),
)


def _strip_input_placeholder(text):
    """去掉微信输入框的 placeholder/提示语（不是用户打的字），返回清洗后文本。"""
    s = str(text or "")
    for pat in _INPUT_PLACEHOLDER_PATTERNS:
        s = pat.sub("", s)
    return s


def _texts_have_typed_evidence(texts, target=""):
    """纯逻辑：从一组 OCR 文本判断聊天输入框里是否真有「被打进去的文字」。返回 (has_text, detail)。

    2026-10-27 第二阶段真机校准（关键）：搜索层守卫的语义是「我们打进去的**目标名**有没有跑进
    聊天输入框」。微信空输入框的 placeholder「长按鼠标 语音输入」会被 OCR 读成
    「-一 让 一 * 长按和鼠标 语音输入」这类噪声（含 3 连汉字），单纯按「连续汉字」判会误 abort。
    所以：
      ① 先去掉 placeholder 短语；
      ② **有目标名时，只认「目标名字符覆盖 >= 50%」或「目标名前 2 字出现」**（真被打进去的目标名
         一定能对上；placeholder/单字幻觉对不上目标）；
      ③ 没传目标名时退化为「>=3 连续汉字」的保守判据（仅兜底）。
    """
    target_clean = _clean_row_text(target)
    for raw in texts or []:
        for part in re.split(r"[|\n]", str(raw)):
            c = _strip_input_placeholder(_clean_row_text(part))
            if not c:
                continue
            if c in ("输入", "输入消息", "消息", "发送", "发送S"):
                continue
            if target_clean:
                if target_clean[:2] in c or _char_coverage(c, target_clean) >= 0.5:
                    return True, str(part)[:60]
                continue
            if len(c) >= 3 and re.search(r"[\u4e00-\u9fff]{2,}", c):
                return True, str(part)[:60]
    return False, ""


def _search_guard_input_box(geom):
    """搜索层守卫用的输入框区域：只取输入框上半部，避开底部工具栏图标（实测 OCR 幻觉 '必 AR'）。"""
    x0, y0, x1, y1 = [float(v) for v in geom["input"]]
    h = max(8.0, y1 - y0)
    return (int(x0) + 4, int(y0) + 2, int(x1) - 4,
            int(y0 + h * SEARCH_GUARD_INPUT_TOP_RATIO))


def _typed_text_evidence(img, geom, ctx, target=""):
    """判断聊天输入框里是否真的有「被打进去的文字」。返回 (has_text, detail)。

    比 `_input_box_is_empty` 严（后者用于点开会话后的校验，保持不变）：
      ① 只 OCR 输入框上半部 —— 下半部工具栏图标会让 tesseract 幻觉出 2 字符噪声（实测 '必 AR'），
         真实搜索下拉截图里如果按旧判据会被误 abort；
      ② 一段文本里出现 **>=2 个连续汉字**才算「有字」（目标名/草稿都是中文；单字/纯字母噪声忽略）；
      ③ 目标名是 ASCII 时额外按「目标名前 2 个字符出现在文本里」判。
    """
    box = _search_guard_input_box(geom)
    try:
        texts = ocr_candidates(img, box, ctx, max_plans=2)
    except ReplyAbort:
        raise
    except Exception as exc:
        log("  [搜索层守卫] 输入框 OCR 失败（按空处理，后续仍会被下拉/标题校验拦）: %s" % exc)
        return False, ""
    return _texts_have_typed_evidence(texts, target)


def _search_box_target_recall(img, geom, ctx, target):
    """OCR 搜索框区域，返回 (目标名字符覆盖率, 最像的那段文本)。

    用途：复核「面板判定 conversation-list」——真搜索下拉只会盖住会话列表上半部分，
    下方露出的行也带时间戳，会污染候选；若搜索框 OCR 里确实出现了目标名的字符
    （覆盖率 >= SEARCH_BOX_TARGET_RECALL_MIN），说明搜索层真的开了，不能误判成会话列表。
    """
    target_clean = _clean_row_text(target)
    if not target_clean:
        return 0.0, ""
    best_cov, best_text = 0.0, ""
    try:
        texts = ocr_candidates(img, geom["search"], ctx, max_plans=2)
    except ReplyAbort:
        raise
    except Exception as exc:
        log("  [下拉] 搜索框复核 OCR 失败（按未命中处理）: %s" % exc)
        return 0.0, ""
    for raw in texts or []:
        for part in re.split(r"[|\n]", str(raw)):
            n = _clean_row_text(part)
            if not n:
                continue
            cov = _char_coverage(n, target_clean)
            if cov > best_cov:
                best_cov, best_text = cov, n
    return best_cov, best_text


def verify_search_layer_after_typing(ctx, hwnd, target, way_label=""):
    """输入目标名之后：确证搜索层真的打开（聊天输入框必须为空）。

    通过 -> ctx.mark_search_open(True) 并返回 (img, geom)；
    不通过 -> 在「前台=微信」确证的前提下清掉误输入残留 -> 抛 ReplyAbort(search-layer-not-opened)。
    """
    img = capture_window(ctx, hwnd)
    geom = geom_of(img, watcher.dpi_scale_of(hwnd))
    has_text, detail = _typed_text_evidence(img, geom, ctx, target)
    if not has_text:
        ctx.mark_search_open(True, "input-box-empty(%s)" % (way_label or "-"))
        log("  [搜索层守卫] 输入目标名后聊天输入框为空 -> 确证搜索层已打开（方式=%s）" % (way_label or "-"))
        return img, geom
    # 不通过：目标名被打进了聊天输入框 = 搜索层没打开
    log("  [搜索层守卫] ⚠ 输入目标名后聊天输入框里识别到文字（%r）-> 判定搜索层没打开，字打进了聊天框"
        % detail)
    cleaned = False
    clean_err = ""
    try:
        # 只有「前台=微信」确证时才允许注入清理键（InputGuard 每条注入前都会再校验）
        check_injection_foreground("清理误输入残留前")
        ctx.guard.tap(VK_A, mods=(VK_CONTROL,))
        ctx.sleep(0.08)
        ctx.guard.tap(VK_DELETE)
        ctx.sleep(0.12)
        img2 = capture_window(ctx, hwnd)
        geom2 = geom_of(img2, watcher.dpi_scale_of(hwnd))
        still, detail2 = _typed_text_evidence(img2, geom2, ctx, target)
        cleaned = not still
        log("  [搜索层守卫] 已清掉误输入残留（前台=微信已确证）-> 清空校验=%s%s" % (
            cleaned, "" if cleaned else "（仍有文字: %r）" % detail2))
    except ReplyAbort:
        raise
    except Exception as exc:
        clean_err = str(exc)
        log("  [搜索层守卫] 清理误输入残留异常（忽略，仍 abort）: %s" % exc)
    exc = ReplyAbort("search-layer-not-opened（Ctrl+F 后输入的目标名跑进了聊天输入框：OCR=%r；"
                     "已清理残留=%s%s；未点击任何行、未输入正文、未按 Enter）"
                     % (detail, cleaned, ("；清理异常=" + clean_err) if clean_err else ""))
    exc.user_message = "小耳朵没打开微信搜索框，这条不发了，原因 search-layer-not-opened"
    raise exc


def validate_opened_chat(ctx, img, geom, target, expected_group, match_cfg):
    """点完会话行的三重校验（任一不过立即 abort，不再点第二次）。"""
    target_clean = _clean_row_text(target)
    title_texts = ocr_candidates(img, geom["header"], ctx, max_plans=2)
    ok, detail = classify_chat_title(title_texts, target, expected_group=expected_group)
    log("  [校验] ①标题 exact/群聊标志: ok=%s detail=%s" % (ok, detail))
    if not ok:
        return False, detail
    ok2, detail2 = _input_box_is_empty(img, geom, ctx)
    log("  [校验] ②输入框为空: ok=%s detail=%s" % (ok2, detail2 or "空"))
    if not ok2:
        return False, detail2
    return True, detail


def _ocr_row_text_psm7(img, row):
    """把一条下拉行的「文字区」整行裁出、放大 2x、用 psm7（单行）精读，返回清洗后文本。

    2026-10-26 晚：下拉小字走通用词级 OCR 容易丢字；对候选头像行单独裁切 + 单行 PSM 更准
    （实测失败截图里的长群名用 psm7 一次读对，约 0.25s）。
    """
    W, H = img.size
    y0 = int(max(0, float(row.get("top") or row["y"]) + 1))
    y1 = int(min(H, float(row.get("bottom") or row["y"])))
    x0 = int(max(0, float(row.get("x0") or 0) - 10))
    x1 = int(min(W, float(row.get("x1") or (W - 2)) + 20))
    if x1 - x0 < 40 or y1 - y0 < 14:
        return ""
    path = ""
    try:
        crop = img.crop((x0, y0, x1, y1))
        gray = cv2.cvtColor(np.array(crop), cv2.COLOR_RGB2GRAY)
        arr = cv2.bitwise_not(gray)
        big = cv2.resize(arr, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
        big = cv2.copyMakeBorder(big, 16, 16, 16, 16, cv2.BORDER_CONSTANT, value=255)
        tess = tesseract_bin()
        tmp_dir = os.path.join(BASE_DIR, "tmp-ocr")
        os.makedirs(tmp_dir, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix="reply-row7-", suffix=".png", dir=tmp_dir)
        os.close(fd)
        from PIL import Image as _Image
        _Image.fromarray(big).save(path)
        p = subprocess.run([tess, path, "stdout", "-l", "chi_sim", "--psm", "7",
                            "--oem", "1", "--dpi", "300"],
                           capture_output=True, timeout=8.0, creationflags=0x08000000)
        text = p.stdout.decode("utf-8", "replace").strip()
        return _clean_row_text(text.splitlines()[0] if text else "")
    except Exception as exc:
        log("  [下拉] 行级 psm7 精读失败（忽略）: %s" % exc)
        return ""
    finally:
        try:
            if path:
                os.remove(path)
        except Exception:
            pass


def _refine_dropdown_analysis(img, geom, ctx, target, target_clean, analysis, s):
    """第一遍选不中时的第二遍：只裁「下拉面板区域」，放大 2.0x + psm6/11 重扫（含原始对照），
    再对候选头像行做 psm7 整行精读。返回新的 analysis（失败返回 None）。

    为什么只裁面板：整窗 1371x870 放大后 tesseract 分割质量下降；实测同一张失败截图
    裁面板放大 2x 后能把长群名读成完整串（见日志 [OCR对照] 放大版/原始版）。
    """
    W, H = img.size
    search = analysis.get("search_line") or {}
    x0 = int(max(0, float(analysis.get("panel_left") or 0)))
    x1 = int(min(W, float(analysis.get("panel_right") or (0.47 * W)) + 14))
    y0 = int(max(0, float(search.get("bottom") or 0) + 2))
    y1 = int(min(H, float(analysis.get("panel_bottom") or 0) + 14))
    if x1 - x0 < 120 or y1 - y0 < 60:
        return None
    lines = _ocr_rows(img, (x0, y0, x1, y1), ctx, zoom=2.0, psms=(6, 11),
                      include_orig=True, log_compare=True)
    if not lines:
        return None
    log("  [下拉] 放大重扫(2.0x psm6+11，含原始对照) 区域=(%d,%d,%d,%d) 行数=%d" % (
        x0, y0, x1, y1, len(lines)))
    analysis2 = analyze_dropdown(img, geom, ctx, target, lines=[search] + lines)
    n = 0
    for r in analysis2.get("rows") or []:
        if r.get("kind") != "avatar" or n >= 8:
            continue
        txt = _ocr_row_text_psm7(img, r)
        if not txt:
            continue
        n += 1
        log("  [下拉] 行级 psm7 精读 y=%.0f -> %r" % (r["y"], txt[:50]))
        r.setdefault("variants", [])
        if txt not in r["variants"]:
            r["variants"].append(txt)
    return analysis2


def find_conversation_row(ctx, img, geom, target):
    """在搜索下拉面板里定位「真正的会话行」并返回点击坐标（2026-10-26 精度改造版）。

    规则：
      1) 只认「头像行（彩色图标）」；聊天记录/命令行（灰色放大镜）一律不参与。
      2) 优先 exact；exact 不中时允许「近似唯一命中」（覆盖率>=0.60 且明显唯一），
         但**安全闸在点开后的标题校验**（标题不过绝不输入正文、绝不按 Enter）。
      3) 同名/多来源/近似不唯一/找不到 -> abort，绝不按相似度瞎猜、绝不乱点。
    """
    target_clean = _clean_row_text(target)
    if not target_clean:
        return None
    W, H = img.size
    s = float(geom.get("s") or 1.0)
    # 🔴 2026-10-26 收尾验收修复（真发全灭的根因）：这里必须用「词级聚类」_ocr_rows
    #   （与 analyze_dropdown 的默认路径完全一致），不能用 _ocr_tsv_lines 的行级结果。
    #   行级结果没有 variants/variant_x0，会把下拉行和右侧聊天区同 y 的文字拼成一行
    #   （实测 '| 文件 传输 助手 | 文人 助手'），exact 匹配永远失败 -> 真发永远 abort；
    #   而 selftest_rows.py 走的是词级路径，所以离线自测发现不了。
    #   2026-10-26 晚：_ocr_rows 默认已升级为 2.0x 放大 + psm6/11 两遍 + 原始对照。
    lines = _ocr_rows(img, (0, 0, W, H), ctx)
    if not lines:
        log("  [下拉] 全窗 OCR 没到任何行（下拉面板可能没弹出来）")
        return None
    log("  [下拉] 全窗 OCR %d 行: %s" % (
        len(lines), " | ".join("%s@%d" % (str(r.get("text") or "")[:10], int(r["y"]))
                               for r in lines[:16])))
    analysis = analyze_dropdown(img, geom, ctx, target, lines=lines)
    # 🔴 2026-10-27 第一阶段：面板类型判别（conversation-list = 把会话列表当成了搜索下拉）
    kind = str(analysis.get("panel_kind") or "unknown")
    if kind == "conversation-list":
        log("  [下拉] 面板判定=conversation-list -> 不取任何候选、不点击（判定依据：%s）"
            % analysis.get("panel_evidence"))
        exc = ReplyAbort("conversation-list（%s；已 abort，不取任何候选、不点击）"
                         % (analysis.get("panel_evidence") or "候选行里有 >=2 行时间戳"))
        exc.user_message = "小耳朵把会话列表看成了搜索下拉，这条不发了，原因 conversation-list"
        raise exc
    if kind == "unknown":
        log("  [下拉] 面板判定=unknown（依据：%s）-> 不取任何候选、不点击"
            % analysis.get("panel_evidence"))
        return None
    if not analysis["rows"]:
        log("  [下拉] 面板分析失败/无行: %s" % (analysis.get("reason") or "?"))
        return None
    log("  [下拉] 面板定位: left=%.0f top=%.0f bottom=%.0f 行数=%d（搜索框 y=%.0f）" % (
        analysis["panel_left"], analysis["panel_top"], analysis["panel_bottom"],
        len(analysis["rows"]), analysis["search_line"]["y"]))

    def _log_rows(rows):
        for r in rows:
            ic = r.get("icon") or {}
            st = r.get("src_texts") or {}
            big = " | ".join(v for k, v in st.items() if "z2" in k and "orig" not in k)
            orig = " | ".join(v for k, v in st.items() if "orig" in k)
            extra = ""
            if orig and big and "".join(big) != "".join(orig):
                extra = " [OCR对照 放大=%r 原始=%r]" % (big[:60], orig[:60])
            log("    %s %-26r y=%.0f section=%r icon=%s(colorful=%.2f satMean=%.0f satStd=%.0f)%s" % (
                "AVATAR" if r["kind"] == "avatar" else ("gray  " if r["kind"] == "gray" else "other "),
                r["text"][:24], r["y"], r["section"], r["kind"],
                ic.get("colorful", -1), ic.get("satMean", -1), ic.get("satStd", -1), extra))

    _log_rows(analysis["rows"])
    sel = select_target_row(analysis["rows"], target_clean)
    if sel["status"] not in ("ok", "ok_approx"):
        # 2026-10-26 晚（真机 row-approximate 修复·治本）：第一遍不够 -> 只裁面板放大重扫 + 行级 psm7
        log("  [下拉] 第一遍未唯一定位（%s coverage=%.2f）-> 放大重扫下拉面板" % (
            sel.get("reason"), float(sel.get("coverage") or 0)))
        refined = None
        try:
            refined = _refine_dropdown_analysis(img, geom, ctx, target, target_clean, analysis, s)
        except Exception as exc:
            log("  [下拉] 放大重扫失败（忽略）: %s" % exc)
        if refined and refined.get("rows"):
            analysis = refined
            log("  [下拉] 放大重扫面板: left=%.0f top=%.0f bottom=%.0f 行数=%d" % (
                analysis["panel_left"], analysis["panel_top"], analysis["panel_bottom"],
                len(analysis["rows"])))
            _log_rows(analysis["rows"])
            sel = select_target_row(analysis["rows"], target_clean)
    if sel["status"] in ("ok", "ok_approx"):
        r = sel["row"]
        xc = int(round((float(r["x0"]) + float(r["x1"])) / 2.0))
        margin = max(6, int(round(8 * s)))
        xc = max(int(round(float(r["x0"]))) + margin,
                 min(int(round(float(r["x1"]))) - margin, xc))
        yc = int(round(float(r["y"])))
        # 2026-10-27 第一阶段：「点击前再确认下拉仍存在」要用的参考截图与区域（面板 + 目标行）
        try:
            panel_box = (int(float(analysis["panel_left"]) - 4),
                         int(float(analysis["panel_top"]) - 6),
                         int(float(analysis["panel_right"]) + 6),
                         int(float(analysis["panel_bottom"]) + 8 * s))
        except Exception:
            panel_box = None
        row_box = (int(float(r["x0"]) - 12 * s), int(float(r.get("top") or r["y"]) - 6),
                   int(float(r["x1"]) + 20 * s), int(float(r.get("bottom") or r["y"]) + 6))
        row = {"text": r["text"], "norm": r["norm"], "y": yc,
               "x0": int(round(r["x0"])), "x1": int(round(r["x1"])),
               "x_click": int(xc), "y_click": int(yc),
               "section": r["section"], "icon": r.get("icon"), "scale": s,
               "approx": sel["status"] == "ok_approx",
               "coverage": float(sel.get("coverage") or 0),
               "score": float(sel.get("score") or 0),
               "secondScore": float(sel.get("secondScore") or 0),
               "expected_group": (r["section"] == "群聊"),
               "img_ref": img, "panel_box": panel_box, "row_box": row_box}
        if sel["status"] == "ok_approx":
            log("  [下拉] 近似唯一命中(覆盖 %.0f%%，得分 %.2f，次优 %.2f) -> 先点击，随后用标题校验确认" % (
                100.0 * row["coverage"], row["score"], row["secondScore"]))
        log("  [下拉] 选中会话行: text=%r section=%r expected_group=%s approx=%s y=%d -> 点击点 x=%d y=%d" % (
            row["text"], row["section"], row["expected_group"], row["approx"], yc, xc, yc))
        return row
    # 选择失败：区分歧义 / 近似 / 找不到
    cov = float(sel.get("coverage") or 0)
    sc = float(sel.get("score") or 0)
    sec = float(sel.get("secondScore") or 0)
    cands = sel.get("candidates") or []
    desc = "、".join(_candidate_desc(r) for r in cands[:6])
    if sel["reason"] == "row-ambiguous":
        # 简化标点（用户要求：降级告警文案不用『』（）这类标点）
        msg = "搜到多个可能的目标 %s，请给我更准确的名字" % (desc or "同名/多来源")
        exc = ReplyAbort("row-ambiguous（最佳覆盖 %.0f%% 得分 %.2f，次优 %.2f 太接近；候选：%s；"
                         "已 abort 不点击）" % (100.0 * cov, sc, sec, desc))
        exc.user_message = msg
        log("  [下拉] %s" % msg)
        raise exc
    if sel["reason"] == "row-approximate":
        msg = "没有搜到会话 %s，请给我更准确的名字，最像的只有 %.0f%% 字符覆盖" % (
            target, 100.0 * cov)
        exc = ReplyAbort("row-approximate（近似候选不达标：覆盖 %.0f%% 精确 %.0f%% 得分 %.2f；"
                         "已 abort 不点击）" % (
                             100.0 * cov, 100.0 * float(sel.get("precision") or 0), sc))
        exc.user_message = msg
        log("  [下拉] %s" % msg)
        raise exc
    log("  [下拉] 没有与 %r 精确/近似相等且判定为「头像行」的会话行 -> 返回 None"
        "（不冒险点聊天记录/命令行，由上层 abort）" % target)
    return None


# ───────── 点击前再确认下拉仍存在（2026-10-27 第一阶段） ─────────
# 用「最近一次截图（find_conversation_row 分析时那张屏幕截图）」和「点击前新截一张」对比：
# 面板区域 / 目标行区域像素变化过大 -> 下拉可能已经关了（或微信画面变了）-> abort，不点击。
DROPDOWN_GONE_DIFF_MAX = 0.35


def verify_dropdown_still_there(ctx, hwnd, row):
    """点击会话行前：再截一张整窗屏幕图，与「最近一次截图」对比，确认下拉面板仍在。"""
    ref = row.get("img_ref")
    panel_box = row.get("panel_box")
    if ref is None or not panel_box:
        log("  [下拉复核] 没有参考截图/面板坐标 -> 跳过点击前复核（离线构造用例）")
        return True
    fresh = capture_screen_window(ctx, hwnd)   # 内含「前台必须属于微信进程」遮挡保护
    panel_ratio = region_diff_ratio(ref, fresh, panel_box)
    row_ratio = region_diff_ratio(ref, fresh, row.get("row_box") or panel_box)
    log("  [下拉复核] 点击前确认下拉仍在: 面板像素差=%s 目标行像素差=%s（阈值 %.2f，对比最近一次截图）"
        % (("%.4f" % panel_ratio) if panel_ratio is not None else "None",
           ("%.4f" % row_ratio) if row_ratio is not None else "None", DROPDOWN_GONE_DIFF_MAX))
    if panel_ratio is None:
        raise ReplyAbort("dropdown-gone（点击前复查：新旧截图尺寸/区域不一致，无法确认下拉仍在；未点击）")
    if panel_ratio > DROPDOWN_GONE_DIFF_MAX or (
            row_ratio is not None and row_ratio > DROPDOWN_GONE_DIFF_MAX):
        raise ReplyAbort("dropdown-gone（点击前复查：下拉面板/目标行画面变化过大"
                         "（面板=%.3f 行=%s，阈值 %.2f），下拉可能已关闭；未点击）"
                         % (panel_ratio,
                            ("%.3f" % row_ratio) if row_ratio is not None else "None",
                            DROPDOWN_GONE_DIFF_MAX))
    return True


def click_row_in_wechat(ctx, hwnd, img_size, row):
    """单击一行。🔴 点击坐标必须落在微信窗口矩形内，越界一律 abort（只在微信窗口自己内部点）。"""
    l, t, r, b = watcher.window_rect(hwnd)
    W, H = int(img_size[0]), int(img_size[1])
    if (r - l) != W or (b - t) != H:      # 抓图后窗口尺寸变了 -> 以最新窗口矩形为准
        l, t, r, b = watcher.window_rect(hwnd)
    x = int(round(float(row.get("x_click", 0))))
    y = int(round(float(row.get("y_click", row.get("y", 0)) or 0)))
    if x <= 0 or y <= 0:   # 2026-10-26：缺 x_click 时旧代码会点窗口左边缘（x=0），必须拦下
        raise ReplyAbort("click-coord-invalid（行坐标异常 x=%d y=%d；未点击）" % (x, y))
    sx, sy = l + x, t + y
    if not (l <= sx < r and t <= sy < b):
        raise ReplyAbort("click-out-of-window（点击坐标 (%d,%d) 不在微信窗口 %s 内；未点击）"
                         % (sx, sy, (l, t, r, b)))
    # 🔴 2026-10-27 第一阶段：点击前再确认下拉仍存在（用最近一次截图对比；下拉关了就不点）
    verify_dropdown_still_there(ctx, hwnd, row)
    # 🔴 2026-10-26 23:3x：点击前再确认前台仍是目标微信（否则一个键鼠都不注入）。
    check_injection_foreground("点击会话行前")
    ctx.checkpoint("点会话行前")
    got = ctx.guard.click_at(sx, sy)
    log("  [点击] 微信窗口内屏幕坐标=(%d,%d)（窗口图像坐标 x=%d y=%d；目标行文本=%r）；%s"
        % (got[0], got[1], x, y, row.get("text", ""), mouse_desc(ctx)))
    return got


def region_diff_ratio(img1, img2, box):
    try:
        a = cv2.cvtColor(np.array(img1), cv2.COLOR_RGB2BGR)
        b = cv2.cvtColor(np.array(img2), cv2.COLOR_RGB2BGR)
        if a.shape != b.shape:
            return None
        x0, y0, x1, y1 = [int(v) for v in box]
        hh, ww = b.shape[:2]
        x0 = max(0, x0); y0 = max(0, y0); x1 = min(ww, x1); y1 = min(hh, y1)
        if x1 <= x0 or y1 <= y0:
            return None
        d = cv2.absdiff(a[y0:y1, x0:x1], b[y0:y1, x0:x1])
        return float((d > 25).mean())
    except Exception:
        return None


# ─────────────── 发送主流程 ───────────────
def load_reply_state():
    try:
        st = json.load(io.open(REPLY_STATE, encoding="utf-8"))
        st.setdefault("sent", [])
        return st
    except Exception:
        return {"sent": []}


def save_reply_state(st):
    try:
        st["sent"] = st.get("sent", [])[-50:]
        out = json.dumps(st, ensure_ascii=False, indent=2)
        tmp = REPLY_STATE + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            f.write(out)
        os.replace(tmp, REPLY_STATE)
    except Exception:
        pass


def find_duplicate(target, text):
    cutoff = time.time() - 120
    for item in load_reply_state().get("sent", []):
        if item.get("chat") == target and item.get("text") == text and item.get("ts", 0) > cutoff:
            return item
    return None


def search_open_check(ctx, hwnd, geom, img_a, attempt):
    """Ctrl+F 后确认「搜索框真的拿到了焦点」（像素层辅助判断）。

    2026-10-25 23:0x 实测（真实窗口）：微信 4.x 在「窗口可见但在后台」时按 Ctrl+F，
    搜索框会出现焦点描边（搜索框区域像素差 ≈ 0.026），但**列表区像素差只有 ≈0.002**
    —— 原来的「列表区 ≥0.004」判据会误判「搜索层没打开」而中止，导致永远发不出去。
    2026-10-27 第一阶段（高怀疑真凶修复）：
      * 这里只做**辅助**判断，不再用 Esc 重置（没确证搜索层就按 Esc = 微信会把窗口收进托盘）；
      * 像素不达标 -> 直接重按 Ctrl+F 重试，**一个 Esc 都不发**；
      * 真正的确证在 do_open_chat 里「输入目标名后聊天输入框为空」（verify_search_layer_after_typing）。
    """
    for sub in (1, 2):
        ctx.guard.tap(VK_F, mods=(VK_CONTROL,))
        ctx.sleep(0.45)
        img_b = capture_window(ctx, hwnd)
        ratio_list = region_diff_ratio(img_a, img_b, geom["list"])
        ratio_box = region_diff_ratio(img_a, img_b, geom["search"])
        log("  Ctrl+F 搜索层变化检测: 列表区像素差=%s 搜索框像素差=%s（第%d次/第%d试）" % (
            ("%.4f" % ratio_list) if ratio_list is not None else "None",
            ("%.4f" % ratio_box) if ratio_box is not None else "None", attempt, sub))
        if ((ratio_box is not None and ratio_box >= 0.005)
                or (ratio_list is not None and ratio_list >= 0.004)):
            ctx.mark_search_open(True, "pixel-diff(搜索框=%s 列表=%s)" % (
                ("%.4f" % ratio_box) if ratio_box is not None else "None",
                ("%.4f" % ratio_list) if ratio_list is not None else "None"))
            return True, img_b
        if sub == 1:
            # 🔴 不按 Esc（搜索层未确证）：直接重按 Ctrl+F 重试
            log("  像素差都不达标 -> 不按 Esc（搜索层未确证，Esc 有把微信窗口收进托盘的风险），"
                "直接重按 Ctrl+F 重试")
            img_a = capture_window(ctx, hwnd)
            geom = geom_of(img_a, watcher.dpi_scale_of(hwnd))
    # 两次都没看到明显变化：不中止，继续让 do_open_chat 用「输入框为空」守卫确证
    ctx.mark_search_open(False)
    log("  搜索框/列表区像素差都很小 -> 继续输入，用「输入目标名后聊天输入框为空」确证"
        "（确认不到目标绝不按 Enter；确证前一个 Esc 都不发）")
    return True, img_a


def do_open_chat(ctx, hwnd, target, match_cfg, attempt, clip):
    """在已打开的搜索层里：输入联系人名 -> OCR 搜索下拉面板定位「真正的会话行」
    -> 受控单击该行 -> 校验聊天标题 == 目标会话名。

    🔴 2026-10-26 验收接线（禁止回退 Enter）：
      实测微信 4.x 搜索下拉默认高亮的是「🔍命令」条目，按 Enter 会打开「搜一搜」网页
      （新进程 WeChatAppEx.exe 抢前台），会话根本不会切换 —— 所以本函数只用
      「受控单击会话行」这一条路：找不到行 / 标题不符 -> 返回 False 由上层 abort，
      绝不再 tap(VK_RETURN)、绝不输入回复正文。
    成功返回 (True, True, img, geom, display)；失败返回 (False, False, img, geom, 原因)。
    """
    log_stage("search", "搜索层输入目标 %r 并定位下拉会话行（第 %d 次；search_uncertain=%s）" % (
        target, attempt, getattr(ctx, "search_uncertain", "?")))
    check_injection_foreground("定位下拉会话行前")
    scale = watcher.dpi_scale_of(hwnd)

    def _clear_search_box():
        ctx.guard.tap(VK_A, mods=(VK_CONTROL,))
        ctx.sleep(0.06)
        ctx.guard.tap(VK_DELETE)
        ctx.sleep(0.06)

    def _try_dropdown(way_label):
        """整窗屏幕截图（含 Qt 搜索下拉 popup）-> 定位会话行 -> 受控单击 -> 校验聊天标题。"""
        img_win = capture_window(ctx, hwnd)             # 干净窗口图：算几何 + 失败时辅助 OCR
        geom_w = geom_of(img_win, scale)
        img_screen = capture_screen_window(ctx, hwnd)   # 屏幕截图：能抓到下拉 popup
        shot = save_img(img_screen, "dropdown")
        log("  下拉面板屏幕截图(%s): %s" % (way_label, shot))
        row = find_conversation_row(ctx, img_screen, geom_w, target)
        if not row:
            # 辅助告警（不是硬闸门）：搜索框 OCR 只用来区分「没输进去」还是「下拉没弹」
            try:
                hit_box, texts_box, _ = name_match_region(
                    img_win, geom_w["search"], target, ctx, match_cfg, max_plans=1)
                log("  [辅助] 搜索框 OCR(%s): %r 目标可见=%s（仅告警，不作为中止依据）"
                    % (way_label, " | ".join(texts_box)[:120], hit_box))
            except Exception as exc:
                log("  [辅助] 搜索框 OCR 失败: %s" % exc)
            return False, img_win, geom_w, "搜索下拉里找不到 %r 的会话行" % target
        expected_group = bool(row.get("expected_group"))
        click_row_in_wechat(ctx, hwnd, img_screen.size, row)
        ctx.sleep(0.6)   # 等会话切换 + 标题栏刷新
        img_after = capture_window(ctx, hwnd)
        geom_after = geom_of(img_after, scale)
        # 🔴 点完只做三重校验（标题 exact / 群聊-私聊 / 输入框为空）；任一不过立即 abort，
        #    绝不再点第二次去找补（用户要求：宁可 abort 不发送，也不能点进错会话）。
        ok_v, detail_v = validate_opened_chat(
            ctx, img_after, geom_after, target, expected_group, match_cfg)
        if not ok_v and ("输入框" in str(detail_v) or "input" in str(detail_v)):
            # 2026-10-26 23:2x：上一次发送如果在「已粘贴正文但校验失败」后 abort（且当时前台
            # 不是微信、收尾清理没跑成），该会话会留下草稿，之后每次打开都被「输入框为空」
            # 校验挡住。这里在「标题已 exact 通过」的前提下，清一次残留输入再重验（纯键盘，
            # 绝不按 Enter）。标题校验逻辑本身不变。
            try:
                if int(user32.GetForegroundWindow() or 0) == hwnd:
                    log("  [校验] 输入框有残留草稿（上次失败遗留）-> 清空后重新校验（不会发送）")
                    # 必须先点一下输入框把焦点拿回来，否则 Ctrl+A/Delete 清不掉草稿
                    try:
                        l, t, r, b = watcher.window_rect(hwnd)
                        ix = int((float(geom_after["input"][0]) + float(geom_after["input"][2])) / 2.0)
                        iy = int((float(geom_after["input"][1]) + float(geom_after["input"][3])) / 2.0)
                        ctx.guard.click_at(l + ix, t + iy)
                        ctx.sleep(0.2)
                    except Exception as exc:
                        log("  [校验] 点击输入框失败（仍尝试清空）: %s" % exc)
                    ctx.guard.tap(VK_A, mods=(VK_CONTROL,))
                    ctx.sleep(0.1)
                    ctx.guard.tap(VK_DELETE)
                    ctx.sleep(0.15)
                    img_after = capture_window(ctx, hwnd)
                    geom_after = geom_of(img_after, scale)
                    ok_v, detail_v = validate_opened_chat(
                        ctx, img_after, geom_after, target, expected_group, match_cfg)
                    log("  [校验] 清草稿后重验: ok=%s detail=%s" % (ok_v, detail_v))
            except Exception as exc:
                log("  [校验] 清残留草稿失败（忽略，仍按不通过处理）: %s" % exc)
        if not ok_v:
            exc = ReplyAbort("post-click-validation-failed（%s；已立即 abort，不连点第二次）"
                             % detail_v)
            exc.user_message = "点开后的会话校验没通过，为安全起见这次不发送"
            raise exc
        ctx.mark_search_open(False)   # 会话已打开、搜索层已关闭 -> 清确证状态（之后再无 Esc）
        # 🔴 2026-10-26 收尾验收修复：_try_dropdown 统一返回 4 元组 (ok, img, geom, detail)，
        #   由 do_open_chat 再包成 5 元组 (opened, confirmed, img, geom, detail) 给上层。
        #   旧代码这里多返回一个 True（5 元组），调用处按 4 元组解包 ->
        #   "ValueError: too many values to unpack (expected 4)"，点开会话成功后必然崩，
        #   真发 100% 发不出去（前几次 live 验证都卡在这一行）。
        return True, img_after, geom_after, detail_v

    # 方式 1：SendInput unicode 逐字符输入（不动剪贴板）
    _clear_search_box()
    ctx.guard.type_unicode(target)
    ctx.sleep(0.55)   # 等搜索下拉渲染
    # 🔴 2026-10-27 第一阶段：输入后必须先确证「搜索层真的打开」（聊天输入框为空）；
    #    字打进了聊天框 -> 清残留 + abort search-layer-not-opened（不再继续把会话列表当下拉）
    verify_search_layer_after_typing(ctx, hwnd, target, way_label="sendinput")
    ok, img, g, detail = _try_dropdown("sendinput")
    if ok:
        return True, True, img, g, detail

    log("  SendInput 方式未定位到会话行（%s）-> 剪贴板 Ctrl+V 兜底重试" % detail)
    # 方式 2：剪贴板 Ctrl+V 兜底（旧版已实测对微信输入框有效）
    _clear_search_box()
    if not clip.saved_ok:
        clip.save_text()
    if not clip.set_text(target):
        log("  剪贴板兜底不可用（设剪贴板失败）-> 中止（未输入正文、未按 Enter）")
        return False, False, img, g, detail
    clip.used = True
    ctx.sleep(0.1)
    ctx.guard.tap(VK_V, mods=(VK_CONTROL,))
    ctx.sleep(0.6)    # 等搜索下拉渲染
    verify_search_layer_after_typing(ctx, hwnd, target, way_label="clipboard")
    ok, img, g, detail2 = _try_dropdown("clipboard")
    if ok:
        return True, True, img, g, detail2
    return False, False, img, g, "搜索下拉里找不到 %r 的会话行（sendinput: %s；clipboard: %s）" % (
        target, detail, detail2)


# ───────────────────── 输入方式选择 + 严格输入校验（2026-10-26 23:2x 新增） ─────────────────────
# 🔴 用户真机实测：长文本用 SendInput 逐字注入 KEYEVENTF_UNICODE 会丢字/重字（全角标点成对重复、
#    个别汉字丢失、ASCII 被改，实测「：：」「『『」「（（」「row--pproximate」），而旧的覆盖率校验
#    match=True 放过了乱码 → 乱码消息真的发了出去。现在：
#    * 长文本 / 含全角标点 -> 一律剪贴板 Ctrl+V（一次写入，不逐字注入）；短文本仍用 SendInput；
#    * 输入校验改「字符数比例 + 去重字符集覆盖 + 顺序一致性」三重判据（三过才算过）；
#    * 校验不过先重试（同一 attempt 内换方式再来一次；外层还有第 2 次 attempt），仍不过 -> abort
#      `type-verify-failed`，**绝不按 Enter**，由 watcher 走降级通道告警。
LONG_TEXT_CLIPBOARD_THRESHOLD = 12
# 全角标点/引号/括号：SendInput 逐字注入时最容易出成对重复/丢字，一律走剪贴板
FULLWIDTH_SENSITIVE = set("：；，。！？、『』「」（）【】《》〈〉“”‘’…—·～　"
                          "：；，。！？、（）【】《》“”‘’")
INPUT_MIN_CHAR_RATIO = 0.85     # OCR 有效字符数 / 期望有效字符数 下限
INPUT_MAX_CHAR_RATIO = 1.25     # 上限（重字/重复标点会超出）
INPUT_MIN_COVER = 0.85          # 期望去重字符集在 OCR 结果中的覆盖率下限（0.90->0.85，容忍长名字 OCR 噪声）
INPUT_MIN_SEQ = 0.80            # SequenceMatcher ratio 下限（0.75->0.80；成对重复会先掉这里）
INPUT_MAX_UNIQUE_RATIO = 1.15   # OCR 去重字符数 / 期望去重字符数 上限（重字/重复标点会显著变多）


_PUNCT_CANON = {
    "「": "“", "」": "”", "『": "“", "』": "”", "“": "“", "”": "”",
    "‘": "‘", "’": "’",
    "（": "(", "）": ")", "【": "[", "】": "]", "《": "<", "》": ">",
    "〈": "<", "〉": ">", "：": ":", "；": ";", "，": ",", "。": ".",
    "！": "!", "？": "?", "、": ",", "～": "~", "—": "-", "－": "-",
    "…": ".",
}


def _norm_for_verify(s):
    """输入校验用归一化：保留中文/字母/数字/标点（去掉空格/换行/装饰噪声）。

    全角/半角标点统一成同一种形态（『』「」-> “”，（）-> ()，：/： -> : …），
    使 OCR 把全角读成半角时不会误判；保留标点是为了能暴露「成对重复」这种 SendInput 乱码。
    """
    out = []
    for ch in str(s or ""):
        if ch.isspace():
            continue
        ch = _PUNCT_CANON.get(ch, ch)
        if ch.isalnum() or ("\u4e00" <= ch <= "\u9fff"):
            out.append(ch)
        elif ch in _PUNCT_CANON.values() or ch in ("…", "—"):
            out.append(ch)
        elif ch in FULLWIDTH_SENSITIVE:
            out.append(ch)
    return "".join(out)


def _dup_punct_pairs(expected_norm, got_norm):
    """找 OCR 结果里「相邻重复标点」且期望文本里没有同样的重复 -> SendInput 丢字/重字的典型特征。

    例：真实乱码 `：：` `『『` `』』』` `（（` `row--pproximate`；
    正常文本里的 `……` 不算（期望文本本身就有）。
    """
    hits = []
    exp_pairs = set(expected_norm[i:i + 2] for i in range(max(0, len(expected_norm) - 1)))
    for i in range(max(0, len(got_norm) - 1)):
        pair = got_norm[i:i + 2]
        if pair[0] == pair[1] and pair[0] in _PUNCT_CANON.values() and pair not in exp_pairs:
            if pair not in hits:
                hits.append(pair)
    return hits


def _needs_clipboard_input(text):
    """长文本或含全角标点/引号/括号 -> 必须走剪贴板粘贴；短纯文本才用 SendInput。"""
    s = str(text or "")
    if len(s) > LONG_TEXT_CLIPBOARD_THRESHOLD:
        return True
    return any(ch in FULLWIDTH_SENSITIVE for ch in s)


def input_text_metrics(expected, ocr_text):
    """计算输入校验判据：字符数比例 / 去重字符集覆盖 / 顺序一致性 / 去重字符数（+ 重复标点）。

    返回 dict：chars、expected、ratio、chars_ok、cover、seq、uniq、uniq_ratio、uniq_ok、dup、ok。
    ok = 三条判据全过 + 去重字符数不超标 + 没有「期望文本里不存在的相邻重复标点」。
    """
    exp = _norm_for_verify(expected)
    got = _norm_for_verify(ocr_text)
    n_exp, n_got = len(exp), len(got)
    ratio = (n_got / float(n_exp)) if n_exp else 0.0
    chars_ok = (INPUT_MIN_CHAR_RATIO <= ratio <= INPUT_MAX_CHAR_RATIO) if n_exp else False
    exp_set = set(exp)
    got_set = set(got)
    cover = (len(exp_set & got_set) / float(len(exp_set))) if exp_set else 0.0
    # 2026-10-26 23:3x 新增：去重字符数判据（成对重复/重字会让 OCR 去重字符数明显多于期望）
    n_uniq_exp, n_uniq_got = len(exp_set), len(got_set)
    uniq_ratio = (n_uniq_got / float(n_uniq_exp)) if n_uniq_exp else 0.0
    uniq_ok = (n_uniq_got <= max(n_uniq_exp + 2, int(n_uniq_exp * INPUT_MAX_UNIQUE_RATIO)))
    try:
        seq = difflib.SequenceMatcher(None, exp, got, autojunk=False).ratio()
    except Exception:
        seq = 0.0
    dup = _dup_punct_pairs(exp, got)
    return {"chars": n_got, "expected": n_exp, "ratio": ratio, "chars_ok": chars_ok,
            "cover": cover, "seq": seq,
            "uniq": n_uniq_got, "uniq_expected": n_uniq_exp, "uniq_ratio": uniq_ratio,
            "uniq_ok": uniq_ok, "dup": dup,
            "ok": bool(chars_ok and cover >= INPUT_MIN_COVER
                       and seq >= INPUT_MIN_SEQ and uniq_ok and not dup)}


def _fmt_input_metrics(m):
    dup = m.get("dup") or []
    return ("chars=%d/%d(ratio=%.2f %s) cover=%.2f seq=%.2f uniq=%d/%d(%.2f %s) dup=%s" % (
        int(m.get("chars") or 0), int(m.get("expected") or 0), float(m.get("ratio") or 0),
        "OK" if m.get("chars_ok") else "BAD", float(m.get("cover") or 0),
        float(m.get("seq") or 0), int(m.get("uniq") or 0),
        int(m.get("uniq_expected") or 0), float(m.get("uniq_ratio") or 0),
        "OK" if m.get("uniq_ok") else "BAD", ("有" + "".join(dup)) if dup else "无"))


def verify_input_text(img, geom, expected, ctx):
    """对输入框区域做 OCR，用三重判据严格校验；返回 (ok, detail, best_metrics, texts)。"""
    texts = ocr_candidates(img, geom["input"], ctx, max_plans=3)
    best = None
    for raw in texts or []:
        for part in re.split(r"[|\n]", str(raw)):
            if not _norm_for_verify(part):
                continue
            m = input_text_metrics(expected, part)
            if best is None or m["seq"] > best["seq"]:
                best = m
    if best is None:
        empty = input_text_metrics(expected, "")
        return False, "input-ocr-empty（输入框没有识别到文字）", empty, texts
    ok = best["ok"]
    bad = []
    if not best["chars_ok"]:
        bad.append("字符数比例不符")
    if best["cover"] < INPUT_MIN_COVER:
        bad.append("字符集覆盖不足")
    if best["seq"] < INPUT_MIN_SEQ:
        bad.append("顺序/内容不一致")
    if not best.get("uniq_ok"):
        bad.append("去重字符数超标")
    if best.get("dup"):
        bad.append("出现成对重复标点(%s)" % "".join(best["dup"]))
    return ok, ("三重校验通过" if ok else "三重校验不通过（%s）" % "、".join(bad)), best, texts


def _clipboard_restore_enabled(cfg):
    """剪贴板是否还原：2026-10-26 用户明确说「完事其实不用还原我的剪贴板」，默认 false。"""
    try:
        return bool(((cfg or {}).get("reply") or {}).get("restoreClipboard", False))
    except Exception:
        return False


def clear_input_and_type(ctx, hwnd, text, clip, attempt):
    """Ctrl+A + Delete 清空 -> 输入内容（长文本/含全角标点走剪贴板，短文本走 SendInput）
    -> 三重判据严格校验；返回 (img, geom, ok, detail)。"""
    log_stage("type", "清空输入框(Ctrl+A/Delete) 并输入正文 %r（第 %d 次）" % (text[:40], attempt))
    scale = watcher.dpi_scale_of(hwnd)
    force = str(os.environ.get("REPLY_FORCE_TYPE_PATH") or "").strip().lower()
    use_clip = _needs_clipboard_input(text)
    if force == "sendinput":
        use_clip = False
    elif force == "clipboard":
        use_clip = True
    log("  输入方式: %s（len=%d 全角敏感=%s force=%s）" % (
        "剪贴板粘贴 Ctrl+V" if use_clip else "SendInput 逐字注入",
        len(text), any(ch in FULLWIDTH_SENSITIVE for ch in text), force or "-"))

    def _clear():
        ctx.guard.tap(VK_A, mods=(VK_CONTROL,))
        ctx.sleep(0.08)
        ctx.guard.tap(VK_DELETE)
        ctx.sleep(0.1)

    # 2026-10-26 23:3x：先点一下输入框确保焦点在输入框（实测某些路径打开会话后焦点不在输入框，
    # Ctrl+A/Delete 清不掉旧草稿、输入也可能丢）；只点微信窗口内部，不会发送任何东西。
    # 注入保护：点击/清空/粘贴/打字每一步之前都复查「前台=目标微信」，否则 abort 零注入。
    check_injection_foreground("清空输入框前")
    try:
        _img0 = capture_window(ctx, hwnd)
        _geom0 = geom_of(_img0, scale)
        _l, _t, _r, _b = watcher.window_rect(hwnd)
        _ix = int((float(_geom0["input"][0]) + float(_geom0["input"][2])) / 2.0)
        _iy = int((float(_geom0["input"][1]) + float(_geom0["input"][3])) / 2.0)
        ctx.checkpoint("点击输入框前")
        ctx.guard.click_at(_l + _ix, _t + _iy)
        ctx.sleep(0.15)
    except ReplyAbort:
        raise
    except Exception as _exc:
        log("  [输入] 点击输入框失败（仍继续输入）: %s" % _exc)

    def _paste():
        check_injection_foreground("粘贴正文前")
        if not clip.saved_ok:
            clip.save_text()
        if not clip.set_text(text):
            return False
        clip.used = True
        ctx.sleep(0.1)
        ctx.guard.tap(VK_V, mods=(VK_CONTROL,))
        ctx.sleep(0.35)
        return True

    def _capture_and_verify(tag):
        img2 = capture_window(ctx, hwnd)
        geom2 = geom_of(img2, scale)
        ok2, detail2, metrics2, texts2 = verify_input_text(img2, geom2, text, ctx)
        log("  输入校验(%s): OCR=%r" % (tag, " | ".join(str(t) for t in (texts2 or []))[:150]))
        log("  输入校验判据(%s): %s -> ok=%s detail=%s" % (
            tag, _fmt_input_metrics(metrics2), ok2, detail2))
        return img2, geom2, ok2, "%s %s %s" % (tag, detail2, _fmt_input_metrics(metrics2))

    _clear()
    if use_clip:
        if not _paste():
            return (capture_window(ctx, hwnd), geom_of(capture_window(ctx, hwnd), scale),
                    False, "clipboard-set-failed")
        img, geom, ok, detail = _capture_and_verify("剪贴板")
        if ok:
            return img, geom, True, "clipboard-paste-verified " + detail
        # 剪贴板粘贴校验不过 -> 允许清空后再粘贴重来一次（仍不过就交给外层 abort）
        log("  剪贴板粘贴校验不通过 -> 清空后重试一次")
        _clear()
        if _paste():
            img, geom, ok, detail2 = _capture_and_verify("剪贴板重试")
            if ok:
                return img, geom, True, "clipboard-paste-verified(2nd) " + detail2
            return img, geom, False, "剪贴板粘贴两次校验均不过: " + detail2
        return img, geom, False, "clipboard-set-failed(retry)"
    # 短文本：SendInput 逐字（每个字符注入前由 InputGuard 复查前台）
    check_injection_foreground("输入正文前")
    ctx.guard.type_unicode(text)
    ctx.sleep(0.25)
    img, geom, ok, detail = _capture_and_verify("SendInput")
    if ok:
        return img, geom, True, "sendinput-unicode-verified " + detail
    # SendInput 校验不过 -> 改用剪贴板重试一次（长/敏感文本本来就走剪贴板，这里是短文本兜底）
    log("  SendInput 校验不通过 -> 改用剪贴板粘贴重试一次")
    _clear()
    if _paste():
        img, geom, ok, detail2 = _capture_and_verify("剪贴板兜底")
        if ok:
            return img, geom, True, "clipboard-paste-verified(fallback) " + detail2
        return img, geom, False, "SendInput+剪贴板两次校验均不过: " + detail2
    return img, geom, False, "输入校验不通过且剪贴板兜底失败: " + detail


def restore_previous_foreground(ctx, wechat_hwnd, user_back=False):
    """发送结束后的前台处理。

    🔴 2026-10-26 用户实测反馈（本轮修复）：发送完成后主动 SetForegroundWindow(原窗口)
    会把微信**甩到后台**（用户原话：「按理来说输出一个虚拟的 enter 就欧克了，但是她在这个
    时候不知道触发什么把微信切到后台了」）。默认改为**让微信留在前台**，
    只有 config.reply.restoreForeground=true 时才恢复旧的「还原给原窗口」行为。
    """
    cfg = getattr(ctx, "config", None)
    if not isinstance(cfg, dict):
        cfg = watcher.load_config()
    restore = bool(((cfg.get("reply") or {}).get("restoreForeground", False)))
    if not restore:
        log("发送后保持微信在前台（restoreForeground=false）%s"
            % ("；用户已回到电脑前也不切走（用户明确要求：发完就让微信留在前台）"
               if user_back else ""))
        return
    # ---- 以下为旧行为（restoreForeground=true 才走）----
    if user_back:
        log("用户已回到电脑前 -> 不还原前台窗口（不抢用户焦点）")
        return
    blocked, reason, detail = busyguard.foreground_blocked(cfg)
    if blocked:
        log("跳过前台还原（%s：%s）" % (reason, detail))
        return
    if int(user32.GetForegroundWindow() or 0) != wechat_hwnd:
        return
    prev = ctx.prev_fg
    try:
        if prev and prev != wechat_hwnd and user32.IsWindow(prev):
            user32.SetForegroundWindow(prev)
            log("已把前台还原给原窗口 hwnd=%s（0 鼠标）" % hex(prev))
    except Exception:
        pass


def run(ctx, args):
    cfg = watcher.load_config()
    match_cfg = (cfg.get("whitelistMatch") or {})
    target = (args.chat or "").strip()
    text = " ".join(str(args.text or "").split())
    if not target or not text:
        log("失败: --chat / --text 不能为空")
        return 2
    if len(text) > 800:
        text = text[:800]
    log("task: chat=%r text=%r dry_run=%s hard_timeout=%.0fs pid=%d" % (
        target, text, args.dry_run, HARD_TIMEOUT_SECONDS, os.getpid()))

    # ---- 0) 重复发送保护（只读，不算动作）----
    dup = find_duplicate(target, text)
    if dup:
        log("skip duplicate: 同一会话同一内容 120 秒内已发过（%s）" % dup.get("at"))
        print(json.dumps({"ok": False, "skip": "duplicate", "chat": target,
                          "text": text, "prev_at": dup.get("at")}, ensure_ascii=False))
        return 0

    # ---- 1) 忙闲守护（只读；只记日志，不动作）----
    log_stage("guard", "开始忙闲守护判断（前台 %s）" % fg_desc())
    guard = busyguard.evaluate(cfg)
    fg = (guard.get("checks") or {}).get("foreground") or {}
    idle = (guard.get("checks") or {}).get("idleSeconds")
    if guard.get("busy"):
        log_stage("guard", "busy -> 不动作直接退出（reason=%s）" % guard.get("reason"))
        log("skip: busy (reason=%s) detail=%s（0 次鼠标事件、0 次键盘注入；不切窗口、不按键、不点鼠标、未注入任何输入）"
            % (guard.get("reason"), guard.get("detail")))
        print(json.dumps({"ok": False, "skip": "busy", "reason": guard.get("reason"),
                          "detail": guard.get("detail"), "chat": target,
                          "mouse_events": ctx.guard.mouse_events,
                          "key_events": ctx.guard.key_events,
                          "elapsed": round(ctx.elapsed(), 3)}, ensure_ascii=False))
        return 0
    log_stage("guard", "通过（前台=%r title=%r idle=%.1fs）只读判断，未注入任何输入"
              % (fg.get("exe_base") or "?", (fg.get("title") or "")[:30], float(idle or 0)))
    log("忙闲守护通过（只读判断）: 前台=%r title=%r idle=%.1fs userActiveSeconds=%s" % (
        fg.get("exe_base") or "?", (fg.get("title") or "")[:40], float(idle or 0),
        busyguard.merge_config(cfg).get("userActiveSeconds")))

    clip = Clip()
    hwnd = None
    before_path = opened_path = typed_path = after_path = ""
    try:
        # ---- 2) 找窗口（必要时无鼠标恢复最小化窗口）----
        log_stage("activate", "开始找微信窗口（当前前台 %s）" % fg_desc())
        win = ensure_wechat_ready(ctx)
        hwnd = win["hwnd"]
        scale = watcher.dpi_scale_of(hwnd)
        log_stage("activate", "找到微信窗口 hwnd=%s size=%s" % (hex(hwnd), win["size"]))
        log("微信窗口: hwnd=%s size=%s（只读枚举/抓图；未点击）" % (hex(hwnd), win["size"]))
        img_before = capture_window(ctx, hwnd)
        before_path = save_img(img_before, "before")
        log("发送前截图: %s" % before_path)

        # ---- 3) 键盘切前台 ----
        ctx.checkpoint("切前台前")
        log_stage("activate", "切前台前 checkpoint 通过（前台 %s）" % fg_desc())
        if not activate_window(ctx, hwnd):
            log_stage("activate", "切前台失败 focus-failed（当前前台 %s）" % fg_desc())
            raise ReplyAbort("focus-failed（无法把微信切到前台；未做任何发送）")
        # 🔴 2026-10-26 23:3x 紧急修复：从这一刻起，所有键鼠注入前都必须确认「前台=这个微信窗口」，
        #    否则一个字符都不许注入（事故：前台已是 msedge 时仍注入了 Ctrl+A/Delete/Ctrl+V）。
        arm_injection_guard(hwnd, "run")
        check_injection_foreground("切前台后")
        log_stage("activate", "微信已在前台（当前前台 %s，0 次鼠标事件）" % fg_desc())
        log("已用零鼠标技巧（AttachThreadInput/ALT）把微信切到前台（0 次鼠标事件）")
        ctx.sleep(0.15)
        img_cur = capture_window(ctx, hwnd)
        geom_cur = geom_of(img_cur, scale)

        # ---- 4) Ctrl+F 搜索并打开目标会话（先确认搜索框真的拿到焦点，再打字）----
        opened = False
        detail = ""
        img = img_cur
        geom = geom_cur
        for attempt in range(1, 3):
            ctx.checkpoint("搜索前" if attempt == 1 else "重新搜索前")
            if attempt > 1:
                # 收尾上一次失败的搜索层（纯键盘）。🔴 2026-10-27 第一阶段：
                # 只有「已确证搜索层打开」时才允许按 Esc（esc_if_search_confirmed），
                # 否则一个 Esc 都不发（盲按 Esc 会把微信主窗口收进托盘）。
                esc_if_search_confirmed(ctx, "第 %d 次搜索前收尾上一次" % attempt)
                ctx.sleep(0.1)
                ctx.mark_search_open(False)
                img_cur = capture_window(ctx, hwnd)
                geom_cur = geom_of(img_cur, scale)
            log_stage("search", "第 %d 次：Ctrl+F 打开搜索（前台 %s）" % (attempt, fg_desc()))
            check_injection_foreground("搜索前再确认前台")
            ok_search, img_s = search_open_check(ctx, hwnd, geom_cur, img_cur, attempt)
            if not ok_search:
                log_stage("search", "Ctrl+F 未生效 -> 中止，未输入任何字符、未按 Enter")
                log_error("搜索层未打开 search-not-opened")
                raise ReplyAbort("search-not-opened（Ctrl+F 未生效；未输入任何字符、未按 Enter）")
            opened, _confirmed, img, geom, detail = do_open_chat(
                ctx, hwnd, target, match_cfg, attempt, clip)
            if opened:
                log_stage("search", "会话已打开 display=%r" % (detail,))
                break
            log_stage("search", "第 %d 次打开失败: %s" % (attempt, detail))
            log("  第%d次搜索/打开未成功: %s" % (attempt, detail))
        if not opened:
            log_stage("search", "两次都没打开目标会话 -> 中止（未输入回复正文、未按 Enter）")
            log_error("打开会话失败 open-chat-failed: %s" % detail)
            exc = ReplyAbort("open-chat-failed（%s；未输入回复正文、未按 Enter）" % detail)
            # 2026-10-26 晚：把可回给用户的说明一起返回，watcher 侧据此写失败日志 + 降级提醒
            # 简化标点（用户要求：降级告警文案不用『』（）这类标点）
            exc.user_message = "小耳朵没打开会话 %s，这条没发出去，原因 open-chat-failed" % target
            raise exc
        opened_path = save_img(img, "opened")
        log("会话已打开截图: %s（标题=%r）" % (opened_path, detail))

        # ---- 5) Ctrl+A 清空输入框 + 输入内容（纯键盘）----
        typed_ok = False
        typed_detail = ""
        typed_img = img
        typed_geom = geom
        for attempt in range(1, 3):
            ctx.checkpoint("输入前" if attempt == 1 else "重新输入前")
            log_stage("type", "第 %d 次：清空输入框 + 输入正文（前台 %s）" % (attempt, fg_desc()))
            typed_img, typed_geom, typed_ok, typed_detail = clear_input_and_type(
                ctx, hwnd, text, clip, attempt)
            log_stage("type", "输入校验 ok=%s detail=%s" % (typed_ok, typed_detail))
            if typed_ok:
                break
            if attempt < 2:
                # 🔴 2026-10-26 23:3x 紧急修复：删掉「Escape/Tab 切焦点后重试」这种盲操作
                #   （实测会在前台被抢走时把键注入到别的窗口/切走焦点、甚至弄丢微信窗口）。
                #   现在：不做任何焦点切换；下一轮 clear_input_and_type 开头会重新确认前台，
                #   前台不是微信就直接 abort。
                log("  输入未确认 -> 不做任何焦点切换；只在仍为微信前台时清空重来一次"
                    "（前台=%s）" % fg_desc())
        typed_path = save_img(typed_img, "typed")
        log("输入后截图: %s（%s，等待发送）" % (typed_path, typed_detail))
        if not typed_ok:
            log_stage("type", "输入未通过校验 -> 不按 Enter，绝不会发出")
            log_error("输入校验失败 type-verify-failed: %s" % typed_detail)
            exc = ReplyAbort("type-verify-failed（%s；未按 Enter，绝不会发出）" % typed_detail)
            # 简化标点（用户要求：降级告警文案不用『』（）这类标点）
            exc.user_message = ("小耳朵提醒，想给 %s 发的消息没能完整输入，已放弃发送，"
                                "原因 type-verify-failed" % target)
            raise exc

        if args.dry_run:
            log_stage("done", "dry-run：定位/打开会话/输入校验全部通过，不按 Enter（未发送）")
            log("dry-run: 键盘定位/打开会话/输入校验全部通过，不按 Enter（未发送）")
            ctx.guard.tap(VK_A, mods=(VK_CONTROL,))
            ctx.sleep(0.05)
            ctx.guard.tap(VK_DELETE)
            print(json.dumps({"ok": True, "dry_run": True, "chat": target,
                              "before": before_path, "opened": opened_path,
                              "typed": typed_path, "elapsed": round(ctx.elapsed(), 2),
                              "mouse_events": ctx.guard.mouse_events,
                              "key_events": ctx.guard.key_events}, ensure_ascii=False))
            return 0

        # ---- 6) 发送前复检（硬超时 / 游戏前台 / 用户动键鼠）+ 防抖窗口 ----
        ctx.sleep(1.0)   # 防抖窗口：用户此刻回来动鼠标/键盘也能被下面抓到，抓不到才按 Enter
        ctx.checkpoint("发送前")
        log_stage("send", "发送前 checkpoint 通过（前台 %s 剩余 %.1fs）" % (
            fg_desc(), ctx.remaining()))
        img_check = capture_window(ctx, hwnd)
        geom_check = geom_of(img_check, scale)
        hit_h, texts_h, _ = name_match_region(img_check, geom_check["header"], target, ctx, match_cfg, max_plans=2)
        if not hit_h:
            log_stage("send", "发送前标题校验失败 chat-changed（OCR=%r）-> 不按 Enter" % (
                " | ".join(texts_h)[:120],))
            log_error("发送前标题校验失败 chat-changed（焦点丢失/会话被切走）")
            raise ReplyAbort("chat-changed（发送前标题校验失败 OCR=%r；未按 Enter）" % (" | ".join(texts_h)[:120]))
        log_stage("send", "发送前校验通过: 标题=%r 输入框有字 前台=%s" % (target, fg_desc()))
        log("发送前校验通过: 标题=%r 输入框有字 前台非游戏 用户未活动（%s）"
            % (target, mouse_desc(ctx)))
        if ctx.remaining() < 2.0:
            log_stage("send", "硬超时余量不足（剩 %.1fs）-> 不按 Enter" % ctx.remaining())
            raise ReplyAbort("hard-timeout-margin（硬超时只剩 %.1fs，不按 Enter，绝不会发出）" % ctx.remaining())

        # ---- 7) Enter 发送 ----
        log_stage("send", "按 Enter 发送（前台 %s）" % fg_desc())
        ctx.guard.tap(VK_RETURN)
        ctx.sleep(0.8)
        img_after = capture_window(ctx, hwnd)
        after_path = save_img(img_after, "after")
        geom_after = geom_of(img_after, scale)
        still_there, texts_in = text_match_region(img_after, geom_after["input"], text, ctx, max_plans=1)
        diff = region_diff_ratio(typed_img, img_after, geom_after["chat"])
        input_cleared = not still_there
        sent_ok = bool(input_cleared and (diff is not None and diff > 0.002))
        log("发送后截图: %s" % after_path)
        log("校验: input_cleared=%s pixel_diff=%s（输入框OCR=%r）" % (
            input_cleared, ("%.4f" % diff) if diff is not None else "None",
            " | ".join(texts_in)[:100]))
        log_stage("send", "发送后校验 input_cleared=%s pixel_diff=%s -> sent_ok=%s" % (
            input_cleared, ("%.4f" % diff) if diff is not None else "None", sent_ok))
        if sent_ok:
            rst = load_reply_state()
            rst["sent"] = rst.get("sent", []) + [{
                "chat": target, "display": detail, "text": text,
                "ts": time.time(), "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "before": before_path, "opened": opened_path,
                "typed": typed_path, "after": after_path,
                "pixel_diff": diff, "hard_timeout": HARD_TIMEOUT_SECONDS,
                "elapsed": round(ctx.elapsed(), 2),
                "mouse_events": ctx.guard.mouse_events,
                "key_events": ctx.guard.key_events}]
            save_reply_state(rst)
            log_stage("done", "已发送 text=%r target=%r 用时 %.2fs（%d 鼠标事件）" % (
                text, target, ctx.elapsed(), ctx.guard.mouse_events))
            log("已发送 text=%r target=%r display=%r（%s，用时 %.2fs）"
                % (text, target, detail, mouse_desc(ctx), ctx.elapsed()))
            print(json.dumps({"ok": True, "chat": target, "display": detail, "text": text,
                              "before": before_path, "opened": opened_path,
                              "typed": typed_path, "after": after_path,
                              "pixel_diff": diff, "elapsed": round(ctx.elapsed(), 2),
                              "mouse_events": ctx.guard.mouse_events,
                              "key_events": ctx.guard.key_events}, ensure_ascii=False))
            return 0
        log_stage("send", "发送失败：已按 Enter 但校验未通过（input_cleared=%s diff=%s）" % (
            input_cleared, diff))
        log_error("发送后校验不一致：已按 Enter 但输入框未清空/聊天区无变化（可能 Enter 未生效/焦点丢失）")
        log("失败: 已按 Enter 但校验未通过（可能未发出）")
        print(json.dumps({"ok": False, "chat": target, "detail": detail,
                          "before": before_path, "after": after_path,
                          "pixel_diff": diff, "input_cleared": input_cleared,
                          "elapsed": round(ctx.elapsed(), 2)}, ensure_ascii=False))
        return 7
    except ReplyAbort as exc:
        log_stage("abort", "阶段=%s 原因=%s（已释放所有按键；%s）" % (
            getattr(ctx, "step", "?"), exc, mouse_desc(ctx)))
        if not ctx.abort_logged:
            log("abort: %s（已释放所有按键；%s）" % (exc, mouse_desc(ctx)))
        log_error("ReplyAbort 中止", exc)
        # 收尾：🔴 2026-10-27 第一阶段铁律 —— 默认什么都不做，**不按 Esc**！
        #   高怀疑真凶：Ctrl+F 没打开搜索层时 Esc 落在微信主窗口上 -> 微信 4.x 把窗口收进托盘
        #   （用户看到的「她把微信窗口关了」）。只有确证搜索层打开时才允许 Esc（见
        #   esc_if_search_confirmed），中止收尾一律不发 Esc。
        #   Ctrl+A/Delete 清理残留输入仍然保留，但必须走 InputGuard（每条注入前校验前台=微信，
        #   非微信一律不注入）。
        try:
            if hwnd and int(user32.GetForegroundWindow() or 0) == hwnd:
                blocked, _r, _d = busyguard.foreground_blocked(ctx.config)
                if not blocked:
                    log("中止收尾：不按 Esc（默认什么都不做；搜索层确证=%s）" % bool(ctx.search_open))
                    # 清一下残留输入（搜索框/输入框草稿），避免下次误发
                    ctx.guard.tap(VK_A, mods=(VK_CONTROL,))
                    ctx.sleep(0.05)
                    ctx.guard.tap(VK_DELETE)
                    log("已清空残留输入（Ctrl+A/Delete 收尾，走 InputGuard 前台校验，未发送、未按 Esc）")
            else:
                log("中止收尾：前台不是微信或没有窗口 -> 不注入任何键鼠（不按 Esc、不清空）")
        except Exception:
            pass
        print(json.dumps({"ok": False, "abort": str(exc), "stage": getattr(ctx, "step", ""),
                          "chat": target,
                          "userMessage": getattr(exc, "user_message", ""),
                          "before": before_path, "typed": typed_path,
                          "elapsed": round(ctx.elapsed(), 2),
                          "mouse_events": ctx.guard.mouse_events,
                          "key_events": ctx.guard.key_events}, ensure_ascii=False))
        return 8
    except Exception as exc:
        log_stage("abort", "阶段=%s 未捕获异常 %s: %s（已释放所有按键）" % (
            getattr(ctx, "step", "?"), type(exc).__name__, exc))
        log_error("未捕获异常", exc)
        log("失败: 异常 %s: %s（已释放所有按键）" % (type(exc).__name__, exc))
        print(json.dumps({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                          "stage": getattr(ctx, "step", ""),
                          "chat": target, "elapsed": round(ctx.elapsed(), 2)},
                         ensure_ascii=False))
        return 9
    finally:
        # 顺序：先判断「用户是否已回来」（要在释放按键之前判，release 会更新本程序注入时刻）
        try:
            user_back = ctx.activity.user_active()[0]
            if user_back:
                log("发送结束：检测到用户已回到电脑前 -> 不再做任何输入/焦点切换")
        except Exception:
            user_back = False
        # 先释放所有按键 -> 处理剪贴板 -> 还原原前台窗口
        ctx.finish()
        try:
            if clip.used:
                # 2026-10-26 用户明确说「完事其实不用还原我的剪贴板」-> 默认不还原；
                # 保留 config.json → reply.restoreClipboard=true 可恢复旧行为。
                if _clipboard_restore_enabled(cfg):
                    clip.restore()
                else:
                    log("本次使用剪贴板发送（不还原，reply.restoreClipboard=false）")
        except Exception:
            pass
        try:
            if hwnd:
                restore_previous_foreground(ctx, hwnd, user_back=user_back)
        except Exception:
            pass
        finally:
            # 注入保护解除：之后不再向任何窗口注入（正常流程已结束）
            disarm_injection_guard()


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    watcher.set_dpi_aware()
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", required=True)
    ap.add_argument("--text", required=True)
    ap.add_argument("--match", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if args.match and args.match.strip() and args.match.strip() != args.chat.strip():
        log("提示: --match=%r 已忽略（v2 只用 --chat 名做键盘搜索）" % args.match)
    cfg = watcher.load_config()
    ctx = Ctx(cfg)
    try:
        rc = run(ctx, args)
        log_stage("done", "退出码=%s" % rc)
        return rc
    finally:
        ctx.finish()


if __name__ == "__main__":
    sys.exit(main())
