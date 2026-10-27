# -*- coding: utf-8 -*-
"""发文件到微信（远程指令链路专用，2026-10-26 新增）

用法:
  E:\\anaconda\\python.exe sendfile.py --chat "文件传输助手" --file "E:\\绝对\\路径\\x.txt" [--caption "说明"]

流程:
  校验文件存在 + 敏感文件拦截（credential/key/token/password/密码/令牌/.pem/.pfx 一律拒绝）
  -> 复用 reply.py：找微信窗口/切前台/Ctrl+F 搜索打开目标会话（do_open_chat）
  -> 受控点击输入框拿焦点 -> 文件放剪贴板（CF_HDROP，只读源文件）
  -> Ctrl+V -> 等附件预览出现（输入框区域像素变化）-> 可选 caption
  -> 发送前复检（标题 == 目标会话、硬超时余量）-> Enter -> 发送后截图/像素校验
  -> 写审计 logs/sendfile-YYYYMMDD.jsonl + 发送记录 logs/sent-YYYYMMDD.jsonl

硬性：
  * 20 秒硬超时（复用 reply.Ctx 的看门狗线程，超时立即释放按键/中止）
  * 鼠标 DOWN/UP 成对 + finally 释放；只点微信窗口内部；全部动作打 stage= 日志
  * 敏感文件一律拒绝（不发送、只写审计）
  * 只读取源文件，绝不修改/移动/删除
"""
import argparse
import ctypes
import io
import json
import os
import re
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import reply          # noqa: E402  复用窗口枚举/切前台/打开会话/截图/像素差
import watcher        # noqa: E402  复用 dpi/config/日志目录
import sendguard      # noqa: E402  发送记录落盘
import win32clipboard  # noqa: E402
import win32con        # noqa: E402

LOG_DIR = os.path.join(BASE_DIR, "logs")
AUDIT_FILE = os.path.join(LOG_DIR, "sendfile-YYYYMMDD.jsonl")
HARD_TIMEOUT_SECONDS = 20.0

# 敏感文件拦截：文件名/路径命中即拒绝（不区分大小写）
SENSITIVE_RE = re.compile(
    r"credential|key|token|password|密码|令牌|\.pem$|\.pfx$", re.IGNORECASE)

_LOG_PATH = None


def log(msg):
    # 2026-10-26 需求：日志里的密码形态一律打 ******
    try:
        _mask = getattr(watcher, "mask_sensitive", None)
        if _mask:
            msg = _mask(msg)
    except Exception:
        pass
    line = "%s [SENDFILE] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = _LOG_PATH or os.path.join(LOG_DIR, "sendfile-%s.log" % time.strftime("%Y%m%d"))
        with io.open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def log_stage(stage, msg=""):
    log("stage=%s %s" % (stage, msg))


def audit(record):
    """写审计 JSONL（UTF-8 无 BOM）返回路径；失败返回空串。"""
    path = os.path.join(LOG_DIR, "sendfile-%s.jsonl" % time.strftime("%Y%m%d"))
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        item = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "ts": time.time()}
        item.update(record or {})
        with io.open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
        return path
    except Exception as exc:
        log("审计写入失败: %s" % exc)
        return ""


class _DROPFILES(ctypes.Structure):
    _fields_ = [("pFiles", ctypes.c_uint), ("x", ctypes.c_long), ("y", ctypes.c_long),
                ("fNC", ctypes.c_int), ("fWide", ctypes.c_int)]


def set_clipboard_files(paths):
    """把文件列表放进剪贴板（CF_HDROP，资源管理器复制文件同款）。成功返回 True。"""
    abs_paths = [os.path.abspath(str(p)) for p in (paths or []) if str(p or "").strip()]
    if not abs_paths:
        return False
    df = _DROPFILES()
    df.pFiles = ctypes.sizeof(_DROPFILES)
    df.fWide = 1
    header = ctypes.string_at(ctypes.byref(df), ctypes.sizeof(_DROPFILES))
    files = ("\0".join(abs_paths) + "\0\0").encode("utf-16-le")
    data = header + files
    for attempt in range(3):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_HDROP, data)
            finally:
                win32clipboard.CloseClipboard()
            return True
        except Exception as exc:
            log("  剪贴板设置失败(第 %d 次): %s" % (attempt + 1, exc))
            time.sleep(0.15)
    return False


def get_clipboard_files():
    """读取剪贴板里的文件列表（CF_HDROP）；失败返回 []。"""
    try:
        win32clipboard.OpenClipboard()
        try:
            if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_HDROP):
                return []
            data = win32clipboard.GetClipboardData(win32con.CF_HDROP)
            return [os.path.abspath(str(x)) for x in (data or []) if str(x or "").strip()]
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return []


def is_sensitive(path):
    """敏感文件判定：文件名/完整路径命中 credential|key|token|password|密码|令牌|.pem|.pfx。"""
    low = str(path or "").replace("/", "\\").lower()
    name = os.path.basename(low)
    return bool(SENSITIVE_RE.search(low) or SENSITIVE_RE.search(name))


def _click_input_box(ctx, hwnd, geom):
    """受控点击微信输入框中心（只点窗口内部，坐标越界 abort）。"""
    l, t, r, b = watcher.window_rect(hwnd)
    x0, y0, x1, y1 = [int(v) for v in geom["input"]]
    ix, iy = int((x0 + x1) // 2), int((y0 + y1) // 2)
    sx, sy = l + ix, t + iy
    if not (l <= sx < r and t <= sy < b):
        raise reply.ReplyAbort("click-out-of-window（输入框坐标 (%d,%d) 不在微信窗口内）" % (sx, sy))
    ctx.checkpoint("点击输入框前")
    got = ctx.guard.click_at(sx, sy)
    log("  [点击] 输入框 窗口图像坐标=(%d,%d) 屏幕=(%d,%d)；%s" % (
        ix, iy, got[0], got[1], reply.mouse_desc(ctx)))
    return got


def _open_conversation(ctx, hwnd, chat, match_cfg, scale, clip):
    """复用 reply.py 的搜索+下拉 OCR+点击+标题校验，打开目标会话。返回 (opened, img, geom, detail)。"""
    img = reply.capture_window(ctx, hwnd)
    geom = reply.geom_of(img, scale)
    detail = ""
    for attempt in (1, 2):
        ctx.checkpoint("搜索前" if attempt == 1 else "重新搜索前")
        if attempt > 1:
            if ctx.search_open:
                ctx.guard.tap(reply.VK_ESCAPE)
                ctx.sleep(0.2)
                ctx.search_open = False
            img = reply.capture_window(ctx, hwnd)
            geom = reply.geom_of(img, scale)
        ok_search, _img_s = reply.search_open_check(ctx, hwnd, geom, img, attempt)
        if not ok_search:
            return False, img, geom, "search-not-opened"
        opened, _confirmed, img2, geom2, detail = reply.do_open_chat(
            ctx, hwnd, chat, match_cfg, attempt, clip)
        if opened:
            return True, img2, geom2, detail
        img, geom = img2, geom2
    return False, img, geom, detail


def run_sendfile(args, cfg):
    path = os.path.abspath(str(args.file))
    chat = str(args.chat).strip()
    caption = str(args.caption or "").strip()
    t0 = time.time()
    base = {"kind": "sendfile", "chat": chat, "file": path}

    # 0) 参数/文件/敏感校验
    log("task: chat=%r file=%r caption=%r size=%s hard_timeout=%.0fs" % (
        chat, path, caption[:40], (os.path.getsize(path) if os.path.isfile(path) else "-"),
        HARD_TIMEOUT_SECONDS))
    if not chat:
        audit(dict(base, result="reject-empty-chat"))
        return 2, {"ok": False, "error": "empty-chat"}
    if not os.path.isfile(path):
        log_stage("guard", "文件不存在或不是普通文件 -> 拒绝：%r" % path)
        audit(dict(base, result="reject-file-not-found"))
        return 2, {"ok": False, "error": "file-not-found", "file": path}
    size = os.path.getsize(path)
    if is_sensitive(path):
        log_stage("guard", "敏感文件拦截（命中 credential|key|token|password|密码|令牌|.pem|.pfx）"
                           "-> 拒绝发送：%r" % path)
        audit(dict(base, size=size, result="reject-sensitive"))
        return 3, {"ok": False, "error": "sensitive-file", "file": path}

    ctx = reply.Ctx(cfg)
    clip = reply.Clip()
    hwnd = None
    before_path = opened_path = pasted_path = after_path = ""
    try:
        match_cfg = cfg.get("whitelistMatch") or {}
        log_stage("activate", "找微信窗口（当前前台 %s）" % reply.fg_desc())
        win = reply.ensure_wechat_ready(ctx)
        hwnd = win["hwnd"]
        scale = watcher.dpi_scale_of(hwnd)
        img = reply.capture_window(ctx, hwnd)
        before_path = reply.save_img(img, "sendfile-before")
        log("微信窗口 hwnd=%s size=%s 发送前截图=%s" % (hex(hwnd), win["size"], before_path))
        ctx.checkpoint("切前台前")
        if not reply.activate_window(ctx, hwnd):
            raise reply.ReplyAbort("focus-failed（无法把微信切到前台）")
        reply.arm_injection_guard(hwnd, "sendfile")
        reply.check_injection_foreground("切前台后")
        ctx.sleep(0.15)

        # 1) 打开目标会话
        opened, img, geom, detail = _open_conversation(ctx, hwnd, chat, match_cfg, scale, clip)
        if not opened:
            raise reply.ReplyAbort("open-chat-failed（%s；未发文件）" % detail)
        opened_path = reply.save_img(img, "sendfile-opened")
        log_stage("search", "会话已打开 display=%r（截图 %s）" % (detail, opened_path))

        # 2) 点击输入框拿焦点 + 记录粘贴前基线
        _click_input_box(ctx, hwnd, geom)
        ctx.sleep(0.2)
        img_ready = reply.capture_window(ctx, hwnd)
        geom = reply.geom_of(img_ready, scale)

        # 3) 文件放剪贴板 + Ctrl+V
        log_stage("paste", "文件放剪贴板(CF_HDROP，只读源文件): %r" % path)
        if not set_clipboard_files([path]):
            raise reply.ReplyAbort("clipboard-set-failed（剪贴板设置失败）")
        got = get_clipboard_files()
        log("  剪贴板文件列表: %r" % got)
        if path not in got:
            raise reply.ReplyAbort("clipboard-verify-failed（剪贴板里没有该文件）")
        ctx.guard.tap(reply.VK_V, mods=(reply.VK_CONTROL,))

        # 4) 等附件预览出现（输入框区域像素变化；没出现绝不按 Enter）
        preview_ok = False
        preview_diff = None
        img_paste = img_ready
        for _i in range(12):
            ctx.sleep(0.25)
            img_paste = reply.capture_window(ctx, hwnd)
            preview_diff = reply.region_diff_ratio(img_ready, img_paste, geom["input"])
            if preview_diff is not None and preview_diff > 0.002:
                preview_ok = True
                break
        pasted_path = reply.save_img(img_paste, "sendfile-pasted")
        # 🔴 2026-10-26 收尾验收修复：粘贴文件卡片后**不要**重新 geom_of(img_paste)！
        #   watcher.detect_list_bounds 用窗口底部 80%~92% 亮度带找「会话列表/聊天区」分界，
        #   文件卡片正好落在这个带里 -> list_right 从 384 误判到 639（logical），
        #   header/input 等区域整体右移 -> 发送前标题 OCR 只读到 '| |' -> 误报
        #   chat-changed，文件永远发不出去。header 布局与附件卡片无关，沿用粘贴前的 geom。
        log_stage("paste", "附件预览 preview_ok=%s input_diff=%s 截图=%s" % (
            preview_ok, preview_diff, pasted_path))
        if not preview_ok:
            raise reply.ReplyAbort("attachment-preview-not-visible（未按 Enter，绝不会发出）")
        if caption:
            log_stage("type", "输入 caption %r" % caption[:40])
            ctx.guard.type_unicode(caption)
            ctx.sleep(0.2)

        # 5) 发送前复检（标题 == 目标会话 + 硬超时余量）
        ctx.checkpoint("发送前")
        img_check = reply.capture_window(ctx, hwnd)
        # 用粘贴前几何（附件卡片不影响 header 区域；见上面 geom_of 的坑）
        geom_check = geom
        hit_h, texts_h, _disp = reply.name_match_region(
            img_check, geom_check["header"], chat, ctx, match_cfg, max_plans=2)
        if not hit_h:
            raise reply.ReplyAbort("chat-changed（发送前标题不匹配 OCR=%r）"
                                   % (" | ".join(texts_h)[:120]))
        if ctx.remaining() < 2.0:
            raise reply.ReplyAbort("hard-timeout-margin（硬超时只剩 %.1fs）" % ctx.remaining())
        log_stage("send", "发送前校验通过: 标题=%r 附件预览已出现 前台=%s（余 %.1fs）" % (
            chat, reply.fg_desc(), ctx.remaining()))

        # 6) Enter 发送 + 发送后校验
        ctx.guard.tap(reply.VK_RETURN)
        ctx.sleep(0.9)
        img_after = reply.capture_window(ctx, hwnd)
        after_path = reply.save_img(img_after, "sendfile-after")
        geom_after = reply.geom_of(img_after, scale)
        chat_diff = reply.region_diff_ratio(img_paste, img_after, geom_after["chat"])
        input_diff = reply.region_diff_ratio(img_paste, img_after, geom_after["input"])
        sent_ok = bool((chat_diff is not None and chat_diff > 0.002)
                       or (input_diff is not None and input_diff > 0.01))
        elapsed = round(time.time() - t0, 2)
        log("校验: chat_diff=%s input_diff=%s 发送后截图=%s -> sent_ok=%s" % (
            ("%.4f" % chat_diff) if chat_diff is not None else "None",
            ("%.4f" % input_diff) if input_diff is not None else "None",
            after_path, sent_ok))
        rec = dict(base, size=size, result="sent" if sent_ok else "send-not-verified",
                   previewOk=True, previewDiff=preview_diff, chatDiff=chat_diff,
                   inputDiff=input_diff, before=before_path, opened=opened_path,
                   pasted=pasted_path, after=after_path, elapsed=elapsed,
                   mouse_events=ctx.guard.mouse_events, key_events=ctx.guard.key_events)
        audit(rec)
        if sent_ok:
            try:
                sendguard.append_sent_log(LOG_DIR, {
                    "at": time.strftime("%Y-%m-%d %H:%M:%S"), "ts": time.time(),
                    "chat": chat, "text": "[文件] " + os.path.basename(path),
                    "kind": "sendfile", "file": path, "size": size,
                    "before": before_path, "after": after_path})
            except Exception as exc:
                log("发送记录落盘失败（不影响已发送）: %s" % exc)
            try:
                import watcher as _w
                _w.memo_log(cfg, "sendfile", "sent %s to %s" % (os.path.basename(path), chat), available=lambda: (True, ""))
            except Exception:
                pass
            log_stage("done", "已发送文件 chat=%r file=%r size=%d 用时 %.2fs（%s）" % (
                chat, path, size, elapsed, reply.mouse_desc(ctx)))
            return 0, {"ok": True, "chat": chat, "file": path, "size": size,
                       "before": before_path, "opened": opened_path, "pasted": pasted_path,
                       "after": after_path, "preview_diff": preview_diff,
                       "chat_diff": chat_diff, "elapsed": elapsed,
                       "mouse_events": ctx.guard.mouse_events,
                       "key_events": ctx.guard.key_events}
        log_stage("send", "发送后校验未通过（已按 Enter，但文件卡片可能没出现）")
        return 5, {"ok": False, "chat": chat, "file": path, "after": after_path,
                   "chat_diff": chat_diff, "input_diff": input_diff, "elapsed": elapsed}
    except reply.ReplyAbort as exc:
        log_stage("abort", "阶段=%s 原因=%s（已释放所有按键；%s）" % (
            getattr(ctx, "step", "?"), exc, reply.mouse_desc(ctx)))
        audit(dict(base, size=size, result="abort", error=str(exc)[:500],
                   stage=getattr(ctx, "step", ""), before=before_path,
                   opened=opened_path, pasted=pasted_path, after=after_path,
                   elapsed=round(time.time() - t0, 2)))
        return 4, {"ok": False, "abort": str(exc), "stage": getattr(ctx, "step", "")}
    except Exception as exc:
        log_stage("abort", "未捕获异常 %s: %s（已释放按键）" % (type(exc).__name__, exc))
        audit(dict(base, size=size, result="error",
                   error="%s: %s" % (type(exc).__name__, exc)))
        return 9, {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    finally:
        try:
            ctx.finish()
        except Exception:
            pass
        try:
            if clip.used:
                # 2026-10-26 用户要求：剪贴板不用还原（默认 reply.restoreClipboard=false）
                if reply._clipboard_restore_enabled(cfg):
                    clip.restore()
                else:
                    log("本次使用剪贴板发送（不还原，reply.restoreClipboard=false）")
        except Exception:
            pass
        try:
            if hwnd:
                reply.restore_previous_foreground(ctx, hwnd, user_back=False)
        except Exception:
            pass
        finally:
            try:
                reply.disarm_injection_guard()
            except Exception:
                pass


def main(argv=None):
    global _LOG_PATH
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="发文件到微信（远程指令链路专用）")
    ap.add_argument("--chat", required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--caption", default="")
    args = ap.parse_args(argv)
    watcher.set_dpi_aware()
    os.makedirs(LOG_DIR, exist_ok=True)
    _LOG_PATH = os.path.join(LOG_DIR, "sendfile-%s.log" % time.strftime("%Y%m%d"))
    cfg = watcher.load_config()
    rc, result = run_sendfile(args, cfg)
    print(json.dumps(result, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    sys.exit(main())
