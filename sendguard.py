# -*- coding: utf-8 -*-
"""发送护栏 + 发送记录落盘（方案A：只限制「别乱发消息」）。

5 条护栏（全部在 config.json 的 guard 段可调）：
  1. 白名单外绝不发              guard.whitelistOnly
  2. 同一会话 N 秒内只发 1 条     guard.perChatMinIntervalSeconds（默认 30）
  3. 同一会话连续 <= N 条         guard.perChatMaxBurst（默认 5）-> 暂停，停了等用户发话
  4. 同一句话不重复发             guard.dedupeSameText（默认 true）
  5. 每条成功发送写 logs/sent-YYYYMMDD.jsonl（时间/会话名/回复原文/未读指纹）

只做判断与记录，不碰微信、不发消息（发送仍由 watcher 调 reply.py）。
"""

import json
import os
import re
import time
from datetime import datetime

GUARD_TAG = "[GUARD]"

DEFAULT_GUARD = {
    "enabled": True,
    "whitelistOnly": True,
    "perChatMinIntervalSeconds": 30,
    "perChatMaxBurst": 5,
    "burstPauseSeconds": 1800,
    "burstResetSeconds": 600,
    "dedupeSameText": True,
    "dedupeWindowSeconds": 600,
    "sentLogEnabled": True,
}


def _ts(now=None):
    return datetime.fromtimestamp(float(now if now is not None else time.time())).strftime(
        "%Y-%m-%d %H:%M:%S")


def norm(name):
    """会话名归一化（去首尾空白/压缩空白/小写）。"""
    return " ".join(str(name or "").split()).lower()


def guard_cfg(cfg):
    """从整份 config.json 取 guard 段并与默认值合并。"""
    out = dict(DEFAULT_GUARD)
    raw = (cfg or {}).get("guard")
    if isinstance(raw, dict):
        out.update(raw)
    return out


def state_path(base_dir):
    return os.path.join(base_dir, "guard_state.json")


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            st = json.load(f)
        if not isinstance(st, dict):
            st = {}
    except Exception:
        st = {}
    st.setdefault("version", 1)
    st.setdefault("chats", {})
    if not isinstance(st["chats"], dict):
        st["chats"] = {}
    return st


def save_state(path, st):
    st["updatedAt"] = _ts()
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        pass


def sent_log_path(log_dir, now=None):
    day = datetime.fromtimestamp(float(now if now is not None else time.time())).strftime("%Y%m%d")
    return os.path.join(log_dir, "sent-%s.jsonl" % day)


def append_sent_log(log_dir, record):
    """追加一行 JSONL（UTF-8 无 BOM）。返回文件路径。"""
    os.makedirs(log_dir, exist_ok=True)
    path = sent_log_path(log_dir, record.get("ts"))
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        return ""
    return path


def _chat_rec(st, chat):
    chats = st.setdefault("chats", {})
    key = norm(chat) or "(unknown)"
    rec = chats.get(key)
    if not isinstance(rec, dict):
        rec = {}
        chats[key] = rec
    rec.setdefault("chat", chat)
    rec.setdefault("lastSentAt", 0.0)
    rec.setdefault("consecutive", 0)
    rec.setdefault("lastText", "")
    rec.setdefault("pausedUntil", 0.0)
    rec.setdefault("pausedReason", "")
    rec.setdefault("totalSent", 0)
    if not isinstance(rec.get("recentTexts"), list):
        rec["recentTexts"] = []
    return rec


def _prune_recent(rec, window, now):
    cutoff = float(now) - float(window)
    rec["recentTexts"] = [x for x in rec.get("recentTexts") or []
                          if isinstance(x, dict) and float(x.get("ts") or 0) >= cutoff]
    if len(rec["recentTexts"]) > 30:
        rec["recentTexts"] = rec["recentTexts"][-30:]


def check(cfg, st, chat, text, whitelisted, now=None):
    """发送前护栏判断。

    返回 {"ok": bool, "skip": "<原因>", "detail": str, "stop": bool, "nextAllowedAt": float}。
    check 可能小幅修改 state（清理过期暂停/重置连续计数），调用方负责 save_state。
    """
    now = float(now if now is not None else time.time())
    g = guard_cfg(cfg)
    res = {"ok": True, "skip": "", "detail": "", "stop": False, "nextAllowedAt": 0.0}
    if not g.get("enabled", True):
        res["detail"] = "guard.enabled=false（护栏未启用）"
        return res

    # 1) 白名单外绝不发
    if g.get("whitelistOnly", True) and not whitelisted:
        res.update({"ok": False, "skip": "whitelist-miss",
                    "detail": "会话 %r 不在白名单" % chat})
        return res

    rec = _chat_rec(st, chat)
    min_int = float(g.get("perChatMinIntervalSeconds") or 0)
    max_burst = int(g.get("perChatMaxBurst") or 0)
    pause = float(g.get("burstPauseSeconds") or 0)
    reset_after = float(g.get("burstResetSeconds") or 0)
    last = float(rec.get("lastSentAt") or 0)
    consecutive = int(rec.get("consecutive") or 0)
    paused_until = float(rec.get("pausedUntil") or 0)

    # 暂停到期 / 冷却很久 -> 重新计数（相当于新一段对话）
    if paused_until and now >= paused_until:
        rec["pausedUntil"] = 0.0
        rec["pausedReason"] = ""
        rec["consecutive"] = 0
        consecutive = 0
    if last and reset_after > 0 and (now - last) >= reset_after:
        rec["consecutive"] = 0
        consecutive = 0

    # 3) 连续条数用满 -> 停了等用户发话
    if paused_until and now < paused_until:
        res.update({"ok": False, "skip": "paused-burst-limit", "stop": True,
                    "detail": "连续 %d 条已达上限，暂停到 %s（等用户发话）"
                              % (consecutive, _ts(paused_until)),
                    "nextAllowedAt": paused_until})
        return res
    if max_burst > 0 and consecutive >= max_burst:
        until = now + max(pause, 60.0)
        rec["pausedUntil"] = until
        rec["pausedReason"] = "consecutive>=%d" % max_burst
        rec["consecutive"] = max_burst
        res.update({"ok": False, "skip": "per-chat-burst-limit", "stop": True,
                    "detail": "同一会话已连续发 %d 条（上限 %d）-> 停了等用户发话，暂停 %.0f 秒"
                              % (consecutive, max_burst, max(pause, 60.0)),
                    "nextAllowedAt": until})
        return res

    # 2) 同一会话 N 秒内只发 1 条
    if min_int > 0 and last and (now - last) < min_int:
        res.update({"ok": False, "skip": "per-chat-interval",
                    "detail": "距上一条只过了 %.0fs（限制 %ds）"
                              % (now - last, int(min_int)),
                    "nextAllowedAt": last + min_int})
        return res

    # 4) 同一句话不重复发
    if g.get("dedupeSameText", True):
        win = float(g.get("dedupeWindowSeconds") or 600)
        _prune_recent(rec, win, now)
        want = norm(text)
        for item in rec.get("recentTexts") or []:
            if norm(item.get("text")) == want:
                res.update({"ok": False, "skip": "duplicate-text",
                            "detail": "同一句话 %s 已发过（%s）"
                                      % (time.strftime("%Y-%m-%d %H:%M:%S",
                                                       time.localtime(float(item.get("ts") or 0))),
                                         "去重窗口 %.0fs" % win)})
                return res

    res["detail"] = "护栏通过"
    return res


def record(cfg, st, chat, text, fingerprint="", extra=None, now=None):
    """记一次已确认发送：更新会话状态，返回可直接写 sent-*.jsonl 的记录。"""
    now = float(now if now is not None else time.time())
    g = guard_cfg(cfg)
    rec = _chat_rec(st, chat)
    rec["chat"] = str(chat or "")
    rec["lastSentAt"] = now
    rec["consecutive"] = int(rec.get("consecutive") or 0) + 1
    rec["lastText"] = str(text or "")
    rec["totalSent"] = int(rec.get("totalSent") or 0) + 1
    rec["recentTexts"] = list(rec.get("recentTexts") or []) + [
        {"text": str(text or ""), "ts": now}]
    rec["lastFingerprint"] = str(fingerprint or "")
    _prune_recent(rec, float(g.get("dedupeWindowSeconds") or 600), now)
    out = {
        "at": _ts(now),
        "ts": now,
        "chat": str(chat or ""),
        "text": str(text or ""),
        "fingerprint": str(fingerprint or ""),
        "consecutive": rec["consecutive"],
        "perChatMinIntervalSeconds": g.get("perChatMinIntervalSeconds"),
    }
    if isinstance(extra, dict):
        for k in ("display", "reason", "guard", "gen", "model", "latencyMs",
                  "before", "after", "elapsed", "mouse_events", "key_events"):
            if k in extra:
                out[k] = extra[k]
    return out
