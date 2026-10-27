# -*- coding: utf-8 -*-
"""tools/setup_password.py —— 生成 / 更新「微信远程指令」两级密码的 salt + sha256。

背景
----
config.json 的 remote.levels.L1 / L2 只保存密码哈希，不保存明文：

    sha256_hex = sha256(bytes.fromhex(salt) + password.encode("utf-8")).hexdigest()

算法与 watcher.py 里的 `sha256_pwd()` / `verify_level_password()` 完全一致。
本工具负责：随机 salt（16 字节 -> 32 位 hex）+ 计算哈希 + 写回 config.json。

用法
----
    python tools/setup_password.py                      # 交互式设置 L1、L2 两级
    python tools/setup_password.py --level L1           # 只设置一级密码
    python tools/setup_password.py --level L2           # 只设置二级密码
    python tools/setup_password.py --verify "密码" --level L1   # 只校验，不写文件（自测用）

安全约定
--------
* **只打印 salt 与 sha256，不回显明文密码**；明文只存在于本进程内存。
* 输入密码用 getpass（终端不回显），并且要输两遍确认。
* config.json 不存在时，自动从 config.example.json 复制一份再改。
* 写 config.json 一律 UTF-8 **无 BOM**（项目硬约束；带 BOM 会让 json.load 报错），
  写完重新 json.load 自校验。
* 本文件不含任何真实密码 / 哈希；公开仓库里两级密码的 salt 与 sha256 都是空字符串。
"""

import argparse
import getpass
import hashlib
import json
import os
import secrets
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
EXAMPLE_PATH = os.path.join(BASE_DIR, "config.example.json")
MIN_PASSWORD_LEN = 8


# ───────────────────────── 哈希算法（必须与 watcher.py 一致） ─────────────────────────
def sha256_pwd(salt_hex, password):
    """sha256(bytes.fromhex(salt) + password_utf8) -> 小写 hex。"""
    return hashlib.sha256(bytes.fromhex(str(salt_hex)) + str(password).encode("utf-8")).hexdigest()


def new_salt():
    """16 字节随机 salt -> 32 位小写 hex。"""
    return secrets.token_hex(16)


# ───────────────────────── config.json 读写 ─────────────────────────
def load_json(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def save_json_no_bom(path, doc):
    """写 JSON：UTF-8 无 BOM + 末尾换行；写完立刻回读校验。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    with open(tmp, "rb") as f:
        head = f.read(3)
    if head == b"\xef\xbb\xbf":
        os.remove(tmp)
        raise SystemExit("写出的 JSON 带 BOM，已中止（这不应该发生，请检查 Python 环境）")
    os.replace(tmp, path)
    # 自校验：能重新解析，不是坏 JSON
    load_json(path)


def ensure_config():
    """config.json 不存在时从 config.example.json 复制一份；已存在则原样保留。"""
    if os.path.exists(CONFIG_PATH):
        return False
    if not os.path.exists(EXAMPLE_PATH):
        raise SystemExit("找不到 config.example.json：%s" % EXAMPLE_PATH)
    doc = load_json(EXAMPLE_PATH)
    save_json_no_bom(CONFIG_PATH, doc)
    print("[setup] 已从 config.example.json 生成 config.json")
    return True


def get_levels(doc):
    remote = doc.setdefault("remote", {})
    levels = remote.setdefault("levels", {})
    for name in ("L1", "L2"):
        lv = levels.get(name)
        if not isinstance(lv, dict):
            levels[name] = {}
    return levels


DEFAULT_LABELS = {
    "L1": ("只读/低风险", ["send_file", "screenshot", "report_status", "read_text"]),
    "L2": ("高危", ["send_file", "screenshot", "report_status", "read_text",
                    "run_command", "modify_file", "delete_file", "install", "start_stop_app"]),
}


def ensure_level_meta(levels, name):
    """补齐 label / allow（新配置里可能缺，别让工具写出半截结构）。"""
    lv = levels.setdefault(name, {})
    label, allow = DEFAULT_LABELS[name]
    lv.setdefault("label", label)
    if not isinstance(lv.get("allow"), list) or not lv["allow"]:
        lv["allow"] = list(allow)
    return lv


def prompt_password(level):
    """交互式读两遍密码（不回显）；不打印明文。"""
    while True:
        pwd = getpass.getpass("请输入 %s 密码（输入时不回显）: " % level)
        if len(pwd) < MIN_PASSWORD_LEN:
            print("  [X] 太短：至少 %d 个字符（建议 12 位以上、字母+数字+符号混合）" % MIN_PASSWORD_LEN)
            continue
        pwd2 = getpass.getpass("请再输一遍 %s 密码: " % level)
        if pwd != pwd2:
            print("  [X] 两次输入不一致，重来")
            continue
        return pwd


def set_level(levels, name):
    lv = ensure_level_meta(levels, name)
    pwd = prompt_password(name)
    salt = new_salt()
    digest = sha256_pwd(salt, pwd)
    lv["salt"] = salt
    lv["sha256"] = digest
    lv.pop("_comment", None)  # 设置完成后去掉模板注释，避免 config.json 里混着说明
    print("  [OK] %s 已更新：salt=%s sha256=%s（明文不落盘）" % (name, salt, digest))
    # 内存里也不留明文
    del pwd
    return digest


# ───────────────────────── 子命令 ─────────────────────────
def cmd_verify(password, level):
    if not os.path.exists(CONFIG_PATH):
        print("[verify] 找不到 config.json（先跑一次本工具生成）", file=sys.stderr)
        return 1
    doc = load_json(CONFIG_PATH)
    levels = get_levels(doc)
    lv = levels.get(level) or {}
    salt = str(lv.get("salt") or "")
    expect = str(lv.get("sha256") or "").lower()
    if not salt or not expect:
        print("[verify] %s 还没设置密码（salt/sha256 为空）" % level)
        return 1
    got = sha256_pwd(salt, password)
    if got == expect:
        print("[verify] %s 校验通过 ✅" % level)
        return 0
    print("[verify] %s 校验失败 ❌（哈希不匹配）" % level)
    return 2


def cmd_setup(level_arg):
    ensure_config()
    doc = load_json(CONFIG_PATH)
    levels = get_levels(doc)
    names = [level_arg] if level_arg else ["L1", "L2"]
    print("=" * 68)
    print("设置远程指令密码（只保存 salt + sha256，不保存明文）")
    print("⚠ 开启 remote.enabled 之前，请确认白名单只有你自己的可信会话")
    print("=" * 68)
    for name in names:
        print("\n--- %s ---" % name)
        set_level(levels, name)
    save_json_no_bom(CONFIG_PATH, doc)
    print("\n[setup] config.json 已更新（UTF-8 无 BOM，JSON 校验通过）: %s" % CONFIG_PATH)
    print("[setup] 下一步：确认 config.json -> remote.enabled=true 并重启 watcher（toggle.ps1 on）")
    print("[setup] 自测：python tools/setup_password.py --verify \"你的密码\" --level L1")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="生成/更新远程指令两级密码的 salt+sha256（不存明文）")
    ap.add_argument("--level", choices=["L1", "L2"], default=None,
                    help="只设置/校验某一级；默认两级都设置")
    ap.add_argument("--verify", metavar="PASSWORD", default=None,
                    help="只校验指定密码是否等于该级密码，不写任何文件")
    ap.add_argument("--config", default=None, help="指定 config.json 路径（默认仓库根目录）")
    args = ap.parse_args(argv)

    global CONFIG_PATH
    if args.config:
        CONFIG_PATH = os.path.abspath(args.config)

    if args.verify is not None:
        if not args.level:
            print("--verify 必须同时指定 --level L1 或 --level L2", file=sys.stderr)
            return 1
        return cmd_verify(args.verify, args.level)
    return cmd_setup(args.level)


if __name__ == "__main__":
    sys.exit(main())
