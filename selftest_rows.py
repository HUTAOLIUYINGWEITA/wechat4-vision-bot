# -*- coding: utf-8 -*-
"""selftest_rows.py —— 会话行选择精度离线回归（2026-10-26 用户反馈后新增）

用途：不用微信、不发消息，直接拿 screenshots 下真实搜索下拉截图当语料，验证
      「面板自适应定位 + 图标判定行类型（头像 vs 灰色放大镜）+ 目标名 exact 匹配 + 歧义 abort」。

用法:
  E:\\anaconda\\python.exe selftest_rows.py                # 默认目标「文件传输助手」
  E:\\anaconda\\python.exe selftest_rows.py --target "BBB示例测试群"

输出:
  ① 每张图：OCR 行（文本/坐标/图标判定/所属分组）→ 选中行 → 判定 OK/ABORT + 理由
  ② 构造用例：同名歧义 / 聊天记录行很多 / 目标不存在 / OCR 读错一个字 / 群聊-私聊标题校验
  ③ 汇总表：图名 -> 结论 -> 理由
"""
import argparse
import glob
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import reply  # noqa: E402
from PIL import Image  # noqa: E402


class _Ctx:
    def check(self):
        pass


def _describe_row(r):
    ic = r.get("icon") or {}
    return "kind=%-6s section=%-4r y=%-4.0f x=[%4.0f,%4.0f] icon(colorful=%.2f satMean=%3.0f satStd=%3.0f) text=%r" % (
        r.get("kind"), r.get("section") or "", r.get("y", 0), r.get("x0", 0), r.get("x1", 0),
        ic.get("colorful", -1), ic.get("satMean", -1), ic.get("satStd", -1), r.get("text", "")[:28])


def _select_point(analysis, target):
    """算出最终点击点（与 find_conversation_row 内一致）。"""
    sel = reply.select_target_row(analysis["rows"], reply._clean_row_text(target))
    if sel["status"] not in ("ok", "ok_approx"):
        return sel, None
    r = sel["row"]
    s = 1.0  # 点击点只用于展示，按面板左缘夹取
    dl = max(4.0, analysis["panel_left"])
    dr = dl + 430.0 * s
    xc = int(round((float(r["x0"]) + float(r["x1"])) / 2.0))
    xc = max(int(dl) + 8, min(int(dr) - 8, xc))
    return sel, (xc, int(round(float(r["y"]))))


def run_image(path, target, verbose=True):
    """分析一张真实截图，返回 (verdict, reason, detail_lines)。"""
    ctx = _Ctx()
    try:
        img = Image.open(path).convert("RGB")
    except Exception as exc:
        return "ABORT", "image-load-failed", ["  %s" % exc]
    geom = reply.geom_of(img, 1.25)
    lines = []
    try:
        analysis = reply.analyze_dropdown(img, geom, ctx, target)
    except Exception as exc:
        return "ABORT", "analysis-error: %s" % exc, lines
    lines.append("  panel_left=%.0f top=%.0f bottom=%.0f search_y=%s rows=%d" % (
        analysis["panel_left"], analysis["panel_top"], analysis["panel_bottom"],
        ("%.0f" % analysis["search_line"]["y"]) if analysis["search_line"] else "-",
        len(analysis["rows"])))
    # 2026-10-27 第一阶段：面板类型判定（dropdown / conversation-list / unknown + 依据）
    lines.append("  ★面板判定=%r 依据: %s" % (
        analysis.get("panel_kind"), analysis.get("panel_evidence") or "-"))
    for r in analysis["rows"]:
        lines.append("  " + _describe_row(r))
    if analysis.get("panel_kind") == "conversation-list":
        if verbose:
            print("\n".join(lines))
        return "ABORT", "conversation-list（判定为会话列表，不是搜索下拉，不取候选）", lines
    if not analysis["rows"]:
        return "ABORT", "no-panel/no-rows(%s)" % (analysis.get("reason") or "?"), lines
    sel, point = _select_point(analysis, target)
    if sel["status"] in ("ok", "ok_approx"):
        r = sel["row"]
        approx = sel["status"] == "ok_approx"
        lines.append("  -> 选中 section=%r text=%r 点击点=%s 理由=%s%s" % (
            r.get("section"), r.get("text"), point, sel["reason"],
            ("（近似唯一 覆盖 %.0f%%）" % (100.0 * float(sel.get("coverage") or 0))) if approx else ""))
        if verbose:
            print("\n".join(lines))
        return "OK", "%s %r @%s%s" % (
            "近似命中" if approx else "选中", r.get("text"), point,
            (" (覆盖 %.0f%%)" % (100.0 * float(sel.get("coverage") or 0))) if approx else ""), lines
    lines.append("  -> 不点击；理由=%s 候选=%s" % (
        sel["reason"], [c.get("text") for c in (sel.get("candidates") or [])][:6]))
    if verbose:
        print("\n".join(lines))
    return "ABORT", "%s(%d 候选)" % (sel["reason"], len(sel.get("candidates") or [])), lines


def _mkrow(text, kind="avatar", section="", y=100.0):
    clean = reply._clean_row_text(text)
    st = {"colorful": 0.4, "satMean": 90.0, "satStd": 100.0} if kind == "avatar" else \
         {"colorful": 0.01, "satMean": 10.0, "satStd": 25.0}
    return {"text": text, "norm": clean, "kind": (kind if kind != "gray" else "gray"),
            "section": section, "y": y, "x0": 120.0, "x1": 260.0, "icon": st}


def fabricated_cases():
    """构造用例（覆盖歧义/聊天记录/群聊同名/OCR 读错/不存在）。"""
    out = []

    # ① 同名歧义：一个私聊会话 + 一个群聊，名字都 exact
    rows = [_mkrow("张三", "avatar", "", 150),
            _mkrow("张三", "avatar", "群聊", 190),
            _mkrow("张三的群", "avatar", "群聊", 230)]
    out.append(("构造: 同名歧义（会话+群聊都叫「张三」）",
                reply.select_target_row(rows, reply._clean_row_text("张三")), "row-ambiguous"))

    # ② 聊天记录行很多：目标名只出现在灰色放大镜行 -> 不能点
    rows = [_mkrow("张三", "gray", "聊天记录", 150 + i * 42) for i in range(6)]
    rows.append(_mkrow("张三", "gray", "", 400))
    out.append(("构造: 只有聊天记录行（灰色放大镜）",
                reply.select_target_row(rows, reply._clean_row_text("张三")), "row-not-found"))

    # ③ 目标名只是别人名字的前缀（「张三」vs「张三的群」）-> 精确率不足 -> ABORT
    rows = [_mkrow("李四", "avatar", "", 150), _mkrow("张三的群", "avatar", "群聊", 200),
            _mkrow("王五", "gray", "聊天记录", 250)]
    out.append(("构造: 目标名是别人名字前缀（精确率不足）",
                reply.select_target_row(rows, reply._clean_row_text("张三")), "row-approximate"))

    # ④ 群聊分组里 exact 唯一 -> OK（expected_group 由 section 推导）
    rows = [_mkrow("张三", "avatar", "群聊", 150), _mkrow("张三的群", "avatar", "群聊", 190)]
    out.append(("构造: 群聊分组 exact 唯一",
                reply.select_target_row(rows, reply._clean_row_text("张三")), "ok"))

    # ⑤ OCR 读错一个字（无 exact）-> 近似唯一，允许先点（安全闸在点开后的标题校验）
    rows = [_mkrow("文件传输助首", "avatar", "", 150)]
    out.append(("构造: 目标名被 OCR 读错一个字（近似唯一）",
                reply.select_target_row(rows, reply._clean_row_text("文件传输助手")), "ok_approx"))

    # ⑥ 两个头像行都跟目标很像、得分接近 -> 近似不唯一 -> ABORT（不瞎猜）
    rows = [_mkrow("示例长名测试群甲乙丙丁戊", "avatar", "", 150),
            _mkrow("示例长名测试群甲乙丙丁戊已", "avatar", "群聊", 190)]
    out.append(("构造: 两个近似候选太接近（不唯一）",
                reply.select_target_row(
                    rows, reply._clean_row_text("示例长名测试群甲乙丙丁戊己")), "row-ambiguous"))

    # ⑦ 覆盖率不足 -> ABORT
    rows = [_mkrow("文件传输", "avatar", "", 150)]
    out.append(("构造: 近似候选覆盖率不足",
                reply.select_target_row(rows, reply._clean_row_text("示例长名测试群甲乙丙丁戊己")),
                "row-approximate"))
    return out


def panel_kind_cases():
    """2026-10-27 第一阶段构造用例：面板类型判别（会话列表 vs 搜索下拉）的纯逻辑回归。"""
    def mk(text, y):
        return {"text": text, "norm": reply._clean_row_text(text), "y": y,
                "x0": 120.0, "x1": 300.0, "kind": "avatar", "section": ""}

    cases = [
        ("会话列表: 2 行时间戳（HH:MM + 昨天）-> conversation-list",
         [mk("文件传输助手 23:20", 130), mk("腾讯新闻 昨天 12:16", 200)], "conversation-list"),
        ("会话列表: 3 行都是裸 HH:MM -> conversation-list",
         [mk("BBB示例测试群 18:37", 130), mk("裤子 16:37", 200), mk("微信团队 09:11", 270)],
         "conversation-list"),
        ("搜索下拉: 头像行不带时间戳 -> dropdown",
         [mk("文件传输助手", 130), mk("文件传输助手 打开", 200)], "dropdown"),
        ("搜索下拉: 只有 1 行时间戳（噪声）-> dropdown",
         [mk("文件传输助手", 130), mk("聊天记录 18:37", 200)], "dropdown"),
        ("空候选 -> unknown", [], "unknown"),
        ("时间戳藏在 variants 里也识别",
         [{"text": "文件传输助手", "norm": "文件传输助手", "y": 130.0, "x0": 120.0,
           "x1": 300.0, "kind": "avatar", "section": "",
           "variants": ["文件传输助手 18:55"]},
          {"text": "腾讯新闻", "norm": "腾讯新闻", "y": 200.0, "x0": 120.0,
           "x1": 300.0, "kind": "avatar", "section": "",
           "variants": ["腾讯新闻 昨天 12:16"]}], "conversation-list"),
    ]
    out = []
    for name, rows, expect in cases:
        kind, evidence = reply.classify_panel_kind(rows)
        out.append((name, kind, expect, evidence))
    return out


def title_validation_cases():
    """三重校验里的「标题 exact + 群聊/私聊」构造用例。"""
    cases = [
        ("私聊: 标题 exact", ["文件传输助手"], "文件传输助手", False, True, "ok"),
        ("私聊: 标题带群聊编号 (5) -> 疑似点错群", ["文件传输助手 (5)"], "文件传输助手", False, False,
         "row-group-mismatch"),
        ("私聊: 目标名被点进「示例长名测试群甲乙丙丁戊己」群聊标题", ["示例长名测试群甲乙丙丁戊己"],
         "文件传输助手", False, False, "标题不是精确目标名"),
        ("群聊: 标题形如「名称 (12)」", ["BBB示例测试群(12)"], "BBB示例测试群", True, True, "ok"),
        ("群聊: 标题不带人数（微信 4.x 实测）", ["BBB示例测试群"], "BBB示例测试群", True, True, "ok"),
        # 2026-10-26 晚：点开后标题 OCR 也可能读错字 -> 高覆盖率 + 唯一 放行（安全闸仍在标题）
        ("私聊: 标题 OCR 读错 1 字（近似唯一通过）", ["文件传输助首"], "文件传输助手", False, True,
         "title-approx"),
        ("私聊: 标题覆盖率不足（仍 abort）", ["文件传"], "示例长名测试群甲乙丙丁戊己", False, False,
         "标题不是精确目标名"),
    ]
    out = []
    for name, texts, target, expected_group, want_ok, note in cases:
        ok, detail = reply.classify_chat_title(texts, target, expected_group=expected_group)
        out.append((name, ok, want_ok, detail))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="文件传输助手")
    ap.add_argument("--quiet", action="store_true")
    # 2026-10-27：语料很多（每张 ~3-5s），允许只跑名字含指定子串的截图（如 --only 2335）
    ap.add_argument("--only", default="", help="只跑文件名含该子串的截图")
    ap.add_argument("--max-files", type=int, default=0, help="最多跑前 N 张截图（0=不限）")
    args = ap.parse_args(argv)
    ctx = _Ctx()
    target = args.target

    print("=" * 100)
    print("selftest_rows.py  目标=%r  语料=screenshots\\*-dropdown.png + diag-*.png" % target)
    print("图标标定: avatar(colorful>=%.2f & satMean>=%.0f & satStd>=%.0f) | gray(colorful<=%.2f & satMean<=%.0f)"
          % (reply.ICON_AVATAR_MIN_COLORFUL, reply.ICON_AVATAR_MIN_SATMEAN,
             reply.ICON_AVATAR_MIN_SATSTD, reply.ICON_GRAY_MAX_COLORFUL,
             reply.ICON_GRAY_MAX_SATMEAN))
    print("面板判别: 候选行 >=%d 行带时间戳 -> conversation-list（不取候选、不点击）"
          % reply.PANEL_CONVERSATION_TIMESTAMP_ROWS)
    print("=" * 100)

    files = sorted(set(glob.glob(os.path.join(BASE_DIR, "screenshots", "*dropdown*.png"))
                       + glob.glob(os.path.join(BASE_DIR, "screenshots", "diag-*.png"))))
    if args.only:
        files = [f for f in files if args.only in os.path.basename(f)]
    if args.max_files and args.max_files > 0:
        files = files[:args.max_files]
    print("语料 %d 张" % len(files))
    summary = []
    for f in files:
        name = os.path.basename(f)
        print("\n---------- %s ----------" % name)
        verdict, reason, _lines = run_image(f, target, verbose=not args.quiet)
        print("  结论: %s  (%s)" % (verdict, reason))
        summary.append((name, verdict, reason))

    print("\n" + "=" * 100)
    print("构造用例（真实截图覆盖不到的歧义/群聊/聊天记录场景，用纯逻辑构造）")
    print("=" * 100)
    unit_summary = []
    for name, sel, expect in fabricated_cases():
        got = sel["status"] if sel["status"] != "abort" else sel["reason"]
        mark = "PASS" if got == expect else "FAIL"
        print("[%s] %-48s -> %-18s 期望=%s 候选=%s" % (
            mark, name, got, expect,
            [c.get("text") for c in (sel.get("candidates") or [])][:4]))
        unit_summary.append((name, mark, got))

    print("\n" + "=" * 100)
    print("三重校验·标题/群聊构造用例")
    print("=" * 100)
    for name, ok, want_ok, detail in title_validation_cases():
        mark = "PASS" if ok == want_ok else "FAIL"
        print("[%s] %-46s -> ok=%-5s 期望=%-5s detail=%s" % (mark, name, ok, want_ok, detail[:90]))
        unit_summary.append((name, mark, "ok=%s" % ok))

    print("\n" + "=" * 100)
    print("面板类型判别构造用例（2026-10-27 第一阶段：会话列表 vs 搜索下拉）")
    print("=" * 100)
    for name, kind, expect, evidence in panel_kind_cases():
        mark = "PASS" if kind == expect else "FAIL"
        print("[%s] %-52s -> %-18s 期望=%s 依据=%s" % (
            mark, name, kind, expect, (evidence or "")[:70]))
        unit_summary.append((name, mark, kind))

    print("\n" + "=" * 100)
    print("汇总表")
    print("=" * 100)
    print("%-46s | %-6s | %s" % ("真实截图", "结论", "理由"))
    print("-" * 100)
    for name, verdict, reason in summary:
        print("%-46s | %-6s | %s" % (name, verdict, reason))
    print("-" * 100)
    ok_cnt = sum(1 for _n, v, _r in summary if v == "OK")
    cl_cnt = sum(1 for _n, _v, r in summary if "conversation-list" in r)
    print("真实截图: %d 张，OK=%d，ABORT=%d（其中 conversation-list 判定=%d 张）" % (
        len(summary), ok_cnt, len(summary) - ok_cnt, cl_cnt))
    fails = [n for n, m, _g in unit_summary if m == "FAIL"]
    print("构造用例: %d 条，FAIL=%d%s" % (len(unit_summary), len(fails),
                                       ("  " + str(fails)) if fails else " ✅"))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
