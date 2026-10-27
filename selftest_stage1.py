# -*- coding: utf-8 -*-
"""selftest_stage1.py —— 「小耳朵」第一阶段离线自测（零屏幕操作、零微信消息、零真机注入）。

用法: python selftest_stage1.py   （退出码 0 = 全过；1 = 有 FAIL）

覆盖：
  T1 输入校验（input_text_metrics）：漏字/重字 -> False；正常 -> True
  T2 面板判别 + 搜索层守卫（读 screenshots 历史截图，离线 OCR；不碰屏幕）
  T3 熔断 mock：连续 2 次失败不熔断 -> 成功清零 -> 再连续 3 次失败 -> 写 breaker_tripped.json + STOP + [PANIC] breaker
  T4 急停：do_panic / 微信口令急停路径（写 STOP + [PANIC] panic-*.jsonl），hotkey 检测只读
  T5 py_compile 7 个 .py
结束清理：恢复 STOP 原内容、删除测试生成的 breaker_state/breaker_tripped、把日志截断回基线。
"""
import io
import os
import subprocess
import sys
import time
import traceback

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import reply      # noqa: E402
import watcher    # noqa: E402

PASS, FAIL = [], []


def ck(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    (PASS if cond else FAIL).append(name)
    print("[%s] %s%s" % (tag, name, ("  " + str(detail)) if detail else ""))


STOP = os.path.join(BASE, "STOP")
BREAKER_STATE = os.path.join(BASE, "breaker_state.json")
BREAKER_TRIPPED = os.path.join(BASE, "breaker_tripped.json")
LOG_DIR = os.path.join(BASE, "logs")
TODAY = time.strftime("%Y%m%d")
LOG_PATHS = [
    os.path.join(LOG_DIR, "watchdog-%s.log" % TODAY),
    os.path.join(LOG_DIR, "panic-%s.jsonl" % TODAY),
    os.path.join(LOG_DIR, "reply-failures-%s.jsonl" % TODAY),
]


def _snapshot():
    snap = {"stop_exists": os.path.exists(STOP), "stop_bytes": None,
            "breaker_state": None, "breaker_tripped": None, "logs": {}}
    if snap["stop_exists"]:
        snap["stop_bytes"] = open(STOP, "rb").read()
    for p in (BREAKER_STATE, BREAKER_TRIPPED):
        if os.path.exists(p):
            snap[os.path.basename(p)] = open(p, "rb").read()
    for p in LOG_PATHS:
        snap["logs"][p] = os.path.getsize(p) if os.path.exists(p) else None
    return snap


def _restore(snap):
    # STOP：用户现在是急停态，必须恢复原样
    try:
        if snap["stop_exists"] and snap["stop_bytes"] is not None:
            with open(STOP, "wb") as f:
                f.write(snap["stop_bytes"])
        elif not snap["stop_exists"] and os.path.exists(STOP):
            os.remove(STOP)
    except Exception as exc:
        print("  [cleanup] 恢复 STOP 失败:", exc)
    for p, key in ((BREAKER_STATE, "breaker_state.json"), (BREAKER_TRIPPED, "breaker_tripped.json")):
        try:
            if snap.get(key) is not None:
                with open(p, "wb") as f:
                    f.write(snap[key])
            elif os.path.exists(p):
                os.remove(p)
        except Exception as exc:
            print("  [cleanup] 清理 %s 失败: %s" % (p, exc))
    for p, size in snap["logs"].items():
        try:
            if size is None:
                if os.path.exists(p):
                    os.remove(p)
            elif os.path.exists(p) and os.path.getsize(p) > size:
                with open(p, "r+b") as f:
                    f.truncate(size)
        except Exception as exc:
            print("  [cleanup] 截断日志 %s 失败: %s" % (p, exc))


# ───────────────────────── T1 输入校验 ─────────────────────────
def t1_input_metrics():
    print("\n===== T1 输入校验（input_text_metrics：漏字/重字 -> False，正常 -> True） =====")
    normal_exp = "在的，今晚可能不行，改天吧"
    m = reply.input_text_metrics(normal_exp, normal_exp)
    ck("T1a 正常文本（OCR 完全一致）-> True", m["ok"] is True, reply._fmt_input_metrics(m))

    normal_exp2 = "小耳朵提醒，找不到会话，请检查白名单名字或微信是否改名"
    got_missing = "小耳提醒，不到会话，检查白名单名字微信改名"
    m = reply.input_text_metrics(normal_exp2, got_missing)
    ck("T1b 漏字（丢「朵/找/请/或/是否」共 6 字，chars=21/27）-> False", m["ok"] is False,
       reply._fmt_input_metrics(m))

    got_dup = "小耳朵提醒，，找不到会话，请检查白名单名字或微信是否改名"
    m = reply.input_text_metrics(normal_exp2, got_dup)
    ck("T1c 重字（全角逗号成对重复「，，」）-> False", m["ok"] is False,
       reply._fmt_input_metrics(m))

    short_exp = "在的"
    got_dup2 = "在的的"
    m = reply.input_text_metrics(short_exp, got_dup2)
    ck("T1d 短文本重字「在的的」-> False", m["ok"] is False, reply._fmt_input_metrics(m))

    # 用户真机那次的乱码样本（成对重复标点 + 丢字）
    exp_real = "小耳朵提醒，找不到会话耳朵不存在的会话ABC，请检查白名单名字或微信是否改名"
    got_real = "小耳朵提醒：：不到会话『『耳朵不存在的会话ABC』』』检查白名单名字或微信是否改名（（因=row--pproximate）"
    m = reply.input_text_metrics(exp_real, got_real)
    ck("T1e 真机乱码样本（：：『『（（row--）-> False", m["ok"] is False,
       reply._fmt_input_metrics(m))


# ───────────────────────── T2 面板判别 + 搜索层守卫 ─────────────────────────
def t2_panel_guard():
    print("\n===== T2 面板判定 + 搜索层守卫（只读历史截图） =====")
    from PIL import Image

    class _C:
        def check(self):
            pass

    ctx = _C()
    cases = [
        # (路径, 目标, 期望 panel_kind, 期望 输入框有文字)
        (r"screenshots\reply-20261026-225320-dropdown.png", "文件传输助手", "dropdown", False),
        (r"screenshots\reply-20261026-191845-dropdown.png", "文件传输助手", "dropdown", False),
        (r"screenshots\reply-20261026-194815-dropdown.png", "文件传输助手", "dropdown", False),
        # 真下拉但面板下方露出会话列表（带时间戳）：搜索框里有目标名 -> 复核后仍判 dropdown
        (r"screenshots\reply-20261026-195419-dropdown.png", "文件传输助手", "dropdown", False),
        (r"screenshots\reply-20261026-231311-dropdown.png", "耳朵不存在的会话ABC", "dropdown", False),
        # 说明：真实截图语料不随公开仓库发布；截图不存在时本组用例自动 SKIP。
        # 目标名用占位名，跑真实语料时可自行替换。
        (r"screenshots\reply-20261026-232052-dropdown.png", "某个较长的中文群名", "dropdown", False),
        # 假下拉（实际是会话列表、搜索框只有 placeholder）：conversation-list
        # 注：输入框有字判据 2026-10-27 第二阶段改为「目标名语义」（只认目标名被打进输入框；
        #     这两张图里是别的草稿文案，与目标名无关 -> 不再判有残留）
        (r"screenshots\reply-20261026-233501-dropdown.png", "文件传输助手", "conversation-list", False),
        (r"screenshots\reply-20261026-233515-dropdown.png", "文件传输助手", "conversation-list", False),
    ]
    skipped_t2 = 0
    for path, target, want_kind, want_text in cases:
        p = os.path.join(BASE, path) if not os.path.isabs(path) else path
        if not os.path.exists(p):
            # 公开仓库不带 screenshots/（真实屏幕截图含聊天隐私），没有语料就整组跳过
            print("[SKIP] T2 %s 截图不存在（公开仓库不附带 screenshots/）" % os.path.basename(path))
            skipped_t2 += 1
            continue
        try:
            img = Image.open(p).convert("RGB")
            geom = reply.geom_of(img, 1.25)
            has_text, detail = reply._typed_text_evidence(img, geom, ctx, target)
            a = reply.analyze_dropdown(img, geom, ctx, target)
            kind = a.get("panel_kind")
            ck("T2 %s -> panel=%s/输入框有字=%s（期望 %s/%s）"
               % (os.path.basename(path)[:34], kind, has_text, want_kind, want_text),
               kind == want_kind and has_text == want_text,
               "依据=%s 输入框OCR=%r" % ((a.get("panel_evidence") or "")[:60], detail))
        except Exception as exc:
            ck("T2 %s 分析异常" % os.path.basename(path), False, exc)
            traceback.print_exc()

    if skipped_t2:
        print("[SKIP] T2 有 %d 张真实截图语料缺失，已跳过（不影响构造用例）" % skipped_t2)

    # 纯逻辑构造用例（与 selftest_rows.panel_kind_cases 相同口径）
    def mk(text, y):
        return {"text": text, "norm": reply._clean_row_text(text), "y": y,
                "x0": 120.0, "x1": 300.0, "kind": "avatar", "section": ""}

    ck("T2f 2 行时间戳 -> conversation-list",
       reply.classify_panel_kind([mk("文件传输助手 23:20", 130),
                                  mk("腾讯新闻 昨天 12:16", 200)])[0] == "conversation-list")
    ck("T2g 无时间戳 -> dropdown",
       reply.classify_panel_kind([mk("文件传输助手", 130),
                                  mk("文件传输助手 打开", 200)])[0] == "dropdown")
    ck("T2h 空候选 -> unknown", reply.classify_panel_kind([])[0] == "unknown")
    # Esc 禁令：未确证搜索层 -> 一个 Esc 都不发（返回 False、不注入）
    class _CtxNoEsc:
        search_open = False
        search_open_ts = 0.0
        guard = None
    ck("T2i 搜索层未确证 -> esc_if_search_confirmed 返回 False（不按 Esc）",
       reply.esc_if_search_confirmed(_CtxNoEsc(), "自测") is False)
    ok, detail = reply.wechat_window_alive(0)
    ck("T2j 窗口存活校验：hwnd=0 -> False（不碰真窗口）", ok is False, detail)
    # 第二阶段真机发现的误判：微信空输入框 placeholder「长按鼠标 语音输入」（OCR 读成「长按电标 语音输入」）
    # 含 >=2 连续汉字，旧判据会误判成「字打进了聊天框」；现在必须去掉 placeholder 再判。
    ck("T2k placeholder『长按鼠标.语音输入』不算残留文字",
       reply._texts_have_typed_evidence(["长按鼠标.语音输入"], "文件传输助手")[0] is False)
    ck("T2l placeholder OCR 变体『-一 E引一、长按电标 语音输入』也不算残留",
       reply._texts_have_typed_evidence(["-一 E引一、 长按电标 语音输入"], "文件传输助手")[0] is False)
    ck("T2l2 placeholder 噪声残留『-一 让 一 * 长按和鼠标 语音输入』也不算残留（真机实际 OCR）",
       reply._texts_have_typed_evidence(["-一 让 一 * 长按和鼠标 语音输入"], "文件传输助手")[0] is False)
    ck("T2m 真被打进输入框的目标名『文件传输助手』-> 判定有残留",
       reply._texts_have_typed_evidence(["文件传输助手"], "文件传输助手")[0] is True)
    ck("T2n 目标名被 OCR 读残『文件传输助』（覆盖 5/6）-> 仍判定有残留",
       reply._texts_have_typed_evidence(["文件传输助"], "文件传输助手")[0] is True)


# ───────────────────────── T3 熔断 mock ─────────────────────────
def t3_breaker():
    print("\n===== T3 熔断 mock（不真发、不碰屏幕） =====")
    wd = object.__new__(watcher.Watchdog)
    wd.config = watcher.load_config()
    wd.config.setdefault("panic", {})["toast"] = False   # 🔴 离线测试不弹气泡
    wd.state = {}
    wd.retry_state = None
    wd.rpc = None
    wd._release_lock = lambda: None

    ck("T3a config.breaker 已配置", wd.config.get("breaker") ==
       {"enabled": True, "consecutiveFailures": 3, "resetOnSuccess": True},
       wd.config.get("breaker"))

    # 先验证「成功清零」
    r1 = wd.breaker_register_failure("mock-failure-1")
    r2 = wd.breaker_register_failure("mock-failure-2")
    ck("T3b 连续 2 次失败不熔断", (r1["count"], r2["count"], r1["tripped"], r2["tripped"])
       == (1, 2, False, False), (r1, r2))
    st = watcher.breaker_state_load()
    ck("T3c breaker_state.json 落盘 {consecutiveFailures=2,lastFailureAt,lastReason}",
       st.get("consecutiveFailures") == 2 and st.get("lastFailureAt")
       and st.get("lastReason") == "mock-failure-2", st)
    wd.breaker_register_success()
    st = watcher.breaker_state_load()
    ck("T3d 成功发送一次 -> 连续失败计数清零", st.get("consecutiveFailures") == 0, st)

    # 正式场景：连续 3 次失败 -> 熔断
    orig_exit = os._exit
    exit_calls = []

    def fake_exit(code=0):
        exit_calls.append(code)
        raise SystemExit(code)

    os._exit = fake_exit
    tripped = False
    try:
        wd.breaker_register_failure("mock-trip-1")
        wd.breaker_register_failure("mock-trip-2")
        ck("T3e 清零后再 2 次失败仍不熔断", not os.path.exists(BREAKER_TRIPPED))
        try:
            wd.breaker_register_failure("mock-trip-3")
        except SystemExit:
            tripped = True
    finally:
        os._exit = orig_exit

    ck("T3f 连续 3 次失败 -> 熔断并结束自身进程（os._exit 被调用）",
       tripped and bool(exit_calls), exit_calls)
    tripped_rec = {}
    if os.path.exists(BREAKER_TRIPPED):
        import json
        tripped_rec = json.load(io.open(BREAKER_TRIPPED, encoding="utf-8"))
    ck("T3g breaker_tripped.json 生成（含触发时间与原因）",
       bool(tripped_rec.get("trippedAt")) and bool(tripped_rec.get("reason"))
       and tripped_rec.get("consecutiveFailures") == 3, tripped_rec)
    ck("T3h STOP 已被写（内容含 breaker）",
       os.path.exists(STOP) and b"breaker" in open(STOP, "rb").read(),
       open(STOP, "rb").read()[:120])
    log_path = LOG_PATHS[0]
    log_txt = io.open(log_path, encoding="utf-8", errors="replace").read() if os.path.exists(log_path) else ""
    ck("T3i 日志有 [PANIC] breaker 行", "[PANIC] breaker" in log_txt,
       next((ln for ln in log_txt.splitlines() if "[PANIC] breaker" in ln), ""))
    panic_txt = io.open(LOG_PATHS[1], encoding="utf-8", errors="replace").read() \
        if os.path.exists(LOG_PATHS[1]) else ""
    ck("T3j panic 审计 logs/panic-*.jsonl 记到 source=breaker", '"source": "breaker"' in panic_txt)


# ───────────────────────── T4 急停 ─────────────────────────
def t4_panic():
    print("\n===== T4 急停（hotkey 只读 / do_panic / 微信口令，不弹气泡） =====")
    wd = object.__new__(watcher.Watchdog)
    wd.config = watcher.load_config()
    wd.config.setdefault("panic", {})["toast"] = False
    wd.state = {}
    wd.rpc = None
    wd._release_lock = lambda: None

    down = wd.panic_hotkey_down()
    ck("T4a 热键检测函数可调用且当前未按下（只读 GetAsyncKeyState）", down is False, down)

    orig_exit = os._exit
    exit_calls = []

    def fake_exit(code=0):
        exit_calls.append(code)
        raise SystemExit(code)

    os._exit = fake_exit
    try:
        # ① 模拟热键急停（直接调 do_panic，等价于 run 循环里命中热键）
        try:
            wd.do_panic("hotkey", "自测：模拟 Ctrl+Alt+Shift+S 四键同按（未真实按键）")
        except SystemExit:
            pass
        ck("T4b hotkey 急停 -> 写 STOP + [PANIC] + 退出",
           os.path.exists(STOP) and b"hotkey" in open(STOP, "rb").read() and bool(exit_calls),
           open(STOP, "rb").read()[:120])
        # ② 微信口令急停（走真实 _remote_forward 最前面的口令检查；不会调用 reply.py）
        try:
            wd._remote_forward("文件传输助手", "小耳朵 急停", "selftest")
            raised = False
        except SystemExit:
            raised = True
        ck("T4c 微信口令「小耳朵 急停」-> 立即写 STOP + 退出（未发消息）",
           raised and b"wechat-command" in open(STOP, "rb").read(),
           open(STOP, "rb").read()[:120])
        # ③ 休眠轮询期间命中热键也要急停（把 panic_hotkey_down 打桩成 True，不真实按键）
        orig_hk = wd.panic_hotkey_down
        wd.panic_hotkey_down = lambda: True
        try:
            try:
                wd.hotkey_watch_sleep(0.5)
                raised3 = False
            except SystemExit:
                raised3 = True
        finally:
            wd.panic_hotkey_down = orig_hk
        ck("T4e 休眠轮询期间热键命中 -> 急停（写 STOP + 退出）",
           raised3 and b"hotkey" in open(STOP, "rb").read())
    finally:
        os._exit = orig_exit
    log_txt = io.open(LOG_PATHS[0], encoding="utf-8", errors="replace").read() \
        if os.path.exists(LOG_PATHS[0]) else ""
    ck("T4d 日志有 [PANIC] source=hotkey 与 source=wechat-command",
       "[PANIC] source=hotkey" in log_txt and "[PANIC] source=wechat-command" in log_txt)


# ───────────────────────── T5 py_compile ─────────────────────────
def t5_compile():
    print("\n===== T5 py_compile 全部 .py =====")
    py = sys.executable
    files = ["reply.py", "watcher.py", "busyguard.py", "genreply.py",
             "sendguard.py", "sendfile.py", "selftest_rows.py"]
    for name in files:
        p = os.path.join(BASE, name)
        r = subprocess.run([py, "-m", "py_compile", p], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        ck("T5 py_compile %s" % name, r.returncode == 0, (r.stderr or "")[-300:])


def main():
    snap = _snapshot()
    print("基线快照：STOP存在=%s breaker_state存在=%s breaker_tripped存在=%s" % (
        snap["stop_exists"], snap["breaker_state"] is not None,
        snap["breaker_tripped"] is not None))
    try:
        t1_input_metrics()
        t2_panel_guard()
        t3_breaker()
        t4_panic()
        t5_compile()
    finally:
        _restore(snap)
        print("\n清理：STOP 已恢复原样（存在=%s 内容=%r）；breaker_state/breaker_tripped=%s/%s；日志已截断回基线" % (
            os.path.exists(STOP),
            open(STOP, "rb").read()[:60] if os.path.exists(STOP) else b"",
            os.path.exists(BREAKER_STATE), os.path.exists(BREAKER_TRIPPED)))
    print("\n===== 汇总：PASS=%d FAIL=%d =====" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项:", FAIL)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
